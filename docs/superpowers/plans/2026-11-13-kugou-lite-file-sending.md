# 酷狗概念版歌曲文件发送 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把现有酷狗点歌插件改造成「只做酷狗概念版（Lite）歌曲文件发送」的插件：搜歌 → 选序号 → 按配置音质优先级取原始音频 → 通过 OneBot 群/私聊文件接口发送文件附件。

**Architecture:** 现有 `main.py` 1584 行同时承担协议、卡片、缓存、命令、登录五件事。按职责拆成 5 个可离线测试的纯/协议模块 + 1 个 AstrBot 胶水模块：`quality.py`（档位词汇表与配置解析）、`audio_probe.py`（音频头解析与档位判定）、`file_cache.py`（安全文件名、URL 安全、受限流式下载、缓存归属与清理）、`kugou_client.py`（Lite 常量与签名、搜索、多档位资源解析、档位降级编排）、`kugou_login.py`（Lite 扫码/微信登录协议）、`main.py`（命令、配置、生命周期、OneBot 上传）。除 `main.py` 外全部不依赖 `astrbot`，可离线测试；HTTP 会话按参数注入，测试注入假会话。

**Tech Stack:** Python 3.9+（本机 3.12.9）、`aiohttp`（已装 3.14.5）、`pycryptodome`（3.17）、`pytest`（9.1.1）。不新增运行期依赖。

**Spec:** `docs/superpowers/specs/2026-11-13-kugou-lite-file-sending-design.md`

## Global Constraints

以下为设计文档锁定的项目级要求，每个任务都隐含遵守：

- 仅支持酷狗概念版 Lite：`appid=3116`、`clientver=11440`；删除标准版分支与版本开关（`kugou_concept`）。
- 平台只声明 `aiocqhttp`；删除 `qq_official` 相关说明与分支。
- 只发送音频文件附件。不发送音乐卡片、语音、封面图片、播放直链；删除卡片构造、OIAPI/咸鱼签名、跳转链接逻辑。
- 配置新增 `quality_priority`（有序字符串列表，默认 `["hires","flac","320","128"]`）与 `shared_cache_dir`（绝对路径文本，默认空）。
- 保留 `kugou_cookie`、`kugou_ua`、`search_count`、`timeout`、`debug`。删除 `kugou_concept`、`card_jump`、`music_card_key`、`card_format`。
- 音频不转码、不改写标签；扩展名以实际音频格式为准；文件名 `歌手 - 歌名 [实际音质].扩展名`。
- 内部安全默认值不新增配置：缓存保留 1 小时、单次下载上限 200 MiB、下载超时 180 秒、并发下载最多 2、搜索缓存 5 分钟。
- 搜索缓存按「会话 + 发起用户」隔离。
- 凭据与搜索状态放在 AstrBot 插件数据目录，绝不放共享音频目录。
- 不引入 ffmpeg、大而全音乐 SDK、第三方解析/签名服务；不新增运行期依赖（`aiohttp`、`pycryptodome` 继续使用）。
- 不绕过验证、权限、版权与加密限制；不用试听片段冒充完整歌曲。
- 不记录或输出 cookie/token/签名/设备指纹/音频直链。
- 不执行 `git commit` / `git push`：设计文档未授权提交与推送。每个任务以「测试通过 + 语法检查通过」作为完成标志，代替提交步骤。
- 文档、配置 schema、metadata 必须一致；metadata 版本升到 `2.0.0`。

---

## 协议决策（实现前已确认，来自上游公开协议材料）

