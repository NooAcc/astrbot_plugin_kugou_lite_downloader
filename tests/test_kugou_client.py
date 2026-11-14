import asyncio

import pytest
from fake_session import FakeSession, json_response

from kugou_client import (
    APPID,
    CLIENTVER,
    GATEWAY,
    PRIVILEGE_ROUTER,
    PUBLIC_SEARCH_URL,
    SEARCH_CLIENTVER,
    SEARCH_ROUTER,
    Credentials,
    Device,
    KugouApiError,
    KugouClient,
    android_sign,
    compact_json,
    default_params,
    redact_secrets,
    sign_key,
    web_sign,
)


# ── 签名（金标量由独立脚本按上游算法算出后固化） ──────────────────────────


def test_android_sign_matches_upstream_algorithm():
    # md5("LnT6xpN3khm36zse0QzvmgTZ3waWdRSA" + "a=1b=2" + "LnT6xpN3khm36zse0QzvmgTZ3waWdRSA")
    assert android_sign({"b": 2, "a": "1"}) == "bc040a436f0cfec4f1489d8c8674833f"


def test_android_sign_serializes_object_values_compactly():
    # 值为 dict/list 时用紧凑 JSON（等价于 JS JSON.stringify）
    assert android_sign({"resource": [{"type": "audio"}]}) == "97b0a44bfb3c35ef8a5a5c4ca7783e1f"


def test_android_sign_includes_post_body():
    assert android_sign({"a": "1"}, body='{"x":1}') == "815038f669dcaff3c3c6f59b479e80dc"
    assert android_sign({"a": "1"}, body='{"x":1}') != android_sign({"a": "1"})


def test_web_sign_vector():
    assert web_sign({"k": "v"}) == "144db51a67ecf96e28b5c1c20f613a3b"


def test_sign_key_vector_and_empty_userid():
    assert sign_key("abc", "mid1", "123", "3116") == "f94dcd8f695d85cc7c40f33513991042"
    assert sign_key("abc", "mid1", "", "3116") == sign_key("abc", "mid1", "0", "3116")


def test_compact_json_has_no_spaces():
    assert compact_json({"a": 1, "b": "中"}) == '{"a":1,"b":"中"}'
    assert compact_json([{"a": 1}, 2]) == '[{"a":1},2]'


# ── 通用参数 ─────────────────────────────────────────────────────────────


def test_default_params_include_device_and_credentials():
    device = Device(dfid="d", mid="m", uuid="u")

    anonymous = default_params(device, None)
    assert (anonymous["appid"], anonymous["clientver"]) == (APPID, CLIENTVER)
    assert (anonymous["dfid"], anonymous["mid"], anonymous["uuid"]) == ("d", "m", "u")
    assert "token" not in anonymous and "userid" not in anonymous

    signed_in = default_params(device, Credentials(token="t", userid="7"))
    assert signed_in["token"] == "t" and signed_in["userid"] == "7"


def test_default_params_clientver_can_be_overridden_for_search():
    device = Device(dfid="d", mid="m", uuid="u")
    assert default_params(device, None, clientver=SEARCH_CLIENTVER)["clientver"] == 201


# ── 脱敏 ─────────────────────────────────────────────────────────────────


def test_redact_secrets_masks_credentials_and_query_strings():
    source = {
        "token": "t" * 20,
        "signature": "s" * 32,
        "dfid": "d" * 24,
        "mid": "m" * 24,
        "url": "https://fs.kgidc.cn/a/b.mp3?sig=deadbeef&token=zzz",
        "nested": [{"key": "k" * 32}],
        "songname": "晴天",
    }

    out = redact_secrets(source)

    assert out["token"] == "***"
    assert out["signature"] == "***"
    assert out["dfid"] == "***"
    assert out["mid"] == "***"
    assert out["url"] == "https://fs.kgidc.cn/a/b.mp3"
    assert out["nested"][0]["key"] == "***"
    assert out["songname"] == "晴天"


def test_redact_secrets_masks_url_keys_and_bare_url_values():
    out = redact_secrets(
        {
            "play_url": "http://fs.kugou.com/x.mp3?t=1",
            "extra": ["https://a.kugou.com/y.flac?sig=1"],
        }
    )
    assert out["play_url"] == "http://fs.kugou.com/x.mp3"
    assert out["extra"][0] == "https://a.kugou.com/y.flac"


