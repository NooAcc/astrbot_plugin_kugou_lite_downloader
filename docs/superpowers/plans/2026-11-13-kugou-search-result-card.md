# 酷狗搜歌结果图片卡片 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `/酷狗搜歌` 的纯文本结果换成一张本地渲染的图片卡片，每行显示序号、封面、歌名、歌手、专辑与时长。

**Architecture:** 协议层（`kugou_client`）在归一化时补齐 `album` 与可直接请求的 `cover` URL；新增 `cover_cache.py` 负责封面安全抓取与落盘缓存；新增 `search_card.py` 用 Pillow 纯本地绘制卡片（不导入 astrbot、不联网）；`main.py` 串起来：搜歌 → 抓封面 → 渲染 → `image_result`，任何一步失败都回退到现有纯文本列表。

**Tech Stack:** Python 3.10+、Pillow、aiohttp、pytest（离线，网络全部 mock）。

**Spec:** `docs/superpowers/specs/2026-11-13-kugou-search-result-card-design.md`

## Global Constraints

- 只支持酷狗概念版与公共搜索两个来源；**不改动**下载链路（`file_cache` 的 `Downloader`/`CacheManager`/`AudioFetcher`/`DownloadLadder`）与登录链路（`kugou_login`）。
- 渲染必须**完全本地**（Pillow）；不得调用 `context.html_renderer` 或不联网的替代服务。
- `search_card.py` **不得导入 astrbot**（便于离线测试）；`cover_cache.py` 同理。
- 新增的唯一配置项是 `cache_ttl_hours`：整数、默认 `24`、滑块 1–720；非法值回落 24。
- `SEARCH_TTL_SECONDS = 300` **保持不变**（搜歌候选列表窗口）。
- `requirements.txt` 新增 `Pillow>=9.5.0`（显式覆盖旧计划「不新增运行期依赖」）。
- 版本号 `2.0.0` → `2.1.0`。
- 卡片视觉基线：画布宽 **800 px**；序号徽章 24×24、圆角 8、accent `#2f86bd` 白字；封面 56×56、圆角 8；歌名 15 px / 600；副行 12.5 px `#8a9097`（格式「歌手 · 专辑」）；正文 `#1a1c1e`；页眉/页脚分隔线 `#f1f2f3`、行间分隔线 `#f7f8f9`、页脚底 `#fafbfb`；页脚文案「发送 /酷狗下载 序号 获取歌曲文件」。
- 封面 URL 规则：`union_cover` 的 `{size}` → `240`，`http://` → `https://`；主机白名单 `.kugou.com`；跟随即 `file_cache.Downloader` 的策略（`allow_redirects=False`、每跳重新 `is_safe_url`、最多 5 跳）；并发上限 3、单张超时 5 s、整批超时 8 s、单张 8 MiB、像素上限 4000 万。
- `covers/` 与 `search_cards/` 位于插件数据目录（`plugin_data_dir()`），与音频缓存共用同一 TTL 值，但各管各的目录；都不写入共享音频目录。

## Review Focus

未在任务测试中穷尽、但最可能让用户踩坑的输入/情形（每条都要落到拥有该代码的任务里的一个测试）：

1. **歌名/歌手/专辑含 emoji、换行或超长** → 卡片不崩、标题按宽度省略、输出文件名不含关键词与平台非法字符（Task 3、Task 5）。
2. **封面是 webp / 带 alpha 的 PNG / CMYK** → 贴进卡片不报错（统一 `convert("RGBA")`）、失败即占位（Task 2、Task 3）。
3. **搜索成功但返回 0 首** → 不渲染空卡片，走「未找到」文本（Task 3、Task 5）。
4. **`event.image_result` 抛错或不可用（老版本 AstrBot）** → 改发纯文本列表，不重复发图（Task 5）。
5. **搜索结果里同一首歌重复出现** → 行号仍按出现顺序正确（Task 3、Task 5）。

---

### Task 1: 归一化补充 `album` 与 `cover`

**Files:**
- Modify: `kugou_client.py:757-783`（`normalize_songs`）
- Test: `tests/test_kugou_client.py`

