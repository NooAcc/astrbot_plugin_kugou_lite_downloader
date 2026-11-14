"""metadata.py 的协议调用与归一化测试（离线，FakeSession）。"""

import asyncio
import base64
import json

import pytest

import kugou_client as kc
import metadata
from fake_session import FakeSession, json_response

_SONG = {"hash": "ABCDEF", "album_audio_id": 32100650, "duration": 269,
         "songname": "晴天", "singername": "周杰伦", "album": "叶惠美"}


def _client(session):
    return kc.KugouClient(session, kc.Device(dfid="d", mid="m", uuid="u"))


# ── parse_krm_entry ─────────────────────────────────────────────────────


def test_parse_krm_entry_maps_all_fields():
    entry = {
        "base": {"songname": "晴天", "author_name": "周杰伦", "album_name": "叶惠美",
                 "publish_date": "2003-07-31"},
        "album_info": {"cover": "http://imge.kugou.com/stdmusic/{size}/x.jpg"},
        "authors": [{"base": {"author_name": "周杰伦"}}],
        "extra": {"composer": "周杰伦", "lyrics": "方文山", "bpm": 68, "disc": 1},
        "tags": [{"name": "流行"}, {"name": "老年版精选"}],
    }
    meta = metadata.parse_krm_entry(entry)
    assert meta.title == "晴天"
    assert meta.artists == ("周杰伦",)
    assert meta.album == "叶惠美"
    assert meta.album_artist == "周杰伦"
    assert meta.composer == "周杰伦"
    assert meta.lyricist == "方文山"
    assert meta.date == "2003-07-31"
    assert meta.bpm == "68" and meta.disc == "1"
    assert meta.cover_url.startswith("https://imge.kugou.com/")
    assert "{size}" not in meta.cover_url
    assert meta.genre == "流行"          # 运营标签必须被筛掉


def test_parse_krm_entry_tolerates_missing_sections():
    meta = metadata.parse_krm_entry({"base": {"songname": "x"}})
    assert meta.title == "x"
    assert meta.artists == () and meta.genre == "" and meta.cover_url == ""


def test_parse_krm_entry_keeps_multiple_authors_in_order():
    entry = {"authors": [{"base": {"author_name": "周杰伦"}},
                         {"base": {"author_name": "方文山"}},
                         {"base": {"author_name": "周杰伦"}}]}
    assert metadata.parse_krm_entry(entry).artists == ("周杰伦", "方文山")


# ── parse_lyric_candidate ───────────────────────────────────────────────


def test_parse_lyric_candidate_returns_id_and_accesskey():
    payload = {"status": 200, "candidates": [{"id": "274944371", "accesskey": "KEY"}]}
    assert metadata.parse_lyric_candidate(payload) == ("274944371", "KEY")


def test_parse_lyric_candidate_none_when_status_404():
    # 无歌词：HTTP 200，但 body 里 status=404
    assert metadata.parse_lyric_candidate({"status": 404, "candidates": []}) is None
    assert metadata.parse_lyric_candidate(
        {"status": 404, "candidates": [{"id": "1", "accesskey": "k"}]}) is None


def test_parse_lyric_candidate_none_on_empty_body():
    # 漏签名的表现：HTTP 200 + 空 body → api_get_json 返回 {}
    assert metadata.parse_lyric_candidate({}) is None


# ── decode_lyric_content ────────────────────────────────────────────────


def test_decode_lyric_content_plain_lrc():
    payload = {"status": 200, "contenttype": 1,
               "content": base64.b64encode("[ti:晴天]\n[ar:周杰伦]\n".encode()).decode()}
    assert metadata.decode_lyric_content(payload).startswith("[ti:晴天]")


def test_decode_lyric_content_never_returns_krc():
    # contenttype=0 是加密 KRC；本次只取 lrc，遇到时必须返回空串而不是把密文塞进标签
    payload = {"status": 200, "contenttype": 0,
               "content": base64.b64encode(b"krc1\x00\x01").decode()}
    assert metadata.decode_lyric_content(payload) == ""


def test_decode_lyric_content_empty_on_missing_content():
    assert metadata.decode_lyric_content({"status": 200, "contenttype": 1}) == ""


# ── fetch_track_meta ────────────────────────────────────────────────────


def test_fetch_track_meta_calls_krm_with_expected_body():
    session = FakeSession([json_response({"status": 1, "data": [{"base": {"songname": "晴天"}}]})])
    meta = asyncio.run(metadata.fetch_track_meta(_client(session), _SONG))
    assert meta.title == "晴天"
    call = session.calls[0]
    assert call["url"] == kc.GATEWAY + "/kmr/v2/audio"
    assert call["kw"]["headers"]["x-router"] == "openapi.kugou.com"
    assert json.loads(call["kw"]["data"])["data"] == [{"entity_id": 32100650}]


def test_fetch_track_meta_raises_on_bad_status():
    session = FakeSession([json_response({"status": 0, "error_code": 20006})])
    with pytest.raises(kc.KugouApiError):
        asyncio.run(metadata.fetch_track_meta(_client(session), _SONG))


# ── fetch_lyric ─────────────────────────────────────────────────────────


def test_fetch_lyric_returns_empty_on_404_body():
    session = FakeSession([json_response({"status": 404, "candidates": []})])
    assert asyncio.run(metadata.fetch_lyric(_client(session), _SONG)) == ""