def test_redact_secrets_masks_long_hex_and_keeps_plain_text():
    out = redact_secrets({"note": "a" * 40, "word": "hello", "num": 3})
    assert out["note"] == "***"
    assert out["word"] == "hello" and out["num"] == 3


def test_redact_secrets_does_not_mutate_input():
    source = {"token": "abc"}
    redact_secrets(source)
    assert source == {"token": "abc"}


def test_redact_secrets_handles_none_and_scalars():
    assert redact_secrets(None) is None
    assert redact_secrets("plain") == "plain"
    assert pytest.approx(redact_secrets(1.5)) == 1.5


# ── 搜索 ─────────────────────────────────────────────────────────────────

_CREDS = Credentials(token="t", userid="7")
_SONG = {
    "hash": "ABCDEF",
    "songname": "晴天",
    "singername": "周杰伦",
    "album_id": 123,
    "album_audio_id": 456,
    "duration": 269,
    "pay_type": 3,
}


def _client(responses, credentials=_CREDS, ua="ua"):
    session = FakeSession(responses)
    device = Device(dfid="d", mid="m", uuid="u")
    return KugouClient(session, device, credentials, ua=ua, timeout=5.0), session


def test_search_uses_lite_endpoint_with_expected_params():
    client, session = _client([json_response({"status": 1, "data": {"info": [_SONG]}})])

    songs = asyncio.run(client.search("晴天", 5))

    call = session.calls[0]
    assert call["url"] == GATEWAY + "/v2/search/song"
    assert call["kw"]["headers"]["x-router"] == SEARCH_ROUTER
    query = call["kw"]["params"]
    assert query["clientver"] == SEARCH_CLIENTVER == 201
    assert query["appid"] == APPID == 3116
    assert query["keyword"] == "晴天" and query["pagesize"] == 5
    assert query["platform"] == "AndroidFilter" and query["area_code"] == 1
    assert query["token"] == "t" and query["userid"] == "7"
    expected = {k: v for k, v in query.items() if k != "signature"}
    assert query["signature"] == android_sign(expected)
    assert songs[0]["hash"] == "ABCDEF"
    assert songs[0]["songname"] == "晴天" and songs[0]["singername"] == "周杰伦"


def test_search_normalizes_alternate_field_names_and_ms_duration():
    payload = {
        "status": 1,
        "data": {"info": [{
            "FileHash": "AA", "SongName": "X", "SingerName": "Y",
            "AlbumID": 9, "AlbumAudioID": 8, "Duration": 123456, "PayType": 0,
        }]},
    }
    client, _ = _client([json_response(payload)])

    songs = asyncio.run(client.search("x", 1))

    assert songs[0]["hash"] == "AA"
    assert songs[0]["duration"] == 123          # 123456 毫秒 → 123 秒
    assert songs[0]["album_id"] == "9"
    assert songs[0]["album_audio_id"] == 8


def test_search_parses_lists_shape():
    payload = {"status": 1, "data": {"lists": [_SONG]}}
    client, _ = _client([json_response(payload)])
    assert asyncio.run(client.search("x", 1))[0]["hash"] == "ABCDEF"


def test_search_skips_items_without_hash():
    payload = {"status": 1, "data": {"info": [{"songname": "无 hash"}, _SONG]}}
    client, _ = _client([json_response(payload)])
    assert [s["hash"] for s in asyncio.run(client.search("x", 2))] == ["ABCDEF"]


def test_search_falls_back_to_public_when_lite_rejects():
    client, session = _client([
        json_response({"status": 0, "error_code": 152}),
        json_response({"status": 1, "data": {"info": [_SONG]}}),
    ])

    songs = asyncio.run(client.search("晴天", 5))

    assert len(songs) == 1
    assert session.calls[0]["url"] == GATEWAY + "/v2/search/song"
    assert session.calls[1]["url"] == PUBLIC_SEARCH_URL
    assert "signature" not in session.calls[1]["kw"]["params"]
    assert "token" not in session.calls[1]["kw"]["params"]


def test_search_survives_lite_transport_error():
    client, session = _client([OSError("boom"), json_response({"status": 1, "data": {"info": [_SONG]}})])

    songs = asyncio.run(client.search("x", 3))

    assert len(songs) == 1
    assert session.calls[1]["url"] == PUBLIC_SEARCH_URL


