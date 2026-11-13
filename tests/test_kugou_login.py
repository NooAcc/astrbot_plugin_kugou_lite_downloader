import asyncio
import hashlib
import json

import pytest
from fake_session import FakeSession, json_response

from kugou_client import Device
from kugou_login import (
    QR_EXPIRED,
    QR_TRANSIENT,
    LiteLogin,
    LoginError,
    aes_decrypt,
    aes_encrypt,
    build_cookie,
    cookie_source_label,
    extract_cookie_field,
    validate_cookie,
)

_DEVICE = Device(dfid="d" * 8, mid="m" * 8, uuid="u" * 8)


def _login(responses, temp_key=None):
    session = FakeSession(responses)
    kwargs = {}
    if temp_key is not None:
        kwargs["temp_key_factory"] = lambda: temp_key
    return LiteLogin(session, _DEVICE, ua="ua", timeout=5.0, **kwargs), session


# ── 扫码登录 ─────────────────────────────────────────────────────────────


def test_qr_start_uses_lite_android_channel():
    login, session = _login(
        [json_response({"data": {"qrcode": "KEY123", "qrcode_img": "b64img"}})]
    )

    qr = asyncio.run(login.qr_start())

    call = session.calls[0]
    assert call["url"] == "https://login-user.kugou.com/v2/qrcode"
    query = call["kw"]["params"]
    assert query["plat"] == "4"                       # 概念版走安卓渠道
    assert query["appid"] == 1001                     # /v2/qrcode 固定 appid
    assert query["clientver"] == 11440
    assert query["srcappid"] == 2919
    assert query["qrcode_txt"].startswith(
        "https://h5.kugou.com/apps/loginQRCode/html/index.html?appid=3116"
    )
    expected = {k: v for k, v in query.items() if k != "signature"}
    assert query["signature"] == _web_sign(expected)
    assert call["kw"]["headers"]["User-Agent"] == "ua"
    assert qr.key == "KEY123"
    assert "qrcode=KEY123" in qr.scan_url


def test_qr_start_raises_when_kugou_returns_no_key():
    login, _ = _login([json_response({"data": {}}), json_response({}, status=503)])
    with pytest.raises(LoginError) as err:
        asyncio.run(login.qr_start())
    assert "二维码" in str(err.value)


def test_qr_start_raises_on_transport_error():
    login, _ = _login([OSError("down"), OSError("down")])
    with pytest.raises(LoginError):
        asyncio.run(login.qr_start())


def test_qr_check_passes_status_through_and_maps_errors():
    login, _ = _login([
        json_response({"data": {"status": 4}}),
        json_response({}, status=500),
        json_response({"data": {}}),
        {"oops": 1},                      # 占位，实际用异常注入
    ])
    assert asyncio.run(login.qr_check("KEY")) == 4
    assert asyncio.run(login.qr_check("KEY")) == QR_TRANSIENT   # HTTP 错误 → 瞬时错误，可重试
    assert asyncio.run(login.qr_check("KEY")) == QR_EXPIRED     # 服务端明确回答但无状态


def test_qr_check_transport_error_is_transient():
    login, _ = _login([OSError("down"), OSError("down")])
    assert asyncio.run(login.qr_check("KEY")) == QR_TRANSIENT


def test_qr_check_uses_lite_params():
    login, session = _login([json_response({"data": {"status": 1}})])
    asyncio.run(login.qr_check("KEY"))
    query = session.calls[0]["kw"]["params"]
    assert query["appid"] == 3116 and query["plat"] == "4" and query["qrcode"] == "KEY"


def test_qr_credentials_returns_login_result():
    login, _ = _login([json_response({"data": {"token": "TK", "userid": 12345}})])

    result = asyncio.run(login.qr_credentials("KEY"))

    assert result.token == "TK"
    assert result.userid == "12345"
    assert result.source == "inline_qr"


def test_qr_credentials_raises_without_leaking_secrets():
    login, _ = _login([json_response({"data": {"token": "", "userid": ""}})])
    with pytest.raises(LoginError) as err:
        asyncio.run(login.qr_credentials("KEY"))
    assert "token" in str(err.value)


# ── 微信登录 ─────────────────────────────────────────────────────────────


