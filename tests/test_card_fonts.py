"""卡片字体的来源与降级：镜像取回优先、失败退 AstrBot 默认字体、开关、不重试、超时不阻塞搜歌。

字体不再随插件分发，所以这里用假的 ``FontStore`` 只盯 main 的编排：哪份用缓存、
哪份去取、取不到时退给谁。
"""

import asyncio
import os

import pytest

import main

_REGULAR = "HarmonyOS_Sans_SC_Regular.ttf"
_BOLD = "HarmonyOS_Sans_SC_Bold.ttf"


def _astrbot_default(bold: bool) -> str:
    """AstrBot 默认字体的绝对路径（与 main 的实现同一约定）。"""
    return os.path.join(os.getcwd(), "data", "font-bold.ttf" if bold else "font.ttf")


@pytest.fixture
def wire(plugin_factory, monkeypatch):
    """把 ``FontStore`` 换成假的；``state`` 用来设定「缓存里有什么 / 取得到什么」。"""
    state = {"available": {}, "ensure": {}, "error": None, "ensure_calls": 0}

    class FakeStore:
        def __init__(self, *args, **kwargs):
            pass

        def available(self):
            return dict(state["available"])

        async def ensure_all(self, sources=None):
            state["ensure_calls"] += 1
            if state["error"] is not None:
                raise state["error"]
            return dict(state["ensure"])

    def build(config=None):
        # conftest 默认关掉字体联网（其它用例都离线），这里要测取回路径，默认打开
        settings = {"font_auto_download": True}
        settings.update(config or {})
        plugin = plugin_factory(settings)
        monkeypatch.setattr(main, "FontStore", FakeStore)

        async def fake_session():
            return object()

        monkeypatch.setattr(plugin, "_get_session", fake_session)
        return plugin

    build.state = state
    return build


# ── 正常路径：从镜像取回 ─────────────────────────────────────────────────

def test_fetched_fonts_are_used(wire):
    plugin = wire()
    wire.state["ensure"] = {_REGULAR: "/data/fonts/regular.ttf", _BOLD: "/data/fonts/bold.ttf"}

    regular, bold = asyncio.run(plugin._preferred_fonts())

    assert regular == ("/data/fonts/regular.ttf",)
    assert bold == ("/data/fonts/bold.ttf",)


def test_only_cached_fonts_are_used_when_download_is_off(wire):
    """关掉联网时只用缓存里那份，不够的部分退 AstrBot 默认字体。"""
    plugin = wire({"font_auto_download": False})
    wire.state["available"] = {_REGULAR: "/data/fonts/regular.ttf"}

    regular, bold = asyncio.run(plugin._preferred_fonts())

    assert regular == ("/data/fonts/regular.ttf",)
    assert bold == (_astrbot_default(True),)
    assert wire.state["ensure_calls"] == 0


# ── 降级：AstrBot 默认字体 ───────────────────────────────────────────────

def test_missing_fonts_fall_back_to_astrbot_default(wire):
    """镜像取不回任何一份时，退回 AstrBot 自己的默认字体，而不是没有字体。"""
    plugin = wire()

    regular, bold = asyncio.run(plugin._preferred_fonts())

    assert regular == (_astrbot_default(False),)
    assert bold == (_astrbot_default(True),)


def test_failed_fetch_falls_back_and_is_not_retried(wire):
    """取回失败不重试（字体 8 MB，每次搜歌都撞一遍网络既慢又没意义）。"""
    plugin = wire()
    wire.state["error"] = OSError("boom")

    first = asyncio.run(plugin._preferred_fonts())
    second = asyncio.run(plugin._preferred_fonts())

    assert first == second == ((_astrbot_default(False),), (_astrbot_default(True),))
    assert wire.state["ensure_calls"] == 1


def test_completion_timeout_does_not_block_or_raise(wire, monkeypatch):
    """取回卡住时，首次搜歌等到上限就往下走，而不是把用户晾在那里。"""

    class HangingStore:
        def __init__(self, *args, **kwargs):
            pass

        async def ensure_all(self, sources=None):
            await asyncio.sleep(30)
            return {}

    plugin = wire()
    monkeypatch.setattr(main, "FontStore", HangingStore)
    monkeypatch.setattr(main, "FONT_WAIT_SECONDS", 0.01)

    regular, bold = asyncio.run(plugin._preferred_fonts())

    assert regular == (_astrbot_default(False),)
    assert bold == (_astrbot_default(True),)


def test_resolution_failure_never_raises(wire, monkeypatch):
    """解析字体整体出错时也只能退化为兜底字体，不能把搜歌带崩。"""
    plugin = wire()

    class ExplodingStore:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr(main, "FontStore", ExplodingStore)

    regular, bold = asyncio.run(plugin._preferred_fonts())

    assert regular == (_astrbot_default(False),)
    assert bold == (_astrbot_default(True),)


# ── 预热任务的生命周期 ───────────────────────────────────────────────────

def test_warm_up_runs_in_the_background(plugin_factory, monkeypatch):
    """预热必须是后台任务：initialize() 不能被一次 8 MB 下载卡住。"""
    async def scenario():
        plugin = plugin_factory({"font_auto_download": True})
        started = asyncio.Event()

        async def hang():
            started.set()
            await asyncio.sleep(30)
            return (), ()

        monkeypatch.setattr(plugin, "_resolve_fonts", hang)
        plugin._warm_fonts()
        task = plugin._font_task
        await asyncio.wait_for(started.wait(), 1)
        await plugin.terminate()
        return task

    task = asyncio.run(scenario())
    assert task.cancelled()


def test_terminate_is_safe_without_a_warm_up(plugin_factory):
    async def scenario():
        plugin = plugin_factory()
        await plugin.terminate()

    asyncio.run(scenario())
