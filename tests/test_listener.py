import asyncio
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from test_live_protocol import command
from test_live_socket import FakeSocket, SocketClient
from test_plugin import wait_for
from test_session import FakeRoom, row

from astrbot_plugin_bilibili_live_chat.bilibili import AuthExpired, parse_history
from astrbot_plugin_bilibili_live_chat.conversation import (
    Conversation,
    ConversationSettings,
)
from astrbot_plugin_bilibili_live_chat.live_protocol import (
    ProtocolError,
    danmaku_event,
    packet,
)
from astrbot_plugin_bilibili_live_chat.live_socket import StreamNotice
from astrbot_plugin_bilibili_live_chat.session import LiveSession


class FallbackClient(FakeRoom):
    def __init__(self):
        super().__init__([])
        self.connections = 0
        self.release = asyncio.Event()
        self.socket_closed = 0
        self.history_rows = []
        self.fail = True

    async def history(self, room):
        self.polls += 1
        return self.history_rows

    async def live_events(self, room, *, attempt=0):
        self.connections += 1
        if self.fail:
            raise ProtocolError("模拟连接失败")
        try:
            yield StreamNotice("ready")
            yield StreamNotice(
                "danmaku", replace(row("ws", "小猫"), server_time=1794369600)
            )
            await self.release.wait()
            raise ProtocolError("模拟断线")
        finally:
            self.socket_closed += 1


class ListenerTests(unittest.IsolatedAsyncioTestCase):
    def live(self, client, **kwargs):
        settings = ConversationSettings.from_config({"KEYWORDS": ["小猫"]})
        generate = AsyncMock(return_value='{"reply":"收到"}')
        live = LiveSession(
            client,
            100,
            "42",
            Conversation(settings),
            generate,
            poll_seconds=0.005,
            listen_mode="auto",
            listener_retry_seconds=0.02,
            preview=True,
            **kwargs,
        )
        self.addAsyncCleanup(live.stop)
        return live

    async def test_full_fake_socket_auth_to_preview_never_sends_or_records(self):
        socket = FakeSocket(
            [
                packet(8, {"code": 0})
                + packet(3, b"\x00" * 4)
                + packet(5, command(), version=0)
            ]
        )
        client = SocketClient(socket)
        client.history = AsyncMock(return_value=[])
        client.send = AsyncMock(side_effect=AssertionError("must not send"))
        observer = AsyncMock()
        live = self.live(client, observer=observer)
        await live.start()
        await wait_for(lambda: live.previews == 1)
        self.assertEqual(live.receiver.snapshot()["source"], "websocket")
        self.assertEqual(live.debug.snapshot()["events"][-1]["channel"], "websocket")
        client.send.assert_not_awaited()
        observer.assert_not_awaited()
        await live.stop()
        self.assertTrue(socket.closed)
        self.assertTrue(all(task.done() for task in live.tasks))

    def test_real_history_shape_and_wire_message_share_a_dedup_timestamp(self):
        event = danmaku_event(command())
        timeline = datetime.fromtimestamp(
            event.server_time, timezone(timedelta(hours=8))
        ).strftime("%Y-%m-%d %H:%M:%S")
        history = parse_history(
            [
                {
                    "uid": 7,
                    "nickname": "观众",
                    "text": event.text,
                    "timeline": timeline,
                    "rnd": 123,
                }
            ]
        )[0]
        self.assertEqual(history.server_time, event.server_time)
        self.assertEqual(history.channel, "poll")

    async def test_failure_keeps_polling_then_promotes_and_stops_poll_delivery(self):
        client = FallbackClient()
        live = self.live(client)
        await live.start()
        await wait_for(lambda: client.polls >= 2 and client.connections >= 1)
        self.assertEqual(live.receiver.snapshot()["source"], "poll")
        self.assertTrue(live.receiver.snapshot()["reason"])
        client.fail = False
        await wait_for(lambda: live.previews == 1)
        report = live.receiver.snapshot()
        self.assertEqual(report["source"], "websocket")
        self.assertEqual(report["reason"], "")
        polls = client.polls
        await asyncio.sleep(0.02)
        self.assertEqual(client.polls, polls)
        await live.stop()
        self.assertEqual(client.socket_closed, 1)

    async def test_fallback_baseline_and_cross_transport_dedup_do_not_replay(self):
        client = FallbackClient()
        client.fail = False
        live = self.live(client)
        await live.start()
        await wait_for(lambda: live.previews == 1)
        client.history_rows = [replace(row("history-old"), server_time=1794369600)]
        client.fail = True
        client.release.set()
        polls = client.polls
        await wait_for(lambda: client.polls > polls)
        client.history_rows.append(
            replace(row("history-delayed"), server_time=1794369600)
        )
        await wait_for(lambda: client.polls > polls + 2)
        self.assertEqual(live.received, 1)
        self.assertEqual(live.previews, 1)
        self.assertTrue(
            any("重复" in entry["reason"] for entry in live.debug.snapshot()["events"])
        )

    async def test_expired_account_halts_both_sources_instead_of_hiding_in_fallback(
        self,
    ):
        class Expired(FallbackClient):
            async def live_events(self, room, *, attempt=0):
                raise AuthExpired()
                yield  # pragma: no cover

        client = Expired()
        live = self.live(client)
        await live.start()
        await wait_for(lambda: client.closed)
        self.assertTrue(live.auth_expired)
        self.assertFalse(live.running)
        self.assertEqual(client.sent, [])

    async def test_room_isolation_and_credential_invalidation_close_only_affected_socket(self):
        current = [True]
        first, second = FallbackClient(), FallbackClient()
        first.fail = second.fail = False
        left = self.live(first, valid=lambda: current[0])
        right = self.live(second)
        await left.start()
        await right.start()
        await wait_for(lambda: left.previews == right.previews == 1)
        self.assertIsNot(left.receiver, right.receiver)
        self.assertEqual((left.received, right.received), (1, 1))
        current[0] = False
        await wait_for(lambda: first.closed)
        self.assertEqual(first.socket_closed, 1)
        self.assertTrue(right.running)
        self.assertEqual(right.receiver.source, 'websocket')

    async def test_poll_inflight_during_promotion_is_ignored_and_stop_cleans_up(self):
        client = FallbackClient()
        client.fail = False
        entered, release = asyncio.Event(), asyncio.Event()

        async def history(room):
            entered.set()
            await release.wait()
            return [row("late-http")]

        client.history = history
        live = self.live(client)
        await live.start()
        await wait_for(lambda: entered.is_set() and live.previews == 1)
        release.set()
        await asyncio.sleep(0.02)
        self.assertEqual(live.received, 1)
        await live.stop()
        self.assertTrue(client.closed)


if __name__ == "__main__":
    unittest.main()
