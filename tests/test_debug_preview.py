import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_dashboard import Context
from test_plugin import Config, FakeClient, load_plugin, wait_for
from test_session import FakeRoom, model, row

from astrbot_plugin_bilibili_live_chat.conversation import (
    Conversation,
    ConversationSettings,
    RoomEvent,
    SendPacer,
)
from astrbot_plugin_bilibili_live_chat.dashboard import Dashboard, DashboardError
from astrbot_plugin_bilibili_live_chat.media import MediaRuntime, MediaSettings
from astrbot_plugin_bilibili_live_chat.rooms import RoomRuntime
from astrbot_plugin_bilibili_live_chat.session import LiveSession


class DebugPreviewTests(unittest.IsolatedAsyncioTestCase):
    def session(self, client, generate=model, pacing=None, **kwargs):
        chat = Conversation(ConversationSettings.from_config({'KEYWORDS': ['小猫']}), pacing=pacing)
        live = LiveSession(client, 100, '42', chat, generate, poll_seconds=0.005, **kwargs)
        self.addAsyncCleanup(live.stop)
        return live

    async def test_preview_generates_without_sending_recording_or_account_budget(self):
        client = FakeRoom([[], [row('wake')]])
        observer = AsyncMock()
        pacing = SendPacer()
        live = self.session(client, preview=True, observer=observer, pacing=pacing)
        await live.start()
        await wait_for(lambda: live.previews == 1)
        self.assertEqual(client.sent, [])
        observer.assert_not_awaited()
        self.assertEqual(live.replies, 0)
        self.assertEqual(list(pacing.attempts), [])
        self.assertGreater(live.conversation.delay(live.clock()), 0)
        entry = live.debug.snapshot()['events'][-1]
        self.assertEqual((entry['status'], entry['reply'], entry['keyword']), ('preview', '在呢', '小猫'))
        self.assertIsNone(entry['timings']['send'])
        self.assertGreaterEqual(entry['timings']['model'], 0)

    async def test_transcription_exposes_measured_collection_and_stt_time(self):
        ticks = [12.0]
        emitted = []
        skipped = []

        async def transcribe(path):
            ticks[0] += 0.5
            return '小猫'

        media = MediaRuntime(MediaSettings(audio=True), None, 100, None, emitted.append,
                             clock=lambda: ticks[0], report=lambda event, reason: skipped.append(reason))
        queue = asyncio.Queue()
        queue.put_nowait((11.0, b'\x00\x00' * 16000, 10.0))
        worker = asyncio.create_task(media._transcribe(queue, SimpleNamespace(get_text=transcribe)))
        try:
            await wait_for(lambda: emitted)
            event = emitted[0]
            self.assertEqual(event.captured_at, 10.0)
            self.assertEqual(event.collection_ms, 1000)
            self.assertEqual(event.transcription_queue_ms, 1000)
            self.assertEqual(event.transcription_ms, 500)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_timings_use_capture_transcription_queue_model_and_actual_send(self):
        ticks = [10.0]
        client = FakeRoom([])

        async def generate(job):
            ticks[0] += 0.2
            return await model(job)

        async def send(room, text):
            ticks[0] += 0.1

        client.send = AsyncMock(side_effect=send)
        live = self.session(client, generate, clock=lambda: ticks[0])
        live.running = True
        event = RoomEvent('voice', '', '语音', '小猫', 'voice', captured_at=8.0,
                          collection_ms=1000, transcription_ms=500, transcription_queue_ms=100)
        live.offer_media(event)
        ticks[0] = 10.3
        job = live.conversation.take(ticks[0])
        await live._process(job)
        timing = live.debug.snapshot()['events'][-1]['timings']
        self.assertAlmostEqual(timing['collection'], 1000)
        self.assertAlmostEqual(timing['transcription'], 500)
        self.assertAlmostEqual(timing['queue'], 300)
        self.assertAlmostEqual(timing['model'], 200)
        self.assertAlmostEqual(timing['send'], 100)
        self.assertAlmostEqual(timing['total'], 2600)
        stats = live.debug.snapshot()['statistics']
        self.assertEqual(stats['sent']['count'], 1)
        self.assertEqual(stats['sent']['averages']['send'], 100)
        self.assertEqual(stats['preview']['count'], 0)

    async def test_failed_transcription_keeps_elapsed_time_without_provider_secrets(self):
        ticks = [10.0]
        rejected = []

        async def transcribe(path):
            ticks[0] += 0.75
            raise RuntimeError('secret-stt-token')

        media = MediaRuntime(MediaSettings(audio=True), None, 100, None, lambda event: None,
                             clock=lambda: ticks[0], report=lambda event, reason: rejected.append((event, reason)))
        queue = asyncio.Queue()
        queue.put_nowait((9.0, b'\x00\x00' * 16000, 8.0))
        worker = asyncio.create_task(media._transcribe(queue, SimpleNamespace(get_text=transcribe)))
        try:
            await wait_for(lambda: rejected)
            self.assertEqual(rejected[0][0].transcription_ms, 750)
            self.assertNotIn('secret-stt-token', str(rejected))
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_waiting_time_updates_live_and_is_kept_when_cancelled(self):
        ticks = [10.0]
        live = self.session(FakeRoom([]), clock=lambda: ticks[0])
        live.running = True
        live.offer_media(row('waiting'))
        live.debug.update('waiting', 'waiting', '等待限流')
        ticks[0] = 11.0
        self.assertEqual(live.debug.snapshot()['events'][-1]['timings']['rate_limit'], 1000)
        ticks[0] = 12.0
        await live.stop()
        entry = live.debug.snapshot()['events'][-1]
        self.assertEqual(entry['status'], 'cancelled')
        self.assertEqual(entry['timings']['rate_limit'], 2000)

    async def test_decisions_model_silence_invalid_output_and_failure_are_visible(self):
        live = self.session(FakeRoom([]))
        live.running = True
        live.offer_media(row('quiet', '普通文字'))
        first = live.debug.snapshot()['events'][-1]
        self.assertEqual(first['status'], 'skipped')
        self.assertIn('附和', first['reason'])
        for key, result, status in [('silent', '{"reply":""}', 'silent'), ('bad', 'not json', 'invalid')]:
            live.generate = AsyncMock(return_value=result)
            live.offer_media(row(key))
            await live._process(live.conversation.take(live.clock()))
            self.assertEqual(live.debug.snapshot()['events'][-1]['status'], status)
        live.generate = AsyncMock(side_effect=RuntimeError('secret-provider-token'))
        live.offer_media(row('error'))
        await live._process(live.conversation.take(live.clock()))
        report = live.debug.snapshot()
        self.assertEqual(report['events'][-1]['status'], 'error')
        self.assertNotIn('secret-provider-token', str(report))

    async def test_trace_is_bounded_room_local_and_marks_replaced_work(self):
        live = self.session(FakeRoom([]))
        live.running = True
        live.offer_media(row('first'))
        live.offer_media(row('second'))
        events = live.debug.snapshot()['events']
        self.assertEqual(events[0]['status'], 'replaced')
        for index in range(70):
            live.offer_media(row(str(index), '继续聊'))
        self.assertEqual(len(live.debug.snapshot()['events']), 50)
        self.assertEqual(self.session(FakeRoom([])).debug.snapshot()['events'], [])

    async def test_preview_preference_survives_restart_and_never_uses_real_account_pacing(self):
        module = load_plugin()
        config = Config(ROOM_IDS=['12'], ENABLED_ROOMS=[], KEYWORDS=['小猫'], MODEL_PROVIDER_ID='chat-model',
                        SESSDATA='cookie', BILI_JCT='csrf', DEDE_USER_ID='42')
        bot = module.LiveChatPlugin(Context(), config)
        self.addAsyncCleanup(bot.terminate)
        ui = Dashboard(bot)
        for room, value in [('12', 'true'), ('999', True), ([], True)]:
            with self.assertRaises(DashboardError):
                await ui.preview_settings(room, value)
        await ui.preview_settings('12', True)
        self.assertEqual(config.saved['ROOM_PREVIEW'], {'12': True})
        restarted = module.LiveChatPlugin(Context(), Config(config.saved))
        self.addAsyncCleanup(restarted.terminate)
        self.assertTrue(restarted.rooms.preview_enabled('12'))
        with patch.object(module, 'BilibiliClient', FakeClient):
            await ui.listen('12', True)
            await wait_for(lambda: bot.rooms.live('12'))
            live = bot.rooms.live('12')
            self.assertTrue(live.preview)
            self.assertNotIn('42', bot.pacers)
            self.assertTrue(ui.snapshot()['rooms'][0]['preview'])
            await ui.preview_settings('12', False)
            self.assertFalse(live.valid())
            await wait_for(lambda: bot.rooms.live('12') is not None and bot.rooms.live('12') is not live)
            self.assertFalse(bot.rooms.live('12').preview)

    async def test_mode_change_cancels_inflight_actual_reply_before_any_send(self):
        module = load_plugin()
        sent = []
        entered, cancelled = asyncio.Event(), asyncio.Event()

        class Client(FakeClient):
            async def send(self, room, text):
                sent.append(text)

        async def generate(**kwargs):
            entered.set()
            try:
                await asyncio.sleep(20)
            finally:
                cancelled.set()

        config = Config(ROOM_IDS=['12'], ENABLED_ROOMS=[], KEYWORDS=['小猫'], MODEL_PROVIDER_ID='chat-model',
                        SESSDATA='cookie', BILI_JCT='csrf', DEDE_USER_ID='42')
        context = Context()
        context.llm_generate = generate
        bot = module.LiveChatPlugin(context, config)
        self.addAsyncCleanup(bot.terminate)
        ui = Dashboard(bot)
        with patch.object(module, 'BilibiliClient', Client):
            await ui.listen('12', True)
            await wait_for(lambda: bot.rooms.live('12'))
            prior = bot.rooms.live('12')
            prior.offer_media(row('wake'))
            await wait_for(entered.is_set)
            await ui.preview_settings('12', True)
            self.assertTrue(cancelled.is_set())
            self.assertFalse(prior.valid())
            self.assertEqual(sent, [])
            self.assertEqual(prior.debug.snapshot()['events'][-1]['status'], 'cancelled')

    async def test_debug_and_preview_routes_require_authentication_before_reading_body(self):
        module = load_plugin()
        context = Context()
        bot = module.LiveChatPlugin(context, Config(ROOM_IDS=['12']))
        self.addAsyncCleanup(bot.terminate)
        body = AsyncMock()
        with patch('astrbot_plugin_bilibili_live_chat.dashboard._transport',
                   return_value=(lambda: None, body, lambda data, status: (status, data))):
            await bot.initialize()
        for endpoint in ('debug', 'rooms/preview'):
            status, _ = await context.routes[(f'/{module.PLUGIN_NAME}/{endpoint}', 'POST')]()
            self.assertEqual(status, 401)
        body.assert_not_awaited()

    async def test_debug_frame_revision_avoids_retransmitting_unchanged_image(self):
        module = load_plugin()
        bot = module.LiveChatPlugin(Context(), Config(ROOM_IDS=['12']))
        self.addAsyncCleanup(bot.terminate)
        ui = Dashboard(bot)
        report = await ui.debug_status('12', '')
        self.assertEqual(report['events'], [])
        with self.assertRaises(DashboardError):
            await ui.debug_status('99', '')
        live = self.session(FakeRoom([]))
        live.running = True
        live.media = MediaRuntime(MediaSettings(video=True), None, 100, None, lambda event: None, clock=lambda: 6)
        live.media.frame, live.media.frames, live.media.frame_at = b'jpeg', 3, 5
        bot.rooms.workers['12'] = RoomRuntime(live=live, task=asyncio.create_task(asyncio.sleep(10)))
        first = await ui.debug_status('12', '')
        second = await ui.debug_status('12', first['frame']['revision'])
        self.assertTrue(first['frame']['image'].startswith('data:image/jpeg;base64,'))
        self.assertIsNone(second['frame']['image'])
        self.assertEqual(first['frame']['age_ms'], 1000)
        self.assertNotIn('frame', ui.snapshot()['rooms'][0]['media'])


if __name__ == '__main__':
    unittest.main()
