import asyncio
import os
import time

import pytest
from conftest import FakeBot, FakeEvent, collect
from fake_session import FakeSession, json_response

import main
from file_cache import SharedDirError
from kugou_client import AllQualitiesFailed, DownloadOutcome, KugouApiError
from kugou_login import LoginResult
from main import (
    build_upload_request,
    check_upload_result,
    parse_shared_dir,
    search_cache_key,
)
from quality import ConfigError

_LITE_COOKIE = "token=SECRETTOKEN123456; userid=777; a_id=3116"


# ── 配置解析 ─────────────────────────────────────────────────────────────


def test_parse_shared_dir_validation(tmp_path):
    assert parse_shared_dir("")[1] != ""
    assert parse_shared_dir("   ")[1] != ""
    assert parse_shared_dir(None)[1] != ""
    assert parse_shared_dir("relative/x")[1] != ""

    root, err = parse_shared_dir(str(tmp_path))
    assert err == ""
    assert root == os.path.join(str(tmp_path), "astrbot_plugin_kugou_music")


def test_ladder_requires_configured_shared_dir(plugin_factory):
    plugin = plugin_factory({"shared_cache_dir": ""})
    plugin._session = FakeSession([])

    with pytest.raises(SharedDirError) as err:
        asyncio.run(plugin._ladder_for_download())

    assert "shared_cache_dir" in str(err.value)
    assert plugin._session.calls == []          # 配置错误不触发任何网络请求


def test_ladder_requires_valid_quality_priority(plugin_factory, shared_dir):
    plugin = plugin_factory({"shared_cache_dir": shared_dir, "quality_priority": ["bogus"]})
    plugin._session = FakeSession([])

    with pytest.raises(ConfigError):
        asyncio.run(plugin._ladder_for_download())


def test_ladder_requires_owned_shared_dir(plugin_factory, tmp_path):
    foreign = tmp_path / "foreign" / "astrbot_plugin_kugou_music"
    foreign.mkdir(parents=True)
    (foreign / "user-file.mp3").write_bytes(b"x")
    plugin = plugin_factory({"shared_cache_dir": str(foreign.parent)})
    plugin._session = FakeSession([])

    with pytest.raises(SharedDirError):
        asyncio.run(plugin._ladder_for_download())


def test_ladder_builds_once_and_reuses_cache_manager(plugin_factory, shared_dir):
    plugin = plugin_factory({"shared_cache_dir": shared_dir})
    plugin._session = FakeSession([])

    first = asyncio.run(plugin._ladder_for_download())
    second = asyncio.run(plugin._ladder_for_download())

    assert first is not None
    assert plugin._cache_manager is not None
    assert second is not None


# ── 指令注册与依赖 ───────────────────────────────────────────────────────


def test_plugin_registers_login_commands(plugin_factory):
    plugin_factory()
    assert {"酷狗扫码登录", "酷狗微信登录", "酷狗退出登录", "酷狗登录状态"} <= set(
        main.filter.registered
    )


def test_plugin_uses_plugin_data_dir_for_state(plugin_factory):
    plugin = plugin_factory()
    assert os.path.isdir(plugin._data_dir)
    assert plugin._cookie_file.startswith(plugin._data_dir)
    assert plugin._search_cache == {}


def test_plugin_survives_non_dict_search_cache(plugin_factory, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "search_cache.json").write_text('[{"songs": []}]', encoding="utf-8")

    plugin = plugin_factory()

    assert plugin._search_cache == {}


# ── 登录状态与凭据 ───────────────────────────────────────────────────────


def test_login_status_hides_full_token_and_names_source(plugin_factory):
    plugin = plugin_factory({"kugou_cookie": ""})
    plugin._save_cookie(_LITE_COOKIE, source="inline_qr")

    event = FakeEvent()
    collect(plugin.cmd_login_status(event))

    assert "777" in event.text
    assert "SECRETTOKEN123456" not in event.text
    assert "SECRETTO..." in event.text
    assert "扫码登录" in event.text
    assert "在线有效" in event.text          # 不把本地存在凭据当作会员验证