def test_search_without_credentials_goes_straight_to_public():
    client, session = _client(
        [json_response({"status": 1, "data": {"info": [_SONG]}})], credentials=None
    )

    songs = asyncio.run(client.search("x", 3))

    assert len(session.calls) == 1
    assert session.calls[0]["url"] == PUBLIC_SEARCH_URL
    assert len(songs) == 1


def test_search_returns_empty_when_both_sources_fail():
    client, session = _client([json_response({"status": 0}), json_response({"status": 0})])
    assert asyncio.run(client.search("x", 3)) == []
    assert len(session.calls) == 2


def test_search_raises_when_both_sources_error():
    # 连公共来源都请求失败 → 让上层区分「没搜到」与「搜索失败」
    client, _ = _client([
        OSError("lite down"),
        json_response({}, status=500),
    ])
    with pytest.raises(KugouApiError):
        asyncio.run(client.search("x", 3))


def test_post_json_signs_the_exact_body_sent():
    client, session = _client([json_response({"status": 1, "data": []})])
    params = default_params(Device(dfid="d", mid="m", uuid="u"), _CREDS, clienttime=1700000000)
    body_obj = {"resource": [{"type": "audio", "hash": "abc"}], "qualities": ["128", "320"]}

    asyncio.run(
        client.api_post_json(GATEWAY + "/v2/get_res_privilege/lite", params, body_obj,
                             router=PRIVILEGE_ROUTER)
    )

    call = session.calls[0]
    assert call["kw"]["headers"]["x-router"] == PRIVILEGE_ROUTER
    assert call["kw"]["headers"]["Content-Type"] == "application/json"
    assert call["kw"]["data"] == compact_json(body_obj)
    query = call["kw"]["params"]
    expected = {k: v for k, v in query.items() if k != "signature"}
    assert query["signature"] == android_sign(expected, body=call["kw"]["data"])


def test_post_json_returns_empty_on_bad_json():
    client, _ = _client([json_response("not-json-but-object")])
    assert asyncio.run(client.api_post_json(GATEWAY + "/x", {}, {})) == {}


# ── 权益查询与档位资源解析 ───────────────────────────────────────────────

import json  # noqa: E402

from kugou_client import (  # noqa: E402
    URL_PAGE_ID,
    URL_PID,
    URL_PPAGE_ID,
    ResourceUnavailable,
    parse_privilege,
)
from quality import TIERS  # noqa: E402

_SONG_OBJ = {"hash": "ABCDEF", "album_id": 123, "album_audio_id": 456}

_PRIVILEGE_FIXTURE = {
    "status": 1,
    "data": [{
        "hash": "ABC",
        "album_id": 1,
        "album_audio_id": 2,
        "name": "晴天",
        "singername": "周杰伦",
        "trans_param": {
            "ogg_128_hash": "O128", "ogg_128_filesize": 3100000,
            "ogg_320_hash": "O320", "ogg_320_filesize": 9200000,
        },
        "relate_goods": [
            {"quality": "128", "level": 1, "privilege": 0, "status": 1, "hash": "M128",
             "info": {"extname": "ogg", "bitrate": 128000, "filesize": 3000000}},
            {"quality": "320", "level": 4, "privilege": 0, "status": 1, "hash": "M320",
             "info": {"extname": "mp3", "bitrate": 320000, "filesize": 9000000}},
            {"quality": "flac", "level": 5, "privilege": 10, "status": 1, "pay_type": 3, "hash": "F",
             "info": {"extname": "flac", "bitrate": 900000, "filesize": 30000000}},
            {"quality": "high", "level": 6, "privilege": 10, "status": 1, "pay_type": 3, "hash": "H",
             "info": {"extname": "flac", "bitrate": 2300000, "filesize": 70000000}},
        ],
    }],
}


def _options(fixture=_PRIVILEGE_FIXTURE, song=_SONG_OBJ):
    return {option.tier: option for option in parse_privilege(fixture, song)}


def test_parse_privilege_prefers_ogg_hash_for_lossy_tiers():
    options = _options()
    assert options["128"].hash == "O128" and options["128"].extname == "ogg"
    assert options["128"].size == 3100000
    assert options["320"].hash == "O320" and options["320"].extname == "ogg"
    assert options["hires"].hash == "H" and options["flac"].hash == "F"


