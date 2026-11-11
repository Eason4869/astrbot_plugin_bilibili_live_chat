"""Optional, in-process link to an already activated BiliBot installation."""

import asyncio

from .bilibili import Credentials

BILIBOT_NAME = "astrbot_plugin_bilibili_ai_bot"
LINK_TIMEOUT = 3


class BiliBotLink:
    def __init__(self, context):
        self.context = context

    def _active(self):
        star = self.context.get_registered_star(BILIBOT_NAME)
        return star if getattr(star, "activated", False) else None

    def credentials(self):
        star = self._active()
        config = getattr(getattr(star, "star_cls", None), "config", None)
        return Credentials.from_config(config) if callable(getattr(config, "get", None)) else None

    def _memory_api(self):
        module = getattr(self._active(), "module", None)
        getter = getattr(module, "get_bilibili_ai_bot_api", None)
        return getter() if callable(getter) else None

    def memory_available(self):
        try:
            api = self._memory_api()
            return callable(getattr(api, "search", None)) and callable(getattr(api, "record_live_event", None))
        except Exception:  # noqa: BLE001
            return False

    async def recall(self, room, job):
        try:
            api = self._memory_api()
            if api is None:
                return []
            memories = await asyncio.wait_for(
                api.search(
                    job.message.text, user_id=job.message.uid, reader_scope="bili_live",
                    memory_types={"live"}, limit=6,
                ),
                timeout=LINK_TIMEOUT,
            )
            room_prefixes = (f"bilibili-live-chat:{room}:", f"bilibot:{room}:")
            return [
                row["text"][:180] for row in memories
                if isinstance(row, dict)
                and str(row.get("user_id")) == job.message.uid
                and str(row.get("session_id", "")).startswith(room_prefixes)
                and isinstance(row.get("text"), str)
            ][:2]
        except Exception:  # noqa: BLE001
            return []

    async def record(self, room, session_id, job, reply):
        try:
            api = self._memory_api()
            if api is None:
                return
            await asyncio.wait_for(
                api.record_live_event(
                    user_id=job.message.uid,
                    username=job.message.name,
                    event_type="reply",
                    content=f"弹幕：{job.message.text}；回复：{reply}",
                    session_id=session_id,
                    event_id=f"bilibili-live-chat:{room}:{job.message.key}",
                    room_id=str(room),
                ),
                timeout=LINK_TIMEOUT,
            )
        except Exception:  # noqa: BLE001
            # Optional memory failures must never interrupt the receiver or sender.
            return
