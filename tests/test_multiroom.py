import asyncio
import unittest
from unittest.mock import patch

from test_dashboard import Context
from test_plugin import Config, FakeClient, load_plugin
from test_plugin import wait_for as until

from astrbot_plugin_bilibili_live_chat.bilibili import BilibiliError
from astrbot_plugin_bilibili_live_chat.conversation import (
    Conversation,
    ConversationSettings,
)


class MultiClient(FakeClient):
    async def resolve_room(self, room):
        return 100 if room in (12, 100) else room


class MultiRoomTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = load_plugin()
        self.context = Context()
        self.config = Config(
            ROOM_IDS=["12", "13", "14"], ENABLED_ROOMS=["12", "13"], KEYWORDS=["自定义"],
            MODEL_PROVIDER_ID="chat-model", SESSDATA="cookie", BILI_JCT="csrf", DEDE_USER_ID="42",
        )

    def instance(self, config):
        plugin = self.module.LiveChatPlugin(self.context, config)
        self.addAsyncCleanup(plugin.terminate)
        return plugin

    async def test_restore_enabled_rooms_preserves_independence_across_reload_and_settings_save(self):
        from astrbot_plugin_bilibili_live_chat.dashboard import Dashboard

        with patch.object(self.module, "BilibiliClient", MultiClient):
            plugin = self.instance(self.config)
            await plugin.initialize()
            await until(lambda: plugin.rooms.live("12") and plugin.rooms.live("13"))
            self.assertIsNot(plugin.rooms.live("12").conversation, plugin.rooms.live("13").conversation)
            self.assertIsNone(plugin.rooms.live("14"))
            prior = plugin.rooms.live("12")
            await Dashboard(plugin).save({"KEYWORDS": ["新关键词"]})
            await until(lambda: plugin.rooms.live("12") and plugin.rooms.live("12") is not prior)
            self.assertEqual(plugin.rooms.live("12").conversation.settings.keywords, ("新关键词",))
            self.assertEqual(self.config.saved["ENABLED_ROOMS"], ["12", "13"])
            await Dashboard(plugin).listen("13", False)
            await plugin.terminate()
            persisted = Config(self.config.saved)
            self.assertEqual(persisted["ENABLED_ROOMS"], ["12"])
            restarted = self.instance(persisted)
            await restarted.initialize()
            await until(lambda: restarted.rooms.live("12"))
            self.assertIsNone(restarted.rooms.live("13"))
            self.assertIsNone(restarted.rooms.live("14"))

    async def test_legacy_room_migrates_without_auto_enabling(self):
        plugin = self.instance(Config(ROOM_ID=12, ROOM_IDS=[], ENABLED_ROOMS=[]))
        await plugin.initialize()
        self.assertEqual(plugin.config.saved["ROOM_IDS"], ["12"])
        self.assertEqual(plugin.config.saved["ROOM_ID"], "")
        self.assertEqual(plugin.rooms.snapshot()[0]["phase"], "disabled")

    async def test_enabled_intent_survives_missing_login_and_recovers_after_it_is_available(self):
        from astrbot_plugin_bilibili_live_chat.dashboard import Dashboard

        self.config.update(SESSDATA="", ENABLED_ROOMS=[])
        plugin = self.instance(self.config)
        dashboard = Dashboard(plugin)
        with patch.object(self.module, "BilibiliClient", MultiClient):
            await dashboard.listen("12", True)
            await until(lambda: plugin.rooms.snapshot()[0]["phase"] == "waiting_login")
            self.assertEqual(self.config.saved["ENABLED_ROOMS"], ["12"])
            self.config["SESSDATA"] = "now-valid"
            await plugin.rooms.reconcile(restart=True)
            await until(lambda: plugin.rooms.live("12"))
            self.assertTrue(plugin.rooms.snapshot()[0]["enabled"])

    async def test_aliases_share_one_canonical_listener_and_removal_promotes_the_other(self):
        from astrbot_plugin_bilibili_live_chat.dashboard import Dashboard

        self.config.update(ROOM_IDS=["12", "100"], ENABLED_ROOMS=["12", "100"])
        plugin = self.instance(self.config)
        with patch.object(self.module, "BilibiliClient", MultiClient), \
             patch("astrbot_plugin_bilibili_live_chat.rooms.RETRY_BASE", 0.01):
            await plugin.initialize()
            await until(lambda: any(room["phase"] == "duplicate" for room in plugin.rooms.snapshot()))
            active = next(room for room in plugin.rooms.snapshot() if room["running"])
            await Dashboard(plugin).room_change(active["id"], "remove")
            await until(lambda: len(plugin.rooms.snapshot()) == 1 and plugin.rooms.snapshot()[0]["running"])
            self.assertNotIn(active["id"], self.config.saved["ENABLED_ROOMS"])

    async def test_failed_save_does_not_stop_running_room(self):
        from astrbot_plugin_bilibili_live_chat.dashboard import (
            Dashboard,
            DashboardError,
        )

        plugin = self.instance(self.config)
        with patch.object(self.module, "BilibiliClient", MultiClient):
            await plugin.initialize()
            await until(lambda: plugin.rooms.live("12"))
            old = plugin.rooms.live("12")
            with patch.object(self.config, "save_config", side_effect=OSError), self.assertRaises(DashboardError):
                await Dashboard(plugin).listen("12", False)
            self.assertTrue(old.running)
            self.assertIn("12", self.config["ENABLED_ROOMS"])

    def test_send_budget_is_shared_without_sharing_conversation(self):
        from astrbot_plugin_bilibili_live_chat.conversation import SendPacer

        settings = ConversationSettings.from_config({"MAX_PER_MINUTE": 1, "KEYWORDS": ["小猫"]})
        pacing = SendPacer()
        first = Conversation(settings, pacing=pacing)
        second = Conversation(settings, pacing=pacing)
        first.attempted(0)
        self.assertEqual(second.delay(20), 40)
        self.assertIsNot(first.history, second.history)

    async def test_stopping_during_initial_lookup_cancels_only_that_room(self):
        from astrbot_plugin_bilibili_live_chat.dashboard import Dashboard

        entered, closed = asyncio.Event(), asyncio.Event()

        class SlowClient(MultiClient):
            async def resolve_room(self, room):
                self.slow = room == 12
                if self.slow:
                    entered.set()
                    await asyncio.sleep(10)
                return await super().resolve_room(room)

            async def close(self):
                if self.slow:
                    closed.set()

        plugin = self.instance(self.config)
        with patch.object(self.module, "BilibiliClient", SlowClient):
            await plugin.initialize()
            await until(lambda: entered.is_set() and plugin.rooms.live("13"))
            other = plugin.rooms.live("13")
            await Dashboard(plugin).listen("12", False)
        self.assertTrue(closed.is_set())
        self.assertIsNone(plugin.rooms.live("12"))
        self.assertIs(plugin.rooms.live("13"), other)
        self.assertEqual(self.config.saved["ENABLED_ROOMS"], ["13"])

    async def test_transient_room_lookup_recovers_without_another_start_click(self):
        failures = []

        class UnstableClient(MultiClient):
            async def resolve_room(self, room):
                if room == 12 and not failures:
                    failures.append(room)
                    raise BilibiliError("网络暂不可用")
                return await super().resolve_room(room)

        plugin = self.instance(self.config)
        with patch.object(self.module, "BilibiliClient", UnstableClient), \
             patch("astrbot_plugin_bilibili_live_chat.rooms.RETRY_BASE", 0.025):
            await plugin.initialize()
            await until(lambda: plugin.rooms.snapshot()[0]["phase"] == "retrying")
            self.assertTrue(plugin.rooms.snapshot()[0]["enabled"])
            await until(lambda: plugin.rooms.live("12"))
            self.assertEqual(plugin.rooms.enabled(), {"12", "13"})

    async def test_cancelled_settings_request_still_restores_enabled_rooms(self):
        from astrbot_plugin_bilibili_live_chat.dashboard import Dashboard

        entered, release = asyncio.Event(), asyncio.Event()

        class SlowClose(MultiClient):
            async def close(self):
                entered.set()
                await release.wait()

        plugin = self.instance(self.config)
        with patch.object(self.module, "BilibiliClient", SlowClose):
            try:
                await plugin.initialize()
                await until(lambda: plugin.rooms.live("12") and plugin.rooms.live("13"))
                change = asyncio.create_task(Dashboard(plugin).save({"KEYWORDS": ["重新配置"]}))
                await until(entered.is_set)
                change.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await change
                await until(lambda: plugin.rooms.live("12") and plugin.rooms.live("13"))
                self.assertEqual(plugin.rooms.live("12").conversation.settings.keywords, ("重新配置",))
            finally:
                release.set()
