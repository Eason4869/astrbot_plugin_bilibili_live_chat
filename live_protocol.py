"""Bounded Bilibili wire parsing. Protocol/WBI fields reference blivedm (MIT)."""

import hashlib
import json
import re
import struct
import zlib
from urllib.parse import urlencode

from .bilibili import uid_text
from .conversation import RoomEvent

HEADER = struct.Struct(">IHHII")
MAX_WIRE = 1024 * 1024
MAX_DECODED = 4 * 1024 * 1024
WBI_ORDER = (
    46,
    47,
    18,
    2,
    53,
    8,
    23,
    32,
    15,
    50,
    10,
    31,
    58,
    3,
    45,
    35,
    27,
    43,
    5,
    49,
    33,
    9,
    42,
    19,
    29,
    28,
    14,
    39,
    12,
    38,
    41,
    13,
)


class ProtocolError(Exception):
    pass


def packet(operation, body, version=1):
    if isinstance(body, dict):
        body = json.dumps(body, separators=(",", ":")).encode("utf-8")
    return (
        HEADER.pack(HEADER.size + len(body), HEADER.size, version, operation, 1) + body
    )


def _inflate(body, version, limit):
    try:
        if version == 2:
            decoder = zlib.decompressobj()
            result = decoder.decompress(body, limit + 1)
            finished = (
                decoder.eof and not decoder.unused_data and not decoder.unconsumed_tail
            )
        else:
            try:
                import brotli
            except ImportError as exc:
                raise ProtocolError("缺少 Brotli，请更新插件依赖。") from exc
            decoder = brotli.Decompressor()
            result = decoder.process(body, output_buffer_limit=limit + 1)
            finished = decoder.is_finished()
        if len(result) > limit or not finished:
            raise ProtocolError("压缩消息不完整或超过解压上限。")
        return result
    except ProtocolError:
        raise
    except Exception as exc:
        raise ProtocolError("压缩消息解码失败，请检查 Brotli 依赖版本。") from exc


class PacketDecoder:
    def __init__(self):
        self.buffer = bytearray()

    def _parse(self, data, budget, depth=0):
        offset = 0
        result = []
        while len(data) - offset >= HEADER.size:
            length, header_length, version, operation, _ = HEADER.unpack_from(
                data, offset
            )
            if not HEADER.size <= header_length <= length <= MAX_WIRE:
                raise ProtocolError("长连接消息长度无效或超过上限。")
            if version not in (0, 1, 2, 3):
                raise ProtocolError("长连接消息协议版本不受支持。")
            if len(data) - offset < length:
                break
            body = bytes(data[offset + header_length : offset + length])
            offset += length
            budget[1] -= 1
            if budget[1] < 0:
                raise ProtocolError("单次消息包数量超过上限。")
            if version in (2, 3):
                if operation != 5 or depth >= 3:
                    raise ProtocolError("压缩消息嵌套层数或类型无效。")
                expanded = _inflate(body, version, budget[0])
                budget[0] -= len(expanded)
                nested, used = self._parse(expanded, budget, depth + 1)
                if used != len(expanded):
                    raise ProtocolError("解压后的消息被截断。")
                result.extend(nested)
            else:
                result.append((operation, body))
        return result, offset

    def feed(self, data):
        if len(self.buffer) + len(data) > MAX_WIRE:
            raise ProtocolError("长连接接收缓存超过上限。")
        self.buffer.extend(data)
        result, used = self._parse(self.buffer, [MAX_DECODED, 1000])
        del self.buffer[:used]
        return result

    def finish(self):
        if self.buffer:
            raise ProtocolError("长连接消息被截断。")


def websocket_urls(hosts):
    result = []
    if not isinstance(hosts, list):
        return ()
    for row in hosts[:16]:
        if not isinstance(row, dict):
            continue
        host = row.get("host")
        if (
            isinstance(host, str)
            and len(host) <= 253
            and host.isascii()
            and (host == "chat.bilibili.com" or host.endswith(".chat.bilibili.com"))
            and all(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in host.split(".")
            )
            and str(row.get("wss_port")) == "443"
        ):
            result.append(f"wss://{host}:443/sub")
    return tuple(dict.fromkeys(result))


def sign_wbi(params, img_key, sub_key, timestamp):
    if not all(
        isinstance(key, str) and re.fullmatch(r"[a-zA-Z0-9]{32}", key)
        for key in (img_key, sub_key)
    ):
        raise ProtocolError("无法获取有效的 WBI 登录参数。")
    combined = img_key + sub_key
    mixin = "".join(combined[index] for index in WBI_ORDER)
    values = {key: re.sub(r"[!'()*]", "", str(value)) for key, value in params.items()}
    values["wts"] = str(int(timestamp))
    encoded = urlencode(sorted(values.items()))
    values["w_rid"] = hashlib.md5(
        (encoded + mixin).encode(), usedforsecurity=False
    ).hexdigest()
    return values


def server_seconds(value):
    if type(value) not in (int, float) or not 946684800 <= value <= 7258118400000:
        return None
    seconds = int(value / 1000) if value >= 100000000000 else int(value)
    return seconds if 946684800 <= seconds <= 7258118400 else None


def danmaku_event(command):
    if (
        not isinstance(command, dict)
        or not isinstance(command.get("cmd"), str)
        or command["cmd"].split(":")[0] != "DANMU_MSG"
    ):
        return None
    try:
        info = command["info"]
        meta, text, user = info[:3]
        if (
            not isinstance(meta, list)
            or len(meta) < 6
            or not isinstance(user, list)
            or len(user) < 2
        ):
            return None
        uid = uid_text(user[0]) if type(user[0]) in (str, int) else ""
        if not uid or not isinstance(text, str) or not text.strip():
            return None
        text = " ".join(text.split())[:240]
        name = " ".join(str(user[1] or "观众").split())[:32]
        identity = [meta[4], meta[5], uid, text]
        if len(meta) > 15 and isinstance(meta[15], dict):
            extra = meta[15].get("extra")
            if isinstance(extra, str) and len(extra) <= 16384:
                try:
                    extra = json.loads(extra)
                except (ValueError, RecursionError):
                    extra = None
            event_id = extra.get("id_str") if isinstance(extra, dict) else None
            if isinstance(event_id, str) and event_id and len(event_id) <= 256:
                identity = event_id
        key = hashlib.sha256(
            json.dumps(identity, ensure_ascii=True).encode()
        ).hexdigest()
        return RoomEvent(
            key,
            uid,
            name,
            text,
            server_time=server_seconds(meta[4]),
            channel="websocket",
        )
    except (KeyError, TypeError, ValueError, IndexError, RecursionError):
        return None
