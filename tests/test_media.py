import asyncio
import unittest
import wave
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
from test_dashboard import Context
from test_plugin import Config, FakeClient, load_plugin, wait_for

from astrbot_plugin_bilibili_live_chat.conversation import (
    Conversation,
    ConversationSettings,
)


class MediaTests(unittest.IsolatedAsyncioTestCase):
    def test_voice_and_video_share_wake_and_echo_rules(self):
        from astrbot_plugin_bilibili_live_chat.conversation import RoomEvent

        chat = Conversation(ConversationSettings.from_config({
            "KEYWORDS": ["小猫"], "ECHO_ENABLED": True, "ECHO_INTERVAL_SECONDS": 180,
        }), lambda: 0)
        chat.offer(RoomEvent("1", "", "直播画面", "画面明显变化", "vision", b"jpeg"), 0)
        self.assertEqual(chat.take(0).mode, "echo")
        chat.offer(RoomEvent("2", "", "直播语音", "这一关真难", "voice"), 1)
        self.assertIsNone(chat.take(1))
        chat.offer(RoomEvent("3", "", "直播语音", "小猫，你怎么看", "voice"), 2)
        self.assertEqual(chat.take(2).mode, "wake")
        chat.offer(RoomEvent("4", "123", "观众", "接着说"), 3)
        self.assertEqual(chat.take(3).mode, "conversation")
        chat.offer(RoomEvent("5", "", "直播画面", "小猫", "vision", b"jpeg"), 4)
        self.assertIsNone(chat.take(4))
        chat.offer(RoomEvent("6", "", "直播语音", "刚刚那关怎么过", "voice"), 5)
        self.assertEqual(chat.take(5).mode, "conversation")

    def test_media_never_bypasses_echo_switch_and_does_not_treat_all_speech_as_formal(self):
        from astrbot_plugin_bilibili_live_chat.conversation import RoomEvent

        chat = Conversation(ConversationSettings.from_config({"KEYWORD_MODE": False}), lambda: 0)
        chat.offer(RoomEvent("1", "", "直播语音", "普通语音", "voice"), 0)
        chat.offer(RoomEvent("2", "", "直播画面", "画面变化", "vision", b"jpeg"), 1)
        self.assertIsNone(chat.take(1))

    async def test_web_media_settings_persist_and_restart_only_the_target_room(self):
        from astrbot_plugin_bilibili_live_chat.dashboard import Dashboard

        module = load_plugin()
        config = Config(
            ROOM_IDS=["12", "100"], ENABLED_ROOMS=["12", "100"], KEYWORDS=["小猫"],
            MODEL_PROVIDER_ID="chat-model", SESSDATA="cookie", BILI_JCT="csrf", DEDE_USER_ID="42",
        )
        plugin = module.LiveChatPlugin(Context(), config)
        self.addAsyncCleanup(plugin.terminate)

        class Client(FakeClient):
            async def resolve_room(self, room):
                return room

        with patch.object(module, "BilibiliClient", Client):
            await plugin.initialize()
            await wait_for(lambda: plugin.rooms.live("12") and plugin.rooms.live("100"))
            before = plugin.rooms.live("100")
            await Dashboard(plugin).media_settings("12", {
                "audio": True, "frame_interval": 7, "echo_interval": 240, "vision_provider": "vision",
            })
            await wait_for(lambda: plugin.rooms.live("12"))
        self.assertEqual(config.saved["ROOM_MEDIA"]["12"]["frame_interval"], 7)
        self.assertEqual(config.saved["ENABLED_ROOMS"], ["12", "100"])
        self.assertIs(plugin.rooms.live("100"), before)
        self.assertTrue(plugin.rooms.snapshot()[0]["media"]["settings"]["audio"])

    async def test_stream_url_selection_accepts_bili_cdn_and_rejects_unrelated_hosts(self):
        from astrbot_plugin_bilibili_live_chat.bilibili import playback_urls

        formats = [{"format_name": "flv", "codec": [{
            "base_url": "/live/test.flv",
            "url_info": [
                {"host": "https://localhost", "extra": "?sig=bad"},
                {"host": "https://live-test.bilivideo.com", "extra": "?sig=example"},
                {"host": "https://bilivideo.com.example.com", "extra": ""},
            ],
        }]}]
        data = {"playurl_info": {"playurl": {"stream": [{"format": formats}]}}}
        self.assertEqual(playback_urls(data), ("https://live-test.bilivideo.com/live/test.flv?sig=example",))

    async def test_media_stop_cancels_both_readers(self):
        from astrbot_plugin_bilibili_live_chat.media import MediaRuntime, MediaSettings

        media = MediaRuntime(MediaSettings(audio=True, video=True), None, 100, None, lambda: None)
        entered = []
        stopped = []

        async def pull(mode):
            entered.append(mode)
            try:
                await asyncio.sleep(20)
            finally:
                stopped.append(mode)

        with patch.object(media, "_run", pull):
            media.start()
            await wait_for(lambda: len(entered) == 2)
            await media.stop()
        self.assertCountEqual(stopped, ["audio", "video"])

    async def test_default_astrbot_stt_receives_a_wav_and_emits_only_fresh_voice(self):
        from astrbot_plugin_bilibili_live_chat.media import MediaRuntime, MediaSettings

        seen = []
        emitted = []

        class Stt:
            async def get_text(self, path):
                with wave.open(path, "rb") as audio:
                    self.assert_wav(audio)
                seen.append(Path(path))
                return "小猫 在吗"

            @staticmethod
            def assert_wav(audio):
                assert (audio.getnchannels(), audio.getframerate(), audio.getsampwidth()) == (1, 16000, 2)

        provider = Stt()
        context = SimpleNamespace(get_using_stt_provider=lambda: provider)
        media = MediaRuntime(MediaSettings(audio=True), None, 100, context, emitted.append, clock=lambda: 50)
        selected = await media._stt()
        self.assertIs(selected, provider)
        queue = asyncio.Queue()
        queue.put_nowait((20, b"\x00\x00" * 100))
        queue.put_nowait((49, b"\x00\x00" * 100))
        worker = asyncio.create_task(media._transcribe(queue, selected))
        try:
            await wait_for(lambda: emitted)
            self.assertEqual([(e.source, e.uid, e.text) for e in emitted], [("voice", "", "小猫 在吗")])
            self.assertFalse(seen[-1].exists())
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_only_a_changed_video_frame_emits_an_echo_candidate(self):
        from astrbot_plugin_bilibili_live_chat.media import MediaRuntime, MediaSettings

        frames = []
        for color in ("black", "white", "white"):
            with BytesIO() as buffer:
                Image.new("RGB", (32, 32), color).save(buffer, "JPEG")
                frames.append(buffer.getvalue())
        reader = asyncio.StreamReader()
        reader.feed_data(b"".join(frames))
        reader.feed_eof()
        emitted = []
        media = MediaRuntime(MediaSettings(video=True), None, 100, None, emitted.append)
        await media._video(reader)
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].source, "vision")
        self.assertEqual(emitted[0].frame, frames[1])
        self.assertEqual(media.latest_frame(), frames[2])

    async def test_vision_defaults_to_dialogue_model_and_falls_back_to_text(self):
        from astrbot_plugin_bilibili_live_chat.conversation import Job, RoomEvent

        module = load_plugin()
        config = Config(
            ROOM_IDS=["12"], ENABLED_ROOMS=["12"], ROOM_MEDIA={"12": {"video": True}},
            KEYWORDS=["小猫"], MODEL_PROVIDER_ID="chat-model", SESSDATA="cookie", BILI_JCT="csrf", DEDE_USER_ID="42",
        )
        calls = []

        class TextContext(Context):
            async def llm_generate(self, **kwargs):
                calls.append(kwargs)
                if kwargs.get("image_urls"):
                    raise ValueError("model does not support images")
                return SimpleNamespace(completion_text='{"reply":"我在"}')

        async def no_capture(self, mode):
            await asyncio.sleep(20)

        plugin = module.LiveChatPlugin(TextContext(), config)
        self.addAsyncCleanup(plugin.terminate)
        with patch.object(module, "BilibiliClient", FakeClient), patch.object(module.MediaRuntime, "_run", no_capture):
            await plugin.initialize()
            await wait_for(lambda: plugin.rooms.live("12"))
            live = plugin.rooms.live("12")
            live.media.frame, live.media.frame_at = b"jpeg", live.media.clock()
            job = Job(RoomEvent("voice", "", "语音", "小猫，屏幕是什么", "voice"), "wake", 0, 1, ())
            self.assertEqual(await live.generate(job), '{"reply":"我在"}')
        self.assertEqual([call["chat_provider_id"] for call in calls], ["chat-model", "chat-model"])
        self.assertIn("image_urls", calls[0])
        self.assertNotIn("image_urls", calls[1])
        self.assertIn("支持图片输入", live.media.errors["vision"])
