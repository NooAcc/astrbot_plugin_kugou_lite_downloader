"""测试引导：注入 astrbot 桩模块、把仓库根加入 sys.path、提供事件与插件夹具。"""

import asyncio
import json
import logging
import os
import sys
import types

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# ── astrbot 桩（本机未安装 astrbot 时用于导入 main.py） ────────────────────


class _FilterStub:
    """记录注册的指令名，其余装饰器均为恒等装饰器。"""

    def __init__(self):
        self.registered: list[str] = []

    def command(self, name):
        def decorator(func):
            self.registered.append(name)
            return func

        return decorator

    def permission_type(self, _value):
        def decorator(func):
            return func

        return decorator


filter_stub = _FilterStub()


def _install_astrbot_stub():
    if "astrbot" in sys.modules:
        return
    try:
        import astrbot  # noqa: F401
        return
    except ImportError:
        pass

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("astrbot")
    astrbot.api = api

    event = types.ModuleType("astrbot.api.event")

    class AstrMessageEvent:      # noqa: D401  （仅作类型占位）
        """桩类型；测试使用 FakeEvent。"""

    event.AstrMessageEvent = AstrMessageEvent
    event.filter = filter_stub

    event_filter = types.ModuleType("astrbot.api.event.filter")

    class PermissionType:
        ADMIN = "admin"
        MEMBER = "member"

    event_filter.PermissionType = PermissionType
    event.filter = filter_stub

    star = types.ModuleType("astrbot.api.star")

    class Context:
        pass

    class Star:
        def __init__(self, context=None):
            self.context = context

    star.Context = Context
    star.Star = Star

    sys.modules.update({
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event,
        "astrbot.api.event.filter": event_filter,
        "astrbot.api.star": star,
    })


_install_astrbot_stub()


# ── 测试夹具 ─────────────────────────────────────────────────────────────


class FakeBotApi:
    """模拟 OneBot 的 `bot.api.call_action`。"""

    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        if self.error is not None:
            raise self.error
        if self.responses:
            return self.responses.pop(0)
        return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}


class FakeBot:
    def __init__(self, responses=None, error=None):
        self.api = FakeBotApi(responses, error)


class FakeEvent:
    """最小 AstrMessageEvent 替身。"""

    def __init__(self, message="", *, group_id=None, sender_id="42",
                 origin="group:1000", bot=None, self_id="100"):
        self.message_str = message
        self._group_id = group_id
        self._sender_id = sender_id
        self.unified_msg_origin = origin
        self.bot = bot
        self._self_id = self_id
        self.results: list[tuple[str, object]] = []
        self.stopped = False

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._sender_id

    def get_self_id(self):
        return self._self_id

    def plain_result(self, text):
        self.results.append(("plain", text))
        return ("plain", text)

    def chain_result(self, chain):
        self.results.append(("chain", chain))
        return ("chain", chain)

    def stop_event(self):
        self.stopped = True

    @property
    def text(self) -> str:
        return "\n".join(str(payload) for _, payload in self.results)


def collect(agen) -> list:
    """驱动 async generator，返回所有产出项。"""

    async def run():
        return [item async for item in agen]

    return asyncio.run(run())


@pytest.fixture
def plugin_factory(tmp_path, monkeypatch):
    """构造插件实例，数据目录指向临时目录。"""
    import main

    def make(config=None):
        data_dir = tmp_path / "data"
        monkeypatch.setattr(main, "plugin_data_dir", lambda: str(data_dir))
        return main.KugouMusic(None, dict(config or {}))

    return make


@pytest.fixture
def shared_dir(tmp_path):
    path = tmp_path / "shared"
    path.mkdir()
    return str(path)


def read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)
