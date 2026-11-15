"""Unwoken participation must be possible without becoming a response stream."""

import unittest
from itertools import pairwise
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_plugin import Config, load_plugin
from test_session import FakeRoom

from astrbot_plugin_bilibili_live_chat.conversation import (
    Conversation,
    ConversationSettings,
    Job,
    RoomEvent,
    parse_reply,
)
from astrbot_plugin_bilibili_live_chat.decision import EchoDecision
from astrbot_plugin_bilibili_live_chat.session import LiveSession


def event(key, text="这波操作真精彩", source="danmaku"):
    return RoomEvent(key, "7" if source == "danmaku" else "", "直播内容", text, source)


def conversation(**overrides):
    return Conversation(ConversationSettings.from_config({
        "KEYWORDS": ["小猫"], "ECHO_ENABLED": True, "ECHO_PROBABILITY": 1.0,
        "VOICE_FOLLOWUP_ENABLED": True, "VISION_ECHO_ENABLED": True, **overrides,
    }), lambda: 0)


class NaturalEchoTests(unittest.TestCase):
    def test_statements_and_positive_exclamations_can_be_echoed(self):
        for text in ("这波操作真精彩呢", "这个角色很可爱呢！", "我还在呢", "我喜欢主播呢",
                     "怎么这么厉害", "怎么那么精彩啊", "你怎么这么厉害呢"):
            with self.subTest(text=text):
                chat = conversation()
                chat.offer(event("first", text), 100)
                self.assertIsNotNone(chat.take(100))
                self.assertEqual(parse_reply('{"reply":"' + text + '"}', "echo", 20), text)

    def test_actual_questions_still_cannot_trigger_or_be_echo_replies(self):
        for text in ("这关怎么过", "你觉得呢", "接下来呢？", "下一步呢", "能教我吗！", "怎么这么厉害？"):
            with self.subTest(text=text):
                chat = conversation()
                chat.offer(event("first", text), 100)
                self.assertIsNone(chat.take(100))
                self.assertIsNone(parse_reply('{"reply":"' + text + '"}', "echo", 20))

    def test_elliptical_questions_remain_blocked_without_rejecting_statements(self):
        for text in ("你呢", "主播呢", "其他人呢", "那你呢", "那，主播呢", "这波很棒，你呢", "你们呢！"):
            with self.subTest(text=text):
                chat = conversation()
                chat.offer(event("first", text), 100)
                self.assertIsNone(chat.take(100))
                self.assertIsNone(parse_reply('{"reply":"' + text + '"}', "echo", 20))

    def test_unsent_attempt_only_blocks_new_input_for_sixty_seconds_across_sources(self):
        chat = conversation()
        chat.offer(event("first"), 100)
        self.assertEqual(chat.take(100).mode, "echo")
        for now, source in ((101, "voice"), (130, "vision"), (159.9, "danmaku")):
            chat.offer(event(str(now), source=source), now)
            self.assertIsNone(chat.take(now))
        chat.offer(event("new-topic", source="voice"), 160)
        retry = chat.take(160)
        self.assertIsNotNone(retry)
        self.assertEqual(retry.mode, "echo")
        self.assertFalse(chat.window_active(160))

    def test_retry_cooldown_never_exceeds_a_shorter_configured_interval(self):
        chat = conversation(ECHO_INTERVAL_SECONDS=10)
        chat.offer(event("first"), 100)
        chat.take(100)
        chat.offer(event("too-soon"), 109)
        self.assertIsNone(chat.take(109))
        chat.offer(event("new-topic"), 110)
        self.assertEqual(chat.take(110).mode, "echo")

    def test_successful_echo_keeps_full_interval_measured_from_completion(self):
        chat = conversation()
        chat.offer(event("first"), 100)
        chat.sent(chat.take(100), 105, "真精彩呢")
        for now, source in ((165, "voice"), (304, "vision"), (404.9, "danmaku")):
            chat.offer(event(str(now), source=source), now)
            self.assertIsNone(chat.take(now))
        chat.offer(event("new-topic"), 405)
        self.assertEqual(chat.take(405).mode, "echo")

    def test_wake_bypasses_attempt_cooldown_and_success_resets_full_interval(self):
        chat = conversation(WINDOW_SECONDS=0)
        chat.offer(event("first"), 100)
        chat.take(100)
        chat.offer(event("wake", "小猫你好"), 101)
        wake = chat.take(101)
        self.assertEqual(wake.mode, "wake")
        chat.sent(wake, 102, "在呢")
        chat.offer(event("too-soon"), 401)
        self.assertIsNone(chat.take(401))
        chat.offer(event("new-topic"), 402)
        self.assertEqual(chat.take(402).mode, "echo")


class NaturalEchoPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_continuous_mixed_input_bounds_model_calls_and_actual_sends(self):
        for silent in (True, False):
            with self.subTest(silent=silent):
                ticks = [100]
                generate = AsyncMock(return_value='{"reply":""}' if silent else '{"reply":"真精彩呢"}')
                chat = conversation()
                client = FakeRoom([])
                live = LiveSession(client, 100, "42", chat, generate, clock=lambda ticks=ticks: ticks[0])
                self.addAsyncCleanup(live.stop)
                live.running = True
                completed = []
                for now in range(100, 1001, 5):
                    ticks[0] = now
                    for source in ("danmaku", "voice", "vision"):
                        live.offer_media(event(f"{now}-{source}", f"第{now}波精彩操作", source))
                        job = chat.take(now)
                        if job:
                            await live._process(job)
                            completed.append(now)
                self.assertEqual(generate.await_count, 16 if silent else 4)
                self.assertEqual(len(client.sent), 0 if silent else 4)
                self.assertTrue(all(right - left >= (60 if silent else 300)
                                    for left, right in pairwise(completed)))
                self.assertFalse(chat.window_active(1000))

    async def test_unsent_outcomes_allow_bounded_retry_on_a_fresh_event(self):
        for outcome in ("reject", "silent", "invalid", "error", "timeout"):
            with self.subTest(outcome=outcome):
                ticks = [100]
                chat = conversation()
                generate = AsyncMock(return_value={
                    "silent": '{"reply":""}', "invalid": "invalid-json",
                }.get(outcome, '{"reply":"真精彩呢"}'))
                if outcome in ("error", "timeout"):
                    generate.side_effect = RuntimeError() if outcome == "error" else TimeoutError()
                gate = AsyncMock(return_value=(False, "建议安静")) if outcome == "reject" else None
                client = FakeRoom([])
                live = LiveSession(client, 100, "42", chat, generate,
                                   preview=True, echo_gate=gate, clock=lambda ticks=ticks: ticks[0])
                self.addAsyncCleanup(live.stop)
                live.running = True
                live.offer_media(event("first"))
                await live._process(chat.take(100))
                self.assertEqual(live.previews, 0)
                self.assertEqual(client.sent, [])
                self.assertEqual(list(chat.pacing.attempts), [])
                ticks[0] = 159
                live.offer_media(event("too-soon", source="voice"))
                self.assertIsNone(chat.take(159))
                ticks[0] = 160
                live.offer_media(event("new-topic", source="vision"))
                self.assertIsNotNone(chat.take(160))

    async def test_vision_skips_text_only_judge_and_reaches_visual_generation(self):
        judge = AsyncMock(return_value=False)
        gate = EchoDecision(SimpleNamespace(decision=SimpleNamespace(yes_no=judge)), {})
        chat = conversation()
        frame = event("frame", source="vision")
        frame = RoomEvent(frame.key, frame.uid, frame.name, frame.text, frame.source, b"frame")
        generate = AsyncMock(return_value='{"reply":"真精彩呢"}')
        live = LiveSession(FakeRoom([]), 100, "42", chat, generate,
                           preview=True, echo_gate=gate.check, clock=lambda: 100)
        self.addAsyncCleanup(live.stop)
        live.running = True
        live.offer_media(frame)
        await live._process(chat.take(100))
        judge.assert_not_awaited()
        generate.assert_awaited_once()
        self.assertEqual(generate.await_args.args[0].message.frame, b"frame")
        self.assertEqual(live.previews, 1)
        self.assertIn("视觉", live.debug.snapshot()["events"][-1]["decision"])

    async def test_echo_prompt_allows_unaddressed_participation_but_keeps_etiquette(self):
        generate = AsyncMock(return_value=SimpleNamespace(completion_text='{"reply":"真精彩呢"}'))
        plugin = load_plugin().LiveChatPlugin(SimpleNamespace(llm_generate=generate), Config())
        job = Job(event("topic"), "echo", 100, 0, ())
        await plugin._generate(job, "chat", "像猫咪一样说话", "", "自然一点", 20, [])
        prompt = generate.await_args.kwargs["system_prompt"]
        self.assertIn("无需被点名", prompt)
        self.assertIn("2–15 字", prompt)
        self.assertIn("保持安静", prompt)
        self.assertIn("不主动追问", prompt)
        self.assertIn("不要为了活跃气氛", prompt)
