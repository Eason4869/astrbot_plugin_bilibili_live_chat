"""Room-local live audio, frames, and AstrBot speech recognition."""

import asyncio
import os
import shutil
import sys
import time
import wave
from array import array
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, dataclass, replace
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from imageio_ffmpeg import get_ffmpeg_exe

from .bilibili import LIVE_ORIGIN, USER_AGENT, BilibiliError
from .conversation import RoomEvent

SAMPLE_RATE = 16000
AUDIO_BLOCK_BYTES = SAMPLE_RATE * 2 // 5
MAX_FRAME_BYTES = 2_000_000
MAX_AUDIO_AGE = 20


class MediaError(Exception):
    pass


def ffmpeg_candidates():
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        packages = local / "Microsoft" / "WinGet" / "Packages"
        for pattern in ("Gyan.FFmpeg*/ffmpeg-*/bin/ffmpeg.exe", "BtbN.FFmpeg*/ffmpeg-*/bin/ffmpeg.exe"):
            with suppress(OSError):
                yield from sorted(packages.glob(pattern), reverse=True)
        yield local / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe"
        yield Path.home() / "scoop" / "apps" / "ffmpeg" / "current" / "bin" / "ffmpeg.exe"
        yield Path(os.environ.get("ProgramData") or "C:/ProgramData") / "chocolatey" / "bin" / "ffmpeg.exe"
        yield Path(sys.prefix) / "Library" / "bin" / "ffmpeg.exe"
    else:
        for folder in ("/usr/bin", "/usr/local/bin", "/opt/homebrew/bin", "/opt/local/bin"):
            yield Path(folder) / "ffmpeg"
    executable = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    yield Path(sys.executable).parent / executable
    yield Path(__file__).parent / "bin" / executable


def _executable(path):
    try:
        return str(path.resolve()) if path.is_file() and os.access(path, os.X_OK) else None
    except OSError:
        return None


def resolve_ffmpeg(configured=""):
    value = os.path.expandvars(str(configured or "").strip().strip('"')).strip()
    if value:
        path = Path(value).expanduser()
        if path.is_dir():
            path /= "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
        found = _executable(path)
        if found:
            return found
        raise MediaError("配置的 FFmpeg 路径不存在或不可执行，请填写 AstrBot 所在环境中的程序路径。")
    found = shutil.which("ffmpeg")
    if found:
        return str(Path(found).resolve())
    for path in ffmpeg_candidates():
        found = _executable(path)
        if found:
            return found
    try:
        bundled = get_ffmpeg_exe()
        found = _executable(Path(bundled)) if bundled else None
        if found:
            return found
    except (OSError, RuntimeError):
        pass
    raise MediaError("AstrBot 所在环境未找到可用 FFmpeg，请安装或更新插件依赖，或在高级设置填写程序路径。")


@dataclass(frozen=True)
class MediaSettings:
    audio: bool = False
    video: bool = False
    stt_provider: str = ""
    vision_provider: str = ""
    audio_segment: int = 6
    frame_interval: int = 5
    echo_interval: int | None = None
    echo_probability: float | None = None

    @classmethod
    def parse(cls, data, *, strict=False):
        if not isinstance(data, dict):
            if strict:
                raise MediaError("音视频设置格式无效。")
            return cls()
        defaults = asdict(cls())
        if strict and not data.keys() <= defaults.keys():
            raise MediaError("包含未知的音视频设置。")
        for key, value in data.items():
            if key in defaults:
                if valid_setting(key, value):
                    defaults[key] = value.strip() if isinstance(value, str) else value
                elif strict:
                    raise MediaError("音视频设置超出范围，请检查模型、片段长度或间隔。")
        return cls(**defaults)


def valid_setting(key, value):
    if key in ("audio", "video"):
        return type(value) is bool
    if key in ("stt_provider", "vision_provider"):
        return isinstance(value, str) and len(value) <= 256
    if key == "echo_probability":
        return value is None or (type(value) in (int, float) and 0 <= value <= 1)
    if key == "echo_interval" and value is None:
        return True
    low, high = {"audio_segment": (3, 15), "frame_interval": (2, 60), "echo_interval": (10, 3600)}[key]
    return type(value) is int and low <= value <= high


def ffmpeg_args(url, room, mode, settings):
    base = [
        "-nostdin", "-hide_banner", "-loglevel", "error", "-rw_timeout", "10000000",
        "-user_agent", USER_AGENT, "-referer", f"{LIVE_ORIGIN}/{room}",
        "-protocol_whitelist", "http,https,tcp,tls,crypto", "-i", url,
    ]
    if mode == "audio":
        return base + ["-map", "0:a:0", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "pipe:1"]
    return base + [
        "-map", "0:v:0", "-vf", f"fps=1/{settings.frame_interval},scale='min(640,iw)':-2",
        "-c:v", "mjpeg", "-threads", "1", "-q:v", "6", "-f", "image2pipe", "pipe:1",
    ]


