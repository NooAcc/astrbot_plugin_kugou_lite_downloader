"""本地 Pillow 搜歌卡片渲染。"""

import asyncio
import os
import re

import pytest
from PIL import Image, ImageDraw, ImageFont

import search_card
from cjk_font import rendering_font_candidates, write_min_cjk_font
from search_card import CardRenderError, _wrap, render_search_card

_SONGS = [
    {"songname": "晴天", "singername": "周杰伦", "album": "叶惠美", "duration": 269,
     "cover": "https://imge.kugou.com/a.jpg", "pay_type": 3},
    {"songname": "夜曲", "singername": "周杰伦", "album": "十一月的萧邦", "duration": 227,
     "cover": "", "pay_type": 0},
    {"songname": "稻香", "singername": "周杰伦", "album": "魔杰座", "duration": 223,
     "cover": "", "pay_type": 0},
]


def _render(tmp_path, songs, **kw):
    # 渲染必须有一个含中文字形的字体。本机没有时由 cjk_font 现场生成一个兜底候选，
    # 而不是把整批渲染用例 skip 掉（skip 会让这些断言在任何无中文字体的机器上静默消失）。
    kw.setdefault("extra_font_candidates", rendering_font_candidates())
    keyword = kw.pop("keyword", "晴天")
    return asyncio.run(render_search_card(songs, keyword, str(tmp_path), **kw))


def test_renders_one_row_per_song_and_returns_png(tmp_path):
    path = _render(tmp_path, _SONGS)
    assert os.path.exists(path)
    with Image.open(path) as image:
        assert image.width == 800
        assert image.height > 0


def test_empty_songs_raises(tmp_path):
    # 没有可渲染条目时不该先去找字体：这条与字体无关，任何机器上都应成立
    with pytest.raises(CardRenderError):
        asyncio.run(render_search_card([], "晴天", str(tmp_path)))


def test_missing_artist_album_duration_does_not_crash(tmp_path):
    path = _render(tmp_path, [{"songname": "只有名字"}])
    assert os.path.exists(path)


def test_long_title_and_emoji_are_tolerated(tmp_path):
    song = {"songname": "🎵" * 3 + "很长的名字" * 40, "singername": "x", "album": "y"}
    path = _render(tmp_path, [song])
    assert os.path.exists(path)


def test_cover_missing_or_broken_falls_back_to_placeholder(tmp_path):
    assert os.path.exists(_render(tmp_path, _SONGS, covers={}))
    broken = _render(tmp_path, _SONGS,
                     covers={_SONGS[0]["cover"]: str(tmp_path / "nope.jpg")})
    assert os.path.exists(broken)


def test_no_cjk_font_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(search_card, "FONT_CANDIDATES", ())
    monkeypatch.setattr(search_card, "FONT_CANDIDATES_BOLD", ())
    with pytest.raises(CardRenderError):
        # 不注入兜底字体：这里验证的正是「一个中文字体都没有」时的回退
        asyncio.run(render_search_card(_SONGS, "晴天", str(tmp_path)))


def test_render_falls_back_to_extra_font_when_system_fonts_absent(tmp_path, monkeypatch):
    """系统候选全不可用时，``extra_font_candidates`` 仍应能出图。"""
    font = str(tmp_path / "fallback.ttf")
    write_min_cjk_font(font)
    monkeypatch.setattr(search_card, "FONT_CANDIDATES", ())
    monkeypatch.setattr(search_card, "FONT_CANDIDATES_BOLD", ())

    path = asyncio.run(render_search_card(_SONGS, "晴天", str(tmp_path),
                                          extra_font_candidates=(font,)))

    assert os.path.exists(path)


def test_system_font_wins_over_extra_fallback(tmp_path, monkeypatch):
    """设计文档：字体发现顺序是「系统候选 → …」，``font.ttf`` 只作回退。"""
    system = str(tmp_path / "system.ttf")
    extra = str(tmp_path / "extra.ttf")
    write_min_cjk_font(system)
    write_min_cjk_font(extra)
    monkeypatch.setattr(search_card, "FONT_CANDIDATES", (system,))
    monkeypatch.setattr(search_card, "FONT_CANDIDATES_BOLD", ())

    fonts = search_card._Fonts((extra,))

    assert os.path.samefile(fonts.get(15).path, system)


def test_wrap_with_non_positive_max_lines_returns_no_lines():
    """``max_lines<=0`` 是「一行都不排」，应当返回空列表而不是 IndexError。"""
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    font = ImageFont.load_default()

    assert _wrap(draw, "很长的歌名", font, 40, 0) == []
    assert _wrap(draw, "很长的歌名", font, 40, -1) == []
    assert _wrap(draw, "", font, 40, 0) == []


def test_one_row_per_song_keeps_constant_row_height(tmp_path):
    def height(count):
        songs = [dict(_SONGS[1], songname=f"歌{i}") for i in range(count)]
        with Image.open(_render(tmp_path, songs)) as image:
            return image.height

    h1, h2, h3 = height(1), height(2), height(3)
    assert h2 > h1
    assert h2 - h1 == h3 - h2          # 每首歌恰好占一行、行高恒定


def test_webp_and_cmyk_covers_are_pasted_without_error(tmp_path):
    webp = tmp_path / "c.webp"
    Image.new("RGB", (64, 64), (10, 120, 200)).save(str(webp), format="WEBP")
    cmyk = tmp_path / "c.jpg"
    Image.new("CMYK", (64, 64)).save(str(cmyk), format="JPEG")

    def render_with(cover_path):
        song = dict(_SONGS[0])
        covers = {song["cover"]: str(cover_path)} if cover_path else {}
        with open(_render(tmp_path, [song], covers=covers), "rb") as handle:
            return handle.read()

    placeholder = render_with(None)
    assert render_with(webp) != placeholder      # 封面确实被贴进去（不是占位）
    assert render_with(cmyk) != placeholder


def test_output_filename_is_safe(tmp_path):
    path = _render(tmp_path, _SONGS, keyword="晴/天:?*|")
    name = os.path.basename(path)
    assert re.fullmatch(r"search_card_\d{14}_[0-9a-f]{8}\.png", name)
    assert "晴" not in name
