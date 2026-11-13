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
        assert image.width > 0
        assert image.height > 0


def test_card_size_is_derived_from_the_type_scale(tmp_path):
    """尺寸不写死：宽 = 版式式，高 = 页眉 + 行高 × 歌曲数 + 页脚。

    这条是「字号决定一切」的守卫——一旦有人把某个几何量重新写死，
    等式就对不上了。
    """
    path = _render(tmp_path, _SONGS)
    fonts = search_card._Fonts(extra=rendering_font_candidates())
    with Image.open(path) as image:
        assert image.width == search_card.layout_width(fonts)
        assert image.height == (search_card.HEADER_H
                                + search_card.ROW_H * len(_SONGS)
                                + search_card.FOOTER_H)


def test_bigger_title_font_widens_the_card(tmp_path, monkeypatch):
    """把歌名字号调大一档，卡片必须跟着变宽（而不是让字挤在旧宽度里）。"""
    fonts = search_card._Fonts(extra=rendering_font_candidates())
    before = search_card.layout_width(fonts)
    monkeypatch.setattr(search_card, "F_TITLE", search_card.F_TITLE + 6)
    after = search_card.layout_width(fonts)

    assert after - before == 6 * search_card.TITLE_CHARS


def test_right_group_is_part_of_the_width(tmp_path, monkeypatch):
    """右侧的「圆点 + 时长」占位也要算进宽度，否则文字会压到它上面。"""
    fonts = search_card._Fonts(extra=rendering_font_candidates())
    before = search_card.layout_width(fonts)
    monkeypatch.setattr(search_card, "DUR_RESERVE", search_card.DUR_RESERVE * 3)
    after = search_card.layout_width(fonts)

    assert after > before


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
    """查找顺序：自带/已补全字体 → 系统中文字体 → 数据目录 font.ttf（最后兜底）。"""
    system = str(tmp_path / "system.ttf")
    extra = str(tmp_path / "extra.ttf")
    write_min_cjk_font(system)
    write_min_cjk_font(extra)
    monkeypatch.setattr(search_card, "FONT_CANDIDATES", (system,))
    monkeypatch.setattr(search_card, "FONT_CANDIDATES_BOLD", ())

    fonts = search_card._Fonts(extra=(extra,))

    assert os.path.samefile(fonts.get(15).path, system)


def test_preferred_font_wins_over_system_font(tmp_path, monkeypatch):
    """自带字体排在系统字体之前是刻意的：卡片版式按它的字宽标定，混用会跑版。"""
    system = str(tmp_path / "system.ttf")
    preferred = str(tmp_path / "preferred.ttf")
    write_min_cjk_font(system)
    write_min_cjk_font(preferred)
    monkeypatch.setattr(search_card, "FONT_CANDIDATES", (system,))
    monkeypatch.setattr(search_card, "FONT_CANDIDATES_BOLD", ())

    fonts = search_card._Fonts((preferred,))

    assert os.path.samefile(fonts.get(15).path, preferred)


def test_missing_preferred_candidate_falls_through_to_system(tmp_path, monkeypatch):
    """补全没成功时不能因此画不出图：候选不存在就往下走。"""
    system = str(tmp_path / "system.ttf")
    write_min_cjk_font(system)
    monkeypatch.setattr(search_card, "FONT_CANDIDATES", (system,))
    monkeypatch.setattr(search_card, "FONT_CANDIDATES_BOLD", ())

    fonts = search_card._Fonts((str(tmp_path / "never-downloaded.ttf"),))

    assert os.path.samefile(fonts.get(15).path, system)


def test_bold_column_does_not_borrow_the_regular_candidate(tmp_path, monkeypatch):
    """正文字体被塞进粗体列时，粗体应挑自己那一列，而不是把正文当粗体用。"""
    regular = str(tmp_path / "regular.ttf")
    bold = str(tmp_path / "bold.ttf")
    write_min_cjk_font(regular)
    write_min_cjk_font(bold)
    monkeypatch.setattr(search_card, "FONT_CANDIDATES", ())
    monkeypatch.setattr(search_card, "FONT_CANDIDATES_BOLD", ())

    fonts = search_card._Fonts((regular,), preferred_bold=(bold,))

    assert os.path.samefile(fonts.get(15, bold=True).path, bold)
    assert os.path.samefile(fonts.get(15).path, regular)


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
