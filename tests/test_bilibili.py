import unittest

from astrbot_plugin_bilibili_live_chat.bilibili import (
    BilibiliClient,
    Credentials,
    SendUnknown,
    parse_history,
    qr_credentials,
)


class FakeClient(BilibiliClient):
    def __init__(self):
        super().__init__(Credentials("example", "csrf", "42"))
        self.requests = []
        self.payload = {"code": 0, "data": {}}

    async def _request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return self.payload, {}


class BilibiliTests(unittest.IsolatedAsyncioTestCase):
    def test_history_sorted_and_names_normalized(self):
        rows = [
            {"rnd": "3", "uid": 7, "nickname": "later", "text": "  later  ", "timeline": "2026-11-10 12:00:03"},
            {"rnd": "2", "uid": 8, "nickname": "early", "text": "earlier", "timeline": "2026-11-10 12:00:02"},
            {"uid": 0, "text": "without uid"},
            {"uid": 8, "text": " "},
            "not a row",
        ]
        self.assertEqual([(row.uid, row.name, row.text) for row in parse_history(rows)],
                         [("8", "early", "earlier"), ("7", "later", "later")])

    def test_uid_and_message_disambiguate_same_rnd(self):
        rows = [
            {"rnd": "1", "uid": 1, "text": "same"},
            {"rnd": "1", "uid": 2, "text": "same"},
            {"rnd": "1", "uid": 1, "text": "other"},
        ]
        self.assertEqual(len({row.key for row in parse_history(rows)}), 3)

    def test_qr_cookies_override_redirect_values(self):
        url = "https://www.bilibili.com/?SESSDATA=old%2Bdata&bili_jct=csrf&DedeUserID=42"
        self.assertEqual(qr_credentials(url, {"SESSDATA": "new"}), Credentials("new", "csrf", "42"))
        self.assertIsNone(qr_credentials("https://example.com/?SESSDATA=s&bili_jct=c&DedeUserID=1", {}))

    async def test_send_uses_canonical_room_and_csrf_once(self):
        client = FakeClient()
        await client.send(100, "你好")
        self.assertEqual(len(client.requests), 1)
        method, url, kwargs = client.requests[0]
        self.assertEqual((method, url), ("POST", "https://api.live.bilibili.com/msg/send"))
        self.assertEqual(kwargs["data"]["roomid"], 100)
        self.assertEqual(kwargs["data"]["msg"], "你好")
        self.assertEqual(kwargs["data"]["csrf"], "csrf")
        self.assertEqual(kwargs["data"]["csrf_token"], "csrf")

    async def test_malformed_send_response_is_unknown(self):
        client = FakeClient()
        client.payload = {}
        with self.assertRaises(SendUnknown):
            await client.send(100, "你好")
        self.assertEqual(len(client.requests), 1)