**Interfaces:**
- Consumes: 现有 `_first(mapping, *keys)`。
- Produces: `normalize_songs(payload) -> list[dict]` 返回项新增两个字段 —— `"album": str`（缺失为 `""`）、`"cover": str`（可直接请求的 `https` URL，缺失为 `""`）。新增模块常量 `COVER_SIZE = 240` 与内部函数 `_cover_url(item: dict) -> str`。

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_kugou_client.py`）

```python
def test_normalize_adds_album_and_cover_url():
    payload = {"status": 1, "data": {"info": [{
        "hash": "H1", "songname": "晴天", "singername": "周杰伦", "duration": 269,
        "AlbumName": "叶惠美",
        "trans_param": {"union_cover": "http://imge.kugou.com/stdmusic/{size}/x.jpg"},
    }]}}
    client, _ = _client([json_response(payload)])
    song = asyncio.run(client.search("x", 1))[0]
    assert song["album"] == "叶惠美"
    assert song["cover"] == "https://imge.kugou.com/stdmusic/240/x.jpg"


def test_normalize_reads_trans_param_from_json_string():
    import json as _json
    payload = {"status": 1, "data": {"info": [{
        "hash": "H1", "songname": "a", "singername": "b",
        "trans_param": _json.dumps(
            {"union_cover": "http://imge.kugou.com/stdmusic/{size}/y.jpg"}),
    }]}}
    client, _ = _client([json_response(payload)])
    song = asyncio.run(client.search("x", 1))[0]
    assert song["cover"] == "https://imge.kugou.com/stdmusic/240/y.jpg"


def test_normalize_missing_album_and_cover_are_empty():
    payload = {"status": 1, "data": {"info": [{"FileHash": "AA", "SongName": "x"}]}}
    client, _ = _client([json_response(payload)])
    song = asyncio.run(client.search("x", 1))[0]
    assert song["album"] == "" and song["cover"] == ""
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_kugou_client.py -k "album and cover or trans_param_from_json or missing_album" -v`
Expected: FAIL（`KeyError: 'album'`）

- [ ] **Step 3: 实现**

在 `kugou_client.py` 常量区加 `COVER_SIZE = 240`。在 `normalize_songs` 的字段字典里加 `"album"` 与 `"cover"` 两项，取值分别来自 `_first(item, "AlbumName", "album_name", "AlbumSingleName")` 与新的 `_cover_url(item)`：

```python
def _cover_url(item: dict) -> str:
    """从 trans_param 取封面模板，替换尺寸并升级为 https。取不到返回 ""。"""
    trans = item.get("trans_param")
    if isinstance(trans, str):
        try:
            trans = json.loads(trans)
        except (TypeError, ValueError):
            trans = None
    template = ""
    if isinstance(trans, dict):
        template = str(trans.get("union_cover") or "")
    if not template:
        template = str(_first(item, "Image", "AlbumImage") or "")
    if "{size}" in template:
        template = template.replace("{size}", str(COVER_SIZE))
    if template.startswith("http://"):
        template = "https://" + template[len("http://"):]
    return template if template.startswith("https://") else ""
```

（`json` 已在模块顶部导入。）

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_kugou_client.py -v`
Expected: PASS（含既有测试）

- [ ] **Step 5: 提交**

```bash
git add kugou_client.py tests/test_kugou_client.py
git commit -m "feat(kugou): normalize album and cover url from search results"
```

---

### Task 2: 封面安全抓取与落盘缓存

**Files:**
- Create: `cover_cache.py`
- Test: `tests/test_cover_cache.py`

**Interfaces:**
- Consumes: `file_cache.is_safe_url(url, resolver=None, allowed_suffixes=...)`、`file_cache.UnsafeUrlError`、`file_cache.default_resolver`。
- Produces:
  - `class CoverLimits`（frozen dataclass）：`max_bytes=8*1024*1024`、`timeout=5.0`、`batch_timeout=8.0`、`concurrency=3`、`max_redirects=5`、`max_pixels=40_000_000`。
  - `class CoverCache(session, cache_dir, *, resolver=None, clock=None, limits=None, allowed_suffixes=(".kugou.com",))`
    - `def path_for(self, url: str) -> str` — 由 URL 派生的确定性本地路径（`sha1(url)+".img"`）。
    - `async def ensure(self, url: str) -> str | None` — 命中缓存直接返回本地路径（不发请求）；否则抓取、校验、落盘后返回路径；任何失败返回 `None`。
    - `async def ensure_many(self, urls: list[str]) -> dict[str, str]` — 受 `concurrency` 与 `batch_timeout` 约束，只返回成功项（键为原始 URL）。抓取结果只用于渲染，不因个别失败抛错。

