"""tagger.py 的映射层与写入层测试。"""

import asyncio
import os
import tempfile

import pytest
from mutagen import File as MutagenFile
from mutagen.id3 import ID3, TIT2

import audio_builders as ab
from metadata import TrackMeta

import tagger

_META = TrackMeta(title="晴天", artists=("周杰伦", "方文山"), album="叶惠美",
                  album_artist="周杰伦", composer="周杰伦", lyricist="方文山",
                  date="2003-07-31", genre="流行", bpm="68", disc="1",
                  cover_url="https://x/y.jpg", lyric="[ti:晴天]\n")


def test_id3_tags_uses_expected_frames():
    tags = tagger.id3_tags(_META)
    assert tags["TIT2"] == "晴天"
    assert tags["TPE1"] == "周杰伦、方文山"
    assert tags["TALB"] == "叶惠美"
    assert tags["TPE2"] == "周杰伦"
    assert tags["TCOM"] == "周杰伦"
    assert tags["TEXT"] == "方文山"
    assert tags["TDRC"] == "2003-07-31"
    assert tags["TCON"] == "流行"
    assert tags["TBPM"] == "68"
    assert tags["TPOS"] == "1"
    assert tags["USLT"] == "[ti:晴天]\n"


def test_vorbis_tags_uses_expected_keys():
    tags = tagger.vorbis_tags(_META)
    assert tags["TITLE"] == "晴天"
    assert tags["ARTIST"] == "周杰伦、方文山"
    assert tags["ALBUM"] == "叶惠美"
    assert tags["ALBUMARTIST"] == "周杰伦"
    assert tags["COMPOSER"] == "周杰伦"
    assert tags["LYRICIST"] == "方文山"
    assert tags["DATE"] == "2003-07-31"
    assert tags["GENRE"] == "流行"
    assert tags["BPM"] == "68"
    assert tags["DISCNUMBER"] == "1"
    assert tags["LYRICS"] == "[ti:晴天]\n"


def test_empty_fields_are_omitted_not_blank():
    tags = tagger.id3_tags(TrackMeta(title="x"))
    assert tags == {"TIT2": "x"}          # 空字段不写，避免污染播放器


def test_cover_url_never_becomes_a_text_frame():
    # 封面走 APIC / picture block，不能混进文本标签
    assert "https://x/y.jpg" not in tagger.id3_tags(_META).values()
    assert "https://x/y.jpg" not in tagger.vorbis_tags(_META).values()


def test_single_artist_needs_no_separator():
    tags = tagger.id3_tags(TrackMeta(artists=("周杰伦",)))
    assert tags["TPE1"] == "周杰伦"


# ── 写入层（需要 mutagen 与真实可加载文件） ──────────────────────────────


