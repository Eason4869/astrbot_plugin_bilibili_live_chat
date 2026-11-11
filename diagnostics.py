"""On-demand health checks executed inside the authenticated AstrBot instance."""

import asyncio
import copy
import re
import time
from base64 import b64encode
from contextlib import suppress
from io import BytesIO
from pathlib import Path
from secrets import randbelow
from urllib.parse import urlsplit, urlunsplit

from .media import MediaError, resolve_ffmpeg, select_stt_provider

ASSETS = Path(__file__).parent / "assets"
CHECK_TIMEOUT = 30
SECRET_FIELD = re.compile(r"key|token|secret|password|authorization|sessdata|bili_jct", re.IGNORECASE)


def _secrets(config):
    if not isinstance(config, dict):
        return
    for key, value in config.items():
        if isinstance(value, dict):
            yield from _secrets(value)
        elif SECRET_FIELD.search(str(key)):
            values = value if isinstance(value, list) else [value]
            yield from (item for item in values if isinstance(item, str) and len(item) >= 4)


def safe_error(error, config, provider=None):
    if isinstance(error, asyncio.TimeoutError):
        return f"检查超过 {CHECK_TIMEOUT} 秒，请检查远端网络或模型响应。"
    message = str(error)
    for secret in (*_secrets(config), *_secrets(getattr(provider, "provider_config", None))):
        message = message.replace(secret, "[已隐藏]")
    message = re.sub(r"(?i)Bearer\s+[^\s,'\"}]+", "Bearer [已隐藏]", message)
    message = re.sub(
        r"(?i)((?:api[_-]?key|token|password|sessdata|bili_jct)\s*['\"]?\s*[:=]\s*['\"]?)[^\s,'\"}]+",
        r"\1[已隐藏]", message,
    )

    def clean_url(match):
        try:
            value = urlsplit(match.group())
            host = value.hostname or ""
            port = f":{value.port}" if value.port else ""
            return urlunsplit((value.scheme, host + port, value.path, "", ""))
        except ValueError:
            return "[地址已隐藏]"

    message = re.sub(r"https?://[^\s'\"<>]+", clean_url, message)
    return f"{type(error).__name__}：{' '.join(message.split())[:500]}"


