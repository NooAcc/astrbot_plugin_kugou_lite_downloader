# Changelog

本文件是插件的更新日志。AstrBot WebUI 的「查看更新日志」会读取它，并在插件更新后自动
展示，所以版本号与 `metadata.yaml` 的 `version` 保持一致，新版本记在最前面。

格式与 AstrBot 自身的 `changelogs/` 一致：标题为 `## [版本] - 日期`，小节用
`### 新增` / `### 优化与修复`，每节版本末尾附 `**Full Changelog**` 对比链接。

本仓库的 `1.7.5` 是上游
[xiaoyunsha/astrbot_plugin_kugou_music](https://github.com/xiaoyunsha/astrbot_plugin_kugou_music)
的代码快照，只作为重写的起点，不属于本仓库的变更记录，因此本文件从 `2.0.0` 开始。

## [2.6.0] - 2026-11-14

### 新增

- **下载时写入媒体信息与歌词**（`embed_tags`，默认开启）：下载到的歌曲文件会带上
  标题、歌手、专辑、专辑艺术家、作词、作曲、发行日期、曲风与 LRC 歌词，任何播放器
  打开即可看到，不再需要旁挂 `.lrc`。MP3 写 ID3v2（`TIT2`/`TPE1`/`TALB`/`TCOM`/`TEXT`/
  `TDRC`/`TCON`/`USLT`…），FLAC 与 OGG 写 Vorbis comment。元信息取自酷狗官方的
  `/kmr/v2/audio` 与歌词接口，**不依赖任何第三方解析服务**。
- `embed_cover`（默认开启）：把封面嵌入标签（APIC / picture block），每首约增加
  几十~几百 KB。封面取不到或字节数超限时自动跳过，其余标签照写。
- 取歌词时只取明文 LRC。酷狗私有的 KRC 逐字歌词是加密格式，且 `USLT`/`LYRICS` 这类
  纯文本标签字段没有播放器能渲染它，因此不取。

### 优化与修复

- **音频仍然不转码**：写入标签是在文件里原地改写元数据块，音频帧一个字节都不动，
  音质与原文件完全一致。README 此前把「不转码」和「不改写标签」写成一句，现在两者
  分开表述。
- 标签写入发生在文件入库缓存**之前**，因此缓存里不会存在未打标的条目，缓存命中
  不需要重写文件（无损文件的全量重写很贵）。写入后会**复探一次**确认没把自己写坏，
  复探不通过则删掉标签回退，绝不让一个探测不出来的文件入库。
- 缓存键按开关条件化（`tagv1`）：写入标签后缓存内容与关闭时不同，两者不会互相命中。
  关闭 `embed_tags` 时缓存键与 2.5.0 逐字节一致，升级不会白丢缓存。切换开关会整体
  失效一次缓存并重新下载，这是预期行为。
- 打标签需要写文件（同步 I/O，且会全量重写），已放到线程里执行，不阻塞事件循环、
  不影响另一个并发下载与进行中的上传。

> **升级提示**：新增运行时依赖 `mutagen`（GPL-2.0-or-later，不随插件分发）。
> AstrBot 会在安装/更新时按 `requirements.txt` 自动处理；离线环境需自行预装。
> 不需要标签功能的话把 `embed_tags` 关掉即可，关闭后行为与 2.5.0 完全一致，
> 也不会 import mutagen。

**Full Changelog**: https://github.com/NooAcc/astrbot_plugin_kugou_lite_downloader/compare/294f659...fa6c8d5

## [2.5.0] - 2026-11-14

### 优化与修复

- 插件更名为 `astrbot_plugin_kugou_lite_downloader`，与原版
  `astrbot_plugin_kugou_music` 区分开：仓库地址、`metadata.yaml` 的 `name` 与
  `repo`、`main.py` 的 `PLUGIN_DIRNAME`、以及共享缓存子目录名同步变更。

> **升级提示**：运行时目录随插件名一起变了。凭据目录由
> `data/plugin_data/astrbot_plugin_kugou_music/` 变为
> `data/plugin_data/astrbot_plugin_kugou_lite_downloader/`，`<shared_cache_dir>/`
> 下的子目录同理。升级后需要重新执行 `/酷狗登录`；旧目录里的音频缓存不再被使用，
> 可以自行删除。

**Full Changelog**: https://github.com/NooAcc/astrbot_plugin_kugou_lite_downloader/compare/d53695b...294f659

## [2.4.0] - 2026-11-14

### 优化与修复

- 字体不再随插件分发：移除仓库内 `resources/` 下的两份 HarmonyOS Sans SC（共
  16 MB），改为首次使用时从固定镜像取回，字节仍按钉死的 `sha256` / 字节数校验后
  才落盘。一份都取不到时退回 AstrBot 默认字体（数据目录的 `font.ttf` /
  `font-bold.ttf`，其次系统字体），搜歌与下载不受影响。
- 上传成功后不再往聊天里多发一条确认消息——文件本身就是结果，实际音质也已经写在
  文件名里（`歌手 - 歌名 [实际音质].扩展名`）。失败原因仍然照常发给用户，那是唯一
  能据以行动的信息。
- `logo.png` 修正为真正的 PNG：此前文件内容是 JPEG 数据（256×256），却配了 `.png`
  扩展名。
- 发布到插件市场：`metadata.yaml` 的 `author` 改为 `NooAcc`，并移除指向上游的
  `social_link`。

**Full Changelog**: https://github.com/NooAcc/astrbot_plugin_kugou_lite_downloader/compare/e911dd8...d53695b

## [2.3.2] - 2026-11-14

### 优化与修复

- OneBot 文件上传的结果判定改为按 aiocqhttp 的拆包契约：`call_action` 只把信封里的
  `data` 返回给调用方、失败时直接抛 `ActionFailed`，所以能带着返回值正常返回即说明
  OneBot 端已受理本次上传，不再把「缺少 `status` / `retcode` 字段」误判为失败。
  少数返回完整信封的实现仍按 `status` / `retcode` 判定。上传被拒绝时会把 OneBot
  返回的 `wording` / `msg` 原因带给用户。

**Full Changelog**: https://github.com/NooAcc/astrbot_plugin_kugou_lite_downloader/compare/8f00101...e911dd8

## [2.3.1] - 2026-11-14

### 优化与修复

- `/搜歌` 的关键词改为**必填**，序号只能作为关键词的后缀：`/搜歌 3` 是搜歌名为
  「3」的歌，不再是「下载上一次搜索的第 3 首」。
- 带序号时优先复用**同一关键词**最近一次搜索的候选（5 分钟内），保证按卡片编号下到
  的就是卡片上那一首；换了关键词则重新搜索。
- 移除 `/搜歌 <序号>` 这个「按上一次搜索结果下载」的独立入口。

**Full Changelog**: https://github.com/NooAcc/astrbot_plugin_kugou_lite_downloader/compare/4733f25...8f00101

## [2.3.0] - 2026-11-14

### 优化与修复

- 指令简化为两个入口，日常输入更短：
  - `/酷狗搜歌 <关键词>` → `/搜歌 <关键词>`
  - `/酷狗下载 <序号>`（含兼容入口 `/酷狗点歌`）→ `/搜歌 <关键词> <序号>`
  - `/酷狗扫码登录` → `/酷狗登录`

**Full Changelog**: https://github.com/NooAcc/astrbot_plugin_kugou_lite_downloader/compare/3bee0b4...4733f25

## [2.2.0] - 2026-11-13

### 新增

- `font_store.py`：卡片中文字体的取回与校验——从固定镜像下载后，必须通过「字节数
  + sha256 + 可加载 + 含中文字形」四关才落盘；同一进程内只取一次，插件启动时后台
  预热。
- 配置项 `font_auto_download`（默认开启）：内网 / 离线环境可以关掉，此时卡片退回
  AstrBot 默认字体。
- `LICENSES/`：字体等第三方资源的许可说明。

### 优化与修复

- 搜歌卡片版式重排：字号阶梯成为版式的唯一真源，宽度、行高、封面边长、内边距全部由
  字号推导（歌名 15 → 22 px，封面边长 56 → 100 px），卡片由 800×517 变为 596×754。
  过长的歌名折行、过长的「歌手 · 专辑」截断加省略号，不再撑宽卡片。
- 卡片字体改为随插件分发的 HarmonyOS Sans SC，版式按它的字宽标定，不再混用系统字体
  与微软雅黑。

**Full Changelog**: https://github.com/NooAcc/astrbot_plugin_kugou_lite_downloader/compare/8c53b7c...3bee0b4

## [2.1.0] - 2026-11-13

### 新增

- 搜歌结果由纯文本改为本地渲染的图片卡片，每行显示序号、封面、歌名、歌手、专辑与
  时长，便于凭封面确认选中的歌（`search_card.py`，Pillow 本地渲染，不联网、不导入
  astrbot）。
- `cover_cache.py`：封面的 SSRF 安全抓取与落盘缓存——重定向逐跳重新校验，大小、
  像素、超时、并发均有上限，失败时用同风格占位图。
- 配置项 `cache_ttl_hours`（默认 24 小时）：统一控制音频、封面与卡片三类缓存的保留
  时长；搜歌候选列表仍固定 5 分钟。
- 依赖新增 `Pillow>=9.5.0`。

### 优化与修复

- 搜歌出图失败、或 OneBot 无法接收图片时，回退为原有的纯文本列表。
- 修掉搜歌卡片评审遗留的 5 处问题：`search_card._wrap` 在 `max_lines <= 0` 时不再抛
  `IndexError`；同一封面 URL 只抓一次、`.part` 临时名逐次唯一；渲染用例不再因为缺
  中文字体而整体 skip（无字体时现场生成最小中文字体）；清理死代码；字体候选顺序与
  设计文档对齐。

**Full Changelog**: https://github.com/NooAcc/astrbot_plugin_kugou_lite_downloader/compare/0395ecf...8c53b7c

## [2.0.0] - 2026-11-13

### 新增

- 按音质优先级下载：`quality_priority` 默认 Hi-Res → FLAC → 高品 320 → 标准 128，
  逐档位尝试，某档位无资源、无权限或格式不可用时自动降级；下载后按音频头**实测**
  规格判定档位，拒绝错误页、加密数据与试听片段。
- 共享缓存目录：`shared_cache_dir` 需 AstrBot 与 OneBot 实现端同一路径可读。目录内
  实现安全文件名、URL 白名单、受限流式下载、原子落盘与有界清理，并以
  `manifest.json` 标记归属。
- 扫码登录：`/酷狗扫码登录` 直接把二维码图片发到聊天里，100 秒未登录自动结束任务并
  撤回二维码（登录成功、过期或失败时同样撤回）。
- 模块拆分：`kugou_client` / `kugou_login` / `file_cache` / `audio_probe` /
  `quality`，并补充全程离线的测试（mock HTTP 会话与 OneBot API）。
- 指令 `/酷狗下载 <序号>`，保留 `/酷狗点歌` 作为兼容入口。

### 优化与修复

- 定位改变：从「音乐卡片分享」改为「搜歌 → 按音质优先级下载原始音频 → 通过 OneBot
  群 / 私聊文件接口发送**文件附件**」，不再发送音乐卡片、语音或播放直链。
- 仅支持酷狗**概念版（Lite）**，平台声明收敛为 `aiocqhttp`（OneBot v11）。
- 搜索结果缓存按「会话 + 发起用户」隔离，避免同群其他人的搜索覆盖自己的选择。
- 移除音乐卡片及其所有实现路径（OIAPI 签名、咸鱼 API、NapCat 原生 `music` 段），
  以及播放直链与语音。
- 移除 VIP 直链（`trackercdn.kugou.com/v5/url`）、微信登录，以及只为它引入的
  `pycryptodome` 依赖。
- 移除酷狗标准版（appid=1005）支持与双协议切换开关、`qq_official` 平台支持。
- 移除旧配置项 `kugou_concept`、`card_jump`、`music_card_key`、`card_format`，以及
  内置的 `127.0.0.1` 静态图片服务。
- 修复 AstrBot 以 `data.plugins.<目录名>.main` 包路径加载插件时的跨模块导入失败。

**Full Changelog**: https://github.com/NooAcc/astrbot_plugin_kugou_lite_downloader/compare/3a06e65...0395ecf