def test_fetch_lyric_returns_empty_on_empty_body():
    session = FakeSession([json_response(None)])       # 漏签名：空 body
    assert asyncio.run(metadata.fetch_lyric(_client(session), _SONG)) == ""


def test_fetch_lyric_degrades_when_accesskey_missing():
    # /download 缺 accesskey → status 400 / error_code 20010
    session = FakeSession([
        json_response({"status": 200, "candidates": [{"id": "1", "accesskey": "k"}]}),
        json_response({"status": 400, "error_code": 20010, "content": ""}),
    ])
    assert asyncio.run(metadata.fetch_lyric(_client(session), _SONG)) == ""


def test_fetch_lyric_happy_path():
    session = FakeSession([
        json_response({"status": 200, "candidates": [{"id": "1", "accesskey": "k"}]}),
        json_response({"status": 200, "contenttype": 1,
                       "content": base64.b64encode(b"[ti:x]\n").decode()}),
    ])
    assert asyncio.run(metadata.fetch_lyric(_client(session), _SONG)) == "[ti:x]\n"


def test_fetch_lyric_sends_id_and_accesskey_to_download():
    session = FakeSession([
        json_response({"status": 200, "candidates": [{"id": "274944371", "accesskey": "KEY"}]}),
        json_response({"status": 200, "contenttype": 1,
                       "content": base64.b64encode(b"[ti:x]\n").decode()}),
    ])
    asyncio.run(metadata.fetch_lyric(_client(session), _SONG))
    download = session.calls[1]
    assert download["url"] == metadata.LYRIC_DOWNLOAD_URL
    assert download["kw"]["params"]["id"] == "274944371"
    assert download["kw"]["params"]["accesskey"] == "KEY"
    assert download["kw"]["params"]["fmt"] == "lrc"


# ── collect_track_meta ──────────────────────────────────────────────────


def test_collect_falls_back_to_search_fields_when_krm_fails():
    session = FakeSession([Exception("boom")])       # /kmr/audio 连接失败
    meta = asyncio.run(metadata.collect_track_meta(_client(session), _SONG))
    assert meta.title == "晴天"
    assert meta.artists == ("周杰伦",)
    assert meta.album == "叶惠美"
    assert meta.lyric == ""


def test_collect_splits_multi_singer_search_field_on_fallback():
    song = dict(_SONG, singername="周杰伦、方文山")
    session = FakeSession([Exception("boom")])
    meta = asyncio.run(metadata.collect_track_meta(_client(session), song))
    assert meta.artists == ("周杰伦", "方文山")
    assert meta.album_artist == "周杰伦"


def test_collect_merges_lyric_on_success():
    session = FakeSession([
        json_response({"status": 1, "data": [{"base": {"songname": "晴天"}}]}),
        json_response({"status": 200, "candidates": [{"id": "1", "accesskey": "k"}]}),
        json_response({"status": 200, "contenttype": 1,
                       "content": base64.b64encode(b"[ti:x]\n").decode()}),
    ])
    meta = asyncio.run(metadata.collect_track_meta(_client(session), _SONG))
    assert meta.title == "晴天"
    assert meta.lyric == "[ti:x]\n"


def test_collect_never_raises_when_everything_fails():
    session = FakeSession([Exception("a"), Exception("b")])
    meta = asyncio.run(metadata.collect_track_meta(_client(session), _SONG))
    assert meta.title == "晴天"        # 起码还有搜索结果的兜底
    assert meta.lyric == ""


def test_fetch_lyric_logs_when_body_is_empty(caplog):
    """空 body 是「漏签名」的表现，必须留下日志，不能与「这首歌没歌词」混为一谈。"""
    import logging

    session = FakeSession([json_response(None)])
    with caplog.at_level(logging.WARNING, logger="astrbot"):
        assert asyncio.run(metadata.fetch_lyric(_client(session), _SONG)) == ""
    assert any("空" in record.getMessage() for record in caplog.records)


def test_fetch_lyric_does_not_warn_for_plain_404(caplog):
    """真正的「没有歌词」是正常情况，不该刷日志。"""
    import logging

    session = FakeSession([json_response({"status": 404, "candidates": []})])
    with caplog.at_level(logging.WARNING, logger="astrbot"):
        assert asyncio.run(metadata.fetch_lyric(_client(session), _SONG)) == ""
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_fetch_track_meta_raises_when_entry_has_no_usable_fields():
    """上游对无效 entity_id 回的是 status=1 + 空条目（实测 {"__status": 1}）。
    那不是成功：必须抛错，否则兜底永不触发，标签会整片空白。"""
    session = FakeSession([json_response({"status": 1, "data": [{"__status": 1}]})])
    with pytest.raises(kc.KugouApiError):
        asyncio.run(metadata.fetch_track_meta(_client(session), _SONG))


def test_collect_falls_back_to_search_fields_on_empty_krm_entry():
    session = FakeSession([json_response({"status": 1, "data": [{"__status": 1}]}),
                           json_response({"status": 404})])
    meta = asyncio.run(metadata.collect_track_meta(_client(session), _SONG))
    assert meta.title == "晴天"
    assert meta.artists == ("周杰伦",)
    assert meta.album == "叶惠美"
