"""One authenticated connection; ephemeral secrets never leave this transport."""

import asyncio
import json
import time
from dataclasses import dataclass
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

import aiohttp

from .bilibili import (
    LIVE,
    LIVE_ORIGIN,
    NAV,
    USER_AGENT,
    AuthExpired,
    BilibiliError,
    uid_text,
)
from .live_protocol import (
    MAX_WIRE,
    PacketDecoder,
    ProtocolError,
    danmaku_event,
    packet,
    sign_wbi,
    websocket_urls,
)


class SocketRejected(BilibiliError):
    pass


@dataclass(frozen=True)
class StreamNotice:
    kind: str
    event: object = None


class LiveSocket:
    def __init__(
        self,
        client,
        room,
        *,
        server_index=0,
        auth_timeout=10,
        heartbeat_seconds=30,
        heartbeat_timeout=75,
    ):
        self.client = client
        self.room = room
        self.server_index = server_index
        self.auth_timeout = auth_timeout
        self.heartbeat_seconds = heartbeat_seconds
        self.heartbeat_timeout = heartbeat_timeout

    async def _bootstrap(self):
        credentials = self.client.credentials
        if not credentials or not credentials.ready:
            raise AuthExpired()
        nav, _ = await self.client._request("GET", NAV, authenticated=True)
        self.client._check_code(nav)
        data = self.client._data(nav)
        if (
            data.get("isLogin") is not True
            or uid_text(data.get("mid")) != credentials.uid
        ):
            raise AuthExpired()
        images = data.get("wbi_img")
        if not isinstance(images, dict):
            raise ProtocolError("登录接口没有提供 WBI 参数，暂用轮询。")
        try:
            img = (
                urlsplit(images.get("img_url", ""))
                .path.rsplit("/", 1)[-1]
                .split(".")[0]
            )
            sub = (
                urlsplit(images.get("sub_url", ""))
                .path.rsplit("/", 1)[-1]
                .split(".")[0]
            )
        except (TypeError, ValueError) as exc:
            raise ProtocolError("登录接口的 WBI 参数无效。") from exc
        buvid = self.client.live_buvid
        if not buvid:
            spi, _ = await self.client._request(
                "GET", "https://api.bilibili.com/x/frontend/finger/spi"
            )
            self.client._check_code(spi)
            buvid = self.client._data(spi).get("b_3")
            if (
                not isinstance(buvid, str)
                or not buvid
                or len(buvid) > 256
                or any(ord(char) < 33 for char in buvid)
            ):
                raise ProtocolError("无法获取长连接所需的设备标识。")
            self.client.live_buvid = buvid
        payload, _ = await self.client._request(
            "GET",
            f"{LIVE}/xlive/web-room/v1/index/getDanmuInfo",
            authenticated=True,
            params=sign_wbi({"id": self.room, "type": 0}, img, sub, int(time.time())),
            headers=self.client._room_headers(self.room),
            extra_cookies={"buvid3": buvid},
        )
        self.client._check_code(payload)
        data = self.client._data(payload)
        urls, token = websocket_urls(data.get("host_list")), data.get("token")
        if not urls or not isinstance(token, str) or not token or len(token) > 4096:
            raise ProtocolError("弹幕服务未返回有效的长连接地址或临时凭证。")
        cookies = SimpleCookie()
        for key, value in {**credentials.cookies(), "buvid3": buvid}.items():
            cookies[key] = value
        headers = {
            **self.client._room_headers(self.room),
            "User-Agent": USER_AGENT,
            "Cookie": cookies.output(header="", sep=";").strip(),
        }
        auth = {
            "uid": int(credentials.uid),
            "roomid": self.room,
            "protover": 3,
            "platform": "web",
            "type": 2,
            "buvid": buvid,
            "key": token,
        }
        return urls[self.server_index % len(urls)], headers, auth

    async def events(self):
        try:
            import brotli

            brotli.Decompressor().process(b"", output_buffer_limit=1)
        except (ImportError, TypeError, AttributeError):
            raise ProtocolError(
                "需要 Brotli 1.2 或更新版本，请更新插件依赖。"
            ) from None
        url, headers, auth = await self._bootstrap()
        decoder = PacketDecoder()
        clock = asyncio.get_running_loop().time
        ws_timeout = (
            aiohttp.ClientWSTimeout(ws_close=10)
            if hasattr(aiohttp, "ClientWSTimeout")
            else 10
        )
        try:
            async with self.client.session.ws_connect(
                url,
                headers=headers,
                origin=LIVE_ORIGIN,
                timeout=ws_timeout,
                autoping=True,
                max_msg_size=MAX_WIRE,
                compress=0,
            ) as socket:
                await asyncio.wait_for(socket.send_bytes(packet(7, auth)), timeout=10)
                authenticated = False
                auth_deadline = clock() + self.auth_timeout
                last_ack, next_heartbeat = clock(), clock()
                while True:
                    now = clock()
                    if not authenticated and now >= auth_deadline:
                        raise ProtocolError("长连接认证超时。")
                    if authenticated:
                        if now - last_ack >= self.heartbeat_timeout:
                            raise ProtocolError("长连接心跳响应超时。")
                        if now >= next_heartbeat:
                            await asyncio.wait_for(
                                socket.send_bytes(packet(2, b"")), timeout=10
                            )
                            next_heartbeat = clock() + self.heartbeat_seconds
                        timeout = min(
                            next_heartbeat - clock(),
                            last_ack + self.heartbeat_timeout - clock(),
                        )
                    else:
                        timeout = auth_deadline - clock()
                    try:
                        message = await socket.receive(timeout=max(0.001, timeout))
                    except asyncio.TimeoutError:
                        continue
                    if message.type in (
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        decoder.finish()
                        raise ProtocolError("直播长连接已断开。")
                    if message.type in (aiohttp.WSMsgType.PING, aiohttp.WSMsgType.PONG):
                        continue
                    if message.type != aiohttp.WSMsgType.BINARY:
                        raise ProtocolError("长连接返回了非二进制消息。")
                    for operation, body in decoder.feed(message.data):
                        if operation == 8:
                            try:
                                reply = json.loads(body)
                            except (ValueError, UnicodeError, RecursionError) as exc:
                                raise ProtocolError("长连接认证响应格式无效。") from exc
                            if (
                                not isinstance(reply, dict)
                                or type(reply.get("code")) is not int
                            ):
                                raise ProtocolError("长连接认证响应缺少状态码。")
                            if reply["code"]:
                                raise SocketRejected(
                                    "长连接临时凭证未通过认证，等待重新获取。"
                                )
                            if not authenticated:
                                authenticated = True
                                last_ack, next_heartbeat = clock(), clock()
                                yield StreamNotice("ready")
                        elif authenticated and operation == 3:
                            if len(body) < 4:
                                raise ProtocolError("长连接心跳响应被截断。")
                            last_ack = clock()
                            yield StreamNotice("heartbeat")
                        elif authenticated and operation == 5 and body:
                            try:
                                command = json.loads(body)
                            except (ValueError, UnicodeError, RecursionError) as exc:
                                raise ProtocolError("长连接弹幕消息格式无效。") from exc
                            name = (
                                command.get("cmd")
                                if isinstance(command, dict)
                                else None
                            )
                            if (
                                isinstance(name, str)
                                and name.startswith("DANMU_MSG")
                                and name.split(":")[0] != "DANMU_MSG"
                            ):
                                raise ProtocolError("当前长连接弹幕格式不受支持。")
                            event = danmaku_event(command)
                            if event:
                                yield StreamNotice("danmaku", event)
        except aiohttp.ClientError:
            raise ProtocolError("长连接网络请求失败或连接已断开。") from None
