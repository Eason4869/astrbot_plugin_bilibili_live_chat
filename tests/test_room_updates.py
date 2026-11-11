import asyncio
import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image
from test_dashboard import Context
from test_plugin import Config, FakeClient, load_plugin, wait_for

from astrbot_plugin_bilibili_live_chat.conversation import (
    Conversation,
    ConversationSettings,
    RoomEvent,
)
from astrbot_plugin_bilibili_live_chat.dashboard import Dashboard, DashboardError
from astrbot_plugin_bilibili_live_chat.media import (
    MediaError,
    MediaRuntime,
    MediaSettings,
    frame_signature,
)


def message(key, text, source="danmaku"):
    return RoomEvent(key, "7" if source == "danmaku" else "", "观众", text, source)


class RoomClient(FakeClient):
    async def resolve_room(self, room):
        return int(room) + 100


class RecognitionTests(unittest.TestCase):
    def test_aliases_wake_only_voice_and_not_other_room(self):
        settings = ConversationSettings.from_config(
            {"KEYWORDS": ["小猫"], "VOICE_KEYWORD_ALIASES": ["小喵"]}
        )
        chat = Conversation(settings)
        chat.offer(message("text", "小喵", "danmaku"), 1)
        self.assertIsNone(chat.take(1))
        chat.offer(message("voice", "小喵", "voice"), 2)
        self.assertEqual(chat.take(2).mode, "wake")
        other = Conversation(ConversationSettings.from_config({"KEYWORDS": ["机器人"]}))
        other.offer(message("v", "小喵", "voice"), 3)
        self.assertIsNone(other.take(3))

    def test_cross_source_dedup_normalizes_punctuation_but_keeps_same_source(self):
        chat = Conversation(
            ConversationSettings.from_config(
                {"KEYWORDS": ["小猫"], "CROSS_SOURCE_DEDUP_SECONDS": 5}
            )
        )
        decisions = []
        chat.observe = lambda key, status, reason, **kwargs: decisions.append(
            (key, status, reason)
        )
        chat.offer(message("voice", "小猫，你好！", "voice"), 1)
        chat.offer(message("text", "小猫 你好", "danmaku"), 2)
        self.assertEqual(chat.take(2).message.key, "voice")
        self.assertEqual(decisions[-1][1], "skipped")
        chat.offer(message("later", "小猫 你好", "danmaku"), 8)
        self.assertEqual(chat.take(8).message.key, "later")
        chat.offer(message("same", "小猫 你好", "danmaku"), 9)
        self.assertEqual(chat.take(9).message.key, "same")

    def test_voice_alias_wake_supersedes_duplicate_echo_candidate(self):
        chat = Conversation(
            ConversationSettings.from_config(
                {
                    "KEYWORDS": ["小猫"],
                    "VOICE_KEYWORD_ALIASES": ["小喵"],
                    "ECHO_ENABLED": True,
                    "ECHO_PROBABILITY": 1.0,
                }
            )
        )
        chat.offer(message("echo", "小喵", "danmaku"), 1)
        chat.offer(message("voice", "小喵", "voice"), 2)
        job = chat.take(2)
        self.assertEqual((job.message.key, job.mode), ("voice", "wake"))
        self.assertTrue(chat.window_active(2))

    def test_ignored_regions_mask_only_comparison_signature(self):
        frames = []
        for top, bottom in [("black", "black"), ("black", "white")]:
            with BytesIO() as buffer:
                image = Image.new("RGB", (32, 32), top)
                image.paste(bottom, (0, 16, 32, 32))
                image.save(buffer, "PNG")
                frames.append(buffer.getvalue())
        self.assertNotEqual(frame_signature(frames[0]), frame_signature(frames[1]))
        self.assertEqual(
            frame_signature(frames[0], [[0, 50, 100, 100]]),
            frame_signature(frames[1], [[0, 50, 100, 100]]),
        )
        for data in (
            {"ignored_regions": [[0, 0, 101, 100]]},
            {"scene_confirm_frames": 0},
            {"scene_threshold": float("nan")},
        ):
            with self.assertRaises(MediaError):
                MediaSettings.parse(data, strict=True)


class RoomUpdateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = load_plugin()
        self.config = Config(
            ROOM_IDS=["12", "13"],
            ENABLED_ROOMS=[],
            KEYWORDS=["小猫"],
            MODEL_PROVIDER_ID="chat-model",
            SESSDATA="cookie",
            BILI_JCT="csrf",
            DEDE_USER_ID="42",
        )
        self.context = Context()
        self.bot = self.module.LiveChatPlugin(self.context, self.config)
        self.addAsyncCleanup(self.bot.terminate)
        self.ui = Dashboard(self.bot)

    async def test_room_overrides_restart_only_that_room_and_allow_explicit_empty_persona(
        self,
    ):
        self.config.update(ASTRBOT_PERSONA_ID="host", PERSONA_PROMPT="shared prompt")
        with patch.object(self.module, "BilibiliClient", RoomClient):
            for room in ["12", "13"]:
                await self.ui.listen(room, True)
            await wait_for(
                lambda: all(self.bot.rooms.live(room) for room in ["12", "13"])
            )
            first, other = self.bot.rooms.live("12"), self.bot.rooms.live("13")
            await self.ui.dialogue_settings(
                "12",
                {
                    "KEYWORDS": ["房间词"],
                    "ASTRBOT_PERSONA_ID": "",
                    "PERSONA_PROMPT": "",
                },
            )
            self.assertFalse(first.valid())
            await wait_for(
                lambda: (
                    self.bot.rooms.live("12") and self.bot.rooms.live("12") is not first
                )
            )
            self.assertIs(self.bot.rooms.live("13"), other)
            self.assertEqual(
                self.bot.rooms.live("12").conversation.settings.keywords, ("房间词",)
            )
            self.assertEqual(
                self.bot.rooms.effective_config("12")["ASTRBOT_PERSONA_ID"], ""
            )
            self.assertEqual(
                self.bot.rooms.effective_config("13")["ASTRBOT_PERSONA_ID"], "host"
            )
            await self.ui.dialogue_settings("12", {"KEYWORDS": None})
            self.assertNotIn("KEYWORDS", self.config.saved["ROOM_DIALOGUE"]["12"])

    async def test_room_echo_preference_cannot_bypass_shared_master_switch(self):
        self.config["ECHO_ENABLED"] = False
        await self.ui.dialogue_settings("12", {"ECHO_ENABLED": True})
        self.assertFalse(self.bot.rooms.effective_config("12")["ECHO_ENABLED"])
        with patch.object(self.module, "BilibiliClient", RoomClient):
            await self.ui.listen("12", True)
            await wait_for(lambda: self.bot.rooms.live("12"))
            self.assertFalse(
                self.bot.rooms.live("12").conversation.settings.echo_enabled
            )

    async def test_copy_saved_settings_preserves_preview_and_active_state(self):
        await self.ui.dialogue_settings(
            "12", {"KEYWORDS": ["独立词"], "PERSONA_PROMPT": "本房间人格"}
        )
        await self.ui.media_settings(
            "12", {"scene_threshold": 30, "scene_confirm_frames": 2}
        )
        await self.ui.preview_settings("13", True)
        await self.ui.copy_room_settings("12", "13")
        self.assertEqual(
            self.bot.rooms.dialogue_overrides("13")["KEYWORDS"], ["独立词"]
        )
        self.assertEqual(self.bot.rooms.media_settings("13").scene_threshold, 30)
        self.assertTrue(self.bot.rooms.preview_enabled("13"))
        self.assertEqual(self.bot.rooms.enabled(), set())
        self.assertEqual(self.config["KEYWORDS"], ["小猫"])
        for payload in [
            {"SESSDATA": "bad"},
            {"KEYWORDS": "bad"},
            {"ECHO_DECISION_THRESHOLD": float("nan")},
        ]:
            with self.assertRaises(DashboardError):
                await self.ui.dialogue_settings("12", payload)
        with self.assertRaises(DashboardError):
            await self.ui.copy_room_settings("12", "99")

    async def test_selfcheck_uses_room_dialogue_model_as_default_vision(self):
        await self.ui.dialogue_settings("12", {"MODEL_PROVIDER_ID": "room-chat"})
        self.context.llm_generate = AsyncMock(
            return_value=SimpleNamespace(completion_text="1234")
        )
        self.context.get_provider_by_id = lambda provider: (
            object() if provider in ("room-chat", "chat-model") else None
        )
        self.context.get_using_stt_provider = lambda: SimpleNamespace(
            get_text=AsyncMock(return_value="测试")
        )
        with (
            patch(
                "astrbot_plugin_bilibili_live_chat.diagnostics.probe_ffmpeg",
                AsyncMock(return_value="OK"),
            ),
            patch(
                "astrbot_plugin_bilibili_live_chat.diagnostics.test_image",
                return_value=("1234", "base64://test"),
            ),
        ):
            self.bot.diagnostics.start("12")
            await wait_for(lambda: not self.bot.diagnostics.snapshot()["running"])
        self.assertEqual(
            self.context.llm_generate.await_args.kwargs["chat_provider_id"], "room-chat"
        )

    async def test_confirmed_scene_change_survives_stable_second_frame(self):
        frames = []
        for color in ("black", "white", "white"):
            with BytesIO() as buffer:
                Image.new("RGB", (32, 32), color).save(buffer, "JPEG")
                frames.append(buffer.getvalue())
        reader = asyncio.StreamReader()
        reader.feed_data(b"".join(frames))
        reader.feed_eof()
        emitted, reports = [], []
        media = MediaRuntime(
            MediaSettings(video=True, scene_confirm_frames=2),
            None,
            100,
            None,
            emitted.append,
            report=lambda event, reason: reports.append(reason),
        )
        await media._video(reader)
        self.assertEqual(len(emitted), 1)
        self.assertIn("确认", reports[1])
        self.assertEqual(media.latest_frame(), frames[-1])


if __name__ == "__main__":
    unittest.main()
