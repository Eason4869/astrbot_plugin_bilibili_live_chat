import unittest
from types import SimpleNamespace

from astrbot_plugin_bilibili_live_chat.conversation import Danmaku, Job
from astrbot_plugin_bilibili_live_chat.link import BiliBotLink


class FakeAPI:
    def __init__(self):
        self.writes = []

    async def search(self, query, *, user_id, reader_scope, memory_types, limit):
        if (user_id, reader_scope, memory_types) != ("42", "bili_live", {"live"}):
            raise AssertionError("incorrect query scope")
        return [
            {"user_id": "42", "session_id": "bilibot:100:session", "text": "在本房间聊过猫"},
            {"user_id": "42", "session_id": "bilibot:101:session", "text": "另一个直播间"},
            {"user_id": "43", "session_id": "bilibot:100:session", "text": "别人聊过的"},
        ]

    async def record_live_event(self, **fields):
        self.writes.append(fields)


class LinkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.star = SimpleNamespace(
            activated=True,
            module=SimpleNamespace(get_bilibili_ai_bot_api=lambda: self.api),
        )
        self.link = BiliBotLink(SimpleNamespace(get_registered_star=lambda name: self.star))
        self.job = Job(Danmaku("event", "42", "观众", "小猫"), "wake", 0, 1, ())

    async def test_recall_stays_in_current_room_and_uid(self):
        self.assertEqual(await self.link.recall(100, self.job), ["在本房间聊过猫"])
        self.star.activated = False
        self.assertEqual(await self.link.recall(100, self.job), [])

    async def test_write_has_stable_room_event_identity(self):
        await self.link.record(100, "bilibili-live-chat:100:run", self.job, "猫猫来了")
        result, = self.api.writes
        self.assertEqual(result["event_id"], "bilibili-live-chat:100:event")
        self.assertEqual(result["user_id"], "42")
        self.assertEqual(result["room_id"], "100")
        self.assertIn("猫猫来了", result["content"])

    async def test_broken_optional_api_is_nonfatal(self):
        self.star.module.get_bilibili_ai_bot_api = lambda: object()
        self.assertEqual(await self.link.recall(100, self.job), [])
        await self.link.record(100, "bilibili-live-chat:100:run", self.job, "猫猫来了")