def test_parse_privilege_reports_resource_and_upstream_requirements():
    options = _options()
    # 四个档位上游都提供资源 → 都值得向服务端请求（是否放行由服务端裁决）
    assert all(option.has_resource for option in options.values())
    assert options["flac"].require_vip_type == "concept"
    assert "需要会员" in options["flac"].reason
    assert "privilege=10" in options["flac"].reason
    assert options["hires"].require_vip_type == "concept"
    assert options["128"].require_vip_type == "" and options["128"].reason == ""
    assert options["320"].require_vip_type == "" and options["320"].reason == ""


def test_parse_privilege_covers_every_configured_tier():
    assert set(_options()) == set(TIERS)


def test_parse_privilege_reports_missing_quality():
    fixture = {"status": 1, "data": [{"hash": "ABC", "relate_goods": []}]}
    options = _options(fixture)
    assert options["flac"].hash == ""
    assert options["flac"].has_resource is False
    assert "无此音质" in options["flac"].reason


def test_parse_privilege_marks_super_vip_tier():
    # 上游把该档位标为 level ≥ 7（DSD 级）时，原因文本要区分「超级VIP」
    fixture = {
        "status": 1,
        "data": [{"hash": "ABC", "relate_goods": [
            {"quality": "high", "level": 7, "privilege": 10, "status": 1, "hash": "D",
             "info": {"extname": "dsd"}},
        ]}],
    }
    options = _options(fixture)
    assert options["hires"].require_vip_type == "suvip"
    assert "超级VIP" in options["hires"].reason


def test_parse_privilege_handles_empty_payload():
    options = _options({"status": 0})
    assert set(options) == set(TIERS)
    assert all(not option.has_resource for option in options.values())


def _lossless_option(tier="hires", hash_value="H"):
    return {option.tier: option for option in parse_privilege(_PRIVILEGE_FIXTURE, _SONG_OBJ)}[tier]


def test_resolve_urls_requests_the_tier_hash_and_protocol_quality():
    client, session = _client([json_response({"status": 1, "url": ["http://fs.kugou.com/a.flac"]})])

    resolved = asyncio.run(client.resolve_urls(_SONG_OBJ, _lossless_option()))

    query = session.calls[0]["kw"]["params"]
    assert query["quality"] == "high"                      # hires → high；不存在 hires 参数
    assert query["hash"] == "h"                            # 统一小写
    assert query["key"] == sign_key("h", "m", "7", APPID)
    assert query["ssa_flag"] == "is_fromtrack"
    assert (query["page_id"], query["pid"], query["ppage_id"]) == (URL_PAGE_ID, URL_PID, URL_PPAGE_ID)
    assert (query["appid"], query["clientver"]) == (APPID, 11430)
    assert query["token"] == "t" and query["userid"] == "7"
    assert len(query["signature"]) == 32
    expected = {k: v for k, v in query.items() if k != "signature"}
    assert query["signature"] == android_sign(expected)
    assert resolved.urls == ["http://fs.kugou.com/a.flac"]
    assert resolved.tier == "hires"


def test_resolve_urls_tries_second_endpoint_when_first_fails():
    client, session = _client([
        OSError("cdn down"),
        json_response({"status": 1, "url": ["http://fs.kugou.com/b.flac"]}),
    ])

    resolved = asyncio.run(client.resolve_urls(_SONG_OBJ, _lossless_option()))

    assert session.calls[0]["url"].startswith("https://trackercdn.kugou.com")
    assert session.calls[1]["url"] == GATEWAY + "/v5/url"
    assert session.calls[1]["kw"]["headers"]["x-router"] == "trackercdn.kugou.com"
    assert resolved.urls == ["http://fs.kugou.com/b.flac"]


def test_resolve_urls_reports_failure_reason_from_payload():
    client, _ = _client([
        json_response({"status": 0, "fail_process": 6, "error": "need verify"}),
        json_response({"status": 0, "error_code": 152}),
    ])

    with pytest.raises(ResourceUnavailable) as err:
        asyncio.run(client.resolve_urls(_SONG_OBJ, _lossless_option()))

    assert err.value.tier == "hires"
    assert "fail_process=6" in err.value.reason or "152" in err.value.reason


def test_resolve_urls_rejects_empty_url_list():
    client, _ = _client([json_response({"status": 1, "url": []}), json_response({"status": 1, "url": []})])
    with pytest.raises(ResourceUnavailable):
        asyncio.run(client.resolve_urls(_SONG_OBJ, _lossless_option()))