@asynccontextmanager
async def capture(binary, args):
    process = await asyncio.create_subprocess_exec(
        binary, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        yield process.stdout
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        await process.communicate()


def speech_present(data):
    samples = array("h")
    samples.frombytes(data)
    if sys.byteorder != "little":
        samples.byteswap()
    return bool(samples) and sum(abs(sample) for sample in samples) >= 110 * len(samples)


def write_wav(path, pcm):
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(SAMPLE_RATE)
        audio.writeframes(pcm)


async def jpeg_frames(reader):
    buffer = bytearray()
    while chunk := await asyncio.wait_for(reader.read(32768), timeout=90):
        buffer.extend(chunk)
        while True:
            start = buffer.find(b"\xff\xd8")
            if start < 0:
                del buffer[:-1]
                break
            if start:
                del buffer[:start]
            end = buffer.find(b"\xff\xd9", 2)
            if end < 0:
                if len(buffer) > MAX_FRAME_BYTES:
                    raise MediaError("直播画面帧过大。")
                break
            if end + 2 > MAX_FRAME_BYTES:
                raise MediaError("直播画面帧过大。")
            yield bytes(buffer[:end + 2])
            del buffer[:end + 2]


def frame_signature(frame):
    try:
        from PIL import Image
    except ImportError as exc:
        raise MediaError("缺少 Pillow，无法读取直播截图。") from exc
    with Image.open(BytesIO(frame)) as image:
        return image.convert("L").resize((16, 16)).tobytes()


def scene_changed(before, after):
    return before is not None and sum(abs(a - b) for a, b in zip(before, after)) >= len(after) * 24


async def select_stt_provider(context, provider_id=""):
    if provider_id:
        provider = context.get_provider_by_id(provider_id)
    else:
        select = getattr(context, "get_using_stt_provider_async", None)
        if callable(select):
            provider = await select()
        else:
            provider = getattr(context, "get_using_stt_provider", lambda: None)()
    if not callable(getattr(provider, "get_text", None)):
        raise MediaError("未找到可用的 AstrBot 语音识别模型。")
    return provider


class MediaRuntime:
    def __init__(self, settings, client, room, context, emit, clock=None, *, ffmpeg_path="", report=None):
        self.settings = settings
        self.client = client
        self.room = room
        self.context = context
        self.emit = emit
        self.report = report or (lambda event, reason: None)
        self.clock = clock or time.monotonic
        self.ffmpeg_path = ffmpeg_path
        self.tasks = []
        self.draining = None
        self.errors = {"audio": "", "video": "", "vision": ""}
        self.transcripts = 0
        self.frames = 0
        self.frame = None
        self.frame_at = 0
        self.vision_retry_at = 0

    def snapshot(self):
        return {
            "transcripts": self.transcripts, "frames": self.frames,
            "issues": list(dict.fromkeys(filter(None, self.errors.values()))),
        }

    def latest_frame(self):
        return self.frame if self.clock() - self.frame_at <= 2 * self.settings.frame_interval + 5 else None

    def start(self):
        for mode in ("audio", "video"):
            if getattr(self.settings, mode):
                self.tasks.append(asyncio.create_task(self._run(mode), name=f"bili-media:{self.room}:{mode}"))

    async def stop(self):
        tasks, self.tasks = self.tasks, []
        self.frame = None
        for task in tasks:
            task.cancel()
        if tasks:
            self.draining = asyncio.gather(*tasks, return_exceptions=True)
        drained = self.draining
        if drained:
            try:
                await asyncio.shield(drained)
            except asyncio.CancelledError:
                await asyncio.shield(drained)
                raise
            finally:
                if drained.done():
                    self.draining = None

    async def _stt(self):
        return await select_stt_provider(self.context, self.settings.stt_provider)

    async def _run(self, mode):
        delay = 5
        attempt = 0
        label = "语音" if mode == "audio" else "视频"
        while True:
            try:
                binary = resolve_ffmpeg(self.ffmpeg_path)
                provider = await asyncio.wait_for(self._stt(), timeout=10) if mode == "audio" else None
                urls = await self.client.play_urls(self.room)
                if not urls:
                    raise MediaError("暂未获取到可播放的直播流，等待开播或重连。")
                url = urls[attempt % len(urls)]
                async with capture(binary, ffmpeg_args(url, self.room, mode, self.settings)) as reader:
                    if mode == "audio":
                        await self._audio(reader, provider)
                    else:
                        await self._video(reader)
                raise MediaError("直播流中断，正在重连。")
            except MediaError as exc:
                self.errors[mode] = str(exc)
            except BilibiliError:
                self.errors[mode] = f"{label}直播流暂不可用，正在重试。"
            except Exception:  # noqa: BLE001
                self.errors[mode] = f"{label}识别暂不可用，正在重试。"
            finally:
                if mode == "video":
                    self.frame = None
            await asyncio.sleep(delay)
            attempt += 1
            delay = min(60, delay * 2)

    async def _audio(self, reader, provider):
        queue = asyncio.Queue(maxsize=1)
        worker = asyncio.create_task(self._transcribe(queue, provider))
        buffer = bytearray()
        collected_at = None
        silence = 0
        cap = SAMPLE_RATE * 2 * self.settings.audio_segment
        try:
            while True:
                block = await asyncio.wait_for(reader.readexactly(AUDIO_BLOCK_BYTES), timeout=12)
                if worker.done():
                    if worker.cancelled():
                        raise MediaError("AstrBot 转写任务已中断，正在重试。")
                    worker.result()
                    raise MediaError("AstrBot 转写任务已结束，正在重试。")
                voiced = speech_present(block)
                if not buffer and not voiced:
                    continue
                if collected_at is None:
                    collected_at = self.clock()
                buffer.extend(block)
                silence = 0 if voiced else silence + 1
                if len(buffer) >= cap or silence >= 2:
                    if queue.full():
                        queue.get_nowait()
                        self.report(RoomEvent(uuid4().hex, '', '直播语音', '待转写语音片段', 'voice'),
                                    '转写排队中已有更新的语音，跳过旧片段。')
                    queue.put_nowait((self.clock(), bytes(buffer), collected_at))
                    buffer = bytearray(buffer[-2 * AUDIO_BLOCK_BYTES:] if len(buffer) >= cap and not silence else b"")
                    collected_at = self.clock() - len(buffer) / (SAMPLE_RATE * 2) if buffer else None
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def _transcribe(self, queue, provider):
        previous = ("", float("-inf"))
        with TemporaryDirectory(prefix="bili-live-stt-", ignore_cleanup_errors=True) as folder:
            path = Path(folder) / "speech.wav"
            while True:
                segment = await queue.get()
                captured, pcm = segment[:2]
                collected_at = segment[2] if len(segment) > 2 else captured - len(pcm) / (SAMPLE_RATE * 2)
                started = self.clock()
                event = RoomEvent(uuid4().hex, '', '直播语音（转写）', '', 'voice', captured_at=collected_at,
                                  collection_ms=round(max(0, captured - collected_at) * 1000, 1),
                                  transcription_queue_ms=round(max(0, started - captured) * 1000, 1))
                try:
                    write_wav(path, pcm)
                    value = await asyncio.wait_for(provider.get_text(str(path)), timeout=15)
                except Exception:  # noqa: BLE001
                    self.errors["audio"] = "AstrBot 语音转写失败，请检查语音识别模型。"
                    self.report(replace(event, transcription_ms=round(max(0, self.clock() - started) * 1000, 1)),
                                self.errors['audio'])
                    continue
                finally:
                    with suppress(OSError):
                        path.unlink(missing_ok=True)
                self.errors["audio"] = ""
                text = " ".join(value.split())[:240] if isinstance(value, str) else ""
                now = self.clock()
                event = replace(event, text=text, transcription_ms=round(max(0, now - started) * 1000, 1))
                if not text:
                    self.report(event, '语音转写结果为空。')
                    continue
                if now - captured > MAX_AUDIO_AGE:
                    self.report(event, '转写完成时语音已过期。')
                    continue
                if text == previous[0] and now - previous[1] < 5:
                    self.report(event, '短时间内重复的语音转写，已跳过。')
                    continue
                previous = (text, now)
                self.transcripts += 1
                self.emit(event)

    async def _video(self, reader):
        previous = None
        started = self.clock()
        async for frame in jpeg_frames(reader):
            current = frame_signature(frame)
            self.frame, self.frame_at = frame, self.clock()
            self.frames += 1
            self.errors["video"] = ""
            event = RoomEvent(uuid4().hex, '', '直播画面', '画面出现明显变化', 'vision', frame,
                              captured_at=started, collection_ms=round(max(0, self.clock() - started) * 1000, 1))
            if scene_changed(previous, current):
                self.emit(event)
            else:
                self.report(replace(event, text='已读取直播截图'),
                            '首帧仅建立画面比较起点。' if previous is None else '画面没有明显变化。')
            previous = current
            started = self.clock()