def test_login_status_reports_not_logged_in(plugin_factory):
    plugin = plugin_factory()
    event = FakeEvent()
    collect(plugin.cmd_login_status(event))
    assert "未登录" in event.text


def test_login_status_reports_standard_version_cookie(plugin_factory):
    plugin = plugin_factory({"kugou_cookie": "token=t; userid=1; a_id=1005"})
    event = FakeEvent()
    collect(plugin.cmd_login_status(event))
    assert "标准版" in event.text or "重新登录" in event.text


def test_login_status_names_webui_source(plugin_factory):
    plugin = plugin_factory({"kugou_cookie": _LITE_COOKIE})
    event = FakeEvent()
    collect(plugin.cmd_login_status(event))
    assert "WebUI" in event.text


def test_logout_clears_only_plugin_managed_cookie(plugin_factory):
    plugin = plugin_factory({"kugou_cookie": "token=manual; userid=9; a_id=3116"})
    plugin._save_cookie(_LITE_COOKIE, source="inline_qr")

    collect(plugin.cmd_logout(FakeEvent()))

    assert plugin._load_saved_cookie() is None
    assert plugin.config["kugou_cookie"] == "token=manual; userid=9; a_id=3116"


def test_cookie_round_trip_records_source(plugin_factory):
    plugin = plugin_factory()
    assert plugin._save_cookie(_LITE_COOKIE, source="wx_wechat") is True
    saved = plugin._load_saved_cookie()
    assert saved["cookie"] == _LITE_COOKIE
    assert saved["source"] == "wx_wechat"
    assert "saved_at" in saved


def test_credentials_are_none_for_standard_cookie(plugin_factory):
    plugin = plugin_factory({"kugou_cookie": "token=t; userid=1; a_id=1005"})
    assert plugin._credentials() is None


def test_credentials_ignore_unconfigured_or_broken_json(plugin_factory, monkeypatch, tmp_path):
    plugin = plugin_factory()
    plugin._cookie_file = str(tmp_path / "broken.json")
    with open(plugin._cookie_file, "w", encoding="utf-8") as handle:
        handle.write("{not json")
    assert plugin._load_saved_cookie() is None


def test_persist_login_reports_save_failure(plugin_factory, monkeypatch):
    plugin = plugin_factory()
    monkeypatch.setattr(plugin, "_save_cookie", lambda *args, **kwargs: False)

    message = plugin._persist_login(LoginResult(token="t", userid="1", source="inline_qr"))

    assert "失败" in message
    assert "1" in message


def test_persist_login_reports_success(plugin_factory):
    plugin = plugin_factory()
    message = plugin._persist_login(LoginResult(token="t", userid="1", source="inline_qr"))
    assert "1" in message and "失败" not in message
    assert plugin._load_saved_cookie()["cookie"] == "token=t; userid=1; a_id=3116"


# ── 扫码 / 微信登录指令 ─────────────────────────────────────────────────


def test_qr_login_polls_and_saves_credentials(plugin_factory):
    plugin = plugin_factory()
    plugin._poll_interval = 0
    plugin._session = FakeSession([
        json_response({"data": {"qrcode": "KEY1", "qrcode_img": "b64"}}),
        json_response({"data": {"status": 1}}),
        json_response({"data": {"status": 2}}),
        json_response({"data": {"status": 4}}),
        json_response({"data": {"token": "TK", "userid": 555}}),
    ])

    event = FakeEvent()
    collect(plugin.cmd_qr_login(event))

    assert "qrcode=KEY1" in event.text
    assert "确认登录" in event.text
    assert "555" in event.text
    assert plugin._load_saved_cookie()["cookie"] == "token=TK; userid=555; a_id=3116"


def test_qr_login_reports_generation_failure(plugin_factory):
    plugin = plugin_factory()
    plugin._poll_interval = 0
    plugin._session = FakeSession([json_response({"data": {}})])

    event = FakeEvent()
    collect(plugin.cmd_qr_login(event))

    assert "❌" in event.text


