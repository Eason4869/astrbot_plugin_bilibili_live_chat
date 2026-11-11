import asyncio
import importlib
import json
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from astrbot_plugin_bilibili_live_chat.bilibili import Credentials
from astrbot_plugin_bilibili_live_chat.conversation import Danmaku, Job


class Config(dict):
    def save_config(self):
        self.saved = dict(self)


class Star:
    def __init__(self, context):
        self.context = context


def identity(*args, **kwargs):
    return lambda func: func


def load_plugin():
    api = ModuleType("astrbot.api")
    api.AstrBotConfig = Config
    event = ModuleType("astrbot.api.event")
    event.AstrMessageEvent = object
    event.filter = SimpleNamespace(command=identity, regex=identity, permission_type=identity, event_message_type=identity,
                                   PermissionType=SimpleNamespace(ADMIN=1),
                                   EventMessageType=SimpleNamespace(PRIVATE_MESSAGE=1))
    star = ModuleType("astrbot.api.star")
    star.Star, star.Context, star.register = Star, object, identity
    components = ModuleType("astrbot.api.message_components")
    components.Image = SimpleNamespace(fromBytes=lambda data: ("image", data))
    components.Plain = lambda text: ("plain", text)
    with patch.dict(sys.modules, {m.__name__: m for m in (ModuleType("astrbot"), api, event, star, components)}):
        return importlib.import_module("astrbot_plugin_bilibili_live_chat.main")


class Event:
    def __init__(self, sender="1", origin="test:FriendMessage:1", admin=True, group=False):
        self.sender, self.unified_msg_origin, self.admin, self.group = sender, origin, admin, group

    def get_sender_id(self):
        return self.sender

    def get_group_id(self):
        return "group" if self.group else ""

    def is_private_chat(self):
        return not self.group

    def is_admin(self):
        return self.admin

    def plain_result(self, text):
        return text

    def chain_result(self, chain):
        return chain


class FakeClient:
    def __init__(self, credentials=None):
        self.credentials = credentials

    async def close(self):
        pass

    async def qr_start(self):
        return "https://passport.bilibili.com/demo", "key"

    async def qr_poll(self, key):
        return "success", Credentials("demo-cookie", "demo-csrf", "42")

    async def check_login(self):
        pass

    async def resolve_room(self, room):
        return 100

    async def history(self, room):
        return []


async def wait_for(check):
    for _ in range(200):
        if check():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition did not become true")


class PluginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = load_plugin()
        self.config = Config(ROOM_ID=12, KEYWORDS=["自定义"], MODEL_PROVIDER_ID="chat-model")
        self.star = None
        self.model_args = None
        self.plugin = self.module.LiveChatPlugin(self, self.config)
        self.addAsyncCleanup(self.plugin.terminate)

    def get_registered_star(self, name):
        return self.star

    def get_provider_by_id(self, provider):
        return object() if provider == "chat-model" else None

    async def llm_generate(self, **kwargs):
        self.model_args = kwargs
        return SimpleNamespace(completion_text='{"reply":"在呢"}')

    async def command(self, event, action):
        return [result async for result in self.plugin.command(event, action)]

    def test_webui_wake_words_are_a_real_list_config(self):
        path = Path(self.module.__file__).with_name("_conf_schema.json")
        schema = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(schema["KEYWORDS"]["type"], "list")
        self.assertEqual(schema["KEYWORDS"]["default"], [])
        self.assertEqual(schema["MODEL_PROVIDER_ID"]["_special"], "select_provider")

    async def test_qr_claim_only_works_in_own_private_admin_session(self):
        owner = Event()
        with patch.object(self.module, "BilibiliClient", FakeClient), \
             patch.object(self.module, "render_qr", return_value=b"test image"):
            self.assertIn(("image", b"test image"), (await self.command(owner, "登录"))[0])
            self.assertIn("同一私聊", (await self.command(Event(origin="another"), "确认"))[0])
            self.assertIn("同一私聊", (await self.command(Event(sender="2"), "确认"))[0])
            self.assertIn("管理员私聊", (await self.command(Event(group=True), "确认"))[0])
            self.assertIn("管理员私聊", (await self.command(Event(admin=False), "确认"))[0])
            self.assertNotIn("SESSDATA", self.config)
            self.assertIn("登录成功", (await self.command(owner, "确认"))[0])
        self.assertEqual(self.config.saved["DEDE_USER_ID"], "42")
        self.assertNotIn("demo-cookie", (await self.command(owner, "状态"))[0])

    async def test_plugin_starts_with_own_credentials_without_bilibot(self):
        self.config.update(SESSDATA="demo-cookie", BILI_JCT="demo-csrf", DEDE_USER_ID="42")
        with patch.object(self.module, "BilibiliClient", FakeClient):
            self.assertIn("已开启", (await self.command(Event(), "开始"))[0])
            await wait_for(lambda: self.plugin.rooms.live("12"))
        live = self.plugin.rooms.live("12")
        self.assertTrue(live.running)
        self.assertEqual(live.room, 100)
        job = Job(Danmaku("1", "7", "观众", "自定义"), "wake", 0, 1, ())
        result = await live.generate(job)
        self.assertEqual(result, '{"reply":"在呢"}')
        self.assertEqual(self.model_args["chat_provider_id"], "chat-model")

    async def test_bilibot_account_is_read_only_and_never_silently_falls_back(self):
        source = Config(SESSDATA="shared-secret", BILI_JCT="csrf", DEDE_USER_ID="42")
        self.star = SimpleNamespace(activated=True, star_cls=SimpleNamespace(config=source))
        self.config["AUTH_SOURCE"] = "bilibot"
        with patch.object(self.module, "BilibiliClient", FakeClient):
            self.assertIn("已开启", (await self.command(Event(), "开始"))[0])
            await wait_for(lambda: self.plugin.rooms.live("12"))
            live = self.plugin.rooms.live("12")
            self.assertEqual(live.client.credentials.uid, "42")
            self.assertNotIn("SESSDATA", self.config)
            source["DEDE_USER_ID"] = "24"
            self.assertFalse(live.valid())
            await self.command(Event(), "停止")
            self.config.update(SESSDATA="local-secret", BILI_JCT="local-csrf", DEDE_USER_ID="11")
            self.star.activated = False
            await self.command(Event(), "开始")
            await wait_for(lambda: self.plugin.rooms.snapshot()[0]["phase"] == "waiting_login")
        self.assertIn("BiliBot", self.plugin.rooms.snapshot()[0]["issues"][0])
        self.assertIsNone(self.plugin.rooms.live("12"))