def test_resolve_urls_normalizes_single_string_url():
    # 服务端偶尔把 url 返回成单个字符串；逐字符迭代会得到一串假地址
    client, _ = _client([json_response({"status": 1, "url": "http://fs.kugou.com/a.flac"})])

    resolved = asyncio.run(client.resolve_urls(_SONG_OBJ, _lossless_option()))

    assert resolved.urls == ["http://fs.kugou.com/a.flac"]


def test_resolve_urls_requires_credentials_for_vip_tier():
    client, session = _client([], credentials=None)

    with pytest.raises(ResourceUnavailable) as err:
        asyncio.run(client.resolve_urls(_SONG_OBJ, _lossless_option()))

    assert "登录" in err.value.reason
    assert session.calls == []


def test_privileges_posts_the_documented_body():
    client, session = _client([json_response(_PRIVILEGE_FIXTURE)])

    options = asyncio.run(client.privileges(_SONG_OBJ))

    call = session.calls[0]
    assert call["url"] == GATEWAY + "/v2/get_res_privilege/lite"
    assert call["kw"]["headers"]["x-router"] == PRIVILEGE_ROUTER
    body = json.loads(call["kw"]["data"])
    assert body["appid"] == APPID and body["relate"] == 1 and body["need_hash_offset"] == 1
    assert body["resource"] == [{"type": "audio", "page_id": 0, "hash": "abcdef", "album_id": 123}]
    assert "high" in body["qualities"] and "128" in body["qualities"]
    assert {option.tier for option in options} == set(TIERS)


# ── 档位降级编排 ─────────────────────────────────────────────────────────

from audio_builders import flac_head  # noqa: E402
from audio_probe import probe as _probe_head  # noqa: E402
from file_cache import (  # noqa: E402
    DownloadError,
    FetchResult,
    SharedDirError,
    UnsafeUrlError,
)
from kugou_client import (  # noqa: E402
    AllQualitiesFailed,
    DownloadLadder,
    DownloadOutcome,
    QualityOption,
    ResolvedUrl,
)

_SONG_LADDER = {"hash": "ABCDEF", "album_id": 123, "album_audio_id": 456,
                "songname": "晴天", "singername": "周杰伦"}


def _option(tier, has_resource=True, reason="", hash_value=None, require_vip_type=""):
    return QualityOption(
        tier=tier, hash=(hash_value or tier.upper()) if has_resource else "",
        extname="flac", size=1, privilege=0 if not reason else 10, status=1,
        pay_type=0, level=5, require_vip_type=require_vip_type,
        has_resource=has_resource, reason=reason,
    )


def _fetch_result(actual_tier, extension="flac", path=None, url="http://fs.kugou.com/a.flac"):
    return FetchResult(
        path=path or f"/cache/{actual_tier}.{extension}", actual_tier=actual_tier,
        extension=extension, info=_probe_head(flac_head()), url=url, size=26, reused=False,
    )


class _FakeClient:
    def __init__(self, options, urls=None, errors=None):
        self._options = list(options)
        self._urls = urls or {}
        self._errors = errors or {}
        self.privilege_calls = 0
        self.resolve_calls = []

    async def privileges(self, song):
        self.privilege_calls += 1
        return list(self._options)

    async def resolve_urls(self, song, option):
        self.resolve_calls.append(option.tier)
        if option.tier in self._errors:
            raise self._errors[option.tier]
        return ResolvedUrl(tier=option.tier, urls=self._urls.get(option.tier, ["http://fs.kugou.com/a.flac"]),
                           extname="flac", size=1, hash=option.hash)


class _FakeFetcher:
    def __init__(self, results=None, cached=None):
        self._results = dict(results or {})
        self._cached = dict(cached or {})
        self.downloads = []
        self.songs = []

    async def cached(self, key, tier):
        return self._cached.get(tier)

    async def fetch(self, key, tier, urls, song=None):
        self.downloads.append(tier)
        self.songs.append(song)
        result = self._results.get(tier)
        if isinstance(result, Exception):
            raise result
        self._cached[result.actual_tier] = result     # 模拟按实测档位入库
        return result


class _FakeCache:
    def __init__(self):
        self.released = []
        self.discarded = []

    def resource_key(self, song):
        return "key1"

    def key_for(self, song):
        return "key1"

    def release(self, path):
        self.released.append(path)

    def discard_entry(self, key, tier):
        self.discarded.append((key, tier))


def _ladder(priority, options, fetcher, cache=None):
    client = _FakeClient(options)
    return DownloadLadder(client, fetcher, cache or _FakeCache(), priority), client


