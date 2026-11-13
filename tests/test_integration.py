"""端到端接线测试：真实 CacheManager + AudioFetcher + DownloadLadder。

这一层是本次改造里唯一没有被单元测试覆盖的接缝（两侧各自用假对象测试），
`CacheManager.resource_key` 缺失就是在该接缝处漏掉的。
"""

import asyncio
import os

from audio_builders import flac_head
from fake_session import FakeResponse, FakeSession
from file_cache import AudioFetcher, CacheManager, Downloader, Limits
from kugou_client import DownloadLadder, QualityOption, ResolvedUrl

_SONG = {
    "hash": "ABCDEF", "album_id": "123", "album_audio_id": "456",
    "songname": "晴天", "singername": "周杰伦", "duration": 269,
}


def _public(host):
    return ["93.184.216.34"]


def _option(tier):
    return QualityOption(
        tier=tier, hash=f"{tier.upper()}-HASH", extname="flac", size=26,
        privilege=0, status=1, pay_type=0, level=5, require_vip_type="",
        has_resource=True, reason="",
    )


class _Client:
    """只实现协议侧的两件事；HTTP 由真实的 Downloader 负责。"""

    def __init__(self):
        self.resolve_calls = []

    async def privileges(self, song):
        return [_option("hires"), _option("flac")]

    async def resolve_urls(self, song, option):
        self.resolve_calls.append(option.tier)
        return ResolvedUrl(
            tier=option.tier, urls=[f"http://fs.kugou.com/{option.tier}.flac"],
            extname="flac", size=26, hash=option.hash,
        )


def _wire(tmp_path, responses):
    cache = CacheManager(str(tmp_path / "shared" / "astrbot_plugin_kugou_music"))
    cache.ensure_ready()
    session = FakeSession(responses)
    downloader = Downloader(
        session, Limits(max_bytes=1_000_000), resolver=_public, user_agent="ua"
    )
    return cache, session, AudioFetcher(downloader, cache)


def test_ladder_end_to_end_downloads_once_and_stores_by_actual_tier(tmp_path):
    # 请求 hires，上游实际给出 CD 规格 FLAC → 只下载一次，按实测档位入库并复用
    cache, session, fetcher = _wire(
        tmp_path, [FakeResponse([flac_head(bits=16, total_samples=44100 * 269)])]
    )
    client = _Client()
    ladder = DownloadLadder(client, fetcher, cache, ["hires", "flac"])

    outcome = asyncio.run(ladder.run(_SONG))

    assert client.resolve_calls == ["hires"]          # 未对 flac 档位再解析
    assert len(session.calls) == 1                    # 只下载一次
    assert outcome.tier == "flac" and outcome.label == "FLAC"
    assert outcome.filename == "周杰伦 - 晴天 [FLAC].flac"
    assert os.path.isfile(outcome.path)
    assert cache.cached_path(cache.resource_key(_SONG), "flac") == outcome.path
    assert cache.cached_path(cache.resource_key(_SONG), "hires") is None

    asyncio.run(ladder.release(outcome))


def test_ladder_reuses_cache_on_a_later_run(tmp_path):
    cache, session, fetcher = _wire(
        tmp_path, [FakeResponse([flac_head(bits=24, total_samples=44100 * 269)])]
    )
    first = asyncio.run(DownloadLadder(_Client(), fetcher, cache, ["hires", "flac"]).run(_SONG))
    assert first.tier == "hires"                      # 24 bit → 真 Hi-Res
    asyncio.run(DownloadLadder(_Client(), fetcher, cache, ["hires"]).release(first))

    # 新一次运行：不解析、不下载，直接用已验证缓存
    session2 = FakeSession([])
    fetcher2 = AudioFetcher(
        Downloader(session2, Limits(max_bytes=1_000_000), resolver=_public), cache
    )
    second = asyncio.run(
        DownloadLadder(_Client(), fetcher2, cache, ["hires", "flac"]).run(_SONG)
    )

    assert second.from_cache is True
    assert second.path == first.path
    assert second.filename == "周杰伦 - 晴天 [Hi-Res].flac"
    assert session2.calls == []


def test_ladder_reports_missing_shared_dir_before_anything_else(tmp_path):
    # 目录不可用时必须在接触网络之前就失败（配置问题不得伪装成音质不可用）
    cache = CacheManager(str(tmp_path / "unowned" / "astrbot_plugin_kugou_music"))
    cache.root and os.makedirs(cache.root, exist_ok=True)      # 存在但没有 manifest
    session = FakeSession([FakeResponse([flac_head()])])
    fetcher = AudioFetcher(
        Downloader(session, Limits(max_bytes=1_000_000), resolver=_public), cache
    )
    ladder = DownloadLadder(_Client(), fetcher, cache, ["flac"])

    import pytest

    from file_cache import SharedDirError

    with pytest.raises(SharedDirError):
        asyncio.run(ladder.run(_SONG))

    assert session.calls == []


def test_fetcher_probes_download_head_with_large_id3_tag(tmp_path):
    # 覆盖率很高的场景：MP3 带大封面（ID3v2 超过下载探测头长度）也要能识别
    from audio_builders import id3v2, mp3_frame

    payload = id3v2(70000) + mp3_frame(9)
    _, _, fetcher = _wire(tmp_path, [FakeResponse([payload])])

    result = asyncio.run(fetcher.fetch("key1", "128", ["http://fs.kugou.com/a.mp3"]))

    assert result.actual_tier == "128"
    assert result.extension == "mp3"
