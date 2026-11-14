"""把 TrackMeta 写进音频文件的标签。

分三层，边界刻意划清：

1. **纯映射层**（本文件上半部分）：逻辑字段 → ID3 帧名 / Vorbis comment 键名。
   不碰文件、不 import mutagen，可完整单测。
2. **写入层**（下半部分）：mutagen 的薄适配，含封面字节上限与「先清空再写」。
3. **编排层**（`Tagger`）：取元信息与封面，再在线程里落盘。

字段映射依据见设计稿 §7。`extra.sort` 故意不映射进 `TRCK`——它是不是专辑内曲目号
未经实测确认；`base.language` 也不写 `TLAN`（实测是「国语」而非 ISO 码）。
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

try:
    from .metadata import TrackMeta, collect_track_meta
except ImportError:  # 以顶层模块方式导入（离线测试）
    from metadata import TrackMeta, collect_track_meta

logger = logging.getLogger(__name__)

#: 多歌手分隔符，与酷狗展示习惯一致。
ARTIST_SEPARATOR = "、"

#: 逻辑字段 → ID3v2 帧名。
ID3_FRAMES: dict[str, str] = {
    "title": "TIT2",
    "artists": "TPE1",
    "album": "TALB",
    "album_artist": "TPE2",
    "composer": "TCOM",
    "lyricist": "TEXT",
    "date": "TDRC",
    "genre": "TCON",
    "bpm": "TBPM",
    "disc": "TPOS",
    "lyric": "USLT",
}

#: 逻辑字段 → Vorbis comment 键名（flac / ogg 共用）。
VORBIS_KEYS: dict[str, str] = {
    "title": "TITLE",
    "artists": "ARTIST",
    "album": "ALBUM",
    "album_artist": "ALBUMARTIST",
    "composer": "COMPOSER",
    "lyricist": "LYRICIST",
    "date": "DATE",
    "genre": "GENRE",
    "bpm": "BPM",
    "disc": "DISCNUMBER",
    "lyric": "LYRICS",
}


def _field_values(meta: TrackMeta) -> dict[str, str]:
    """逻辑字段 → 文本值。**空值一律不入字典**（不是写空串）。"""
    values = {
        "title": meta.title,
        "artists": ARTIST_SEPARATOR.join(a for a in meta.artists if a),
        "album": meta.album,
        "album_artist": meta.album_artist,
        "composer": meta.composer,
        "lyricist": meta.lyricist,
        "date": meta.date,
        "genre": meta.genre,
        "bpm": meta.bpm,
        "disc": meta.disc,
        "lyric": meta.lyric,
    }
    return {key: value for key, value in values.items() if value}


def id3_tags(meta: TrackMeta) -> dict[str, str]:
    """ID3v2 帧名 → 值。封面不在此列（走 APIC）。"""
    return {ID3_FRAMES[key]: value for key, value in _field_values(meta).items()}


def vorbis_tags(meta: TrackMeta) -> dict[str, str]:
    """Vorbis comment 键名 → 值。封面不在此列（走 picture block）。"""
    return {VORBIS_KEYS[key]: value for key, value in _field_values(meta).items()}


# ── 写入层（mutagen 薄适配） ─────────────────────────────────────────────


@dataclass(frozen=True)
class TagLimits:
    """标签写入的内部安全默认值（不作为插件配置项暴露）。"""

    #: 封面字节上限。ID3v2 一旦撑到 1 MiB，`audio_probe` 就读不到帧同步点，
    #: 该缓存条目会在每次命中时被判「档位不符」而反复重下（实测见设计稿 §9）。
    max_cover_bytes: int = 512 * 1024


_DEFAULT_LIMITS = TagLimits()

#: 支持的容器：扩展名 → (清空, 写入) 两个动作所在的容器族。
_SUPPORTED = frozenset({"mp3", "flac", "ogg"})


def _mime_for(path: str) -> str:
    return "image/png" if str(path).lower().endswith(".png") else "image/jpeg"


def _cover_bytes(cover_path: str | None, limits: TagLimits) -> bytes | None:
    """读封面字节；超限或读不到一律返回 None（丢封面，不丢整张标签）。"""
    if not cover_path:
        return None
    try:
        size = os.path.getsize(cover_path)
    except OSError:
        return None
    if size <= 0 or size > limits.max_cover_bytes:
        return None
    try:
        with open(cover_path, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def _write_id3(path: str, meta: TrackMeta, cover: bytes | None, cover_path: str | None) -> None:
    from mutagen.id3 import (APIC, TBPM, TCOM, TCON, TDRC, TEXT, TIT2, TALB,
                             TPE1, TPE2, TPOS, USLT, ID3, ID3NoHeaderError)

    try:
        tag = ID3(path)
    except ID3NoHeaderError:
        tag = ID3()                  # 文件本来没有 ID3 头，从一个空标签写起
    tag.clear()                      # 先清空，酷狗遗留的帧不能活下来
    mapping = {
        "TIT2": lambda v: TIT2(encoding=3, text=[v]),
        "TPE1": lambda v: TPE1(encoding=3, text=[v]),
        "TALB": lambda v: TALB(encoding=3, text=[v]),
        "TPE2": lambda v: TPE2(encoding=3, text=[v]),
        "TCOM": lambda v: TCOM(encoding=3, text=[v]),
        "TEXT": lambda v: TEXT(encoding=3, text=[v]),
        "TDRC": lambda v: TDRC(encoding=3, text=[v]),
        "TCON": lambda v: TCON(encoding=3, text=[v]),
        "TBPM": lambda v: TBPM(encoding=3, text=[v]),
        "TPOS": lambda v: TPOS(encoding=3, text=[v]),
        "USLT": lambda v: USLT(encoding=3, lang="XXX", desc="", text=v),
    }
    for key, value in id3_tags(meta).items():
        build = mapping.get(key)
        if build is not None:
            tag.add(build(value))
    if cover is not None:
        tag.add(APIC(encoding=3, mime=_mime_for(cover_path or ""),
                     type=3, desc="Cover", data=cover))
    tag.save(path)


def _write_vorbis(path: str, meta: TrackMeta, cover: bytes | None,
                  cover_path: str | None, *, is_flac: bool) -> None:
    from mutagen.flac import FLAC, Picture
    from mutagen.oggvorbis import OggVorbis

    handle = FLAC(path) if is_flac else OggVorbis(path)
    # 在内存里清空再一次性保存：delete() 自己就会落一次盘，配 save() 就成了两次全量重写
    if handle.tags is not None:
        handle.tags.clear()
    if is_flac:
        # 必须无条件清封面：酷狗文件里本来就可能嵌了自己的封面，
        # 只在「有封面可写」时才清会让 embed_cover=False 形同虚设。
        handle.clear_pictures()
    for key, value in vorbis_tags(meta).items():
        handle[key] = [value]
    if cover is not None:
        picture = Picture()
        picture.type = 3
        picture.mime = _mime_for(cover_path or "")
        picture.desc = "Cover"
        picture.data = cover
        if is_flac:
            handle.add_picture(picture)
        else:
            import base64
            handle["metadata_block_picture"] = [
                base64.b64encode(picture.write()).decode("ascii")
            ]
    handle.save()


def write_tags(path: str, meta: TrackMeta, extension: str,
               cover_path: str | None, limits: TagLimits | None = None) -> bool:
    """把标签写进文件。**同步**（调用方负责 `asyncio.to_thread`）。

    任何失败都返回 `False`，不抛异常——标签是加分项，不该拖垮下载。
    """
    container = str(extension or "").lower()
    if container not in _SUPPORTED:
        return False
    limits = limits or _DEFAULT_LIMITS
    try:
        cover = _cover_bytes(cover_path, limits)
        if container == "mp3":
            _write_id3(path, meta, cover, cover_path)
        else:
            _write_vorbis(path, meta, cover, cover_path, is_flac=(container == "flac"))
        return True
    except Exception as exc:
        logger.warning("[kugou] 写入标签失败（%s）：%s", container, type(exc).__name__)
        return False


def delete_tags(path: str, extension: str) -> bool:
    """清空文件里的标签（回退用：打标后探测不通过时把文件还原）。"""
    container = str(extension or "").lower()
    if container not in _SUPPORTED:
        return False
    try:
        if container == "mp3":
            from mutagen.id3 import ID3, ID3NoHeaderError
            try:
                tag = ID3(path)
            except ID3NoHeaderError:
                tag = ID3()
            tag.clear()
            tag.save(path)
        else:
            from mutagen.flac import FLAC
            from mutagen.oggvorbis import OggVorbis
            handle = FLAC(path) if container == "flac" else OggVorbis(path)
            if handle.tags is not None:
                handle.tags.clear()
            if container == "flac":
                handle.clear_pictures()      # delete() 不清封面，图片块必须显式清
            handle.save()
        return True
    except Exception as exc:
        logger.warning("[kugou] 清除标签失败（%s）：%s", container, type(exc).__name__)
        return False


# ── 编排层 ───────────────────────────────────────────────────────────────


class Tagger:
    """取元信息与封面，再在线程里把标签写进文件。

    `file_cache.AudioFetcher` 只依赖本类的 `apply` / `strip` 两个方法（鸭子类型），
    因此 `file_cache.py` 不需要 import tagger，更不需要认识 mutagen。
    """

    def __init__(self, client, covers, *, embed_cover: bool = True,
                 limits: TagLimits | None = None):
        self._client = client
        self._covers = covers
        self._embed_cover = embed_cover
        self._limits = limits or _DEFAULT_LIMITS

    async def apply(self, path: str, extension: str, song: dict) -> bool:
        """给 `path` 写标签。失败返回 `False`，**不抛异常**。"""
        try:
            meta = await collect_track_meta(self._client, song)
            cover = None
            if self._embed_cover and meta.cover_url and self._covers is not None:
                cover = await self._covers.ensure(meta.cover_url)
            # mutagen 是同步 I/O 且会全量重写文件，必须离开事件循环
            return await asyncio.to_thread(
                write_tags, path, meta, extension, cover, self._limits
            )
        except Exception as exc:
            logger.warning("[kugou] 打标签失败：%s", type(exc).__name__)
            return False

    def strip(self, path: str, extension: str) -> bool:
        """清掉 `apply` 写进去的标签（打标后探测不通过时的回退）。"""
        return delete_tags(path, extension)