def test_uses_verified_cache_first_without_resolving():
    fetcher = _FakeFetcher(cached={"hires": _fetch_result("hires")})
    ladder, client = _ladder(["hires", "flac"], [_option("hires"), _option("flac")], fetcher)

    outcome = asyncio.run(ladder.run(_SONG_LADDER))

    assert outcome.from_cache is True and outcome.tier == "hires"
    assert client.resolve_calls == []
    assert fetcher.downloads == []
    assert client.privilege_calls == 0


def test_downloads_once_when_high_tier_yields_lower_actual_quality():
    # Review Focus #3：请求 hires 实得 CD 规格 FLAC → 该文件按 flac 入库 → flac 档位复用，不重复下载
    fetcher = _FakeFetcher(results={"hires": _fetch_result("flac")})
    ladder, client = _ladder(["hires", "flac"], [_option("hires"), _option("flac")], fetcher)

    outcome = asyncio.run(ladder.run(_SONG_LADDER))

    assert fetcher.downloads == ["hires"]
    assert client.resolve_calls == ["hires"]
    assert outcome.tier == "flac" and outcome.label == "FLAC"
    # 本次运行内复用了刚下载的文件（没有为 flac 档位再发起下载）
    assert outcome.from_cache is True
    assert outcome.path == fetcher._cached["flac"].path


def test_vip_required_tier_is_still_attempted_and_served():
    # 权益接口说「需要会员」，但服务端实际给了地址 → 必须发送（权限由服务端裁决）
    options = [_option("flac", require_vip_type="concept",
                       reason="需要会员才能获取（privilege=10）")]
    fetcher = _FakeFetcher(results={"flac": _fetch_result("flac")})
    ladder, client = _ladder(["flac"], options, fetcher)

    outcome = asyncio.run(ladder.run(_SONG_LADDER))

    assert outcome.tier == "flac"
    assert client.resolve_calls == ["flac"]        # 未因 privilege≠0 而跳过


def test_server_refusal_keeps_membership_hint():
    error = ResourceUnavailable("flac", "解析播放地址被服务端拒绝：fail_process=6")
    options = [_option("flac", require_vip_type="concept",
                       reason="需要会员才能获取（privilege=10）")]
    client = _FakeClient(options, errors={"flac": error})
    ladder = DownloadLadder(client, _FakeFetcher(), _FakeCache(), ["flac"])

    with pytest.raises(AllQualitiesFailed) as err:
        asyncio.run(ladder.run(_SONG_LADDER))

    summary = str(err.value)
    assert "fail_process=6" in summary and "需要会员" in summary


def test_tier_without_upstream_resource_is_skipped():
    fetcher = _FakeFetcher(results={"flac": _fetch_result("flac")})
    options = [_option("hires", has_resource=False), _option("flac")]
    ladder, client = _ladder(["hires", "flac"], options, fetcher)

    outcome = asyncio.run(ladder.run(_SONG_LADDER))

    assert outcome.tier == "flac"
    assert client.resolve_calls == ["flac"]        # 未对上游没有的档位解析地址


def test_does_not_try_tiers_outside_configured_priority():
    fetcher = _FakeFetcher(results={"320": _fetch_result("128")})
    ladder, _ = _ladder(["320"], [_option("320"), _option("flac")], fetcher)

    with pytest.raises(AllQualitiesFailed) as err:
        asyncio.run(ladder.run(_SONG_LADDER))

    assert fetcher.downloads == ["320"]        # 即使 flac 可用也不请求未配置的档位
    assert [tier for tier, _ in err.value.reasons] == ["320"]


def test_higher_actual_tier_satisfies_lower_request():
    fetcher = _FakeFetcher(results={"320": _fetch_result("flac")})
    ladder, _ = _ladder(["320"], [_option("320")], fetcher)

    outcome = asyncio.run(ladder.run(_SONG_LADDER))

    assert outcome.tier == "flac" and outcome.label == "FLAC"


def test_all_tiers_unavailable_raises_with_reason_summary():
    # hires：上游有资源但服务端拒绝；flac：上游没有该资源
    options = [_option("hires", require_vip_type="concept",
                       reason="需要会员才能获取（privilege=10）"),
               _option("flac", has_resource=False)]
    error = ResourceUnavailable("hires", "解析播放地址被服务端拒绝：fail_process=6")
    client = _FakeClient(options, errors={"hires": error})
    ladder = DownloadLadder(client, _FakeFetcher(), _FakeCache(), ["hires", "flac"])

    with pytest.raises(AllQualitiesFailed) as err:
        asyncio.run(ladder.run(_SONG_LADDER))

    summary = str(err.value)
    assert "hires" in summary and "flac" in summary
    assert "需要会员" in summary and "上游无此音质" in summary


