"""中文字体的远端获取与内容校验。

搜歌卡片的观感完全依赖 HarmonyOS Sans SC（Regular + Bold）：**每一个字号的字形、
每一处字宽都按它标定**，换成系统里别的中文字体，行宽与折行位置就都变了。

字体**不随插件分发**（两份共 16 MB，会让插件本身的更新包大得离谱），只有一条
来路：从约定的镜像取回，取回的字节按钉死的 ``sha256`` 与字节数校验，不匹配一律
丢弃。取不回来时由调用方回退到 AstrBot 默认字体，卡片会跑版但不会没有卡片。

约束（与 ``cover_cache`` / ``file_cache`` 同一套规矩）：

- 只走 ``font_store`` 自己声明的白名单后缀，且**逐跳重新校验重定向**——
  Gitee 的 raw 地址会 302 跳到 ``raw.giteeusercontent.com``，两个后缀缺一不可。
- 体积、超时、跳转次数都有上限；音频/封面的上限不适用于这里（字体本身就有 8 MB）。
- **先校验临时文件，再** ``os.replace``：坏字节永远不会出现在最终路径上。
- 任何失败都收敛为「没有这份字体」，交由调用方回退；绝不让搜歌失败。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
import uuid
from dataclasses import dataclass
from urllib.parse import urljoin

try:
    from .file_cache import default_resolver, is_safe_url
except ImportError:  # 以顶层模块方式导入（离线测试）
    from file_cache import default_resolver, is_safe_url

try:
    from .search_card import has_cjk_glyph, load_font
except ImportError:  # 以顶层模块方式导入（离线测试）
    from search_card import has_cjk_glyph, load_font

_REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})
_CHUNK_SIZE = 65536
_FONT_PROBE_SIZE = 24


@dataclass(frozen=True)
class FontSource:
    """一份可从镜像取回的字体：文件名、字节数与内容哈希，三者都必须对上。

    ``bold`` 只用来把候选拆成「正文列」与「粗体列」——渲染层查找新字体时要拿
    对应的字重，混在一起会让粗体槽位挑中常规字体，标题就没了字重对比。
    """

    filename: str
    sha256: str
    size: int
    bold: bool = False


#: 字体来路：Gitee 上的 HarmonyOS Sans 镜像。仓库打包是 MIT（© 2021-2022 IKKI2000），
#: 字体本体是华为 HarmonyOS Sans 免费可商用授权；两份说明见 ``LICENSES/``。
#:
#: ``sha256`` / ``size`` 是**内容钉**：镜像上的文件被替换、被截断、被投毒，都会在
#: 这里被挡下——字体不再随插件分发，这两项就是唯一的完整性锚点。要升级字体版本，
#: 必须同时改这两项，并确认镜像上那份新字节与之相符。
FONT_BASE_URL = "https://gitee.com/IKKI2000/harmonyos-fonts/raw/main/fonts/HarmonyOS_Sans_SC/"
ALLOWED_FONT_SUFFIXES: tuple[str, ...] = (".gitee.com", ".giteeusercontent.com")
FONT_SOURCES: tuple[FontSource, ...] = (
    FontSource(
        filename="HarmonyOS_Sans_SC_Regular.ttf",
        sha256="297b088424be212207df2ce8b98e335468b782aa6b96832af0b8b773d711e2b1",
        size=8261128,
    ),
    FontSource(
        filename="HarmonyOS_Sans_SC_Bold.ttf",
        sha256="43a424b85e47fb53a17b3b32026a71801f86f8e022ca6798d186b47d39fa5f01",
        size=8158996,
        bold=True,
    ),
)


def verify_font(path: str, source: FontSource) -> bool:
    """字节数、内容哈希、能否当字体加载、是否真的有汉字，四关全过才算数。"""
    try:
        if os.path.getsize(path) != source.size:
            return False
        if _sha256(path) != source.sha256:
            return False
    except OSError:
        return False
    try:
        return has_cjk_glyph(load_font(path, _FONT_PROBE_SIZE))
    except Exception:
        return False


def candidates_by_weight(
    fonts: dict[str, str], sources: tuple[FontSource, ...] = FONT_SOURCES
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """把 ``{文件名: 路径}`` 拆成 ``(正文字体候选, 粗体候选)``。

    顺序沿用 ``sources``（即 ``FONT_SOURCES``）的顺序，让渲染层的查找顺序稳定可预期。
    """
    regular: list[str] = []
    bold: list[str] = []
    for source in sources:
        path = fonts.get(source.filename)
        if not path:
            continue
        (bold if source.bold else regular).append(path)
    return tuple(regular), tuple(bold)


@dataclass(frozen=True)
class FontLimits:
    """取回路径的内部安全默认值（不作为插件配置项暴露）。"""

    max_bytes: int = 12 * 1024 * 1024
    timeout: float = 60.0
    max_redirects: int = 5


class FontStore:
    """字体取回：``ensure_all`` 失败返回空映射，从不抛错。"""

    def __init__(self, session, cache_dir: str, *, resolver=None, clock=None,
                 limits: FontLimits | None = None, user_agent: str = "",
                 sources: tuple[FontSource, ...] = FONT_SOURCES,
                 base_url: str = FONT_BASE_URL):
        self._session = session
        self._dir = cache_dir
        self._resolver = resolver or default_resolver
        self._clock = clock or time.monotonic
        self.limits = limits or FontLimits()
        self._sources = tuple(sources)
        self._base_url = base_url
        self._allowed = ALLOWED_FONT_SUFFIXES
        self._headers = {"User-Agent": user_agent} if user_agent else {}

    # ── 查询 ─────────────────────────────────────────────────────────────

    def path_for(self, filename: str) -> str:
        """字体在本地的固定路径（文件名来自 ``FONT_SOURCES``，不接受外部拼接）。"""
        return os.path.join(self._dir, os.path.basename(str(filename)))

    def url_for(self, source: FontSource) -> str:
        return urljoin(self._base_url, source.filename)

    def verify(self, path: str, source: FontSource) -> bool:
        """字节数、内容哈希、能否当字体加载、是否真的有汉字，四关全过才算数。"""
        return verify_font(path, source)

    def available(self) -> dict[str, str]:
        """已经就位且校验通过的字体：``{文件名: 本地路径}``。"""
        found: dict[str, str] = {}
        for source in self._sources:
            path = self.path_for(source.filename)
            if os.path.isfile(path) and self.verify(path, source):
                found[source.filename] = path
        return found

    # ── 取回 ─────────────────────────────────────────────────────────────

    async def ensure_all(self, sources: tuple[FontSource, ...] | None = None) -> dict[str, str]:
        """逐份取回指定字体（默认全部）；已就位的直接返回，不发请求。"""
        ensured: dict[str, str] = {}
        for source in (self._sources if sources is None else sources):
            path = await self.ensure_one(source)
            if path:
                ensured[source.filename] = path
        return ensured

    async def ensure_one(self, source: FontSource) -> str | None:
        path = self.path_for(source.filename)
        if os.path.isfile(path) and self.verify(path, source):
            return path
        try:
            data = await self._download(self.url_for(source))
        except Exception:  # 网络/磁盘——一律降级
            return None
        if data is None:
            return None
        return self._store(data, path, source)

    # ── 内部 ─────────────────────────────────────────────────────────────

    def _store(self, data: bytes, path: str, source: FontSource) -> str | None:
        try:
            os.makedirs(self._dir, exist_ok=True)
            # 临时名每次唯一：两个进程同时取回时若共用一个 .part，先落盘的那次会把
            # 文件搬走，后落盘的那次 os.replace 就找不到它了（Windows 上直接失败）。
            tmp = f"{path}.{os.getpid()}.{uuid.uuid4().hex}.part"
            with open(tmp, "wb") as handle:
                handle.write(data)
        except OSError:
            return None
        try:
            # 先验后换：坏字节绝不落到最终路径上
            if not self.verify(tmp, source):
                os.remove(tmp)
                return None
            os.replace(tmp, path)
        except OSError:
            _remove_quietly(tmp)
            return None
        return path

    async def _download(self, url: str) -> bytes | None:
        """取回字体字节；任何不合规都返回 ``None``。"""
        start = self._clock()
        current = url
        redirects = 0
        while True:
            safe, _reason = is_safe_url(
                current, resolver=self._resolver, allowed_suffixes=self._allowed
            )
            if not safe:
                return None
            try:
                ctx = self._session.get(
                    current,
                    headers=dict(self._headers),
                    allow_redirects=False,
                    timeout=self.limits.timeout,
                )
                async with ctx as resp:
                    status = resp.status
                    if status in _REDIRECT_STATUS:
                        location = resp.headers.get("Location", "")
                        if not location:
                            return None
                        redirects += 1
                        if redirects > self.limits.max_redirects:
                            return None
                        current = urljoin(current, location)
                        continue
                    if status != 200:
                        return None
                    declared = resp.headers.get("Content-Length")
                    if declared and str(declared).isdigit() \
                            and int(declared) > self.limits.max_bytes:
                        return None
                    buffer = bytearray()
                    async for chunk in resp.content.iter_chunked(_CHUNK_SIZE):
                        if not chunk:
                            continue
                        buffer.extend(chunk)
                        if len(buffer) > self.limits.max_bytes:
                            return None
                        if self._clock() - start > self.limits.timeout:
                            return None
                    return bytes(buffer) if buffer else None
            except (asyncio.TimeoutError, TimeoutError):
                return None
            except Exception:
                return None


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
