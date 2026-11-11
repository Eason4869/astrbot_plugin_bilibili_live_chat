"""Room-local conversation policy. All time values use a monotonic clock."""

import asyncio
import json
import random
import re
import unicodedata
from collections import deque
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher

MAX_JOB_AGE = 45
QUESTION = re.compile(r"[?？]|为什么|怎么|如何|哪里|哪儿|是不是|有没有|能不能|要不要|该不该|[吗么呢]$")
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff]")
PRIORITY = {"wake": 3, "conversation": 2, "echo": 1}
MEANING_MARKERS = re.compile(
    r"[+\-−]?\d+(?:[.,:/-]\d+)*|[零〇一二三四五六七八九十百千万亿两点]+|正|负|不|没|无|别|勿|未|非|莫|甭|休"
    r"|\b[a-z]+n['’]t\b|\b(?:not|never|no|minus|plus)\b"
)


def bounded_number(config, key, default, low, high):
    try:
        value = type(default)(config.get(key, default))
    except (ValueError, TypeError, OverflowError):
        return default
    return value if low <= value <= high else default


def normalized_keyword(text):
    return unicodedata.normalize("NFKC", text).strip().casefold()


@dataclass(frozen=True)
class ConversationSettings:
    keywords: tuple[str, ...]
    keyword_mode: bool
    window_seconds: int
    window_extension: int
    window_limit: int
    echo_enabled: bool
    echo_interval: int
    echo_probability: float
    cooldown: int
    per_minute: int
    max_chars: int
    voice_aliases: tuple[str, ...] = ()
    dedup_seconds: float = 5.0
    followup_scope: str = "speaker"
    voice_followup: bool = False
    vision_echo: bool = False

    @classmethod
    def from_config(cls, config):
        words = config.get("KEYWORDS", [])
        keywords = tuple(
            dict.fromkeys(normalized_keyword(word) for word in words if isinstance(word, str))
        ) if isinstance(words, list) else ()
        return cls(
            keywords=tuple(word for word in keywords if word),
            keyword_mode=config.get("KEYWORD_MODE", True) is True,
            window_seconds=bounded_number(config, "WINDOW_SECONDS", 30, 0, 3600),
            window_extension=bounded_number(config, "WINDOW_EXTEND_SECONDS", 0, 0, 3600),
            window_limit=bounded_number(config, "WINDOW_MAX_SECONDS", 90, 1, 7200),
            echo_enabled=config.get("ECHO_ENABLED", False) is True,
            echo_interval=bounded_number(config, "ECHO_INTERVAL_SECONDS", 300, 10, 3600),
            echo_probability=bounded_number(config, "ECHO_PROBABILITY", 0.2, 0, 1),
            cooldown=bounded_number(config, "REPLY_COOLDOWN_SECONDS", 20, 2, 120),
            per_minute=bounded_number(config, "MAX_PER_MINUTE", 2, 1, 30),
            max_chars=bounded_number(config, "MAX_REPLY_CHARS", 20, 2, 40),
            voice_aliases=tuple(normalized_keyword(word) for word in config.get('VOICE_KEYWORD_ALIASES', [])
                                if isinstance(word, str) and normalized_keyword(word))
            if isinstance(config.get('VOICE_KEYWORD_ALIASES', []), list) else (),
            dedup_seconds=bounded_number(config, 'CROSS_SOURCE_DEDUP_SECONDS', 5.0, 0, 30),
            followup_scope="room" if config.get("FOLLOWUP_SCOPE") == "room" else "speaker",
            voice_followup=config.get("VOICE_FOLLOWUP_ENABLED", False) is True,
            vision_echo=config.get("VISION_ECHO_ENABLED", False) is True,
        )


@dataclass(frozen=True)
class RoomEvent:
    key: str
    uid: str
    name: str
    text: str
    source: str = "danmaku"
    frame: bytes | None = field(default=None, repr=False)
    captured_at: float | None = None
    collection_ms: float | None = None
    transcription_ms: float | None = None
    transcription_queue_ms: float | None = None
    server_time: int | None = None
    channel: str = ''


Danmaku = RoomEvent


@dataclass(frozen=True)
class Job:
    message: RoomEvent
    mode: str
    offered_at: float
    window: int
    context: tuple[tuple[str, str], ...]