async def process_output(binary, args):
    process = await asyncio.create_subprocess_exec(
        binary, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        try:
            output, errors = await asyncio.wait_for(process.communicate(), timeout=10)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("FFmpeg 子进程在 10 秒内未返回结果。") from exc
        if process.returncode:
            reason = errors.decode("utf-8", errors="replace").strip()
            raise RuntimeError(reason or f"FFmpeg 退出码 {process.returncode}")
        return output
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.communicate()


async def probe_ffmpeg(configured):
    binary = await asyncio.to_thread(resolve_ffmpeg, configured)
    version = (await process_output(binary, ["-version"])).decode("utf-8", errors="replace").splitlines()
    if not version or not version[0].lower().startswith("ffmpeg version"):
        raise RuntimeError("指定程序没有返回有效的 FFmpeg 版本信息。")
    base = ["-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(ASSETS / "selfcheck.mp4")]
    audio = await process_output(binary, base + ["-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"])
    image = await process_output(binary, base + ["-an", "-frames:v", "1", "-c:v", "mjpeg", "-f", "image2pipe", "pipe:1"])
    if not audio or not image.startswith(b"\xff\xd8"):
        raise RuntimeError("FFmpeg 未能解码测试音轨或输出 JPEG 画面。")
    return f"{version[0]}\n程序路径：{binary}\nH.264/AAC 解码、音轨提取与 JPEG 抽帧通过。"


def test_image():
    from PIL import Image, ImageDraw, ImageFont

    code = f"{randbelow(10000):04d}"
    image = Image.new("RGB", (70, 24), "white")
    ImageDraw.Draw(image).text((8, 5), code, fill="black", font=ImageFont.load_default())
    image = image.resize((560, 192), Image.Resampling.NEAREST)
    with BytesIO() as output:
        image.save(output, "JPEG")
        return code, "base64://" + b64encode(output.getvalue()).decode("ascii")


class EnvironmentCheck:
    def __init__(self, plugin):
        self.plugin = plugin
        self.task = None
        self.config = {}
        self.providers = {}
        self.report = {"running": False, "room": "", "items": []}

    def snapshot(self):
        return copy.deepcopy(self.report)

    def start(self, room):
        if self.task and not self.task.done():
            return self.snapshot()
        if room and room not in self.plugin.rooms.configured():
            raise MediaError("找不到所选直播间，请刷新房间列表。")
        self.config = dict(self.plugin.config)
        if room:
            self.config.update(self.plugin.rooms.effective_config(room))
        settings = self.plugin.rooms.media_settings(room) if room else None
        self.providers = {}
        self.report = {
            "running": True, "room": room,
            "items": [
                {"key": "ffmpeg", "label": "FFmpeg 音视频处理", "status": "pending", "detail": "", "elapsed_ms": 0},
                {"key": "speech", "label": "AstrBot 语音识别", "status": "pending", "detail": "", "elapsed_ms": 0},
                {"key": "vision", "label": "视觉模型", "status": "pending", "detail": "", "elapsed_ms": 0},
            ],
        }
        stt_id = settings.stt_provider if settings else ""
        vision_id = (settings.vision_provider if settings else "") or self.config.get("MODEL_PROVIDER_ID", "")
        self.task = asyncio.create_task(self._run(stt_id, vision_id), name="bilibili-live-chat.selfcheck")
        return self.snapshot()

    async def close(self):
        task = self.task
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _run(self, stt_id, vision_id):
        checks = (
            lambda: probe_ffmpeg(self.config.get("FFMPEG_PATH", "")),
            lambda: self._speech(stt_id),
            lambda: self._vision(vision_id),
        )
        try:
            for item, check in zip(self.report["items"], checks):
                item["status"] = "running"
                started = time.monotonic()
                try:
                    item["detail"] = await asyncio.wait_for(check(), timeout=CHECK_TIMEOUT)
                    item["status"] = "ok"
                except Exception as exc:  # noqa: BLE001
                    item["detail"] = safe_error(exc, self.config, self.providers.get(item["key"]))
                    item["status"] = "error"
                finally:
                    item["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        finally:
            for item in self.report["items"]:
                if item["status"] in ("pending", "running"):
                    item.update(status="cancelled", detail="插件重载或停用，检查已取消。")
            self.report["running"] = False
            self.config = {}
            self.providers = {}

    async def _speech(self, provider_id):
        provider = await select_stt_provider(self.plugin.context, provider_id)
        self.providers["speech"] = provider
        path = ASSETS / "selfcheck.wav"
        if not path.is_file():
            raise MediaError("安装包缺少语音自检样本，请重新安装完整插件包。")
        result = await provider.get_text(str(path))
        if not isinstance(result, str) or not result.strip():
            raise MediaError("语音模型未返回转写文本，请检查模型配置与服务状态。")
        text = " ".join(result.split())[:160]
        return f"模型 {provider_id or 'AstrBot 默认语音识别'}，测试音频转写成功：{text}"

    async def _vision(self, provider_id):
        provider = self.plugin.context.get_provider_by_id(provider_id) if provider_id else None
        if not provider:
            raise MediaError("尚未选择可用的视觉模型或默认对话模型。")
        self.providers["vision"] = provider
        expected, image = test_image()
        response = await self.plugin.context.llm_generate(
            chat_provider_id=provider_id,
            prompt="请读取图片中的四位数字，只返回这四位数字。",
            system_prompt="你正在进行图片输入能力测试，请直接读取测试图片。",
            image_urls=[image],
        )
        answer = response.completion_text
        if not isinstance(answer, str) or expected not in re.findall(r"(?<!\d)\d{4}(?!\d)", answer):
            raise MediaError("模型未正确识别测试图片，请检查所选模型是否支持图片输入。")
        return f"模型 {provider_id} 已成功识别测试图片。"
