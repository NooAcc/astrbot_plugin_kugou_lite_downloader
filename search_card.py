"""搜歌结果卡片：用 Pillow 在本地画一张图片。

**不导入 astrbot、不联网**：封面由调用方先下载好，通过 ``covers`` 传入本模块。
这样卡片渲染可以在纯离线环境下测试，也不会把网络失败与绘制失败混在一起。

不可渲染时（缺 Pillow、找不到含中文字形的字体、没有可渲染的条目）抛
``CardRenderError``，由调用方决定回退到纯文本列表。

绘制是 CPU 密集的同步操作，公开入口用 ``asyncio.to_thread`` 放到线程里执行。
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from typing import Any, Sequence

try:  # Pillow 是唯一硬依赖
    from PIL import Image, ImageDraw, ImageFont, ImageOps
except ImportError:  # pragma: no cover - 缺失时由 render 抛 CardRenderError
    Image = ImageDraw = ImageFont = ImageOps = None  # type: ignore[assignment]

# ── 版式基线（与设计文档 §4 一致） ───────────────────────────────────────

WIDTH = 800
PAD = 32
COVER = 56
COVER_RADIUS = 8
IDX = 24
IDX_RADIUS = 8
ROW_PAD_Y = 8
HEADER_H = 74
FOOTER_H = 48

INK = (26, 28, 30)
MUTED = (138, 144, 151)
LINE = (241, 242, 243)
ROW_LINE = (247, 248, 249)
FOOTER_BG = (250, 251, 251)
ACCENT = (47, 134, 189)
PLACEHOLDER_TOP = (238, 240, 242)
PLACEHOLDER_BOTTOM = (228, 231, 234)

#: 正文字体候选（按优先级；必须含中文字形，否则视为不可用）。
FONT_CANDIDATES: tuple[str, ...] = (
    "C:/Windows/Fonts/msyh.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
)
#: 粗体候选；找不到时回退到正文字体。
FONT_CANDIDATES_BOLD: tuple[str, ...] = (
    "C:/Windows/Fonts/msyhbd.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
)
#: 调用方额外提供的候选（如 AstrBot 数据目录下的 ``font.ttf``）：**追加在系统候选之后**，
#: 只作回退——系统自带字体优先（设计文档 §「字体发现顺序」）。

#: 探测中文字形覆盖用的字符：一个常用汉字，与一个必然缺字的私用区码位。
_PROBE_HAN = "永"
_PROBE_MISSING = "\ue000"


class CardRenderError(Exception):
    """卡片无法渲染（缺依赖、缺中文字体、没有可渲染内容）。"""


# ── 字体 ─────────────────────────────────────────────────────────────────

_font_path_cache: dict[tuple[str, ...], str | None] = {}


def _has_cjk_glyph(font: Any) -> bool:
    """该字体是否真的画得出汉字（而不只是能加载）。"""
    try:
        han = font.getbbox(_PROBE_HAN)
        missing = font.getbbox(_PROBE_MISSING)
    except Exception:
        return False
    if han == missing:          # 汉字与缺字码位渲染成同一个字形 → 豆腐块
        return False
    return (han[2] - han[0]) > 0 and (han[3] - han[1]) > 0


def _find_font_path(candidates: Sequence[str]) -> str | None:
    key = tuple(candidates)
    if key in _font_path_cache:
        return _font_path_cache[key]
    found: str | None = None
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            probe = ImageFont.truetype(path, 24)
        except Exception:
            continue
        if _has_cjk_glyph(probe):
            found = path
            break
    _font_path_cache[key] = found
    return found


def cjk_font_available(extra_candidates: tuple[str, ...] = ()) -> bool:
    """是否存在可用的中文字体（正文或粗体任一即可）。"""
    if ImageFont is None:
        return False
    return bool(
        _find_font_path(FONT_CANDIDATES + tuple(extra_candidates))
        or _find_font_path(FONT_CANDIDATES_BOLD + tuple(extra_candidates))
    )


class _Fonts:
    """按需加载并缓存字号。"""

    def __init__(self, extra: tuple[str, ...]):
        regular = _find_font_path(FONT_CANDIDATES + tuple(extra))
        bold = _find_font_path(FONT_CANDIDATES_BOLD + tuple(extra)) or regular
        if regular is None and bold is None:
            raise CardRenderError("找不到含中文字形的字体，无法渲染卡片")
        self._regular = regular or bold
        self._bold = bold or regular
        self._cache: dict[tuple[int, bool], Any] = {}

    def get(self, size: int, *, bold: bool = False):
        key = (size, bold)
        if key not in self._cache:
            path = self._bold if bold else self._regular
            self._cache[key] = ImageFont.truetype(path, size)
        return self._cache[key]


# ── 文本工具 ─────────────────────────────────────────────────────────────

def safe_text(value: Any, limit: int = 200) -> str:
    """去掉控制字符与首尾空白，并截断到 limit 个字符。"""
    text = "" if value is None else str(value)
    text = "".join(ch for ch in text if ch == " " or ch.isprintable()).strip()
    return text[:limit]


def _ellipsize(draw, text: str, font, max_w: float) -> str:
    if max_w <= 0 or not text:
        return ""
    if draw.textlength(text, font=font) <= max_w:
        return text
    ellipsis = "…"
    for end in range(len(text) - 1, 0, -1):
        candidate = text[:end] + ellipsis
        if draw.textlength(candidate, font=font) <= max_w:
            return candidate
    return ellipsis


def _wrap(draw, text: str, font, max_w: float, max_lines: int) -> list[str]:
    """按宽度折行（逐字符，兼容中英文混排），最多 max_lines 行，末行省略。

    ``max_lines <= 0`` 表示一行都不排，返回空列表（与 ``_ellipsize`` 对 ``max_w <= 0``
    的处理一致），而不是让后面的「末行加省略号」去索引空列表。
    """
    if not text or max_lines <= 0:
        return []
    lines: list[str] = []
    current = ""
    for ch in text:
        if draw.textlength(current + ch, font=font) <= max_w:
            current += ch
            continue
        if current:
            lines.append(current)
        current = ch
        if len(lines) == max_lines:
            break
    if len(lines) < max_lines and current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
    # 还有没放下的内容 → 末行加省略号
    rendered = "".join(lines)
    if len(rendered) < len(text):
        lines[-1] = _ellipsize(draw, lines[-1] + text[len(rendered):], font, max_w)
    return lines


def format_duration(seconds: Any) -> str:
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    minutes, secs = divmod(total, 60)
    return f"{minutes}:{secs:02d}"


# ── 图片工具 ─────────────────────────────────────────────────────────────

def _rounded(image, radius: int):
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, image.size[0] - 1, image.size[1] - 1), radius=radius, fill=255
    )
    out = image.copy()
    out.putalpha(mask)
    return out


def _placeholder(size: int):
    top = Image.new("RGBA", (size, size), PLACEHOLDER_TOP + (255,))
    bottom = Image.new("RGBA", (size, size), PLACEHOLDER_BOTTOM + (255,))
    mask = Image.linear_gradient("L").resize((size, size))
    tile = Image.composite(bottom, top, mask)
    draw = ImageDraw.Draw(tile)
    center = size // 2
    draw.ellipse((center - 9, center - 9, center + 9, center + 9),
                 outline=ACCENT + (110,), width=2)
    draw.ellipse((center - 2, center - 2, center + 2, center + 2), fill=ACCENT + (110,))
    return tile


def _cover_tile(path: Any, size: int):
    """读入封面并裁剪成方形圆角；任何失败返回 None（由调用方画占位）。"""
    if not path:
        return None
    try:
        with Image.open(path) as image:
            image.load()
            fitted = ImageOps.fit(image.convert("RGBA"), (size, size))
    except Exception:
        return None
    return _rounded(fitted, COVER_RADIUS)


# ── 绘制 ─────────────────────────────────────────────────────────────────

def _render_sync(songs: Sequence[Any], keyword: str, output_dir: str,
                 options: dict) -> str:
    if Image is None or ImageDraw is None:
        raise CardRenderError("缺少 Pillow 依赖")

    rows = [s for s in songs if isinstance(s, dict)]
    if not rows:
        raise CardRenderError("没有可渲染的歌曲")

    width = int(options.get("width") or WIDTH)
    strips = [safe_text(s.get("songname"), 120) or "未知歌曲" for s in rows]
    covers = options.get("covers") or {}
    command = safe_text(options.get("command") or "/酷狗下载", 20) or "/酷狗下载"

    fonts = _Fonts(tuple(options.get("extra_font_candidates") or ()))
    f_head = fonts.get(20, bold=True)
    f_head_sub = fonts.get(13)
    f_title = fonts.get(15, bold=True)
    f_sub = fonts.get(13)
    f_dur = fonts.get(13)
    f_idx = fonts.get(12, bold=True)
    f_foot = fonts.get(13)

    # 高度预算：先算清再开画布
    line_h = 15 + 4
    block_h = 2 * line_h + 6 + (13 + 6)
    row_h = max(COVER, block_h) + ROW_PAD_Y * 2
    total_h = HEADER_H + row_h * len(rows) + FOOTER_H

    canvas = Image.new("RGBA", (width, total_h), (255, 255, 255, 255))
    draw = ImageDraw.Draw(canvas)

    # 页眉
    draw.text((PAD, 22), _ellipsize(draw, f"「{safe_text(keyword, 60)}」搜索结果",
                                    f_head, width - PAD * 2 - 90),
              font=f_head, fill=INK)
    count = f"共 {len(rows)} 首"
    draw.text((width - PAD - draw.textlength(count, font=f_head_sub),
               26 + 7), count, font=f_head_sub, fill=MUTED)
    draw.line((PAD, HEADER_H - 1, width - PAD, HEADER_H - 1), fill=LINE, width=1)

    # 列表
    y = HEADER_H
    for index, (song, title) in enumerate(zip(rows, strips), start=1):
        row_top = y
        cover_x = PAD
        cover_y = row_top + (row_h - COVER) // 2

        tile = _cover_tile(covers.get(song.get("cover")), COVER)
        if tile is None:
            tile = _placeholder(COVER)
        canvas.alpha_composite(tile, (cover_x, cover_y))

        # 序号徽章：压在封面左下角
        bx = cover_x + 6
        by = cover_y + COVER - 6
        draw.rounded_rectangle((bx, by - IDX, bx + IDX, by), radius=IDX_RADIUS, fill=ACCENT)
        num = str(index)
        draw.text((bx + IDX / 2 - draw.textlength(num, font=f_idx) / 2,
                   by - IDX + (IDX - 12) / 2 - 1), num, font=f_idx, fill=(255, 255, 255))

        text_x = cover_x + COVER + 14
        duration = format_duration(song.get("duration"))
        right_w = (draw.textlength(duration, font=f_dur) + 12) if duration else 0
        text_w = width - PAD - text_x - right_w

        block_top = cover_y + max(0, (COVER - block_h) // 2)
        ty = block_top
        for line in _wrap(draw, title, f_title, text_w, 2):
            draw.text((text_x, ty), line, font=f_title, fill=INK)
            ty += line_h

        artist = safe_text(song.get("singername"), 60)
        album = safe_text(song.get("album"), 60)
        meta = " · ".join(part for part in (artist, album) if part)
        if meta:
            draw.text((text_x, ty + 6), _ellipsize(draw, meta, f_sub, text_w),
                      font=f_sub, fill=MUTED)

        if duration:
            draw.text((width - PAD - draw.textlength(duration, font=f_dur),
                       block_top), duration, font=f_dur, fill=MUTED)

        y += row_h
        if index < len(rows):
            draw.line((PAD, y - 1, width - PAD, y - 1), fill=ROW_LINE, width=1)

    # 页脚
    footer_top = total_h - FOOTER_H
    draw.rectangle((0, footer_top, width, total_h), fill=FOOTER_BG)
    draw.line((0, footer_top, width, footer_top), fill=LINE, width=1)
    draw.text((PAD, footer_top + 16),
              f"发送 {command} 序号 获取歌曲文件", font=f_foot, fill=MUTED)

    os.makedirs(output_dir, exist_ok=True)
    name = f"search_card_{time.strftime('%Y%m%d%H%M%S')}_{random.randbytes(4).hex()}.png"
    path = os.path.join(output_dir, name)
    canvas.convert("RGB").save(path, format="PNG")
    return path


async def render_search_card(songs, keyword: str, output_dir: str, *,
                             covers: dict | None = None, command: str = "/酷狗下载",
                             width: int = WIDTH,
                             extra_font_candidates: tuple[str, ...] = ()) -> str:
    """渲染搜歌结果卡片，返回 PNG 路径；不可渲染时抛 ``CardRenderError``。"""
    options = {
        "covers": covers or {},
        "command": command,
        "width": width,
        "extra_font_candidates": extra_font_candidates,
    }
    return await asyncio.to_thread(
        _render_sync, list(songs or []), keyword, output_dir, options
    )