- [ ] **Step 1: 写失败测试**（`tests/test_cover_cache.py`）

用 `tests/fake_session.py` 的 `FakeSession` 构造响应。至少覆盖：

```python
def test_second_call_hits_disk_without_request(tmp_path)
    # ensure(url) 两次；断言 FakeSession.calls 长度为 1，两次返回同一路径且文件存在

def test_rejects_host_outside_allowlist(tmp_path, monkeypatch)
    # monkeypatch resolver 返回 ["93.184.216.34"]；url 用 http://evil.example/x.jpg
    # 断言 ensure 返回 None

def test_rejects_redirect_to_private_host(tmp_path)
    # 首个响应 302 -> http://127.0.0.1/x；resolver 对 127.0.0.1 返回 ["127.0.0.1"]
    # 断言 ensure 返回 None 且没有对 127.0.0.1 的第二个请求

def test_rejects_too_many_redirects(tmp_path)
    # 连续 302 自指；断言 ensure 返回 None

def test_oversize_and_non_200_and_bad_image_return_none(tmp_path)
    # Content-Length 超 max_bytes / status=500 / body 不是图片 → 均 None

def test_pixel_bomb_rejected(tmp_path)
    # 构造声明巨大尺寸的 PNG 头 → None（或异常被吸收）
```

（`resolver` 通过构造参数注入，测试里给一个纯函数即可，不碰真实 DNS。）

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_cover_cache.py -v`
Expected: FAIL（`ModuleNotFoundError: cover_cache`）

- [ ] **Step 3: 实现 `cover_cache.py`**

要点（具体写法由实现者决定）：

- 缓存键：`hashlib.sha1(url.encode("utf-8")).hexdigest()`；路径 `os.path.join(cache_dir, key + ".img")`。
- `ensure`：先查本地文件（存在即返回，**不发请求**）；否则在**临时文件**里流式写入，成功后 `os.replace` 到最终路径（原子），已有同名文件时直接复用。
- 请求与 SSRF：完全照 `file_cache.Downloader._download_one` 的形状 —— `allow_redirects=False`、每跳 `is_safe_url(current, resolver=self._resolver, allowed_suffixes=self._allowed)`、`_REDIRECT_STATUS` 跳转计数、超跳数即失败；不携带任何 Cookie / token，只带 `{"User-Agent": ...}`。
- 校验：`status == 200`；`Content-Length`（若有）不超 `max_bytes`；实际写入字节数不超 `max_bytes`；用 Pillow `Image.open(BytesIO(...))` 打开并检查 `width*height <= max_pixels`，失败即丢弃临时文件并返回 `None`。
- 所有网络/磁盘/解码异常都收敛为 `None`；单个 URL 失败不影响其他 URL。
- `ensure_many`：`asyncio.Semaphore(limits.concurrency)` + `asyncio.wait_for(..., batch_timeout)`，返回 `{url: path}`。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_cover_cache.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add cover_cache.py tests/test_cover_cache.py
git commit -m "feat(kugou): add ssrf-safe cover cache with redirect re-validation"
```

---

### Task 3: 卡片渲染

**Files:**
- Create: `search_card.py`
- Test: `tests/test_search_card.py`

**Interfaces:**
- Consumes: 无（只用 Pillow + 标准库）。
- Produces:
  - `class CardRenderError(Exception)`。
  - `def cjk_font_available(extra_candidates: tuple[str, ...] = ()) -> bool` — 按候选顺序探测，只要有一个**含中文字形**即返回 True。
  - `async def render_search_card(songs, keyword, output_dir, *, covers=None, command="/酷狗下载", width=800, extra_font_candidates=()) -> str` — 返回保存后的 PNG 路径；不可渲染时抛 `CardRenderError`。`songs` 为 `normalize_songs` 风格的 dict 列表；`covers` 为 `{cover_url: 本地路径}`。

