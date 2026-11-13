"""本地 Pillow 搜歌卡片渲染。"""

import asyncio
import os
import re

import pytest
from PIL import Image

import search_card
from search_card import CardRenderError, render_search_card

_SONGS = [
    {"songname": "晴天", "singername": "周杰伦", "album": "叶惠美", "duration": 269,
     "cover": "https://imge.kugou.com/a.jpg", "pay_type": 3},
    {"songname": "夜曲", "singername": "周杰伦", "album": "十一月的萧邦", "duration": 227,
     "cover": "", "pay_type": 0},
    {"songname": "稻香", "singername": "周杰伦", "album": "魔杰座", "duration": 223,
     "cover": "", "pay_type": 0},
]


def _render(tmp_path, songs, *, require_font=True, **kw):
    # 没有中文字体的机器上渲染本就会回退文本，这类测试跳过而不是误报失败
    if require_font and not search_card.cjk_font_available():
        pytest.skip("本机没有可用的中文字体")
    keyword = kw.pop("keyword", "晴天")
    return asyncio.run(render_search_card(songs, keyword, str(tmp_path), **kw))


def test_renders_one_row_per_song_and_returns_png(tmp_path):
    path = _render(tmp_path, _SONGS)
    assert os.path.exists(path)
    with Image.open(path) as image:
        assert image.width == 800
        assert image.height > 0


def test_empty_songs_raises(tmp_path):
    with pytest.raises(CardRenderError):
        _render(tmp_path, [])


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
        _render(tmp_path, _SONGS, require_font=False)


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