def _tmp(data: bytes, suffix: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.write(fd, data)
    os.close(fd)
    return path


def _read(path: str, suffix: str) -> dict:
    """读回标签，统一成 {大写键: 首个值}；mp3 走 ID3，flac/ogg 走 Vorbis comment。

    Vorbis 的键在 mutagen 里按小写存、查找大小写不敏感，因此统一转成大写再比对。
    """
    if suffix == "mp3":
        try:
            id3 = ID3(path)
        except Exception:
            return {}
        return {key: str(frame.text[0]) for key, frame in id3.items() if hasattr(frame, "text")}
    handle = MutagenFile(path)
    # 注意：mutagen 对象在无标签时是 falsy 的，判定必须用 `is not None`
    assert handle is not None
    if not handle.tags:
        return {}
    return {str(key).upper(): str(value[0]) for key, value in handle.tags.items()}


_TITLE_KEY = {"mp3": "TIT2", "flac": "TITLE", "ogg": "TITLE"}
_ARTIST_KEY = {"mp3": "TPE1", "flac": "ARTIST", "ogg": "ARTIST"}
_BUILDERS = {"mp3": ab.mp3_file, "flac": ab.flac_file, "ogg": ab.ogg_file}


@pytest.mark.parametrize("suffix", ["mp3", "flac", "ogg"])
def test_write_tags_round_trips(suffix):
    path = _tmp(_BUILDERS[suffix](), "." + suffix)
    try:
        assert tagger.write_tags(path, _META, suffix, None) is True
        tags = _read(path, suffix)
        assert tags[_TITLE_KEY[suffix]] == "晴天"
        assert tags[_ARTIST_KEY[suffix]] == "周杰伦、方文山"
        assert tags["TCON" if suffix == "mp3" else "GENRE"] == "流行"
    finally:
        os.remove(path)


@pytest.mark.parametrize("suffix", ["mp3", "flac", "ogg"])
def test_write_tags_clears_existing_tags_first(suffix):
    """CDN 文件自带酷狗的帧；只覆盖映射到的键会让遗留帧活下来。"""
    path = _tmp(_BUILDERS[suffix](), "." + suffix)
    try:
        tagger.write_tags(path, TrackMeta(title="酷狗原标题", album="酷狗专辑"), suffix, None)
        assert tagger.write_tags(path, _META, suffix, None) is True
        tags = _read(path, suffix)
        assert tags[_TITLE_KEY[suffix]] == "晴天"
        # 上一轮写入但本轮没有的字段必须消失
        assert tags.get("TALB" if suffix == "mp3" else "ALBUM", "") == "叶惠美"
        assert "酷狗专辑" not in tags.values()
    finally:
        os.remove(path)


def test_write_tags_embeds_small_cover():
    path = _tmp(ab.mp3_file(), ".mp3")
    cover = _tmp(b"\xff\xd8\xff\xe0" + bytes(1000), ".jpg")
    try:
        assert tagger.write_tags(path, _META, "mp3", cover) is True
        assert any(key.startswith("APIC") for key in ID3(path).keys())
    finally:
        os.remove(path)
        os.remove(cover)


def test_write_tags_rejects_oversized_cover_but_keeps_labels():
    path = _tmp(ab.mp3_file(), ".mp3")
    cover = _tmp(b"\xff\xd8\xff\xe0" + bytes(600 * 1024), ".jpg")
    try:
        assert tagger.write_tags(path, _META, "mp3", cover) is True   # 丢封面但仍写标签
        assert not any(key.startswith("APIC") for key in ID3(path).keys())
        assert ID3(path)["TIT2"].text == ["晴天"]
    finally:
        os.remove(path)
        os.remove(cover)


def test_write_tags_returns_false_on_unknown_container():
    path = _tmp(b"not audio", ".xyz")
    try:
        assert tagger.write_tags(path, _META, "xyz", None) is False
    finally:
        os.remove(path)


def test_write_tags_returns_false_for_missing_file():
    assert tagger.write_tags("no/such/file.mp3", _META, "mp3", None) is False


@pytest.mark.parametrize("suffix", ["mp3", "flac", "ogg"])
def test_delete_tags_removes_everything(suffix):
    path = _tmp(_BUILDERS[suffix](), "." + suffix)
    try:
        assert tagger.write_tags(path, _META, suffix, None) is True
        assert tagger.delete_tags(path, suffix) is True
        assert _read(path, suffix) == {}
    finally:
        os.remove(path)


# ── 编排层 Tagger ───────────────────────────────────────────────────────


class _FakeCovers:
    def __init__(self, path=None):
        self.path = path
        self.seen: list[str] = []

    async def ensure(self, url):
        self.seen.append(url)
        return self.path


def _patch_collect(monkeypatch, meta=_META):
    async def fake(client, song):
        return meta
    monkeypatch.setattr(tagger, "collect_track_meta", fake)


def test_apply_writes_and_reports_success(tmp_path, monkeypatch):
    _patch_collect(monkeypatch)
    seen = []
    monkeypatch.setattr(tagger, "write_tags",
                        lambda p, m, e, c, l=None: (seen.append((m, e, c)), True)[1])
    path = str(tmp_path / "a.mp3")
    open(path, "wb").write(ab.mp3_file())

    tagger_obj = tagger.Tagger(client=None, covers=None, embed_cover=False)
    assert asyncio.run(tagger_obj.apply(path, "mp3", {"hash": "x"})) is True
    assert seen and seen[0][0] is _META and seen[0][1] == "mp3"


def test_apply_returns_false_when_write_fails(tmp_path, monkeypatch):
    _patch_collect(monkeypatch)
    monkeypatch.setattr(tagger, "write_tags", lambda *a, **k: False)
    path = str(tmp_path / "a.mp3")
    open(path, "wb").write(ab.mp3_file())
    tagger_obj = tagger.Tagger(client=None, covers=None, embed_cover=False)
    assert asyncio.run(tagger_obj.apply(path, "mp3", {"hash": "x"})) is False


def test_apply_returns_false_when_collect_raises(tmp_path, monkeypatch):
    async def boom(client, song):
        raise RuntimeError("nope")
    monkeypatch.setattr(tagger, "collect_track_meta", boom)
    path = str(tmp_path / "a.mp3")
    open(path, "wb").write(ab.mp3_file())
    tagger_obj = tagger.Tagger(client=None, covers=None, embed_cover=False)
    assert asyncio.run(tagger_obj.apply(path, "mp3", {"hash": "x"})) is False


def test_apply_skips_cover_when_disabled(tmp_path, monkeypatch):
    _patch_collect(monkeypatch)
    covers = _FakeCovers("/tmp/cover.jpg")
    seen = {}
    monkeypatch.setattr(tagger, "write_tags",
                        lambda p, m, e, c, l=None: seen.setdefault("cover", c) or True)
    path = str(tmp_path / "a.mp3")
    open(path, "wb").write(ab.mp3_file())

    tagger_obj = tagger.Tagger(client=None, covers=covers, embed_cover=False)
    asyncio.run(tagger_obj.apply(path, "mp3", {"hash": "x"}))
    assert seen["cover"] is None
    assert covers.seen == []          # 关掉封面就不该去抓


def test_apply_fetches_cover_when_enabled(tmp_path, monkeypatch):
    _patch_collect(monkeypatch)
    covers = _FakeCovers("/tmp/cover.jpg")
    seen = {}
    monkeypatch.setattr(tagger, "write_tags",
                        lambda p, m, e, c, l=None: seen.setdefault("cover", c) or True)
    path = str(tmp_path / "a.mp3")
    open(path, "wb").write(ab.mp3_file())

    tagger_obj = tagger.Tagger(client=None, covers=covers, embed_cover=True)
    asyncio.run(tagger_obj.apply(path, "mp3", {"hash": "x"}))
    assert seen["cover"] == "/tmp/cover.jpg"
    assert covers.seen == [_META.cover_url]


def test_apply_tolerates_cover_fetch_failure(tmp_path, monkeypatch):
    _patch_collect(monkeypatch)
    monkeypatch.setattr(tagger, "write_tags", lambda p, m, e, c, l=None: c is None)
    path = str(tmp_path / "a.mp3")
    open(path, "wb").write(ab.mp3_file())
    covers = _FakeCovers(None)        # ensure 返回 None（抓不到）
    tagger_obj = tagger.Tagger(client=None, covers=covers, embed_cover=True)
    assert asyncio.run(tagger_obj.apply(path, "mp3", {"hash": "x"})) is True


def test_strip_removes_tags(tmp_path):
    path = str(tmp_path / "a.mp3")
    open(path, "wb").write(ab.mp3_file())
    assert tagger.write_tags(path, _META, "mp3", None) is True
    tagger_obj = tagger.Tagger(client=None, covers=None, embed_cover=False)
    assert tagger_obj.strip(path, "mp3") is True
    assert _read(path, "mp3") == {}



def _flac_with_picture():
    from mutagen.flac import FLAC, Picture

    path = _tmp(ab.flac_file(), ".flac")
    picture = Picture()
    picture.type = 3
    picture.mime = "image/jpeg"
    picture.data = b"\xff\xd8\xff\xe0" + bytes(100)
    handle = FLAC(path)
    handle.add_picture(picture)
    handle.save()
    assert len(FLAC(path).pictures) == 1
    return path


def test_write_tags_clears_pre_existing_flac_picture():
    """酷狗自己塞的封面也必须被清掉——否则 embed_cover=False 形同虚设。"""
    from mutagen.flac import FLAC

    path = _flac_with_picture()
    try:
        assert tagger.write_tags(path, _META, "flac", None) is True
        assert FLAC(path).pictures == []
    finally:
        os.remove(path)


def test_delete_tags_clears_flac_picture():
    from mutagen.flac import FLAC

    path = _flac_with_picture()
    try:
        assert tagger.delete_tags(path, "flac") is True
        assert FLAC(path).pictures == []
    finally:
        os.remove(path)


def test_write_tags_replaces_pre_existing_flac_picture():
    from mutagen.flac import FLAC

    path = _flac_with_picture()
    cover = _tmp(b"\xff\xd8\xff\xe0" + bytes(1000), ".jpg")
    try:
        assert tagger.write_tags(path, _META, "flac", cover) is True
        pictures = FLAC(path).pictures
        assert len(pictures) == 1 and pictures[0].data.endswith(bytes(1000))
    finally:
        os.remove(path)
        os.remove(cover)