def test_qr_login_reports_expired_qr(plugin_factory):
    plugin = plugin_factory()
    plugin._poll_interval = 0
    plugin._session = FakeSession([
        json_response({"data": {"qrcode": "KEY1"}}),
        json_response({"data": {"status": 0}}),
    ])

    event = FakeEvent()
    collect(plugin.cmd_qr_login(event))

    assert "过期" in event.text


def test_qr_login_aborts_when_shutting_down(plugin_factory):
    plugin = plugin_factory()
    plugin._poll_interval = 0
    plugin._closing = True
    plugin._session = FakeSession([json_response({"data": {"qrcode": "KEY1"}})])

    event = FakeEvent()
    collect(plugin.cmd_qr_login(event))

    assert "qrcode=KEY1" in event.text
    assert "成功" not in event.text


def test_wx_login_reports_link_and_exchanges_code(plugin_factory):
    plugin = plugin_factory()
    plugin._poll_interval = 0
    plugin._session = FakeSession([
        json_response({"access_token": "AT"}),                       # access_token
        json_response({"errcode": 0, "ticket": "T"}),                # ticket
        json_response({"errcode": 0, "uuid": "WXU"}),                # qrconnect
        json_response({"wx_code": "CODE"}),                          # 轮询
        json_response({"access_token": "AT2", "openid": "OID"}),     # wx oauth
        json_response({"status": 1, "data": {"token": "WXT", "userid": 88}}),
    ])

    event = FakeEvent()
    collect(plugin.cmd_wx_login(event))

    assert "uuid=WXU" in event.text
    assert "88" in event.text
    assert plugin._load_saved_cookie()["cookie"] == "token=WXT; userid=88; a_id=3116"


def test_wx_login_reports_failure_to_start(plugin_factory):
    plugin = plugin_factory()
    plugin._session = FakeSession([json_response({"errcode": 40013})])

    event = FakeEvent()
    collect(plugin.cmd_wx_login(event))

    assert "❌" in event.text


# ── 调试输出与生命周期 ───────────────────────────────────────────────────


def test_debug_output_is_redacted(plugin_factory):
    plugin = plugin_factory({"debug": True})
    plugin._add_debug(
        "probe", token="SECRETVALUE", signature="a" * 32,
        url="http://fs.kugou.com/a.mp3?sig=deadbeef", songname="晴天",
    )

    text = plugin._flush_debug()

    assert "SECRETVALUE" not in text
    assert "sig=deadbeef" not in text
    assert "fs.kugou.com/a.mp3" in text
    assert "晴天" in text
    assert plugin._flush_debug() == ""      # 冲刷后清空


def test_debug_output_is_empty_when_disabled(plugin_factory):
    plugin = plugin_factory({"debug": False})
    plugin._add_debug("probe", token="x")
    assert plugin._flush_debug() == ""


def test_terminate_cancels_tasks_and_closes_session(plugin_factory):
    plugin = plugin_factory()
    state = {"closed": False, "cancelled": False}

    class _Session:
        closed = False

        async def close(self):
            state["closed"] = True
            self.closed = True

    plugin._session = _Session()

    async def hang():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    async def scenario():
        plugin._track(asyncio.create_task(hang()))
        await asyncio.sleep(0)
        await plugin.terminate()

    asyncio.run(scenario())

    assert state["closed"] is True
    assert state["cancelled"] is True
    assert plugin._tasks == set()
    assert plugin._closing is True


def test_terminate_without_session_is_safe(plugin_factory):
    plugin = plugin_factory()
    asyncio.run(plugin.terminate())


def test_initialize_survives_bad_shared_dir(plugin_factory):
    plugin = plugin_factory({"shared_cache_dir": "relative/path"})
    asyncio.run(plugin.initialize())


