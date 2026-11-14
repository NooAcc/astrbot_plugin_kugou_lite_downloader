# astrbot_plugin_kugou_lite_downloader

酷狗**概念版（Lite）**歌曲文件发送插件：搜歌返回带封面的图片卡片 → 按音质优先级获取原始音频 → 通过 OneBot 群/私聊文件接口发送**文件附件**。

不依赖第三方解析或签名服务。搜歌结果以本地渲染的图片卡片呈现（含封面、歌手、专辑）；**下载**只发送音频文件附件，不发送音乐卡片、语音或播放直链。

## 特性

- **搜歌取文件**：`/搜歌 <关键词>` 返回一张带封面的图片卡片（序号 / 封面 / 歌名 / 歌手 · 专辑 / 时长），再按卡片编号 `/搜歌 <关键词> <序号>` 获取歌曲文件——**关键词必填**，序号只是后缀，没有单独的「按序号下载」入口
  - 卡片用 HarmonyOS Sans SC 渲染，跨平台观感一致；字体**不随插件分发**（16 MB 会拖累插件更新），首次使用时从固定镜像取回（见 `font_auto_download`）
- **音质优先级**：WebUI 配置 `quality_priority`，默认 `Hi-Res → FLAC → 高品 320 → 标准 128`
  - 逐档位尝试；某档位无资源、无权限或格式不可用时自动降级
  - 下载后按音频头实测规格判定档位，达标才发送；实测档位高于请求档位也算达标
  - 只处理配置里列出的档位，不会偷偷请求你没选的档位
- **不转码**：音频一帧不改，音质与原文件完全一致；扩展名以实际音频格式为准
- **自带媒体信息与歌词**：下载后写入标题、歌手、专辑、作词/作曲、发行日期、曲风与歌词标签
  （MP3 走 ID3v2，FLAC/OGG 走 Vorbis comment），任何播放器打开即可看到，不需要旁挂 `.lrc`。
  由 `embed_tags` 控制，关闭后行为与 2.5.0 一致；封面单独由 `embed_cover` 控制
  （约增加几十~几百 KB）。歌词与封面取不到时只少这两项，不影响文件发送
- **文件名**：`歌手 - 歌名 [实际音质].扩展名`，例如 `周杰伦 - 晴天 [FLAC].flac`
- **登录**：`/酷狗登录`，或 WebUI 手动填写 Cookie（概念版账号）

## 安装

