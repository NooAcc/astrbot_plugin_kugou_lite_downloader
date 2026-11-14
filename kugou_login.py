"""酷狗概念版（Lite）登录协议：扫码登录。

从旧版 main.py 移植，只保留概念版（安卓渠道）分支：删除标准版 RSA 模数、
标准版 appid/clientver、PC 渠道参数与全部版本开关。

不打印、不抛出任何 token / 签名内容；错误信息只说明失败环节。
"""

from __future__ import annotations

import base64
import binascii
import json
import time
from dataclasses import dataclass

from astrbot.api import logger

try:
    from .kugou_client import (
        APPID,
        CLIENTVER,
        KG_STATIC_HEADERS,
        SRCAPPID,
        Device,
        web_sign,
    )
except ImportError:  # 以顶层模块方式导入（离线测试）
    from kugou_client import (
        APPID,
        CLIENTVER,
        KG_STATIC_HEADERS,
        SRCAPPID,
        Device,
        web_sign,
    )

QR_APPID = 1001                     # /v2/qrcode 固定 appid（上游 module 常量）
SCAN_PAGE = "https://h5.kugou.com/apps/loginQRCode/html/index.html"
DEFAULT_UA = "Android15-1070-11083-46-0-DiscoveryDRADProtocol-wifi"

QR_URL = "https://login-user.kugou.com/v2/qrcode"
QR_CHECK_URL = "https://login-user.kugou.com/v2/get_userinfo_qrcode"
MAX_QR_IMAGE_B64 = 64 * 1024     # 二维码 PNG 通常 1~3 KB；防止异常大 payload 撑爆消息

# 扫码登录轮询状态：-1 瞬时错误（可重试）、0 已过期、1 待扫、2 已扫、4 已确认
QR_TRANSIENT = -1
QR_EXPIRED = 0
QR_WAITING = 1
QR_SCANNED = 2
QR_CONFIRMED = 4


class LoginError(Exception):
    """登录流程失败。`str()` 可直接展示给用户，且不含任何凭据。"""


# ── 数据结构 ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class QrSession:
    key: str
    scan_url: str
    image_b64: str = ""     # 酷狗返回的二维码 PNG 的 base64（不含 data URI 前缀）
    image: bytes = b""      # 校验过的 PNG 原始字节，供离线校验/调试


@dataclass(frozen=True)
class LoginResult:
    token: str
    userid: str
    source: str          # inline_qr


# ── Cookie 工具 ──────────────────────────────────────────────────────────


def extract_cookie_field(cookie: str, field: str) -> str:
    if not cookie:
        return ""
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith(field + "="):
            return part[len(field) + 1:]
    return ""


def build_cookie(token: str, userid: str) -> str:
    return f"token={token}; userid={userid}; a_id={APPID}"


def validate_cookie(cookie: str) -> tuple[bool, str]:
    """校验凭据是否可用于概念版。

    显式带标准版 `a_id` 的凭据直接拒绝；没有版本字段的凭据不盲目判定为
    概念版有效 —— 是否真正可用，由正常接口的授权结果决定。
    """
    raw = (cookie or "").strip()
    if not raw:
        return False, "未配置登录凭据"

    if raw.startswith("{") and "cookie_string" in raw:
        try:
            raw = str(json.loads(raw).get("cookie_string") or "").strip()
        except (json.JSONDecodeError, TypeError, AttributeError):
            return False, "登录凭据格式无法解析（JSON）"
    if not raw:
        return False, "未配置登录凭据"

    a_id = extract_cookie_field(raw, "a_id").strip()
    if a_id and a_id != str(APPID):
        label = "标准版" if a_id == "1005" else f"未知版本 a_id={a_id}"
        return False, f"该凭据属于{label}账号，酷狗概念版无法使用，请重新登录"

    if not extract_cookie_field(raw, "token").strip():
        return False, "登录凭据缺少 token，请重新登录"
    if not extract_cookie_field(raw, "userid").strip():
        return False, "登录凭据缺少 userid，请重新登录"
    return True, ""


def cookie_source_label(saved: dict | None, configured: str) -> str:
    """凭据来源标签；不返回凭据内容本身。"""
    if isinstance(saved, dict):
        source = str(saved.get("source") or "")
        if source == "inline_qr":
            return "扫码登录"
    return "WebUI 手动配置" if (configured or "").strip() else "无"