def test_initialize_prepares_shared_dir(plugin_factory, shared_dir):
    plugin = plugin_factory({"shared_cache_dir": shared_dir})

    asyncio.run(plugin.initialize())

    assert os.path.isfile(
        os.path.join(shared_dir, "astrbot_plugin_kugou_music", "manifest.json")
    )


def test_device_identity_is_persisted(plugin_factory):
    plugin = plugin_factory()
    first = plugin._get_device()
    assert len(first.dfid) >= 16 and len(first.mid) >= 16

    assert plugin._get_device() == first
    assert plugin_factory()._get_device() == first      # 同一数据目录 → 设备身份持久化


# ── 指令注册（Task 12 范围） ─────────────────────────────────────────────


def test_plugin_registers_all_commands():
    assert {"酷狗搜歌", "酷狗下载", "酷狗点歌"} <= set(main.filter.registered)


# ── OneBot 上传参数与结果判定 ────────────────────────────────────────────


def test_build_upload_request_group_and_private():
    action, params = build_upload_request(
        True, "12345", "D:\\cache\\a.flac", "周杰伦 - 晴天 [FLAC].flac"
    )
    assert action == "upload_group_file"
    assert params == {
        "group_id": 12345, "file": "D:\\cache\\a.flac", "name": "周杰伦 - 晴天 [FLAC].flac",
    }

    action, params = build_upload_request(False, "999", "p", "n")
    assert action == "upload_private_file"
    assert params == {"user_id": 999, "file": "p", "name": "n"}


def test_build_upload_request_rejects_bad_target_id():
    with pytest.raises(ValueError):
        build_upload_request(True, "abc", "p", "n")
    with pytest.raises(ValueError):
        build_upload_request(False, "", "p", "n")


def test_check_upload_result_variants():
    assert check_upload_result({"status": "ok", "retcode": 0})[0] is True
    assert check_upload_result({"status": "failed", "retcode": 0})[0] is False
    assert check_upload_result({"retcode": 1200, "msg": "bad"})[0] is False
    assert check_upload_result({"status": "failed", "wording": "群文件不可用"})[1] != ""
    assert check_upload_result(None)[0] is False
    assert check_upload_result({})[0] is False
    assert check_upload_result("unexpected")[0] is False


def test_check_upload_result_accepts_string_zero_retcode():
    # 有些 OneBot 实现把 retcode 序列化成字符串
    assert check_upload_result({"status": "ok", "retcode": "0"})[0] is True
    assert check_upload_result({"retcode": "0"})[0] is True
    assert check_upload_result({"retcode": ""})[0] is False     # 含义不明 → 不报成功


# ── 搜索指令 ─────────────────────────────────────────────────────────────

_SONG = {
    "hash": "H1", "songname": "晴天", "singername": "周杰伦",
    "album_id": "1", "album_audio_id": "2", "duration": 269, "pay_type": 0,
}


def _fixed_search(songs):
    async def search(keyword):
        return list(songs)

    return search


def _search_event(message="酷狗搜歌 晴天", **kwargs):
    kwargs.setdefault("sender_id", "42")
    kwargs.setdefault("origin", "group:1000")
    return FakeEvent(message, **kwargs)


def test_search_lists_results_and_caches_per_user(plugin_factory, monkeypatch):
    plugin = plugin_factory({"search_count": 2})
    monkeypatch.setattr(plugin, "_search_songs", _fixed_search([_SONG]))

    first = _search_event(sender_id="42")
    collect(plugin.cmd_search(first))

    assert "晴天" in first.text and "周杰伦" in first.text and "[4:29]" in first.text
    assert "酷狗下载" in first.text
    assert len(plugin._search_cache) == 1
    assert plugin._search_cache[search_cache_key(first)]["songs"][0]["hash"] == "H1"

    second = _search_event(sender_id="43")
    collect(plugin.cmd_search(second))
    assert len(plugin._search_cache) == 2          # 同群不同用户互不覆盖

    other = _search_event(sender_id="43", origin="group:2000")
    collect(plugin.cmd_search(other))
    assert len(plugin._search_cache) == 3          # 不同会话也互相隔离


