"""中文字体取回：校验、下载、SSRF、失败降级。

字体不再随插件分发，测试样本改为现场生成的最小中文字体（``cjk_font``）：它同样
要能过 ``verify`` 的四关（字节数、哈希、可加载、真有汉字），所以拿它当载荷足以
覆盖下载与校验的每条分支。镜像上那份字节与钉死值是否一致，由
``CHECK_FONT_MIRROR=1`` 时运行的 ``test_mirror_serves_the_pinned_bytes`` 盯住。
"""

import asyncio
import hashlib
import os
import urllib.request

import pytest

from cjk_font import write_min_cjk_font
from fake_session import FakeResponse, FakeSession, reading
from font_store import ALLOWED_FONT_SUFFIXES, FONT_BASE_URL, FONT_SOURCES, FontLimits, FontSource, FontStore

_URL = FONT_BASE_URL + "Test_Font.ttf"


@pytest.fixture(scope="module")
def regular_bytes(tmp_path_factory):
    """样本字体字节：只映射「永」一个字，够 verify 用。"""
    path = tmp_path_factory.mktemp("font") / "sample.ttf"
    write_min_cjk_font(str(path))
    return path.read_bytes()


def _public_resolver(host):
    return ["93.184.216.34"]


def _source(data: bytes, filename: str = "Test_Font.ttf") -> FontSource:
    return FontSource(filename, hashlib.sha256(data).hexdigest(), len(data))


def _store(tmp_path, session, sources, resolver=_public_resolver):
    return FontStore(session, str(tmp_path / "fonts"), sources=sources, resolver=resolver)


def _resp(body: bytes = b"", status: int = 200, headers=None):
    return FakeResponse([body], status=status, headers=dict(headers or {}))


def _redirect(location: str):
    return FakeResponse([], status=302, headers={"Location": location})


def _run(coro):
    return asyncio.run(coro)


# ── 校验 ─────────────────────────────────────────────────────────────────

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.mark.skipif(os.environ.get("CHECK_FONT_MIRROR") != "1",
                    reason="需要联网；CHECK_FONT_MIRROR=1 时运行")
def test_mirror_serves_the_pinned_bytes():
    """镜像上那两份字节必须与 ``FONT_SOURCES`` 钉死的值一致。

    字体不再随插件分发，``sha256`` / ``size`` 就是唯一的完整性锚点：它们一旦
    漂移，所有新装用户都取不回字体（只会退到 AstrBot 默认字体）。改字体版本时
    手动跑一次：``CHECK_FONT_MIRROR=1 python -m pytest tests/test_font_store.py -q``。
    """
    for source in FONT_SOURCES:
        request = urllib.request.Request(FONT_BASE_URL + source.filename)
        with urllib.request.urlopen(request, timeout=120) as resp:
            data = resp.read()
        assert len(data) == source.size, source.filename
        assert _sha256(data) == source.sha256, source.filename


def test_verify_accepts_matching_file(tmp_path, regular_bytes):
    source = _source(regular_bytes)
    store = _store(tmp_path, FakeSession(), (source,))
    path = store.path_for(source.filename)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(regular_bytes)

    assert store.verify(path, source) is True
    assert store.available() == {source.filename: path}


def test_verify_rejects_wrong_hash(tmp_path, regular_bytes):
    source = _source(regular_bytes)
    store = _store(tmp_path, FakeSession(), (source,))
    tampered = tmp_path / "tampered.ttf"
    tampered.write_bytes(regular_bytes[:-1] + bytes([regular_bytes[-1] ^ 0xFF]))

    assert store.verify(str(tampered), source) is False


