"""Durable listening preferences and one supervised runtime per enabled room."""

import asyncio
from dataclasses import asdict, dataclass

from .bilibili import AuthExpired, BilibiliError, RateLimited, uid_text
from .media import MediaError, MediaSettings
from .room_settings import (
    DIALOGUE_FIELDS,
    SCHEMA,
    RoomSettingsError,
    saved_overrides,
    validate_dialogue,
)
from .session import LiveSession

MAX_ROOMS = 20
RETRY_BASE = 5
RETRY_MAX = 60


class RoomError(Exception):
    """A room-management operation could not be persisted or applied."""


class RoomUnavailable(Exception):
    def __init__(self, phase, message):
        super().__init__(message)
        self.phase = phase


@dataclass
class RoomRuntime:
    task: asyncio.Task | None = None
    live: LiveSession | None = None
    phase: str = "starting"
    issue: str = ""


def _ids(values):
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(room for value in values if (room := uid_text(value))))


class RoomManager:
    def __init__(self, config, save, create):
        self.config = config
        self.save = save
        self.create = create
        self.workers: dict[str, RoomRuntime] = {}
        self.startup = asyncio.Semaphore(3)
        self.closed = False

    def configured(self):
        rooms = _ids(self.config.get("ROOM_IDS"))
        legacy = uid_text(self.config.get("ROOM_ID"))
        return rooms or ([legacy] if legacy else [])

    def enabled(self):
        return set(_ids(self.config.get("ENABLED_ROOMS"))) & set(self.configured())

    def migrate(self):
        if uid_text(self.config.get("ROOM_ID")):
            self.save({"ROOM_IDS": self.configured(), "ROOM_ID": ""})

    def live(self, room):
        worker = self.workers.get(room)
        return worker.live if worker else None

    def _media_config(self):
        value = self.config.get("ROOM_MEDIA")
        return value if isinstance(value, dict) else {}

    def media_settings(self, room):
        return MediaSettings.parse(self._media_config().get(room))

    def _dialogue_config(self):
        value = self.config.get('ROOM_DIALOGUE')
        return value if isinstance(value, dict) else {}

    def dialogue_overrides(self, room):
        return saved_overrides(self._dialogue_config().get(room))

    def effective_config(self, room):
        shared = {key: self.config.get(key, SCHEMA[key]['default']) for key in DIALOGUE_FIELDS}
        effective = {**shared, **self.dialogue_overrides(room)}
        effective['ECHO_ENABLED'] = self.config.get('ECHO_ENABLED') is True and effective['ECHO_ENABLED'] is True
        return effective

    async def set_dialogue(self, value, payload):
        room = uid_text(value)
        if room not in self.configured():
            raise RoomError('找不到这个直播间。')
        try:
            changes = validate_dialogue(payload)
        except RoomSettingsError as exc:
            raise RoomError(str(exc)) from exc
        current = self.dialogue_overrides(room)
        updated = {key: value for key, value in {**current, **changes}.items() if value is not None}
        if current != updated:
            if not self.save({'ROOM_DIALOGUE': {**self._dialogue_config(), room: updated}}):
                raise RoomError('保存房间对话设置失败，请重试。')
            await self.reconcile(restart_room=room)

    async def copy_settings(self, source, destination):
        source, destination = uid_text(source), uid_text(destination)
        if source not in self.configured() or destination not in self.configured() or source == destination:
            raise RoomError('请选择两个不同的已添加直播间。')
        updates = {
            'ROOM_DIALOGUE': {**self._dialogue_config(), destination: self.dialogue_overrides(source)},
            'ROOM_MEDIA': {**self._media_config(), destination: asdict(self.media_settings(source))},
        }
        if not self.save(updates):
            raise RoomError('复制房间设置失败，请重试。')
        await self.reconcile(restart_room=destination)

    def _preview_config(self):
        value = self.config.get('ROOM_PREVIEW')
        return value if isinstance(value, dict) else {}

    def preview_enabled(self, room):
        return self._preview_config().get(room) is True

    async def set_preview(self, value, enabled):
        room = uid_text(value)
        if room not in self.configured() or type(enabled) is not bool:
            raise RoomError('请选择有效的直播间和预览模式。')
        if enabled != self.preview_enabled(room):
            values = {**self._preview_config(), room: enabled}
            if not self.save({'ROOM_PREVIEW': values}):
                raise RoomError('保存预览模式失败，请重试。')
            await self.reconcile(restart_room=room)

    def snapshot(self):
        enabled = self.enabled()
        result = []
        for room in self.configured():
            worker = self.workers.get(room)
            live = worker.live if worker else None
            running = bool(live and live.running)
            phase = worker.phase if worker else ("starting" if room in enabled else "disabled")
            issues = [worker.issue] if worker and worker.issue else []
            if running:
                connected = live.last_snapshot is not None or live.receiver.source == 'websocket'
                phase = "retrying" if live.poll_error else ("listening" if connected else "starting")
                issues = [issue for issue in (live.poll_error, live.model_error, live.send_error) if issue]
            result.append({
                "id": room,
                "canonical": str(live.room) if live else "",
                "enabled": room in enabled,
                "running": running,
                "phase": phase,
                "received": live.received if live else 0,
                "replies": live.replies if live else 0,
                "preview": self.preview_enabled(room),
                "previews": live.previews if live else 0,
                'dialogue': {'overrides': self.dialogue_overrides(room), 'effective': self.effective_config(room)},
                'listener': live.receiver.snapshot() if running else {
                    'configured': self.effective_config(room)['LISTEN_MODE'], 'source': '', 'phase': 'disabled',
                    'reason': '', 'retry_in': 0, 'heartbeat_age_ms': None, 'attempts': 0, 'fallbacks': 0, 'duplicates': 0,
                },
                "remaining": max(0, int(live.conversation.window_until - live.clock())) if running else 0,
                "issues": issues,
                "media": {
                    "settings": asdict(self.media_settings(room)),
                    **(live.media.snapshot() if running and live.media else {
                        "transcripts": 0, "frames": 0, "issues": [],
                    }),
                },
            })
        return result

    async def change(self, value, action):
        room = uid_text(value)
        if not room:
            raise RoomError("请输入有效的数字房间号。")
        rooms = self.configured()
        enabled = self.enabled()
        if action == "add":
            if room in rooms:
                raise RoomError("这个直播间已在列表中。")
            if len(rooms) >= MAX_ROOMS:
                raise RoomError(f"控制台最多可添加 {MAX_ROOMS} 个直播间。")
            rooms.append(room)
        elif room not in rooms:
            raise RoomError("找不到这个直播间，请先在控制台添加。")
        elif action == "remove":
            rooms.remove(room)
            enabled.discard(room)
        elif action == "start":
            enabled.add(room)
        elif action == "stop":
            enabled.discard(room)
        else:
            raise RoomError("未知的直播间操作。")
        ordered_enabled = [item for item in rooms if item in enabled]
        if rooms != self.configured() or enabled != self.enabled():
            updates = {"ROOM_IDS": rooms, "ENABLED_ROOMS": ordered_enabled, "ROOM_ID": ""}
            if action == "remove" and room in self._media_config():
                updates["ROOM_MEDIA"] = {key: value for key, value in self._media_config().items() if key != room}
            if action == 'remove' and room in self._preview_config():
                updates['ROOM_PREVIEW'] = {key: value for key, value in self._preview_config().items() if key != room}
            if action == 'remove' and room in self._dialogue_config():
                updates['ROOM_DIALOGUE'] = {key: value for key, value in self._dialogue_config().items() if key != room}
            if not self.save(updates):
                raise RoomError("保存直播间设置失败，请重试。")
        await self.reconcile()

    async def set_media(self, value, payload):
        room = uid_text(value)
        if room not in self.configured():
            raise RoomError("找不到这个直播间。")
        if not isinstance(payload, dict) or not payload:
            raise RoomError("请选择要保存的音视频设置。")
        current = self.media_settings(room)
        try:
            updated = MediaSettings.parse({**asdict(current), **payload}, strict=True)
        except MediaError as exc:
            raise RoomError(str(exc)) from exc
        if current != updated:
            media = {**self._media_config(), room: asdict(updated)}
            if not self.save({"ROOM_MEDIA": media}):
                raise RoomError("保存音视频设置失败，请重试。")
            await self.reconcile(restart_room=room)

    async def reconcile(self, *, restart=False, restart_room=None):
        if self.closed:
            return
        enabled = self.enabled()
        stale = [room for room in self.workers if restart or room == restart_room or room not in enabled]
        try:
            await self._cancel(stale)
        finally:
            # The preference is already committed even if the WebUI request disconnects.
            if not self.closed:
                self._start_enabled()

    def _start_enabled(self):
        enabled = self.enabled()
        for room in self.configured():
            if room in enabled and room not in self.workers:
                worker = self.workers[room] = RoomRuntime()
                worker.task = asyncio.create_task(self._watch(room, worker), name=f"bilibili-live-chat.room:{room}")

    async def close(self):
        self.closed = True
        await self._cancel(list(self.workers))

    async def _cancel(self, rooms):
        tasks = [self.workers.pop(room).task for room in rooms]
        for task in tasks:
            task.cancel()
        if tasks:
            drained = asyncio.gather(*tasks, return_exceptions=True)
            try:
                await asyncio.shield(drained)
            except asyncio.CancelledError:
                await drained
                raise

    async def _watch(self, room, worker):
        delay = RETRY_BASE
        while not self.closed and room in self.enabled():
            worker.phase = "starting"
            worker.issue = ""
            live = None
            try:
                async with self.startup:
                    live = await self.create(room)
                for other in self.workers.values():
                    active = other.live
                    if other is not worker and active and active.running and active.room == live.room:
                        raise RoomUnavailable("duplicate", f"与另一个已监听的房间指向同一直播间（{live.room}）。")
                worker.live = live
                await live.start()
                completed, _ = await asyncio.wait(live.tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in completed:
                    if not task.cancelled() and task.exception():
                        raise RoomUnavailable("retrying", "监听任务异常，稍后重试。")
                if live.auth_expired:
                    raise AuthExpired()
                worker.phase = "retrying"
                worker.issue = live.stop_reason or "连接中断，正在重试。"
                if live.last_snapshot is not None:
                    delay = RETRY_BASE
            except RoomUnavailable as exc:
                worker.phase, worker.issue = exc.phase, str(exc)
            except AuthExpired:
                worker.phase, worker.issue = "waiting_login", "登录已失效，请重新扫码或检查 BiliBot。"
            except RateLimited:
                worker.phase, worker.issue = "retrying", "B 站暂时限流，稍后重试。"
                delay = RETRY_MAX
            except BilibiliError:
                worker.phase, worker.issue = "retrying", "B 站暂时不可用，稍后重试。"
            except Exception:  # noqa: BLE001
                worker.phase, worker.issue = "retrying", "监听任务异常，稍后重试。"
            finally:
                if live:
                    await live.stop()
                    worker.live = None
            await asyncio.sleep(delay)
            delay = min(RETRY_MAX, delay * 2)
