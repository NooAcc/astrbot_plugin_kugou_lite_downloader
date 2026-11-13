"""封面抓取与落盘缓存。

只做两件事：把搜索结果里的封面 URL 安全地取回本地、并缓存起来供卡片渲染复用。
**不做账号相关请求**：不携带 Cookie / token，只带 User-Agent。

安全与降级原则（与 ``file_cache.Downloader`` 一致）：
- URL 必须通过 ``file_cache.is_safe_url``：仅 http(s)、不含凭据、主机落在允许后缀内、
  解析结果必须是公网地址。
- **跟随重定向时逐跳重新校验**（``allow_redirects=False`` + 手动跳转），
  既不能被 302 绕过 SSRF 检查，也不因一律拒绝跳转而丢掉合法跳转。
- 大小、像素数、超时、并发都有上限。
- 任何失败（超时、非 2xx、超限、坏图、留不下盘）都收敛为 ``None``：
  封面缺失只该让那一格用占位图，不该让整张卡片失败。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from dataclasses import dataclass
from io import BytesIO
from urllib.parse import urljoin

try:
    from .file_cache import default_resolver, is_safe_url
except ImportError:  # 以顶层模块方式导入（离线测试）
    from file_cache import default_resolver, is_safe_url

try:
    from PIL import Image
except ImportError:  # pragma: no cover - Pillow 缺失时降级为「所有封面都取不到」
    Image = None  # type: ignore[assignment]

_REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})
_CHUNK_SIZE = 65536
_SUFFIX = ".img"
#: 封面只可能来自酷狗图床；比音频的允许后缀收得更紧。
ALLOWED_COVER_SUFFIXES: tuple[str, ...] = (".kugou.com",)


@dataclass(frozen=True)
class CoverLimits:
    """封面抓取的内部安全默认值（不作为插件配置项暴露）。"""

    max_bytes: int = 8 * 1024 * 1024
    timeout: float = 5.0
    batch_timeout: float = 8.0
    concurrency: int = 3
    max_redirects: int = 5
    max_pixels: int = 40_000_000


class CoverCache:
    """按 URL 哈希落盘的封面缓存；``ensure`` 失败返回 ``None``，从不抛错。"""

    def __init__(self, session, cache_dir: str, *, resolver=None, clock=None,
                 limits: CoverLimits | None = None,
                 allowed_suffixes: tuple[str, ...] = ALLOWED_COVER_SUFFIXES,
                 user_agent: str = ""):
        self._session = session
        self._dir = cache_dir
        self._resolver = resolver or default_resolver
        self._clock = clock or time.monotonic
        self.limits = limits or CoverLimits()
        self._allowed = tuple(allowed_suffixes)
        self._headers = {"User-Agent": user_agent} if user_agent else {}

    def path_for(self, url: str) -> str:
        """由 URL 派生的确定性本地路径（同一 URL 永远同一路径）。"""
        key = hashlib.sha1(str(url).encode("utf-8")).hexdigest()
        return os.path.join(self._dir, key + _SUFFIX)

    async def ensure(self, url: str) -> str | None:
        """命中缓存直接返回本地路径（不发请求）；否则抓取并落盘；失败返回 ``None``。"""
        if not isinstance(url, str) or not url:
            return None
        path = self.path_for(url)
        if os.path.isfile(path):
            return path
        try:
            return await self._fetch(url, path)
        except Exception:  # 网络/磁盘/解码——一律降级，不向上抛
            return None

    async def ensure_many(self, urls) -> dict[str, str]:
        """并发抓取多个封面，只返回成功项（键为原始 URL）。整批超时即放弃剩余。"""
        wanted = [u for u in urls if isinstance(u, str) and u]
        if not wanted:
            return {}
        semaphore = asyncio.Semaphore(max(1, self.limits.concurrency))

        async def one(target: str):
            async with semaphore:
                return await self.ensure(target)

        async def run() -> dict[str, str]:
            paths = await asyncio.gather(*(one(u) for u in wanted))
            return {u: p for u, p in zip(wanted, paths) if p}

        try:
            return await asyncio.wait_for(run(), self.limits.batch_timeout)
        except (asyncio.TimeoutError, TimeoutError):
            return {}

    # ── 内部 ─────────────────────────────────────────────────────────────

    async def _fetch(self, url: str, path: str) -> str | None:
        data = await self._download(url)
        if data is None:
            return None
        try:
            _verify_image(data, self.limits.max_pixels)
        except Exception:
            return None
        try:
            os.makedirs(self._dir, exist_ok=True)
            tmp = f"{path}.{os.getpid()}.part"
            with open(tmp, "wb") as handle:
                handle.write(data)
            os.replace(tmp, path)
        except OSError:
            return None
        return path

    async def _download(self, url: str) -> bytes | None:
        """取回封面字节；任何不合规都返回 ``None``。"""
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
                    if declared and str(declared).isdigit() and int(declared) > self.limits.max_bytes:
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
            except Exception:
                return None


def _verify_image(data: bytes, max_pixels: int) -> None:
    """确认字节确实是图片，且像素数不超上限（防解压炸弹）。"""
    if Image is None:
        raise RuntimeError("缺少 Pillow 依赖")
    with Image.open(BytesIO(data)) as image:
        width, height = image.size
    if width * height > max_pixels:
        raise ValueError("封面像素数超过上限")
