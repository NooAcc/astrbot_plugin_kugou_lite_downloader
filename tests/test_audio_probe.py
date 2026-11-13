from dataclasses import replace

from audio_builders import (
    MPEG2,
    flac_head,
    html_page,
    id3v2,
    json_error,
    m4a_head,
    mp3_frame,
    ogg_vorbis_head,
    xing_header,
)

from audio_probe import extension_of, probe, refine_bitrate, tier_of


def test_flac_cd_spec_is_flac_not_hires():
    info = probe(flac_head(sample_rate=44100, bits=16))
    assert (info.valid, info.container, info.codec) == (True, "flac", "flac")
    assert (info.sample_rate, info.bits_per_sample, info.channels) == (44100, 16, 2)
    assert info.duration_ms == 10000
    assert tier_of(info) == "flac"
    assert extension_of(info) == "flac"


def test_flac_24bit_or_96k_is_hires():
    assert tier_of(probe(flac_head(bits=24, sample_rate=44100))) == "hires"
    assert tier_of(probe(flac_head(bits=16, sample_rate=96000))) == "hires"


def test_flac_48k_16bit_is_not_hires():
    assert tier_of(probe(flac_head(bits=16, sample_rate=48000))) == "flac"


def test_mp3_bitrate_bands():
    assert tier_of(probe(mp3_frame(9))) == "128"       # 128 kbps
    assert tier_of(probe(mp3_frame(13))) == "320"      # 256 kbps
    assert tier_of(probe(mp3_frame(5))) == ""          # 64 kbps，低于最低档位


def test_mp3_fields():
    info = probe(mp3_frame(9))
    assert (info.valid, info.container, info.codec) == (True, "mp3", "mp3")
    assert info.bitrate == 128000 and info.sample_rate == 44100
    assert extension_of(info) == "mp3"


def test_mp3_after_id3_tag_still_parsed():
    info = probe(id3v2(2000) + mp3_frame(9))
    assert info.valid and info.container == "mp3" and info.bitrate == 128000


def test_mp3_mpeg2_bitrate_table():
    info = probe(mp3_frame(9, version=MPEG2))
    assert info.valid and info.bitrate == 80000       # MPEG2 L3 索引 9 = 80 kbps
    assert info.sample_rate == 22050


def test_vbr_flag_set_from_xing_marker():
    info = refine_bitrate(probe(id3v2() + mp3_frame(9, tail=b"Xing")), total_bytes=8_000_000)
    assert info.vbr is True and info.bitrate > 0


def test_vbr_duration_comes_from_xing_frame_count():
    # MPEG1 Layer III 每帧 1152 个样本；1000 帧 ≈ 26.1 秒
    body = id3v2() + mp3_frame(9, tail=xing_header(frames=1000, byte_count=8_000_000))

    info = probe(body)

    assert info.vbr is True
    assert info.duration_ms == 1000 * 1152 * 1000 // 44100


def test_vbr_average_bitrate_replaces_frame_header_bitrate():
    body = id3v2() + mp3_frame(9, tail=xing_header(frames=1000, byte_count=8_000_000))
    info = probe(body)

    refined = refine_bitrate(info, total_bytes=8_000_000)

    assert refined.bitrate != info.bitrate                  # 平均码率确实被改写
    assert refined.bitrate == 8_000_000 * 8000 // info.duration_ms
    assert refine_bitrate(info, total_bytes=0) == info      # 未知大小时保持原值


def test_cbr_without_xing_keeps_frame_header_bitrate():
    info = probe(mp3_frame(9))
    assert info.vbr is False and info.duration_ms == 0
    assert refine_bitrate(info, total_bytes=8_000_000) == info


def test_vbr_average_uses_file_size_when_duration_known():
    info = replace(probe(id3v2() + mp3_frame(9, tail=b"Xing")), duration_ms=32000)
    refined = refine_bitrate(info, total_bytes=8_000_000)
    assert refined.bitrate == 2_000_000                                # 8MB / 32s = 2 Mbps


def test_ogg_vorbis_nominal_bitrate():
    assert tier_of(probe(ogg_vorbis_head(nominal=320000))) == "320"
    assert tier_of(probe(ogg_vorbis_head(nominal=128000))) == "128"
    info = probe(ogg_vorbis_head(nominal=128000))
    assert (info.valid, info.container, info.codec) == (True, "ogg", "vorbis")
    assert (info.sample_rate, info.channels) == (44100, 2)
    assert extension_of(info) == "ogg"


def test_non_audio_payloads_are_invalid():
    for payload in (html_page(), json_error(), b"\x00" * 64, b"", b"fLaC", b"OggS", m4a_head()):
        info = probe(payload)
        assert info.valid is False, payload[:8]
        assert tier_of(info) == "" and extension_of(info) == ""


def test_truncated_flac_header_does_not_crash():
    assert probe(flac_head()[:12]).valid is False
    assert probe(flac_head()[:26]).valid is True      # 26 字节刚好够 STREAMINFO