def test_verify_rejects_wrong_size(tmp_path, regular_bytes):
    source = _source(regular_bytes)
    store = _store(tmp_path, FakeSession(), (source,))
    short = tmp_path / "short.ttf"
    short.write_bytes(regular_bytes[: len(regular_bytes) // 2])

    assert store.verify(str(short), source) is False


def test_verify_rejects_non_font_bytes(tmp_path):
    """哈希对得上但根本不是字体：仍然要挡掉，不能让 Pillow 在渲染时才炸。"""
    payload = b"not a font at all" * 64
    source = _source(payload)
    store = _store(tmp_path, FakeSession(), (source,))
    path = store.path_for(source.filename)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(payload)

    assert store.verify(path, source) is False


# ── 下载 ─────────────────────────────────────────────────────────────────

def test_missing_font_is_downloaded_and_reported(tmp_path, regular_bytes):
    source = _source(regular_bytes)
    session = FakeSession([_resp(regular_bytes)])
    store = _store(tmp_path, session, (source,))

    result = _run(store.ensure_all())

    assert result == {source.filename: store.path_for(source.filename)}
    assert os.path.getsize(result[source.filename]) == source.size
    assert reading(session.calls) == [_URL]


def test_present_font_is_not_requested_again(tmp_path, regular_bytes):
    source = _source(regular_bytes)
    store = _store(tmp_path, FakeSession(), (source,))
    path = store.path_for(source.filename)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(regular_bytes)

    session = FakeSession([])
    store = _store(tmp_path, session, (source,))

    assert _run(store.ensure_all()) == {source.filename: path}
    assert session.calls == []


def test_redirect_between_allowed_hosts_is_followed(tmp_path, regular_bytes):
    """Gitee 的 raw 地址会 302 跳到 raw.giteeusercontent.com，这一跳必须放行。"""
    source = _source(regular_bytes)
    target = "https://raw.giteeusercontent.com/IKKI2000/harmonyos-fonts/raw/main/x.ttf"
    session = FakeSession([_redirect(target), _resp(regular_bytes)])
    store = _store(tmp_path, session, (source,))

    assert _run(store.ensure_all()) == {source.filename: store.path_for(source.filename)}
    assert reading(session.calls) == [_URL, target]


def test_redirect_to_foreign_host_is_refused(tmp_path, regular_bytes):
    source = _source(regular_bytes)
    session = FakeSession([_redirect("https://evil.example.com/x.ttf")])
    store = _store(tmp_path, session, (source,))

    assert _run(store.ensure_all()) == {}
    assert not os.path.exists(store.path_for(source.filename))


def test_host_outside_allowlist_is_refused(tmp_path, regular_bytes):
    source = _source(regular_bytes)
    session = FakeSession([_resp(regular_bytes)])
    store = FontStore(session, str(tmp_path / "fonts"), sources=(source,),
                      resolver=_public_resolver,
                      base_url="https://evil.example.com/fonts/")

    assert _run(store.ensure_all()) == {}
    assert session.calls == []              # 地址在发起请求之前就被否掉


def test_declared_oversize_is_refused(tmp_path, regular_bytes):
    source = _source(regular_bytes)
    session = FakeSession([_resp(regular_bytes, headers={"Content-Length": str(99 * 1024 * 1024)})])
    store = _store(tmp_path, session, (source,))

    assert _run(store.ensure_all()) == {}
    assert not os.path.exists(store.path_for(source.filename))


def test_streamed_oversize_is_refused(tmp_path):
    """没有 Content-Length 时按实际读到的字节数兜住上限。"""
    payload = b"x" * (2 * 1024 * 1024)
    source = _source(payload)
    session = FakeSession([_resp(payload)])
    store = FontStore(session, str(tmp_path / "fonts"), sources=(source,),
                      resolver=_public_resolver, limits=FontLimits(max_bytes=1024))

    assert _run(store.ensure_all()) == {}
    assert not os.path.exists(store.path_for(source.filename))


def test_bad_payload_leaves_no_file_behind(tmp_path, regular_bytes):
    """下载回来的哈希对不上：既不能落盘，也不能留下 .part 残渣。"""
    source = _source(regular_bytes)
    session = FakeSession([_resp(b"garbage" * 100)])
    store = _store(tmp_path, session, (source,))

    assert _run(store.ensure_all()) == {}
    assert os.listdir(str(tmp_path / "fonts")) == []


@pytest.mark.parametrize("status", [403, 404, 500])
def test_non_200_is_refused(tmp_path, regular_bytes, status):
    source = _source(regular_bytes)
    store = _store(tmp_path, FakeSession([_resp(regular_bytes, status=status)]), (source,))

    assert _run(store.ensure_all()) == {}


def test_connection_error_is_swallowed(tmp_path, regular_bytes):
    source = _source(regular_bytes)
    store = _store(tmp_path, FakeSession([OSError("connection reset")]), (source,))

    assert _run(store.ensure_all()) == {}


def test_too_many_redirects_is_refused(tmp_path, regular_bytes):
    source = _source(regular_bytes)
    loop = [_redirect(_URL) for _ in range(10)]
    store = _store(tmp_path, FakeSession(loop), (source,))

    assert _run(store.ensure_all()) == {}


def test_allowlist_covers_the_redirect_target():
    """两个后缀缺一不可：少了 .giteeusercontent.com，正常下载会被自己的白名单挡掉。"""
    assert ".gitee.com" in ALLOWED_FONT_SUFFIXES
    assert ".giteeusercontent.com" in ALLOWED_FONT_SUFFIXES


def test_url_never_leaves_the_declared_host(tmp_path, regular_bytes):
    """`path_for` 由固定文件名派生，不接受调用方拼路径。"""
    source = _source(regular_bytes, filename="Test_Font.ttf")
    store = _store(tmp_path, FakeSession(), (source,))

    assert os.path.dirname(store.path_for(source.filename)) == str(tmp_path / "fonts")