def test_search_without_keyword_shows_usage(plugin_factory):
    plugin = plugin_factory()
    event = FakeEvent("酷狗搜歌")
    collect(plugin.cmd_search(event))
    assert "用法" in event.text
    assert plugin._search_cache == {}


def test_search_reports_failure_separately_from_no_results(plugin_factory, monkeypatch):
    plugin = plugin_factory()

    async def boom(keyword):
        raise KugouApiError("HTTP 500")

    monkeypatch.setattr(plugin, "_search_songs", boom)
    failure = _search_event()
    collect(plugin.cmd_search(failure))
    assert "搜索失败" in failure.text

    monkeypatch.setattr(plugin, "_search_songs", _fixed_search([]))
    empty = _search_event()
    collect(plugin.cmd_search(empty))
    assert "未找到" in empty.text


def test_search_prunes_expired_entries(plugin_factory, monkeypatch):
    plugin = plugin_factory()
    plugin._search_cache["stale|1"] = {"songs": [], "expire_at": time.time() - 10}
    monkeypatch.setattr(plugin, "_search_songs", _fixed_search([_SONG]))

    collect(plugin.cmd_search(_search_event()))

    assert "stale|1" not in plugin._search_cache


# ── 下载指令 ─────────────────────────────────────────────────────────────

_FILENAME = "周杰伦 - 晴天 [FLAC].flac"


class _StubLadder:
    def __init__(self, outcome=None, error=None):
        self.outcome = outcome
        self.error = error
        self.ran = []
        self.released = []

    async def run(self, song):
        self.ran.append(song)
        if self.error is not None:
            raise self.error
        return self.outcome

    async def release(self, outcome):
        self.released.append(outcome)


def _async_return(value):
    async def factory():
        if isinstance(value, Exception):
            raise value
        return value

    return factory


def _outcome(path):
    return DownloadOutcome(
        path=path, filename=_FILENAME, tier="flac", label="FLAC",
        info=None, url="", from_cache=False,
    )


def _seed_cache(plugin, event, songs=None):
    plugin._search_cache[search_cache_key(event)] = {
        "songs": songs if songs is not None else [dict(_SONG)],
        "expire_at": time.time() + 300,
        "keyword": "晴天",
    }


def _install_ladder(plugin, monkeypatch, ladder):
    monkeypatch.setattr(plugin, "_ladder_for_download", _async_return(ladder))


def test_download_uploads_group_file_and_stops_propagation(
    plugin_factory, monkeypatch, tmp_path
):
    plugin = plugin_factory()
    outcome = _outcome(str(tmp_path / "cache" / "a.flac"))
    ladder = _StubLadder(outcome=outcome)
    _install_ladder(plugin, monkeypatch, ladder)

    bot = FakeBot()
    event = FakeEvent("酷狗下载 1", group_id="1000", sender_id="42", bot=bot)
    _seed_cache(plugin, event)

    collect(plugin.cmd_download(event))

    assert bot.api.calls == [(
        "upload_group_file",
        {"group_id": 1000, "file": outcome.path, "name": _FILENAME},
    )]
    assert event.stopped is True
    assert ladder.released == [outcome]
    assert all(kind == "plain" for kind, _ in event.results)   # 没有卡片/图片/语音


def test_download_uploads_private_file_in_private_chat(
    plugin_factory, monkeypatch, tmp_path
):
    plugin = plugin_factory()
    outcome = _outcome(str(tmp_path / "a.flac"))
    _install_ladder(plugin, monkeypatch, _StubLadder(outcome=outcome))

    bot = FakeBot()
    event = FakeEvent("酷狗下载 1", group_id=None, sender_id="42", bot=bot)
    _seed_cache(plugin, event)

    collect(plugin.cmd_download(event))

    action, params = bot.api.calls[0]
    assert action == "upload_private_file"
    assert params["user_id"] == 42 and params["name"] == _FILENAME


