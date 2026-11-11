"""Optional, in-process link to an already activated BiliBot installation."""

import asyncio
import inspect

from .bilibili import Credentials

BILIBOT_NAME = "astrbot_plugin_bilibili_ai_bot"
LINK_TIMEOUT = 3


class BiliBotLink:
    def __init__(self, context):
        self.context = context

    def _active(self):
        try:
            star = self.context.get_registered_star(BILIBOT_NAME)
            return star if getattr(star, "activated", False) else None
        except Exception:  # noqa: BLE001
            return None

    def credentials(self):
        try:
            star = self._active()
            config = getattr(getattr(star, "star_cls", None), "config", None)
            return Credentials.from_config(config) if callable(getattr(config, "get", None)) else None
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _capability(api, name, args, kwargs):
        method = getattr(api, name, None)
        if not callable(method):
            return {'available': False, 'message': f'未提供 {name} 接口。'}
        try:
            inspect.signature(method).bind(*args, **kwargs)
        except (TypeError, ValueError):
            return {'available': False, 'message': f'{name} 接口参数与当前联动方式不兼容。'}
        return {'available': True, 'message': '接口参数兼容；未调用远端服务。'}

    def status(self):
        star = self._active()
        credentials = self.credentials()
        ready = bool(credentials and credentials.ready)
        try:
            api = self._memory_api()
            read = self._capability(api, 'search', ('',), {
                'user_id': '0', 'reader_scope': 'bili_live', 'memory_types': {'live'}, 'limit': 1,
            })
            write = self._capability(api, 'record_live_event', (), {
                'user_id': '0', 'username': '', 'event_type': 'reply', 'content': '',
                'session_id': '', 'event_id': '', 'room_id': '0',
            })
        except Exception:  # noqa: BLE001
            read = write = {'available': False, 'message': 'BiliBot 记忆接口尚未就绪或加载失败。'}
        return {
            'active': star is not None, 'version': str(getattr(star, 'version', '') or '')[:64],
            'login': {'available': ready, 'message': '已找到可复用的登录配置，实际有效性在连接时检查。' if ready else '未找到完整登录配置。'},
            'memory_read': read, 'memory_write': write,
        }

    def _memory_api(self):
        module = getattr(self._active(), "module", None)
        getter = getattr(module, "get_bilibili_ai_bot_api", None)
        return getter() if callable(getter) else None

    def memory_available(self):
        report = self.status()
        return report['memory_read']['available'] and report['memory_write']['available']

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
