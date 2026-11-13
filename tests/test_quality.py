import pytest

from quality import (
    DEFAULT_QUALITY_PRIORITY,
    TIERS,
    TIER_PROTOCOL_QUALITY,
    ConfigError,
    meets,
    parse_quality_priority,
    rank,
)


def test_none_and_blank_use_default():
    assert parse_quality_priority(None) == DEFAULT_QUALITY_PRIORITY
    assert parse_quality_priority("") == DEFAULT_QUALITY_PRIORITY
    assert parse_quality_priority("   ") == DEFAULT_QUALITY_PRIORITY


def test_default_list_accepted_as_is():
    assert parse_quality_priority(list(DEFAULT_QUALITY_PRIORITY)) == DEFAULT_QUALITY_PRIORITY
    assert parse_quality_priority(["flac", "320"]) == ["flac", "320"]


def test_dedupe_keeps_first_occurrence():
    assert parse_quality_priority(["flac", "hires", "flac", "FLAC"]) == ["flac", "hires"]


def test_aliases_normalized():
    assert parse_quality_priority(["Hi-Res", "hi_res", "FLAC", "320K", "128k"]) == TIERS


def test_comma_string_accepted():
    assert parse_quality_priority(" hires , flac ,320") == ["hires", "flac", "320"]


def test_invalid_value_is_error_not_silently_dropped():
    with pytest.raises(ConfigError) as e:
        parse_quality_priority(["hires", "bogus"])
    assert "bogus" in str(e.value)


def test_empty_list_is_error():
    with pytest.raises(ConfigError):
        parse_quality_priority([])


def test_blank_entry_is_error():
    with pytest.raises(ConfigError):
        parse_quality_priority(["hires", ""])


def test_rank_and_meets():
    assert rank("hires") == 4 and rank("128") == 1 and rank("") == 0 and rank("bogus") == 0
    assert meets("flac", "hires") is False
    assert meets("hires", "flac") is True
    assert meets("320", "320") is True
    assert meets("", "128") is False


def test_protocol_quality_never_hires_and_covers_all_tiers():
    assert set(TIER_PROTOCOL_QUALITY) == set(TIERS)
    assert "hires" not in TIER_PROTOCOL_QUALITY.values()
    assert TIER_PROTOCOL_QUALITY["hires"] == "high"
