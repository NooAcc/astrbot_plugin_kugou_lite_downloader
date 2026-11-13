"""构造最小合法音频头的测试夹具。

这些构造器与 audio_probe 的解析器共享同一份字节布局定义（FLAC STREAMINFO 位域、
MP3 帧头位域、Ogg 页头 + Vorbis 标识头），因此能真实地验证解析结果。
"""

from __future__ import annotations

# ── FLAC ────────────────────────────────────────────────────────────────

_FLAC_BLOCK_STREAMINFO = 0x00
_FLAC_BLOCK_LAST = 0x80
_FLAC_STREAMINFO_LEN = 34


def flac_head(sample_rate: int = 44100, bits: int = 16, channels: int = 2,
              total_samples: int = 44100 * 10) -> bytes:
    """返回 "fLaC" + 一个 last-STREAMINFO 块头 + 34 字节 STREAMINFO。"""
    packed = (
        ((sample_rate & 0xFFFFF) << 44)
        | (((channels - 1) & 0x07) << 41)
        | (((bits - 1) & 0x1F) << 36)
        | (total_samples & 0xFFFFFFFFF)
    )
    streaminfo = bytearray(_FLAC_STREAMINFO_LEN)
    streaminfo[0:2] = (4096).to_bytes(2, "big")       # min blocksize
    streaminfo[2:4] = (4096).to_bytes(2, "big")       # max blocksize
    streaminfo[4:7] = (1024).to_bytes(3, "big")       # min framesize
    streaminfo[7:10] = (8192).to_bytes(3, "big")      # max framesize
    streaminfo[10:18] = packed.to_bytes(8, "big")
    header = bytes([_FLAC_BLOCK_LAST | _FLAC_BLOCK_STREAMINFO, 0x00, 0x00, _FLAC_STREAMINFO_LEN])
    return b"fLaC" + header + bytes(streaminfo)


# ── MP3 ─────────────────────────────────────────────────────────────────

BITRATES_V1_L3 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]
BITRATES_V2_L3 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160]
SAMPLE_RATES = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}

MPEG1 = 3
MPEG2 = 2
MPEG25 = 0
LAYER3 = 1


def mp3_frame(bitrate_idx: int, sample_rate_idx: int = 0, version: int = MPEG1,
              layer: int = LAYER3, padding: int = 0, tail: bytes = b"") -> bytes:
    """返回一个合法的 MPEG 音频帧头（4 字节），可附加 tail（如 b"Xing"）。"""
    b0 = 0xFF
    b1 = 0xE0 | ((version & 0x03) << 3) | ((layer & 0x03) << 1) | 0x01
    b2 = ((bitrate_idx & 0x0F) << 4) | ((sample_rate_idx & 0x03) << 2) | ((padding & 0x01) << 1)
    b3 = 0xC0
    return bytes([b0, b1, b2, b3]) + tail


def mp3_bitrate(bitrate_idx: int, version: int = MPEG1) -> int:
    table = BITRATES_V1_L3 if version == MPEG1 else BITRATES_V2_L3
    return table[bitrate_idx] * 1000


def id3v2(size: int = 2000) -> bytes:
    """返回一个声明 size 字节附加数据的 ID3v2 头，总长为 10 + size。"""
    synchsafe = bytes([
        (size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F,
    ])
    return b"ID3" + bytes([0x03, 0x00, 0x00]) + synchsafe + b"\x00" * size


def xing_header(frames: int, byte_count: int = 0) -> bytes:
    """Xing/Info 头：标记 + 4 字节 flags + frames（+ bytes）。"""
    flags = 0x0001 | (0x0002 if byte_count else 0)
    body = b"Xing" + flags.to_bytes(4, "big") + frames.to_bytes(4, "big")
    if byte_count:
        body += byte_count.to_bytes(4, "big")
    return body


# ── Ogg Vorbis ──────────────────────────────────────────────────────────


def ogg_vorbis_head(nominal: int = 128000, sample_rate: int = 44100,
                    channels: int = 2) -> bytes:
    """返回一个只含 Vorbis 标识头的 Ogg 页。"""
    payload = (
        b"\x01vorbis"
        + (0).to_bytes(4, "little")            # vorbis_version
        + bytes([channels])
        + sample_rate.to_bytes(4, "little")
        + (0).to_bytes(4, "little", signed=True)       # bitrate_max
        + nominal.to_bytes(4, "little", signed=True)   # bitrate_nominal
        + (0).to_bytes(4, "little", signed=True)       # bitrate_min
    )
    page = (
        b"OggS"
        + bytes([0])                            # stream structure version
        + bytes([2])                            # header type: BOS
        + (0).to_bytes(8, "little")             # granule position
        + (0).to_bytes(4, "little")             # bitstream serial
        + (1).to_bytes(4, "little")             # page sequence
        + (0).to_bytes(4, "little")             # CRC
        + bytes([1])                            # page segments
        + bytes([len(payload)])                 # segment table
        + payload
    )
    return page


# ── 非音频载荷 ───────────────────────────────────────────────────────────


def html_page() -> bytes:
    return b"<!DOCTYPE html>\n<html><body><h1>403 Forbidden</h1></body></html>"


def json_error() -> bytes:
    return b'{"status":0,"error":"need login","error_code":152}'


def m4a_head() -> bytes:
    return b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 32