本项目基于 [xiaoyunsha/astrbot_plugin_kugou_music](https://github.com/xiaoyunsha/astrbot_plugin_kugou_music) 重构。

在 AstrBot WebUI 中通过仓库地址 `https://github.com/NooAcc/astrbot_plugin_kugou_lite_downloader` 安装，或下载本仓库 zip 后手动上传。

运行期依赖（`requirements.txt`，安装时自动处理）：

- `aiohttp>=3.8.0`
- `Pillow>=9.5.0`
- `mutagen>=1.47`（写入音频标签；GPL-2.0-or-later，见 `LICENSES/`）

## 前置条件

1. **平台**：仅支持 `aiocqhttp`（OneBot v11）。
2. **OneBot 实现**：必须支持文件上传扩展接口
   - 群聊：`upload_group_file(group_id, file, name)`
   - 私聊：`upload_private_file(user_id, file, name)`

   并非所有 OneBot v11 实现都提供这两个 API。不支持时插件会明确报错，不会退化为卡片或链接。
3. **共享缓存目录**：在 WebUI 填写 `shared_cache_dir` 绝对路径，且 **AstrBot 与 OneBot 实现端必须能以同一路径访问**（同一台机器，或 Docker 挂载到相同路径）。插件只使用该目录下自己的子目录 `astrbot_plugin_kugou_lite_downloader/`。
4. **登录**：管理员执行 `/酷狗登录`（也可在 WebUI 填写概念版 Cookie）。插件共享同一个酷狗账号。登录二维码会直接作为图片发到聊天里，消息发出 **100 秒**内未登录会自动结束任务并撤回二维码；登录成功、过期或失败时同样立即撤回。撤回依赖 OneBot `delete_msg`，若协议端不支持，二维码会留在聊天里，但不影响登录。

## 指令

| 指令 | 说明 |
| --- | --- |
| `/搜歌 <关键词>` | 搜索歌曲，返回带封面的图片卡片；候选列表缓存 5 分钟（按会话 + 发起用户隔离） |
| `/搜歌 <关键词> <序号>` | 下载第 N 首，不额外发卡片；刚搜过同一关键词（5 分钟内）就直接下，否则先搜再下 |
| `/酷狗登录` | 直接发送登录二维码图片（概念版安卓渠道，管理员）；100 秒内未登录自动结束并撤回 |
| `/酷狗退出登录` | 清除插件保存的凭据（不影响 WebUI 里手动填写的 Cookie，管理员） |
| `/酷狗登录状态` | 查看 userid、脱敏 token 与凭据来源（管理员） |

> 序号解析规则：**末段是纯数字、且前面还有关键词时才算序号**。关键词必填，所以 `/搜歌 3` 是搜歌名为「3」的歌，不是下载第 3 首；想搜带数字的歌名（例如「七里香 2004」）要写成 `/搜歌 七里香2004`，否则会被读成「关键词=七里香、序号=2004」而报序号越界。带序号时会优先复用**同一关键词**最近一次搜索的候选（5 分钟内），这样按卡片编号点歌下到的就是卡片上那一首；换了关键词则重新搜索。

## 配置项（WebUI）

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| `quality_priority` | 音质优先级（有序），可用值 `hires` / `flac` / `320` / `128` | `["hires","flac","320","128"]` |
| `shared_cache_dir` | 共享缓存目录绝对路径，需 AstrBot 与 OneBot 端同路径可读 | 空（未配置时无法下载） |
| `kugou_cookie` | 概念版登录 Cookie；可留空，用扫码登录自动保存 | 空 |
| `kugou_ua` | 自定义 User-Agent，留空使用概念版客户端 UA | 空 |
| `search_count` | 每次搜索返回条数（1-10） | `5` |
| `cache_ttl_hours` | 缓存保留时长（小时），统一控制音频文件、封面图片与搜歌卡片图 | `24` |
| `font_auto_download` | 卡片字体不再随插件分发：允许从固定镜像取回并按内容校验后落盘；内网/离线环境可关闭，此时卡片退回 AstrBot 默认字体 | `true` |
| `timeout` | 接口请求超时（秒） | `15` |
| `debug` | 在回复末尾附加 `_debug` JSON（凭据/签名/设备指纹/直链自动脱敏） | `false` |

## 音质档位与判定

| 配置值 | 显示 | 上游协议参数 | 权益接口 level | 说明 |
| --- | --- | --- | --- | --- |
| `hires` | Hi-Res | `quality=high` | 6 | 上游没有 `hires` 参数；`high` 即 Hi-Res 档 |
| `flac` | FLAC | `quality=flac` | 5 | 无损 |
| `320` | 320K | `quality=320` | 4 | 高品 MP3/Ogg |
| `128` | 128K | `quality=128` | 1/2 | 标准 MP3/Ogg（概念版对付费歌曲也可取） |

判定依据是**下载后的音频头**，不是 URL 后缀或 Content-Type：

- FLAC：解析 `STREAMINFO`；位深 > 16 或采样率 > 48 kHz 才标记 Hi-Res，普通 CD 规格（16 bit / 44.1 kHz）标记为 FLAC
- MP3：解析帧头码率，并用 Xing/Info 帧计数修正 VBR 的平均码率（≥ 256 kbps 记 320 档，≥ 96 kbps 记 128 档）
- Ogg Vorbis：解析标识头（identification header）的 nominal 码率，档位判定同上
- HTML/JSON 错误页、加密数据、不可识别容器一律拒绝，绝不命名为音频发送
- 实测时长明显短于搜索结果时长（超过 10% 偏差）时判定为**试听片段**，不当作完整歌曲发送

内部安全默认值（不暴露为配置项）：单次下载上限 200 MiB、下载超时 180 秒、并发下载 2 个、封面单张上限 8 MiB / 超时 5 秒 / 并发 3、搜歌候选列表 5 分钟、字体单份上限 12 MiB / 单份超时 60 秒 / 首次搜歌最多为取回字体等待 20 秒。音频、封面与卡片三类缓存的保留时长由 `cache_ttl_hours` 统一控制（默认 24 小时）。

## 卡片渲染与字体

卡片版式只有一个真源：`search_card.py` 顶部的**字号阶梯**。宽度、行高、封面边长、
内边距全部由字号推导——调大一档字号，卡片自己会跟着变宽变高，不会出现「大字挤在
旧卡片里」。

卡片宽度只由一个常量决定：`TITLE_CHARS`（文字列按几个歌名字符预留，默认 15）。
它是「紧凑」与「能放多少字」之间唯一的旋钮：调小卡片更窄，代价是过长的歌名折行、
过长的「歌手 · 专辑」被截断加省略号（不会撑宽卡片）；调大则那些字放得下，但短行
右侧的空白会跟着回来。行内不再重复展示平台名——整张卡片只有酷狗一个来源。

字体来源按优先级排列：

1. 从镜像取回、落在插件数据目录 `fonts/` 并校验通过的 HarmonyOS Sans SC（默认）
2. 数据目录下的 `data/font.ttf` / `font-bold.ttf`——AstrBot 自己的默认字体
3. 系统中文字体（msyh / PingFang / Noto CJK / 文泉驿…）

字体**不随插件分发**：两份共 16 MB，塞进仓库会把插件本身的更新包撑得又大又慢，
所以只在需要时从 `gitee.com` 上的
[harmonyos-fonts](https://gitee.com/IKKI2000/harmonyos-fonts) 镜像取回。取回的字节
必须通过**内容校验**（字节数 + sha256 + 可加载 + 含中文字形）才会落盘，校验不通过
一律丢弃。取回在进程内只尝试一次，不会每次搜歌都去撞网络；插件启动时会后台预热，
让首次搜歌通常不必等。首次装好后需要联网一次（约 16 MB），超时上限见代码里的
`FONT_WAIT_SECONDS`，超时或取不回来就先用 AstrBot 默认字体渲染，不影响搜歌。

版式按 HarmonyOS Sans SC 的字宽标定，退到别的中文字体会跑版（行宽、折行位置都会
变）——这是取不回字体时的既定代价，而不是故障。授权与校验值见 [LICENSES](LICENSES/README.md)。

## 已知限制与待验证项

- **登录二维码来自酷狗官方接口**：`/v2/qrcode` 返回的 `qrcode_img` 是真正的二维码 PNG（内容为 H5 扫码地址），插件用 OneBot 原生 `send_group_msg` / `send_private_msg` 直接发出图片，以便拿到 `message_id` 用 `delete_msg` 自动撤回；正常情况下不再发送二维码链接，酷狗未返回图片时仍会退回链接。AstrBot 的标准 `yield event.chain_result()` 会丢弃 `message_id`，因此不用于此消息；没有可用 OneBot 原生 API 时会退回该方式显示图片，但此时无法自动撤回。
- **已在真实账号上验证核心链路**（2026-11-13，扫码登录后）：搜索 → 各档位权益 → 取址 → 受限下载 → 音频头实测 → 缓存复用 → 有界清理全部通过；`Hi-Res` 档实测返回 **24-bit FLAC**（44.1 kHz / 269.7 秒），被正确标记为 Hi-Res。**未验证**：OneBot 文件真正到达群/私聊（本地没有 AstrBot/OneBot 运行时）。
- **权限由服务端裁决**：权益接口返回的 `privilege` 是**歌曲的要求**（如「需要会员」），不是本账号的资格。因此插件不会据此在本地拦下档位，而是照常请求播放地址，由服务端决定给不给；被拒绝时如实报告并附上该档位的要求提示。未登录时，需要会员的档位会直接提示先扫码登录，不会发起请求。
- **设备注册 dfid 未实现**：酷狗文档提示取播放地址前可能需要 `/register/dev` 注册设备。本插件使用 Cookie 中的 `dfid/mid`，缺失时生成并持久化本机随机标识；实测（见上）未注册设备也能取得 Hi-Res。若服务端要求「本次请求需要验证」，会明确报错（可重新扫码登录）。
- **CDN 域名允许列表**：仅接受 `*.kugou.com`、`*.kglink.com`、`*.kgidc.cn`。若酷狗新 CDN 使用其他域名，下载会以「主机不在允许列表」明确报错，需要扩充 `file_cache.ALLOWED_HOST_SUFFIXES`。
- **NapCat / CQHTTP 等实现差异**：文件上传 API 的可用性与文件大小限制取决于具体实现，以真实端到端验证为准。
- **卡片字体不再随插件分发**（2026-11-14 起）：两份 HarmonyOS Sans SC 共 16 MB，留在仓库里会让插件本身的更新包大得容易拉不下来，所以改为首次使用时从 Gitee 镜像取回、按钉死的 `sha256`/`size` 校验后落盘。代价是**首次装好后要联网一次**；取不回来（离线、镜像挂了）就退回 AstrBot 默认字体，版式会跑但功能不受影响。镜像上那份字节与钉死值是否仍然一致，用 `CHECK_FONT_MIRROR=1 python -m pytest tests/test_font_store.py -q -k mirror` 核对。
- **OneBot 上传结果的判定契约**（2026-11-14 修正）：aiocqhttp 的 `call_action` 走 `_handle_api_result`，只把信封里的 `data` 返回给调用方，`status == "failed"` 会直接抛 `ActionFailed`。`upload_*_file` 在 OneBot v11 里没有响应数据，`data` 常见是 `null` / `{}`（部分实现会附 `file_id` 等字段），所以「拿到返回值且无异常」就等于 OneBot 端已受理。旧实现按完整信封判定，会把成功也报成「文件上传失败：OneBot 返回结果缺少 status/retcode 字段」；现在只有信封明确失败才报错，并把 OneBot 自己的原因（如 `retcode=1200 群文件空间已满`）显示给用户。打开 `debug` 可在输出里看到 `upload_result` 记录的响应字段名，便于核对实现差异。
- **上传成功的确认只进日志，不进聊天**（2026-11-14 起）：文件本身就是结果，音质也已写在文件名里（`歌手 - 歌名 [实际音质].扩展名`），所以成功时只记一行 `[kugou] 已发送文件「…」实际音质: …`，聊天里最多只有「正在获取…」的进度提示。**上传失败仍然发给用户**——那是用户唯一能据以行动的信息（群文件空间已满、OneBot 不支持该接口等）。`debug` 打开时，末尾的 `_debug` JSON 照旧单独发一条。

## 开发与测试

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests -q
```

测试全部离线运行（mock HTTP 会话与 OneBot API），不访问酷狗网络。未安装 `astrbot` 时，`tests/conftest.py` 会注入最小桩模块。`tests/test_package_import.py` 会模拟 AstrBot 的 `data.plugins.<目录名>.main` 包路径布局，验证跨模块导入不依赖插件目录在 `sys.path` 上。

渲染用例（`tests/test_search_card.py`）要求有含中文字形的字体：系统字体可用就用系统字体，否则由 `tests/cjk_font.py` 现场生成一个只含「永」的最小 TTF（依赖 `fontTools`，见 `requirements-dev.txt`）。这样没有中文字体的机器/CI 上渲染用例照常运行，而不是整批 skip。用例里凡涉及版式尺寸的断言都写成「等于推导值」而不是写死像素，避免字号一调就要改一批数字。

> **导入约束**：AstrBot 以包路径加载插件，插件目录本身不在 `sys.path`，因此同级模块之间必须用包内相对导入（`from .file_cache import ...`）；每个模块同时保留顶层绝对导入的回退分支，仅供离线测试以顶层模块方式导入时使用。新增同级模块时请沿用该写法。

代码结构：

| 文件 | 职责 |
| --- | --- |
| `main.py` | AstrBot 指令、配置、生命周期、OneBot 上传协调 |
| `kugou_client.py` | 概念版常量与签名、搜索、权益查询、多档位资源解析、档位降级编排 |
| `kugou_login.py` | 扫码登录协议、凭据校验 |
| `file_cache.py` | 安全文件名、URL 安全、受限流式下载、缓存归属与清理 |
| `audio_probe.py` | 音频头解析与实测档位判定 |
| `quality.py` | 档位词汇表与 `quality_priority` 解析 |
| `search_card.py` | 搜歌结果卡片的本地 Pillow 渲染（不联网、不导入 astrbot）；字号阶梯是版式唯一真源 |
| `cover_cache.py` | 封面的 SSRF 安全抓取与落盘缓存 |
| `font_store.py` | 卡片中文字体的取回：从固定镜像下载、按内容校验后落盘 |

## 更新日志

各版本的变更记录见 [CHANGELOG.md](CHANGELOG.md)。

## 许可与致谢

项目主许可为 [MIT](LICENSE)。卡片中文字体**不从本仓库分发**，仅在使用时从镜像取回，其许可如下：

| 资源 | 许可 | 用途 |
| --- | --- | --- |
| HarmonyOS Sans SC（[镜像仓库](https://gitee.com/IKKI2000/harmonyos-fonts)） | 仓库打包为 MIT，字体本体按华为公开条款免费使用（含商用，不得转售字体文件） | 搜歌卡片的中文字体，按需从镜像取回，说明见 [LICENSES](LICENSES/README.md) |

## 版权提醒

拥有播放权限不等于拥有再分发许可。请自行确认所下载歌曲的使用与分享授权；本插件不绕过版权、付费或加密限制，也不提供试听片段以外的内容。

## 致谢

- 接口签名、音质档位与登录流程参考 [KuGouMusicApi](https://github.com/MakcRe/KuGouMusicApi)（Node.js 版），本仓库用纯 Python 重写

## License

MIT