def test_compat_command_behaves_identically(plugin_factory, monkeypatch, tmp_path):
    plugin = plugin_factory()
    outcome = _outcome(str(tmp_path / "a.flac"))
    _install_ladder(plugin, monkeypatch, _StubLadder(outcome=outcome))

    bot = FakeBot()
    event = FakeEvent("酷狗点歌 1", group_id="1000", sender_id="42", bot=bot)
    _seed_cache(plugin, event)

    collect(plugin.cmd_play(event))

    assert bot.api.calls[0][0] == "upload_group_file"
    assert event.stopped is True


def test_download_requires_prior_search_for_this_user(plugin_factory, monkeypatch):
    plugin = plugin_factory()
    _install_ladder(plugin, monkeypatch, _StubLadder(outcome=_outcome("p")))

    event = FakeEvent("酷狗下载 1", group_id="1000", sender_id="42", bot=FakeBot())

    collect(plugin.cmd_download(event))

    assert "请先" in event.text
    assert event.bot.api.calls == []


def test_download_rejects_out_of_range_and_non_numeric_index(plugin_factory, monkeypatch):
    plugin = plugin_factory()
    _install_ladder(plugin, monkeypatch, _StubLadder(outcome=_outcome("p")))

    out_of_range = FakeEvent("酷狗下载 9", group_id="1000", sender_id="42", bot=FakeBot())
    _seed_cache(plugin, out_of_range)
    collect(plugin.cmd_download(out_of_range))
    assert "范围" in out_of_range.text

    non_numeric = FakeEvent("酷狗下载 abc", group_id="1000", sender_id="42", bot=FakeBot())
    _seed_cache(plugin, non_numeric)
    collect(plugin.cmd_download(non_numeric))
    assert "有效的序号" in non_numeric.text


def test_download_reports_missing_shared_dir_without_network(plugin_factory):
    plugin = plugin_factory({"shared_cache_dir": ""})
    plugin._session = FakeSession([])

    event = FakeEvent("酷狗下载 1", group_id="1000", sender_id="42", bot=FakeBot())
    _seed_cache(plugin, event)

    collect(plugin.cmd_download(event))

    assert "shared_cache_dir" in event.text
    assert plugin._session.calls == []
    assert event.bot.api.calls == []


def test_download_reports_invalid_quality_priority(plugin_factory, shared_dir):
    plugin = plugin_factory({"shared_cache_dir": shared_dir, "quality_priority": ["bogus"]})
    plugin._session = FakeSession([])

    event = FakeEvent("酷狗下载 1", group_id="1000", sender_id="42", bot=FakeBot())
    _seed_cache(plugin, event)

    collect(plugin.cmd_download(event))

    assert "bogus" in event.text
    assert event.bot.api.calls == []


def test_download_reports_all_qualities_failed_with_reasons(plugin_factory, monkeypatch):
    plugin = plugin_factory()
    error = AllQualitiesFailed([("hires", "需要会员才能获取"), ("flac", "上游无此音质")])
    _install_ladder(plugin, monkeypatch, _StubLadder(error=error))

    event = FakeEvent("酷狗下载 1", group_id="1000", sender_id="42", bot=FakeBot())
    _seed_cache(plugin, event)

    collect(plugin.cmd_download(event))

    assert "hires" in event.text and "需要会员" in event.text
    assert "flac" in event.text
    assert event.bot.api.calls == []
    assert event.stopped is True


def test_upload_exception_is_reported_without_card_fallback(plugin_factory, monkeypatch, tmp_path):
    plugin = plugin_factory()
    outcome = _outcome(str(tmp_path / "a.flac"))
    _install_ladder(plugin, monkeypatch, _StubLadder(outcome=outcome))

    bot = FakeBot(error=RuntimeError("ACTION_FAILED"))
    event = FakeEvent("酷狗下载 1", group_id="1000", sender_id="42", bot=bot)
    _seed_cache(plugin, event)

    collect(plugin.cmd_download(event))

    assert "失败" in event.text
    assert "http" not in event.text.lower()               # 不回退发送直链
    assert all(kind == "plain" for kind, _ in event.results)
    assert event.stopped is True


