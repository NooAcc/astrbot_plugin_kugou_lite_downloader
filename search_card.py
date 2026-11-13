"""搜歌结果卡片：用 Pillow 在本地画一张图片。

**不导入 astrbot、不联网**：封面由调用方先下载好，通过 ``covers`` 传入本模块；
字体路径也由调用方给全（见 ``preferred_fonts`` / ``preferred_fonts_bold``）。
这样卡片渲染可以在纯离线环境下测试，也不会把网络失败与绘制失败混在一起。

版式只有一个真源：**字号阶梯**。宽度、行高、封面边长、内边距全部由字号推导，
不再各自写死——否则调大一档字号就会得到「大字挤在旧卡片里」的结果。

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

# ── 字号阶梯：版式的唯一真源 ────────────────────────────────────────────
#
# 中文字形在小字号下会糊成一团（笔画并连、粗细失真），所以这套阶梯的下限由
# 「汉字能否看清」决定，而不是由「一行能塞下多少」决定。

F_HEAD = 28          # 页眉：「关键词」搜索结果
F_HEAD_SUB = 17      # 页眉右侧：共 N 首
F_TITLE = 22         # 歌名（粗体）
F_META = 18          # 歌手 · 专辑
F_DUR = 18           # 时长
F_IDX = 16           # 序号徽章
F_FOOT = 16          # 页脚用法提示

# ── 由字号推导的版式（改字号，这些数会跟着走）─────────────────────────

LEAD = max(4, round(F_TITLE * 0.22))              # 歌名行距
LINE_H = F_TITLE + LEAD
META_GAP = max(6, round(F_TITLE * 0.30))          # 歌名与「歌手 · 专辑」的间距
TITLE_LINES = 2                                   # 歌名最多折几行
BLOCK_H = TITLE_LINES * LINE_H + META_GAP + F_META   # 文字块高度（恒定，行高才恒定）

COVER = int(round(BLOCK_H * 1.25 / 4) * 4)        # 封面边长：跟着文字块放大
COVER_RADIUS = round(COVER * 0.14)
ROW_PAD_Y = max(6, round(F_TITLE * 0.5))
ROW_H = max(COVER, BLOCK_H) + ROW_PAD_Y * 2
PAD = max(32, round(F_HEAD * 1.2))                # 页边距
TEXT_GAP = max(14, round(F_TITLE * 0.9))          # 封面与文字之间
IDX = max(20, round(F_IDX * 1.9))                 # 序号徽章边长
IDX_RADIUS = round(IDX * 0.34)
IDX_INSET = max(5, round(F_IDX * 0.45))
HEADER_H = round(F_HEAD * 2.6 + F_HEAD_SUB)
FOOTER_H = round(F_FOOT * 3.4)

# ── 右侧「圆点 · 时长」组合 ──────────────────────────────────────────────
#
# 只有时长的时候，短行（歌手/专辑很短）右半边会空出一大片。给时长配一枚 accent
# 圆点，让右边缘有个视觉落点；平台名不放——整张卡片本来就只有酷狗一个来源，
# 每行重复一遍纯属噪音。

DOT = max(8, F_IDX // 2)
DOT_GAP = 6
#: 时长列按最宽的时长预留，卡片宽度不随某次搜索出现的时长字数变化。
DUR_RESERVE = "88:88"

#: 文字列宽度：按 ``TITLE_CHARS`` 个歌名字符预留。这是「一行想看多少字」的
#: 设计意图，也是卡片宽度的**唯一**自由变量——调它就是在调卡片宽度。
#: 超出这个宽度的歌名折行、元信息截断加省略号，不会把卡片撑宽。
#:
#: 取值是「够短」与「够放」的折中：偏小则卡片紧凑，但长的「歌手 · 专辑」会被
#: 截掉（15 时，28 字的长条目还能留住 22 字）；偏大则那一片空白又回来了。
TITLE_CHARS = 15

INK = (26, 28, 30)
MUTED = (138, 144, 151)
LINE = (241, 242, 243)
ROW_LINE = (247, 248, 249)
FOOTER_BG = (250, 251, 251)
ACCENT = (47, 134, 189)
PLACEHOLDER_TOP = (238, 240, 242)
PLACEHOLDER_BOTTOM = (228, 231, 234)

#: 正文字体候选（按优先级；必须含中文字形，否则视为不可用）。
#: 自带字体与补全字体由调用方通过 ``preferred_fonts*`` 排在更前面。
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
#: 调用方额外提供的候选（如 AstrBot 数据目录下的 ``font.ttf``）：**排在系统候选之后**，
#: 只作最后兜底。

#: 探测中文字形覆盖用的字符：一个常用汉字，与一个必然缺字的私用区码位。
_PROBE_HAN = "永"
_PROBE_MISSING = "\ue000"


class CardRenderError(Exception):
    """卡片无法渲染（缺依赖、缺中文字体、没有可渲染内容）。"""


# ── 字体 ─────────────────────────────────────────────────────────────────

_font_path_cache: dict[tuple[str, ...], str | None] = {}


def load_font(path: Any, size: int):
    """按字号加载字体；Pillow 缺失或字体损坏时抛异常，由调用方决定降级。"""
    if ImageFont is None:
        raise CardRenderError("缺少 Pillow 依赖")
    return ImageFont.truetype(str(path), int(size))


def has_cjk_glyph(font: Any) -> bool:
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
            probe = load_font(path, 24)
        except Exception:
            continue
        if has_cjk_glyph(probe):
            found = path
            break
    _font_path_cache[key] = found
    return found


def _regular_candidates(preferred: Sequence[str], extra: Sequence[str]) -> tuple[str, ...]:
    return tuple(preferred) + FONT_CANDIDATES + tuple(extra)


def _bold_candidates(preferred: Sequence[str], extra: Sequence[str]) -> tuple[str, ...]:
    return tuple(preferred) + FONT_CANDIDATES_BOLD + tuple(extra)


def cjk_font_available(preferred: Sequence[str] = (), extra_candidates: tuple[str, ...] = ()) -> bool:
    """是否存在可用的中文字体（正文或粗体任一即可）。"""
    if ImageFont is None:
        return False
    return bool(
        _find_font_path(_regular_candidates(preferred, extra_candidates))
        or _find_font_path(_bold_candidates(preferred, extra_candidates))
    )


class _Fonts:
    """按需加载并缓存字号。

    查找顺序：``preferred``（自带 / 已补全的中文字体）→ 系统中文字体 → ``extra``。
    自带字体排在最前是刻意的：卡片版式是按它的字宽标定的，混用系统字体就会跑版。
    """

    def __init__(self, preferred: Sequence[str] = (), *,
                 preferred_bold: Sequence[str] = (),
                 extra: tuple[str, ...] = ()):
        regular = _find_font_path(_regular_candidates(preferred, extra))
        bold = _find_font_path(_bold_candidates(preferred_bold, extra)) or regular
        if regular is None and bold is None:
            raise CardRenderError("找不到含中文字形的字体，无法渲染卡片")
        self._regular = regular or bold
        self._bold = bold or regular
        self._cache: dict[tuple[int, bool], Any] = {}

    def get(self, size: int, *, bold: bool = False):
        key = (size, bold)
        if key not in self._cache:
            path = self._bold if bold else self._regular
            self._cache[key] = load_font(path, size)
        return self._cache[key]


_MEASURE: Any = None


def _measure_draw() -> Any:
    """只用来量文字宽度的 1×1 画布（量宽度与画布尺寸无关）。"""
    global _MEASURE
    if _MEASURE is None and ImageDraw is not None:
        _MEASURE = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    return _MEASURE


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
    ring = round(size * 0.16)
    dot = max(2, round(size * 0.036))
    draw.ellipse((center - ring, center - ring, center + ring, center + ring),
                 outline=ACCENT + (110,), width=max(2, round(size * 0.036)))
    draw.ellipse((center - dot, center - dot, center + dot, center + dot),
                 fill=ACCENT + (110,))
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


# ── 版式计算 ─────────────────────────────────────────────────────────────

def right_group_width(draw, fonts: _Fonts) -> int:
    """右侧组合按最宽时长预留的宽度（圆点 + 时长）。"""
    return (DOT + DOT_GAP
            + int(draw.textlength(DUR_RESERVE, font=fonts.get(F_DUR))))


def layout_width(fonts: _Fonts) -> int:
    """卡片宽度 = 页边距 ×2 + 封面 + 间距 + 文字列 + 间距 + 右侧组合。

    宽度是**算出来的**，不是拍的：换字号或改「一行想看多少字」，卡片自己会跟着变。
    """
    draw = _measure_draw()
    return (PAD * 2 + COVER + TEXT_GAP + TITLE_CHARS * F_TITLE + TEXT_GAP
            + right_group_width(draw, fonts))


# ── 绘制 ─────────────────────────────────────────────────────────────────

def _render_sync(songs: Sequence[Any], keyword: str, output_dir: str,
                 options: dict) -> str:
    if Image is None or ImageDraw is None:
        raise CardRenderError("缺少 Pillow 依赖")

    rows = [s for s in songs if isinstance(s, dict)]
    if not rows:
        raise CardRenderError("没有可渲染的歌曲")

    strips = [safe_text(s.get("songname"), 120) or "未知歌曲" for s in rows]
    covers = options.get("covers") or {}
    command = safe_text(options.get("command") or "/酷狗下载", 20) or "/酷狗下载"

    fonts = _Fonts(tuple(options.get("preferred_fonts") or ()),
                   preferred_bold=tuple(options.get("preferred_fonts_bold") or ()),
                   extra=tuple(options.get("extra_font_candidates") or ()))
    f_head = fonts.get(F_HEAD, bold=True)
    f_head_sub = fonts.get(F_HEAD_SUB)
    f_title = fonts.get(F_TITLE, bold=True)
    f_meta = fonts.get(F_META)
    f_dur = fonts.get(F_DUR)
    f_idx = fonts.get(F_IDX, bold=True)
    f_foot = fonts.get(F_FOOT)

    # 高度预算：先算清再开画布
    total_h = HEADER_H + ROW_H * len(rows) + FOOTER_H
    explicit = int(options.get("width") or 0)
    if explicit:
        # 显式宽度也要留得下「页边距 + 封面 + 最小文字列 + 右侧组合」，
        # 否则文字会压到右侧组合上（推导宽度天然满足这条，无需兜底）
        floor = (PAD * 2 + COVER + TEXT_GAP + 60 + TEXT_GAP
                 + right_group_width(_measure_draw(), fonts))
        width = max(explicit, floor)
    else:
        width = layout_width(fonts)

    canvas = Image.new("RGBA", (width, total_h), (255, 255, 255, 255))
    draw = ImageDraw.Draw(canvas)

    # 页眉
    head_y = round(HEADER_H * 0.26)
    count = f"共 {len(rows)} 首"
    count_w = draw.textlength(count, font=f_head_sub)
    draw.text((PAD, head_y),
              _ellipsize(draw, f"「{safe_text(keyword, 60)}」搜索结果",
                         f_head, width - PAD * 2 - count_w - 24),
              font=f_head, fill=INK)
    draw.text((width - PAD - count_w, head_y + round(F_HEAD * 0.4)), count,
              font=f_head_sub, fill=MUTED)
    draw.line((PAD, HEADER_H - 1, width - PAD, HEADER_H - 1), fill=LINE, width=1)

    # 列表
    y = HEADER_H
    for index, (song, title) in enumerate(zip(rows, strips), start=1):
        row_top = y
        cover_x = PAD
        cover_y = row_top + (ROW_H - COVER) // 2

        tile = _cover_tile(covers.get(song.get("cover")), COVER)
        if tile is None:
            tile = _placeholder(COVER)
        canvas.alpha_composite(tile, (cover_x, cover_y))

        # 序号徽章：压在封面左下角
        bx = cover_x + IDX_INSET
        by = cover_y + COVER - IDX_INSET
        draw.rounded_rectangle((bx, by - IDX, bx + IDX, by), radius=IDX_RADIUS, fill=ACCENT)
        num = str(index)
        # 按字形墨迹居中（而不是按行高）：数字才会落在徽章正中，不偏高
        left, top, right, bottom = draw.textbbox((0, 0), num, font=f_idx)
        draw.text((bx + (IDX - (right - left)) / 2 - left,
                   by - IDX + (IDX - (bottom - top)) / 2 - top),
                  num, font=f_idx, fill=(255, 255, 255))

        # 右侧组合：圆点 + 时长，整体右对齐，时长依旧成列可比
        duration = format_duration(song.get("duration"))
        dur_w = int(draw.textlength(duration, font=f_dur)) if duration else 0
        group_w = (DOT + DOT_GAP if dur_w else 0) + dur_w
        dot_x = width - PAD - group_w
        dur_x = dot_x + DOT + DOT_GAP

        text_x = cover_x + COVER + TEXT_GAP
        text_w = max(60, dot_x - TEXT_GAP - text_x)

        block_top = cover_y + max(0, (COVER - BLOCK_H) // 2)
        ty = block_top
        for line in _wrap(draw, title, f_title, text_w, TITLE_LINES):
            draw.text((text_x, ty), line, font=f_title, fill=INK)
            ty += LINE_H

        artist = safe_text(song.get("singername"), 60)
        album = safe_text(song.get("album"), 60)
        meta = " · ".join(part for part in (artist, album) if part)
        if meta:
            draw.text((text_x, ty + META_GAP),
                      _ellipsize(draw, meta, f_meta, text_w), font=f_meta, fill=MUTED)

        # 右侧文字与文字块垂直居中：位置只由版式常量决定，跨行不跳动
        ry = block_top + (BLOCK_H - F_META) // 2
        if duration:
            draw.text((dur_x, ry), duration, font=f_dur, fill=MUTED)
            dy = ry + round(F_META * 0.62)
            draw.ellipse((dot_x, dy - DOT // 2, dot_x + DOT, dy + DOT // 2), fill=ACCENT)

        y = row_top + ROW_H
        if index < len(rows):
            draw.line((PAD, y - 1, width - PAD, y - 1), fill=ROW_LINE, width=1)

    # 页脚
    footer_top = total_h - FOOTER_H
    draw.rectangle((0, footer_top, width, total_h), fill=FOOTER_BG)
    draw.line((0, footer_top, width, footer_top), fill=LINE, width=1)
    draw.text((PAD, footer_top + round(FOOTER_H * 0.3)),
              f"发送 {command} 序号 获取歌曲文件", font=f_foot, fill=MUTED)

    os.makedirs(output_dir, exist_ok=True)
    name = f"search_card_{time.strftime('%Y%m%d%H%M%S')}_{random.randbytes(4).hex()}.png"
    path = os.path.join(output_dir, name)
    canvas.convert("RGB").save(path, format="PNG")
    return path


async def render_search_card(songs, keyword: str, output_dir: str, *,
                             covers: dict | None = None, command: str = "/酷狗下载",
                             width: int | None = None,
                             preferred_fonts: Sequence[str] = (),
                             preferred_fonts_bold: Sequence[str] = (),
                             extra_font_candidates: tuple[str, ...] = ()) -> str:
    """渲染搜歌结果卡片，返回 PNG 路径；不可渲染时抛 ``CardRenderError``。

    :param preferred_fonts: 排在最前的中文字体候选（自带字体 / 已补全字体）。
    :param preferred_fonts_bold: 粗体一列的同义参数；为空时回退到 ``preferred_fonts``。
    :param width: 不传则按字号推导；传了就用给定宽度（文字列相应伸缩）。
    """
    options = {
        "covers": covers or {},
        "command": command,
        "width": width,
        "preferred_fonts": tuple(preferred_fonts),
        "preferred_fonts_bold": tuple(preferred_fonts_bold) or tuple(preferred_fonts),
        "extra_font_candidates": extra_font_candidates,
    }
    return await asyncio.to_thread(
        _render_sync, list(songs or []), keyword, output_dir, options
    )