# ── 登录客户端 ───────────────────────────────────────────────────────────


class LiteLogin:
    """概念版扫码登录。会话由调用方注入，便于离线测试。"""

    def __init__(self, session, device: Device, ua: str = "", timeout: float = 10.0):
        self._session = session
        self._device = device
        self._ua = ua or DEFAULT_UA
        self._timeout = timeout

    async def qr_start(self) -> QrSession:
        params = self._qr_params({
            "type": "1",
            "plat": "4",                     # 概念版走安卓渠道
            "appid": QR_APPID,
            "qrcode_txt": f"{SCAN_PAGE}?appid={APPID}&",
        })
        payload = await self._kugou_get(QR_URL, params)
        inner = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        key = str((inner or {}).get("qrcode") or "")
        if not key:
            raise LoginError(
                "生成登录二维码失败：酷狗没有返回二维码"
                "（可能接口临时不可用，或设备指纹被风控）"
            )
        image_b64, image = _qr_image((inner or {}).get("qrcode_img"))
        return QrSession(
            key=key,
            scan_url=f"{SCAN_PAGE}?qrcode={key}",
            image_b64=image_b64,
            image=image,
        )

    async def qr_check(self, key: str) -> int:
        """返回轮询状态；网络异常返回 `QR_TRANSIENT`，不会中断轮询。"""
        params = self._qr_params({"plat": "4", "qrcode": key})
        try:
            payload = await self._kugou_get(QR_CHECK_URL, params)
        except LoginError as exc:
            logger.debug("[kugou] 扫码状态查询失败：%s", exc)
            return QR_TRANSIENT
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        return _to_int((data or {}).get("status"))

    async def qr_credentials(self, key: str) -> LoginResult:
        params = self._qr_params({"plat": "4", "qrcode": key})
        payload = await self._kugou_get(QR_CHECK_URL, params)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        token = str((data or {}).get("token") or "")
        userid = str((data or {}).get("userid") or "")
        if not token or not userid:
            raise LoginError("扫码已确认，但没有取到 token/userid，请重新登录")
        return LoginResult(token=token, userid=userid, source="inline_qr")

    def _qr_params(self, extra: dict) -> dict:
        params = {
            "dfid": self._device.dfid,
            "mid": self._device.mid,
            "uuid": self._device.uuid,
            "appid": APPID,
            "clientver": CLIENTVER,
            "clienttime": int(time.time()),
            "srcappid": SRCAPPID,
        }
        params.update(extra)
        params["signature"] = web_sign(params)
        return params

    # ── 请求 ─────────────────────────────────────────────────────────────

    async def _kugou_get(self, url: str, params: dict) -> dict:
        headers = {
            "User-Agent": self._ua,
            "dfid": self._device.dfid,
            "mid": self._device.mid,
            "clienttime": str(params.get("clienttime", "")),
            **KG_STATIC_HEADERS,
        }
        return await self._request_json(url, params=params, headers=headers)

    async def _request_json(self, url: str, params: dict, headers: dict | None = None) -> dict:
        request_headers = {"User-Agent": self._ua}
        if headers:
            request_headers.update(headers)

        try:
            ctx = self._session.get(url, params=params, headers=request_headers,
                                     timeout=self._timeout)
            async with ctx as resp:
                if resp.status != 200:
                    raise LoginError(f"接口返回 HTTP {resp.status}")
                return _parse_json(await resp.text())
        except LoginError:
            raise
        except Exception as exc:
            raise LoginError(f"请求失败：{type(exc).__name__}") from exc


def _qr_image(raw) -> tuple[str, bytes]:
    """解析酷狗 `data:image/png;base64,...`；无效或非 PNG 数据当作没有图片。"""
    text = raw if isinstance(raw, str) else ""
    prefix = "data:image/png;base64,"
    if not text.startswith(prefix):
        return "", b""
    encoded = text[len(prefix):].strip()
    if not encoded or len(encoded) > MAX_QR_IMAGE_B64:
        return "", b""
    try:
        image = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return "", b""
    if not image.startswith(b"\x89PNG\r\n\x1a\n"):
        return "", b""
    return encoded, image


def _parse_json(text: str) -> dict:
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _to_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
