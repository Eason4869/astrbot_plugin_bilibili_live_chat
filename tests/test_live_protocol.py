import json
import unittest
import zlib

import brotli

from astrbot_plugin_bilibili_live_chat.live_protocol import (
    HEADER,
    MAX_DECODED,
    PacketDecoder,
    ProtocolError,
    danmaku_event,
    packet,
    sign_wbi,
    websocket_urls,
)


def command(text="小猫你好", uid=7, stamp=1794369600123, rnd=123):
    return {
        "cmd": "DANMU_MSG",
        "info": [[0, 1, 25, 0, stamp, rnd], text, [uid, "观众"]],
    }


class ProtocolTests(unittest.TestCase):
    def test_split_and_coalesced_packets_are_ordered(self):
        first = packet(8, {"code": 0})
        second = packet(5, command(), version=0)
        decoder = PacketDecoder()
        self.assertEqual(decoder.feed(first[:7]), [])
        result = decoder.feed(first[7:] + second)
        self.assertEqual([op for op, body in result], [8, 5])
        self.assertEqual(json.loads(result[1][1])["info"][1], "小猫你好")
        decoder.finish()

    def test_zlib_and_brotli_nested_packets_are_decoded(self):
        inner = packet(5, command(), version=0) + packet(3, b"\x00\x00\x00\x01")
        for version, encode in [(2, zlib.compress), (3, brotli.compress)]:
            with self.subTest(version=version):
                result = PacketDecoder().feed(packet(5, encode(inner), version=version))
                self.assertEqual([op for op, body in result], [5, 3])

    def test_invalid_lengths_truncation_and_unknown_versions_are_rejected(self):
        for bad in [
            HEADER.pack(0, 16, 1, 5, 1),
            HEADER.pack(32, 8, 1, 5, 1),
            HEADER.pack(2_000_000, 16, 1, 5, 1),
            packet(5, b"{}", version=99),
        ]:
            with self.subTest(bad=bad), self.assertRaises(ProtocolError):
                PacketDecoder().feed(bad)
        decoder = PacketDecoder()
        decoder.feed(packet(5, b"{}")[:-1])
        with self.assertRaises(ProtocolError):
            decoder.finish()

    def test_bombs_depth_and_message_count_have_limits(self):
        for version, encode in [(2, zlib.compress), (3, brotli.compress)]:
            with self.subTest(version=version), self.assertRaises(ProtocolError):
                PacketDecoder().feed(
                    packet(5, encode(b"x" * (MAX_DECODED + 1)), version=version)
                )
        nested = packet(5, b"{}", version=0)
        for _ in range(5):
            nested = packet(5, zlib.compress(nested), version=2)
        with self.assertRaises(ProtocolError):
            PacketDecoder().feed(nested)
        with self.assertRaises(ProtocolError):
            PacketDecoder().feed(packet(5, b"{}", version=0) * 1001)

    def test_unknown_identity_and_other_commands_never_become_user_events(self):
        for data in [
            command(uid=0),
            command(uid=True),
            {"cmd": "SEND_GIFT"},
            {"cmd": "DANMU_MSG", "info": []},
        ]:
            self.assertIsNone(danmaku_event(data))
        event = danmaku_event(command())
        self.assertEqual(
            (event.uid, event.name, event.text, event.channel),
            ("7", "观众", "小猫你好", "websocket"),
        )
        self.assertEqual(event.server_time, 1794369600)
        self.assertEqual(event.key, danmaku_event(command()).key)
        self.assertNotEqual(event.key, danmaku_event(command(rnd=124)).key)

    def test_wss_hosts_and_ports_are_restricted(self):
        data = [
            {"host": "broadcastlv.chat.bilibili.com", "wss_port": 443},
            {"host": "127.0.0.1", "wss_port": 443},
            {"host": "evil.chat.bilibili.com.attacker.org", "wss_port": 443},
            {"host": "broadcastlv.chat.bilibili.com", "wss_port": 80},
            {"host": "user@chat.bilibili.com", "wss_port": 443},
        ]
        self.assertEqual(
            websocket_urls(data), ("wss://broadcastlv.chat.bilibili.com:443/sub",)
        )

    def test_wbi_fixed_inputs_are_deterministic_sorted_and_filtered(self):
        result = sign_wbi(
            {"foo": "a!b(c)*d'e", "id": 100, "type": 0}, "a" * 32, "b" * 32, 1700000000
        )
        self.assertEqual(result["foo"], "abcde")
        self.assertEqual(result["wts"], "1700000000")
        self.assertEqual(len(result["w_rid"]), 32)
        other = sign_wbi(
            {"type": 0, "id": 100, "foo": "abcde"}, "a" * 32, "b" * 32, 1700000000
        )
        self.assertEqual(other, result)
        with self.assertRaises(ProtocolError):
            sign_wbi({"id": 100}, "short", "short", 0)


if __name__ == "__main__":
    unittest.main()
