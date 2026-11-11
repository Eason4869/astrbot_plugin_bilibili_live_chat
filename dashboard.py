"""Authenticated AstrBot plugin-page operations."""

import base64
import json
from pathlib import Path
from secrets import token_urlsafe

from .bilibili import AuthExpired, BilibiliError, uid_text
from .media import MediaError, resolve_ffmpeg
from .rooms import RoomError

NUMBER_LIMITS = {
    "WINDOW_SECONDS": (0, 3600),
    "WINDOW_EXTEND_SECONDS": (0, 3600),
    "WINDOW_MAX_SECONDS": (1, 7200),
    "ECHO_INTERVAL_SECONDS": (10, 3600),
    "ECHO_PROBABILITY": (0, 1),
    "REPLY_COOLDOWN_SECONDS": (2, 120),
    "MAX_PER_MINUTE": (1, 30),
    "MAX_REPLY_CHARS": (2, 40),
    "POLL_SECONDS": (2, 30),
    "MODEL_TIMEOUT_SECONDS": (5, 40),
}
TEXT_LIMITS = {
    "FFMPEG_PATH": 4096,
    "MODEL_PROVIDER_ID": 256,
    "ASTRBOT_PERSONA_ID": 256,
    "PERSONA_PROMPT": 4000,
    "REPLY_INSTRUCTION": 2000,
    "ECHO_INSTRUCTION": 2000,
}
PUBLIC_FIELDS = (
    "AUTH_SOURCE", "KEYWORDS", "IGNORE_UIDS", "KEYWORD_MODE",
    "ECHO_ENABLED", "BILIBOT_MEMORY_ENABLED", *TEXT_LIMITS, *NUMBER_LIMITS,
)
SCHEMA = json.loads(Path(__file__).with_name("_conf_schema.json").read_text(encoding="utf-8"))


class DashboardError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def validated_settings(payload):
    if not isinstance(payload, dict) or not payload or not set(payload) <= set(PUBLIC_FIELDS):
        raise DashboardError("包含未知或不可编辑的设置。")
    result = {}
    for key, value in payload.items():
        field = SCHEMA[key]
        kind = field["type"]
        if kind == "bool" and type(value) is not bool:
            raise DashboardError(f"请检查「{field['description']}」的开关值。")
        if kind == "list":
            if not isinstance(value, list) or len(value) > 100 or any(
                not isinstance(item, str) or len(item) > 80 for item in value
            ):
                raise DashboardError(f"请检查「{field['description']}」的列表。")
            value = list(dict.fromkeys(item.strip() for item in value if item.strip()))
            if key == "IGNORE_UIDS" and any(not uid_text(item) for item in value):
                raise DashboardError("忽略名单里的 UID 必须为正整数。")
        if key in NUMBER_LIMITS:
            low, high = NUMBER_LIMITS[key]
            types = (int, float) if kind == "float" else (int,)
            if type(value) not in types or not low <= value <= high:
                raise DashboardError(f"「{field['description']}」应在 {low} 到 {high} 之间。")
        if key in TEXT_LIMITS:
            if not isinstance(value, str) or len(value) > TEXT_LIMITS[key]:
                raise DashboardError(f"请检查「{field['description']}」的文本长度。")
            value = value.strip()
        if key == "AUTH_SOURCE" and value not in ("local", "bilibot"):
            raise DashboardError("请选择有效的登录来源。")
        result[key] = value
    return result


