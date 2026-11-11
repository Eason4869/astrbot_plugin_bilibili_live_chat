"""One receiver and one reply worker for an explicitly started room."""

import asyncio
import time
from collections import deque
from uuid import uuid4

from .bilibili import AuthExpired, BilibiliError, RateLimited, SendUnknown
from .conversation import MAX_JOB_AGE, parse_reply


class LiveSession:
    def __init__(
        self, client, room, own_uid, conversation, generate, *,
        poll_seconds=5, model_timeout=20, ignored_users=(), clock=None,
        valid=lambda: True, observer=None,
    ):
        self.client = client
        self.room = room
        self.own_uid = own_uid
        self.conversation = conversation
        self.generate = generate
        self.poll_seconds = poll_seconds
        self.model_timeout = model_timeout
        self.ignored_users = frozenset(ignored_users)
        self.clock = clock or time.monotonic
        self.valid = valid
        self.observer = observer
        self.media = None
        self.id = f"bilibili-live-chat:{room}:{uuid4().hex}"
        self.tasks = ()
        self.running = False
        self.ready = asyncio.Event()
        self.seen = set()
        self.seen_order = deque()
        self.last_snapshot = None
        self.poll_error = ""
        self.send_error = ""
        self.model_error = ""
        self.stop_reason = ""
        self.auth_expired = False
        self.received = 0
        self.replies = 0

    async def start(self):
        if self.tasks:
            raise RuntimeError("直播会话只能启动一次")
        self.running = True
        self.tasks = (
            asyncio.create_task(self._poll(), name="bilibili-live-chat.receiver"),
            asyncio.create_task(self._reply(), name="bilibili-live-chat.sender"),
        )
        if self.media:
            self.media.start()

    async def stop(self):
        await self._halt(self.stop_reason or "已停止")

    async def _halt(self, reason):
        self.running = False
        self.stop_reason = reason
        others = [task for task in self.tasks if task is not asyncio.current_task() and not task.done()]
        for task in others:
            task.cancel()
        try:
            if others:
                await asyncio.gather(*others, return_exceptions=True)
        finally:
            try:
                if self.media:
                    await self.media.stop()
            finally:
                await self.client.close()

    async def _can_run(self):
        if not self.running:
            return False
        if not self.valid():
            await self._halt("房间或登录状态已变更，请重新开始")
            return False
        return True

    def _accept_snapshot(self, rows, now):
        baseline = self.last_snapshot is None or now - self.last_snapshot >= MAX_JOB_AGE
        self.last_snapshot = now
        for row in rows:
            if row.key in self.seen:
                continue
            if len(self.seen_order) == 1000:
                self.seen.remove(self.seen_order.popleft())
            self.seen_order.append(row.key)
            self.seen.add(row.key)
            if baseline or row.uid == self.own_uid or row.uid in self.ignored_users:
                continue
            self.received += 1
            self.conversation.offer(row, now)
            self.ready.set()

    def offer_media(self, event):
        if self.running and self.valid():
            self.conversation.offer(event, self.clock())
            self.ready.set()

    async def _poll(self):
        while await self._can_run():
            delay = self.poll_seconds
            try:
                rows = await self.client.history(self.room)
                if not await self._can_run():
                    return
                self._accept_snapshot(rows, self.clock())
                self.poll_error = ""
            except AuthExpired:
                self.auth_expired = True
                await self._halt("登录已失效，请重新扫码")
                return
            except RateLimited:
                self.poll_error = "B 站暂时限制弹幕读取"
                delay = max(60, delay)
            except BilibiliError as exc:
                self.poll_error = str(exc) or "弹幕读取失败"
                delay = max(10, delay)
            await asyncio.sleep(delay)

    async def _reply(self):
        while await self._can_run():
            self.ready.clear()
            now = self.clock()
            delay = self.conversation.delay(now)
            job = self.conversation.take(now) if delay <= 0 else None
            if job is not None:
                await self._process(job)
            elif delay > 0 and self.conversation.pending:
                try:
                    await asyncio.wait_for(self.ready.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
            else:
                await self.ready.wait()

    async def _process(self, job):
        try:
            raw = await asyncio.wait_for(self.generate(job), timeout=self.model_timeout)
        except asyncio.TimeoutError:
            self.model_error = "模型生成超时"
            return
        except Exception:  # noqa: BLE001
            self.model_error = "模型调用失败"
            return

        self.model_error = ""
        text = parse_reply(raw, job.mode, self.conversation.settings.max_chars)
        if not text or not await self._send_when_ready(job, text):
            return
        self.replies += 1
        self.conversation.sent(job, self.clock(), text)
        if self.observer and job.mode != "echo" and job.message.source == "danmaku":
            await self.observer(job, text)

    async def _send_when_ready(self, job, text):
        pacing = self.conversation.pacing
        while True:
            async with pacing.lock:
                if not await self._can_run():
                    return False
                now = self.clock()
                if not self.conversation.is_current(job, now):
                    return False
                delay = self.conversation.delay(now)
                if not delay:
                    self.conversation.attempted(now)
                    return await self._send(text)
            await asyncio.sleep(min(delay, MAX_JOB_AGE - (now - job.offered_at)))

    async def _send(self, text):
        try:
            await self.client.send(self.room, text)
        except AuthExpired:
            self.auth_expired = True
            await self._halt("登录已失效，请重新扫码")
            return False
        except RateLimited:
            self.send_error = "B 站暂时限制发送"
            self.conversation.pacing.block(self.clock() + 300)
            return False
        except SendUnknown:
            self.send_error = "发送结果未知"
            self.conversation.pacing.block(self.clock() + 60)
            return False
        except BilibiliError as exc:
            self.send_error = str(exc) or "弹幕发送失败"
            self.conversation.pacing.block(self.clock() + 60)
            return False
        self.send_error = ""
        return True
