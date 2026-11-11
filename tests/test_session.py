import asyncio
import unittest

from astrbot_plugin_bilibili_live_chat.bilibili import AuthExpired, SendUnknown
from astrbot_plugin_bilibili_live_chat.conversation import (
    Conversation,
    ConversationSettings,
    Danmaku,
    SendPacer,
)
from astrbot_plugin_bilibili_live_chat.session import LiveSession


class FakeRoom:
    def __init__(self, snapshots):
        self.snapshots = iter(snapshots)
        self.polls = 0
        self.sent = []
        self.closed = False
        self.send_error = None

    async def history(self, room):
        self.polls += 1
        result = next(self.snapshots, [])
        if isinstance(result, Exception):
            raise result
        return result

    async def send(self, room, reply):
        self.sent.append((room, reply))
        if self.send_error:
            raise self.send_error

    async def close(self):
        self.closed = True


def row(key, text="小猫", uid="7"):
    return Danmaku(key, uid, "观众", text)


async def model(job):
    return '{"reply":"在呢"}'


class LiveSessionTests(unittest.IsolatedAsyncioTestCase):
    def create(self, room, generate=model, clock=None, valid=lambda: True, observer=None, *, room_id=100, pacing=None):
        settings = ConversationSettings.from_config({"KEYWORDS": ["小猫"]})
        session = LiveSession(room, room_id, "42", Conversation(settings, pacing=pacing), generate,
                              poll_seconds=0.005, model_timeout=1, clock=clock,
                              valid=valid, observer=observer)
        self.addAsyncCleanup(session.stop)
        return session

    async def until(self, condition):
        async with asyncio.timeout(1):
            while not condition():
                await asyncio.sleep(0.005)

    async def test_first_snapshot_and_own_rows_do_not_reply(self):
        old = row("old")
        own = row("own", uid="42")
        blocked = row("blocked", uid="8")
        room = FakeRoom([[old], [old, own, blocked], [row("new"), old], [row("new")]])
        seen = []

        async def generate(job):
            seen.append(job.message.key)
            return await model(job)

        session = self.create(room, generate)
        session.ignored_users = frozenset({"8"})
        await session.start()
        await self.until(lambda: room.polls >= 4)
        self.assertEqual(seen, ["new"])
        self.assertEqual(room.sent, [(100, "在呢")])

    async def test_polling_continues_during_model_and_stop_prevents_send(self):
        room = FakeRoom([[], [row("new")], [row("another")]])
        entered = asyncio.Event()
        canceled = asyncio.Event()

        async def generate(job):
            entered.set()
            try:
                await asyncio.sleep(10)
            finally:
                canceled.set()

        session = self.create(room, generate)
        await session.start()
        await self.until(lambda: entered.is_set() and room.polls >= 3)
        await session.stop()
        self.assertTrue(canceled.is_set())
        self.assertTrue(room.closed)
        self.assertEqual(room.sent, [])

    async def test_stale_generation_is_discarded(self):
        room = FakeRoom([[], [row("new")]])
        ticks = [1]

        async def generate(job):
            ticks[0] = 92
            return await model(job)

        session = self.create(room, generate, clock=lambda: ticks[0])
        await session.start()
        await self.until(lambda: room.polls >= 3)
        self.assertEqual(room.sent, [])

    async def test_changed_configuration_drops_in_flight_reply(self):
        room = FakeRoom([[], [row("new")]])
        current = [True]

        async def generate(job):
            current[0] = False
            return await model(job)

        session = self.create(room, generate, valid=lambda: current[0])
        await session.start()
        await self.until(lambda: not session.running)
        self.assertEqual(room.sent, [])

    async def test_unknown_send_is_never_retried_or_recorded(self):
        room = FakeRoom([[], [row("new")], [row("new"), row("second")]])
        room.send_error = SendUnknown()
        observed = []

        async def observer(job, text):
            observed.append((job.message.key, text))

        session = self.create(room, observer=observer)
        await session.start()
        await self.until(lambda: room.polls >= 5)
        self.assertEqual(room.sent, [(100, "在呢")])
        self.assertEqual(observed, [])
        self.assertEqual(session.send_error, "发送结果未知")

    async def test_auth_expiry_halts_both_tasks(self):
        room = FakeRoom([[], AuthExpired()])
        session = self.create(room)
        await session.start()
        await self.until(lambda: room.closed)
        self.assertFalse(session.running)
        self.assertEqual(session.stop_reason, "登录已失效，请重新扫码")

    async def test_two_rooms_finish_generation_together_but_share_send_cooldown(self):
        pacing = SendPacer()
        release = asyncio.Event()
        generated = []
        first = FakeRoom([[], [row("first")]])
        second = FakeRoom([[], [row("second")]])

        async def generate(job):
            generated.append(job.message.key)
            await release.wait()
            return await model(job)

        await self.create(first, generate, pacing=pacing).start()
        await self.create(second, generate, room_id=101, pacing=pacing).start()
        await self.until(lambda: len(generated) == 2)
        release.set()
        await self.until(lambda: first.sent or second.sent)
        await asyncio.sleep(0.025)
        self.assertEqual(len(first.sent) + len(second.sent), 1)
