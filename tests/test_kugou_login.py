import asyncio
import hashlib

import pytest
from fake_session import FakeSession, json_response

from kugou_client import Device
from kugou_login import (
    QR_EXPIRED,
    QR_TRANSIENT,
    LiteLogin,
    LoginError,
    build_cookie,
    cookie_source_label,
    extract_cookie_field,
    validate_cookie,
)

_DEVICE = Device(dfid="d" * 8, mid="m" * 8, uuid="u" * 8)


def _login(responses):
    session = FakeSession(responses)
    return LiteLogin(session, _DEVICE, ua="ua", timeout=5.0), session


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
    assert cookie_source_label(None, "token=t") == "WebUI 手动配置"
    assert cookie_source_label(None, "") == "无"


def test_cookie_source_label_does_not_leak_cookie_value():
    assert "token" not in cookie_source_label(None, "token=SECRET")


def _web_sign(params):
    """按上游算法独立实现，避免断言依赖被测代码的 web_sign。"""
    salt = "NVPh5oo715z5DIWAeQlhMDsWXXQV4hwt"
    joined = "".join(f"{key}={params[key]}" for key in sorted(params))
    return hashlib.md5(f"{salt}{joined}{salt}".encode()).hexdigest()