class Dashboard:
    def __init__(self, plugin):
        self.plugin = plugin

    def _check_open(self):
        if self.plugin.closing:
            raise DashboardError("插件已停用或正在重载。", 503)

    def _account(self, source):
        credentials = self.plugin._credentials(source)
        configured = bool(credentials and credentials.ready)
        return {"configured": configured, "uid": credentials.uid if configured else ""}

    def snapshot(self):
        self._check_open()
        bot = self.plugin
        try:
            ffmpeg = {"available": True, "path": resolve_ffmpeg(bot.config.get("FFMPEG_PATH", "")), "message": ""}
        except MediaError as exc:
            ffmpeg = {"available": False, "path": "", "message": str(exc)}
        return {
            "environment": {"ffmpeg": ffmpeg},
            "settings": {key: bot.config.get(key, SCHEMA[key]["default"]) for key in PUBLIC_FIELDS},
            "account": {
                "source": bot._auth_source(),
                "local": self._account("local"),
                "bilibot": self._account("bilibot"),
                "memory_enabled": bot.memory_enabled(),
                "memory_available": bot.link.memory_available(),
            },
            "rooms": bot.rooms.snapshot(),
        }

    async def state(self):
        self._check_open()
        context = self.plugin.context
        providers = []
        for provider in context.get_all_providers():
            meta = provider.meta()
            providers.append({"id": meta.id, "label": meta.model or meta.id})
        speech_providers = []
        for provider in getattr(context, "get_all_stt_providers", list)():
            meta = provider.meta()
            speech_providers.append({"id": meta.id, "label": meta.model or meta.id})
        personas = await context.persona_manager.get_all_personas()
        return {
            **self.snapshot(),
            "providers": providers,
            "speech_providers": speech_providers,
            "personas": [persona.persona_id for persona in personas],
        }

    async def save(self, payload):
        updates = validated_settings(payload)
        bot = self.plugin
        async with bot.command_lock:
            self._check_open()
            current = self.snapshot()["settings"]
            updates = {key: value for key, value in updates.items() if value != current[key]}
            if updates and not bot.save_config(updates):
                raise DashboardError("设置保存失败，请重试。", 500)
            if "AUTH_SOURCE" in updates:
                bot.challenge = None
            if updates.keys() - {"BILIBOT_MEMORY_ENABLED"}:
                await bot.rooms.reconcile(restart=True)
            return self.snapshot()

    async def listen(self, room, start):
        if type(start) is not bool:
            raise DashboardError("请选择开始或停止监听。")
        return await self._change_room(room, "start" if start else "stop")

    async def room_change(self, room, action):
        if action not in ("add", "remove"):
            raise DashboardError("请选择添加或移除直播间。")
        return await self._change_room(room, action)

    async def media_settings(self, room, settings):
        async with self.plugin.command_lock:
            self._check_open()
            try:
                await self.plugin.rooms.set_media(room, settings)
            except RoomError as exc:
                raise DashboardError(str(exc)) from exc
            return self.snapshot()

    async def start_selfcheck(self, room):
        async with self.plugin.command_lock:
            self._check_open()
            if not isinstance(room, str):
                raise DashboardError("请选择默认配置或一个直播间。")
            try:
                return self.plugin.diagnostics.start(room)
            except MediaError as exc:
                raise DashboardError(str(exc)) from exc

    async def selfcheck_status(self):
        self._check_open()
        return self.plugin.diagnostics.snapshot()

    async def _change_room(self, room, action):
        bot = self.plugin
        async with bot.command_lock:
            self._check_open()
            try:
                await bot.rooms.change(room, action)
            except RoomError as exc:
                raise DashboardError(str(exc)) from exc
            return self.snapshot()

    async def generate_qr(self):
        bot = self.plugin
        async with bot.command_lock:
            self._check_open()
            if bot._auth_source() != "local":
                raise DashboardError("请先切换到独立扫码登录。")
            token = token_urlsafe(32)
            image = await bot._login(("web", token), replace_web=True)
            if not isinstance(image, bytes):
                raise DashboardError(image)
            return {
                "challenge": token,
                "image": "data:image/png;base64," + base64.b64encode(image).decode("ascii"),
            }

    async def poll_qr(self, token):
        if not isinstance(token, str) or not token or len(token) > 64:
            raise DashboardError("二维码已失效，请重新生成。")
        bot = self.plugin
        async with bot.command_lock:
            self._check_open()
            owner = ("web", token)
            if not bot.challenge or bot.challenge.owner != owner:
                raise DashboardError("二维码已失效，请重新生成。")
            phase, message = await bot._poll_login(owner)
            if phase == "error":
                raise DashboardError(message)
            return {"phase": phase}


def _transport():
    try:
        from astrbot.api.web import json_response, request
    except ImportError:
        try:
            from quart import g, jsonify, request
        except ImportError:
            return None

        async def body():
            return await request.get_json(silent=True)

        def reply(data, status):
            response = jsonify(data)
            response.status_code = status
            response.headers["Cache-Control"] = "no-store"
            return response

        return lambda: getattr(g, "username", None), body, reply

    async def body():
        return await request.json(default=None)

    def reply(data, status):
        return json_response(data, status_code=status, headers={"Cache-Control": "no-store"})

    return lambda: request.username, body, reply


def register_webui(plugin, name):
    register = getattr(plugin.context, "register_web_api", None)
    transport = _transport() if callable(register) else None
    if transport is None:
        return
    username, body, reply = transport
    dashboard = Dashboard(plugin)

    async def run(action, *args):
        if not username():
            return reply({"status": "error", "message": "请先登录 AstrBot 管理面板。"}, 401)
        try:
            return reply({"status": "ok", "data": await action(*args)}, 200)
        except DashboardError as exc:
            return reply({"status": "error", "message": str(exc)}, exc.status)
        except AuthExpired:
            message = "B 站登录未生效或已失效，请重新扫码或检查 BiliBot。"
            return reply({"status": "error", "message": message}, 400)
        except BilibiliError:
            return reply({"status": "error", "message": "B 站暂时不可用，请稍后重试。"}, 502)

    async def state():
        return await run(dashboard.state)

    async def status():
        async def read():
            return dashboard.snapshot()

        return await run(read)

    async def settings():
        async def save():
            return await dashboard.save(await body())

        return await run(save)

    async def listen():
        async def change():
            data = await body()
            if not isinstance(data, dict):
                raise DashboardError("请选择直播间。")
            return await dashboard.listen(data.get("room"), data.get("running"))

        return await run(change)

    async def rooms():
        async def change():
            data = await body()
            if not isinstance(data, dict):
                raise DashboardError("请选择直播间。")
            return await dashboard.room_change(data.get("room"), data.get("action"))

        return await run(change)

    async def room_media():
        async def change():
            data = await body()
            if not isinstance(data, dict):
                raise DashboardError("请选择直播间。")
            return await dashboard.media_settings(data.get("room"), data.get("settings"))

        return await run(change)

    async def generate():
        return await run(dashboard.generate_qr)

    async def selfcheck_start():
        async def start():
            data = await body()
            room = data.get("room", "") if isinstance(data, dict) else None
            return await dashboard.start_selfcheck(room)

        return await run(start)

    async def selfcheck_status():
        return await run(dashboard.selfcheck_status)

    async def poll():
        async def check():
            data = await body()
            return await dashboard.poll_qr(data.get("challenge") if isinstance(data, dict) else None)

        return await run(check)

    for endpoint, method, handler in (
        ("state", "GET", state), ("status", "GET", status), ("settings", "POST", settings),
        ("rooms", "POST", rooms), ("rooms/media", "POST", room_media), ("listen", "POST", listen),
        ("login/generate", "POST", generate), ("login/poll", "POST", poll),
        ("selfcheck/start", "POST", selfcheck_start), ("selfcheck", "GET", selfcheck_status),
    ):
        register(f"/{name}/{endpoint}", handler, [method], "Bilibili live chat console")
