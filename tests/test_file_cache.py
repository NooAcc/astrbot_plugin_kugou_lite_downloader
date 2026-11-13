import asyncio
import os
import time

import pytest
from audio_builders import flac_head, html_page
from fake_session import FakeResponse, FakeSession

from file_cache import (
    AudioFetcher,
    CacheManager,
    DownloadError,
    Downloader,
    Limits,
    SharedDirError,
    UnsafeUrlError,
    build_filename,
    is_safe_url,
    sanitize_filename,
)


# ── 文件名安全 ───────────────────────────────────────────────────────────


def test_sanitize_removes_path_separators_and_control_chars():
    assert sanitize_filename("../../etc/passwd") == ".._.._etc_passwd"
    assert sanitize_filename('a/b\\c:d*e?f"g<h>i|j.mp3').endswith(".mp3")
    name = sanitize_filename("a/b\\c")
    assert "/" not in name and "\\" not in name
    assert sanitize_filename("bad\x00name\x1f") == "badname"


def test_sanitize_windows_reserved_and_edges():
    assert sanitize_filename("CON.mp3").upper() != "CON.MP3"
    assert sanitize_filename("nul") != "nul"
    assert sanitize_filename("name.") == "name"
    assert sanitize_filename("name ") == "name"
    assert sanitize_filename("   ") == "song"
    assert sanitize_filename("") == "song"
    assert sanitize_filename("...") == "song"
    assert len(sanitize_filename("歌" * 500)) <= 120
    assert sanitize_filename("歌" * 500).startswith("歌")


def test_sanitize_keeps_extension_when_truncating():
    long_name = "a" * 500 + ".flac"
    assert sanitize_filename(long_name).endswith(".flac")
    assert len(sanitize_filename(long_name)) <= 120


def test_build_filename_shape():
    assert build_filename("周杰伦", "晴天", "flac", "flac") == "周杰伦 - 晴天 [FLAC].flac"
    assert build_filename("a/b", "c:d", "hires", "flac").startswith("a_b - c_d [Hi-Res]")
    assert build_filename("", "", "320", "mp3") == "song - song [320K].mp3"


# ── URL 安全 ─────────────────────────────────────────────────────────────


def _public(host):
    return ["93.184.216.34"]


def _private(host):
    return ["10.0.0.7"]


def test_url_accepts_only_kugou_cdn_over_http():
    assert is_safe_url("http://fs.kugou.com/a/b.mp3", resolver=_public)[0] is True
    assert is_safe_url("https://trackercdn.kugou.com/v5/x", resolver=_public)[0] is True


def test_url_rejects_scheme_and_credentials():
    assert is_safe_url("ftp://fs.kugou.com/a.mp3", resolver=_public)[0] is False
    assert is_safe_url("file:///etc/passwd", resolver=_public)[0] is False
    assert is_safe_url("http://u:p@fs.kugou.com/a.mp3", resolver=_public)[0] is False


def test_url_rejects_unknown_origin_and_names_the_host():
    ok, why = is_safe_url("http://evil.example.com/a.mp3", resolver=_public)
    assert ok is False
    assert "evil.example.com" in why


def test_url_suffix_must_be_on_a_dot_boundary():
    assert is_safe_url("http://kugou.com.evil.com/a.mp3", resolver=_public)[0] is False
    assert is_safe_url("http://notkugou.com/a.mp3", resolver=_public)[0] is False


def test_url_rejects_private_loopback_and_reserved_targets():
    assert is_safe_url("http://fs.kugou.com/a.mp3", resolver=_private)[0] is False
    assert is_safe_url("http://127.0.0.1/a.mp3", resolver=_public)[0] is False
    assert is_safe_url("http://[::1]/a.mp3", resolver=_public)[0] is False
    assert is_safe_url("http://169.254.1.1/a.mp3", resolver=_public)[0] is False
    assert is_safe_url("http://[::ffff:127.0.0.1]/a.mp3", resolver=_public)[0] is False


def test_url_resolver_failure_is_rejected():
    def boom(host):
        raise OSError("no dns")

    ok, why = is_safe_url("http://fs.kugou.com/a.mp3", resolver=boom)
    assert ok is False and "fs.kugou.com" in why


def test_url_rejects_ip_literal_audio_source():
    # 资源地址应当来自酷狗域名解析；IP 直连地址属于「未知来源」
    ok, why = is_safe_url("http://93.184.216.34/a.mp3", resolver=_public)
    assert ok is False
    assert "IP" in why


