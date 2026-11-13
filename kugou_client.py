"""酷狗概念版（Lite）协议客户端：常量、签名、设备标识、脱敏。

协议结论与依据见 docs/superpowers/plans/2026-11-13-kugou-lite-file-sending.md
的「协议决策」一节（上游 KuGouMusicApi 的 util/helper.js、util/request.js、
module/song_url.js、module/privilege_lite.js、module/search.js）。

本模块不导入 astrbot，也不直接导入 aiohttp：HTTP 会话由调用方注入。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from file_cache import DownloadError, SharedDirError, UnsafeUrlError, build_filename
from quality import TIER_PROTOCOL_QUALITY, TIER_RELATE_LEVELS, TIERS, label, meets

# ── Lite 常量 ────────────────────────────────────────────────────────────

APPID = 3116
CLIENTVER = 11440
SEARCH_CLIENTVER = 201          # 概念版搜索用的 versionCode（上游 search.js: isLite → 201）
SRCAPPID = 2919

WEB_SIGN_SALT = "NVPh5oo715z5DIWAeQlhMDsWXXQV4hwt"
ANDROID_SALT = "LnT6xpN3khm36zse0QzvmgTZ3waWdRSA"
KEY_SALT = "185672dd44712f60bb1736df5a377e82"

# /v5/url 固定参数（上游 song_url.js 的 dataMap 原样照搬）
URL_VERSION = 11430
URL_PAGE_ID = 967177915
URL_PPAGE_ID = "356753938,823673182,967485191"
URL_PID = 411

KG_UA = "Android15-1070-11083-46-0-DiscoveryDRADProtocol-wifi"
KG_STATIC_HEADERS = {
    "kg-rc": "1",
    "kg-thash": "5d816a0",
    "kg-rec": "1",
    "kg-rf": "B9EDA08A64250DEFFBCADDEE00F8F25F",
}

GATEWAY = "https://gateway.kugou.com"
SEARCH_ROUTER = "complexsearch.kugou.com"
PRIVILEGE_ROUTER = "media.store.kugou.com"
#: 先试直连 CDN，失败再走网关 + x-router（两者都是上游出现过的形态）
URL_ROUTERS: tuple[tuple[str, str | None], ...] = (
    ("https://trackercdn.kugou.com/v5/url", None),
    (GATEWAY + "/v5/url", "trackercdn.kugou.com"),
)
PUBLIC_SEARCH_URL = "https://mobilecdn.kugou.com/api/v3/search/song"

#: 权益接口一次请求的音质全集（上游会**整体**返回，缺项才代表上游没有该音质）
UPSTREAM_QUALITIES = [
    "128", "320", "flac", "high", "viper_atmos", "viper_tape", "viper_clear",
    "super", "multitrack",
]


# ── 设备与凭据 ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Device:
    """设备标识。优先取 cookie 中的值，缺失时生成本机随机值并持久化。"""

    dfid: str
    mid: str
    uuid: str


@dataclass(frozen=True)
class Credentials:
    """酷狗登录凭据（插件共享一个账号）。"""

    token: str
    userid: str


# ── 签名 ─────────────────────────────────────────────────────────────────


def compact_json(value) -> str:
    """等价于 JS `JSON.stringify`：无空格、保留非 ASCII。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def default_params(device: Device, credentials: Credentials | None = None,
                   clientver: int = CLIENTVER, clienttime: int | None = None) -> dict:
    """通用请求参数（不含 `signature`，由请求函数按最终参数表计算）。"""
    params = {
        "dfid": device.dfid,
        "mid": device.mid,
        "uuid": device.uuid,
        "appid": APPID,
        "clientver": clientver,
        "clienttime": int(clienttime if clienttime is not None else time.time()),
    }
    if credentials is not None:
        if credentials.token:
            params["token"] = credentials.token
        if credentials.userid:
            params["userid"] = credentials.userid
    return params


