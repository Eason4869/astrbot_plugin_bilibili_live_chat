import unittest

from astrbot_plugin_bilibili_live_chat.conversation import (
    Conversation,
    ConversationSettings,
    Danmaku,
    parse_reply,
)


def message(text, key="1"):
    return Danmaku(key, "42", "观众", text)


class ConversationTests(unittest.TestCase):
    def make(self, **overrides):
        config = {"KEYWORDS": ["  小猫 ", "", "Bot"], **overrides}
        return Conversation(ConversationSettings.from_config(config), lambda: 0)

    def test_custom_words_are_case_insensitive_and_no_example_is_installed(self):
        chat = self.make()
        chat.offer(message("E包，聊聊吧"), 0)
        self.assertIsNone(chat.take(0))
        chat.offer(message("在吗 ＢＯＴ"), 1)
        self.assertEqual(chat.take(1).mode, "wake")
        chat.offer(message("继续聊"), 2)
        self.assertEqual(chat.take(2).mode, "conversation")

    def test_empty_keyword_list_suppresses_echo_too(self):
        chat = self.make(KEYWORDS=["  "], ECHO_ENABLED=True)
        chat.offer(message("这好可爱"), 0)
        self.assertIsNone(chat.take(0))

    def test_zero_window_only_accepts_explicit_wake(self):
        chat = self.make(WINDOW_SECONDS=0)
        chat.offer(message("小猫"), 0)
        wake = chat.take(0)
        self.assertTrue(chat.is_current(wake, 1))
        chat.sent(wake, 1)
        chat.offer(message("继续聊"), 15)
        self.assertIsNone(chat.take(15))

    def test_wake_is_preserved_in_burst_and_repeated_wake_respects_absolute_cap(self):
        chat = self.make(WINDOW_SECONDS=90, WINDOW_EXTEND_SECONDS=45, WINDOW_MAX_SECONDS=100)
        chat.offer(message("小猫", "wake"), 0)
        chat.offer(message("接一句", "follow"), 0)
        job = chat.take(0)
        self.assertEqual(job.message.key, "wake")
        chat.sent(job, 1)
        self.assertEqual(chat.window_until, 100)
        chat.offer(message("小猫", "again"), 95)
        job = chat.take(95)
        self.assertEqual(chat.window_until, 100)
        self.assertFalse(chat.is_current(job, 100))

    def test_reply_extends_window_from_original_deadline(self):
        chat = self.make()
        chat.offer(message("小猫"), 0)
        chat.sent(chat.take(1), 2)
        self.assertEqual(chat.window_until, 135)

    def test_echo_is_quiet_on_questions_and_yields_to_wake(self):
        chat = self.make(ECHO_ENABLED=True)
        chat.offer(message("你为什么不说话"), 0)
        self.assertIsNone(chat.take(0))
        chat.offer(message("这也太萌了"), 1)
        echo = chat.take(1)
        self.assertEqual(echo.mode, "echo")
        chat.offer(message("小猫"), 2)
        self.assertFalse(chat.is_current(echo, 2))
        self.assertEqual(chat.take(2).mode, "wake")

    def test_shared_limit_counts_echo_and_uncertain_attempt(self):
        chat = self.make(ECHO_ENABLED=True, REPLY_COOLDOWN_SECONDS=12, MAX_PER_MINUTE=1)
        chat.offer(message("这也太萌了"), 0)
        self.assertEqual(chat.take(0).mode, "echo")
        chat.attempted(0)
        chat.offer(message("小猫"), 1)
        self.assertEqual(chat.delay(1), 59)
        self.assertIsNone(chat.take(1))
        chat.offer(message("小猫", "fresh"), 59)
        self.assertEqual(chat.take(60).message.key, "fresh")

    def test_sent_context_is_separate_from_audience(self):
        chat = self.make()
        chat.offer(message("小猫", "first"), 0)
        chat.sent(chat.take(0), 0, "我在呢")
        chat.offer(message("继续聊", "second"), 13)
        self.assertEqual(chat.take(13).context, (("观众", "小猫"), ("Bot", "我在呢")))

    def test_cooldown_can_outlast_the_per_minute_accounting_window(self):
        chat = self.make(REPLY_COOLDOWN_SECONDS=120)
        chat.attempted(0)
        self.assertEqual(chat.delay(61), 59)
        self.assertEqual(chat.delay(120), 0)

    def test_model_output_must_be_one_short_valid_reply(self):
        self.assertEqual(parse_reply('{"reply":"好可爱"}', "echo", 20), "好可爱")
        for raw in ('{"reply":""}', '{"reply":"A"}', '{"reply":"嗯？"}',
                    '{"reply":"嗯嗯","reply":"好吧"}', '{"reply":"上\\n下"}',
                    '{"reply":"@某某"}', '{"reply":"哈哈","extra":1}', 'plain',
                    '{"reply":"1234567890123456"}'):
            with self.subTest(raw=raw):
                self.assertIsNone(parse_reply(raw, "echo", 20))

    def test_unicode_line_separators_are_rejected(self):
        for char in ("\u0085", "\u2028", "\u2029"):
            with self.subTest(char=repr(char)):
                self.assertIsNone(parse_reply('{"reply":"上' + char + '下"}', "wake", 20))