- [ ] **Step 1: 写失败测试**（`tests/test_search_card.py`）

```python
def test_renders_one_row_per_song_and_returns_png(tmp_path)
    # 3 首；断言返回路径存在、可被 PIL 打开、尺寸宽 == 800

def test_empty_songs_raises(tmp_path)
    # pytest.raises(CardRenderError)

def test_missing_artist_album_duration_does_not_crash(tmp_path)
    # 只有 songname 的条目；断言正常产出

def test_long_title_and_emoji_are_tolerated(tmp_path)
    # songname 为 "🎵"*3 + "很长的名字"*40；断言产出且不抛

def test_cover_missing_or_broken_falls_back_to_placeholder(tmp_path)
    # covers={} 与 covers={url: "不存在的文件"} 都产出

def test_no_cjk_font_raises(tmp_path, monkeypatch)
    # monkeypatch 字体候选为空且 extra 为空 → CardRenderError
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_search_card.py -v`
Expected: FAIL（`ModuleNotFoundError: search_card`）

- [ ] **Step 3: 实现 `search_card.py`**

要点：

- 顶部 `from PIL import Image, ImageDraw, ImageFont` 用 `try/except ImportError` 包住并把名字置 `None`；绘制入口若 `Image is None` 抛 `CardRenderError("缺少 Pillow 依赖")`。
- 字体发现：候选路径顺序为 Windows `C:/Windows/Fonts/msyh.ttc`（粗体 `msyhbd.ttc`）→ macOS `/System/Library/Fonts/PingFang.ttc` → Linux `/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc` 等；再把 `extra_font_candidates` 追加进去。每个候选 `ImageFont.truetype(...)` 成功后做**字形覆盖探测**：分别渲染 `"永"` 与 `"\ue000"`（私用区，必然缺字），若两次 `getbbox`/位图一致则判为无中文字形，换下一个。全部失败 → `CardRenderError`。
- 高度预算：先算页眉 + `行数 × 行高` + 页脚，再开画布，杜绝溢出。
- 每行：序号徽章 → 封面（`covers` 命中则打开、`convert("RGBA")`、等比裁剪成 56×56、加 8 px 圆角；否则画同风格占位）→ 歌名（最多 2 行、超宽省略）→ 副行「歌手 · 专辑」（缺项只显示存在的一项）→ 右侧时长 `M:SS`。
- 文本一律走一个 `safe_text()`（去控制字符、截断）与 `ellipsize()`；文件名用 `time.strftime` + 4 字节随机后缀，不含关键词与 `/`。
- `Image.open` 的结果必须 `load()` 后关闭。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_search_card.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add search_card.py tests/test_search_card.py
git commit -m "feat(kugou): render local pillow search-result card"
```

---

### Task 4: 缓存 TTL 配置与清理

**Files:**
- Modify: `_conf_schema.json`
- Modify: `file_cache.py`（新增 `prune_expired_entries`）
- Modify: `main.py:103-104, 393-410`（`CACHE_TTL_SECONDS` 的使用点、`_cleanup_cache`）
- Test: `tests/test_main.py`、`tests/test_file_cache.py`

**Interfaces:**
- Consumes: `CacheManager.cleanup(ttl_seconds)`（已存在）。
- Produces:
  - `file_cache.prune_expired_entries(base_dir: str, ttl_seconds: float, *, clock=None) -> int` — 只扫一级、跳过符号链接与 reparse point、删除 `mtime` 超过 TTL 的**文件或目录**，返回删除数。
  - `KugouMusic._cache_ttl_seconds(self) -> int` — 读 `cache_ttl_hours`，非整数或 < 1 回落 `24`，返回**秒**。
  - `KugouMusic._covers_dir(self) -> str`、`KugouMusic._search_cards_dir(self) -> str` — 插件数据目录下的 `covers` / `search_cards`，按需创建。

- [ ] **Step 1: 写失败测试**

`tests/test_main.py`：

```python
def test_cache_ttl_defaults_to_24h_and_rejects_bad_values(plugin_factory):
    assert plugin_factory({})._cache_ttl_seconds() == 24 * 3600
    assert plugin_factory({"cache_ttl_hours": 2})._cache_ttl_seconds() == 2 * 3600
    assert plugin_factory({"cache_ttl_hours": 0})._cache_ttl_seconds() == 24 * 3600
    assert plugin_factory({"cache_ttl_hours": "x"})._cache_ttl_seconds() == 24 * 3600


