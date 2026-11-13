"""AstrBot 插件入口：酷狗概念版歌曲文件发送。

职责：AstrBot 指令、配置解析、生命周期与后台任务清理、OneBot 文件上传协调。
协议细节在 kugou_client / kugou_login，文件与缓存安全在 file_cache，
档位词汇表在 quality，音频头判定在 audio_probe。

只发送音频**文件附件**：不发送音乐卡片、语音、封面图片或播放直链。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import PermissionType
from astrbot.api.star import Context, Star

# 同时兼容两种导入方式：AstrBot 以包路径 data.plugins.<dir>.main 加载时走
# 相对导入；离线测试/工具以顶层模块 main 加载时回退到绝对导入。
try:
    from .file_cache import (
        AudioFetcher,
        CacheManager,
        DownloadError,
        Downloader,
        Limits,
        SharedDirError,
        UnsafeUrlError,
    )
    from .kugou_client import (
        KG_UA,
        AllQualitiesFailed,
        Credentials,
        Device,
        DownloadLadder,
        KugouApiError,
        KugouClient,
        redact_secrets,
    )
    from .kugou_login import (
        QR_CONFIRMED,
        QR_EXPIRED,
        QR_SCANNED,
        QR_TRANSIENT,
        QR_WAITING,
        LiteLogin,
        LoginError,
        LoginResult,
        build_cookie,
        cookie_source_label,
        extract_cookie_field,
        validate_cookie,
    )
    from .quality import ConfigError, parse_quality_priority
except ImportError:  # 以顶层模块方式导入（离线测试）
    from file_cache import (
        AudioFetcher,
        CacheManager,
        DownloadError,
        Downloader,
        Limits,
        SharedDirError,
        UnsafeUrlError,
    )
    from kugou_client import (
        KG_UA,
        AllQualitiesFailed,
        Credentials,
        Device,
        DownloadLadder,
        KugouApiError,
        KugouClient,
        redact_secrets,
    )
    from kugou_login import (
        QR_CONFIRMED,
        QR_EXPIRED,
        QR_SCANNED,
        QR_TRANSIENT,
        QR_WAITING,
        LiteLogin,
        LoginError,
        LoginResult,
        build_cookie,
        cookie_source_label,
        extract_cookie_field,
        validate_cookie,
    )
    from quality import ConfigError, parse_quality_priority

PLUGIN_DIRNAME = "astrbot_plugin_kugou_music"

SEARCH_TTL_SECONDS = 300
CACHE_TTL_SECONDS = 3600
CLEANUP_INTERVAL_SECONDS = 600        # 运行期有界清理的最小间隔
DOWNLOAD_TIMEOUT_SECONDS = 180.0
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
MAX_CONCURRENT_DOWNLOADS = 2
QR_TTL_SECONDS = 300
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_POLL_INTERVAL = 2.0


def plugin_data_dir() -> str:
    """AstrBot 插件数据目录（凭据与搜索状态，绝不放共享音频目录）。"""
    return os.path.join(os.getcwd(), "data", "plugin_data", PLUGIN_DIRNAME)


def parse_shared_dir(raw) -> tuple[str, str]:
    """把配置值转成插件专属共享子目录，返回 `(绝对路径, 错误信息)`。"""
    text = str(raw or "").strip()
    if not text:
        return "", (
            "未配置共享缓存目录 shared_cache_dir：请在 WebUI 填写 AstrBot 与 "
            "OneBot 实现端都能访问的绝对路径"
        )
    if not os.path.isabs(text):
        return "", f"共享缓存目录必须是绝对路径：{text}"
    return os.path.join(os.path.abspath(text), PLUGIN_DIRNAME), ""


def search_cache_key(event) -> str:
    """搜索结果按「会话 + 发起用户」隔离，避免同群其他人的搜索覆盖自己的选择。"""
    return f"{event.unified_msg_origin}|{event.get_sender_id()}"


def build_upload_request(is_group: bool, target_id, file_path: str,
                         name: str) -> tuple[str, dict]:
    """构造 OneBot 文件上传调用（群/私聊使用不同的扩展 API）。"""
    try:
        numeric = int(str(target_id).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"无法识别的会话 ID：{target_id!r}") from exc
    if numeric <= 0:
        raise ValueError(f"无法识别的会话 ID：{target_id!r}")
    if is_group:
        return "upload_group_file", {"group_id": numeric, "file": file_path, "name": name}
    return "upload_private_file", {"user_id": numeric, "file": file_path, "name": name}


def check_upload_result(response) -> tuple[bool, str]:
    """判定 OneBot 上传结果。

    只有明确表示成功才算成功：非 dict、空结果、缺少 status/retcode、
    `status == "failed"`、非零 `retcode` 一律按失败处理，绝不报告发送成功。
    """
    if not isinstance(response, dict):
        return False, "OneBot 没有返回可识别的结果"
    if not response:
        return False, "OneBot 返回了空结果"
    if "status" not in response and "retcode" not in response:
        return False, "OneBot 返回结果缺少 status/retcode 字段"
    if response.get("status") == "failed":
        detail = response.get("wording") or response.get("msg") or "未知原因"
        return False, f"OneBot 返回失败：{detail}"
    retcode = response.get("retcode", 0)
    if retcode is not None and str(retcode).strip() != "0":
        detail = response.get("msg") or response.get("wording") or ""
        return False, f"OneBot 返回 retcode={retcode} {detail}".strip()
    return True, ""


class KugouMusic(Star):
    """酷狗概念版歌曲文件发送插件。"""

    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = dict(config or {})
        self._session = None
        self._closing = False
        self._debug: list = []
        self._poll_interval = DEFAULT_POLL_INTERVAL

        self._data_dir = plugin_data_dir()
        os.makedirs(self._data_dir, exist_ok=True)
        self._cookie_file = os.path.join(self._data_dir, "cookie.json")
        self._device_file = os.path.join(self._data_dir, "device.json")
        self._search_cache_file = os.path.join(self._data_dir, "search_cache.json")
        self._search_cache = self._read_json(self._search_cache_file, {})
        if not isinstance(self._search_cache, dict):
            self._search_cache = {}

        self._cache_manager: CacheManager | None = None
        self._cache_root = ""
        self._fetcher: AudioFetcher | None = None
        self._device: Device | None = None
        self._last_cleanup = 0.0

    # ── 通用配置读取 ─────────────────────────────────────────────────────

    def _ua(self) -> str:
        return str(self.config.get("kugou_ua", "") or "").strip() or KG_UA

    def _timeout(self) -> float:
        try:
            return float(self.config.get("timeout", DEFAULT_TIMEOUT_SECONDS))
        except (TypeError, ValueError):
            return DEFAULT_TIMEOUT_SECONDS

    def _search_count(self) -> int:
        try:
            return max(1, min(10, int(self.config.get("search_count", 5))))
        except (TypeError, ValueError):
            return 5

    def _quality_priority(self) -> list:
        return parse_quality_priority(self.config.get("quality_priority"))

    # ── 凭据 ─────────────────────────────────────────────────────────────

    def _load_saved_cookie(self) -> dict | None:
        saved = self._read_json(self._cookie_file, None)
        if isinstance(saved, dict) and saved.get("cookie"):
            return saved
        return None

    def _save_cookie(self, cookie: str, source: str = "qr") -> bool:
        saved = self._load_saved_cookie() or {}
        payload = {
            "cookie": cookie,
            "source": source,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if not self._write_json(self._cookie_file, payload):
            return False
        logger.info("[kugou] 登录凭据已保存（来源 %s）", source)
        return True

    def _clear_cookie(self) -> bool:
        """只清除插件管理的凭据，不改动 WebUI 里手动填写的 Cookie。"""
        try:
            if os.path.exists(self._cookie_file):
                os.remove(self._cookie_file)
        except OSError as exc:
            logger.warning("[kugou] 删除凭据文件失败：%s", type(exc).__name__)
            return False
        return True

    def _raw_cookie(self) -> str:
        """插件保存的凭据（扫码登录）优先于 WebUI 手动填写。"""
        saved = self._load_saved_cookie()
        if saved:
            return str(saved.get("cookie") or "")
        return str(self.config.get("kugou_cookie", "") or "").strip()

    def _credentials(self) -> Credentials | None:
        cookie = self._raw_cookie()
        ok, _reason = validate_cookie(cookie)
        if not ok:
            return None
        return Credentials(
            token=extract_cookie_field(cookie, "token"),
            userid=extract_cookie_field(cookie, "userid"),
        )

    def _persist_login(self, result: LoginResult) -> str:
        """保存登录结果并返回给用户的消息（保存失败必须体现出来）。"""
        cookie = build_cookie(result.token, result.userid)
        if not self._save_cookie(cookie, source=result.source):
            return (
                f"⚠️ 已获取登录凭据（userid: {result.userid}），"
                "但写入插件数据目录失败，请检查目录权限后重新登录"
            )
        return f"✅ 登录成功！\nuserid: {result.userid}\n凭据已保存，立即可用。"

    # ── 设备标识 ─────────────────────────────────────────────────────────

    def _get_device(self) -> Device:
        """Cookie 中的设备标识优先，其次使用本机生成并持久化的标识。"""
        persisted = self._load_or_create_device()
        cookie = self._raw_cookie()
        return Device(
            dfid=extract_cookie_field(cookie, "dfid") or persisted.dfid,
            mid=extract_cookie_field(cookie, "mid") or persisted.mid,
            uuid=extract_cookie_field(cookie, "uuid") or persisted.uuid,
        )

    def _load_or_create_device(self) -> Device:
        if self._device is not None:
            return self._device
        saved = self._read_json(self._device_file, {}) or {}
        dfid = str(saved.get("dfid") or "")
        mid = str(saved.get("mid") or "")
        device_uuid = str(saved.get("uuid") or "")
        if not dfid or not mid or not device_uuid:
            dfid = dfid or uuid.uuid4().hex
            mid = mid or uuid.uuid4().hex
            device_uuid = device_uuid or uuid.uuid4().hex
            self._write_json(self._device_file, {"dfid": dfid, "mid": mid, "uuid": device_uuid})
        self._device = Device(dfid=dfid, mid=mid, uuid=device_uuid)
        return self._device

    # ── 会话与客户端 ─────────────────────────────────────────────────────

    async def _get_session(self) -> aiohttp.ClientSession:
        closed = getattr(self._session, "closed", False)
        if self._session is None or closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT_SECONDS)
            )
        return self._session

    def _make_client(self, session) -> KugouClient:
        return KugouClient(
            session, self._get_device(), self._credentials(),
            ua=self._ua(), timeout=self._timeout(),
        )

    def _make_login(self, session) -> LiteLogin:
        return LiteLogin(session, self._get_device(), ua=self._ua(), timeout=self._timeout())

    async def _ladder_for_download(self) -> DownloadLadder:
        """校验配置并装配档位降级器。配置问题抛 ConfigError/SharedDirError。"""
        shared_root, error = parse_shared_dir(self.config.get("shared_cache_dir", ""))
        if error:
            raise SharedDirError(error)
        priority = self._quality_priority()          # 非法档位在此报配置错误
        session = await self._get_session()

        if self._cache_manager is None or self._cache_root != shared_root:
            cache = CacheManager(shared_root)
            cache.ensure_ready()
            self._cache_manager = cache
            self._cache_root = shared_root
            self._fetcher = None
        if self._fetcher is None:
            downloader = Downloader(
                session,
                Limits(
                    max_bytes=MAX_DOWNLOAD_BYTES,
                    total_seconds=DOWNLOAD_TIMEOUT_SECONDS,
                    max_concurrency=MAX_CONCURRENT_DOWNLOADS,
                ),
                user_agent=KG_UA,
            )
            self._fetcher = AudioFetcher(downloader, self._cache_manager)

        self._cleanup_cache()        # 运行期有界清理（内部限频）
        return DownloadLadder(
            self._make_client(session), self._fetcher, self._cache_manager, priority
        )

    def _cleanup_cache(self) -> None:
        if self._cache_manager is None:
            return
        now = time.time()
        if now - self._last_cleanup < CLEANUP_INTERVAL_SECONDS:
            return          # 运行期清理有界且限频，避免每次下载都扫描磁盘
        self._last_cleanup = now
        try:
            removed = self._cache_manager.cleanup(CACHE_TTL_SECONDS)
            if removed:
                logger.debug("[kugou] 清理过期缓存条目：%d", removed)
        except Exception as exc:
            logger.warning("[kugou] 缓存清理失败：%s", type(exc).__name__)

    # ── 调试输出 ─────────────────────────────────────────────────────────

    def _add_debug(self, action: str, **kwargs) -> None:
        if not self.config.get("debug", False):
            return
        entry = {"action": action, "time": time.strftime("%H:%M:%S")}
        entry.update(kwargs)
        self._debug.append(redact_secrets(entry))

    def _flush_debug(self) -> str:
        if not self._debug or not self.config.get("debug", False):
            self._debug = []
            return ""
        lines = ["\n[_debug]"]
        lines.extend(json.dumps(entry, ensure_ascii=False) for entry in self._debug)
        self._debug = []
        return "\n".join(lines)

    # ── JSON 读写 ────────────────────────────────────────────────────────

    @staticmethod
    def _read_json(path: str, default):
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as handle:
                    return json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("[kugou] 读取 %s 失败：%s", os.path.basename(path), type(exc).__name__)
        return default

    @staticmethod
    def _write_json(path: str, payload) -> bool:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp_path = path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
            return True
        except OSError as exc:
            logger.error("[kugou] 写入 %s 失败：%s", os.path.basename(path), type(exc).__name__)
            return False

    def _save_search_cache(self) -> None:
        self._write_json(self._search_cache_file, self._search_cache)

    # ── 生命周期 ─────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        logger.info("[kugou] 插件已加载：酷狗概念版 · 歌曲文件发送")
        shared_root, error = parse_shared_dir(self.config.get("shared_cache_dir", ""))
        if error:
            logger.warning("[kugou] %s（搜歌与登录仍可使用）", error)
            return
        try:
            cache = CacheManager(shared_root)
            cache.ensure_ready()
            self._cache_manager = cache
            self._cache_root = shared_root
            self._cleanup_cache()
        except SharedDirError as exc:
            logger.warning("[kugou] 共享缓存目录不可用：%s", exc)
        except Exception as exc:
            logger.warning("[kugou] 初始化共享缓存目录时出错：%s", type(exc).__name__)

    async def terminate(self) -> None:
        self._closing = True
        session = self._session
        self._session = None
        if session is not None and hasattr(session, "close"):
            try:
                await session.close()
            except Exception as exc:
                logger.debug("[kugou] 关闭 HTTP 会话失败：%s", type(exc).__name__)
        logger.info("[kugou] 插件已卸载")

    # ── 登录指令 ─────────────────────────────────────────────────────────

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("酷狗扫码登录")
    async def cmd_qr_login(self, event: AstrMessageEvent):
        """扫码登录（酷狗概念版，安卓设备渠道）。"""
        try:
            async for result in self._qr_login_flow(event):
                yield result
        finally:
            self._stop(event)

    async def _qr_login_flow(self, event: AstrMessageEvent):
        login = self._make_login(await self._get_session())
        yield event.plain_result("⏳ 正在生成二维码（酷狗概念版 · 安卓设备渠道）...")

        try:
            qr = await login.qr_start()
        except LoginError as exc:
            yield event.plain_result(f"❌ {exc}")
            return

        yield event.plain_result(
            "📱 请在手机浏览器打开以下链接，然后用手机酷狗 App 扫码\n\n"
            f"（登录请求为酷狗概念版协议，设备显示为安卓手机）\n"
            f"⏱ 二维码有效期 5 分钟\n\n"
            f"👉 {qr.scan_url}"
        )

        deadline = time.time() + QR_TTL_SECONDS
        last_state = 0
        while time.time() < deadline and not self._closing:
            await asyncio.sleep(self._poll_interval)
            status = await login.qr_check(qr.key)
            if status in (QR_TRANSIENT, QR_WAITING):
                continue
            if status == QR_SCANNED and last_state != QR_SCANNED:
                yield event.plain_result("✅ 已扫码，请在手机上点「确认登录」")
                last_state = QR_SCANNED
            elif status == QR_CONFIRMED:
                try:
                    result = await login.qr_credentials(qr.key)
                except LoginError as exc:
                    yield event.plain_result(f"⚠️ {exc}")
                    return
                yield event.plain_result(self._persist_login(result))
                return
            elif status == QR_EXPIRED:
                yield event.plain_result("❌ 二维码已过期，请重新发送 /酷狗扫码登录")
                return

        if self._closing:
            return
        yield event.plain_result("⏱ 二维码超时（5 分钟未扫码），请重新发送 /酷狗扫码登录")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("酷狗退出登录")
    async def cmd_logout(self, event: AstrMessageEvent):
        """清除插件管理的登录凭据。"""
        try:
            async for result in self._logout_flow(event):
                yield result
        finally:
            self._stop(event)

    async def _logout_flow(self, event: AstrMessageEvent):
        ok = self._clear_cookie()
        if ok:
            yield event.plain_result(
                "✅ 已退出登录，插件保存的凭据已清除。\n"
                "（WebUI 中手动填写的 kugou_cookie 不会被修改）"
            )
        else:
            yield event.plain_result("⚠️ 删除凭据文件失败，请检查插件数据目录权限")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("酷狗登录状态")
    async def cmd_login_status(self, event: AstrMessageEvent):
        """查看登录状态（不显示完整 token）。"""
        try:
            async for result in self._login_status_flow(event):
                yield result
        finally:
            self._stop(event)

    async def _login_status_flow(self, event: AstrMessageEvent):
        cookie = self._raw_cookie()
        if not cookie:
            yield event.plain_result(
                "❌ 未登录\n"
                "发送 /酷狗扫码登录 用手机扫码登录\n"
                "或在 WebUI 填写 kugou_cookie"
            )
            return

        ok, reason = validate_cookie(cookie)
        token = extract_cookie_field(cookie, "token")
        userid = extract_cookie_field(cookie, "userid") or "未知"
        preview = f"{token[:8]}..." if token else "无"

        lines = [
            "✅ 已保存登录凭据" if ok else "⚠️ 登录凭据存在问题",
            f"userid: {userid}",
            f"token: {preview}",
            f"来源: {cookie_source_label(self._load_saved_cookie(), self.config.get('kugou_cookie', ''))}",
            "说明: 本地存在凭据不等于在线有效，实际可用性以酷狗接口的授权结果为准。",
        ]
        if not ok:
            lines.append(f"问题: {reason}")
        yield event.plain_result("\n".join(lines))

    # ── OneBot 文件上传 ──────────────────────────────────────────────────

    @staticmethod
    def _evidence(song: dict, outcome) -> dict:
        """设计文档 §5 要求的可核对证据：URL、格式、规格、大小、时长。

        只在 debug 打开时输出；`_add_debug` 会经 `redact_secrets` 脱敏，
        直链查询串与签名不会进入输出。
        """
        info = outcome.info
        try:
            size = os.path.getsize(outcome.path)
        except OSError:
            size = 0
        return {
            "hash": str(song.get("hash") or ""),
            "tier": outcome.tier,
            "format": getattr(info, "container", "") or "",
            "sample_rate": getattr(info, "sample_rate", 0) or 0,
            "bits": getattr(info, "bits_per_sample", 0) or 0,
            "bitrate": getattr(info, "bitrate", 0) or 0,
            "channels": getattr(info, "channels", 0) or 0,
            "duration_ms": getattr(info, "duration_ms", 0) or 0,
            "size": size,
            "url": outcome.url,
        }

    async def _upload_outcome(self, event, outcome) -> str:
        """通过 OneBot 群/私聊文件接口发送文件，返回给用户的结果文本。"""
        is_group = bool(event.get_group_id())
        target = event.get_group_id() if is_group else event.get_sender_id()
        try:
            action, params = build_upload_request(is_group, target, outcome.path, outcome.filename)
        except ValueError as exc:
            return f"[酷狗] 文件上传失败：{exc}"

        api = getattr(getattr(event, "bot", None), "api", None)
        if api is None:
            return "[酷狗] 文件上传失败：当前平台没有可用的 OneBot API，无法发送文件附件"

        self._add_debug("upload_start", upload_action=action, target=str(target),
                        tier=outcome.tier)
        try:
            response = await api.call_action(action, **params)
        except Exception as exc:
            logger.error("[kugou] 文件上传异常：%s", type(exc).__name__)
            return (
                f"[酷狗] 文件上传失败：{type(exc).__name__}\n"
                f"请确认 OneBot 实现支持 {action} / upload_private_file 扩展接口，"
                "并确认共享缓存目录在 OneBot 端可读"
            )

        ok, reason = check_upload_result(response)
        if not ok:
            logger.warning("[kugou] 文件上传未成功：%s", reason)
            return f"[酷狗] 文件上传失败：{reason}"

        source = "（复用缓存）" if outcome.from_cache else ""
        return (
            f"[酷狗] 已发送文件「{outcome.filename}」{source}\n"
            f"实际音质: {outcome.label}\n"
            "（发送成功仅表示 OneBot 接受上传请求）"
        )

    @staticmethod
    def _stop(event) -> None:
        if hasattr(event, "stop_event"):
            event.stop_event()

    # ── 搜索指令 ─────────────────────────────────────────────────────────

    async def _search_songs(self, keyword: str) -> list:
        session = await self._get_session()
        return await self._make_client(session).search(keyword, self._search_count())

    def _prune_search_cache(self) -> None:
        now = time.time()
        stale = [
            key for key, entry in self._search_cache.items()
            if not isinstance(entry, dict) or entry.get("expire_at", 0) < now
        ]
        for key in stale:
            self._search_cache.pop(key, None)

    @filter.command("酷狗搜歌")
    async def cmd_search(self, event: AstrMessageEvent):
        """搜索歌曲：/酷狗搜歌 <关键词>"""
        try:
            async for result in self._search_flow(event):
                yield result
        finally:
            self._stop(event)

    async def _search_flow(self, event: AstrMessageEvent):
        keyword = re.sub(r"^/?\s*酷狗\s*搜歌\s*", "", event.message_str.strip()).strip()
        if not keyword:
            yield event.plain_result("用法: /酷狗搜歌 <关键词>\n例如: /酷狗搜歌 晴天")
            return

        try:
            songs = await self._search_songs(keyword)
        except KugouApiError as exc:
            yield event.plain_result(f"[酷狗] 搜索失败，请稍后重试（{exc}）")
            return
        except Exception as exc:
            logger.error("[kugou] 搜索出错：%s", type(exc).__name__)
            yield event.plain_result("[酷狗] 搜索失败，请稍后重试")
            return

        if not songs:
            yield event.plain_result(
                f"[酷狗] 未找到与「{keyword}」相关的歌曲" + self._flush_debug()
            )
            return

        self._prune_search_cache()
        self._search_cache[search_cache_key(event)] = {
            "songs": songs,
            "expire_at": time.time() + SEARCH_TTL_SECONDS,
            "keyword": keyword,
        }
        self._save_search_cache()

        lines = [f"[酷狗搜索] 「{keyword}」共找到 {len(songs)} 首歌:"]
        for index, song in enumerate(songs, 1):
            duration = ""
            if song.get("duration"):
                minutes, seconds = divmod(int(song["duration"]), 60)
                duration = f" [{minutes}:{seconds:02d}]"
            vip = " [VIP]" if song.get("pay_type") == 3 else ""
            lines.append(f"{index}. {song['songname']} - {song['singername']}{duration}{vip}")
        lines.append("\n发送 /酷狗下载 序号 获取歌曲文件")
        yield event.plain_result("\n".join(lines) + self._flush_debug())

    # ── 下载指令 ─────────────────────────────────────────────────────────

    @filter.command("酷狗下载")
    async def cmd_download(self, event: AstrMessageEvent):
        """下载并发送歌曲文件：/酷狗下载 <序号>"""
        async for result in self._download_flow(event):
            yield result

    @filter.command("酷狗点歌")
    async def cmd_play(self, event: AstrMessageEvent):
        """兼容入口，行为与 /酷狗下载 完全相同。"""
        async for result in self._download_flow(event):
            yield result

    async def _download_flow(self, event: AstrMessageEvent):
        """两个入口共用的下载流程：校验 → 降级取文件 → 上传文件附件。"""
        raw = re.sub(r"^/?\s*酷狗\s*(下载|点歌)\s*", "", event.message_str.strip()).strip()

        cached = self._search_cache.get(search_cache_key(event))
        if not isinstance(cached, dict) or cached.get("expire_at", 0) < time.time():
            yield event.plain_result("[酷狗] 请先使用 /酷狗搜歌 <关键词> 搜索歌曲")
            self._stop(event)
            return

        songs = cached.get("songs")
        if not isinstance(songs, list) or not songs:
            yield event.plain_result("[酷狗] 请先使用 /酷狗搜歌 <关键词> 重新搜索歌曲")
            self._stop(event)
            return
        try:
            index = int(raw) - 1
        except (TypeError, ValueError):
            yield event.plain_result(
                f"[酷狗] 请输入有效的序号\n例如: /酷狗下载 1  (范围 1-{len(songs)})"
            )
            self._stop(event)
            return
        if index < 0 or index >= len(songs):
            yield event.plain_result(f"[酷狗] 序号超出范围，请输入 1-{len(songs)}")
            self._stop(event)
            return

        song = songs[index]
        if not isinstance(song, dict):
            yield event.plain_result("[酷狗] 缓存的歌曲信息已损坏，请重新搜索")
            self._stop(event)
            return

        # 配置问题在触网之前就报出来，并指明需要改哪个配置项
        try:
            ladder = await self._ladder_for_download()
        except (SharedDirError, ConfigError) as exc:
            yield event.plain_result(f"[酷狗] 暂时无法下载，配置有问题：{exc}")
            self._stop(event)
            return
        except Exception as exc:
            logger.error("[kugou] 初始化下载环境失败：%s", type(exc).__name__)
            yield event.plain_result("[酷狗] 初始化下载环境失败，请查看插件日志")
            self._stop(event)
            return

        label = f"{song.get('singername', '')} - {song.get('songname', '')}"
        yield event.plain_result(f"[酷狗] 正在获取「{label}」的歌曲文件...")

        try:
            outcome = await ladder.run(song)
        except AllQualitiesFailed as exc:
            yield event.plain_result(f"[酷狗] 没有可用的音质：\n{exc}{self._flush_debug()}")
            self._stop(event)
            return
        except UnsafeUrlError as exc:
            yield event.plain_result(
                f"[酷狗] 资源地址未通过安全检查，已中止：{exc.reason}"
            )
            self._stop(event)
            return
        except (SharedDirError, ConfigError) as exc:
            yield event.plain_result(f"[酷狗] 配置或目录问题，已中止：{exc}")
            self._stop(event)
            return
        except DownloadError as exc:
            yield event.plain_result(f"[酷狗] 下载失败：{exc.reason}")
            self._stop(event)
            return
        except Exception as exc:
            logger.error("[kugou] 下载流程出错：%s", type(exc).__name__)
            yield event.plain_result("[酷狗] 下载失败，请查看插件日志")
            self._stop(event)
            return

        self._add_debug("verified", **self._evidence(song, outcome))
        try:
            message = await self._upload_outcome(event, outcome)
        finally:
            await ladder.release(outcome)

        yield event.plain_result(message + self._flush_debug())
        self._stop(event)