def test_local_write_failure_is_reported_as_dir_error(tmp_path):
    """本地写入失败（磁盘满/目录不可写）不得伪装成网络失败。"""
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"x")                       # 是个文件，不能作为目录使用
    part = str(blocker / "sub" / "a.part")
    session = FakeSession([FakeResponse([b"x" * 10])])
    downloader = Downloader(session, Limits(max_bytes=1000), resolver=_public, user_agent="ua")

    with pytest.raises(SharedDirError):
        asyncio.run(downloader.download_part([_AUDIO_URL], part))

    assert len(session.calls) == 1                  # 不因本地错误重试备用地址


def test_error_types_are_distinct():
    assert issubclass(UnsafeUrlError, Exception)
    err = UnsafeUrlError("不安全", url="http://x/")
    assert err.reason == "不安全" and err.url == "http://x/"
    assert isinstance(SharedDirError("x"), Exception)


# ── 受限流式下载 ─────────────────────────────────────────────────────────

_AUDIO_URL = "http://fs.kugou.com/a/b.mp3"


def _downloader(session, limits=None, clock=None):
    return Downloader(
        session,
        limits or Limits(max_bytes=1_000_000),
        resolver=_public,
        user_agent="test-ua",
        clock=clock,
    )


def test_download_writes_part_and_returns_head(tmp_path):
    part = tmp_path / "task" / "a.part"
    session = FakeSession(
        [FakeResponse([b"A" * 1000, b"B" * 500], headers={"Content-Length": "1500"})]
    )
    outcome = asyncio.run(_downloader(session).download_part([_AUDIO_URL], str(part)))

    assert outcome.size == 1500
    assert outcome.url == _AUDIO_URL
    assert outcome.head[:1000] == b"A" * 1000
    assert part.read_bytes() == b"A" * 1000 + b"B" * 500
    assert session.calls[0]["kw"]["allow_redirects"] is False
    assert session.calls[0]["kw"]["headers"]["User-Agent"] == "test-ua"
    assert "Cookie" not in session.calls[0]["kw"]["headers"]


def test_oversized_content_length_rejected_before_body(tmp_path):
    part = tmp_path / "a.part"
    session = FakeSession([FakeResponse([b"x"], headers={"Content-Length": "1000000000"})])
    with pytest.raises(DownloadError) as err:
        asyncio.run(
            _downloader(session, Limits(max_bytes=1000)).download_part([_AUDIO_URL], str(part))
        )
    assert "上限" in err.value.reason
    assert not part.exists()


def test_stream_over_limit_aborts_and_removes_part(tmp_path):
    part = tmp_path / "a.part"
    session = FakeSession([FakeResponse([b"x" * 800] * 4)])
    with pytest.raises(DownloadError) as err:
        asyncio.run(
            _downloader(session, Limits(max_bytes=1000)).download_part([_AUDIO_URL], str(part))
        )
    assert "上限" in err.value.reason
    assert not part.exists()


def test_short_body_vs_content_length_rejected(tmp_path):
    part = tmp_path / "a.part"
    session = FakeSession([FakeResponse([b"x" * 1000], headers={"Content-Length": "1500"})])
    with pytest.raises(DownloadError) as err:
        asyncio.run(_downloader(session).download_part([_AUDIO_URL], str(part)))
    assert "不完整" in err.value.reason
    assert not part.exists()


def test_http_error_status_rejected(tmp_path):
    part = tmp_path / "a.part"
    session = FakeSession([FakeResponse([b"nope"], status=404)])
    with pytest.raises(DownloadError) as err:
        asyncio.run(_downloader(session).download_part([_AUDIO_URL], str(part)))
    assert "404" in err.value.reason
    assert not part.exists()


def test_empty_body_rejected(tmp_path):
    part = tmp_path / "a.part"
    session = FakeSession([FakeResponse([])])
    with pytest.raises(DownloadError) as err:
        asyncio.run(_downloader(session).download_part([_AUDIO_URL], str(part)))
    assert "空" in err.value.reason
    assert not part.exists()


def test_timeout_removes_part(tmp_path):
    part = tmp_path / "a.part"
    calls = {"n": 0}

    def clock():
        calls["n"] += 1
        return 0.0 if calls["n"] == 1 else 100.0

    session = FakeSession([FakeResponse([b"x" * 10])])
    with pytest.raises(DownloadError) as err:
        asyncio.run(
            _downloader(session, Limits(total_seconds=1.0), clock=clock).download_part(
                [_AUDIO_URL], str(part)
            )
        )
    assert "超时" in err.value.reason
    assert not part.exists()