def test_cleanup_prunes_covers_and_cards_but_keeps_search_cache(...)
    # 在 _covers_dir()/_search_cards_dir() 各放一个老文件（mtime 回拨 25h）
    # 调用 plugin._cleanup_cache()（先把 _last_cleanup 置 0）
    # 断言两个老文件被删；_search_cache 仍存在且 SEARCH_TTL_SECONDS 未变
```

`tests/test_file_cache.py`：`prune_expired_entries` 删除老文件、保留新文件、跳过符号链接。

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_main.py -k "cache_ttl or cleanup_prunes" tests/test_file_cache.py -v`
Expected: FAIL（`AttributeError: _cache_ttl_seconds`）

- [ ] **Step 3: 实现**

- `_conf_schema.json` 增加：`"cache_ttl_hours": {"description": "缓存保留时长（小时）", "type": "int", "default": 24, "hint": "统一控制音频文件、封面图片与搜歌卡片图的保留时间。搜歌候选列表固定 5 分钟，不受此项影响。", "slider": {"min": 1, "max": 720, "step": 1}}`。
- `file_cache.py` 新增 `prune_expired_entries`（实现规则同 `CacheManager.cleanup` 的扫描部分，但每个一级项都可以是文件或目录）。
- `main.py`：`_cleanup_cache` 里把 `CACHE_TTL_SECONDS` 换成 `self._cache_ttl_seconds()`，并追加对 `self._covers_dir()` 与 `self._search_cards_dir()` 的 `prune_expired_entries(...)`；保留既有的限频与异常兜底。`CACHE_TTL_SECONDS` 常量删除或改为默认值来源，不留第二处 TTL 真值。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_main.py tests/test_file_cache.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add _conf_schema.json file_cache.py main.py tests/test_main.py tests/test_file_cache.py
git commit -m "feat(kugou): configurable cache ttl covering audio, covers and cards"
```

---

### Task 5: 接入 `_search_flow`（出图 + 回退）

**Files:**
- Modify: `tests/conftest.py`（给 `FakeEvent` 加 `image_result`）
- Modify: `main.py:1-8, 799-838`（导入、`_search_flow`）
- Modify: `tests/test_main.py`（改写 `test_search_lists_results_and_caches_per_user`，新增回退用例）

（`tests/test_integration.py` 只覆盖下载阶梯、不涉及搜歌输出，无需改动。）

**Interfaces:**
- Consumes: Task 1 的 `song["cover"]`；Task 2 的 `CoverCache.ensure_many`；Task 3 的 `render_search_card` / `CardRenderError`；Task 4 的 `_cache_ttl_seconds` / `_covers_dir` / `_search_cards_dir`。
- Produces: `KugouMusic._render_search_card(self, songs, keyword) -> str | None`（成功返回卡片路径，失败返回 `None`）。`_search_flow` 行为如上。

- [ ] **Step 1: 写失败测试**

`tests/conftest.py` 的 `FakeEvent` 增加：

```python
    def image_result(self, path):
        self.results.append(("image", path))
        return ("image", path)
```

`tests/test_main.py`：

```python
def test_search_sends_image_card(plugin_factory, monkeypatch)
    # monkeypatch _search_songs 返回带 cover 的 _SONG
    # monkeypatch 插件内部的封面抓取与渲染为可用替身，返回 tmp 下真实 PNG
    # 断言 event.results 里出现 ("image", <path>)，且没有纯文本列表

def test_search_falls_back_to_text_when_render_fails(plugin_factory, monkeypatch)
    # 让渲染抛 CardRenderError → 断言 text 含 "酷狗下载" 与歌名

