"""卡片字体的来源与降级：自带优先、缺失/损坏补全、开关、失败不重试、超时不阻塞搜歌。"""

import asyncio
import os

import pytest

import main
import search_card
from fake_session import FakeResponse, FakeSession
from font_store import BUNDLED_FONT_DIR

_REGULAR = "HarmonyOS_Sans_SC_Regular.ttf"
_BOLD = "HarmonyOS_Sans_SC_Bold.ttf"


@pytest.fixture(scope="module")
def font_bytes():
    """仓库自带的两份字体：既是被保护资源，也是补全路径最真实的载荷。"""
    data = {}
    for name in (_REGULAR, _BOLD):
        path = os.path.join(BUNDLED_FONT_DIR, name)
        if not os.path.isfile(path):
            pytest.skip("仓库自带字体缺失")
        with open(path, "rb") as handle:
            data[name] = handle.read()
    return data


@pytest.fixture
def wire(plugin_factory, monkeypatch):
    """给插件换上一个假会话；``bundled=`` 才覆写「自带字体有哪些」。"""

    def build(config=None, *, bundled=None, responses=None):
        plugin = plugin_factory(config)
        session = FakeSession(responses or [])
        if bundled is not None:
            monkeypatch.setattr(main, "bundled_fonts", lambda: dict(bundled))

        async def fake_session():
            return session

        monkeypatch.setattr(plugin, "_get_session", fake_session)
        plugin.session = session
        return plugin

    return build


def _bundled(name):
    return {name: os.path.join(BUNDLED_FONT_DIR, name)}


# ── 自带优先 ─────────────────────────────────────────────────────────────

def test_bundled_fonts_are_used_without_any_request(wire):
    """自带字体齐了就绝不该联网——这条守住「默认零请求」。"""
    plugin = wire()

    regular, bold = asyncio.run(plugin._preferred_fonts())

    assert plugin.session.calls == []
    assert [os.path.basename(p) for p in regular] == [_REGULAR]
    assert [os.path.basename(p) for p in bold] == [_BOLD]


def test_titles_resolve_to_the_bundled_bold_face(wire):
    """粗体槽位拿到的是 Bold 那一份，而不是被正文候选顶掉。"""
    plugin = wire()

    regular, bold = asyncio.run(plugin._preferred_fonts())
    fonts = search_card._Fonts(regular, preferred_bold=bold)

    assert os.path.basename(fonts.get(search_card.F_TITLE, bold=True).path) == _BOLD
    assert os.path.basename(fonts.get(search_card.F_TITLE).path) == _REGULAR


# ── 缺失 / 损坏 → 补全 ───────────────────────────────────────────────────

def test_only_the_missing_font_is_fetched(wire, font_bytes):
    plugin = wire(bundled=_bundled(_BOLD),
                  responses=[FakeResponse([font_bytes[_REGULAR]])])

    regular, bold = asyncio.run(plugin._preferred_fonts())

    data_fonts = os.path.join(plugin._data_dir, "fonts")
    assert regular == (os.path.join(data_fonts, _REGULAR),)
    assert bold == (os.path.join(BUNDLED_FONT_DIR, _BOLD),)     # 已有的那份不重复取
    assert os.path.getsize(regular[0]) == len(font_bytes[_REGULAR])
    assert len(plugin.session.calls) == 1                      # 只补缺的那一份


def test_broken_bundled_font_is_replaced_by_the_fetched_one(wire, font_bytes, tmp_path):
    """随插件分发的文件被截断时，补全那份要能顶上去，而不是一直被它挡住。"""
    broken_dir = tmp_path / "broken"
    broken_dir.mkdir()
    for name in (_REGULAR, _BOLD):
        (broken_dir / name).write_bytes(b"truncated")
    broken = {name: str(broken_dir / name) for name in (_REGULAR, _BOLD)}
    plugin = wire(bundled=broken,
                  responses=[FakeResponse([font_bytes[_REGULAR]]),
                             FakeResponse([font_bytes[_BOLD]])])

    regular, bold = asyncio.run(plugin._preferred_fonts())

    data_fonts = os.path.join(plugin._data_dir, "fonts")
    assert regular == (os.path.join(data_fonts, _REGULAR),)
    assert bold == (os.path.join(data_fonts, _BOLD),)
    assert os.path.getsize(bold[0]) == len(font_bytes[_BOLD])
    assert len(plugin.session.calls) == 2


# ── 降级 ─────────────────────────────────────────────────────────────────

def test_auto_download_switch_off_keeps_it_offline(wire):
    plugin = wire({"font_auto_download": False}, bundled={})

    assert asyncio.run(plugin._preferred_fonts()) == ((), ())
    assert plugin.session.calls == []


def test_failed_completion_is_not_retried(wire):
    """字体有 8 MB：失败之后每次搜歌都去撞一遍网络既慢又没意义。"""
    plugin = wire(bundled={}, responses=[FakeResponse([b"nope"]), FakeResponse([b"nope"])])

    first = asyncio.run(plugin._preferred_fonts())
    second = asyncio.run(plugin._preferred_fonts())

    assert first == ((), ()) and second == ((), ())
    assert len(plugin.session.calls) == 2       # 两个文件名各一次，之后再也不发


def test_completion_timeout_does_not_block_or_raise(wire, monkeypatch):
    """补全卡住时，首次搜歌等到上限就往下走，而不是把用户晾在那里。"""

    class HangingStore:
        def __init__(self, *args, **kwargs):
            pass

        async def ensure_all(self, sources):
            await asyncio.sleep(30)
            return {}

    plugin = wire(bundled={})
    monkeypatch.setattr(main, "FontStore", HangingStore)
    monkeypatch.setattr(main, "FONT_WAIT_SECONDS", 0.01)

    assert asyncio.run(plugin._preferred_fonts()) == ((), ())


def test_resolution_failure_never_raises(wire, monkeypatch, caplog):
    """解析字体整体出错时也只能退化为空候选，不能把搜歌带崩。"""
    plugin = wire()
    monkeypatch.setattr(main, "bundled_fonts",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    assert asyncio.run(plugin._preferred_fonts()) == ((), ())


# ── 预热任务的生命周期 ───────────────────────────────────────────────────

def test_warm_up_runs_in_the_background(plugin_factory, monkeypatch):
    """预热必须是后台任务：initialize() 不能被一次 8 MB 下载卡住。"""
    async def scenario():
        plugin = plugin_factory()
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