def android_sign(params: dict, body: str = "") -> str:
    """`md5(盐 + 排序参数串 + 请求体 + 盐)`。

    `body` 必须是**实际发送**的请求体字符串（POST 请求参与签名）。
    """
    return _sign(ANDROID_SALT, params, body)


def web_sign(params: dict, body: str = "") -> str:
    """Web/H5 登录接口使用的同构签名。"""
    return _sign(WEB_SIGN_SALT, params, body)


def sign_key(hash_value: str, mid: str, userid: str, appid: str = APPID) -> str:
    """`key` 参数签名：`md5(hash + 盐 + appid + mid + userid)`。"""
    return _md5(f"{hash_value}{KEY_SALT}{appid}{mid}{userid or 0}")


def _sign(salt: str, params: dict, body: str = "") -> str:
    return _md5(f"{salt}{_join_params(params)}{body}{salt}")


def _join_params(params: dict) -> str:
    parts = []
    for key in sorted(params.keys()):
        value = params[key]
        if isinstance(value, (dict, list, tuple)):
            value = compact_json(value)
        parts.append(f"{key}={value}")
    return "".join(parts)


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


# ── 脱敏 ─────────────────────────────────────────────────────────────────

_SECRET_KEYS = frozenset({
    "token", "key", "signature", "cookie", "secret", "password", "authorization",
    "dfid", "mid", "uuid", "kg-thash", "kg-rf", "kugou_api_mid",
})
_URL_KEYS = frozenset({
    "url", "urls", "play_url", "jump_url", "audio", "musicurl", "preview",
})
_MASK = "***"
_HEX_RE = re.compile(r"[0-9a-fA-F]{24,}")


def redact_secrets(value):
    """把调试输出里的凭据、设备指纹、签名与直链查询串替换掉。

    策略：敏感键整体打码；URL 类键与任何 `http` 开头的字符串只保留
    `scheme://host/path`；长十六进制串打码。返回新对象，不修改入参。
    """
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in _SECRET_KEYS:
                result[key] = _MASK
            elif lowered in _URL_KEYS:
                result[key] = _redact_url_value(item)
            else:
                result[key] = redact_secrets(item)
        return result

    if isinstance(value, (list, tuple)):
        return [redact_secrets(item) for item in value]

    if isinstance(value, str):
        if value.startswith("http"):
            return _redact_url(value)
        if _HEX_RE.fullmatch(value):
            return _MASK
        return value

    return value


def _redact_url_value(value):
    if isinstance(value, str):
        return _redact_url(value) if value.startswith("http") else _MASK
    return redact_secrets(value)