上游参考：[KuGouMusicApi](https://github.com/MakcRe/KuGouMusicApi) 的 `module/song_url.js`、`module/privilege_lite.js`、`module/search.js`、`util/helper.js`、`util/request.js` 与 `docs/README.md`。

### Hi-Res 结论（设计文档 §5 要求先确认）

- `/song/url` 的 `quality` 参数**不存在 `hires`**；文档列出的取值为 `128` `320` `flac` `high` `viper_atmos` `viper_clear` `viper_tape` `super`，其中 `high` 只写「返回无损格式音频」。
- `module/privilege_lite.js` 头部注释给出上游权益接口的音质 level 表：**`2=128、4=320、5=flac、6=Hi-Res、7=DSD`**，并给出映射 `QUALITY_LEVEL = { 128: [1,2], 320: [4], flac: [5], high: [6], super: [7] }`。
- 因此实现取：**配置档位 `hires` → 协议 `quality=high` → `relate_goods` level 6（Hi-Res 档）**。请求参数与配置标识分开管理（`TIER_PROTOCOL_QUALITY`），不请求不存在的 `hires` 参数。
- 档位标签仍以**下载后音频头实测规格**为准：位深 > 16 或采样率 > 48000 Hz 才算 Hi-Res；普通 CD 规格 FLAC 标记为 FLAC。因此请求 `high` 拿到 16/44.1 FLAC 时，该文件按 FLAC 归档并降级复用，不标 Hi-Res。
- **交付说明不得声称 Hi-Res 已实测可用**：真实账号下的实际返回规格属于待验证项（需用户明确授权后测试）。

### Lite 常量

| 名称 | 值 |
| --- | --- |
| `APPID` | `3116` |
| `CLIENTVER` | `11440` |
| `SEARCH_CLIENTVER` | `201` |
| `SRCAPPID` | `2919` |
| `WEB_SIGN_SALT` | `NVPh5oo715z5DIWAeQlhMDsWXXQV4hwt` |
| `ANDROID_SALT` | `LnT6xpN3khm36zse0QzvmgTZ3waWdRSA` |
| `KEY_SALT` | `185672dd44712f60bb1736df5a377e82` |
| `URL_VERSION` / `URL_CLIENTVER` | `11430` |
| `URL_PAGE_ID` | `967177915` |
| `URL_PPAGE_ID` | `356753938,823673182,967485191` |
| `URL_PID` | `411` |
| `KG_UA` | `Android15-1070-11083-46-0-DiscoveryDRADProtocol-wifi` |
| `KG_STATIC_HEADERS` | `kg-rc: 1`、`kg-thash: 5d816a0`、`kg-rec: 1`、`kg-rf: B9EDA08A64250DEFFBCADDEE00F8F25F` |

签名（均 MD5，hex 小写）：

- `android_sign(params, body="")` → `md5(ANDROID_SALT + "".join(f"{k}={v}" for k in sorted(params)) + body + ANDROID_SALT)`；值为 dict/list 时用 `json.dumps(v, ensure_ascii=False, separators=(",",":"))`。`body` 为**实际发送的请求体字符串**。
- `web_sign(params, body="")` → 同上但盐为 `WEB_SIGN_SALT`（登录接口用）。
- `sign_key(hash, mid, userid, appid)` → `md5(f"{hash}{KEY_SALT}{appid}{mid}{userid or 0}")`。
- 设备标识：优先取 cookie 中的 `dfid`/`mid`/`uuid`；缺失时生成并持久化本机随机值（`uuid4().hex` / `random_string(24)`）。

### 端点与参数

| 用途 | 请求 | 说明 |
| --- | --- | --- |
| 概念版搜索（主） | `GET https://gateway.kugou.com/v2/search/song`，头 `x-router: complexsearch.kugou.com` | query: `keyword,page,pagesize,platform=AndroidFilter,iscorrection=1,privilegefilter=0,area_code=1,dopicfull=1,clientver=201,dfid,mid,uuid,appid=3116,clienttime[,token,userid]` + `signature=android_sign(query)`。需登录（无凭据返回 `error_code:152`） |
| 公共搜索（兼容来源） | `GET https://mobilecdn.kugou.com/api/v3/search/song` | query: `format=json,keyword,page=1,pagesize`，无需登录 |
| 权益/资源 hash | `POST https://gateway.kugou.com/v2/get_res_privilege/lite`，头 `x-router: media.store.kugou.com`、`Content-Type: application/json` | body（紧凑 JSON，签名覆盖该字符串）：`{"appid":3116,"area_code":1,"behavior":"play","clientver":11440,"need_hash_offset":1,"relate":1,"support_verify":1,"resource":[{"type":"audio","page_id":0,"hash":H,"album_id":A}],"qualities":["128","320","flac","high","viper_atmos","viper_tape","viper_clear","super","multitrack"]}`；query 为通用默认参数 + `signature` |
| 取播放地址 | `GET https://trackercdn.kugou.com/v5/url`（失败再试 `https://gateway.kugou.com/v5/url` + 头 `x-router: trackercdn.kugou.com`） | query 见下 |

`/v5/url` query（全部参数都进入签名）：`album_id, album_audio_id, appid=3116, area_code=1, behavior=play, cdnBackup=1, clienttime, clientver=11430, cmd=26, dfid, hash, IsFreePart=0, key=sign_key(hash,mid,userid,appid), mid, module="", page_id, pid=411, pidversion=3001, ppage_id, quality=<协议档位参数>, ssa_flag=is_fromtrack, token, userid, uuid, version=11430, signature=android_sign(query)`。

### 资源选择

- `relate_goods[]` 每项：`quality, level, privilege, status, pay_type, fail_process, hash, info{extname, bitrate, filesize}`；`trans_param.ogg_128_hash/ogg_128_filesize/ogg_320_hash/ogg_320_filesize` 为概念版实际播放的 ogg 变体。
- `128`：优先 `trans_param.ogg_128_hash`，否则 `level ∈ {1,2}` 的条目；`320`：优先 `ogg_320_hash`，否则 `level=4`；`flac`：`level=5`；`hires`：`level=6`。
- 可获取判定：`privilege == 0 且 status != 0`；概念版对 `128` 额外放宽（付费歌曲也可取标准音质）→ `tier == "128" and privilege != 0` 亦视为可获取。
- 不可获取的原因要分类返回：无该音质 / 需会员（`require_vip_type`：`suvip` 为超级 VIP）/ 服务端标记不可播 / 需要验证（`fail_process` 非 0 或 `error_code` 152）/ 网络错误。网络与配置错误不得伪装成「音质不可用」。

### 通用请求参数（除搜索/权益外的所有 Lite 请求）

query 中加入 `dfid, mid, uuid, appid=3116, clientver=11440, clienttime[,token,userid]` + `signature`；头中加入 `User-Agent: KG_UA`、`dfid`、`clienttime`、`mid` 以及 `KG_STATIC_HEADERS`。

### 设备指纹

设计文档允许「按正常协议处理或明确报错」。本计划采用：持久化本机随机 `dfid`/`mid`/`uuid`（cookie 中有则优先），不实现 `/register/dev` 设备注册（该接口响应为 AES 加密 + RSA 加密请求体，无真实账号无法验证正确性）。服务端返回「需要验证」类错误时给出可行动提示（重新扫码登录 / 检查网络）。此为已知限制，须写入交付说明。

---

## Review Focus

设计文档隐含、但没有任何任务的测试直接覆盖，且最容易让真实使用者踩到的五类输入/失败模式。每一条都在对应任务里补了测试。

1. **两个进程看到的共享目录不一致**（AstrBot 与 OneBot 端路径不同/Docker 未同挂载）：期望下载前检查目录可用性并明确报错，绝不发送半成品文件。→ Task 12。
2. **酷狗 CDN 返回允许后缀表之外的域名**：期望明确报错并指出该域名（提示扩充 `ALLOWED_HOST_SUFFIXES`），既不静默失败也不能误判成 SSRF。→ Task 3。
3. **同一首歌的高档位与低档位实际解析到同一资源**（例如请求 `high` 实得 CD 规格 FLAC）：期望复用已下载文件，只下载一次、只上传一次。→ Task 9。
4. **`/v5/url` 返回多个 CDN 地址且第一个不可用**：期望有限重试备用地址后成功，不因首个地址失败就判定整档不可用。→ Task 4。
5. **OneBot 实现不支持 `upload_group_file`**：期望明确报错，不回退发送卡片/语音/文字直链。→ Task 12。

---

## 文件结构

| 文件 | 职责 | 任务 |
| --- | --- | --- |
| `quality.py` | 档位词汇表（顺序、等级、标签、协议参数、relate level）、`quality_priority` 配置解析与校验 | 1 |
| `audio_probe.py` | 纯字节解析（FLAC STREAMINFO / MP3 帧头 / Ogg Vorbis ID 头）、档位判定、扩展名 | 2 |
| `file_cache.py` | 安全文件名、URL 安全（含逐跳重定向校验）、受限流式下载、缓存归属 manifest/存储/复用/清理、并发合并与探针校验 | 3,4,5 |
| `kugou_client.py` | Lite 常量、签名、脱敏、概念版搜索与公共回退、权益查询与档位资源解析、`/v5/url`、档位降级编排 `DownloadLadder` | 6,7,8,9 |
| `kugou_login.py` | Lite 扫码登录与微信登录的协议函数、cookie 校验与来源标签 | 10 |
| `main.py` | AstrBot `Star`：配置解析、会话、生命周期与任务清理、指令、OneBot 上传协调 | 11,12 |
| `_conf_schema.json` / `metadata.yaml` / `README.md` / `requirements-dev.txt` | 配置 schema、元数据、文档、开发依赖 | 13 |
| `tests/conftest.py` | 注入 `astrbot` 桩模块（本机未安装 astrbot），把仓库根加入 `sys.path` | 11 |
| `tests/audio_builders.py` | 构造最小合法音频头（FLAC/MP3/ID3/Ogg）的测试夹具 | 2 |
| `tests/fake_session.py` | 假 aiohttp 会话/响应（含分块流、重定向、异常注入） | 4 |
| `tests/test_quality.py` 等 | 各模块离线测试 | 1–12 |

模块划分说明：设计文档建议三个模块，本计划用五个协议/领域模块 + `main.py`。理由：档位词汇表被配置、探针、客户端三处共用且测试最密，独立成 `quality.py`；登录加解密与轮询协议与搜索/URL 解析无交集，独立成 `kugou_login.py`，否则 `kugou_client.py` 会重新长成一个数百行的多职责文件——正是本次要消除的问题。

测试运行方式：测试全部是同步函数，内部用 `asyncio.run()` 驱动协程，不依赖 `pytest-asyncio`。

---

### Task 1: `quality.py` — 档位词汇表与配置解析

**Files:**
- Create: `quality.py`
- Test: `tests/test_quality.py`

**Interfaces:**
- Produces:
  - `TIERS: list[str] = ["hires", "flac", "320", "128"]`（高 → 低）
  - `TIER_RANK: dict[str, int] = {"hires": 4, "flac": 3, "320": 2, "128": 1}`
  - `TIER_LABEL: dict[str, str] = {"hires": "Hi-Res", "flac": "FLAC", "320": "320K", "128": "128K"}`
  - `TIER_PROTOCOL_QUALITY: dict[str, str] = {"hires": "high", "flac": "flac", "320": "320", "128": "128"}`
  - `TIER_RELATE_LEVELS: dict[str, list[int]] = {"hires": [6], "flac": [5], "320": [4], "128": [1, 2]}`
  - `DEFAULT_QUALITY_PRIORITY: list[str] = ["hires", "flac", "320", "128"]`
  - `class ConfigError(ValueError)`
  - `parse_quality_priority(raw: object) -> list[str]`
  - `rank(tier: str) -> int`
  - `meets(actual: str, requested: str) -> bool`
  - `label(tier: str) -> str`

- [ ] **Step 1: 写失败测试** `tests/test_quality.py`

```python
import pytest
from quality import (ConfigError, DEFAULT_QUALITY_PRIORITY, TIERS,
                     TIER_PROTOCOL_QUALITY, meets, parse_quality_priority, rank)

def test_none_and_blank_use_default():
    assert parse_quality_priority(None) == DEFAULT_QUALITY_PRIORITY
    assert parse_quality_priority("") == DEFAULT_QUALITY_PRIORITY
    assert parse_quality_priority("   ") == DEFAULT_QUALITY_PRIORITY

def test_default_list_accepted_as_is():
    assert parse_quality_priority(list(DEFAULT_QUALITY_PRIORITY)) == DEFAULT_QUALITY_PRIORITY
    assert parse_quality_priority(["flac", "320"]) == ["flac", "320"]   # 允许调整顺序/删除档位

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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_quality.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'quality'`

- [ ] **Step 3: 实现 `quality.py`**

`parse_quality_priority(raw)` 规则：`None` / 非字符串非列表 / 去空白后为空字符串 → 返回 `DEFAULT_QUALITY_PRIORITY` 的副本；`str` 按 `,` 切分；`list`/`tuple` 逐项 `str()`；每项 `strip().lower()` 后查别名表（`hires|hi-res|hi_res|hires`、`flac|lossless`、`320|320k|high`？——**不要**接受 `high`，避免与 `hires` 混淆；`128|128k|standard`），未命中则 `raise ConfigError(f"未知音质档位：{item!r}（可用：hires, flac, 320, 128）")`；空项 → `ConfigError("音质优先级包含空项")`；全部合法但去重后为空（即原列表为空）→ `ConfigError("音质优先级不能为空")`。去重保持首次出现顺序。
`rank` 用 `TIER_RANK.get(tier, 0)`；`meets(actual, requested)` = `rank(actual) >= rank(requested) > 0`；`label` 用 `TIER_LABEL.get(tier, tier.upper())`。
模块顶部加 `from __future__ import annotations`；无第三方导入。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_quality.py -q`
Expected: 10 passed

---

### Task 2: `audio_probe.py` — 音频头解析与档位判定

**Files:**
- Create: `audio_probe.py`, `tests/audio_builders.py`
- Test: `tests/test_audio_probe.py`

**Interfaces:**
- Consumes: `quality.TIER_*` 常量
- Produces:
  - `@dataclass(frozen=True) class AudioInfo: container: str; codec: str; sample_rate: int; bits_per_sample: int; bitrate: int; channels: int; duration_ms: int; vbr: bool; valid: bool`
  - `probe(head: bytes) -> AudioInfo`
  - `refine_bitrate(info: AudioInfo, total_bytes: int) -> AudioInfo`（MP3/VBR 用实际文件大小算平均码率）
  - `tier_of(info: AudioInfo) -> str`（返回 `"hires"|"flac"|"320"|"128"|""`）
  - `extension_of(info: AudioInfo) -> str`（`"flac"|"mp3"|"ogg"|""`）

**头部字节布局（实现依据）：**
- FLAC：`b"fLaC"` + 元数据块头 4 字节（首块 `type = head[4] & 0x7F` 必须为 `0` STREAMINFO）；STREAMINFO 自偏移 8 起 18 字节：`head[8:10]`/`head[10:12]` 为 min/max blocksize，`head[12:15]`/`head[15:18]` 为 min/max framesize，随后 8 字节位域（自偏移 18）：`sample_rate = (b[18]<<12)|(b[19]<<4)|(b[20]>>4)`，`channels = ((b[20]>>1)&0x07)+1`，`bits_per_sample = (((b[20]&0x01)<<4)|(b[21]>>4))+1`，`total_samples = ((b[21]&0x0F)<<32)|(b[22]<<24)|(b[23]<<16)|(b[24]<<8)|b[25]`。
- MP3：可选的 `ID3` 头（v2 大小 = synchsafe `b[6:10]`，加 10，若 `b[5]&0x10` 再加 10）；从该偏移向后找 `0xFF` 且 `b+1 & 0xE0 == 0xE0` 的帧同步；版本位 `(b1>>3)&0x03`（3=MPEG1、2=MPEG2、0=MPEG2.5，1 非法），层位 `(b1>>1)&0x03`，码率索引 `b2>>4`，采样率索引 `(b2>>2)&0x03`，padding `(b2>>1)&0x01`；码率表 MPEG1-L3 `[0,32,40,48,56,64,80,96,112,128,160,192,224,256,320]`，MPEG2/2.5-L3 `[0,8,16,24,32,40,48,56,64,80,96,112,128,144,160]`；采样率表 MPEG1 `[44100,48000,32000]`、MPEG2 `[22050,24000,16000]`、MPEG2.5 `[11025,12000,8000]`。帧内 `"Xing"`/`"Info"` 标记 → `vbr=True`。
- Ogg：`b"OggS"`；页头固定 27 字节，段表长度 `head[26]`，载荷自 `27 + head[26]` 起；载荷前 7 字节为 `b"\x01vorbis"`，随后 `version u32le`、`channels u8`、`sample_rate u32le`、`bitrate_max s32le`、`bitrate_nominal s32le`、`bitrate_min s32le`（有符号；≤0 视为未知）。

- [ ] **Step 1: 写测试夹具** `tests/audio_builders.py`：`flac_head(sample_rate=44100, bits=16, channels=2, total_samples=44100*10)`、`mp3_frame(bitrate_idx, sample_rate_idx=0, version=3, layer=1, padding=0)`、`id3v2(size=2000)`、`ogg_vorbis_head(nominal=128000, sample_rate=44100, channels=2)`、`html_page()`、`json_error()` 等构造函数（按上面布局拼字节）。

- [ ] **Step 2: 写失败测试** `tests/test_audio_probe.py`

```python
import pytest
from audio_builders import flac_head, html_page, id3v2, json_error, mp3_frame, ogg_vorbis_head
from audio_probe import extension_of, probe, refine_bitrate, tier_of

def test_flac_cd_spec_is_flac_not_hires():
    i = probe(flac_head(sample_rate=44100, bits=16))
    assert (i.valid, i.container, i.codec) == (True, "flac", "flac")
    assert (i.sample_rate, i.bits_per_sample) == (44100, 16)
    assert i.duration_ms == 10000 and tier_of(i) == "flac"

def test_flac_24bit_or_96k_is_hires():
    assert tier_of(probe(flac_head(bits=24, sample_rate=44100))) == "hires"
    assert tier_of(probe(flac_head(bits=16, sample_rate=96000))) == "hires"
    assert tier_of(probe(flac_head(bits=16, sample_rate=48000))) == "flac"   # 48k 不算 Hi-Res

def test_mp3_bitrate_bands():
    assert (tier_of(probe(mp3_frame(9))), ) == ("128",)      # 128 kbps
    assert tier_of(probe(mp3_frame(13))) == "320"            # 256 kbps 及以上算 320 档
    assert tier_of(probe(mp3_frame(5))) == "128"             # 64 kbps 仍属 128 档? 否 → 见下
```
（注：`mp3_frame(5)` 为 64 kbps，**低于最低档位**，`tier_of` 必须返回 `""`；测试按此断言：`assert tier_of(probe(mp3_frame(5))) == ""`。）

```python
def test_mp3_after_id3_tag_still_parsed():
    i = probe(id3v2(2000) + mp3_frame(9))
    assert i.valid and i.container == "mp3" and i.bitrate == 128000

def test_vbr_uses_average_from_real_size():
    i = refine_bitrate(probe(id3v2() + mp3_frame(9) + b"Xing"), total_bytes=8_000_000)
    assert i.vbr is True and i.bitrate > 0

def test_ogg_vorbis_nominal_bitrate():
    assert tier_of(probe(ogg_vorbis_head(nominal=320000))) == "320"
    assert tier_of(probe(ogg_vorbis_head(nominal=128000))) == "128"
    assert extension_of(probe(ogg_vorbis_head())) == "ogg"

def test_non_audio_payloads_are_invalid():
    for payload in (html_page(), json_error(), b"\x00" * 64, b"", b"fLaC", b"OggS"):
        assert probe(payload).valid is False
        assert tier_of(probe(payload)) == "" and extension_of(probe(payload)) == ""

def test_unsupported_container_is_invalid_for_ladder():
    m4a = b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 32
    assert probe(m4a).valid is False

def test_truncated_flac_header_does_not_crash():
    assert probe(flac_head()[:12]).valid is False
```

- [ ] **Step 3: 运行测试确认失败** — `python -m pytest tests/test_audio_probe.py -q`，Expected: FAIL（模块不存在）

- [ ] **Step 4: 实现 `audio_probe.py`**

`probe` 按魔数分派到 `_probe_flac` / `_probe_mp3` / `_probe_ogg`，任何解析异常与越界都返回 `AudioInfo(valid=False)`（用 `_INVALID` 常量）。`tier_of`：`flac` 且（`bits_per_sample > 16` 或 `sample_rate > 48000`）→ `"hires"`；`flac` → `"flac"`；有损（mp3/ogg）`bitrate >= 256000` → `"320"`；`bitrate >= 96000` → `"128"`；其余 → `""`。`extension_of` 直接映射 `container`。`refine_bitrate`：仅当 `container == "mp3"` 且 `duration_ms > 0` 且 `vbr` 时用 `total_bytes * 8000 // duration_ms` 覆盖 `bitrate`，否则原样返回。无第三方导入。

- [ ] **Step 5: 运行测试确认通过** — `python -m pytest tests/test_audio_probe.py -q`，Expected: all passed

---

### Task 3: `file_cache.py` — 安全文件名与 URL 安全

**Files:**
- Create: `file_cache.py`
- Test: `tests/test_file_cache.py`

**Interfaces:**
- Produces:
  - `class SharedDirError(Exception)`
  - `class DownloadError(Exception)`（带 `reason: str` 属性）
  - `class UnsafeUrlError(DownloadError)`
  - `ALLOWED_HOST_SUFFIXES: tuple[str, ...] = (".kugou.com", ".kglink.com", ".kgidc.cn")`
  - `sanitize_filename(name: str, max_len: int = 120) -> str`
  - `build_filename(artist: str, title: str, tier: str, ext: str) -> str`
  - `is_safe_url(url: str, resolver=None, allowed_suffixes=ALLOWED_HOST_SUFFIXES) -> tuple[bool, str]`

- [ ] **Step 1: 写失败测试**

```python
import pytest
from file_cache import SharedDirError, UnsafeUrlError, build_filename, is_safe_url, sanitize_filename

def test_sanitize_removes_path_separators_and_control_chars():
    assert sanitize_filename("../../etc/passwd") == ".._.._etc_passwd"
    assert sanitize_filename("a/b\\c:d*e?f\"g<h>i|j.mp3").endswith(".mp3")
    assert "/" not in sanitize_filename("a/b\\c") and "\\" not in sanitize_filename("a/b\\c")
    assert sanitize_filename("bad\x00name\x1f") == "badname"

def test_sanitize_windows_reserved_and_edges():
    assert sanitize_filename("CON.mp3").upper() != "CON.MP3"
    assert sanitize_filename("nul") != "nul"
    assert sanitize_filename("name.") == "name" and sanitize_filename("name ") == "name"
    assert sanitize_filename("   ") == "song" and sanitize_filename("") == "song"
    assert sanitize_filename("...") == "song"
    assert len(sanitize_filename("歌" * 500)) <= 120

def test_build_filename_shape():
    assert build_filename("周杰伦", "晴天", "flac", "flac") == "周杰伦 - 晴天 [FLAC].flac"
    assert build_filename("a/b", "c:d", "hires", "flac").startswith("a_b - c_d [Hi-Res]")

def test_url_rejects_scheme_credentials_and_unknown_origin():
    assert is_safe_url("ftp://fs.kugou.com/a.mp3")[0] is False
    assert is_safe_url("http://u:p@fs.kugou.com/a.mp3")[0] is False
    ok, why = is_safe_url("http://evil.example.com/a.mp3")
    assert ok is False and "evil.example.com" in why      # Review Focus #2：报错要点名域名

def test_url_rejects_private_loopback_and_reserved_ips():
    pub = lambda host: ["93.184.216.34"]
    priv = lambda host: ["10.0.0.7"]
    assert is_safe_url("http://fs.kugou.com/a.mp3", resolver=pub)[0] is True
    assert is_safe_url("http://fs.kugou.com/a.mp3", resolver=priv)[0] is False
    assert is_safe_url("http://127.0.0.1/a.mp3", resolver=pub)[0] is False
    assert is_safe_url("http://[::1]/a.mp3", resolver=pub)[0] is False
    assert is_safe_url("http://169.254.1.1/a.mp3", resolver=pub)[0] is False
    assert is_safe_url("http://kugou.com.evil.com/a.mp3", resolver=pub)[0] is False   # 后缀须为点边界

def test_url_resolver_failure_is_rejected():
    def boom(host): raise OSError("no dns")
    assert is_safe_url("http://fs.kugou.com/a.mp3", resolver=boom)[0] is False
```

- [ ] **Step 2: 运行测试确认失败** — `python -m pytest tests/test_file_cache.py -q`，Expected: FAIL

- [ ] **Step 3: 实现（Task 3 范围）**

`sanitize_filename`：把 `<>:"/\|?*` 与控制字符（含 `\x00-\x1f`、`\x7f`）替换为 `_`；`rstrip(" .")`；若去扩展名后的主干落在 Windows 保留名集合（`CON PRN AUX NUL COM1-9 LPT1-9`，比较不区分大小写）→ 前缀 `_`；空或全为 `.` → `"song"`；按 `max_len` 截断（保留扩展名：截主干再拼回扩展名）。
`build_filename` = `f"{sanitize_filename(artist)} - {sanitize_filename(title)} [{label(tier)}].{ext}"`，`label` 来自 `quality`。
`is_safe_url`：`urlsplit` → scheme 必须 `http|https`；`netloc` 不得含 `@`；端口若存在必须为 80/443 或正整数（否则拒绝）；host 小写后：若是 IP 字面量（`ipaddress.ip_address`）→ 必须 `is_global`（拒绝 private/loopback/link-local/reserved/multicast/unspecified）；否则先查允许后缀（`host == s.lstrip(".")` 或 `host.endswith(s)`），再调用 `resolver(host)` 得到 IP 列表（默认实现用 `socket.getaddrinfo`，只保留 `AF_INET/AF_INET6`），全部必须 `is_global`，解析异常或空结果 → 拒绝。返回 `(False, 原因字符串)`。

- [ ] **Step 4: 运行测试确认通过** — `python -m pytest tests/test_file_cache.py -q`，Expected: all passed

---

### Task 4: `file_cache.py` — 受限流式下载（含逐跳重定向校验）

**Files:**
- Modify: `file_cache.py`
- Create: `tests/fake_session.py`
- Test: `tests/test_file_cache.py`（追加）

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Limits: max_bytes: int = 200 * 1024 * 1024; total_seconds: float = 180.0; max_concurrency: int = 2; max_redirects: int = 5; max_url_attempts: int = 2; head_bytes: int = 65536`
  - `@dataclass class DownloadOutcome: head: bytes; size: int; url: str; content_type: str`
  - `class Downloader:`
    - `__init__(self, session, limits: Limits = Limits(), resolver=None)`
    - `async def download_part(self, urls: list[str], part_path: str, on_abort=None) -> DownloadOutcome`
  - `def default_resolver(host: str) -> list[str]`

- [ ] **Step 1: 写假会话** `tests/fake_session.py`

```python
class FakeResponse:
    def __init__(self, chunks, status=200, headers=None, exc=None): ...
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    @property
    def content(self): return self
    def iter_chunked(self, n): ...        # 逐块 yield；遇 exc 抛出
class FakeSession:
    def __init__(self, responses): self.responses = list(responses); self.calls = []
    def get(self, url, **kw): self.calls.append((url, kw)); r = self.responses.pop(0)
```
（`FakeSession.get` 记录调用参数，用于断言 `allow_redirects=False` 与头，并支持 `responses` 里放异常实例表示连接错误。）

- [ ] **Step 2: 写失败测试**

```python
import asyncio, os
from fake_session import FakeResponse, FakeSession
from file_cache import DownloadError, Downloader, Limits, UnsafeUrlError
from file_cache import probe as _unused  # 不要这样导入；见下

def test_download_writes_part_and_returns_head(tmp_path):
    s = FakeSession([FakeResponse([b"A" * 1000, b"B" * 500], headers={"Content-Length": "1500"})])
    d = Downloader(s, resolver=lambda h: ["93.184.216.34"])
    out = asyncio.run(d.download_part(["http://fs.kugou.com/a.mp3"], str(tmp_path / "a.part")))
    assert out.size == 1500 and out.head[:1000] == b"A" * 1000
    assert (tmp_path / "a.part").stat().st_size == 1500
    assert s.calls[0][1]["allow_redirects"] is False        # 必须自行逐跳校验

def test_oversized_content_length_rejected_before_body(tmp_path):
    s = FakeSession([FakeResponse([b"x"], headers={"Content-Length": str(10**9)})])
    d = Downloader(s, Limits(max_bytes=1000), resolver=lambda h: ["93.184.216.34"])
    with pytest.raises(DownloadError) as e:
        asyncio.run(d.download_part(["http://fs.kugou.com/a.mp3"], str(tmp_path / "a.part")))
    assert "上限" in str(e.value.reason) and not (tmp_path / "a.part").exists()

def test_stream_over_limit_aborts_and_removes_part(tmp_path):
    chunks = [b"x" * 800] * 10                                    # 8000 > 1000
    s = FakeSession([FakeResponse(chunks)])
    ...  # 同上，断言 part 不存在

def test_short_body_vs_content_length_rejected(tmp_path): ...      # Content-Length 1500 但只给 1000
def test_http_error_status_rejected(tmp_path): ...                 # status=404
def test_empty_body_rejected(tmp_path): ...                        # 0 字节
def test_redirect_to_private_ip_is_rejected(tmp_path):
    s = FakeSession([FakeResponse([], status=302, headers={"Location": "http://127.0.0.1/x.mp3"})])
    with pytest.raises(UnsafeUrlError): ...
def test_too_many_redirects_rejected(tmp_path): ...
def test_alternate_url_used_when_first_fails(tmp_path):            # Review Focus #4：首个地址连接错误 → 用第二个
    s = FakeSession([OSError("conn reset"), FakeResponse([b"z" * 10])])
    out = asyncio.run(d.download_part(["http://a.kugou.com/1", "http://b.kugou.com/2"], part))
    assert out.size == 10 and out.url.endswith("/2")
def test_cancel_removes_part(tmp_path): ...                        # 流中途 raise asyncio.CancelledError
def test_timeout_removes_part(tmp_path): ...                       # 注入时钟，超过 total_seconds
```

- [ ] **Step 3: 运行测试确认失败** — `python -m pytest tests/test_file_cache.py -q`

- [ ] **Step 4: 实现 `download_part`**

流程：`asyncio.timeout` 不可用于 3.9，改用 `time.monotonic()` 截止时间判断（每块检查一次）。对 `urls` 逐个尝试（最多 `limits.max_url_attempts` 个），单个 url 内部处理重定向：每次 `is_safe_url(current_url, resolver=...)`，不安全 → `UnsafeUrlError`（**不换地址、不降级**，直接抛出让上层报配置/安全问题）；`status in (301,302,303,307,308)` → 读 `Location`（`urljoin` 相对地址）继续，超过 `max_redirects` → `DownloadError("重定向次数过多")`；`status != 200` → `DownloadError(f"HTTP {status}")`；`Content-Length` 存在且 `> max_bytes` → 提前 `DownloadError("文件超过单次下载上限")`；逐块写 `.part`（`open(..., "wb")`），累计超过 `max_bytes` 或超过截止时间 → 抛错；结束后：`size == 0` → 错；`Content-Length` 存在且 `size != 该值` → `DownloadError("下载不完整")`。**任何**失败/取消路径都在 `finally` 中关闭文件并删除 `.part`；网络类异常（`OSError` 及 `aiohttp.ClientError` 的名字匹配，不导入 aiohttp）在还有备用地址时转下一个地址，否则包装成 `DownloadError`。第一块起累计 `limits.head_bytes` 作为 `head`。请求头只带 `User-Agent: KG_UA`（从 `kugou_client` 导入会形成循环，改为 `Downloader(user_agent=...)` 构造参数，默认空字符串），**不携带 cookie**。

- [ ] **Step 5: 运行测试确认通过** — `python -m pytest tests/test_file_cache.py -q`

---

### Task 5: `file_cache.py` — 缓存归属、存储、复用、清理与并发合并

**Files:**
- Modify: `file_cache.py`
- Test: `tests/test_file_cache.py`（追加）

**Interfaces:**
- Produces:
  - `class CacheManager:`
    - `__init__(self, shared_root: str)`（`shared_root` 即 `<shared_cache_dir>/astrbot_plugin_kugou_music`）
    - `ensure_ready(self) -> None`（不可用则 `raise SharedDirError(原因)`）
    - `begin_task_dir(self) -> str`
    - `cached_path(self, resource_key: str, tier: str) -> str | None`
    - `store(self, part_path: str, resource_key: str, tier: str, filename: str) -> str`（创建目标目录 + `os.replace` 原子重命名 + 返回最终绝对路径）
    - `hold(self, path: str) -> None` / `release(self, path: str) -> None`
    - `cleanup(self, ttl_seconds: int = 3600) -> int`
    - `resource_key(self, song: dict) -> str`（`sha1(f"{hash}:{album_audio_id}".lower())[:16]`）
  - `@dataclass class FetchResult: path: str; filename: str; actual_tier: str; info: AudioInfo; url: str; size: int; reused: bool; duration_ms: int`
  - `class AudioFetcher:`
    - `__init__(self, downloader: Downloader, cache: CacheManager, limits: Limits = Limits())`
    - `async def cached(self, resource_key: str, tier: str) -> FetchResult | None`
    - `async def fetch(self, resource_key: str, requested_tier: str, urls: list[str]) -> FetchResult`
  - `MANIFEST_NAME = "manifest.json"`、`MANIFEST = {"plugin": "astrbot_plugin_kugou_music", "version": 1}`

- [ ] **Step 1: 写失败测试**

```python
def test_ensure_ready_creates_dir_and_manifest(tmp_path):
    cm = CacheManager(str(tmp_path / "astrbot_plugin_kugou_music"))
    cm.ensure_ready()
    assert (tmp_path / "astrbot_plugin_kugou_music" / "manifest.json").exists()

def test_ensure_ready_refuses_unowned_existing_dir(tmp_path):
    d = tmp_path / "astrbot_plugin_kugou_music"; d.mkdir(); (d / "user-song.mp3").write_bytes(b"x")
    with pytest.raises(SharedDirError): CacheManager(str(d)).ensure_ready()

def test_ensure_ready_rejects_relative_path():
    with pytest.raises(SharedDirError): CacheManager("relative/dir").ensure_ready()

def test_store_is_atomic_and_cached_path_finds_it(tmp_path): ...   # 无 .part 残留；cached_path 返回同一路径
def test_cached_path_returns_none_for_other_tier(tmp_path): ...
def test_cleanup_removes_expired_but_keeps_held(tmp_path): ...     # hold() 中的文件不被删除
def test_cleanup_ignores_symlinks_and_foreign_files(tmp_path): ...  # 目录内符号链接不跟随、不删除
def test_fetcher_rejects_non_audio_payload(tmp_path): ...          # 返回 HTML → DownloadError
def test_fetcher_reports_actual_tier_and_stores_by_it(tmp_path): ...# 请求 hires，实得 CD FLAC → actual_tier "flac"，文件名 [FLAC].flac
def test_cached_reuse_revalidates_head_and_drops_corrupt_entry(tmp_path): ...
def test_concurrent_same_resource_downloads_once(tmp_path): ...    # 两个 gather 任务 → FakeSession 只被调用一次
```

- [ ] **Step 2: 运行测试确认失败**

- [ ] **Step 3: 实现**

布局：`<shared_root>/manifest.json`、`<shared_root>/tmp/<uuid4>/audio.part`、`<shared_root>/cache/<resource_key>__<tier>/<filename>`。
`ensure_ready`：空/非绝对路径 → 错；`os.makedirs(shared_root, exist_ok=True)`；已存在且无 `manifest.json` → `SharedDirError("目录已存在且不是本插件创建的缓存目录，请清空或改用一个新目录")`；否则写入 manifest 并验证可写。设 `self._ready` 缓存。
`hold`/`release` 维护 `set[str]`（比较用 `os.path.normcase(os.path.abspath(p))`）。
`cleanup(ttl)`：只遍历 `cache/` 与 `tmp/` 一级子项，`os.scandir` 且 `is_dir(follow_symlinks=False)`；`tmp/` 超 300 秒、`cache/*` 超 `ttl` 秒（用子项目录 `mtime`）→ `shutil.rmtree`，跳过 `hold` 中的路径与仍在 `cache` 中被引用的。返回删除数量。
`AudioFetcher.cached`：`cache.cached_path` → 读前 64 KiB → `probe`；无效 → 删除该缓存目录、返回 `None`；有效 → 返回 `FetchResult(reused=True)`，`filename` 取路径 `basename`。
`AudioFetcher.fetch`：`self._inflight` 按 `(resource_key, requested_tier)` 合并（`asyncio.Future` + `try/finally` 清理）；信号量限流 `limits.max_concurrency`；`begin_task_dir` → `downloader.download_part` → `probe(head)` → `refine_bitrate(info, size)` → 无效 → `DownloadError("服务端返回的不是可识别的音频文件")`；`tier_of` 为空 → `DownloadError("音频规格无法识别或低于最低档位")` → `cache.store(...)`。成功后路径进入 `hold`，由调用方在发送完成后 `release`。

- [ ] **Step 4: 运行测试确认通过**

---

### Task 6: `kugou_client.py` — Lite 常量、签名与脱敏

**Files:**
- Create: `kugou_client.py`
- Test: `tests/test_kugou_client.py`

**Interfaces:**
- Produces:
  - 常量：`APPID, CLIENTVER, SEARCH_CLIENTVER, SRCAPPID, WEB_SIGN_SALT, ANDROID_SALT, KEY_SALT, URL_VERSION, URL_PAGE_ID, URL_PPAGE_ID, URL_PID, KG_UA, KG_STATIC_HEADERS, GATEWAY, SEARCH_ROUTER, PRIVILEGE_ROUTER, URL_ROUTERS: list[tuple[str, str | None]], PUBLIC_SEARCH_URL`
  - `android_sign(params: dict, body: str = "") -> str`
  - `web_sign(params: dict, body: str = "") -> str`
  - `sign_key(hash_value: str, mid: str, userid: str, appid: str = APPID) -> str`
  - `compact_json(obj) -> str`（`json.dumps(obj, ensure_ascii=False, separators=(",", ":"))`）
  - `@dataclass class Device: dfid: str; mid: str; uuid: str`
  - `default_params(device: Device, credentials: Credentials | None, clientver: int = CLIENTVER) -> dict`
  - `redact_secrets(value) -> object`

- [ ] **Step 1: 写失败测试**

```python
from kugou_client import (ANDROID_SALT, KEY_SALT, WEB_SIGN_SALT, android_sign, compact_json,
                          redact_secrets, sign_key, web_sign)

def test_android_sign_matches_upstream_algorithm():
    # 金标量：md5(盐 + "a=1b=2" + "" + 盐)，盐 = LnT6xpN3khm36zse0QzvmgTZ3waWdRSA
    assert android_sign({"b": 2, "a": "1"}) == "<实现时用 hashlib 独立算出并固化>"

def test_android_sign_includes_post_body():
    assert android_sign({"a": "1"}, body='{"x":1}') != android_sign({"a": "1"})
    assert android_sign({"resource": [{"type": "audio"}]}) == \
           "<md5(盐 + 'resource=' + compact_json([{'type':'audio'}]) + 盐)>"   # dict/list 走 compact_json

def test_web_and_key_sign_vectors():
    assert web_sign({"k": "v"}) == "<金标量>"
    assert sign_key("abc", "mid1", "123", "3116") == "<金标量>"

def test_compact_json_has_no_spaces():
    assert compact_json({"a": 1, "b": "中"}) == '{"a":1,"b":"中"}'

def test_redact_secrets_masks_credentials_and_urls():
    src = {"token": "t" * 20, "signature": "s" * 32, "dfid": "d" * 24,
           "url": "https://fs.kgidc.cn/a/b.mp3?sig=deadbeef", "n": [{"key": "k" * 32}],
           "songname": "晴天"}
    out = redact_secrets(src)
    assert out["token"] == "***" and out["signature"] == "***" and out["dfid"] == "***"
    assert out["url"] == "https://fs.kgidc.cn/a/b.mp3"     # 去掉查询串
    assert out["n"][0]["key"] == "***" and out["songname"] == "晴天"
    assert redact_secrets({"play_url": "http://fs.kugou.com/x.mp3?t=1"})["play_url"] == "http://fs.kugou.com/x.mp3"
```

- [ ] **Step 2: 运行测试确认失败**

- [ ] **Step 3: 实现**

`android_sign`：键排序后 `f"{k}={compact_json(v) if isinstance(v,(dict,list)) else v}"` 拼接，`md5((salt + joined + body + salt).encode()).hexdigest()`。`redact_secrets`：递归处理 dict/list/tuple；键名（小写）在 `{"token","key","signature","cookie","secret","password","authorization","dfid","mid","kg-thash","kg-rf"}` → `"***"`；键名在 `{"url","urls","play_url","jump_url","audio","musicurl"}` 或字符串值以 `http` 开头 → 去掉 query/fragment 后返回；其他字符串若长度 ≥ 24 且全为十六进制 → `"***"`。

- [ ] **Step 4: 运行测试确认通过**

---

### Task 7: `kugou_client.py` — 概念版搜索与公共回退

**Files:**
- Modify: `kugou_client.py`
- Test: `tests/test_kugou_client.py`（追加）

**Interfaces:**
- Produces:
  - `@dataclass class Credentials: token: str; userid: str`
  - `class KugouClient:`
    - `__init__(self, session, device: Device, credentials: Credentials | None = None, ua: str = "", timeout: float = 15.0)`
    - `async def search(self, keyword: str, count: int) -> list[dict]`（返回项：`hash, songname, singername, album_id, album_audio_id, duration, pay_type`）
    - `async def _search_lite(...)` / `async def _search_public(...)`（内部）
    - `async def api_get_json(self, url: str, params: dict, headers: dict | None = None, router: str | None = None) -> dict`
    - `async def api_post_json(self, url: str, params: dict, body_obj: dict, router: str | None = None) -> dict`

- [ ] **Step 1: 写失败测试**

```python
LITE_SEARCH_FIXTURE = {"status": 1, "data": {"info": [
    {"hash": "ABCDEF", "songname": "晴天", "singername": "周杰伦", "album_id": 123,
     "album_audio_id": 456, "duration": 269, "pay_type": 3}]}}
PUBLIC_SEARCH_FIXTURE = {"status": 1, "data": {"info": [
    {"hash": "FFFF", "songname": "晴天", "singername": "周杰伦", "album_id": "9",
     "album_audio_id": 8, "duration": 270, "pay_type": 0}]}}

def test_search_uses_lite_endpoint_with_expected_params():
    # 断言 URL = 网关 /v2/search/song，头含 x-router，query 含 clientver=201、appid=3116、signature，
    # 且不在 query 里出现 token/cookie（凭据按需注入，见下）
    ...
def test_search_normalizes_song_fields(): ...
def test_search_falls_back_to_public_when_lite_returns_error_152(): ...
def test_search_returns_empty_when_both_fail(): ...
def test_search_lists_shape_also_parsed(): ...          # data.lists（部分响应形态）
def test_no_credentials_still_searches_via_public(): ...
```

- [ ] **Step 2: 运行测试确认失败**

- [ ] **Step 3: 实现**

`search`：有凭据时先走 Lite（`GATEWAY + "/v2/search/song"`，`router=SEARCH_ROUTER`，query 用 `default_params(device, credentials, clientver=SEARCH_CLIENTVER)` 合并 `keyword,page=1,pagesize=count,platform=AndroidFilter,iscorrection=1,privilegefilter=0,area_code=1,dopicfull=1` 再算 `signature`）；`status != 1` 或抛错 → 回退 `_search_public`（`PUBLIC_SEARCH_URL`，无签名、无 cookie）；两者都空 → `[]`。归一化：`data.info` 优先，退 `data.lists`，字段兼容 `hash/FileHash`、`songname/SongName`、`singername/SingerName`、`album_id/AlbumID`、`album_audio_id/AlbumAudioID`、`duration/Duration`、`pay_type/PayType`；`duration` 若 > 1000 视为毫秒并换算为秒。`api_post_json`：body 字符串只序列化一次（`compact_json`），同时用于签名与请求体。
`tests/fake_session.py` 扩展：`FakeSession.get/post` 支持返回 `(status, json_obj)`。

- [ ] **Step 4: 运行测试确认通过**

---

### Task 8: `kugou_client.py` — 权益查询、档位资源解析与 `/v5/url`

**Files:**
- Modify: `kugou_client.py`
- Test: `tests/test_kugou_client.py`（追加）

**Interfaces:**
- Produces:
  - `@dataclass class QualityOption: tier: str; hash: str; extname: str; size: int; privilege: int; status: int; pay_type: int; require_vip_type: str; obtainable: bool; reason: str`
  - `def parse_privilege(payload: dict, song: dict) -> list[QualityOption]`（纯函数，可离线测试）
  - `async def privileges(self, song: dict) -> list[QualityOption]`
  - `@dataclass class ResolvedUrl: tier: str; urls: list[str]; extname: str; size: int; hash: str`
  - `class ResourceUnavailable(Exception)`：`__init__(self, tier: str, reason: str)`
  - `async def resolve_urls(self, song: dict, option: QualityOption) -> ResolvedUrl`

- [ ] **Step 1: 写失败测试**

```python
PRIVILEGE_FIXTURE = {"status": 1, "data": [{
    "hash": "ABC", "album_id": 1, "album_audio_id": 2, "name": "晴天", "singername": "周杰伦",
    "trans_param": {"ogg_128_hash": "O128", "ogg_128_filesize": 3100000,
                    "ogg_320_hash": "O320", "ogg_320_filesize": 9200000},
    "relate_goods": [
        {"quality": "128", "level": 1, "privilege": 0, "status": 1, "hash": "M128",
         "info": {"extname": "ogg", "bitrate": 128000, "filesize": 3000000}},
        {"quality": "320", "level": 4, "privilege": 0, "status": 1, "hash": "M320",
         "info": {"extname": "mp3", "bitrate": 320000, "filesize": 9000000}},
        {"quality": "flac", "level": 5, "privilege": 10, "status": 1, "pay_type": 3, "hash": "F",
         "info": {"extname": "flac", "bitrate": 900000, "filesize": 30000000}},
        {"quality": "high", "level": 6, "privilege": 10, "status": 1, "pay_type": 3, "hash": "H",
         "info": {"extname": "flac", "bitrate": 2300000, "filesize": 70000000}},
    ]}]}

def test_parse_privilege_prefers_ogg_hash_for_lossy_tiers():
    opts = {o.tier: o for o in parse_privilege(PRIVILEGE_FIXTURE, SONG)}
    assert opts["128"].hash == "O128" and opts["320"].hash == "O320"
    assert opts["hires"].hash == "H" and opts["flac"].hash == "F"      # 无损及以上用 relate_goods 的 hash

def test_obtainable_flags_and_reasons():
    opts = {o.tier: o for o in parse_privilege(PRIVILEGE_FIXTURE, SONG)}
    assert opts["128"].obtainable is True                              # 概念版付费歌曲可免费听标准音质
    assert opts["flac"].obtainable is False and "VIP" in opts["flac"].reason
    assert opts["flac"].require_vip_type == "concept"
def test_missing_quality_reason():
    assert "无此音质" in {o.tier: o for o in parse_privilege({"data": [{"relate_goods": []}]}, SONG)}["flac"].reason
def test_super_vip_tier_requires_suvip(): ...                          # viper_* / level 7 → require_vip_type "suvip"
def test_resolve_urls_requests_the_tier_hash_and_protocol_quality():
    # 断言 /v5/url query: hash=H, quality=high, key=sign_key(...), ssa_flag, ppage_id, page_id, pid,
    #   appid=3116, clientver=11430, signature 存在；头含 x-router
def test_resolve_urls_second_endpoint_used_when_first_fails(): ...      # 直连 trackercdn 失败 → 网关 + x-router
def test_resolve_urls_raises_with_reason_on_fail_process(): ...          # status!=1 且 fail_process/error 非空
def test_resolve_urls_requires_credentials_for_vip_tier(): ...            # 无凭据且档位需 VIP → ResourceUnavailable("需登录")
```

- [ ] **Step 2: 运行测试确认失败**

- [ ] **Step 3: 实现**

`parse_privilege`：取 `data[0]`（或按 `hash` 匹配），对 `TIERS` 每个档位调用 `pick(tier)`；`128/320` 先用 `trans_param.ogg_*_hash`（有则 `extname="ogg"`, `size` 用 `filesize`），否则用 `TIER_RELATE_LEVELS` 顺序匹配 `relate_goods`；`obtainable` 与 `reason` 按协议决策表生成；`require_vip_type` 由 `level >= 7` 或 `quality` 以 `viper_` 开头 → `"suvip"`，否则 `"concept"`。
`resolve_urls`：按 `URL_ROUTERS` 顺序尝试；query 组装按协议决策；`quality = TIER_PROTOCOL_QUALITY[tier]`；`key = sign_key(hash, mid, userid, APPID)`；响应 `status != 1` → 记 `error_code/error/fail_process` 到 `ResourceUnavailable.reason`；`url` 为空 → 空档位；成功返回 `ResolvedUrl(urls=list, ...)`。第一处失败时换下一处 endpoint（不换档位）。

- [ ] **Step 4: 运行测试确认通过**

---

### Task 9: `kugou_client.py` — 档位降级编排 `DownloadLadder`

**Files:**
- Modify: `kugou_client.py`
- Test: `tests/test_kugou_client.py`（追加）

**Interfaces:**
- Produces:
  - `@dataclass class DownloadOutcome: path: str; filename: str; tier: str; label: str; info: AudioInfo; url: str; from_cache: bool`
  - `class AllQualitiesFailed(Exception)`：`__init__(self, reasons: list[tuple[str, str]])`（档位 → 原因）
  - `class DownloadLadder:`
    - `__init__(self, client: KugouClient, fetcher: AudioFetcher, cache: CacheManager, quality_priority: list[str])`
    - `async def run(self, song: dict) -> DownloadOutcome`
    - `async def release(self, outcome: DownloadOutcome) -> None`

- [ ] **Step 1: 写失败测试**（注入假 `client`/`fetcher`/`cache` 对象）

```python
def test_uses_verified_cache_first_without_resolving(): ...     # 命中缓存 → client.resolve_urls 未被调用
def test_downloads_once_when_high_tier_yields_lower_actual_quality():
    # Review Focus #3：hires 请求实得 flac → 记入 cache(flac) → flac 档位命中缓存 → 只下载一次
    assert fake.calls == ["hires"] and fake.download_count == 1
    assert outcome.tier == "flac" and outcome.label == "FLAC"
def test_skips_unavailable_tier_and_continues():
    # hires 需 VIP → 继续 flac，成功后 reasons 里保留 hires 的原因但不抛错
def test_does_not_try_tiers_outside_configured_priority():
    # priority=["320"] 时，即使 flac 可用也不请求 flac
def test_all_tiers_unavailable_raises_with_reason_summary(): ...
def test_network_error_is_reported_as_itself_not_as_quality_unavailable():
    # DownloadError("连接失败") → 原因文本里含「网络」，不含「无此音质」
def test_config_error_propagates_to_caller(): ...                # SharedDirError 不被吞成档位不可用
```

- [ ] **Step 2: 运行测试确认失败**

- [ ] **Step 3: 实现**

`run`：`key = cache.resource_key(song)`；对 `quality_priority` 顺序：① `await fetcher.cached(key, tier)` 命中且 `meets(info_tier, tier)` → 构造 outcome（`from_cache=True`）返回；命中但档位不符 → 记原因继续；② `option = pick(self._options, tier)`（`_options` 由 `privileges(song)` 懒加载一次并缓存）→ 不可获取 → 记原因继续；③ `resolved = await client.resolve_urls(song, option)`；`ResourceUnavailable` → 记原因继续；④ `res = await fetcher.fetch(key, tier, resolved.urls)`；`DownloadError` → 记原因继续；⑤ `meets(res.actual_tier, tier)` → `DownloadOutcome(filename=build_filename(song["singername"], song["songname"], res.actual_tier, extension_of(res.info)), label=label(...))` 返回；否则记原因「请求 X 实得 Y」继续（文件已按实测档位入库，后续档位会命中）。`SharedDirError`/`UnsafeUrlError` **不**捕获为档位原因，直接上抛。全部失败 → `AllQualitiesFailed(reasons)`。`release`：`cache.release(outcome.path)` + 删除成功发送后的缓存目录由 `cleanup` 负责，不在此删除。

- [ ] **Step 4: 运行测试确认通过**

---

### Task 10: `kugou_login.py` — Lite 扫码 / 微信登录协议

**Files:**
- Create: `kugou_login.py`
- Test: `tests/test_kugou_login.py`

**Interfaces:**
- Produces:
  - `class LoginError(Exception)`
  - `@dataclass class QrSession: key: str; scan_url: str`
  - `@dataclass class QrState: status: int; token: str = ""; userid: str = ""`（`status`：0 过期、1 待扫、2 已扫、4 已确认）
  - `@dataclass class WxSession: uuid: str; auth_url: str`
  - `@dataclass class Credentials: token: str; userid: str; source: str`（与 `kugou_client.Credentials` 区分，`kugou_login` 的是「带来源的登录结果」）
  - `class LiteLogin:`
    - `__init__(self, session, device: Device, ua: str = "", timeout: float = 10.0)`
    - `async def qr_start(self) -> QrSession`
    - `async def qr_check(self, key: str) -> int`
    - `async def qr_credentials(self, key: str) -> Credentials`
    - `async def wx_start(self) -> WxSession`
    - `async def wx_poll_once(self, uuid: str) -> str`
    - `async def wx_exchange(self, code: str) -> Credentials`
  - `def validate_cookie(cookie: str) -> tuple[bool, str]`
  - `def cookie_source_label(saved: dict | None, configured: str) -> str`
  - `def extract_cookie_field(cookie: str, field: str) -> str`
  - `def build_cookie(token: str, userid: str) -> str`（`f"token={token}; userid={userid}; a_id={APPID}"`）

- [ ] **Step 1: 写失败测试**

```python
def test_qr_start_uses_lite_android_channel_and_web_signature(): ...  # plat=4, appid=3116, clientver=11440,
                                                                     # signature=web_sign(params), 返回 key/scan_url
def test_qr_check_status_codes_passthrough(): ...
def test_qr_credentials_extracts_token_and_userid(): ...
def test_wx_exchange_full_flow_with_fake_session(): ...              # access_token→openid→AES→RSA→login_by_openplat→解密 secu_params
def test_wx_exchange_rejects_when_kugou_status_not_1(): ...
def test_build_cookie_uses_lite_appid():
    assert build_cookie("t", "1") == "token=t; userid=1; a_id=3116"
def test_validate_cookie_rejects_standard_version_cookie():
    ok, why = validate_cookie("token=t; userid=1; a_id=1005")
    assert ok is False and "重新登录" in why
def test_validate_cookie_accepts_lite_and_bare_cookie():
    assert validate_cookie("token=t; userid=1; a_id=3116")[0] is True
    assert validate_cookie("token=t; userid=1")[0] is True      # 无版本字段不盲目判定，交给接口结果
def test_validate_cookie_rejects_empty():
    assert validate_cookie("")[0] is False
def test_cookie_source_label_distinguishes_sources():
    assert cookie_source_label({"source": "inline_qr"}, "") == "扫码登录"
    assert cookie_source_label({"source": "wx_wechat"}, "") == "微信登录"
    assert cookie_source_label(None, "token=t") == "WebUI 手动配置"
def test_no_secret_leaks_in_error_messages(): ...            # LoginError 文本不含 token/签名
```

- [ ] **Step 2: 运行测试确认失败**

- [ ] **Step 3: 实现**

从现有 `main.py` 搬运并**只保留 Lite 分支**：`_web_sign`、`_android_sign`（复用 `kugou_client.android_sign`）、`_aes_encrypt/_aes_decrypt`、`_rsa_encrypt`（原始 modPow + 左零填充，保留 `_RSA_LITE_N`）、`_qr_*`、`_wx_*`；删除 `_RSA_STANDARD_N`、标准版 appid/clientver、`_is_concept()` 分支、`_WX_APPID/_WX_SECRET`。所有请求注入 `session`，超时用显式参数。`wx_exchange` 内不打印 token。

- [ ] **Step 4: 运行测试确认通过**

---

### Task 11: `main.py` — 骨架、配置、生命周期与登录指令

**Files:**
- Rewrite: `main.py`
- Create: `tests/conftest.py`, `tests/test_main.py`
- Test: `tests/test_main.py`（追加）

**Interfaces:**
- Consumes: `quality.parse_quality_priority`、`kugou_client.KugouClient/Device/Credentials`、`kugou_login.LiteLogin/validate_cookie/cookie_source_label/build_cookie/extract_cookie_field`、`file_cache.CacheManager/Downloader/AudioFetcher/SharedDirError`
- Produces:
  - `class KugouMusic(Star)`，`__init__(self, context, config: dict)`
  - `def parse_shared_dir(raw: str) -> tuple[str, str]`（返回 `(绝对路径, 错误信息)`，错误信息非空表示配置无效）
  - `def plugin_data_dir() -> str`（`os.path.join(os.getcwd(), "data", "plugin_data", "astrbot_plugin_kugou_music")`）
  - `async def initialize(self)` / `async def terminate(self)`
  - 指令：`酷狗扫码登录`、`酷狗微信登录`、`酷狗退出登录`、`酷狗登录状态`（均 `PermissionType.ADMIN`）

- [ ] **Step 1: 写 `tests/conftest.py`**

在导入任何被测模块前，若 `importlib.util.find_spec("astrbot") is None`，向 `sys.modules` 注入最小的 `astrbot.api`、`astrbot.api.event`（`filter` 对象提供 `command(name)`/`permission_type(pt)` 装饰器并把名字记进 `filter.registered`）、`astrbot.api.star`（`Star.__init__(self, context)`、`Context`）、`astrbot.api.message_components`（`Plain`、`Image`）、`astrbot.api.logger`（走标准 `logging`）。同时把仓库根目录插入 `sys.path`。

- [ ] **Step 2: 写失败测试**

```python
import asyncio, json
from conftest import FakeEvent, make_plugin          # make_plugin(config) → (plugin, tmp paths)
from main import parse_shared_dir

def test_parse_shared_dir_validation():
    assert parse_shared_dir("")[1] != ""
    assert parse_shared_dir("relative/x")[1] != ""
    ok, err = parse_shared_dir(str(ABS_OK)); assert err == "" and ok.endswith("astrbot_plugin_kugou_music")

def test_login_status_hides_token_and_names_source(): ...
def test_logout_clears_only_plugin_managed_cookie(): ...
def test_save_failure_is_reported(): ...                 # _save_cookie 返回 False → 指令文本含失败提示
def test_terminate_cancels_tracked_tasks_and_closes_session(): ...
def test_invalid_quality_priority_config_is_reported_not_crashed(): ...
def test_debug_output_is_redacted(): ...                 # _flush_debug() 不含 token/url query
```

- [ ] **Step 3: 运行测试确认失败**

- [ ] **Step 4: 实现**

`__init__`：`super().__init__(context)`；保存 `config`；`_session=None`；`_tasks: set[asyncio.Task]`；`_closing=False`；`_debug: list`；`_search_cache` 从 `plugin_data_dir()/search_cache.json` 载入；`_cookie_file = plugin_data_dir()/cookie.json`；`_cache_manager`/`_fetcher`/`_ladder` 懒初始化（首次下载时按当前 `shared_cache_dir` 构造），因为配置可能尚未就绪。
`parse_shared_dir`：空 → `("", "未配置共享缓存目录 shared_cache_dir")`；非绝对 → 错误；否则返回 `(os.path.join(os.path.abspath(raw), "astrbot_plugin_kugou_music"), "")`。
`initialize`：`parse_shared_dir` 有错 → `logger.warning` 但不阻断（搜歌与登录仍可用）；可用则 `ensure_ready()` + `cleanup()`（异常只记日志）。
`terminate`：`_closing = True`；`cancel()` 所有 `_tasks` 并 `await asyncio.gather(*tasks, return_exceptions=True)`；关闭 `_session`。
登录指令：扫码登录轮询循环每轮检查 `self._closing` 后退出；微信登录的 110 秒撤回任务 `asyncio.create_task` 后加入 `_tasks` 并在完成回调里丢弃。两个指令都在成功后 `_save_cookie`，失败时输出「凭据保存失败」。`cmd_login_status` 显示 `userid`、脱敏 token、来源标签。

- [ ] **Step 5: 运行测试确认通过**

---

### Task 12: `main.py` — 搜歌、下载/点歌指令与 OneBot 文件上传

**Files:**
- Modify: `main.py`
- Test: `tests/test_main.py`（追加）

**Interfaces:**
- Produces:
  - `def search_cache_key(event) -> str`（`f"{event.unified_msg_origin}|{event.get_sender_id()}"`）
  - `def build_upload_request(is_group: bool, target_id: str, file_path: str, name: str) -> tuple[str, dict]`
  - `def check_upload_result(resp) -> tuple[bool, str]`
  - 指令：`酷狗搜歌`、`酷狗下载`、`酷狗点歌`（兼容入口，行为相同）

- [ ] **Step 1: 写失败测试**

```python
def test_build_upload_request_group_and_private():
    action, params = build_upload_request(True, "12345", "D:\\c\\a.flac", "周杰伦 - 晴天 [FLAC].flac")
    assert (action, params) == ("upload_group_file",
                                {"group_id": 12345, "file": "D:\\c\\a.flac", "name": "周杰伦 - 晴天 [FLAC].flac"})
    assert build_upload_request(False, "999", "p", "n")[0] == "upload_private_file"

def test_build_upload_request_rejects_bad_target_id():
    with pytest.raises(ValueError): build_upload_request(True, "abc", "p", "n")

def test_check_upload_result_variants():
    assert check_upload_result({"status": "ok", "retcode": 0})[0] is True
    assert check_upload_result({"status": "failed", "retcode": 0})[0] is False
    assert check_upload_result({"retcode": 1200, "msg": "bad"})[0] is False
    assert check_upload_result(None)[0] is False and check_upload_result({})[0] is False

def test_upload_failure_does_not_fall_back_to_card_or_link(): ...
    # api.call_action 抛异常 → 回复含失败原因；断言 event.chain_result 未被调用、无 Image/music 段
def test_upload_unsupported_action_returns_clear_error(): ...     # Review Focus #5
def test_download_reports_missing_shared_dir_and_skips_network(): ...
def test_download_reports_invalid_quality_priority_config(): ...
def test_search_cache_is_per_session_and_user(): ...              # 同群不同用户互不覆盖
def test_index_out_of_range_and_non_numeric_are_reported(): ...
def test_download_stops_event_propagation_on_success(): ...
def test_compat_command_酷狗点歌_behaves_identically(): ...        # 两个入口都注册且共享实现
```

- [ ] **Step 2: 运行测试确认失败**

- [ ] **Step 3: 实现**

`cmd_search`：`search_cache[session|user] = {"songs": [...], "expire_at": time.time()+300, "keyword": kw}` 持久化；输出编号/歌名/歌手/时长/VIP 标记；提示 `发送 /酷狗下载 序号`。
`_download_flow(index_str, event)`（`cmd_download` 与 `cmd_play` 共用，均 `yield`）：校验共享目录与 `quality_priority` 配置 → 从缓存取歌与序号校验 → `ladder.run(song)` → `build_upload_request` + `bot.api.call_action` → `check_upload_result`；成功 `yield` 一行结果并 `event.stop_event()`；失败 `yield` 明确原因（不降级成卡片/语音/链接），同样停止传播；`AllQualitiesFailed` → 输出各档位原因摘要；`finally` 中 `ladder.release(outcome)`。
回复中出现的路径与直链一律走 `redact_secrets`。

- [ ] **Step 4: 运行测试确认通过**

- [ ] **Step 5: 全量回归**

Run: `python -m py_compile main.py kugou_client.py kugou_login.py file_cache.py audio_probe.py quality.py && python -m pytest tests -q`

---

### Task 13: 配置 schema、元数据与文档

**Files:**
- Rewrite: `_conf_schema.json`, `metadata.yaml`, `README.md`
- Create: `requirements-dev.txt`

- [ ] **Step 1: 改写 `_conf_schema.json`**

保留 `kugou_cookie`（hint 改为「概念版登录凭据；也可用 /酷狗扫码登录 获取」）、`kugou_ua`、`search_count`、`debug`、`timeout`。新增：

```json
"quality_priority": {
  "description": "音质优先级（从高到低依次尝试）",
  "type": "list",
  "default": ["hires", "flac", "320", "128"],
  "hint": "可调整顺序或删除档位。可用值：hires=Hi-Res、flac=无损、320=高品 320kbps、128=标准。非法值会报配置错误。",
  "obvious_hint": true
},
"shared_cache_dir": {
  "description": "共享缓存目录（绝对路径）",
  "type": "string",
  "default": "",
  "hint": "AstrBot 与 OneBot 实现端必须能以同一路径读到该目录。留空则只能搜歌/登录，下载会提示配置问题。",
  "obvious_hint": true
}
```
删除 `kugou_concept`、`card_jump`、`music_card_key`、`card_format`。

- [ ] **Step 2: 改写 `metadata.yaml`**

`display_name: 酷狗歌曲文件发送`；`short_desc` 与 `desc` 描述搜歌 → 按音质优先级取原始音频 → OneBot 文件发送；`version: 2.0.0`；`support_platforms: [aiocqhttp]`；tags 增补「文件」；`desc` 中说明 OneBot 实现需支持 `upload_group_file`/`upload_private_file` 且共享目录同路径。

- [ ] **Step 3: 创建 `requirements-dev.txt`**：`pytest>=8`。

- [ ] **Step 4: 改写 `README.md`**

指令表（`/酷狗搜歌`、`/酷狗下载`、`/酷狗点歌`（兼容）、`/酷狗扫码登录`、`/酷狗微信登录`、`/酷狗退出登录`、`/酷狗登录状态`）、配置表（含 `quality_priority` 与 `shared_cache_dir`）、音质档位与判定说明（含 `high`=Hi-Res 档、按音频头实测标记、CD 规格 FLAC 标 FLAC）、限制与待验证项（无真实账号未实测、设备注册 dfid 未实现、CDN 域名允许后缀表可能需扩充、版权与授权提醒）、依赖说明（`aiohttp`、`pycryptodome`；测试需 `pytest`）。

- [ ] **Step 5: 校验一致性与语法**

Run: `python -c "import json;json.load(open('_conf_schema.json',encoding='utf-8'));print('schema ok')"` 与 `python -m py_compile main.py`
Expected: `schema ok`，无异常；自查 schema 中每个键都有代码读取，且代码读取的每个键都在 schema 中。

---

### Task 14: 整体验证与交付说明

**Files:**
- Test: 全量

- [ ] **Step 1: 语法检查** — `python -m py_compile main.py kugou_client.py kugou_login.py file_cache.py audio_probe.py quality.py`，Expected: 静默通过

- [ ] **Step 2: 全量测试** — `python -m pytest tests -q`，Expected: 全部通过（记录通过数）

- [ ] **Step 3: 残留符号扫描** — `grep -rn "kugou_concept\|card_jump\|music_card_key\|card_format\|qq_official\|_build_music_card\|_send_card\|oiapi\|xianyuw\|com.tencent.music.lua\|Image.fromURL" --include=*.py --include=*.json --include=*.yaml --include=*.md .`，Expected: 无输出

- [ ] **Step 4: 依赖与文档一致性** — `grep -rn "import" main.py kugou_client.py kugou_login.py file_cache.py audio_probe.py quality.py | grep -v "^.*:.*from \(astrbot\|Crypto\|\.\)"`，确认运行期只用到 `aiohttp`（仅 `main.py`）、`pycryptodome`（仅 `kugou_login.py`）与标准库；README 的依赖说明与此一致。

- [ ] **Step 5: 写交付说明**（写入最终回复，不新建文件）

必含：改动模块清单与职责；`quality_priority`/`shared_cache_dir` 的配置方式；Hi-Res 结论（`hires` → 协议 `high` → level 6；标签按音频头实测；**未在真实账号下实测**）；待验证项（真实账号搜索/多档位解析/下载/OneBot 附件到达、dfid 设备注册未实现、CDN 允许后缀表）；本次未执行 `git commit`/`git push`；未执行任何真实账号登录或网络下载测试。

---

## 自检记录

- **Spec 覆盖**：§1–§3 目标/交互 → Task 11/12；§4 配置 → Task 1/11/13；§5 搜索与音质/Hi-Res → Task 2/7/8/9（含协议确认）；§6 缓存与文件安全 → Task 3/4/5；§7 OneBot 发送 → Task 12；§8 登录保留与精简 → Task 10/11；§9 代码组织 → 文件结构（5 模块 + main.py，已说明偏离理由）；§10 验证标准 → Task 14 + 各任务测试；§11 参考与限制 → 协议决策 + 交付说明。
- **Review Focus 落地**：①→Task 12 共享目录检查；②→Task 3 域名报错；③→Task 9 单次下载；④→Task 4 备用地址；⑤→Task 12 上传不支持报错。
- **类型/命名一致性**：`AudioInfo`/`FetchResult`/`QualityOption`/`ResolvedUrl`/`DownloadOutcome`/`Credentials` 在任务间串联一致；`kugou_login.Credentials`（含 `source`）与 `kugou_client.Credentials`（`token/userid`）同名不同义，已在 Task 10 显式标注，实现时后者命名为 `kugou_client.Credentials` 供客户端使用，登录结果类型命名为 `kugou_login.LoginResult` 以避免混淆。
- **未授权事项**：真实账号登录、真实网络下载、OneBot 端到端发送、git 提交/推送均不在本计划内。
