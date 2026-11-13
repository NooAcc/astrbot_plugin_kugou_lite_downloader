# LICENSES

本目录存放项目内第三方资源（目前只有卡片字体）的许可说明。

| 文件 | 许可 | 覆盖范围 |
| --- | --- | --- |
| `HarmonyOS-Fonts-MIT.txt` | MIT | [IKKI2000/harmonyos-fonts](https://gitee.com/IKKI2000/harmonyos-fonts) 镜像仓库本身的打包代码与目录结构；字体文件的取回地址也来自该仓库 |

这些文件是第三方许可的**原文副本，请勿修改**。

项目主许可为根目录的 [LICENSE](../LICENSE)（MIT）。

## 字体本体

卡片用的是华为发布的 **HarmonyOS Sans SC**，按该字体公开的授权条款使用：允许免费
使用（含商业用途），但**不得单独转售字体文件本身**。上面那份 MIT 只覆盖镜像仓库
的打包，不构成字体本体的授权依据。

**本项目不再随插件分发字体文件**（两份共 16 MB，会把插件更新包撑大），而是在需要
时从 [IKKI2000/harmonyos-fonts](https://gitee.com/IKKI2000/harmonyos-fonts) 镜像
取回，只用于渲染自己的搜歌卡片，不做修改、不转售。若需在应用商店页面等处展示授权
说明，请以华为官方发布的条款原文为准。

## 完整性校验

取回的字体文件按内容钉死（`font_store.FONT_SOURCES` 里的 `size` 与 `sha256`）；
对不上就丢弃并退回 AstrBot 默认字体：

| 文件 | 字节数 | sha256 |
| --- | --- | --- |
| `HarmonyOS_Sans_SC_Regular.ttf` | 8261128 | `297b088424be212207df2ce8b98e335468b782aa6b96832af0b8b773d711e2b1` |
| `HarmonyOS_Sans_SC_Bold.ttf` | 8158996 | `43a424b85e47fb53a17b3b32026a71801f86f8e022ca6798d186b47d39fa5f01` |

替换字体（升级版本、换字重）时必须同时改 `font_store.FONT_SOURCES` 的校验值，并
确认镜像上那份新字节与之相符：字体不再随包分发，这两个值就是唯一的锚点，一旦漂移
所有新装用户都只会退到 AstrBot 默认字体。核对方式：

```bash
CHECK_FONT_MIRROR=1 python -m pytest tests/test_font_store.py -q -k mirror
```