def test_cancel_mid_stream_removes_part(tmp_path):
    part = tmp_path / "a.part"
    session = FakeSession([FakeResponse([b"x" * 10], exc=asyncio.CancelledError())])
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_downloader(session).download_part([_AUDIO_URL], str(part)))
    assert not part.exists()


def test_redirect_to_unsafe_target_is_rejected(tmp_path):
    part = tmp_path / "a.part"
    session = FakeSession(
        [FakeResponse([], status=302, headers={"Location": "http://127.0.0.1/x.mp3"})]
    )
    with pytest.raises(UnsafeUrlError):
        asyncio.run(_downloader(session).download_part([_AUDIO_URL], str(part)))
    assert not part.exists()


def test_redirect_to_allowed_host_is_followed(tmp_path):
    part = tmp_path / "a.part"
    session = FakeSession(
        [
            FakeResponse([], status=302, headers={"Location": "http://b.kugou.com/ok.mp3"}),
            FakeResponse([b"z" * 20]),
        ]
    )
    outcome = asyncio.run(_downloader(session).download_part([_AUDIO_URL], str(part)))
    assert outcome.size == 20 and outcome.url == "http://b.kugou.com/ok.mp3"


def test_too_many_redirects_rejected(tmp_path):
    part = tmp_path / "a.part"
    hop = "/%d.mp3"
    session = FakeSession(
        [
            FakeResponse([], status=302, headers={"Location": "http://a.kugou.com" + hop % 1}),
            FakeResponse([], status=302, headers={"Location": "http://a.kugou.com" + hop % 2}),
            FakeResponse([], status=302, headers={"Location": "http://a.kugou.com" + hop % 3}),
        ]
    )
    with pytest.raises(DownloadError) as err:
        asyncio.run(
            _downloader(session, Limits(max_redirects=2)).download_part([_AUDIO_URL], str(part))
        )
    assert "重定向" in err.value.reason
    assert not part.exists()


def test_alternate_url_used_when_first_fails(tmp_path):
    part = tmp_path / "a.part"
    session = FakeSession([OSError("conn reset"), FakeResponse([b"z" * 10])])
    outcome = asyncio.run(
        _downloader(session).download_part(
            ["http://a.kugou.com/1.mp3", "http://b.kugou.com/2.mp3"], str(part)
        )
    )
    assert outcome.size == 10
    assert outcome.url.endswith("/2.mp3")
    assert part.exists()


def test_attempts_are_capped_by_max_url_attempts(tmp_path):
    part = tmp_path / "a.part"
    session = FakeSession([OSError("x"), OSError("y"), OSError("z")])
    with pytest.raises(DownloadError):
        asyncio.run(
            _downloader(session, Limits(max_url_attempts=2)).download_part(
                ["http://a.kugou.com/1.mp3", "http://b.kugou.com/2.mp3", "http://c.kugou.com/3.mp3"],
                str(part),
            )
        )
    assert len(session.calls) == 2


def test_no_urls_reports_error(tmp_path):
    part = tmp_path / "a.part"
    with pytest.raises(DownloadError):
        asyncio.run(_downloader(FakeSession([])).download_part([], str(part)))


def test_unsafe_initial_url_aborts_without_trying_alternates(tmp_path):
    part = tmp_path / "a.part"
    session = FakeSession([FakeResponse([b"x" * 10])])
    with pytest.raises(UnsafeUrlError):
        asyncio.run(
            _downloader(session).download_part(
                ["http://evil.example.com/a.mp3", "http://a.kugou.com/b.mp3"], str(part)
            )
        )
    assert len(session.calls) == 0


# ── 缓存归属、存储与清理 ─────────────────────────────────────────────────


def _make_cache(tmp_path) -> CacheManager:
    cache = CacheManager(str(tmp_path / "astrbot_plugin_kugou_music"))
    cache.ensure_ready()
    return cache


def _write_part(tmp_path, name, payload=b"payload"):
    path = tmp_path / f"{name}.part"
    path.write_bytes(payload)
    return str(path)


def _age(path, seconds):
    stamp = time.time() - seconds
    for target in (path, os.path.dirname(path)):
        os.utime(target, (stamp, stamp))


