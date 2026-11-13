"""纯字节级音频头解析与音质档位判定。

不依赖第三方库、不做任何 I/O：输入是文件（或响应）开头的一段字节，
输出是编码规格与它实际能达到的档位。

设计文档 §5 要求「下载后依据音频头和可用响应元数据验证档位」，并明确
「不能只看 URL 后缀或 Content-Type」。因此：

- FLAC：解析 STREAMINFO 的采样率/位深/总样本数。位深 > 16 或采样率 > 48000 Hz
  才算 Hi-Res；普通 CD 规格 FLAC（16 bit / 44.1 kHz）标记为 FLAC。
  这只确认编码规格，不声称证明母带品质。
- MP3：解析帧头的码率与采样率，并识别 Xing/Info 标记。
- Ogg Vorbis：解析 BOS 页的标识头，取 nominal 码率。
- 其他容器（m4a/aac/DSD/ape/wav/wma…）与 HTML/JSON 错误页一律判为无效，
  由上层按不可用处理并降级，绝不当作音频发送。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

# ── 档位判定阈值（设计文档 §5：位深 > 16 或采样率 > 48000 Hz 才算 Hi-Res）──────

HIRES_MIN_BITS = 16
HIRES_MIN_SAMPLE_RATE = 48000
TIER_320_MIN_BITRATE = 256000
TIER_128_MIN_BITRATE = 96000

# ── MPEG 音频帧表 ────────────────────────────────────────────────────────

_BITRATES_V1_L3 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]
_BITRATES_V2_L3 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160]
_SAMPLE_RATES = {
    3: [44100, 48000, 32000],   # MPEG 1
    2: [22050, 24000, 16000],   # MPEG 2
    0: [11025, 12000, 8000],    # MPEG 2.5
}
_MPEG1 = 3
_LAYER3 = 1
_XING_SCAN_BYTES = 200
#: Layer III 每帧样本数：MPEG-1 为 1152，MPEG-2/2.5 为 576。
_SAMPLES_PER_FRAME_L3 = {3: 1152, 2: 576, 0: 576}

# ── Ogg 字节布局 ─────────────────────────────────────────────────────────

_OGG_HEADER_LEN = 27
_VORBIS_ID_MAGIC = b"\x01vorbis"
_VORBIS_ID_LEN = 28


@dataclass(frozen=True)
class AudioInfo:
    """一段音频头的解析结果。`valid` 为 False 时其余字段无意义。"""

    container: str = ""
    codec: str = ""
    sample_rate: int = 0
    bits_per_sample: int = 0
    bitrate: int = 0
    channels: int = 0
    duration_ms: int = 0
    vbr: bool = False
    valid: bool = False


_INVALID = AudioInfo()


def probe(head: bytes) -> AudioInfo:
    """按魔数解析音频头；任何解析失败都返回无效结果而不是抛异常。"""
    if not head:
        return _INVALID
    try:
        if head[:4] == b"fLaC":
            return _probe_flac(head)
        if head[:4] == b"OggS":
            return _probe_ogg(head)
        if head[:3] == b"ID3" or (len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
            return _probe_mp3(head)
    except Exception:  # 解析实现细节不应把异常泄漏给调用方
        return _INVALID
    return _INVALID


def refine_bitrate(info: AudioInfo, total_bytes: int) -> AudioInfo:
    """用实际文件大小修正 VBR MP3 的码率（帧头码率对 VBR 文件不代表平均值）。"""
    if (
        not info.valid
        or info.container != "mp3"
        or not info.vbr
        or info.duration_ms <= 0
        or total_bytes <= 0
    ):
        return info
    return replace(info, bitrate=total_bytes * 8000 // info.duration_ms)


def tier_of(info: AudioInfo) -> str:
    """按实测编码规格给出实际档位；不可识别或低于最低档位时返回空串。"""
    if not info.valid:
        return ""
    if info.container == "flac":
        if info.bits_per_sample > HIRES_MIN_BITS or info.sample_rate > HIRES_MIN_SAMPLE_RATE:
            return "hires"
        return "flac"
    if info.bitrate >= TIER_320_MIN_BITRATE:
        return "320"
    if info.bitrate >= TIER_128_MIN_BITRATE:
        return "128"
    return ""


def extension_of(info: AudioInfo) -> str:
    """实际音频格式对应的扩展名（不含点）。"""
    return {"flac": "flac", "mp3": "mp3", "ogg": "ogg"}.get(info.container, "")


# ── 各容器解析 ───────────────────────────────────────────────────────────


def _probe_flac(head: bytes) -> AudioInfo:
    # "fLaC"(4) + 元数据块头(4) + STREAMINFO(34)中的前 18 字节
    if len(head) < 26:
        return _INVALID
    if head[4] & 0x7F != 0x00:      # 首块必须是 STREAMINFO
        return _INVALID
    b = head
    sample_rate = (b[18] << 12) | (b[19] << 4) | (b[20] >> 4)
    channels = ((b[20] >> 1) & 0x07) + 1
    bits_per_sample = (((b[20] & 0x01) << 4) | (b[21] >> 4)) + 1
    total_samples = ((b[21] & 0x0F) << 32) | (b[22] << 24) | (b[23] << 16) | (b[24] << 8) | b[25]
    if sample_rate <= 0:
        return _INVALID
    return AudioInfo(
        container="flac",
        codec="flac",
        sample_rate=sample_rate,
        bits_per_sample=bits_per_sample,
        channels=channels,
        duration_ms=total_samples * 1000 // sample_rate,
        valid=True,
    )


def _probe_mp3(head: bytes) -> AudioInfo:
    offset = _skip_id3v2(head)
    frame_at = _find_frame_sync(head, offset)
    if frame_at is None:
        return _INVALID

    b1, b2 = head[frame_at + 1], head[frame_at + 2]
    version = (b1 >> 3) & 0x03
    layer = (b1 >> 1) & 0x03
    bitrate_idx = b2 >> 4
    sample_rate_idx = (b2 >> 2) & 0x03
    if version == 1 or layer != _LAYER3:
        return _INVALID
    if bitrate_idx in (0, 15) or sample_rate_idx == 3:
        return _INVALID

    table = _BITRATES_V1_L3 if version == _MPEG1 else _BITRATES_V2_L3
    tail = head[frame_at : frame_at + _XING_SCAN_BYTES]
    sample_rate = _SAMPLE_RATES[version][sample_rate_idx]
    vbr = b"Xing" in tail or b"Info" in tail
    return AudioInfo(
        container="mp3",
        codec="mp3",
        sample_rate=sample_rate,
        bitrate=table[bitrate_idx] * 1000,
        channels=2 if (head[frame_at + 3] >> 6) != 3 else 1,
        duration_ms=(
            _xing_duration_ms(tail, _SAMPLES_PER_FRAME_L3[version], sample_rate)
            if vbr
            else 0
        ),
        vbr=vbr,
        valid=True,
    )


def _xing_duration_ms(tail: bytes, samples_per_frame: int, sample_rate: int) -> int:
    """从 Xing/Info 头的帧计数推算时长；无帧计数时返回 0。

    VBR 文件每帧样本数固定，帧数 × 每帧样本数 ÷ 采样率即总时长，
    因此无需读完整文件就能得到真实时长（进而修正平均码率）。
    """
    if sample_rate <= 0:
        return 0
    for magic in (b"Xing", b"Info"):
        idx = tail.find(magic)
        if idx < 0 or idx + 12 > len(tail):
            continue
        flags = int.from_bytes(tail[idx + 4 : idx + 8], "big")
        if not flags & 0x0001:          # 未记录帧数
            continue
        frames = int.from_bytes(tail[idx + 8 : idx + 12], "big")
        if frames <= 0:
            continue
        return frames * samples_per_frame * 1000 // sample_rate
    return 0


def _skip_id3v2(head: bytes) -> int:
    if head[:3] != b"ID3":
        return 0
    if len(head) < 10:
        return 0
    size = 0
    for byte in head[6:10]:
        if byte & 0x80:     # synchsafe 要求每字节最高位为 0
            return 0
        size = (size << 7) | byte
    offset = 10 + size
    if head[5] & 0x10:      # footer present
        offset += 10
    return offset


def _find_frame_sync(head: bytes, start: int) -> int | None:
    for i in range(start, len(head) - 3):
        if head[i] == 0xFF and head[i + 1] & 0xE0 == 0xE0:
            return i
    return None


def _probe_ogg(head: bytes) -> AudioInfo:
    if len(head) < _OGG_HEADER_LEN:
        return _INVALID
    segments = head[26]
    payload_at = _OGG_HEADER_LEN + segments
    if payload_at + _VORBIS_ID_LEN > len(head):
        return _INVALID
    if head[payload_at : payload_at + 7] != _VORBIS_ID_MAGIC:
        return _INVALID

    base = payload_at + 7
    channels = head[base + 4]
    sample_rate = int.from_bytes(head[base + 5 : base + 9], "little")
    nominal = int.from_bytes(head[base + 13 : base + 17], "little", signed=True)
    if sample_rate <= 0:
        return _INVALID
    return AudioInfo(
        container="ogg",
        codec="vorbis",
        sample_rate=sample_rate,
        bitrate=nominal if nominal > 0 else 0,
        channels=channels,
        valid=True,
    )
