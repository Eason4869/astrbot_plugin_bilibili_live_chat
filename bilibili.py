"""Small Bilibili Web transport adapted from BiliBot's login and live flows."""

import hashlib
import json
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

import aiohttp

from .conversation import Danmaku

PASSPORT = "https://passport.bilibili.com/x/passport-login/web/qrcode"
NAV = "https://api.bilibili.com/x/web-interface/nav"
LIVE = "https://api.live.bilibili.com"
LIVE_ORIGIN = "https://live.bilibili.com"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
AUTH_CODES = {-101, -111, 401}
RATE_CODES = {-352, -412, -509, 412, 429}
CDN_SUFFIXES = ("bilivideo.com", "bilivideo.cn", "bilibili.com", "bilicdn1.com")


class BilibiliError(Exception):
    pass


class AuthExpired(BilibiliError):
    pass


class RateLimited(BilibiliError):
    pass


class SendUnknown(BilibiliError):
    pass


def uid_text(value):
    text = str(value or "").strip()
    if not text.isascii() or not text.isdigit() or len(text) > 20:
        return ""
    return text.lstrip("0")


@dataclass(frozen=True)
class Credentials:
    sessdata: str = field(repr=False)
    csrf: str = field(repr=False)
    uid: str

    @property
    def ready(self):
        return bool(self.sessdata and self.csrf and self.uid)

    @classmethod
    def from_config(cls, config):
        return cls(
            str(config.get("SESSDATA") or "").strip(),
            str(config.get("BILI_JCT") or "").strip(),
            uid_text(config.get("DEDE_USER_ID")),
        )

    def cookies(self):
        return {"SESSDATA": self.sessdata, "bili_jct": self.csrf, "DedeUserID": self.uid}


def qr_credentials(url, cookies):
    try:
        target = urlsplit(url)
        host = target.hostname or ""
        if target.scheme != "https" or not (host == "bilibili.com" or host.endswith(".bilibili.com")):
            return None
        params = parse_qs(target.query)
    except (TypeError, ValueError):
        return None
    values = {key: cookies.get(key) or params.get(key, [""])[0]
              for key in ("SESSDATA", "bili_jct", "DedeUserID")}
    credentials = Credentials(values["SESSDATA"], values["bili_jct"], uid_text(values["DedeUserID"]))
    return credentials if credentials.ready else None


def parse_history(rows):
    messages = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        uid = uid_text(row.get("uid"))
        text = " ".join(str(row.get("text") or "").split())[:240]
        if not uid or not text:
            continue
        timeline = str(row.get("timeline") or "")
        name = " ".join(str(row.get("nickname") or row.get("uname") or "观众").split())[:32]
        identity = row.get("id_str") or (row.get("rnd"), timeline, uid, text)
        key = hashlib.sha256(json.dumps(identity, ensure_ascii=True).encode()).hexdigest()
        messages.append((timeline, Danmaku(key, uid, name, text)))
    return [message for _, message in sorted(messages, key=lambda item: item[0])]


def playback_urls(data):
    info = (data.get("playurl_info") or {}).get("playurl") or {}
    candidates = []
    for stream in info.get("stream") or ():
        for fmt in stream.get("format") or ():
            flv = fmt.get("format_name") == "flv"
            for codec in fmt.get("codec") or ():
                base = codec.get("base_url") or ""
                if not isinstance(base, str) or not base.startswith("/") or base.startswith("//"):
                    continue
                for row in codec.get("url_info") or ():
                    host, extra = row.get("host", ""), row.get("extra", "")
                    if not isinstance(host, str) or not isinstance(extra, str):
                        continue
                    url = host.rstrip("/") + base + extra
                    if any(ord(char) <= 32 for char in url) or "\\" in url:
                        continue
                    try:
                        parsed = urlsplit(url)
                        name = parsed.hostname or ""
                        allowed_host = any(name == suffix or name.endswith("." + suffix) for suffix in CDN_SUFFIXES)
                        if (
                            parsed.scheme in ("https", "http") and allowed_host
                            and parsed.username is None and parsed.port in (None, 80, 443)
                        ):
                            candidates.append((not flv, url))
                    except ValueError:
                        continue
    return tuple(dict.fromkeys(url for _, url in sorted(candidates, key=lambda item: item[0])))


