import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_plugin import Config, Event, FakeClient, load_plugin, wait_for

from astrbot_plugin_bilibili_live_chat.bilibili import Credentials
from astrbot_plugin_bilibili_live_chat.conversation import Danmaku, Job
from astrbot_plugin_bilibili_live_chat.dashboard import Dashboard, DashboardError


class LoginClient(FakeClient):
    calls = 0

    async def qr_poll(self, key):
        type(self).calls += 1
        if self.calls <= 2:
            return ("waiting" if self.calls == 1 else "scanned"), None
        return "success", Credentials("secret-cookie", "secret-csrf", "42")


class Context:
    def __init__(self):
        self.star = None
        self.routes = {}
        self.persona_manager = self
        self.last_model_args = None

    def get_registered_star(self, name):
        return self.star

    def get_all_providers(self):
        return [SimpleNamespace(meta=lambda: SimpleNamespace(id="chat-model", model="Chat Model"))]

    def get_provider_by_id(self, provider):
        return object() if provider == "chat-model" else None

    async def get_all_personas(self):
        return [await self.get_persona("host")]

    async def get_persona(self, persona):
        if persona != "host":
            raise ValueError()
        return SimpleNamespace(persona_id="host", system_prompt="AstrBot 人格原文")

    async def llm_generate(self, **kwargs):
        self.last_model_args = kwargs
        return SimpleNamespace(completion_text='{"reply":"你好"}')

    def register_web_api(self, route, handler, methods, desc):
        self.routes[(route, methods[0])] = handler


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = load_plugin()
        self.ctx = Context()
        self.config = Config(ROOM_ID="12", MODEL_PROVIDER_ID="chat-model", KEYWORDS=["自定义"])
        self.plugin = self.module.LiveChatPlugin(self.ctx, self.config)
        self.addAsyncCleanup(self.plugin.terminate)

    async def test_browser_qr_confirms_automatically_and_publishes_only_status(self):
        ui = Dashboard(self.plugin)
        LoginClient.calls = 0
        with patch.object(self.module, "BilibiliClient", LoginClient), \
             patch.object(self.module, "render_qr", return_value=b"test image"):
            started = await ui.generate_qr()
            token = started["challenge"]
            self.assertNotEqual(token, "key")
            self.assertTrue(started["image"].startswith("data:image/png;base64,"))
            with self.assertRaises(DashboardError):
                await ui.poll_qr("wrong-token")
            self.assertEqual(LoginClient.calls, 0)
            for status in ("waiting", "scanned", "success"):
                self.assertEqual((await ui.poll_qr(token))["phase"], status)
            with self.assertRaises(DashboardError):
                await ui.poll_qr(token)
        state = await ui.state()
        self.assertEqual(state["account"]["local"]["uid"], "42")
        self.assertEqual(self.config.saved["SESSDATA"], "secret-cookie")
        self.assertNotIn("secret-cookie", str(state))
        self.assertNotIn("secret-csrf", str(state))
        self.assertNotIn("SESSDATA", state["settings"])
        self.assertEqual(state["personas"], ["host"])
        self.assertNotIn("AstrBot 人格原文", str(state))

    async def test_web_replacement_rejects_old_qr_and_cannot_claim_chat_challenge(self):
        ui = Dashboard(self.plugin)
        with patch.object(self.module, "BilibiliClient", FakeClient), \
             patch.object(self.module, "render_qr", return_value=b"test image"):
            first = await ui.generate_qr()
            second = await ui.generate_qr()
            self.assertNotEqual(first["challenge"], second["challenge"])
            with self.assertRaises(DashboardError):
                await ui.poll_qr(first["challenge"])
            self.assertEqual((await ui.poll_qr(second["challenge"]))["phase"], "success")
            self.assertIsInstance(await self.plugin._login(("chat:1", "room")), bytes)
            with self.assertRaises(DashboardError):
                await ui.generate_qr()

    async def test_source_and_memory_are_independent_and_protected_settings_rejected(self):
        ui = Dashboard(self.plugin)
        self.ctx.star = SimpleNamespace(
            activated=True,
            star_cls=SimpleNamespace(config=Config(SESSDATA="shared-cookie", BILI_JCT="csrf", DEDE_USER_ID="7")),
        )
        with self.assertRaises(DashboardError):
            await ui.save({"SESSDATA": "bad", "KEYWORDS": ["new"]})
        self.assertEqual(self.config["KEYWORDS"], ["自定义"])
        state = await ui.save({"AUTH_SOURCE": "bilibot"})
        self.assertEqual(state["account"]["source"], "bilibot")
        self.assertEqual(state["account"]["bilibot"]["uid"], "7")
        self.assertFalse(state["account"]["memory_enabled"])
        state = await ui.save({"BILIBOT_MEMORY_ENABLED": True})
        self.assertEqual(state["account"]["source"], "bilibot")
        self.assertTrue(state["account"]["memory_enabled"])
        self.assertNotIn("SESSDATA", self.config)

    async def test_listen_uses_astrbot_persona_and_live_addendum(self):
        self.config.update(SESSDATA="cookie", BILI_JCT="csrf", DEDE_USER_ID="42")
        ui = Dashboard(self.plugin)
        await ui.save({"ASTRBOT_PERSONA_ID": "host", "PERSONA_PROMPT": "直播补充"})
        with patch.object(self.module, "BilibiliClient", FakeClient):
            state = await ui.listen("12", True)
            await wait_for(lambda: self.plugin.rooms.live("12"))
        self.assertTrue(state["rooms"][0]["enabled"])
        job = Job(Danmaku("1", "7", "观众", "自定义"), "wake", 0, 1, ())
        await self.plugin.rooms.live("12").generate(job)
        prompt = self.ctx.last_model_args["system_prompt"]
        self.assertLess(prompt.index("AstrBot 人格原文"), prompt.index("直播补充"))
        self.assertFalse((await ui.listen("12", False))["rooms"][0]["enabled"])

    async def test_memory_switch_applies_without_restarting(self):
        self.config.update(SESSDATA="cookie", BILI_JCT="csrf", DEDE_USER_ID="42")
        ui = Dashboard(self.plugin)
        with patch.object(self.module, "BilibiliClient", FakeClient):
            await ui.listen("12", True)
            await wait_for(lambda: self.plugin.rooms.live("12"))
        live = self.plugin.rooms.live("12")
        job = Job(Danmaku("1", "7", "观众", "自定义"), "wake", 0, 1, ())
        with patch.object(self.plugin.link, "recall", AsyncMock(return_value=["同直播间记忆"])) as recall:
            await ui.save({"BILIBOT_MEMORY_ENABLED": True})
            await live.generate(job)
            self.assertIn("同直播间记忆", self.ctx.last_model_args["prompt"])
            await ui.save({"BILIBOT_MEMORY_ENABLED": False})
            await live.generate(job)
            self.assertNotIn("同直播间记忆", self.ctx.last_model_args["prompt"])
            self.assertEqual(recall.await_count, 1)
        self.assertIs(self.plugin.rooms.live("12"), live)
        self.assertTrue(live.running)

    async def test_registered_routes_require_a_dashboard_identity(self):
        username = [None]
        body = AsyncMock(return_value={"KEYWORDS": ["另一个词"]})
        transport = (lambda: username[0], body, lambda data, status: (status, data))
        with patch("astrbot_plugin_bilibili_live_chat.dashboard._transport", return_value=transport):
            await self.plugin.initialize()
        route = self.ctx.routes[(f"/{self.module.PLUGIN_NAME}/settings", "POST")]
        status, _ = await route()
        self.assertEqual(status, 401)
        body.assert_not_awaited()
        self.assertEqual(self.config["KEYWORDS"], ["自定义"])
        username[0] = "dashboard-admin"
        status, response = await route()
        self.assertEqual(status, 200)
        self.assertEqual(response["data"]["settings"]["KEYWORDS"], ["另一个词"])

    async def test_slash_with_no_action_provides_status_and_group_guidance(self):
        event = Event()
        event.get_message_str = lambda: "/直播对话"
        direct = [result async for result in self.plugin.command(event)]
        literal = [result async for result in self.plugin.literal_command(event)]
        self.assertEqual(direct, literal)
        self.assertIn("未启动", literal[0])
        group = Event(group=True)
        results = [result async for result in self.plugin.command(group)]
        self.assertIn("管理员私聊", results[0])
        self.assertNotIn("登录来源", results[0])

    async def test_custom_ffmpeg_path_is_saved_and_used_by_the_media_runtime(self):
        self.config.update(
            SESSDATA="cookie", BILI_JCT="csrf", DEDE_USER_ID="42",
            ROOM_MEDIA={"12": {"audio": True}},
        )
        ui = Dashboard(self.plugin)
        with TemporaryDirectory() as folder:
            binary = Path(folder) / "ffmpeg"
            binary.touch()
            binary.chmod(0o755)
            with patch.object(self.module, "BilibiliClient", FakeClient):
                snapshot = await ui.save({"FFMPEG_PATH": str(binary)})
                self.assertEqual(snapshot["environment"]["ffmpeg"]["path"], str(binary.resolve()))
                self.assertTrue(snapshot["environment"]["ffmpeg"]["available"])
                await ui.listen("12", True)
                await wait_for(lambda: self.plugin.rooms.live("12"))
            self.assertEqual(self.plugin.rooms.live("12").media.ffmpeg_path, str(binary))
            self.assertEqual(self.config.saved["FFMPEG_PATH"], str(binary))