def test_search_falls_back_to_text_when_image_result_raises(plugin_factory, monkeypatch)
    # FakeEvent.image_result 抛 RuntimeError → 断言仍产出纯文本列表，且只发一次文本

def test_search_no_results_still_text(plugin_factory, monkeypatch)
    # 空结果 → "未找到"
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_main.py -k "image_card or falls_back or no_results_still_text" -v`
Expected: FAIL（无 `image_result` 或断言不成立）

- [ ] **Step 3: 实现 `main.py`**

- 顶部导入 `CoverCache`、`render_search_card`、`CardRenderError`（沿用既有的「包路径 / 顶层回退」双导入写法）。
- 新增 `_card_renderer_deps()` 或直接在 `_search_flow` 内：用 `await self._get_session()` 建 `CoverCache(session, self._covers_dir(), ttl_seconds=self._cache_ttl_seconds())`（可缓存到 `self._cover_cache`）。
- 把现有「拼 `lines` 列表」的收尾替换为先试出图：

```python
        songs = [...]
        # 先缓存搜索结果（既有逻辑保留）
        ...
        card_path = await self._render_search_card(songs, keyword)
        if card_path is not None:
            try:
                yield event.image_result(card_path)
            except Exception as exc:
                logger.warning("[kugou] 发送搜歌卡片失败：%s", type(exc).__name__)
            else:
                if self.config.get("debug", False):
                    yield event.plain_result(self._flush_debug().lstrip("\n"))
                return
        yield event.plain_result("\n".join(lines) + self._flush_debug())
```

  其中 `_render_search_card` 内部：`urls = [s["cover"] for s in songs if s.get("cover")]` → `covers = await cache.ensure_many(urls)` → 校验返回路径可读 → `await render_search_card(songs, keyword, self._search_cards_dir(), covers=covers, ...)`；捕获 `CardRenderError` 与任何异常，返回 `None`。
- 文本回退函数（把现在拼 `lines` 的代码抽成 `_search_text_result(songs, keyword) -> str`）保证两条路径文案一致。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/ -v`
Expected: PASS（全量）

- [ ] **Step 5: 提交**

```bash
git add main.py tests/conftest.py tests/test_main.py tests/test_integration.py
git commit -m "feat(kugou): send search results as an image card with text fallback"
```

---

### Task 6: 文档、元数据与版本同步

**Files:**
- Modify: `requirements.txt`、`metadata.yaml`、`main.py:7`（模块头注释）、`README.md`

**Interfaces:**
- Consumes: 前五个任务的全部产物。
- Produces: 无代码接口；对外描述与版本号更新。

- [ ] **Step 1: 更新 `requirements.txt`**

追加一行 `Pillow>=9.5.0`。

- [ ] **Step 2: 更新 `metadata.yaml`**

`version: 2.0.0` → `2.1.0`；`desc` 中「只发送音频文件附件：不发送音乐卡片、语音、封面图片或播放直链」改为「搜歌返回带封面的图片卡片；下载只发送音频文件附件，不发送音乐卡片、语音或播放直链」；「搜歌取文件」一条补上「搜歌结果以图片卡片呈现，含封面/歌手/专辑」。

- [ ] **Step 3: 更新 `main.py` 模块头注释与 `README.md`**

`main.py` 第 7 行同义句改为反映新契约。`README.md` 更新搜歌输出说明，并新增 `cache_ttl_hours` 的配置说明（默认 24 小时，覆盖音频/封面/卡片；搜歌候选列表仍 5 分钟）。

- [ ] **Step 4: 全量验证**

Run: `python -m pytest tests/ -v` 与 `python -m py_compile main.py kugou_client.py file_cache.py search_card.py cover_cache.py`
Expected: 全绿、无语法错误

- [ ] **Step 5: 提交**

```bash
git add requirements.txt metadata.yaml main.py README.md
git commit -m "docs(kugou): document image-card search output and cache ttl; bump to 2.1.0"
```

---

## 收尾

完成后跑一遍设计文档 §11 的验证清单，并把「已实测」与「仍待真实环境验证」分开写清楚（真实的一次群内发送效果属于后者）。
