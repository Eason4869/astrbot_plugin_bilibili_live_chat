"""Supervise one selected receiver; poll while opening/retrying WebSocket."""

import asyncio
import time
from contextlib import suppress

from .bilibili import AuthExpired, BilibiliError, RateLimited
from .live_protocol import ProtocolError


class ReceiverChanged(Exception):
    pass


class LiveReceiver:
    def __init__(
        self,
        client,
        room,
        on_poll,
        on_event,
        valid,
        *,
        mode="poll",
        poll_seconds=5,
        retry_seconds=60,
        clock=None,
    ):
        self.client, self.room = client, room
        self.on_poll, self.on_event, self.valid = on_poll, on_event, valid
        self.mode, self.poll_seconds, self.retry_seconds = (
            mode,
            poll_seconds,
            retry_seconds,
        )
        self.clock = clock or time.monotonic
        self.source = "poll"
        self.phase = "connecting" if mode == "auto" else "polling"
        self.reason = self.poll_error = ""
        self.epoch = self.attempts = self.fallbacks = self.duplicates = 0
        self.baseline = True
        self.retry_at = None
        self.last_heartbeat = None

    def snapshot(self):
        return {
            "configured": self.mode,
            "source": self.source,
            "phase": self.phase,
            "reason": self.reason,
            "attempts": self.attempts,
            "fallbacks": self.fallbacks,
            "duplicates": self.duplicates,
            "retry_in": max(0, int(self.retry_at - self.clock()))
            if self.retry_at is not None
            else 0,
            "heartbeat_age_ms": round(max(0, self.clock() - self.last_heartbeat) * 1000)
            if self.source == "websocket" and self.last_heartbeat is not None
            else None,
        }

    def _current(self):
        if not self.valid():
            raise ReceiverChanged()

    async def run(self):
        tasks = [asyncio.create_task(self._poll(), name="bili-listener.poll")]
        if self.mode == "auto":
            tasks.append(
                asyncio.create_task(self._websocket(), name="bili-listener.websocket")
            )
        try:
            completed, _ = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in completed:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.phase, self.source, self.retry_at = "stopped", "", None

    async def _poll(self):
        while True:
            self._current()
            delay = self.poll_seconds
            if self.source == "poll":
                epoch = self.epoch
                try:
                    rows = await self.client.history(self.room)
                    self._current()
                    if self.source == "poll" and epoch == self.epoch:
                        self.on_poll(rows, self.clock(), force_baseline=self.baseline)
                        self.baseline = False
                        self.poll_error = ""
                except AuthExpired:
                    raise
                except RateLimited:
                    if self.source == "poll" and epoch == self.epoch:
                        self.poll_error = "B 站暂时限制弹幕读取"
                    delay = max(60, delay)
                except BilibiliError as exc:
                    if self.source == "poll" and epoch == self.epoch:
                        self.poll_error = str(exc) or "弹幕读取失败"
                    delay = max(10, delay)
            await asyncio.sleep(delay)

    async def _websocket(self):
        failures = 0
        while True:
            self._current()
            self.attempts += 1
            self.phase = "connecting"
            stream = None
            try:
                stream = self.client.live_events(self.room, attempt=self.attempts - 1)
                async for notice in stream:
                    self._current()
                    if notice.kind == "ready":
                        self.epoch += 1
                        self.source, self.phase = "websocket", "connected"
                        self.reason = self.poll_error = ""
                        self.last_heartbeat, self.retry_at = None, None
                        failures = 0
                    elif notice.kind == "heartbeat" and self.source == "websocket":
                        self.last_heartbeat = self.clock()
                    elif notice.kind == "danmaku" and self.source == "websocket":
                        self.on_event(notice.event, self.clock())
                raise ProtocolError("直播长连接已结束。")
            except (AuthExpired, ReceiverChanged):
                raise
            except RateLimited:
                self.reason = "B 站暂时限制长连接请求，已使用轮询回退。"
            except ProtocolError as exc:
                self.reason = str(exc) + " 当前使用轮询回退。"
            except BilibiliError as exc:
                self.reason = str(exc) + " 当前使用轮询回退。"
            except Exception:  # noqa: BLE001
                self.reason = "长连接暂不可用，当前使用轮询回退。"
            finally:
                if stream is not None:
                    with suppress(Exception):
                        await stream.aclose()
            if self.source == "websocket":
                self.epoch += 1
                self.baseline = True
            self.source, self.phase = "poll", "fallback"
            self.last_heartbeat = None
            self.fallbacks += 1
            failures += 1
            delay = min(300, self.retry_seconds * 2 ** min(failures - 1, 4))
            self.retry_at = self.clock() + delay
            await asyncio.sleep(delay)
