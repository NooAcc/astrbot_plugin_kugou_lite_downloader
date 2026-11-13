"""文件与缓存安全：文件名净化、URL 安全校验、受限下载、缓存归属与清理。

本模块不导入 astrbot，也不直接导入 aiohttp：HTTP 会话由调用方注入，
因此可以在没有网络的情况下完整测试。
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import stat
import time
import uuid
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

try:
    from .audio_probe import extension_of, probe, refine_bitrate, tier_of
    from .quality import label
except ImportError:  # 以顶层模块方式导入（离线测试）
    from audio_probe import extension_of, probe, refine_bitrate, tier_of
    from quality import label

# ── 常量 ─────────────────────────────────────────────────────────────────

#: 只接受来自这些酷狗 CDN 域名的音频地址。真实 CDN 若使用其他域名，
#: 会得到一条点名域名的错误，管理员按提示扩充本表即可。
ALLOWED_HOST_SUFFIXES: tuple[str, ...] = (".kugou.com", ".kglink.com", ".kgidc.cn")

MAX_FILENAME_LEN = 120

_ILLEGAL_CHARS = '<>:"/\\|?*'
_CONTROL_LIMIT = " "
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
_WHITESPACE_RE = re.compile(r"\s+")
_FALLBACK_NAME = "song"
_MAX_EXT_LEN = 8


# ── 异常 ─────────────────────────────────────────────────────────────────


class SharedDirError(Exception):
    """共享缓存目录不可用（未配置、非法、或不是本插件创建的目录）。"""


class DownloadError(Exception):
    """下载失败。`reason` 是可直接展示给用户的原因。"""

    def __init__(self, reason: str, *, url: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.url = url


class UnsafeUrlError(DownloadError):
    """地址不安全（协议、凭据、内网/回环/保留地址、未知来源）。

    这类错误说明资源解析结果不可信或配置有问题，**不得**当作
    「音质不可用」继续降级。
    """


# ── 文件名安全 ───────────────────────────────────────────────────────────


def sanitize_filename(name: str, max_len: int = MAX_FILENAME_LEN) -> str:
    """把任意文本净化为单个安全的文件名（不含任何路径成分）。

    - 控制字符直接删除；制表符/换行折叠为空格。
    - 路径分隔符与 Windows 非法字符 `<>:"/\\|?*` 替换为 `_`，因此
      结果永远不可能包含目录成分。
    - 去尾部空格与点、避开 Windows 保留设备名、按 `max_len` 截断（保留扩展名）。
    """
    text = "".join(_CONTROL_LIMIT if ch in "\t\r\n" else ch for ch in str(name or ""))
    text = "".join(ch for ch in text if ch >= " " and ch != "\x7f")
    for ch in _ILLEGAL_CHARS:
        text = text.replace(ch, "_")
    text = _WHITESPACE_RE.sub(" ", text).strip()
    text = text.rstrip(" .")

    stem, ext = _split_extension(text)
    if stem.upper() in _RESERVED_NAMES:
        stem = "_" + stem

    if len(stem) + len(ext) > max_len:
        stem = stem[: max(0, max_len - len(ext))].rstrip(" .")

    if not stem:
        return _FALLBACK_NAME + ext
    return stem + ext


def build_filename(artist: str, title: str, tier: str, ext: str) -> str:
    """`歌手 - 歌名 [实际音质].扩展名`（扩展名以实际音频格式为准）。"""
    stem = f"{sanitize_filename(artist)} - {sanitize_filename(title)} [{label(tier)}]"
    name = f"{stem}.{ext}" if ext else stem
    return sanitize_filename(name)


def _split_extension(text: str) -> tuple[str, str]:
    """把 `名字.ext` 拆开；扩展名必须是短且纯字母数字，避免误切 `.._.._x`。"""
    idx = text.rfind(".")
    if 0 < idx < len(text) - 1:
        ext = text[idx + 1 :]
        if len(ext) <= _MAX_EXT_LEN and ext.isalnum():
            return text[:idx], text[idx:]
    return text, ""


# ── URL 安全 ─────────────────────────────────────────────────────────────


def default_resolver(host: str) -> list[str]:
    """把主机名解析成 IP 列表（供 SSRF 校验）。"""
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return [info[4][0] for info in infos]


def is_safe_url(
    url: str,
    resolver=None,
    allowed_suffixes: tuple[str, ...] = ALLOWED_HOST_SUFFIXES,
) -> tuple[bool, str]:
    """校验音频地址是否可安全请求，返回 `(是否安全, 原因)`。

    规则：仅 http/https；不含凭据；主机必须是 IP 字面量或落在酷狗 CDN
    允许后缀内（点边界匹配）；解析结果与 IP 字面量都必须是公网地址。
    """
    resolver = resolver or default_resolver

    try:
        parts = urlsplit(url)
    except ValueError:
        return False, f"无法解析的地址：{url[:80]}"

    if parts.scheme not in ("http", "https"):
        return False, f"不支持的协议：{parts.scheme or '(空)'}"

    if "@" in parts.netloc:
        return False, "地址中包含凭据信息"

    try:
        host = (parts.hostname or "").strip().lower()
        port = parts.port
    except ValueError:
        return False, "地址端口非法"

    if not host:
        return False, "地址缺少主机名"
    if port is not None and not 0 < port < 65536:
        return False, f"地址端口非法：{port}"

    if _as_ip(host) is not None:
        # 资源地址必须来自酷狗域名的解析结果；IP 直连地址无法归属到已知来源
        return False, f"音频地址不得使用 IP 直连（未知来源）：{host}"

    if not _host_allowed(host, allowed_suffixes):
        return False, (
            f"主机不在酷狗 CDN 允许列表：{host}"
            f"（如为酷狗新域名，请扩充 ALLOWED_HOST_SUFFIXES）"
        )

    try:
        addresses = resolver(host)
    except Exception as exc:  # DNS 失败、超时等
        return False, f"域名解析失败：{host}（{type(exc).__name__}）"

    if not addresses:
        return False, f"域名无解析结果：{host}"

    for address in addresses:
        resolved = _as_ip(address)
        if resolved is None:
            return False, f"域名解析到无法识别的地址：{host} → {address}"
        if not _is_global_ip(resolved):
            return False, f"域名解析到非公网地址：{host} → {address}"

    return True, ""


def _host_allowed(host: str, allowed_suffixes: tuple[str, ...]) -> bool:
    return any(host == suffix.lstrip(".") or host.endswith(suffix) for suffix in allowed_suffixes)


def _as_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    text = str(value).strip()
    if not text:
        return None
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def _is_global_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # IPv4-mapped IPv6（::ffff:127.0.0.1）按被映射的 IPv4 判定。
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return bool(address.is_global)


# ── 受限流式下载 ─────────────────────────────────────────────────────────

_CHUNK_SIZE = 65536
_REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})


@dataclass(frozen=True)
class Limits:
    """下载与并发的内部安全默认值（不作为插件配置项暴露）。"""

    max_bytes: int = 200 * 1024 * 1024
    total_seconds: float = 180.0
    max_concurrency: int = 2
    max_redirects: int = 5
    max_url_attempts: int = 2
    #: 探测音频头所需的字节数。MP3 的 ID3v2 标签（封面等）可能很大，
    #: 头部帧同步点在标签之后，因此需要读出足够多才能看到帧头。
    head_bytes: int = 1024 * 1024


@dataclass(frozen=True)
class DownloadOutcome:
    head: bytes
    size: int
    url: str
    content_type: str = ""


class Downloader:
    """把一段音频流式写入 `.part` 文件，并强制大小/时间/长度/地址安全约束。

    会话由调用方注入（保持本模块与 aiohttp 解耦），请求**不携带**酷狗
    Cookie 或 token，避免把账号凭据泄漏给 CDN。
    """

    def __init__(self, session, limits: Limits | None = None, resolver=None,
                 user_agent: str = "", clock=None):
        self._session = session
        self.limits = limits or Limits()
        self._resolver = resolver or default_resolver
        self._clock = clock or time.monotonic
        self._headers = {"User-Agent": user_agent} if user_agent else {}

    async def download_part(self, urls: list[str], part_path: str) -> DownloadOutcome:
        """依次尝试备用 CDN 地址（最多 `max_url_attempts` 个）。

        地址不安全会立即中止：这说明资源解析结果不可信或配置有问题，
        不应伪装成普通的传输失败继续换地址。
        """
        last_error: DownloadError | None = None
        attempts = 0
        for url in urls:
            if attempts >= self.limits.max_url_attempts:
                break
            attempts += 1
            try:
                return await self._download_one(url, part_path)
            except UnsafeUrlError:
                self._discard(part_path)
                raise
            except DownloadError as exc:
                self._discard(part_path)
                last_error = exc

        if last_error is None:
            raise DownloadError("没有可用的下载地址")
        raise last_error

    async def _download_one(self, url: str, part_path: str) -> DownloadOutcome:
        start = self._clock()
        current = url
        redirects = 0

        while True:
            safe, reason = is_safe_url(current, resolver=self._resolver)
            if not safe:
                raise UnsafeUrlError(reason, url=current)

            try:
                ctx = self._session.get(
                    current,
                    headers=dict(self._headers),
                    allow_redirects=False,
                    timeout=self.limits.total_seconds,
                )
                async with ctx as resp:
                    status = resp.status
                    if status in _REDIRECT_STATUS:
                        location = resp.headers.get("Location", "")
                        if not location:
                            raise DownloadError(f"重定向缺少 Location（HTTP {status}）", url=current)
                        redirects += 1
                        if redirects > self.limits.max_redirects:
                            raise DownloadError("重定向次数过多", url=current)
                        current = urljoin(current, location)
                        continue
                    if status != 200:
                        raise DownloadError(f"服务端返回 HTTP {status}", url=current)
                    return await self._stream(resp, current, part_path, start)
            except (UnsafeUrlError, DownloadError, SharedDirError):
                raise
            except Exception as exc:  # 连接中断、DNS、超时等
                if _is_network_error(exc):
                    raise DownloadError(f"连接失败：{type(exc).__name__}", url=current) from exc
                raise

    async def _stream(self, resp, url: str, part_path: str, start: float) -> DownloadOutcome:
        declared = resp.headers.get("Content-Length")
        total = int(declared) if declared and str(declared).isdigit() else None
        if total is not None and total > self.limits.max_bytes:
            raise DownloadError(
                f"文件超过单次下载上限（{_mib(self.limits.max_bytes)}）", url=url
            )

        try:
            _ensure_parent(part_path)
        except OSError as exc:
            # 本地目录问题（磁盘满、路径被文件占用等）不是网络故障，
            # 不得被降级为「换一个 CDN 地址重试」或伪装成连接失败。
            raise SharedDirError(
                f"无法创建下载目录：{os.path.dirname(os.path.abspath(part_path))}"
                f"（{type(exc).__name__}）"
            ) from exc
        head = bytearray()
        size = 0
        try:
            try:
                handle = open(part_path, "wb")
            except OSError as exc:
                raise SharedDirError(
                    f"无法写入下载文件：{part_path}（{type(exc).__name__}）"
                ) from exc
            with handle:
                async for chunk in resp.content.iter_chunked(_CHUNK_SIZE):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > self.limits.max_bytes:
                        raise DownloadError(
                            f"文件超过单次下载上限（{_mib(self.limits.max_bytes)}）", url=url
                        )
                    if self._clock() - start > self.limits.total_seconds:
                        raise DownloadError("下载超时", url=url)
                    if len(head) < self.limits.head_bytes:
                        head.extend(chunk[: self.limits.head_bytes - len(head)])
                    try:
                        handle.write(chunk)
                    except OSError as exc:
                        raise SharedDirError(
                            f"无法写入下载文件：{part_path}（{type(exc).__name__}）"
                        ) from exc
        except BaseException:
            # 失败或取消都要清掉当前任务的半成品
            self._discard(part_path)
            raise

        if size == 0:
            self._discard(part_path)
            raise DownloadError("下载内容为空", url=url)
        if total is not None and size != total:
            self._discard(part_path)
            raise DownloadError("下载不完整（与 Content-Length 不符）", url=url)

        return DownloadOutcome(
            head=bytes(head),
            size=size,
            url=url,
            content_type=resp.headers.get("Content-Type", ""),
        )

    @staticmethod
    def _discard(part_path: str) -> None:
        try:
            os.remove(part_path)
        except OSError:
            pass


def _is_network_error(exc: BaseException) -> bool:
    if isinstance(exc, OSError):        # 含 ConnectionResetError、TimeoutError 等
        return True
    module = type(exc).__module__ or ""
    return module.startswith("aiohttp") and isinstance(exc, Exception)


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def _mib(value: int) -> str:
    return f"{value / 1024 / 1024:.0f} MiB"


# ── 缓存目录归属与生命周期 ───────────────────────────────────────────────

MANIFEST_NAME = "manifest.json"
MANIFEST = {"plugin": "astrbot_plugin_kugou_music", "version": 1}
CACHE_DIRNAME = "cache"
TMP_DIRNAME = "tmp"
_TMP_TTL_SECONDS = 300


def _norm_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _cache_key(resource_key: str, tier: str) -> str:
    return f"{resource_key}__{tier}"


def _is_reparse_point(path: str) -> bool:
    """是否为符号链接 / Windows 联接点（junction）。

    注意 Windows 的 junction 在 `os.path.islink()` 与
    `DirEntry.is_dir(follow_symlinks=False)` 下都表现为**普通目录**，
    因此必须显式用 lstat 的 `st_reparse_tag` 判定，否则清理会进入
    链接目标 —— 那可能删除共享目录之外的文件。`shutil.rmtree` 是否
    纵深进入取决于 Python 版本，不能依赖。
    """
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool(getattr(info, "st_reparse_tag", 0))


class CacheManager:
    """共享目录下插件专属子目录的归属、存储、复用与清理。

    只处理 `<shared_root>/cache/<resource_key>__<tier>/` 与
    `<shared_root>/tmp/<task>/`，绝不触碰共享目录里的其他内容。
    """

    def __init__(self, shared_root: str):
        self._raw = shared_root or ""
        self.root = os.path.abspath(shared_root) if shared_root else ""
        self.cache_dir = os.path.join(self.root, CACHE_DIRNAME) if self.root else ""
        self.tmp_dir = os.path.join(self.root, TMP_DIRNAME) if self.root else ""
        self._held: set[str] = set()
        self._ready = False

    def ensure_ready(self) -> None:
        """创建并校验目录归属；不可用时抛 `SharedDirError`。"""
        if self._ready:
            return
        if not self._raw or not os.path.isabs(self._raw):
            raise SharedDirError(
                f"共享缓存目录必须是绝对路径：{self._raw or '(空)'}"
            )

        if os.path.isdir(self.root):
            manifest = os.path.join(self.root, MANIFEST_NAME)
            if not os.path.isfile(manifest):
                raise SharedDirError(
                    f"目录 {self.root} 已存在但不是本插件创建的（缺少 {MANIFEST_NAME}）。"
                    f"请改用一个新目录，或手动清理该目录后再试。"
                )
        else:
            try:
                os.makedirs(self.root, exist_ok=True)
            except OSError as exc:
                raise SharedDirError(f"无法创建共享缓存目录 {self.root}：{exc}") from exc

        self._write_manifest()
        self._check_writable()
        for sub in (self.cache_dir, self.tmp_dir):
            # 子目录本身若是符号链接/junction，清理会进入链接目标，
            # 从而删除插件目录之外的内容 —— 必须在可用之前就拒绝。
            if os.path.exists(sub) and _is_reparse_point(sub):
                raise SharedDirError(
                    f"缓存子目录 {sub} 是符号链接或 junction，指向共享目录之外的"
                    f"位置：请删除该链接后重试。"
                )
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            os.makedirs(self.tmp_dir, exist_ok=True)
        except OSError as exc:
            raise SharedDirError(f"无法创建缓存子目录：{exc}") from exc
        self._ready = True

    @staticmethod
    def resource_key(song: dict) -> str:
        """歌曲在缓存中的稳定标识（`hash` + `album_audio_id`）。

        同一首歌在不同的搜索会话里可能带不同的序号/字段，但这两个标识
        稳定不变，因此用它做缓存键，跨会话也能复用。
        """
        raw = f"{song.get('hash') or ''}:{song.get('album_audio_id') or ''}".lower()
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def _write_manifest(self) -> None:
        path = os.path.join(self.root, MANIFEST_NAME)
        if os.path.isfile(path):
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(
                    {**MANIFEST, "created_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                    handle,
                    ensure_ascii=False,
                )
        except OSError as exc:
            raise SharedDirError(f"无法写入归属标记 {path}：{exc}") from exc

    def _check_writable(self) -> None:
        probe_path = os.path.join(self.root, ".write-probe")
        try:
            with open(probe_path, "w", encoding="utf-8") as handle:
                handle.write("")
            os.remove(probe_path)
        except OSError as exc:
            raise SharedDirError(f"共享缓存目录不可写：{self.root}（{exc}）") from exc

    def begin_task_dir(self) -> str:
        """为一次下载任务创建独立目录。"""
        self.ensure_ready()
        path = os.path.join(self.tmp_dir, uuid.uuid4().hex)
        os.makedirs(path, exist_ok=True)
        return path

    def cached_path(self, resource_key: str, tier: str) -> str | None:
        """返回该资源在该档位下已入库的文件路径（可能存在但内容已损坏）。"""
        self.ensure_ready()
        directory = os.path.join(self.cache_dir, _cache_key(resource_key, tier))
        if not os.path.isdir(directory):
            return None
        newest: str | None = None
        newest_mtime = -1.0
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.name.endswith(".part") or not entry.is_file(follow_symlinks=False):
                        continue
                    try:
                        mtime = entry.stat(follow_symlinks=False).st_mtime
                    except OSError:
                        continue
                    if mtime > newest_mtime:
                        newest, newest_mtime = entry.path, mtime
        except OSError:
            return None
        return newest

    def store(self, part_path: str, resource_key: str, tier: str, filename: str) -> str:
        """把校验通过的半成品原子地移入缓存（`os.replace`，无 `.part` 残留）。"""
        self.ensure_ready()
        directory = os.path.join(self.cache_dir, _cache_key(resource_key, tier))
        os.makedirs(directory, exist_ok=True)
        final = os.path.join(directory, sanitize_filename(filename))
        os.replace(part_path, final)
        return final

    def discard_entry(self, resource_key: str, tier: str) -> None:
        """丢弃损坏或档位不符的缓存条目（被持有或是目录链接时跳过）。"""
        directory = os.path.join(self.cache_dir, _cache_key(resource_key, tier))
        if _norm_path(directory) in self._held:
            return
        if _is_reparse_point(directory):
            return
        shutil.rmtree(directory, ignore_errors=True)

    def hold(self, path: str) -> None:
        """标记文件（及其所属缓存目录）在使用中，清理时跳过。"""
        self._held.add(_norm_path(path))
        self._held.add(_norm_path(os.path.dirname(path)))

    def release(self, path: str) -> None:
        self._held.discard(_norm_path(path))
        parent = _norm_path(os.path.dirname(path))
        if not any(entry.startswith(parent + os.sep) for entry in self._held):
            self._held.discard(parent)

    def cleanup(self, ttl_seconds: int = 3600) -> int:
        """清理过期的任务目录与缓存目录，返回删除数量。

        只扫描 `cache/` 与 `tmp/` 的一级子目录，不跟随符号链接，
        不递归进出共享目录的其他内容；被 `hold` 的目录跳过。
        """
        self.ensure_ready()
        now = time.time()
        removed = 0
        for base, ttl in ((self.tmp_dir, _TMP_TTL_SECONDS), (self.cache_dir, ttl_seconds)):
            if not os.path.isdir(base):
                continue
            candidates: list[tuple[str, float]] = []
            try:
                with os.scandir(base) as entries:
                    for entry in entries:
                        # 不跟随符号链接与 junction：既不删除链接本身，更不进入其目标
                        if entry.is_symlink() or _is_reparse_point(entry.path):
                            continue
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                        try:
                            candidates.append((entry.path, entry.stat(follow_symlinks=False).st_mtime))
                        except OSError:
                            continue
            except OSError:
                continue
            for path, mtime in candidates:
                if _norm_path(path) in self._held:
                    continue
                if now - mtime <= ttl:
                    continue
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        return removed


def prune_expired_entries(base_dir: str, ttl_seconds: float, *, clock=None) -> int:
    """删除 `base_dir` 一级下 `mtime` 超过 TTL 的文件或目录，返回删除数量。

    与 `CacheManager.cleanup` 同一套扫描规则：只扫一级、跳过符号链接与
    reparse point（junction），不递归进出其它内容。目录不存在视为无事可做。
    """
    if not os.path.isdir(base_dir):
        return 0
    now = (clock or time.time)()
    removed = 0
    try:
        entries = list(os.scandir(base_dir))
    except OSError:
        return 0
    for entry in entries:
        try:
            if entry.is_symlink() or _is_reparse_point(entry.path):
                continue
            if now - entry.stat(follow_symlinks=False).st_mtime <= ttl_seconds:
                continue
        except OSError:
            continue
        try:
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path, ignore_errors=True)
            else:
                os.remove(entry.path)
        except OSError:
            continue
        removed += 1
    return removed


# ── 下载 + 探针校验 + 缓存复用 ───────────────────────────────────────────


@dataclass(frozen=True)
class FetchResult:
    """一次「取得可用音频文件」的结果。

    `actual_tier` 是**实测**档位（由音频头判定），不是请求档位。
    `path` 是磁盘上的缓存路径；用户可见文件名由上层用
    `build_filename()` 生成并在上传时传入。
    """

    path: str
    actual_tier: str
    extension: str
    info: object
    url: str
    size: int
    reused: bool


class AudioFetcher:
    """受限并发地下载音频、用音频头校验、并按实测档位入库。"""

    def __init__(self, downloader: Downloader, cache: CacheManager, limits: Limits | None = None):
        self._downloader = downloader
        self._cache = cache
        self.limits = limits or downloader.limits
        self._semaphore: asyncio.Semaphore | None = None
        self._inflight: dict[str, asyncio.Future] = {}

    async def cached(self, resource_key: str, tier: str) -> FetchResult | None:
        """读取已验证缓存；内容损坏或实测档位不符时丢弃该条目并返回 None。"""
        path = self._cache.cached_path(resource_key, tier)
        if not path:
            return None
        info = self._probe_file(path)
        if not info.valid or tier_of(info) != tier:
            self._cache.discard_entry(resource_key, tier)
            return None
        self._cache.hold(path)
        return FetchResult(
            path=path,
            actual_tier=tier,
            extension=extension_of(info),
            info=info,
            url="",
            size=os.path.getsize(path),
            reused=True,
        )

    async def fetch(self, resource_key: str, requested_tier: str, urls: list[str]) -> FetchResult:
        """下载并校验。同一 `(资源, 请求档位)` 的并发调用会合并为一次下载。"""
        key = f"{resource_key}::{requested_tier}"
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(self._fetch_once(resource_key, requested_tier, urls))
            self._inflight[key] = task
            task.add_done_callback(lambda done, k=key: self._forget(k, done))
        return await asyncio.shield(task)

    def _forget(self, key: str, task: asyncio.Future) -> None:
        self._inflight.pop(key, None)
        if not task.cancelled():
            task.exception()      # 标记已取用，避免「未取用异常」告警

    def _probe_file(self, path: str):
        try:
            with open(path, "rb") as handle:
                head = handle.read(self.limits.head_bytes)
            return refine_bitrate(probe(head), os.path.getsize(path))
        except OSError:
            return probe(b"")

    async def _fetch_once(self, resource_key: str, requested_tier: str,
                          urls: list[str]) -> FetchResult:
        if self._semaphore is None:
            # 延迟到运行中的事件循环里创建，兼容 Python 3.9 的循环绑定
            self._semaphore = asyncio.Semaphore(self.limits.max_concurrency)
        async with self._semaphore:
            task_dir = self._cache.begin_task_dir()
            part_path = os.path.join(task_dir, "audio.part")
            try:
                outcome = await self._downloader.download_part(urls, part_path)
                info = refine_bitrate(probe(outcome.head), outcome.size)
                if not info.valid:
                    raise DownloadError("服务端返回的不是可识别的音频文件")
                tier = tier_of(info)
                if not tier:
                    raise DownloadError("音频规格无法识别或低于最低档位")
                extension = extension_of(info)
                final = self._cache.store(
                    part_path, resource_key, tier, f"{resource_key}__{tier}.{extension}"
                )
            except BaseException:
                shutil.rmtree(task_dir, ignore_errors=True)
                raise
            shutil.rmtree(task_dir, ignore_errors=True)
            self._cache.hold(final)
            return FetchResult(
                path=final,
                actual_tier=tier,
                extension=extension,
                info=info,
                url=outcome.url,
                size=outcome.size,
                reused=False,
            )
