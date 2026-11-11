"""One receiver and one reply worker for an explicitly started room."""

import asyncio
import json
import time
from collections import deque
from uuid import uuid4

from .bilibili import AuthExpired, BilibiliError, RateLimited, SendUnknown
from .conversation import MAX_JOB_AGE, SendPacer, parse_reply
from .debug import RoomDebug


class LiveSession:
    def __init__(
        self, client, room, own_uid, conversation, generate, *,
        poll_seconds=5, model_timeout=20, ignored_users=(), clock=None,
        valid=lambda: True, observer=None, preview=False, echo_gate=None,
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
        self.echo_gate = echo_gate
        self.preview = preview
        if preview:
            self.conversation.pacing = SendPacer()
        self.previews = 0
        self.debug = RoomDebug(self.clock, preview)
        self.conversation.observe = self.debug.update
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
        self.debug.cancel_pending(reason)
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
            self.debug.add(row)
            if baseline or row.uid == self.own_uid or row.uid in self.ignored_users:
                reason = '首次快照或长时间断线，仅建立监听起点。' if baseline else '已忽略自己的账号或忽略名单中的消息。'
                self.debug.update(row.key, 'skipped', reason)
                continue
            self.received += 1
            self.conversation.offer(row, now)
            self.ready.set()

    def offer_media(self, event):
        if self.running and self.valid():
            self.debug.add(event)
            self.conversation.offer(event, self.clock())
            self.ready.set()

    def skip_media(self, event, reason):
        if self.running and self.valid():
            self.debug.add(event)
            self.debug.update(event.key, 'skipped', reason)

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
                for pending in self.conversation.pending.values():
                    self.debug.update(pending.message.key, 'queued', f'等待生成前冷却，约 {delay:.1f} 秒。')
                try:
                    await asyncio.wait_for(self.ready.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
            else:
                await self.ready.wait()

    async def _process(self, job):
        key = job.message.key
        self.debug.elapsed(key, 'queue', job.offered_at)
        if job.mode == 'echo' and self.echo_gate:
            self.debug.update(key, 'judging', '正在判断是否适合附和。')
            started = self.clock()
            try:
                allowed, reason = await asyncio.wait_for(self.echo_gate(job), timeout=3)
            except Exception:  # noqa: BLE001
                allowed, reason = None, '判断超时或调用失败，沿用原规则。'
            finally:
                self.debug.elapsed(key, 'decision', started)
            self.debug.update(key, decision=reason)
            if allowed is False:
                self.debug.update(key, 'skipped', reason)
                return
            if not await self._can_run() or not self.conversation.is_current(job, self.clock()):
                self.debug.update(key, 'expired', '判断完成时消息已过期或房间已停止。')
                return
        self.debug.update(key, 'generating', '正在调用模型生成回复。')
        started = self.clock()
        try:
            raw = await asyncio.wait_for(self.generate(job), timeout=self.model_timeout)
        except asyncio.TimeoutError:
            self.model_error = "模型生成超时"
            self.debug.update(key, 'error', self.model_error)
            return
        except Exception:  # noqa: BLE001
            self.model_error = "模型调用失败"
            self.debug.update(key, 'error', self.model_error)
            return
        finally:
            self.debug.elapsed(key, 'model', started)

        self.model_error = ""
        text = parse_reply(raw, job.mode, self.conversation.settings.max_chars)
        if not text:
            try:
                silent = json.loads(raw) == {'reply': ''}
            except (ValueError, TypeError, RecursionError):
                silent = False
            self.debug.update(key, 'silent' if silent else 'invalid',
                              '模型选择保持安静。' if silent else '模型回复格式或长度不符合发送要求。')
            return
        self.debug.update(key, reply=text)
        if not await self._send_when_ready(job, text):
            return
        if self.preview:
            self.previews += 1
            self.debug.update(key, 'preview', '仅预览：未发送弹幕，未写入长期记忆。')
            self.conversation.sent(job, self.clock(), text)
            return
        self.replies += 1
        self.debug.update(key, 'sent', 'B 站发送接口已确认成功。')
        self.conversation.sent(job, self.clock(), text)
        if self.observer and job.mode != "echo" and job.message.source == "danmaku":
            await self.observer(job, text)

    async def _send_when_ready(self, job, text):
        pacing = self.conversation.pacing
        key = job.message.key
        self.debug.update(key, 'waiting', '正在检查发送限流与消息时效。')
        while True:
            async with pacing.lock:
                if not await self._can_run():
                    self.debug.update(key, 'expired', '房间已停止或配置已变更。')
                    return False
                now = self.clock()
                if not self.conversation.is_current(job, now):
                    self.debug.update(key, 'expired', '消息已过期或接话窗口已结束。')
                    return False
                delay = self.conversation.delay(now)
                if not delay:
                    self.conversation.attempted(now)
                    if self.preview:
                        return True
                    self.debug.update(key, 'sending', '正在向 B 站发送接口提交弹幕。')
                    send_started = self.clock()
                    result = await self._send(text)
                    self.debug.elapsed(key, 'send', send_started)
                    if not result:
                        self.debug.update(key, 'error', self.send_error or self.stop_reason or '弹幕发送未成功。')
                    return result
            self.debug.update(key, 'waiting', '正在等待账号发送冷却或频率限制。')
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
