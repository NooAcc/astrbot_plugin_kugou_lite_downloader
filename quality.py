"""音质档位词汇表与 `quality_priority` 配置解析。

本模块只做纯数据与纯函数处理，不导入 astrbot 或任何网络库，便于离线测试。

协议结论（见 docs/superpowers/specs/2026-11-13-kugou-lite-file-sending-design.md
与 docs/superpowers/plans/2026-11-13-kugou-lite-file-sending.md 的「协议决策」）：

- 配置档位 `hires` 对应的上游协议参数是 `quality=high`，
  对应权益接口 `relate_goods` 的 `level=6`（上游文档：`2=128、4=320、5=flac、6=Hi-Res、7=DSD`）。
- 上游**不存在** `hires` 参数，因此协议参数与配置标识分开维护。
- 档位标签以下载后音频头实测规格为准：位深 > 16 或采样率 > 48000 Hz 才算 Hi-Res。
"""

from __future__ import annotations

TIERS = ["hires", "flac", "320", "128"]

TIER_RANK = {"hires": 4, "flac": 3, "320": 2, "128": 1}

TIER_LABEL = {"hires": "Hi-Res", "flac": "FLAC", "320": "320K", "128": "128K"}

TIER_PROTOCOL_QUALITY = {"hires": "high", "flac": "flac", "320": "320", "128": "128"}

TIER_RELATE_LEVELS = {"hires": [6], "flac": [5], "320": [4], "128": [1, 2]}

DEFAULT_QUALITY_PRIORITY = ["hires", "flac", "320", "128"]

_TIER_ALIASES = {
    "hires": "hires",
    "hi-res": "hires",
    "hi_res": "hires",
    "hi res": "hires",
    "flac": "flac",
    "lossless": "flac",
    "320": "320",
    "320k": "320",
    "320kbps": "320",
    "128": "128",
    "128k": "128",
    "128kbps": "128",
    "standard": "128",
}

_TIER_HINT = "hires, flac, 320, 128"


class ConfigError(ValueError):
    """配置非法。调用方应把消息原样展示给用户，而不是静默兜底。"""


def parse_quality_priority(raw: object) -> list[str]:
    """把 WebUI 配置值解析成有序档位列表。

    - 未配置（None / 空串 / 纯空白）→ 默认优先级。
    - 空列表、空项、未知档位 → ConfigError（不静默丢弃，也不追加用户没选的档位）。
    - 支持字符串（逗号分隔）与列表；去重并保持首次出现顺序。
    """
    if raw is None:
        return list(DEFAULT_QUALITY_PRIORITY)

    if isinstance(raw, str):
        if not raw.strip():
            return list(DEFAULT_QUALITY_PRIORITY)
        items: list[object] = raw.split(",")
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        raise ConfigError(f"音质优先级格式无法识别：{type(raw).__name__}（可用：{_TIER_HINT}）")

    if not items:
        raise ConfigError(f"音质优先级不能为空（可用：{_TIER_HINT}）")

    resolved: list[str] = []
    for item in items:
        text = str(item).strip().lower()
        if not text:
            raise ConfigError("音质优先级包含空项")
        tier = _TIER_ALIASES.get(text)
        if tier is None:
            raise ConfigError(f"未知音质档位：{text!r}（可用：{_TIER_HINT}）")
        if tier not in resolved:
            resolved.append(tier)

    if not resolved:
        raise ConfigError(f"音质优先级不能为空（可用：{_TIER_HINT}）")
    return resolved


def rank(tier: str) -> int:
    """档位高低排名；未知/空档位返回 0（低于所有档位）。"""
    return TIER_RANK.get(tier, 0)


def meets(actual: str, requested: str) -> bool:
    """实测档位是否达到请求档位。"""
    requested_rank = rank(requested)
    return requested_rank > 0 and rank(actual) >= requested_rank


def label(tier: str) -> str:
    """用于文件名与消息展示的档位标签。"""
    return TIER_LABEL.get(tier, tier.upper())