def test_wx_start_chains_access_token_ticket_and_qrconnect():
    login, session = _login([
        json_response({"access_token": "AT"}),
        json_response({"errcode": 0, "ticket": "TICKET"}),
        json_response({"errcode": 0, "uuid": "WXUUID"}),
    ])

    started = asyncio.run(login.wx_start())

    assert "cgi-bin/token" in session.calls[0]["url"]
    assert session.calls[0]["kw"]["params"]["appid"] == "wx72b795aca60ad321"   # 概念版微信 appid
    assert "ticket/getticket" in session.calls[1]["url"]
    assert "qrconnect" in session.calls[2]["url"]
    assert session.calls[2]["kw"]["params"]["appid"] == "wx72b795aca60ad321"
    assert len(session.calls[2]["kw"]["params"]["signature"]) == 40    # sha1
    assert started.uuid == "WXUUID"
    assert started.auth_url == "https://open.weixin.qq.com/connect/confirm?uuid=WXUUID"


def test_wx_start_raises_when_access_token_missing():
    login, _ = _login([json_response({"errcode": 40013, "errmsg": "bad appid"})])
    with pytest.raises(LoginError) as err:
        asyncio.run(login.wx_start())
    assert "微信" in str(err.value)


def test_wx_start_raises_when_qrconnect_rejected():
    login, _ = _login([
        json_response({"access_token": "AT"}),
        json_response({"errcode": 0, "ticket": "T"}),
        json_response({"errcode": 40029}),
    ])
    with pytest.raises(LoginError):
        asyncio.run(login.wx_start())


def test_wx_poll_once_returns_code_or_empty():
    login, _ = _login([
        json_response({"wx_code": ""}),
        json_response({"wx_code": "CODE1"}),
        json_response({}, status=500),
    ])
    assert asyncio.run(login.wx_poll_once("U")) == ""
    assert asyncio.run(login.wx_poll_once("U")) == "CODE1"
    assert asyncio.run(login.wx_poll_once("U")) == ""


def test_wx_exchange_decrypts_secu_params_and_hides_access_token():
    temp_key = "0123456789abcdef"
    aes_key = hashlib.md5(temp_key.encode()).hexdigest()[:32]
    secu = aes_encrypt(json.dumps({"token": "TK", "userid": 42}), aes_key, aes_key[-16:])
    login, session = _login([
        json_response({"access_token": "AT-SECRET", "openid": "OPENID1"}),
        json_response({"status": 1, "data": {"secu_params": secu, "userid": 42}}),
    ], temp_key=temp_key)

    result = asyncio.run(login.wx_exchange("CODE1"))

    assert result.token == "TK" and result.userid == "42" and result.source == "wx_wechat"
    oauth_call = session.calls[0]
    assert oauth_call["url"] == "https://api.weixin.qq.com/sns/oauth2/access_token"
    assert oauth_call["kw"]["params"]["appid"] == "wx72b795aca60ad321"
    login_call = session.calls[1]
    assert login_call["url"] == "https://gateway.kugou.com/v6/login_by_openplat"
    assert login_call["kw"]["headers"]["x-router"] == "login.user.kugou.com"
    assert login_call["kw"]["headers"]["Content-Type"] == "application/json"

    # 上游 login_openplat.js：请求体只有这 10 个字段，设备标识走 query
    body_raw = login_call["kw"]["data"]
    assert isinstance(body_raw, str)                    # JSON 字符串：签名与发送的是同一份
    body = json.loads(body_raw)
    assert set(body) == {
        "dev", "force_login", "partnerid", "clienttime_ms",
        "t1", "t2", "t3", "openid", "params", "pk",
    }
    assert body["openid"] == "OPENID1"
    assert not ({"dfid", "mid", "uuid", "appid", "clientver", "clienttime", "signature"} & set(body))

    query = login_call["kw"]["params"]
    assert query["appid"] == 3116 and query["clientver"] == 11440 and query["srcappid"] == 2919
    assert query["dfid"] == _DEVICE.dfid and query["mid"] == _DEVICE.mid and query["uuid"] == _DEVICE.uuid
    assert login_call["kw"]["headers"]["dfid"] == _DEVICE.dfid
    assert login_call["kw"]["headers"]["mid"] == _DEVICE.mid
    unsigned = {k: v for k, v in query.items() if k != "signature"}
    assert query["signature"] == _android_sign(unsigned, body_raw)   # 签名覆盖 query + body

    assert "AT-SECRET" not in body_raw                  # access_token 必须加过密
    assert hashlib.md5(b"AT-SECRET").hexdigest() not in body_raw