class SendPacer:
    """Send budget shared by all rooms using one Bilibili account."""

    def __init__(self):
        self.attempts: deque[float] = deque()
        self.last_attempt = float("-inf")
        self.blocked_until = float("-inf")
        self.lock = asyncio.Lock()

    def delay(self, settings, now):
        while self.attempts and now - self.attempts[0] >= 60:
            self.attempts.popleft()
        cooldown = self.last_attempt + settings.cooldown - now
        rate_wait = self.attempts[0] + 60 - now if len(self.attempts) >= settings.per_minute else 0
        return max(0, cooldown, rate_wait, self.blocked_until - now)

    def attempted(self, now):
        self.attempts.append(now)
        self.last_attempt = now

    def block(self, until):
        self.blocked_until = max(self.blocked_until, until)


class Conversation:
    def __init__(self, settings: ConversationSettings, roll=random.random, *, pacing=None):
        self.settings = settings
        self.roll = roll
        self.window_until = 0.0
        self.window_started = 0.0
        self.window = 0
        self.pending: dict[str, Job] = {}
        self.history: deque[tuple[str, str]] = deque(maxlen=10)
        self.pacing = pacing if pacing is not None else SendPacer()
        self.last_echo = float("-inf")
        self.observe = lambda *args, **kwargs: None
        self.decision_reason = ""
        self.matched_keyword = ""
        self.recent_inputs = deque(maxlen=50)
        self.participants = set()

    def window_active(self, now):
        return now < self.window_until

    def _keyword(self, message):
        words = self.settings.keywords + (self.settings.voice_aliases if message.source == 'voice' else ())
        text = normalized_keyword(message.text)
        return next((word for word in words if word in text), '')

    def offer(self, message: RoomEvent, now: float):
        self.matched_keyword = ""
        normalized = normalized_keyword(message.text)
        text_key = re.sub(r'[\W_]+', '', normalized)
        # Capture numbers and negation before removing punctuation (1.5 != 15).
        fingerprint = (text_key, tuple(MEANING_MARKERS.findall(normalized)))
        priority = 3 if self._keyword(message) and (self.settings.keyword_mode or message.source == 'voice') else (
            2 if self.window_active(now) or (not self.settings.keyword_mode and message.source == 'danmaku') else 1)
        if (message.source in ('voice', 'danmaku') and len(text_key) >= 2 and self.settings.dedup_seconds
            and any(self._same_input(key, fingerprint) and source != message.source and 0 <= now - at < self.settings.dedup_seconds
                    and previous_priority >= priority for key, source, at, previous_priority in self.recent_inputs)):
            if self.window_active(now) and self._keyword(message) and message.source == "danmaku" and message.uid:
                self.participants.add(message.uid)
            self.observe(message.key, 'skipped', '近期语音和弹幕内容相同，合并处理。')
            return
        mode = self._classify(message, now)
        if mode:
            if message.source in ('voice', 'danmaku') and len(text_key) >= 2:
                self.recent_inputs.append((fingerprint, message.source, now, PRIORITY[mode]))
            if any(PRIORITY[old.mode] > PRIORITY[mode] for old in self.pending.values()):
                self.decision_reason = '已合并到本轮更高优先级回复，不单独接话。'
                # Keep the extra information as context for the winning response.
                for name, old in self.pending.items():
                    self.pending[name] = replace(old, context=(*old.context, (message.name, message.text))[-10:])
                mode = None
            else:
                for previous in self.pending.values():
                    self.observe(previous.message.key, 'replaced', '同轮输入已合并，优先回复唤醒或更新消息。')
                self.pending.clear()
                self.pending[mode] = Job(message, mode, now, self.window, tuple(self.history))
        self.observe(message.key, 'queued' if mode else 'skipped', self.decision_reason,
                     mode=mode or '', keyword=self.matched_keyword)
        if message.source != "vision":
            self.history.append((message.name, message.text))

    @staticmethod
    def _same_input(left, right):
        if left[1] != right[1]:
            return False
        left, right = left[0], right[0]
        return left == right or (
            min(len(left), len(right)) >= 10
            and max(len(left), len(right)) <= 200
            and SequenceMatcher(None, left, right, autojunk=False).ratio() >= 0.9
        )

    def _classify(self, message, now):
        settings = self.settings
        text = message.text
        if message.source == "vision":
            if not settings.vision_echo:
                self.decision_reason = '画面仅辅助回答，不单独触发回复。'
                return None
            self.decision_reason = '接话窗口内的画面仅作为回复参考。'
            return None if self.window_active(now) else self._echo(text, now)
        if message.source == "voice" and not self._keyword(message) and not settings.voice_followup:
            self.decision_reason = '语音仅作上下文，明确喊唤醒词才回复。'
            return None
        if not settings.keyword_mode:
            if message.source == "voice" and self._keyword(message):
                self.matched_keyword = self._keyword(message)
                self.decision_reason = '语音命中唤醒词，等待回复。'
                return "wake"
            self.decision_reason = '关键词模式已关闭，进入对话。'
            return "conversation" if message.source == "danmaku" else self._echo(text, now)
        keywords = settings.keywords + (settings.voice_aliases if message.source == 'voice' else ())
        if not keywords:
            self.decision_reason = '尚未配置唤醒词。'
            return None
        self.matched_keyword = self._keyword(message)
        if self.matched_keyword:
            if not self.window_active(now):
                self.window += 1
                self.window_started = now
                self.window_until = now + min(settings.window_seconds, settings.window_limit)
                self.participants.clear()
            if message.source == "danmaku" and message.uid:
                self.participants.add(message.uid)
            previous = self.pending.pop("echo", None)
            if previous:
                self.observe(previous.message.key, 'replaced', '优先处理唤醒消息，取消待处理附和。')
            self.decision_reason = '命中唤醒词，等待回复。'
            return "wake"
        if self.window_active(now):
            if (message.source == "danmaku" and settings.followup_scope == "speaker"
                    and message.uid not in self.participants):
                self.decision_reason = '当前窗口只接已唤醒观众的话，其他弹幕仅作上下文。'
                return None
            self.decision_reason = '在接话窗口内，等待回复。'
            return "conversation"
        return self._echo(text, now)

    def _echo(self, text, now):
        settings = self.settings
        if not settings.echo_enabled:
            self.decision_reason = '窗口外低频附和未开启。'
        elif now - self.last_echo < settings.echo_interval:
            self.decision_reason = '低频附和仍在间隔内。'
        elif QUESTION.search(text):
            self.decision_reason = '窗口外的明显提问不参与低频附和。'
        elif self.roll() >= settings.echo_probability:
            self.decision_reason = '本次未被低频附和采样选中。'
        else:
            self.decision_reason = '已选中低频附和，等待生成。'
            return 'echo'
        return None

    def is_current(self, job: Job, now: float):
        if now - job.offered_at >= MAX_JOB_AGE:
            return False
        if any(PRIORITY[pending.mode] > PRIORITY[job.mode] for pending in self.pending.values()):
            return False
        if not self.settings.keyword_mode:
            return True
        if job.mode == "wake" and self.settings.window_seconds == 0:
            return True
        if job.window != self.window:
            return False
        return self.window_active(now) == (job.mode != "echo")

    def delay(self, now: float):
        return self.pacing.delay(self.settings, now)

    def take(self, now: float):
        if self.delay(now):
            return None
        for mode in ("wake", "conversation", "echo"):
            job = self.pending.pop(mode, None)
            if job is None:
                continue
            if not self.is_current(job, now):
                self.observe(job.message.key, 'expired', '消息已过期或接话窗口已结束。')
                continue
            if mode == "echo":
                if now - self.last_echo < self.settings.echo_interval:
                    self.observe(job.message.key, 'skipped', '低频附和仍在间隔内。')
                    continue
                self.last_echo = now
            return job
        return None

    def attempted(self, now: float):
        self.pacing.attempted(now)

    def sent(self, job: Job, now: float, text=""):
        if text:
            self.history.append(("Bot", text))
        # Any successful reply counts as participation: never immediately echo after it.
        self.last_echo = now
        for mode, pending in list(self.pending.items()):
            if mode != "wake" and PRIORITY[mode] <= PRIORITY[job.mode] and pending.offered_at <= now:
                self.pending.pop(mode)
                self.observe(pending.message.key, 'replaced', '本轮已经回复，合并生成期间收到的接话或附和。')
        if job.mode != "echo" and self.settings.keyword_mode and self.is_current(job, now) and self.window_active(now):
            self.window_until = min(
                self.window_until + self.settings.window_extension,
                self.window_started + self.settings.window_limit,
            )


def parse_reply(raw: str, mode: str, max_chars: int):
    if not isinstance(raw, str) or len(raw) > 2048:
        return None
    try:
        fields = json.loads(raw, object_pairs_hook=list)
    except (ValueError, RecursionError):
        return None
    if not isinstance(fields, list) or len(fields) != 1 or not isinstance(fields[0], tuple):
        return None
    key, reply = fields[0]
    if key != "reply" or not isinstance(reply, str) or CONTROL.search(reply):
        return None
    reply = reply.strip()
    if mode == "echo":
        if not 2 <= len(reply) <= min(15, max_chars) or QUESTION.search(reply) or re.search(r"[@＠]", reply):
            return None
    elif not 1 <= len(reply) <= max_chars:
        return None
    return reply