class BilibiliClient:
    def __init__(self, credentials: Credentials | None = None):
        self.credentials = credentials
        self.session: aiohttp.ClientSession | None = None

    async def close(self):
        if self.session is not None:
            await self.session.close()

    async def _request(self, method, url, *, authenticated=False, headers=None, **kwargs):
        if self.session is None:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
                cookie_jar=aiohttp.DummyCookieJar(),
                headers={"User-Agent": USER_AGENT},
            )
        try:
            cookies = self.credentials.cookies() if authenticated and self.credentials else None
            async with self.session.request(
                method, url, cookies=cookies, headers=headers, allow_redirects=False, **kwargs
            ) as response:
                if response.status in AUTH_CODES:
                    raise AuthExpired()
                if response.status in RATE_CODES:
                    raise RateLimited()
                if response.status != 200:
                    if method == "POST":
                        raise SendUnknown()
                    raise BilibiliError(f"HTTP {response.status}")
                payload = await response.json(content_type=None)
                if not isinstance(payload, dict):
                    raise TypeError("invalid payload")
                return payload, {name: cookie.value for name, cookie in response.cookies.items()}
        except (aiohttp.ClientError, TimeoutError, TypeError, ValueError):
            if method == "POST":
                raise SendUnknown() from None
            raise BilibiliError("B 站请求失败") from None

    @staticmethod
    def _check_code(payload, *, sending=False):
        code = payload.get("code")
        if type(code) is not int:
            if sending:
                raise SendUnknown()
            raise BilibiliError("B 站返回格式异常")
        if code in AUTH_CODES:
            raise AuthExpired()
        if code in RATE_CODES:
            raise RateLimited()
        if code:
            raise BilibiliError(f"B 站返回错误码 {code}")

    @staticmethod
    def _data(payload):
        data = payload.get("data")
        if not isinstance(data, dict):
            raise BilibiliError("B 站返回格式异常")
        return data

    @staticmethod
    def _room_headers(room):
        return {"Origin": LIVE_ORIGIN, "Referer": f"{LIVE_ORIGIN}/{room}"}

    async def qr_start(self):
        payload, _ = await self._request("GET", f"{PASSPORT}/generate")
        self._check_code(payload)
        data = self._data(payload)
        url, key = data.get("url"), data.get("qrcode_key")
        if not isinstance(url, str) or not url.startswith("https://passport.bilibili.com/") or not isinstance(key, str) or not key:
            raise BilibiliError("二维码响应格式异常")
        return url, key

    async def qr_poll(self, key):
        payload, cookies = await self._request("GET", f"{PASSPORT}/poll", params={"qrcode_key": key})
        self._check_code(payload)
        data = self._data(payload)
        code = data.get("code")
        if code in (86038, 86090, 86101):
            return {86038: "expired", 86090: "scanned", 86101: "waiting"}[code], None
        if code == 0:
            credentials = qr_credentials(data.get("url") or "https://www.bilibili.com/", cookies)
            if credentials:
                return "success", credentials
        raise BilibiliError("无法完成扫码登录")

    async def check_login(self):
        if not self.credentials or not self.credentials.ready:
            raise AuthExpired()
        payload, _ = await self._request("GET", NAV, authenticated=True)
        self._check_code(payload)
        data = self._data(payload)
        if data.get("isLogin") is not True or uid_text(data.get("mid")) != self.credentials.uid:
            raise AuthExpired()

    async def resolve_room(self, room):
        payload, _ = await self._request("GET", f"{LIVE}/room/v1/Room/room_init", params={"id": room})
        self._check_code(payload)
        canonical = uid_text(self._data(payload).get("room_id"))
        if not canonical:
            raise BilibiliError("无法识别直播间")
        return int(canonical)

    async def history(self, room):
        payload, _ = await self._request(
            "GET", f"{LIVE}/xlive/web-room/v1/dM/gethistory",
            authenticated=True,
            headers=self._room_headers(room),
            params={"roomid": room, "room_type": 0},
        )
        self._check_code(payload)
        rows = self._data(payload).get("room")
        if not isinstance(rows, list):
            raise BilibiliError("弹幕快照格式异常")
        return parse_history(rows)

    async def play_urls(self, room):
        payload, _ = await self._request(
            "GET", f"{LIVE}/xlive/web-room/v2/index/getRoomPlayInfo",
            authenticated=True,
            headers=self._room_headers(room),
            params={"room_id": room, "protocol": "0,1", "format": "0,1,2", "codec": "0,1",
                    "qn": 150, "platform": "web", "ptype": 8},
        )
        self._check_code(payload)
        data = self._data(payload)
        return playback_urls(data) if data.get("live_status", 1) == 1 else ()

    async def send(self, room, text):
        if not self.credentials or not self.credentials.ready:
            raise AuthExpired()
        payload, _ = await self._request(
            "POST", f"{LIVE}/msg/send",
            authenticated=True,
            headers=self._room_headers(room),
            data={
                "bubble": 0, "msg": text, "color": 16777215, "mode": 1, "fontsize": 25,
                "rnd": int(time.time()), "roomid": room,
                "csrf": self.credentials.csrf, "csrf_token": self.credentials.csrf,
            },
        )
        self._check_code(payload, sending=True)
