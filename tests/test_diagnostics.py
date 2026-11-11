import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_dashboard import Context
from test_plugin import Config, load_plugin, wait_for

from astrbot_plugin_bilibili_live_chat.diagnostics import EnvironmentCheck, safe_error


class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def setup_check(self, **updates):
        context = Context()
        context.get_using_stt_provider = lambda: SimpleNamespace(get_text=AsyncMock(return_value="你好，测试语音"))
        config = Config(MODEL_PROVIDER_ID="chat-model", **updates)
        plugin = load_plugin().LiveChatPlugin(context, config)
        self.addAsyncCleanup(plugin.terminate)
        return plugin

    async def test_checks_are_independent_and_use_real_provider_input(self):
        plugin = self.setup_check()
        check = plugin.diagnostics
        self.assertIsInstance(check, EnvironmentCheck)
        code = "5827"
        with patch("astrbot_plugin_bilibili_live_chat.diagnostics.probe_ffmpeg", AsyncMock(side_effect=PermissionError("no execution permission"))), \
             patch("astrbot_plugin_bilibili_live_chat.diagnostics.test_image", return_value=(code, "base64://image")):
            plugin.context.llm_generate = AsyncMock(return_value=SimpleNamespace(completion_text="5827"))
            check.start("")
            await wait_for(lambda: not check.snapshot()["running"])
        rows = check.snapshot()["items"]
        self.assertEqual([row["status"] for row in rows], ["error", "ok", "ok"])
        self.assertIn("PermissionError", rows[0]["detail"])
        self.assertIn("你好", rows[1]["detail"])
        self.assertEqual(plugin.context.llm_generate.await_args.kwargs["image_urls"], ["base64://image"])
        self.assertNotIn(code, plugin.context.llm_generate.await_args.kwargs["prompt"])

    async def test_room_overrides_select_the_effective_providers(self):
        plugin = self.setup_check(
            ROOM_IDS=["12"], ROOM_MEDIA={"12": {"stt_provider": "room-stt", "vision_provider": "room-vision"}},
        )
        speech = SimpleNamespace(get_text=AsyncMock(return_value="听到测试"))
        plugin.context.get_provider_by_id = lambda name: speech if name == "room-stt" else object()
        plugin.context.llm_generate = AsyncMock(return_value=SimpleNamespace(completion_text="1234"))
        with patch("astrbot_plugin_bilibili_live_chat.diagnostics.probe_ffmpeg", AsyncMock(return_value="FFmpeg OK")), \
             patch("astrbot_plugin_bilibili_live_chat.diagnostics.test_image", return_value=("1234", "base64://image")):
            plugin.diagnostics.start("12")
            await wait_for(lambda: not plugin.diagnostics.snapshot()["running"])
        self.assertEqual(plugin.context.llm_generate.await_args.kwargs["chat_provider_id"], "room-vision")
        speech.get_text.assert_awaited_once()
        self.assertEqual(plugin.diagnostics.snapshot()["room"], "12")

    async def test_repeated_start_does_not_repeat_model_calls_and_shutdown_cancels(self):
        plugin = self.setup_check()
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def blocked(_):
            entered.set()
            try:
                await asyncio.sleep(20)
            finally:
                cancelled.set()

        with patch("astrbot_plugin_bilibili_live_chat.diagnostics.probe_ffmpeg", blocked):
            check = plugin.diagnostics
            check.start("")
            await wait_for(entered.is_set)
            first = check.task
            check.start("")
            self.assertIs(check.task, first)
            await plugin.terminate()
        self.assertTrue(cancelled.is_set())
        self.assertFalse(check.snapshot()["running"])

    async def test_text_only_model_is_not_reported_as_vision_success(self):
        plugin = self.setup_check()
        plugin.context.llm_generate = AsyncMock(return_value=SimpleNamespace(completion_text="我看不到图片"))
        with patch("astrbot_plugin_bilibili_live_chat.diagnostics.probe_ffmpeg", AsyncMock(return_value="OK")), \
             patch("astrbot_plugin_bilibili_live_chat.diagnostics.test_image", return_value=("1234", "base64://image")):
            plugin.diagnostics.start("")
            await wait_for(lambda: not plugin.diagnostics.snapshot()["running"])
        self.assertEqual(plugin.diagnostics.snapshot()["items"][2]["status"], "error")
        self.assertIn("未正确", plugin.diagnostics.snapshot()["items"][2]["detail"])

    def test_error_detail_hides_keys_authorization_and_url_secrets(self):
        provider = SimpleNamespace(provider_config={"api_key": "secret-test-key", "nested": {"password": "password-value"}})
        reason = safe_error(
            RuntimeError("401 api_key=secret-test-key Authorization: Bearer abc123 https://name:pass@api.example.com/v1?token=private password-value"),
            {}, provider,
        )
        for secret in ("secret-test-key", "abc123", "private", "name:pass", "password-value"):
            self.assertNotIn(secret, reason)
        self.assertIn("401", reason)
        self.assertIn("api.example.com/v1", reason)

    async def test_a_timed_out_provider_does_not_stop_the_next_check(self):
        plugin = self.setup_check()
        async def slow_text(_):
            await asyncio.sleep(1)
            return "你好"

        plugin.context.get_using_stt_provider = lambda: SimpleNamespace(get_text=slow_text)
        plugin.context.llm_generate = AsyncMock(return_value=SimpleNamespace(completion_text="1234"))
        with patch("astrbot_plugin_bilibili_live_chat.diagnostics.CHECK_TIMEOUT", 0.02), \
             patch("astrbot_plugin_bilibili_live_chat.diagnostics.probe_ffmpeg", AsyncMock(return_value="OK")), \
             patch("astrbot_plugin_bilibili_live_chat.diagnostics.test_image", return_value=("1234", "base64://image")):
            plugin.diagnostics.start("")
            await wait_for(lambda: not plugin.diagnostics.snapshot()["running"])
        self.assertEqual([row["status"] for row in plugin.diagnostics.snapshot()["items"]], ["ok", "error", "ok"])
