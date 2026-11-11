import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp
import brotli
from aiohttp import web
from test_live_protocol import command
from test_plugin import wait_for

from astrbot_plugin_bilibili_live_chat.bilibili import (
    AuthExpired,
    BilibiliClient,
    BilibiliError,
    Credentials,
)
from astrbot_plugin_bilibili_live_chat.conversation import (
    Conversation,
    ConversationSettings,
)
from astrbot_plugin_bilibili_live_chat.live_protocol import (
    PacketDecoder,
    ProtocolError,
    packet,
)
from astrbot_plugin_bilibili_live_chat.live_socket import LiveSocket, SocketRejected
from astrbot_plugin_bilibili_live_chat.session import LiveSession


class FakeSocket:
    def __init__(self, messages=()):
        self.incoming = asyncio.Queue()
        for data in messages:
            self.incoming.put_nowait(
                aiohttp.WSMessage(aiohttp.WSMsgType.BINARY, data, None)
            )
        self.sent = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def send_bytes(self, data):
        self.sent.append(data)

    async def receive(self, timeout=None):
        return await asyncio.wait_for(self.incoming.get(), timeout)


class FakeSession:
    def __init__(self, socket):
        self.socket = socket
        self.calls = []

    def ws_connect(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.socket

    async def close(self):
        self.socket.closed = True


class SocketClient(BilibiliClient):
    def __init__(self, socket):
        super().__init__(Credentials("private-cookie", "private-csrf", "42"))
        self.session = FakeSession(socket)
        self.requests = []
        self.expired = False
        self.host = "broadcastlv.chat.bilibili.com"

    async def _request(self, method, url, **kwargs):
        self.requests.append((url, kwargs))
        if url.endswith("/nav"):
            return {
                "code": 0,
                "data": {
                    "isLogin": not self.expired,
                    "mid": 42,
                    "wbi_img": {
                        "img_url": "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png",
                        "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png",
                    },
                },
            }, {}
        if url.endswith("/spi"):
            return {"code": 0, "data": {"b_3": "device-buvid"}}, {}
        return {
            "code": 0,
            "data": {
                "token": "private-ws-token",
                "host_list": [{"host": self.host, "wss_port": 443}],
            },
        }, {}


class SocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_websocket_compressed_stream_to_preview_never_posts(self):
        heartbeats = []
        authenticated = []

        async def endpoint(request):
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            auth = await socket.receive()
            operation, body = PacketDecoder().feed(auth.data)[0]
            authenticated.append((operation, json.loads(body)["uid"]))
            ack = packet(8, {"code": 0})
            await socket.send_bytes(ack[:7])
            await socket.send_bytes(ack[7:])
            content = packet(5, command(uid=0), version=0) + packet(
                5, command(), version=0
            )
            await socket.send_bytes(packet(5, brotli.compress(content), version=3))
            async for message in socket:
                if message.type == aiohttp.WSMsgType.BINARY:
                    for op, _ in PacketDecoder().feed(message.data):
                        if op == 2:
                            heartbeats.append(op)
                            await socket.send_bytes(packet(3, b"\x00" * 4))
            return socket

        async def initialize(request):
            return web.json_response({"code": 0, "data": {}})

        app = web.Application()
        app.router.add_get("/init", initialize)
        app.router.add_get("/ws", endpoint)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        base = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
        client = BilibiliClient(Credentials("private-cookie", "csrf", "42"))
        client.history = AsyncMock(return_value=[])
        client.send = AsyncMock(side_effect=AssertionError("must not post"))
        observer = AsyncMock()
        chat = Conversation(ConversationSettings.from_config({"KEYWORDS": ["小猫"]}))
        live = LiveSession(
            client,
            100,
            "42",
            chat,
            AsyncMock(return_value='{"reply":"收到"}'),
            preview=True,
            listen_mode="auto",
            poll_seconds=0.005,
            observer=observer,
        )
        try:
            await client._request("GET", base + "/init")
            with patch.object(
                LiveSocket,
                "_bootstrap",
                AsyncMock(
                    return_value=(
                        base + "/ws",
                        {"Cookie": "SESSDATA=private-cookie"},
                        {"uid": 42, "key": "private-ws-token"},
                    )
                ),
            ):
                await live.start()
                await wait_for(lambda: live.previews == 1 and bool(heartbeats))
                self.assertEqual(live.receiver.source, "websocket")
                self.assertEqual(live.received, 1)
                self.assertEqual(authenticated, [(7, 42)])
                client.send.assert_not_awaited()
                observer.assert_not_awaited()
                await live.stop()
            self.assertTrue(all(task.done() for task in live.tasks))
        finally:
            await live.stop()
            await runner.cleanup()

    async def test_upgrade_redirect_is_blocked_before_target_receives_credentials(self):
        target_calls = []

        async def target(request):
            target_calls.append(request.headers.get("Cookie"))
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            await socket.receive()
            await socket.send_bytes(packet(8, {"code": 0}))
            return socket

        async def redirect(request):
            raise web.HTTPFound("/target")

        async def initialize(request):
            return web.json_response({"code": 0, "data": {}})

        app = web.Application()
        app.router.add_get("/init", initialize)
        app.router.add_get("/redirect", redirect)
        app.router.add_get("/target", target)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        base = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
        client = BilibiliClient(Credentials("private-cookie", "csrf", "42"))
        stream = None
        try:
            await client._request("GET", base + "/init")
            with patch.object(
                LiveSocket,
                "_bootstrap",
                AsyncMock(
                    return_value=(
                        base + "/redirect",
                        {"Cookie": "SESSDATA=private-cookie"},
                        {"key": "private-ws-token"},
                    )
                ),
            ):
                stream = LiveSocket(client, 100).events()
                with self.assertRaises((BilibiliError, ProtocolError)):
                    await asyncio.wait_for(anext(stream), 1)
            self.assertEqual(target_calls, [])
        finally:
            if stream:
                await stream.aclose()
            await client.close()
            await runner.cleanup()

    async def test_login_is_reused_auth_precedes_events_and_context_closes(self):
        socket = FakeSocket(
            [
                packet(5, command("before auth"), version=0),
                packet(8, {"code": 0})
                + packet(3, b"\x00" * 4)
                + packet(5, command(), version=0),
            ]
        )
        client = SocketClient(socket)
        stream = LiveSocket(client, 100).events()
        try:
            notices = [await anext(stream) for _ in range(3)]
            self.assertEqual(
                [notice.kind for notice in notices], ["ready", "heartbeat", "danmaku"]
            )
            self.assertEqual(notices[-1].event.text, "小猫你好")
            auth = json.loads(PacketDecoder().feed(socket.sent[0])[0][1])
            self.assertEqual(
                (auth["uid"], auth["roomid"], auth["protover"]), (42, 100, 3)
            )
            self.assertEqual(auth["key"], "private-ws-token")
            self.assertEqual(auth["buvid"], "device-buvid")
            self.assertIn(
                "private-cookie", client.session.calls[0][1]["headers"]["Cookie"]
            )
            signed = client.requests[-1][1]["params"]
            self.assertIn("w_rid", signed)
        finally:
            await stream.aclose()
        self.assertTrue(socket.closed)

    async def test_token_denial_is_not_assumed_to_be_expired_qr_login(self):
        socket = FakeSocket([packet(8, {"code": -101, "message": "private-ws-token"})])
        stream = LiveSocket(SocketClient(socket), 100).events()
        with self.assertRaises(SocketRejected) as caught:
            await anext(stream)
        self.assertNotIn("private-ws-token", str(caught.exception))
        self.assertTrue(socket.closed)

    async def test_unsupported_danmaku_format_requests_fallback_instead_of_silent_stall(
        self,
    ):
        socket = FakeSocket(
            [
                packet(8, {"code": 0}),
                packet(5, {"cmd": "DANMU_MSG_V2", "data": "opaque"}, version=0),
            ]
        )
        stream = LiveSocket(SocketClient(socket), 100).events()
        self.assertEqual((await anext(stream)).kind, "ready")
        with self.assertRaisesRegex(ProtocolError, "格式"):
            await asyncio.wait_for(anext(stream), 0.05)

    async def test_account_expiry_and_untrusted_server_do_not_open_socket(self):
        for expired, host, error in [
            (True, "broadcastlv.chat.bilibili.com", AuthExpired),
            (False, "127.0.0.1", ProtocolError),
        ]:
            client = SocketClient(FakeSocket())
            client.expired, client.host = expired, host
            stream = LiveSocket(client, 100).events()
            with self.assertRaises(error):
                await anext(stream)
            self.assertEqual(client.session.calls, [])

    async def test_auth_and_heartbeat_timeouts_are_bounded(self):
        waiting = FakeSocket()
        stream = LiveSocket(SocketClient(waiting), 100, auth_timeout=0.02).events()
        with self.assertRaises(ProtocolError):
            await anext(stream)
        self.assertTrue(waiting.closed)
        socket = FakeSocket([packet(8, {"code": 0})])
        stream = LiveSocket(
            SocketClient(socket), 100, heartbeat_seconds=0.01, heartbeat_timeout=0.035
        ).events()
        self.assertEqual((await anext(stream)).kind, "ready")
        with self.assertRaises(ProtocolError):
            await anext(stream)
        self.assertGreaterEqual(len(socket.sent), 3)
        self.assertTrue(socket.closed)

    async def test_busy_unrelated_messages_do_not_extend_heartbeat_deadline(self):
        socket = FakeSocket([packet(8, {"code": 0})])
        stream = LiveSocket(
            SocketClient(socket), 100, heartbeat_seconds=0.01, heartbeat_timeout=0.035
        ).events()
        await anext(stream)

        async def noise():
            while True:
                socket.incoming.put_nowait(
                    aiohttp.WSMessage(
                        aiohttp.WSMsgType.BINARY,
                        packet(5, {"cmd": "OTHER"}, version=0),
                        None,
                    )
                )
                await asyncio.sleep(0.002)

        producer = asyncio.create_task(noise())
        try:
            with self.assertRaises(ProtocolError):
                await asyncio.wait_for(anext(stream), 0.2)
        finally:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
        self.assertTrue(socket.closed)


if __name__ == "__main__":
    unittest.main()
