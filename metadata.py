"""酷狗歌曲详情与歌词：协议调用与归一化。

三个接口（结论与实测依据见设计稿 §3）：

1. ``POST gateway.kugou.com/kmr/v2/audio``（``x-router: openapi.kugou.com``）
   按 ``album_audio_id`` 取媒体信息（作词/作曲/发行日期/封面/曲风…）。
2. ``GET lyrics.kugou.com/v1/search``（**必须签名**）取歌词候选的 ``id`` + ``accesskey``。
3. ``GET lyrics.kugou.com/download``（**免签名**，但 ``id`` 与 ``accesskey`` 都必需）取 LRC 明文。

签名与请求管道一律复用 :class:`kugou_client.KugouClient` 已有实现，本模块不重新实现签名。
任何一环取不到都只降级为「缺这一项」，不向上抛（元信息除外，见 `fetch_track_meta`）。
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, replace

try:
    from .kugou_client import (
        GATEWAY, KugouApiError, default_params,
    )
except ImportError:  # 以顶层模块方式导入（离线测试）
    from kugou_client import (
        GATEWAY, KugouApiError, default_params,
    )

logger = logging.getLogger(__name__)

KRM_URL = GATEWAY + "/kmr/v2/audio"
KRM_ROUTER = "openapi.kugou.com"
KRM_FIELDS = "base,album_info,authors.base,extra,tags"

LYRIC_SEARCH_URL = "https://lyrics.kugou.com/v1/search"
LYRIC_DOWNLOAD_URL = "https://lyrics.kugou.com/download"

#: 嵌入标签用的封面尺寸（搜索卡片用的是 240，嵌入要更大）。
EMBED_COVER_SIZE = 500

#: 曲风白名单。`tags` 实测混入大量运营标签（「老年版精选」「抖音版（中央同步）」等），
#: 必须与白名单求交集；交集为空时留空，宁可不写也不能污染 TCON。
_GENRE_ALLOWLIST = frozenset({
    "流行", "摇滚", "民谣", "电子", "古典", "爵士", "嘻哈", "说唱", "R&B",
    "乡村", "金属", "朋克", "舞曲", "轻音乐", "ACG", "古风", "动漫", "民族",
})


@dataclass(frozen=True)
class TrackMeta:
    """一首歌可写入标签的媒体信息。取不到的项保持空值。"""

    title: str = ""
    artists: tuple[str, ...] = ()
    album: str = ""
    album_artist: str = ""
    composer: str = ""
    lyricist: str = ""
    date: str = ""
    genre: str = ""
    bpm: str = ""
    disc: str = ""
    cover_url: str = ""
    lyric: str = ""


# ── 纯解析 ───────────────────────────────────────────────────────────────


def _to_str(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _https_cover(template: str, size: int = EMBED_COVER_SIZE) -> str:
    """把 `{size}` 模板补成具体尺寸并升级为 https；不合规返回空串。"""
    text = _to_str(template).replace("{size}", str(size))
    if text.startswith("http://"):
        text = "https://" + text[len("http://"):]
    return text if text.startswith("https://") else ""


def pick_genre(tags) -> str:
    """从 tags 里挑一个已知曲风；挑不到返回空串。"""
    if not isinstance(tags, (list, tuple)):
        return ""
    for tag in tags:
        name = _to_str(tag.get("name") if isinstance(tag, dict) else tag)
        if name in _GENRE_ALLOWLIST:
            return name
    return ""


def parse_krm_entry(entry: dict) -> TrackMeta:
    """把 /kmr/audio 返回的单个 entry 归一化成 TrackMeta。"""
    if not isinstance(entry, dict):
        return TrackMeta()
    base = entry.get("base") if isinstance(entry.get("base"), dict) else {}
    album = entry.get("album_info") if isinstance(entry.get("album_info"), dict) else {}
    extra = entry.get("extra") if isinstance(entry.get("extra"), dict) else {}
    authors = entry.get("authors") if isinstance(entry.get("authors"), list) else []

    names: list[str] = []
    for author in authors:
        inner = author.get("base") if isinstance(author, dict) else None
        name = _to_str((inner or {}).get("author_name"))
        if name and name not in names:
            names.append(name)

    cover = _to_str(album.get("cover")) or _to_str(extra.get("cover"))

    return TrackMeta(
        title=_to_str(base.get("songname")),
        artists=tuple(names),
        album=_to_str(base.get("album_name") or album.get("album_name")),
        album_artist=names[0] if names else "",
        composer=_to_str(extra.get("composer")),
        lyricist=_to_str(extra.get("lyrics")),          # 注意：lyrics 字段装的是「作词」
        date=_to_str(base.get("publish_date") or album.get("publish_date")),
        genre=pick_genre(entry.get("tags")),
        bpm=_to_str(extra.get("bpm")),
        disc=_to_str(extra.get("disc")),
        cover_url=_https_cover(cover),
    )


def parse_lyric_candidate(payload: dict) -> tuple[str, str] | None:
    """从歌词搜索响应里取 (id, accesskey)；没有歌词或响应异常返回 None。

    判定看 **body 里的 status**，不是 HTTP 码：无歌词时是 HTTP 200 + ``status:404``；
    漏签名时是 HTTP 200 + 空 body（``api_get_json`` 已折成 ``{}``）。
    """
    if not isinstance(payload, dict) or _to_int(payload.get("status")) != 200:
        return None
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return None
    first = candidates[0]
    if not isinstance(first, dict):
        return None
    lyric_id = _to_str(first.get("id"))
    accesskey = _to_str(first.get("accesskey"))
    if not lyric_id or not accesskey:
        return None
    return lyric_id, accesskey


def decode_lyric_content(payload: dict) -> str:
    """解出 LRC 明文；加密的 KRC 一律返回空串（本次只取 lrc）。"""
    if not isinstance(payload, dict):
        return ""
    contenttype = payload.get("contenttype")
    # contenttype == 0 才是加密 KRC（反直觉，见设计稿 §3.3）；缺失时按「我们请求的是 lrc」当明文
    if contenttype is not None and _to_int(contenttype) == 0:
        return ""
    content = payload.get("content")
    if not isinstance(content, str) or not content:
        return ""
    try:
        return base64.b64decode(content).decode("utf-8", "replace")
    except (ValueError, TypeError):
        return ""


def _to_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ── 协议调用 ─────────────────────────────────────────────────────────────


async def fetch_track_meta(client, song: dict) -> TrackMeta:
    """取媒体信息。请求失败或上游报错时抛 `KugouApiError`（由调用方决定如何兜底）。"""
    body = {
        "data": [{"entity_id": _to_int(song.get("album_audio_id"))}],
        "fields": KRM_FIELDS,
    }
    params = default_params(client._device, client._credentials)
    payload = await client.api_post_json(KRM_URL, params, body, router=KRM_ROUTER)
    if _to_int(payload.get("status")) != 1:
        raise KugouApiError(
            f"/kmr/audio 返回 status={payload.get('status')} "
            f"error_code={payload.get('error_code')}"
        )
    entries = payload.get("data")
    if not isinstance(entries, list) or not entries:
        raise KugouApiError("/kmr/audio 没有返回条目")
    entry = entries[0]
    # entity_id 无效时上游仍回 status=1，只是条目里除了 __status 什么都没有
    # （实测 {"__status": 1}）。这不算成功：不抛错，调用方的搜索字段兜底就永不触发，
    # 最终写进文件的是一整套空标签。
    if not isinstance(entry, dict) or not any(
        entry.get(section) for section in ("base", "album_info", "authors", "extra")
    ):
        raise KugouApiError(
            f"/kmr/audio 返回了空条目（entity_id={_to_int(song.get('album_audio_id'))}）"
        )
    return parse_krm_entry(entry)


async def fetch_lyric(client, song: dict) -> str:
    """取 LRC 明文。任何失败（含「这首歌没有歌词」）都返回空串，**不抛异常**。"""
    try:
        search_params = default_params(client._device, client._credentials)
        search_params.update({
            "album_audio_id": _to_int(song.get("album_audio_id")),
            "duration": _to_int(song.get("duration")),
            "hash": str(song.get("hash") or ""),
            "keyword": str(song.get("songname") or ""),
            "lrctxt": 1,
            "man": "no",
        })
        payload = await client.api_get_json(LYRIC_SEARCH_URL, search_params)
        if not payload:
            # 漏签名时上游返回的是 HTTP 200 + 空 body。必须记日志：它与「这首歌没歌词」
            # 的表象一样，不区分就永远查不出来（设计稿 §8）。
            logger.warning("[kugou] 歌词搜索返回空 body（疑似签名失效），跳过歌词")
            return ""
        found = parse_lyric_candidate(payload)
        if found is None:
            return ""
        lyric_id, accesskey = found
        download_params = {
            "ver": 1,
            "client": "android",
            "id": lyric_id,
            "accesskey": accesskey,
            "fmt": "lrc",
            "charset": "utf8",
        }
        # /download 免签名（实测），也无需设备头
        payload = await client.api_get_json(
            LYRIC_DOWNLOAD_URL, download_params, sign=False
        )
        if _to_int(payload.get("status")) != 200:
            logger.debug(
                "[kugou] 歌词下载失败：status=%s error_code=%s",
                payload.get("status"), payload.get("error_code"),
            )
            return ""
        return decode_lyric_content(payload)
    except Exception as exc:      # 歌词是加分项，任何异常都降级为空
        logger.debug("[kugou] 取歌词失败：%s", type(exc).__name__)
        return ""


def _split_singers(text: str) -> tuple[str, ...]:
    """搜索结果里的 `singername` 用「、」分隔多歌手。"""
    parts = [part.strip() for part in str(text or "").split("、")]
    return tuple(part for part in parts if part)


async def collect_track_meta(client, song: dict) -> TrackMeta:
    """组装待写入的媒体信息。**永不抛异常。**

    元信息请求失败时退回搜索结果里已有的字段；歌词失败则只少歌词这一项。
    """
    try:
        meta = await fetch_track_meta(client, song)
    except Exception as exc:
        logger.debug("[kugou] 取歌曲详情失败，改用搜索字段兜底：%s", type(exc).__name__)
        artists = _split_singers(song.get("singername"))
        meta = TrackMeta(
            title=_to_str(song.get("songname")),
            artists=artists,
            album=_to_str(song.get("album")),
            album_artist=artists[0] if artists else "",
        )
    lyric = await fetch_lyric(client, song)
    if lyric:
        meta = replace(meta, lyric=lyric)
    return meta