def test_resource_unavailable_is_recorded_and_next_tier_tried():
    error = ResourceUnavailable("hires", "解析播放地址被服务端拒绝：fail_process=6")
    fetcher = _FakeFetcher(results={"flac": _fetch_result("flac")})
    client = _FakeClient([_option("hires"), _option("flac")], errors={"hires": error})
    ladder = DownloadLadder(client, fetcher, _FakeCache(), ["hires", "flac"])

    outcome = asyncio.run(ladder.run(_SONG_LADDER))

    assert outcome.tier == "flac"


def test_network_error_is_reported_as_itself():
    fetcher = _FakeFetcher(results={"hires": DownloadError("连接失败：ConnectionResetError")})
    ladder, _ = _ladder(["hires"], [_option("hires")], fetcher)

    with pytest.raises(AllQualitiesFailed) as err:
        asyncio.run(ladder.run(_SONG_LADDER))

    assert "连接失败" in str(err.value)
    assert "无此音质" not in str(err.value)


def test_unrecognizable_audio_is_reported_and_next_tier_tried():
    fetcher = _FakeFetcher(results={"hires": DownloadError("音频规格无法识别或低于最低档位"),
                                    "flac": _fetch_result("flac")})
    ladder, _ = _ladder(["hires", "flac"], [_option("hires"), _option("flac")], fetcher)

    assert asyncio.run(ladder.run(_SONG_LADDER)).tier == "flac"


def test_shared_dir_error_propagates_to_caller():
    # 目录/配置错误不得伪装成「音质不可用」
    fetcher = _FakeFetcher(results={"hires": SharedDirError("共享缓存目录不可写")})
    ladder, _ = _ladder(["hires"], [_option("hires")], fetcher)

    with pytest.raises(SharedDirError):
        asyncio.run(ladder.run(_SONG_LADDER))


def test_unsafe_url_error_propagates_to_caller():
    fetcher = _FakeFetcher(results={"hires": UnsafeUrlError("主机不在允许列表", url="http://x/")})
    ladder, _ = _ladder(["hires"], [_option("hires")], fetcher)

    with pytest.raises(UnsafeUrlError):
        asyncio.run(ladder.run(_SONG_LADDER))


def test_trial_fragment_is_not_sent_as_a_full_song():
    # 搜索结果显示 269 秒，实测只有 10 秒（flac_head 默认）→ 是试听片段
    song = {**_SONG_LADDER, "duration": 269}
    cache = _FakeCache()
    fetcher = _FakeFetcher(results={"flac": _fetch_result("flac")})
    ladder, _ = _ladder(["flac"], [_option("flac")], fetcher, cache)

    with pytest.raises(AllQualitiesFailed) as err:
        asyncio.run(ladder.run(song))

    assert "试听" in str(err.value)
    assert cache.discarded == [("key1", "flac")]
    assert cache.released == ["/cache/flac.flac"]      # 先释放再丢弃


def test_cached_trial_fragment_is_discarded_before_reuse():
    song = {**_SONG_LADDER, "duration": 269}
    cache = _FakeCache()
    fetcher = _FakeFetcher(cached={"flac": _fetch_result("flac")})
    ladder, _ = _ladder(["flac"], [_option("flac")], fetcher, cache)

    with pytest.raises(AllQualitiesFailed) as err:
        asyncio.run(ladder.run(song))

    assert "试听" in str(err.value)
    assert cache.discarded == [("key1", "flac")]
    assert fetcher.downloads == []


def test_duration_within_tolerance_is_accepted():
    # 服务端时长不精确：260 秒对 269 秒（96.7%）应当照常发送
    song = {**_SONG_LADDER, "duration": 269}
    info = _probe_head(flac_head(total_samples=44100 * 260))
    full = FetchResult(
        path="/cache/flac.flac", actual_tier="flac", extension="flac",
        info=info, url="", size=26, reused=False,
    )
    ladder, _ = _ladder(["flac"], [_option("flac")], _FakeFetcher(results={"flac": full}))

    outcome = asyncio.run(ladder.run(song))

    assert outcome.tier == "flac"


