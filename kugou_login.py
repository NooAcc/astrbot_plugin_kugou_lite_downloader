"""酷狗概念版（Lite）登录协议：扫码登录与微信授权登录。

从旧版 main.py 移植，只保留概念版（安卓渠道）分支：删除标准版 RSA 模数、
标准版 appid/clientver、PC 渠道参数与全部版本开关。加解密逻辑与上游
KuGouMusicApi 的 util/crypto.js 一致。

不打印、不抛出任何 token / 签名内容；错误信息只说明失败环节。

微信登录的请求形态（body 只有 10 个字段、设备标识走 query、JSON 体与 query
一起参与 android 签名）已按上游 login_openplat.js / util/request.js 对齐，
但**未经过真实账号验证**（设计文档禁止在无授权时执行真实登录）：上游的
`dev` 与 t2 中的 KUGOU_API_GUID/MAC/DEV 取自浏览器 cookie，本实现以 "0"
占位。若服务端校验这些指纹，微信登录会失败 —— 此时请改用扫码登录。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

from kugou_client import (
    APPID,
    CLIENTVER,
    KG_STATIC_HEADERS,
    SRCAPPID,
    Device,
    android_sign,
    compact_json,
    web_sign,
)

logger = logging.getLogger(__name__)

QR_APPID = 1001                     # /v2/qrcode 固定 appid（上游 module 常量）
SCAN_PAGE = "https://h5.kugou.com/apps/loginQRCode/html/index.html"
DEFAULT_UA = "Android15-1070-11083-46-0-DiscoveryDRADProtocol-wifi"

QR_URL = "https://login-user.kugou.com/v2/qrcode"
QR_CHECK_URL = "https://login-user.kugou.com/v2/get_userinfo_qrcode"
WX_OAUTH_URL = "https://api.weixin.qq.com/sns/oauth2/access_token"
WX_TOKEN_URL = "https://api.weixin.qq.com/cgi-bin/token"
WX_TICKET_URL = "https://api.weixin.qq.com/cgi-bin/ticket/getticket"
WX_QRCONNECT_URL = "https://open.weixin.qq.com/connect/sdk/qrconnect"
WX_LONG_POLL_URL = "https://long.open.weixin.qq.com/connect/l/qrconnect"
KG_OPENPLAT_URL = "https://gateway.kugou.com/v6/login_by_openplat"

# 扫码登录轮询状态：-1 瞬时错误（可重试）、0 已过期、1 待扫、2 已扫、4 已确认
QR_TRANSIENT = -1
QR_EXPIRED = 0
QR_WAITING = 1
QR_SCANNED = 2
QR_CONFIRMED = 4

# RSA 模数（概念版公钥；上游 crypto.js publicLiteRasKey）
_RSA_LITE_N = 137663794008607651653890046938671327369715483147545627213325703442262571612527589810823814641909238236485832314923006785354760218579495585325103045620379147165040411681626483377778892956210750442114872075206236006794712873114289397588119752792503667245748501905527122576158396773297736227160469650546868400160
_RSA_E = 65537
# 概念版 AES 固定密钥（上游 login_openplat.js）
_LITE_T2_KEY = "fd14b35e3f81af3817a20ae7adae7020"
_LITE_T2_IV = "17a20ae7adae7020"
_LITE_T1_KEY = "5e4ef500e9597fe004bd09a46d8add98"
_LITE_T1_IV = "04bd09a46d8add98"
# 概念版微信小程序 appid/secret（上游 util/config.json）
_WX_LITE_APPID = "wx72b795aca60ad321"
_WX_LITE_SECRET = "33e486041e5e25729a4e3d2da7502f9a"

_T3_CONST = "MCwwLDAsMCwwLDAsMCwwLDA="


class LoginError(Exception):
    """登录流程失败。`str()` 可直接展示给用户，且不含任何凭据。"""


# ── 加解密 ───────────────────────────────────────────────────────────────


def aes_encrypt(data: str, key: str, iv: str) -> str:
    """AES-128-CBC + PKCS7 → hex（与上游固定 key/iv 的 cryptoAesEncrypt 一致）。"""
    cipher = AES.new(key.encode("utf-8"), AES.MODE_CBC, iv.encode("utf-8"))
    return cipher.encrypt(pad(data.encode("utf-8"), 16)).hex()


def aes_decrypt(hex_str: str, key: str, iv: str) -> str:
    cipher = AES.new(key.encode("utf-8"), AES.MODE_CBC, iv.encode("utf-8"))
    return unpad(cipher.decrypt(bytes.fromhex(hex_str)), 16).decode("utf-8")


def rsa_encrypt(data: bytes, modulus: int = _RSA_LITE_N, exponent: int = _RSA_E) -> str:
    """原始模幂 + 左侧零填充（与上游 BigInteger.modPow 实现一致）。"""
    key_len = (modulus.bit_length() + 7) // 8
    padded = b"\x00" * (key_len - len(data)) + data
    return hex(pow(int.from_bytes(padded, "big"), exponent, modulus))[2:].upper().zfill(key_len * 2)


# ── 数据结构 ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class QrSession:
    key: str
    scan_url: str


@dataclass(frozen=True)
class WxSession:
    uuid: str
    auth_url: str


@dataclass(frozen=True)
class LoginResult:
    token: str
    userid: str
    source: str          # inline_qr / wx_wechat


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
        if source == "wx_wechat":
            return "微信登录"
    return "WebUI 手动配置" if (configured or "").strip() else "无"


# ── 登录客户端 ───────────────────────────────────────────────────────────


class LiteLogin:
    """概念版扫码/微信登录。会话由调用方注入，便于离线测试。"""

    def __init__(self, session, device: Device, ua: str = "", timeout: float = 10.0,
                 temp_key_factory: Callable[[], str] | None = None):
        self._session = session
        self._device = device
        self._ua = ua or DEFAULT_UA
        self._timeout = timeout
        self._temp_key_factory = temp_key_factory or (
            lambda: hashlib.md5(uuid.uuid4().hex.encode()).hexdigest()[:16]
        )

    # ── 扫码登录 ─────────────────────────────────────────────────────────

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
        return QrSession(key=key, scan_url=f"{SCAN_PAGE}?qrcode={key}")

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

    # ── 微信登录 ─────────────────────────────────────────────────────────

    async def wx_start(self) -> WxSession:
        access_token = await self._wx_access_token()
        ticket = await self._wx_ticket(access_token)

        timestamp = int(time.time() * 1000)
        noncestr = hashlib.md5(uuid.uuid4().hex.encode()).hexdigest()
        sig_str = (
            f"appid={_WX_LITE_APPID}&noncestr={noncestr}"
            f"&sdk_ticket={ticket}&timestamp={timestamp}"
        )
        params = {
            "appid": _WX_LITE_APPID,
            "noncestr": noncestr,
            "timestamp": timestamp,
            "scope": "snsapi_userinfo",
            "signature": hashlib.sha1(sig_str.encode()).hexdigest(),
        }
        payload = await self._request_json("get", WX_QRCONNECT_URL, params=params)
        if _to_int(payload.get("errcode")) != 0 or not payload.get("uuid"):
            raise LoginError("生成微信授权链接失败，请稍后重试")
        wx_uuid = str(payload["uuid"])
        return WxSession(
            uuid=wx_uuid,
            auth_url=f"https://open.weixin.qq.com/connect/confirm?uuid={wx_uuid}",
        )

    async def wx_poll_once(self, wx_uuid: str) -> str:
        """查询一次授权结果，返回授权码；未确认或请求失败返回空串。"""
        try:
            payload = await self._request_json(
                "get", WX_LONG_POLL_URL, params={"f": "json", "uuid": wx_uuid}
            )
        except LoginError as exc:
            logger.debug("[kugou] 微信授权轮询失败：%s", exc)
            return ""
        return str(payload.get("wx_code") or "")

    async def wx_exchange(self, code: str) -> LoginResult:
        """授权码 → 酷狗 token 与 userid。"""
        oauth = await self._request_json("post", WX_OAUTH_URL, params={
            "secret": _WX_LITE_SECRET,
            "appid": _WX_LITE_APPID,
            "code": code,
            "grant_type": "authorization_code",
        })
        access_token = str(oauth.get("access_token") or "")
        openid = str(oauth.get("openid") or "")
        if not access_token or not openid:
            raise LoginError("微信授权失败：没有取到 access_token/openid，请稍后重试")

        temp_key = self._temp_key_factory()
        aes_key = hashlib.md5(temp_key.encode()).hexdigest()[:32]
        aes_iv = aes_key[-16:]
        clienttime_ms = int(time.time() * 1000)

        body_map = {
            "dev": "0",          # 上游取 cookie 中的 KUGOU_API_DEV；无浏览器指纹时以 "0" 占位
            "force_login": 1,
            "partnerid": 36,
            "clienttime_ms": clienttime_ms,
            "t1": aes_encrypt(f"|{clienttime_ms}", _LITE_T1_KEY, _LITE_T1_IV),
            "t2": aes_encrypt(
                # 上游：`${GUID}|0f60…|${MAC}|${DEV}|${ms}`；此处以 "0" 占位 GUID/MAC/DEV
                f"0|0f607264fc6318a92b9e13c65db7cd3c|0|0|{clienttime_ms}",
                _LITE_T2_KEY, _LITE_T2_IV,
            ),
            "t3": _T3_CONST,
            "openid": openid,
            "params": aes_encrypt(json.dumps({"access_token": access_token}), aes_key, aes_iv),
            "pk": rsa_encrypt(
                json.dumps({"clienttime_ms": clienttime_ms, "key": temp_key}).encode()
            ),
        }
        # 与上游 login_openplat.js 一致：设备标识走 query，请求体只含上面 10 个字段，
        # 且签名覆盖「排序后的 query + 实际发送的 JSON 体」（android 渠道规则）。
        body = compact_json(body_map)
        clienttime = int(time.time())
        query = {
            "dfid": self._device.dfid,
            "mid": self._device.mid,
            "uuid": self._device.uuid,
            "appid": APPID,
            "clientver": CLIENTVER,
            "clienttime": clienttime,
            "srcappid": SRCAPPID,
        }
        query["signature"] = android_sign(query, body=body)

        headers = {
            "User-Agent": self._ua,
            "clienttime": str(clienttime),
            "dfid": self._device.dfid,
            "mid": self._device.mid,
            "x-router": "login.user.kugou.com",
            "Content-Type": "application/json",
        }
        response_body = await self._request_json(
            "post", KG_OPENPLAT_URL, params=query, data=body, headers=headers
        )
        if _to_int(response_body.get("status")) != 1:
            raise LoginError(
                f"酷狗拒绝了本次登录（status={response_body.get('status')}）。"
                "请确认该微信号已绑定酷狗账号，或稍后重试"
            )

        data = response_body.get("data") if isinstance(response_body.get("data"), dict) else {}
        secu = str((data or {}).get("secu_params") or "")
        if secu:
            try:
                decoded = json.loads(aes_decrypt(secu, aes_key, aes_iv))
                token = str(decoded.get("token") or "") if isinstance(decoded, dict) else str(decoded)
                userid = str(
                    (decoded.get("userid") if isinstance(decoded, dict) else None)
                    or (data or {}).get("userid") or "0"
                )
                if token:
                    return LoginResult(token=token, userid=userid, source="wx_wechat")
            except Exception:
                logger.debug("[kugou] secu_params 解密失败，尝试使用明文 token")

        raw_token = str((data or {}).get("token") or "")
        if raw_token:
            return LoginResult(
                token=raw_token, userid=str((data or {}).get("userid") or "0"),
                source="wx_wechat",
            )
        raise LoginError("微信授权成功，但没有取到酷狗 token，请稍后重试")

    async def _wx_access_token(self) -> str:
        payload = await self._request_json("get", WX_TOKEN_URL, params={
            "grant_type": "client_credential",
            "appid": _WX_LITE_APPID,
            "secret": _WX_LITE_SECRET,
        })
        token = str(payload.get("access_token") or "")
        if not token:
            logger.debug("[kugou] 微信 access_token 获取失败（errcode=%s）", payload.get("errcode"))
            raise LoginError("获取微信 access_token 失败，请稍后重试")
        return token

    async def _wx_ticket(self, access_token: str) -> str:
        payload = await self._request_json("get", WX_TICKET_URL, params={
            "access_token": access_token, "type": 2,
        })
        ticket = str(payload.get("ticket") or "")
        if _to_int(payload.get("errcode")) != 0 or not ticket:
            raise LoginError("获取微信 sdk ticket 失败，请稍后重试")
        return ticket

    # ── 请求 ─────────────────────────────────────────────────────────────

    async def _kugou_get(self, url: str, params: dict) -> dict:
        headers = {
            "User-Agent": self._ua,
            "dfid": self._device.dfid,
            "mid": self._device.mid,
            "clienttime": str(params.get("clienttime", "")),
            **KG_STATIC_HEADERS,
        }
        return await self._request_json("get", url, params=params, headers=headers)

    async def _request_json(self, method: str, url: str, params: dict | None = None,
                            data: dict | None = None, headers: dict | None = None) -> dict:
        request_headers = {"User-Agent": self._ua}
        if headers:
            request_headers.update(headers)

        try:
            if method == "post":
                ctx = self._session.post(url, params=params, data=data,
                                         headers=request_headers, timeout=self._timeout)
            else:
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