def test_wx_exchange_falls_back_to_plain_token_when_secu_undecryptable():
    login, _ = _login([
        json_response({"access_token": "AT", "openid": "OID"}),
        json_response({"status": 1, "data": {"secu_params": "zzzz", "token": "RAW", "userid": 7}}),
    ], temp_key="0123456789abcdef")

    result = asyncio.run(login.wx_exchange("CODE"))

    assert result.token == "RAW" and result.userid == "7"


def test_wx_exchange_raises_when_kugou_rejects_login():
    login, _ = _login([
        json_response({"access_token": "AT", "openid": "OID"}),
        json_response({"status": 0, "error": "风控"}),
    ], temp_key="0123456789abcdef")

    with pytest.raises(LoginError) as err:
        asyncio.run(login.wx_exchange("CODE"))
    assert "AT" not in str(err.value)


def test_wx_exchange_raises_when_wechat_oauth_fails():
    login, _ = _login([json_response({"errcode": 40029, "errmsg": "invalid code"})],
                      temp_key="0123456789abcdef")
    with pytest.raises(LoginError):
        asyncio.run(login.wx_exchange("BAD"))


# ── Cookie 处理 ──────────────────────────────────────────────────────────


def test_build_cookie_uses_lite_appid():
    assert build_cookie("t", "1") == "token=t; userid=1; a_id=3116"


def test_validate_cookie_rejects_standard_version_cookie():
    ok, why = validate_cookie("token=t; userid=1; a_id=1005")
    assert ok is False
    assert "重新登录" in why


def test_validate_cookie_accepts_lite_and_bare_cookie():
    assert validate_cookie("token=t; userid=1; a_id=3116")[0] is True
    assert validate_cookie("token=t; userid=1")[0] is True      # 无版本字段不盲目判定


def test_validate_cookie_rejects_empty_and_incomplete():
    assert validate_cookie("")[0] is False
    assert validate_cookie("   ")[0] is False
    assert validate_cookie("userid=1; a_id=3116")[0] is False
    assert validate_cookie("token=t; a_id=3116")[0] is False


def test_validate_cookie_accepts_json_wrapped_cookie():
    ok, why = validate_cookie('{"cookie_string": "token=t; userid=1; a_id=3116"}')
    assert ok is True and why == ""


def test_extract_cookie_field():
    assert extract_cookie_field("token=abc; userid=7; a_id=3116", "token") == "abc"
    assert extract_cookie_field("token=abc; userid=7", "userid") == "7"
    assert extract_cookie_field("token=abc", "vip_token") == ""
    assert extract_cookie_field("", "token") == ""


def test_cookie_source_label_distinguishes_sources():
    assert cookie_source_label({"source": "inline_qr"}, "") == "扫码登录"
    assert cookie_source_label({"source": "wx_wechat"}, "") == "微信登录"
    assert cookie_source_label(None, "token=t") == "WebUI 手动配置"
    assert cookie_source_label(None, "") == "无"


def test_cookie_source_label_does_not_leak_cookie_value():
    assert "token" not in cookie_source_label(None, "token=SECRET")


def _web_sign(params):
    """按上游算法独立实现，避免断言依赖被测代码的 web_sign。"""
    salt = "NVPh5oo715z5DIWAeQlhMDsWXXQV4hwt"
    joined = "".join(f"{key}={params[key]}" for key in sorted(params))
    return hashlib.md5(f"{salt}{joined}{salt}".encode()).hexdigest()


def _android_sign(params, body=""):
    """按上游 android 渠道算法独立实现（query 排序串 + body 一起签名）。"""
    salt = "LnT6xpN3khm36zse0QzvmgTZ3waWdRSA"
    joined = "".join(f"{key}={params[key]}" for key in sorted(params))
    return hashlib.md5(f"{salt}{joined}{body}{salt}".encode()).hexdigest()