def test_filename_uses_actual_tier_and_extension():
    fetcher = _FakeFetcher(results={"hires": _fetch_result("hires", extension="flac")})
    ladder, _ = _ladder(["hires"], [_option("hires")], fetcher)

    outcome = asyncio.run(ladder.run(_SONG_LADDER))

    assert outcome.filename == "周杰伦 - 晴天 [Hi-Res].flac"
    assert outcome.label == "Hi-Res"


def test_release_returns_the_hold():
    cache = _FakeCache()
    fetcher = _FakeFetcher(results={"flac": _fetch_result("flac")})
    ladder, _ = _ladder(["flac"], [_option("flac")], fetcher, cache=cache)

    outcome = asyncio.run(ladder.run(_SONG_LADDER))
    asyncio.run(ladder.release(outcome))

    assert cache.released == [outcome.path]


def test_privileges_are_queried_only_once_per_run():
    fetcher = _FakeFetcher(results={"320": _fetch_result("320")})
    options = [_option("hires", has_resource=False), _option("flac", has_resource=False), _option("320")]
    ladder, client = _ladder(["hires", "flac", "320"], options, fetcher)

    assert asyncio.run(ladder.run(_SONG_LADDER)).tier == "320"
    assert client.privilege_calls == 1


def test_download_outcome_is_frozen_dataclass():
    outcome = DownloadOutcome(path="p", filename="f", tier="flac", label="FLAC",
                              info=None, url="", from_cache=False)
    with pytest.raises(Exception):
        outcome.tier = "hires"


# ── 归一化：专辑与封面 ───────────────────────────────────────────────────


def test_normalize_adds_album_and_cover_url():
    payload = {"status": 1, "data": {"info": [{
        "hash": "H1", "songname": "晴天", "singername": "周杰伦", "duration": 269,
        "AlbumName": "叶惠美",
        "trans_param": {"union_cover": "http://imge.kugou.com/stdmusic/{size}/x.jpg"},
    }]}}
    client, _ = _client([json_response(payload)])
    song = asyncio.run(client.search("x", 1))[0]
    assert song["album"] == "叶惠美"
    assert song["cover"] == "https://imge.kugou.com/stdmusic/240/x.jpg"


def test_normalize_reads_trans_param_from_json_string():
    import json as _json
    payload = {"status": 1, "data": {"info": [{
        "hash": "H1", "songname": "a", "singername": "b",
        "trans_param": _json.dumps(
            {"union_cover": "http://imge.kugou.com/stdmusic/{size}/y.jpg"}),
    }]}}
    client, _ = _client([json_response(payload)])
    song = asyncio.run(client.search("x", 1))[0]
    assert song["cover"] == "https://imge.kugou.com/stdmusic/240/y.jpg"


def test_normalize_missing_album_and_cover_are_empty():
    payload = {"status": 1, "data": {"info": [{"FileHash": "AA", "SongName": "x"}]}}
    client, _ = _client([json_response(payload)])
    song = asyncio.run(client.search("x", 1))[0]
    assert song["album"] == "" and song["cover"] == ""


def test_ladder_hands_the_song_to_the_fetcher():
    """打标签需要歌曲元信息，因此 ladder 必须把 song 透传给 fetch。"""
    fetcher = _FakeFetcher(results={"flac": _fetch_result("flac")})
    ladder, _ = _ladder(["flac"], [_option("flac")], fetcher)

    asyncio.run(ladder.run(_SONG_LADDER))

    assert fetcher.songs == [_SONG_LADDER]


def test_normalize_reads_mixsongid_as_album_audio_id():
    """概念版搜索把结果放在 data.lists，且 id 只叫 MixSongID/ID，
    没有 album_audio_id —— 不认就会退化成 entity_id=0 的空请求。"""
    payload = {"status": 1, "data": {"lists": [{
        "hash": "H1", "songname": "晴天", "singername": "周杰伦",
        "MixSongID": "32100650",
    }]}}
    client, _ = _client([json_response(payload)])
    song = asyncio.run(client.search("x", 1))[0]
    assert song["album_audio_id"] == "32100650"


def test_normalize_falls_back_to_id_when_mixsongid_absent():
    payload = {"status": 1, "data": {"lists": [{
        "hash": "H1", "songname": "晴天", "singername": "周杰伦", "ID": "32100650",
    }]}}
    client, _ = _client([json_response(payload)])
    song = asyncio.run(client.search("x", 1))[0]
    assert song["album_audio_id"] == "32100650"