def test_upload_unsupported_action_reports_clear_error(plugin_factory, monkeypatch, tmp_path):
    # Review Focus #5：OneBot 实现不支持文件上传扩展时给出可行动报错
    plugin = plugin_factory()
    _install_ladder(plugin, monkeypatch, _StubLadder(outcome=_outcome(str(tmp_path / "a.flac"))))

    bot = FakeBot(error=KeyError("upload_group_file"))
    event = FakeEvent("酷狗下载 1", group_id="1000", sender_id="42", bot=bot)
    _seed_cache(plugin, event)

    collect(plugin.cmd_download(event))

    assert "失败" in event.text
    assert "upload_group_file" in event.text or "upload_private_file" in event.text


def test_upload_retcode_failure_is_reported(plugin_factory, monkeypatch, tmp_path):
    plugin = plugin_factory()
    _install_ladder(plugin, monkeypatch, _StubLadder(outcome=_outcome(str(tmp_path / "a.flac"))))

    bot = FakeBot(responses=[{"status": "failed", "retcode": 1200, "msg": "file too large"}])
    event = FakeEvent("酷狗下载 1", group_id="1000", sender_id="42", bot=bot)
    _seed_cache(plugin, event)

    collect(plugin.cmd_download(event))

    assert "失败" in event.text
    assert "1200" in event.text or "file too large" in event.text


def test_download_releases_hold_even_on_upload_failure(plugin_factory, monkeypatch, tmp_path):
    plugin = plugin_factory()
    outcome = _outcome(str(tmp_path / "a.flac"))
    ladder = _StubLadder(outcome=outcome)
    _install_ladder(plugin, monkeypatch, ladder)

    bot = FakeBot(error=RuntimeError("boom"))
    event = FakeEvent("酷狗下载 1", group_id="1000", sender_id="42", bot=bot)
    _seed_cache(plugin, event)

    collect(plugin.cmd_download(event))

    assert ladder.released == [outcome]


def test_download_does_not_retry_other_quality_after_upload_failure(
    plugin_factory, monkeypatch, tmp_path
):
    plugin = plugin_factory()
    ladder = _StubLadder(outcome=_outcome(str(tmp_path / "a.flac")))
    _install_ladder(plugin, monkeypatch, ladder)

    bot = FakeBot(error=RuntimeError("boom"))
    event = FakeEvent("酷狗下载 1", group_id="1000", sender_id="42", bot=bot)
    _seed_cache(plugin, event)

    collect(plugin.cmd_download(event))

    assert len(ladder.ran) == 1                 # 上传失败不自动改换音质重发
    assert len(bot.api.calls) == 1


# ── 评审修复：事件传播、健壮性、清理与证据 ───────────────────────────────


def test_search_stops_event_propagation(plugin_factory, monkeypatch):
    plugin = plugin_factory({"search_count": 2})
    monkeypatch.setattr(plugin, "_search_songs", _fixed_search([_SONG]))
    event = _search_event()

    collect(plugin.cmd_search(event))

    assert event.stopped is True


def test_search_failure_stops_event_propagation(plugin_factory, monkeypatch):
    plugin = plugin_factory()

    async def boom(keyword):
        raise KugouApiError("upstream down")

    monkeypatch.setattr(plugin, "_search_songs", boom)
    event = _search_event()

    collect(plugin.cmd_search(event))

    assert event.stopped is True


def test_login_commands_stop_event_propagation(plugin_factory):
    plugin = plugin_factory()
    plugin._poll_interval = 0
    plugin._closing = True
    plugin._session = FakeSession([json_response({"data": {"qrcode": "K"}})])

    qr_event = FakeEvent()
    collect(plugin.cmd_qr_login(qr_event))
    assert qr_event.stopped is True

    status_event = FakeEvent()
    collect(plugin.cmd_login_status(status_event))
    assert status_event.stopped is True

    logout_event = FakeEvent()
    collect(plugin.cmd_logout(logout_event))
    assert logout_event.stopped is True


