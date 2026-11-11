import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_session import FakeRoom

from astrbot_plugin_bilibili_live_chat.conversation import (
    Conversation,
    ConversationSettings,
    RoomEvent,
)
from astrbot_plugin_bilibili_live_chat.decision import EchoDecision
from astrbot_plugin_bilibili_live_chat.link import BiliBotLink
from astrbot_plugin_bilibili_live_chat.session import LiveSession


class DecisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_provider_and_threshold_are_forwarded_and_failures_fall_back(
        self,
    ):
        yes_no = AsyncMock(return_value=False)
        decision = EchoDecision(
            SimpleNamespace(decision=SimpleNamespace(yes_no=yes_no)),
            {"ECHO_DECISION_PROVIDER_ID": "judge", "ECHO_DECISION_THRESHOLD": 0.8},
        )
        chat = Conversation(
            ConversationSettings.from_config(
                {"KEYWORDS": ["小猫"], "ECHO_ENABLED": True, "ECHO_PROBABILITY": 1.0}
            )
        )
        chat.offer(RoomEvent("echo", "7", "观众", "这一幕真精彩"), 1)
        job = chat.take(1)
        allowed, reason = await decision.check(job)
        self.assertFalse(allowed)
        self.assertEqual(yes_no.await_args.kwargs["provider_id"], "judge")
        self.assertEqual(yes_no.await_args.kwargs["threshold"], 0.8)
        yes_no.side_effect = RuntimeError("secret-provider-token")
        allowed, reason = await decision.check(job)
        self.assertIsNone(allowed)
        self.assertNotIn("secret-provider-token", reason)
        self.assertIn("原规则", reason)
        missing = EchoDecision(SimpleNamespace(), {})
        self.assertIsNone((await missing.check(job))[0])

    async def test_negative_decision_does_not_generate_send_or_consume_budget(self):
        chat = Conversation(
            ConversationSettings.from_config(
                {"KEYWORDS": ["小猫"], "ECHO_ENABLED": True, "ECHO_PROBABILITY": 1.0}
            )
        )
        generate = AsyncMock(return_value='{"reply":"确实精彩"}')
        client = FakeRoom([])
        gate = AsyncMock(return_value=(False, "判断模型建议保持安静。"))
        live = LiveSession(
            client, 100, "42", chat, generate, preview=True, echo_gate=gate
        )
        self.addAsyncCleanup(live.stop)
        live.running = True
        live.offer_media(RoomEvent("echo", "7", "观众", "这一幕真精彩"))
        await live._process(chat.take(live.clock()))
        generate.assert_not_awaited()
        self.assertEqual(client.sent, [])
        self.assertEqual(list(chat.pacing.attempts), [])
        entry = live.debug.snapshot()["events"][-1]
        self.assertEqual(entry["status"], "skipped")
        self.assertIsNotNone(entry["timings"]["decision"])

    async def test_gate_failure_keeps_preview_and_wake_bypasses_gate(self):
        chat = Conversation(
            ConversationSettings.from_config(
                {"KEYWORDS": ["小猫"], "ECHO_ENABLED": True, "ECHO_PROBABILITY": 1.0}
            )
        )
        gate = AsyncMock(side_effect=RuntimeError("private-key"))
        live = LiveSession(
            FakeRoom([]),
            100,
            "42",
            chat,
            AsyncMock(return_value='{"reply":"确实精彩"}'),
            preview=True,
            echo_gate=gate,
        )
        self.addAsyncCleanup(live.stop)
        live.running = True
        live.offer_media(RoomEvent("echo", "7", "观众", "这一幕真精彩"))
        await live._process(chat.take(live.clock()))
        self.assertEqual(live.previews, 1)
        self.assertNotIn("private-key", str(live.debug.snapshot()))
        self.assertIn("原规则", live.debug.snapshot()["events"][-1]["decision"])
        gate.reset_mock()
        chat.pacing.attempts.clear()
        chat.pacing.last_attempt = float("-inf")
        live.offer_media(RoomEvent("wake", "7", "观众", "小猫在吗"))
        await live._process(chat.take(live.clock()))
        gate.assert_not_awaited()


class LinkStatusTests(unittest.TestCase):
    def test_reports_partial_and_incompatible_interfaces_without_calling_them(self):
        api = SimpleNamespace(search=AsyncMock(), record_live_event=lambda wrong: None)
        star = SimpleNamespace(
            activated=True,
            version="2.0.3",
            module=SimpleNamespace(get_bilibili_ai_bot_api=lambda: api),
            star_cls=SimpleNamespace(
                config={"SESSDATA": "cookie", "BILI_JCT": "csrf", "DEDE_USER_ID": "42"}
            ),
        )
        link = BiliBotLink(SimpleNamespace(get_registered_star=lambda name: star))
        report = link.status()
        self.assertTrue(report["login"]["available"])
        self.assertTrue(report["memory_read"]["available"])
        self.assertFalse(report["memory_write"]["available"])
        self.assertEqual(report["version"], "2.0.3")
        api.search.assert_not_called()
        self.assertNotIn("cookie", str(report))
        star.activated = False
        self.assertFalse(link.status()["active"])


if __name__ == "__main__":
    unittest.main()