def _redact_url(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return _MASK
    if not parts.scheme or not parts.netloc:
        return _MASK
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


# ── 客户端 ───────────────────────────────────────────────────────────────

logger = logging.getLogger(__name__)


class KugouApiError(Exception):
    """协议调用失败（HTTP 错误、非 JSON 响应等），通常可回退或重试。"""


class KugouClient:
    """概念版协议的只读客户端：搜索、权益查询、资源解析。

    会话由调用方注入；本类不持有凭据以外的状态，可安全并发使用。
    """

    def __init__(self, session, device: Device, credentials: Credentials | None = None,
                 ua: str = "", timeout: float = 15.0):
        self._session = session
        self._device = device
        self._credentials = credentials
        self._ua = ua or KG_UA
        self._timeout = timeout

    # ── 搜索 ─────────────────────────────────────────────────────────────

    async def search(self, keyword: str, count: int) -> list[dict]:
        """搜索歌曲。优先概念版接口，失败或缺凭据时回退公共来源。

        返回项：`hash, songname, singername, album_id, album_audio_id,
        duration, pay_type`。

        两个来源都「应答但没有结果」时返回空列表；连公共来源都请求失败时
        让 `KugouApiError` 冒泡，使上层能区分「没搜到」与「搜索失败」。
        """
        songs: list[dict] = []
        if self._credentials is not None:
            try:
                songs = await self._search_lite(keyword, count)
            except Exception as exc:
                logger.debug("[kugou] 概念版搜索失败，改用公共搜索：%s", type(exc).__name__)
        if not songs:
            songs = await self._search_public(keyword, count)
        return songs

    async def _search_lite(self, keyword: str, count: int) -> list[dict]:
        params = default_params(self._device, self._credentials, clientver=SEARCH_CLIENTVER)
        params.update({
            "keyword": keyword,
            "page": 1,
            "pagesize": count,
            "platform": "AndroidFilter",
            "iscorrection": 1,
            "privilegefilter": 0,
            "area_code": 1,
            "dopicfull": 1,
        })
        payload = await self.api_get_json(
            GATEWAY + "/v2/search/song", params, router=SEARCH_ROUTER
        )
        if payload.get("status") != 1:
            logger.debug(
                "[kugou] 概念版搜索返回 status=%s error_code=%s",
                payload.get("status"), payload.get("error_code"),
            )
            return []
        return normalize_songs(payload)

    async def _search_public(self, keyword: str, count: int) -> list[dict]:
        """公共搜索作为兼容来源：无需登录，也不使用标准版账号协议。"""
        params = {"format": "json", "keyword": keyword, "page": 1, "pagesize": count}
        headers = {
            "Accept": "application/json",
            "Referer": "https://www.kugou.com/",
        }
        payload = await self.api_get_json(PUBLIC_SEARCH_URL, params, headers=headers, sign=False)
        if payload.get("status") != 1:
            return []
        return normalize_songs(payload)

    # ── 通用请求 ─────────────────────────────────────────────────────────

    async def api_get_json(self, url: str, params: dict, headers: dict | None = None,
                           router: str | None = None, sign: bool = True) -> dict:
        query = dict(params)
        request_headers = self._base_headers(router)
        if sign:
            query["signature"] = android_sign(query)
            request_headers = self._device_headers(query, request_headers)
        if headers:
            request_headers.update(headers)

        ctx = self._session.get(url, params=query, headers=request_headers, timeout=self._timeout)
        async with ctx as resp:
            if resp.status != 200:
                raise KugouApiError(f"HTTP {resp.status}")
            return _parse_json(await resp.text())

    async def api_post_json(self, url: str, params: dict, body_obj: dict,
                            router: str | None = None) -> dict:
        """POST 时请求体只序列化一次：同一字符串既参与签名也原样发送。"""
        body = compact_json(body_obj)
        query = dict(params)
        query["signature"] = android_sign(query, body=body)
        request_headers = self._device_headers(query, self._base_headers(router))
        request_headers["Content-Type"] = "application/json"

        ctx = self._session.post(
            url, params=query, data=body, headers=request_headers, timeout=self._timeout
        )
        async with ctx as resp:
            if resp.status != 200:
                raise KugouApiError(f"HTTP {resp.status}")
            return _parse_json(await resp.text())

    def _base_headers(self, router: str | None) -> dict:
        headers = {"User-Agent": self._ua, **KG_STATIC_HEADERS}
        if router:
            headers["x-router"] = router
        return headers

    @staticmethod
    def _device_headers(params: dict, headers: dict) -> dict:
        headers["dfid"] = str(params.get("dfid", ""))
        headers["mid"] = str(params.get("mid", ""))
        headers["clienttime"] = str(params.get("clienttime", ""))
        return headers

    # ── 权益查询与资源解析 ───────────────────────────────────────────────

    async def privileges(self, song: dict) -> list[QualityOption]:
        """查询该歌曲各档位的权益与资源 hash（一次请求拿到全部音质）。"""
        hash_value = str(song.get("hash") or "").lower()
        body = {
            "appid": APPID,
            "area_code": 1,
            "behavior": "play",
            "clientver": CLIENTVER,
            "need_hash_offset": 1,
            "relate": 1,
            "support_verify": 1,
            "resource": [{
                "type": "audio",
                "page_id": 0,
                "hash": hash_value,
                "album_id": _to_int(song.get("album_id")),
            }],
            "qualities": list(UPSTREAM_QUALITIES),
        }
        params = default_params(self._device, self._credentials)
        payload = await self.api_post_json(
            GATEWAY + "/v2/get_res_privilege/lite", params, body, router=PRIVILEGE_ROUTER
        )
        return parse_privilege(payload, song)

    async def resolve_urls(self, song: dict, option: QualityOption) -> ResolvedUrl:
        """取该档位资源的播放地址列表（可能含多个备用 CDN）。"""
        if option.require_vip_type and self._credentials is None:
            raise ResourceUnavailable(
                option.tier, "该音质需要登录酷狗账号，请让管理员先执行 /酷狗扫码登录"
            )

        hash_value = str(option.hash or song.get("hash") or "").lower()
        if not hash_value:
            raise ResourceUnavailable(option.tier, "缺少资源 hash")

        last_error: ResourceUnavailable | None = None
        for endpoint, router in URL_ROUTERS:
            try:
                payload = await self._request_url(endpoint, router, song, option, hash_value)
            except ResourceUnavailable as exc:
                last_error = exc
                continue
            except Exception as exc:     # 传输失败 → 换下一个 endpoint，而不是换档位
                logger.debug("[kugou] %s 请求失败：%s", endpoint, type(exc).__name__)
                last_error = ResourceUnavailable(option.tier, f"请求失败：{type(exc).__name__}")
                continue

            raw_urls = payload.get("url")
            if isinstance(raw_urls, str):        # 服务端偶尔只返回一个地址
                raw_urls = [raw_urls]
            if not isinstance(raw_urls, (list, tuple)):
                raw_urls = []
            urls = [str(item) for item in raw_urls if item]
            if payload.get("status") == 1 and urls:
                return ResolvedUrl(
                    tier=option.tier,
                    urls=urls,
                    extname=str(payload.get("extName") or option.extname or ""),
                    size=_to_int(payload.get("fileSize")) or option.size,
                    hash=hash_value,
                )
            last_error = ResourceUnavailable(option.tier, _url_reason(payload))

        raise last_error or ResourceUnavailable(option.tier, "没有可用的播放地址")

    async def _request_url(self, endpoint: str, router: str | None, song: dict,
                           option: QualityOption, hash_value: str) -> dict:
        userid = self._credentials.userid if self._credentials else "0"
        params = default_params(self._device, self._credentials)
        params.update({
            "album_id": _to_int(song.get("album_id")),
            "album_audio_id": _to_int(song.get("album_audio_id")),
            "area_code": 1,
            "behavior": "play",
            "cdnBackup": 1,
            "clientver": URL_VERSION,
            "cmd": 26,
            "hash": hash_value,
            "IsFreePart": 0,
            "key": sign_key(hash_value, self._device.mid, userid, APPID),
            "module": "",
            "page_id": URL_PAGE_ID,
            "pid": URL_PID,
            "pidversion": 3001,
            "ppage_id": URL_PPAGE_ID,
            "quality": TIER_PROTOCOL_QUALITY[option.tier],
            "ssa_flag": "is_fromtrack",
            "version": URL_VERSION,
        })
        return await self.api_get_json(endpoint, params, router=router)


def _url_reason(payload: dict) -> str:
    if not payload:
        return "解析播放地址失败：服务端没有返回内容"
    parts = []
    if payload.get("fail_process"):
        parts.append(f"fail_process={payload['fail_process']}")
    if payload.get("error_code"):
        parts.append(f"error_code={payload['error_code']}")
    if payload.get("error"):
        parts.append(str(payload["error"]))
    reason = "解析播放地址被服务端拒绝"
    return f"{reason}：{'，'.join(parts)}" if parts else reason


def _parse_json(text: str) -> dict:
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


# ── 权益解析 ─────────────────────────────────────────────────────────────


class ResourceUnavailable(Exception):
    """某个档位拿不到资源。`reason` 可直接展示，用于聚合成失败摘要。"""

    def __init__(self, tier: str, reason: str):
        super().__init__(reason)
        self.tier = tier
        self.reason = reason


@dataclass(frozen=True)
class QualityOption:
    """某个档位的上游资源与权益信息。`tier` 是配置档位，不是上游质量名。

    `has_resource` 表示**上游是否提供该档位的资源**（有 hash 才值得向服务端
    请求）。是否真的能取得由服务端裁决 —— `privilege` 是歌曲的**要求**，
    不是本账号的资格，因此不用它做客户端硬拦（见 `DownloadLadder.run`）。
    """

    tier: str
    hash: str
    extname: str
    size: int
    privilege: int
    status: int
    pay_type: int
    level: int
    require_vip_type: str
    has_resource: bool
    reason: str


@dataclass(frozen=True)
class ResolvedUrl:
    tier: str
    urls: list
    extname: str
    size: int
    hash: str


def parse_privilege(payload: dict, song: dict) -> list[QualityOption]:
    """把 `/v2/get_res_privilege/lite` 的响应归一化成每个配置档位一条判定。

    上游语义（见 privilege_lite.js）：
    - `relate_goods` 每个音质一条，`level` 2=128 / 4=320 / 5=flac / 6=Hi-Res / 7=DSD；
    - `privilege == 0` 表示免费可获取，非 0 表示需要会员/付费；
    - `status == 0` 表示不可播；
    - 概念版对标准音质有「免费听」：付费歌曲也能取 128；
    - 128/320 概念版实际播放的是 ogg 变体（`trans_param.ogg_*_hash`）。
    """
    entries = payload.get("data") if isinstance(payload, dict) else None
    resource = _match_resource(entries, str(song.get("hash") or ""))
    relate = resource.get("relate_goods") if isinstance(resource, dict) else None
    relate = relate if isinstance(relate, list) else []
    trans = resource.get("trans_param") if isinstance(resource, dict) else None
    trans = trans if isinstance(trans, dict) else {}

    return [_build_option(tier, relate, trans) for tier in TIERS]


def _match_resource(entries, hash_value: str) -> dict:
    if not isinstance(entries, list) or not entries:
        return {}
    wanted = hash_value.lower()
    for item in entries:
        if isinstance(item, dict) and str(item.get("hash") or "").lower() == wanted:
            return item
    first = entries[0]
    return first if isinstance(first, dict) else {}


def _build_option(tier: str, relate: list, trans: dict) -> QualityOption:
    item = _pick_relate(relate, tier)
    if item is None:
        return QualityOption(
            tier=tier, hash="", extname="", size=0, privilege=-1, status=0, pay_type=-1,
            level=0, require_vip_type="", has_resource=False, reason="上游无此音质",
        )

    info = item.get("info") if isinstance(item.get("info"), dict) else {}
    ogg_hash = ""
    ogg_size = 0
    if tier in ("128", "320"):
        ogg_hash = str(trans.get(f"ogg_{tier}_hash") or "")
        ogg_size = _to_int(trans.get(f"ogg_{tier}_filesize"))

    privilege = _to_int(item.get("privilege"))
    status = _to_int(item.get("status", 1))
    level = _to_int(item.get("level"))
    quality = str(item.get("quality") or "")
    needs_vip = privilege != 0
    # 概念版「免费听」：付费/会员歌曲下标准音质（128）无需会员即可播放
    free_listen = tier == "128" and needs_vip
    super_vip = level >= 7 or quality.startswith("viper_")
    require_vip_type = ""
    if needs_vip and not free_listen:
        require_vip_type = "suvip" if super_vip else "concept"
    hash_value = ogg_hash or str(item.get("hash") or "")

    return QualityOption(
        tier=tier,
        hash=hash_value,
        extname="ogg" if ogg_hash else str(info.get("extname") or ""),
        size=ogg_size or _to_int(info.get("filesize")),
        privilege=privilege,
        status=status,
        pay_type=_to_int(item.get("pay_type")),
        level=level,
        require_vip_type=require_vip_type,
        has_resource=bool(hash_value),
        reason="" if free_listen else _requirement_reason(item, privilege, status, require_vip_type),
    )


def _pick_relate(relate: list, tier: str):
    """按上游的 level 优先级挑取该音质的权益条目。

    注意匹配的是**上游质量名**（`TIER_PROTOCOL_QUALITY`，例如 `hires` → `high`），
    不是配置档位名，两者刻意分开维护。
    """
    upstream_quality = TIER_PROTOCOL_QUALITY.get(tier, tier)
    for wanted_level in TIER_RELATE_LEVELS.get(tier, []):
        for item in relate:
            if not isinstance(item, dict):
                continue
            if _to_int(item.get("level")) == wanted_level and item.get("quality") == upstream_quality:
                return item
    for item in relate:
        if isinstance(item, dict) and item.get("quality") == upstream_quality:
            return item
    return None


def _requirement_reason(item: dict, privilege: int, status: int, require_vip_type: str) -> str:
    """该档位的**上游要求**说明（不等于本账号无权限；是否放行以服务端为准）。"""
    if privilege != 0:
        vip_name = "超级VIP" if require_vip_type == "suvip" else "会员"
        return f"需要{vip_name}才能获取（privilege={privilege}）"
    if status == 0:
        return "服务端标记为不可播放"
    fail_process = _to_int(item.get("fail_process"))
    if fail_process:
        return f"服务端拒绝（fail_process={fail_process}）"
    return ""


# ── 档位降级编排 ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DownloadOutcome:
    """一次成功下载的最终产物。`tier`/`label` 都是**实测**档位。

    `from_cache` 表示**本次运行没有为这份产物发起下载**（可能是历史缓存，
    也可能是本次为更高档位下载后按实测档位复用）。
    """

    path: str
    filename: str
    tier: str
    label: str
    info: object
    url: str
    from_cache: bool


class AllQualitiesFailed(Exception):
    """配置内的所有档位都拿不到可用文件。`reasons` 是 (档位, 原因) 列表。"""

    def __init__(self, reasons: list):
        self.reasons = list(reasons)
        super().__init__("；".join(f"{tier}: {reason}" for tier, reason in self.reasons))


class DownloadLadder:
    """按配置顺序逐档位尝试，直到拿到一份**实测达标**的文件。

    关键约束（设计文档 §5）：
    - 先看本档位已验证缓存，不能为了复用低档位缓存而跳过高档位；
    - 上游提供该档位资源就发起解析：`privilege` 是歌曲的**要求**而非本账号的
      资格，权限由服务端裁决，拒绝时如实报告并附上要求提示；
    - 高档位实际拿到低规格文件时，该文件已按实测档位入库，后续档位复用，不重复下载；
    - 网络/目录/配置错误不伪装成「音质不可用」；
    - 只处理配置里列出的档位；
    - 同一份文件只上传一次（上传失败不改换音质重发）。
    """

    def __init__(self, client, fetcher, cache, quality_priority: list):
        self._client = client
        self._fetcher = fetcher
        self._cache = cache
        self._priority = list(quality_priority)

    async def run(self, song: dict) -> DownloadOutcome:
        key = self._cache.resource_key(song)
        reasons: list[tuple[str, str]] = []
        options = None

        for tier in self._priority:
            cached = await self._fetcher.cached(key, tier)
            if cached is not None:
                if _is_trial_fragment(cached.info, song):
                    # 旧版本可能把试听片段写进过缓存：释放后丢弃，绝不当作完整歌曲发送
                    self._cache.release(cached.path)
                    self._cache.discard_entry(key, cached.actual_tier)
                    reasons.append((tier, "缓存中是试听片段，已丢弃"))
                    continue
                if meets(cached.actual_tier, tier):
                    return self._outcome(song, cached, from_cache=True)
                reasons.append((tier, f"缓存实测为 {label(cached.actual_tier)}"))
                continue

            if options is None:
                options = await self._client.privileges(song)
            option = _option_for(options, tier)
            if option is None or not option.has_resource:
                reasons.append((tier, "上游无此音质"))
                continue

            try:
                resolved = await self._client.resolve_urls(song, option)
            except ResourceUnavailable as exc:
                # 是否放行由服务端裁决；拒绝时附上该档位的上游要求作为提示
                detail = exc.reason
                if option.reason and option.reason not in detail:
                    detail = f"{detail}｜{option.reason}"
                reasons.append((tier, detail))
                continue

            try:
                result = await self._fetcher.fetch(key, tier, resolved.urls)
            except (SharedDirError, UnsafeUrlError):
                raise                      # 目录/配置/地址安全错误：不当作音质问题
            except DownloadError as exc:
                reasons.append((tier, exc.reason))
                continue

            if _is_trial_fragment(result.info, song):
                self._cache.release(result.path)
                self._cache.discard_entry(key, result.actual_tier)
                reasons.append((tier, "服务端返回的是试听片段，不能作为完整歌曲发送"))
                continue

            if meets(result.actual_tier, tier):
                return self._outcome(song, result, from_cache=False)
            reasons.append((tier, f"请求 {label(tier)} 实际返回 {label(result.actual_tier)}"))

        raise AllQualitiesFailed(reasons)

    async def release(self, outcome: DownloadOutcome) -> None:
        """上传结束后释放缓存文件的持有标记，允许后续清理。"""
        self._cache.release(outcome.path)

    def _outcome(self, song: dict, result, from_cache: bool) -> DownloadOutcome:
        return DownloadOutcome(
            path=result.path,
            filename=build_filename(
                str(song.get("singername") or ""),
                str(song.get("songname") or ""),
                result.actual_tier,
                result.extension,
            ),
            tier=result.actual_tier,
            label=label(result.actual_tier),
            info=result.info,
            url=result.url,
            from_cache=from_cache,
        )


#: 实测时长低于搜索结果时长的这个比例即视为试听片段（容错 10%）。
TRIAL_DURATION_RATIO = 0.9


def _is_trial_fragment(info, song: dict) -> bool:
    """音频头解析出的时长是否明显短于搜索结果声明的时长。

    服务端只允许试听时通常返回几十秒的片段；把它当作完整歌曲发送是
    设计文档 §5 明确禁止的。任一时长未知时不做判断（无法证伪）。
    """
    actual_ms = int(getattr(info, "duration_ms", 0) or 0)
    expected_ms = _to_int(song.get("duration")) * 1000
    if actual_ms <= 0 or expected_ms <= 0:
        return False
    return actual_ms < expected_ms * TRIAL_DURATION_RATIO


def _option_for(options: list, tier: str):
    for option in options:
        if option.tier == tier:
            return option
    return None


def normalize_songs(payload: dict) -> list[dict]:
    """把搜索结果（含兼容来源的字段差异）归一化成统一结构。"""
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        return []
    items = data.get("info") or data.get("lists") or []
    songs: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        hash_value = _first(item, "hash", "FileHash")
        if not hash_value:
            continue
        duration = _to_int(_first(item, "duration", "Duration"))
        if duration > 1000:      # 兼容以毫秒返回的来源
            duration //= 1000
        album_id = _first(item, "album_id", "AlbumID")
        songs.append({
            "hash": str(hash_value),
            "songname": str(_first(item, "songname", "SongName") or ""),
            "singername": str(_first(item, "singername", "SingerName") or ""),
            "album_id": "" if album_id is None else str(album_id),
            "album_audio_id": _first(item, "album_audio_id", "AlbumAudioID") or "",
            "duration": duration,
            "pay_type": _to_int(_first(item, "pay_type", "PayType")),
        })
    return songs


def _first(mapping: dict, *keys):
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != "":
            return value
    return None


def _to_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