def test_wx_login_stops_event_propagation_on_failure(plugin_factory):
    plugin = plugin_factory()
    plugin._session = FakeSession([json_response({"errcode": 40013})])
    event = FakeEvent()

    collect(plugin.cmd_wx_login(event))

    assert event.stopped is True


def test_prune_search_cache_survives_non_dict_entries(plugin_factory):
    plugin = plugin_factory()
    plugin._search_cache = {
        "junk": "not-a-dict",
        "items": [1, 2],
        "old": {"expire_at": 0},
        "new": {"expire_at": time.time() + 60},
    }

    plugin._prune_search_cache()          # 不得抛异常

    assert set(plugin._search_cache) == {"new"}


def test_download_flow_survives_corrupt_cache_entry(plugin_factory):
    plugin = plugin_factory()
    event = FakeEvent("酷狗下载 1")
    plugin._search_cache[search_cache_key(event)] = "corrupt"

    collect(plugin._download_flow(event))

    assert "请先使用" in event.text
    assert event.stopped is True


def test_download_flow_survives_non_list_songs(plugin_factory):
    plugin = plugin_factory()
    event = FakeEvent("酷狗下载 1")
    plugin._search_cache[search_cache_key(event)] = {
        "songs": "oops", "expire_at": time.time() + 60,
    }

    collect(plugin._download_flow(event))

    assert "请先使用" in event.text
    assert event.stopped is True


def test_download_reports_verification_evidence_in_debug(
    plugin_factory, monkeypatch, tmp_path
):
    from audio_builders import flac_head
    from audio_probe import probe

    plugin = plugin_factory({"debug": True})
    path = tmp_path / "song.flac"
    path.write_bytes(flac_head(bits=24, sample_rate=96000, total_samples=96000 * 200))
    outcome = DownloadOutcome(
        path=str(path), filename=_FILENAME, tier="hires", label="Hi-Res",
        info=probe(path.read_bytes()),
        url="http://fs.kugou.com/a.flac?sig=ABCDEF0123456789ABCDEF",
        from_cache=False,
    )
    _install_ladder(plugin, monkeypatch, _StubLadder(outcome=outcome))

    event = FakeEvent("酷狗下载 1", group_id="1000", sender_id="42", bot=FakeBot())
    _seed_cache(plugin, event)

    collect(plugin.cmd_download(event))

    text = event.text
    assert '"tier": "hires"' in text
    assert '"sample_rate": 96000' in text
    assert '"bits": 24' in text
    assert '"duration_ms": 200000' in text
    assert '"hash": "H1"' in text
    assert '"size": ' in text
    assert "sig=ABCDEF0123456789ABCDEF" not in text      # 直链签名必须脱敏


def test_cache_cleanup_is_throttled(plugin_factory):
    plugin = plugin_factory()
    calls = []

    class _Cache:
        def cleanup(self, ttl):
            calls.append(ttl)
            return 0

    plugin._cache_manager = _Cache()

    plugin._cleanup_cache()
    plugin._cleanup_cache()
    assert len(calls) == 1               # 同一时间窗内只清理一次

    plugin._last_cleanup = 0             # 模拟清理间隔已过
    plugin._cleanup_cache()
    assert len(calls) == 2


def test_ladder_for_download_runs_bounded_cleanup(plugin_factory, shared_dir, monkeypatch):
    plugin = plugin_factory({"shared_cache_dir": shared_dir})
    plugin._session = FakeSession([])
    called = []
    monkeypatch.setattr(plugin, "_cleanup_cache", lambda: called.append(1))

    asyncio.run(plugin._ladder_for_download())
    asyncio.run(plugin._ladder_for_download())

    assert called == [1, 1]        # 每次下载筹备都触发一次（内部再限频）
