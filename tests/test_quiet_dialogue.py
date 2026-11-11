import unittest
from types import SimpleNamespace

from test_plugin import Config, load_plugin
from test_session import FakeRoom

from astrbot_plugin_bilibili_live_chat.conversation import (
    Conversation,
    ConversationSettings,
    Job,
    RoomEvent,
)
from astrbot_plugin_bilibili_live_chat.room_settings import (
    RoomSettingsError,
    validate_dialogue,
)
from astrbot_plugin_bilibili_live_chat.session import LiveSession


def event(key, text, uid="7", source="danmaku"):
    return RoomEvent(key, uid if source == "danmaku" else "", "观众", text, source)


class QuietDialogueTests(unittest.TestCase):
    def chat(self, **overrides):
        return Conversation(ConversationSettings.from_config({
            "KEYWORDS": ["小猫"], "ECHO_ENABLED": True,
            "ECHO_PROBABILITY": 1.0, **overrides,
        }), lambda: 0)

    def wake(self, chat):
        chat.offer(event("wake", "小猫你好"), 1)
        job = chat.take(1)
        chat.sent(job, 1, "你好")
        return job

    def test_window_follows_addressed_viewer_not_everyone(self):
        chat = self.chat()
        self.wake(chat)
        chat.offer(event("other", "这关怎么过", uid="8"), 3)
        self.assertIsNone(chat.take(3))
        chat.offer(event("follow", "这关怎么过"), 4)
        self.assertEqual(chat.take(4).message.key, "follow")

    def test_voice_cannot_borrow_audience_window_or_reply_without_wake(self):
        chat = self.chat()
        self.wake(chat)
        chat.offer(event("voice", "这一关好难啊", source="voice"), 3)
        self.assertIsNone(chat.take(3))
        chat.offer(event("call", "小猫你看呢", source="voice"), 4)
        self.assertEqual(chat.take(4).mode, "wake")
        chat.offer(event("more", "主播继续说话", source="voice"), 6)
        self.assertIsNone(chat.take(6))

    def test_voice_and_vision_do_not_add_echo_candidates_by_default(self):
        for source in ("voice", "vision"):
            with self.subTest(source=source):
                chat = self.chat()
                chat.offer(event("ambient", "画面出现明显变化", source=source), 1)
                self.assertIsNone(chat.take(1))

    def test_explicit_ambient_options_keep_shared_echo_budget(self):
        chat = self.chat(VISION_ECHO_ENABLED=True, VOICE_FOLLOWUP_ENABLED=True)
        chat.offer(event("frame", "画面出现明显变化", source="vision"), 1)
        job = chat.take(1)
        self.assertEqual(job.mode, "echo")
        chat.sent(job, 1, "漂亮")
        chat.offer(event("speech", "刚才真精彩", source="voice"), 2)
        self.assertIsNone(chat.take(2))

    def test_same_burst_has_only_one_wake_response(self):
        chat = self.chat()
        chat.offer(event("call", "小猫你在吗"), 1)
        chat.offer(event("follow", "今天好开心"), 1.1)
        self.assertEqual(chat.take(2).message.key, "call")
        self.assertIsNone(chat.take(3), "a queued follow-up must not produce a second reply")

    def test_wake_preempts_inflight_lower_priority_response(self):
        chat = self.chat()
        chat.offer(event("ambient", "这波操作真精彩"), 1)
        old = chat.take(1)
        chat.offer(event("call", "小猫你在吗"), 2)
        self.assertFalse(chat.is_current(old, 2))
        self.assertEqual(chat.take(2).mode, "wake")
        # An explicit wake should also preempt a follow-up in the same window.
        chat.offer(event("follow", "今天好开心"), 3)
        follow = chat.take(3)
        chat.offer(event("again", "小猫看看这个"), 4)
        self.assertFalse(chat.is_current(follow, 4))

    def test_every_successful_reply_suppresses_immediate_echo(self):
        chat = self.chat(WINDOW_SECONDS=0)
        self.wake(chat)
        chat.offer(event("ambient", "这一幕真精彩"), 22)
        self.assertIsNone(chat.take(22))

    def test_long_read_aloud_with_minor_transcription_difference_is_merged(self):
        chat = self.chat()
        chat.offer(event("text", "小猫这个游戏真的太有意思了"), 1)
        self.assertEqual(chat.take(1).mode, "wake")
        chat.offer(event("voice", "小猫这个游戏真是太有意思了", source="voice"), 2)
        self.assertIsNone(chat.take(2))
        chat.offer(event("different", "小猫这个游戏一点都没有意思", source="voice"), 3)
        self.assertEqual(chat.take(3).mode, "wake")

    def test_optional_room_scope_accepts_other_viewer_followup(self):
        chat = self.chat(FOLLOWUP_SCOPE="room")
        self.wake(chat)
        chat.offer(event("other", "这关怎么过", uid="8"), 3)
        self.assertEqual(chat.take(3).mode, "conversation")

    def test_default_reply_does_not_extend_window(self):
        chat = self.chat()
        self.wake(chat)
        self.assertFalse(chat.window_active(32))

    def test_minor_similarity_never_merges_negation_or_number_changes(self):
        for original, changed in (
            ("小猫这个游戏我真的非常喜欢", "小猫这个游戏我真的不非常喜欢"),
            ("小猫这个游戏现在是第15关了", "小猫这个游戏现在是第16关了"),
            ("小猫这一关我应该选择第一条路线吗", "小猫这一关我应该选择第二条路线吗"),
            ("小猫这个道具的倍率是1.5倍", "小猫这个道具的倍率是15倍"),
            ("小猫 I really do want to play this game", "小猫 I really don't want to play this game"),
            ("小猫 should the temperature be -15 degrees now", "小猫 should the temperature be 15 degrees now"),
            ("小猫这个温度要设置成负十五度吗", "小猫这个温度要设置成十五度吗"),
        ):
            with self.subTest(changed=changed):
                chat = self.chat()
                chat.offer(event("text", original), 1)
                chat.take(1)
                chat.offer(event("voice", changed, source="voice"), 2)
                self.assertIsNotNone(chat.take(2))

    def test_explicit_voice_wake_still_works_when_text_keyword_mode_is_off(self):
        chat = self.chat(KEYWORD_MODE=False, ECHO_ENABLED=False)
        chat.offer(event("voice", "小猫在吗", source="voice"), 1)
        self.assertEqual(chat.take(1).mode, "wake")

    def test_merged_voice_wake_retains_identified_viewer_for_followups(self):
        chat = self.chat()
        chat.offer(event("voice", "小猫你在吗", source="voice"), 1)
        chat.take(1)
        chat.offer(event("text", "小猫你在吗"), 2)
        self.assertIsNone(chat.take(2))
        chat.offer(event("follow", "看看这一关"), 3)
        self.assertEqual(chat.take(3).mode, "conversation")

    def test_followups_during_generation_do_not_queue_another_reply(self):
        chat = self.chat()
        self.wake(chat)
        chat.offer(event("follow", "接着玩这一关"), 3)
        job = chat.take(3)
        chat.offer(event("newer", "接着跳下去"), 4)
        chat.sent(job, 5, "跳右边试试")
        self.assertIsNone(chat.take(6))

    def test_new_explicit_wake_during_generation_is_preserved(self):
        chat = self.chat()
        chat.offer(event("call", "小猫在吗"), 1)
        job = chat.take(1)
        chat.offer(event("again", "小猫我还有个问题"), 2)
        chat.sent(job, 3, "在呢")
        self.assertEqual(chat.take(4).message.key, "again")

    def test_new_strategy_controls_are_editable_shared_and_per_room(self):
        from astrbot_plugin_bilibili_live_chat.dashboard import (
            DashboardError,
            validated_settings,
        )

        payload = {"FOLLOWUP_SCOPE": "room", "VOICE_FOLLOWUP_ENABLED": True, "VISION_ECHO_ENABLED": True}
        self.assertEqual(validated_settings(payload), payload)
        self.assertEqual(validate_dialogue(payload), payload)
        for invalid in ({"FOLLOWUP_SCOPE": "anywhere"}, {"VOICE_FOLLOWUP_ENABLED": 1}, {"VISION_ECHO_ENABLED": "true"}):
            for validate, error in ((validated_settings, DashboardError), (validate_dialogue, RoomSettingsError)):
                with self.subTest(invalid=invalid, validate=validate.__name__), self.assertRaises(error):
                    validate(invalid)


class QuietPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_generation_time_inputs_do_not_produce_a_followup_burst(self):
        ticks = [1]
        chat = Conversation(ConversationSettings.from_config({"KEYWORDS": ["小猫"]}))
        client = FakeRoom([])

        async def generate(job):
            ticks[0] = 2
            live.offer_media(event("follow", "这关也好难"))
            live.offer_media(event("voice", "主播在持续说话", source="voice"))
            live.offer_media(event("frame", "画面出现明显变化", source="vision"))
            return '{"reply":"先试试左边"}'

        live = LiveSession(client, 100, "42", chat, generate, clock=lambda: ticks[0])
        live.running = True
        self.addAsyncCleanup(live.stop)
        live.offer_media(event("wake", "小猫怎么过关"))
        await live._process(chat.take(1))
        self.assertEqual(client.sent, [(100, "先试试左边")])
        self.assertIsNone(chat.take(30))

    async def test_model_receives_builtin_room_etiquette_and_explicit_source(self):
        captured = []

        async def generate(**kwargs):
            captured.append(kwargs)
            return SimpleNamespace(completion_text='{"reply":"在呢"}')

        module = load_plugin()
        plugin = module.LiveChatPlugin(SimpleNamespace(llm_generate=generate), Config())
        job = Job(event("voice", "小猫在吗", source="voice"), "wake", 1, 1, ())
        await plugin._generate(job, "chat", "像猫咪一样说话", "口语一点", "", 20, [])
        self.assertEqual(len(captured), 1)
        request = captured[0]
        self.assertIn("不要为了活跃气氛", request["system_prompt"])
        self.assertIn("像猫咪一样说话", request["system_prompt"])
        self.assertIn('"类型": "voice"', request["prompt"])
        self.assertIn('"观众UID": null', request["prompt"])