def test_ensure_ready_creates_dir_and_manifest(tmp_path):
    cache = _make_cache(tmp_path)
    assert os.path.isfile(os.path.join(cache.root, "manifest.json"))
    assert os.path.isdir(cache.cache_dir)
    assert os.path.isdir(cache.tmp_dir)


def test_ensure_ready_refuses_unowned_existing_dir(tmp_path):
    root = tmp_path / "astrbot_plugin_kugou_music"
    root.mkdir()
    (root / "someone-elses.mp3").write_bytes(b"x")
    with pytest.raises(SharedDirError) as err:
        CacheManager(str(root)).ensure_ready()
    assert "manifest" in str(err.value)


def test_ensure_ready_rejects_relative_path():
    with pytest.raises(SharedDirError):
        CacheManager("relative/dir").ensure_ready()


def test_store_is_atomic_and_cached_path_finds_it(tmp_path):
    cache = _make_cache(tmp_path)
    part = tmp_path / "task" / "audio.part"
    part.parent.mkdir()
    part.write_bytes(b"data")

    final = cache.store(str(part), "abc123", "flac", "song.flac")

    assert os.path.isfile(final)
    assert not part.exists()
    assert cache.cached_path("abc123", "flac") == final
    assert cache.cached_path("abc123", "hires") is None
    assert cache.cached_path("other", "flac") is None
    leftovers = [name for _, _, names in os.walk(cache.root) for name in names]
    assert not [name for name in leftovers if name.endswith(".part")]


def test_cleanup_removes_expired_dirs_but_keeps_held(tmp_path):
    cache = _make_cache(tmp_path)
    old = cache.store(_write_part(tmp_path, "a"), "oldkey", "flac", "a.flac")
    fresh = cache.store(_write_part(tmp_path, "b"), "freshkey", "flac", "b.flac")
    held = cache.store(_write_part(tmp_path, "c"), "heldkey", "flac", "c.flac")
    _age(old, 7200)
    _age(fresh, 10)
    _age(held, 7200)
    cache.hold(held)

    assert cache.cleanup(ttl_seconds=3600) == 1

    assert not os.path.exists(old)
    assert os.path.isfile(fresh)
    assert os.path.isfile(held)


def test_cleanup_ignores_plain_files_in_cache_dir(tmp_path):
    cache = _make_cache(tmp_path)
    stray = os.path.join(cache.cache_dir, "stray.mp3")
    with open(stray, "wb") as handle:
        handle.write(b"x")
    _age(stray, 7200)

    cache.cleanup(ttl_seconds=3600)

    assert os.path.exists(stray)


def _make_dir_link(target_dir, link_path) -> bool:
    """创建目录链接：优先符号链接，Windows 上退化为不需要特权的 junction。"""
    try:
        os.symlink(str(target_dir), str(link_path), target_is_directory=True)
        return True
    except (OSError, NotImplementedError, AttributeError):
        pass
    if os.name == "nt":
        import subprocess

        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link_path), str(target_dir)],
            capture_output=True,
        )
        return result.returncode == 0 and os.path.exists(str(link_path))
    return False


