"""自带中文字体补全：校验、下载、SSRF、失败降级。

这些用例直接拿仓库里真实的自带字体当样本——它既是被测对象要保护的资源，也是
最真实的输入：能通过校验说明 `test_bundled_fonts_match_pinned_hashes` 之外的
所有路径都拿的是同一份文件。
"""

import asyncio
import hashlib
import os

import pytest

import font_store
from fake_session import FakeResponse, FakeSession, reading
from font_store import ALLOWED_FONT_SUFFIXES, FONT_BASE_URL, FONT_SOURCES, FontLimits, FontSource, FontStore

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BUNDLED_DIR = os.path.join(_REPO_ROOT, "resources", "fonts")
_URL = FONT_BASE_URL + "Test_Font.ttf"


@pytest.fixture(scope="module")
def regular_bytes():
    path = os.path.join(_BUNDLED_DIR, "HarmonyOS_Sans_SC_Regular.ttf")
    if not os.path.isfile(path):
        pytest.skip("仓库自带字体缺失")
    with open(path, "rb") as handle:
        return handle.read()


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

def test_bundled_fonts_match_pinned_hashes():
    """仓库里的字体必须与 `FONT_SOURCES` 钉死的校验值一致。

    否则「校验」保护的是一份对不上的文件：要么误判自带字体损坏，要么把
    被替换过的文件当成合法的。
    """
    for source in FONT_SOURCES:
        path = os.path.join(_BUNDLED_DIR, source.filename)
        assert os.path.isfile(path), f"自带字体缺失：{source.filename}"
        with open(path, "rb") as handle:
            data = handle.read()
        assert len(data) == source.size, source.filename
        assert hashlib.sha256(data).hexdigest() == source.sha256, source.filename


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