def test_cleanup_does_not_follow_directory_links(tmp_path):
    cache = _make_cache(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.mp3").write_bytes(b"x")
    link = os.path.join(cache.cache_dir, "link")
    if not _make_dir_link(outside, link):
        pytest.skip("本环境既不允许创建符号链接也不允许创建 junction")

    # ttl=0 让所有条目都「过期」，从而真正走到删除判定
    removed = cache.cleanup(ttl_seconds=0)

    assert (outside / "keep.mp3").exists()      # 绝不递归删除链接目标的内容
    assert removed == 0                          # 链接本身既不算过期缓存，也不删除
    assert os.path.exists(link)


def test_ensure_ready_rejects_linked_cache_subdir(tmp_path):
    """`<root>/cache` 本身是符号链接/junction 时必须在初始化就拒绝。

    否则 cleanup 会把链接目标（插件目录之外）的内容当成自己的缓存删掉。
    """
    root = tmp_path / "astrbot_plugin_kugou_music"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim").mkdir()
    (outside / "victim" / "data.txt").write_bytes(b"secret")

    root.mkdir()
    (root / "manifest.json").write_text(
        '{"plugin": "astrbot_plugin_kugou_music", "version": 1}', encoding="utf-8"
    )
    (root / "tmp").mkdir()
    if not _make_dir_link(outside, root / "cache"):
        pytest.skip("本环境既不允许创建符号链接也不允许创建 junction")

    with pytest.raises(SharedDirError) as err:
        CacheManager(str(root)).ensure_ready()

    assert "cache" in str(err.value)
    assert (outside / "victim" / "data.txt").exists()


# ── 下载 + 探针 + 入库 ───────────────────────────────────────────────────


def _make_fetcher(tmp_path, responses, limits=None):
    cache = _make_cache(tmp_path)
    session = FakeSession(responses)
    downloader = Downloader(
        session,
        limits or Limits(max_bytes=1_000_000),
        resolver=_public,
        user_agent="ua",
    )
    return AudioFetcher(downloader, cache), cache, session


def test_fetcher_rejects_non_audio_payload(tmp_path):
    fetcher, cache, _ = _make_fetcher(tmp_path, [FakeResponse([html_page()])])

    with pytest.raises(DownloadError) as err:
        asyncio.run(fetcher.fetch("key1", "flac", [_AUDIO_URL]))

    assert "音频" in err.value.reason
    assert cache.cached_path("key1", "flac") is None
    assert os.listdir(cache.tmp_dir) == []


def test_fetcher_reports_actual_tier_and_stores_by_it(tmp_path):
    body = flac_head(sample_rate=44100, bits=16)
    fetcher, cache, _ = _make_fetcher(tmp_path, [FakeResponse([body])])

    result = asyncio.run(fetcher.fetch("key1", "hires", [_AUDIO_URL]))

    assert result.actual_tier == "flac"
    assert result.extension == "flac"
    assert result.reused is False
    assert os.path.isfile(result.path)
    assert cache.cached_path("key1", "flac") == result.path
    assert cache.cached_path("key1", "hires") is None


def test_cached_reuse_returns_verified_entry(tmp_path):
    fetcher, _, _ = _make_fetcher(tmp_path, [FakeResponse([flac_head(bits=24)])])
    first = asyncio.run(fetcher.fetch("key1", "hires", [_AUDIO_URL]))

    second = asyncio.run(fetcher.cached("key1", "hires"))

    assert second is not None
    assert second.reused is True
    assert second.path == first.path
    assert second.actual_tier == "hires"


def test_cached_reuse_drops_corrupt_entry(tmp_path):
    fetcher, cache, _ = _make_fetcher(tmp_path, [FakeResponse([flac_head()])])
    first = asyncio.run(fetcher.fetch("key1", "flac", [_AUDIO_URL]))
    cache.release(first.path)          # 发送结束后调用方释放，之后才允许被丢弃
    with open(first.path, "wb") as handle:
        handle.write(b"not audio at all")

    assert asyncio.run(fetcher.cached("key1", "flac")) is None

    assert cache.cached_path("key1", "flac") is None


def test_held_entry_is_not_discarded(tmp_path):
    fetcher, cache, _ = _make_fetcher(tmp_path, [FakeResponse([flac_head()])])
    first = asyncio.run(fetcher.fetch("key1", "flac", [_AUDIO_URL]))
    with open(first.path, "wb") as handle:
        handle.write(b"not audio at all")

    assert asyncio.run(fetcher.cached("key1", "flac")) is None     # 仍被持有 → 不删除

    assert cache.cached_path("key1", "flac") == first.path


def test_cached_returns_none_for_unknown_key(tmp_path):
    fetcher, _, _ = _make_fetcher(tmp_path, [])
    assert asyncio.run(fetcher.cached("nope", "flac")) is None


def test_fetcher_uses_alternate_url_when_first_cdn_fails(tmp_path):
    fetcher, _, session = _make_fetcher(
        tmp_path, [OSError("reset"), FakeResponse([flac_head()])]
    )

    result = asyncio.run(
        fetcher.fetch("key1", "flac", ["http://a.kugou.com/1.mp3", "http://b.kugou.com/2.mp3"])
    )

    assert len(session.calls) == 2
    assert result.actual_tier == "flac"


def test_concurrent_same_resource_downloads_once(tmp_path):
    fetcher, _, session = _make_fetcher(tmp_path, [FakeResponse([flac_head()])])

    async def both():
        return await asyncio.gather(
            fetcher.fetch("key1", "flac", [_AUDIO_URL]),
            fetcher.fetch("key1", "flac", [_AUDIO_URL]),
        )

    results = asyncio.run(both())

    assert len(session.calls) == 1
    assert results[0].path == results[1].path


def test_concurrency_is_capped(tmp_path):
    fetcher, _, _ = _make_fetcher(tmp_path, [])
    assert fetcher.limits.max_concurrency == 2
