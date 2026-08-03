import logging
import asyncio
import secrets
import time
import urllib.request
from pathlib import Path
from typing import Dict, Any, Optional
from datetime import timedelta
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .ssl_client import SSLClient
from .https_client import HttpsClient
from .cos_media import CosMediaManager
from .video_archive import VideoArchiver
from .history import history_dir, save_snapshot
from .temp_password import (
    describe_record,
    is_expired,
    parse_authorization_item,
    parse_grant_response,
)
from .device_types import (
    DeviceCategory,
    classify_device,
    get_device_profile,
    is_hidden_category,
)
from .redact import fingerprint, redact_packet
from .models import AccountCredentials
from .cloud import CHINA_CLOUD, CloudEndpoint, cloud_candidates
from .state_store import StateSource, StateStore
from .parsers import get_state_parser
from .lock_manager import LockEventManager
from .control_router import (
    ControlRoute,
    brightness_route,
    color_temp_route,
    power_route,
)
from .const import (
    SSL_PORT,
    UPDATE_INTERVAL,
    DEVICE_TYPE_SWITCH,
    DEVICE_TYPE_LIGHT,
    DEVICE_TYPE_COVER,
    DEVICE_TYPE_CLOTHES_HORSE,
    CMD_CONTROL,
    DEFAULT_KEY,
    SOFTWARE_VER, DEBUG_INFO,
)

_LOGGER = logging.getLogger(__name__)


def _download_bytes(url: str, timeout: int = 30) -> Optional[bytes]:
    """下载预签名 URL 返回字节（同步阻塞，调用方应放线程池）。"""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "orvibohomebridge"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        err_body = e.read(600).decode("utf-8", "replace")
        _LOGGER.warning(
            "下载失败 HTTP %s: %s", e.code, err_body[:500]
        )
        return None
    except Exception as e:  # noqa: BLE001
        _LOGGER.warning("下载失败: %r", e)
        return None


class OrviboMeshCoordinator(DataUpdateCoordinator[Dict[str, Any]]):
    MOTION_RESET_DELAY = 30  # 人体传感器触发后恢复延时（秒）
    EMERGENCY_RESET_DELAY = 180  # 紧急按钮触发后恢复延时（秒），3分钟
    LOCK_RESET_DELAY = 5  # 门锁门铃/开锁事件触发后恢复延时（秒）
    LOCK_DOOR_OPEN_WINDOW = 30  # 开锁 → 开门 的归属窗口（秒）
    
    def __init__(
        self,
        hass: HomeAssistant,
        credentials: AccountCredentials,
        lock_user_names: Optional[Dict[str, str]] = None,
        cloud: CloudEndpoint = CHINA_CLOUD,
    ):
        self.credentials = credentials
        self.username = credentials.username
        self.password_hash = credentials.password_hash
        self.family_id = credentials.family_id
        self.hass = hass

        self.https_client = HttpsClient(
            username=credentials.username,
            password_hash=credentials.password_hash,
            session=async_get_clientsession(hass),
            cloud=cloud,
        )
        self.https_client.family_id = credentials.family_id or None
        self.ssl_client = None
        self.cos_media: Optional[CosMediaManager] = None
        self.video_archiver: Optional[VideoArchiver] = None
        self._lock_cameras: Dict[str, Any] = {}  # device_id -> camera 实体
        self._snapshot_pending: set[tuple[str, str]] = set()  # (device_id, object_key) 进行中
        self._history_cleanup_unsub = None
        self.HISTORY_KEEP_DAYS = 7  # 历史截图/录像保留天数
        self._temp_passwords: Dict[str, list[dict]] = {}  # device_id -> 临时密码记录
        self._temp_cleanup_unsub = None
        self.TEMP_PASSWORD_MAX = 4  # 服务端限制：每设备最多 4 个临时密码
        
        self._motion_reset_tasks: Dict[str, asyncio.Task] = {}  # 人体传感器重置任务
        self._emergency_reset_tasks: Dict[str, asyncio.Task] = {}  # 紧急按钮重置任务
        self._lock_reset_tasks: Dict[tuple, asyncio.Task] = {}  # 门锁事件复位任务
        self._lock_user_names: Dict[str, Dict[str, str]] = {}  # device_id -> {user_id: 名称}
        self._lock_user_names_shared: Dict[str, str] = dict(lock_user_names or {})  # 持久化映射（entry 级）
        self.lock_events = LockEventManager(
            self.lock_user_name,
            door_open_window=self.LOCK_DOOR_OPEN_WINDOW,
        )
        
        # 调试信息：记录最近收到的原始状态推送（仅内存，日志/诊断均脱敏）
        self._cmd42_log: list[dict] = []
        self._redaction_salt = secrets.token_bytes(32)
        self._last_update_time: Dict[str, float] = {}  # 设备最后更新时间戳
        self.OFFLINE_TIMEOUT = 600  # 设备离线超时秒数

        super().__init__(
            hass,
            _LOGGER,
            name="Orvibo Mesh Coordinator",
            update_interval=UPDATE_INTERVAL,
        )

        self.devices: Dict[str, Any] = {}
        self.device_states: Dict[str, Any] = {}
        self.state_store = StateStore(self.device_states)

    def _apply_registered_state_parser(
        self, category: DeviceCategory, dev_state: dict, raw_status: dict
    ) -> bool:
        """Apply a pure state parser registered for the device category."""

        parser = get_state_parser(category)
        if parser is None:
            return False
        parser(dev_state, raw_status).apply_to(dev_state)
        return True

    def _apply_motion_state_parser(
        self, dev_state: dict, raw_status: dict, device_id: Optional[str]
    ) -> None:
        """Parse motion state, then own its coordinator-level reset lifecycle."""

        self._apply_registered_state_parser(
            DeviceCategory.MOTION_SENSOR, dev_state, raw_status
        )
        value3 = raw_status.get("value3")
        reset_key = device_id or raw_status.get("deviceId", "")
        if value3 is None or not reset_key:
            return
        try:
            detected = int(value3) == 1
        except (TypeError, ValueError):
            return
        if detected:
            asyncio.create_task(self._schedule_motion_reset(reset_key))
        else:
            self._cancel_motion_reset(reset_key)

    def _apply_emergency_state_parser(
        self, dev_state: dict, raw_status: dict, device_id: Optional[str]
    ) -> None:
        """Parse an emergency button, then own its reset-task lifecycle."""

        self._apply_registered_state_parser(
            DeviceCategory.EMERGENCY_BUTTON, dev_state, raw_status
        )
        value1 = raw_status.get("value1")
        if value1 is None or not device_id:
            return
        try:
            triggered = int(value1) == 1
        except (TypeError, ValueError):
            return
        if triggered:
            asyncio.create_task(self._schedule_emergency_reset(device_id))
        else:
            self._cancel_emergency_reset(device_id)

    async def _schedule_motion_reset(self, device_id: str) -> None:
        """安排人体传感器状态重置"""
        self._cancel_motion_reset(device_id)
        
        async def reset_motion():
            await asyncio.sleep(self.MOTION_RESET_DELAY)
            state = self.device_states.get(device_id)
            if state and state.get("motion_detected"):
                state["motion_detected"] = False
                _LOGGER.debug(f"[人体传感器] {device_id[:12]}... 延时{self.MOTION_RESET_DELAY}秒后恢复为未触发")
                self.async_update_listeners()
        
        self._motion_reset_tasks[device_id] = asyncio.create_task(reset_motion())

    def _cancel_motion_reset(self, device_id: str) -> None:
        """取消人体传感器状态重置任务"""
        task = self._motion_reset_tasks.pop(device_id, None)
        if task and not task.done():
            task.cancel()

    async def _schedule_emergency_reset(self, device_id: str) -> None:
        """安排紧急按钮状态重置（3分钟后自动恢复为正常）"""
        self._cancel_emergency_reset(device_id)

        async def reset_emergency():
            await asyncio.sleep(self.EMERGENCY_RESET_DELAY)
            state = self.device_states.get(device_id)
            if state and state.get("emergency_state"):
                state["emergency_state"] = False
                _LOGGER.debug(f"[紧急按钮] {device_id[:12]}... 延时{self.EMERGENCY_RESET_DELAY}秒后恢复为正常")
                self.async_update_listeners()

        self._emergency_reset_tasks[device_id] = asyncio.create_task(reset_emergency())

    def _cancel_emergency_reset(self, device_id: str) -> None:
        """取消紧急按钮状态重置任务"""
        task = self._emergency_reset_tasks.pop(device_id, None)
        if task and not task.done():
            task.cancel()

    def _apply_lock_transient_event(
        self, device_id: str, dev_state: dict, raw_status: dict
    ) -> None:
        """Apply transient cmd=352 flags and schedule their reset."""

        update = self.lock_events.transient_update(raw_status)
        update.patch.apply_to(dev_state)
        if update.reset_kind:
            self._schedule_lock_reset(device_id, update.reset_kind)

    def _schedule_lock_reset(self, device_id: str, kind: str) -> None:
        """安排门锁事件状态复位（默认5秒后恢复为未触发），同设备同类型只保留一个任务。"""
        key = (device_id, kind)
        task = self._lock_reset_tasks.pop(key, None)
        if task and not task.done():
            task.cancel()

        async def reset_lock():
            try:
                await asyncio.sleep(self.LOCK_RESET_DELAY)
            except asyncio.CancelledError:
                return
            state = self.device_states.get(device_id)
            if not state:
                return
            if kind == "doorbell" and state.get("doorbell_ring"):
                state["doorbell_ring"] = False
            elif kind == "unlock" and state.get("unlock_event"):
                state["unlock_event"] = False
            self.async_set_updated_data(self.device_states)

        self._lock_reset_tasks[key] = asyncio.create_task(reset_lock())

    def _publish_lock_event(self, device_id: str, raw_status: dict) -> None:
        """把归一化后的门锁状态/事件发布到 HA 事件总线（日志脱敏）。"""
        from .const import LOCK_EVENT

        data = self.lock_events.build_event(device_id, raw_status)
        if data is None:
            return
        if data.get("kind"):
            data.update(self._attach_media_urls(device_id, raw_status, data))
        self.hass.bus.async_fire(LOCK_EVENT, data)
        _LOGGER.debug(
            "[门锁事件总线] device=%s locked=%s door=%s kind=%s",
            fingerprint(device_id, self._redaction_salt),
            data.get("locked"),
            data.get("door_open"),
            data.get("kind"),
        )

    def _attach_media_urls(
        self,
        device_id: str,
        raw_status: dict,
        event: dict,
    ) -> Dict[str, str]:
        """把事件里的 COS 对象键签成临时 URL（media_url/pic_media_url/doorbell_media_url）。

        使用已缓存的门锁 COS 凭证同步签名（零网络等待）；无有效凭证时
        触发后台刷新（cmd=313，36 小时有效），本轮事件不阻塞。
        """
        cos = self.cos_media
        if cos is None or not event:
            return {}
        uid = raw_status.get("uid") or event.get("uid") or ""
        if not uid:
            dev = self.devices.get(device_id) or {}
            uid = dev.get("uid", "")
        # time 字段可能为 None（而非缺失），统一兜底为当前时间戳
        event_ts = event.get("time") or int(time.time())
        snapshot_kind = event.get("snapshot_kind") or event.get("kind", "event")
        out: Dict[str, str] = {}
        for field, target in (
            ("video_url", "media_url"),
            ("pic_url", "pic_media_url"),
            ("doorbell_url", "doorbell_media_url"),
        ):
            key = event.get(field)
            if not key:
                continue
            url = cos.try_signed_url(device_id, uid, key)
            if url:
                out[target] = url
            elif cos.cached_credentials(device_id) is None:
                self.hass.async_create_task(cos.get_credentials(device_id, uid))
            if field == "video_url":
                self._schedule_video_archive(
                    device_id,
                    uid,
                    key,
                    url,
                    out,
                    snapshot_kind,
                    event_ts,
                )
        snapshot_key = event.get("pic_url") or event.get("doorbell_url")
        if snapshot_key:
            self.hass.async_create_task(
                self._update_lock_snapshot(
                    device_id,
                    uid,
                    snapshot_key,
                    snapshot_kind,
                    event_ts,
                )
            )
        return out

    def register_lock_camera(self, device_id: str, camera: Any) -> None:
        """camera 平台实体注册（device_id → 实体），事件截图推送用。"""
        self._lock_cameras[device_id] = camera
        _LOGGER.debug("门锁截图实体已注册 device=%s", fingerprint(device_id, self._redaction_salt))

    async def _update_lock_snapshot(
        self,
        device_id: str,
        device_uid: str,
        object_key: str,
        kind: str,
        ts: int | str,
    ) -> None:
        """获取凭证 → 签名 URL → 下载截图 → 落盘历史 → 推送 camera 实体。"""
        dedup_key = (device_id, object_key)
        if dedup_key in self._snapshot_pending:
            return  # 同一事件已有一个下载任务在跑（cmd352/cmd82 可能重复触发）
        self._snapshot_pending.add(dedup_key)
        camera = self._lock_cameras.get(device_id)
        if camera is None:
            _LOGGER.debug(
                "门锁截图实体未注册，跳过截图更新 device=%s",
                fingerprint(device_id, self._redaction_salt),
            )
            self._snapshot_pending.discard(dedup_key)
            return
        cos = self.cos_media
        if cos is None:
            self._snapshot_pending.discard(dedup_key)
            return
        try:
            url = await cos.signed_url(device_id, device_uid, object_key)
            if not url:
                _LOGGER.warning("门锁截图签名 URL 获取失败 device=%s", fingerprint(device_id, self._redaction_salt))
                return
            image: Optional[bytes] = None
            # 门铃图片上传可能在事件推送后数十秒才完成：先等 8 秒再首试，
            # 失败间隔 10/20/25 秒重试（总窗口 ~63s），给足上传时间
            retry_delays = (8, 10, 20, 25)
            for attempt, delay in enumerate(retry_delays):
                if delay:
                    await asyncio.sleep(delay)
                image = await self.hass.async_add_executor_job(_download_bytes, url)
                if image:
                    break
                _LOGGER.debug(
                    "截图下载重试 %s/%s device=%s",
                    attempt + 1,
                    len(retry_delays),
                    fingerprint(device_id, self._redaction_salt),
                )
        except Exception:  # noqa: BLE001 - 截图失败不应影响事件流
            _LOGGER.exception("门锁截图更新异常 device=%s", fingerprint(device_id, self._redaction_salt))
            return
        finally:
            self._snapshot_pending.discard(dedup_key)
        if image:
            try:
                await self.hass.async_add_executor_job(
                    lambda: save_snapshot(
                        history_dir(self.hass.config.path("media"), device_id),
                        kind,
                        ts,
                        image,
                    )
                )
            except OSError as e:
                _LOGGER.warning("截图落盘失败: %s", e)
            await camera.async_set_image(image)
            _LOGGER.info(
                "门锁截图已更新并归档 device=%s kind=%s bytes=%s",
                fingerprint(device_id, self._redaction_salt),
                kind,
                len(image),
            )
        else:
            _LOGGER.warning(
                "门锁截图下载为空 device=%s key=%s",
                fingerprint(device_id, self._redaction_salt),
                object_key,
            )

    def _schedule_video_archive(
        self,
        device_id: str,
        device_uid: str,
        object_key: str,
        signed_url: Optional[str],
        out: Dict[str, str],
        kind: str,
        ts: int | str,
    ) -> None:
        """后台下载事件录像并转 MP4；事件附带预期本地路径与媒体 ID。"""
        archiver = self.video_archiver
        if archiver is None:
            return
        _h264, mp4_path = archiver.event_paths(device_id, kind, ts)
        if mp4_path.exists() and mp4_path.stat().st_size > 0:
            out["video_file"] = str(mp4_path)
            out["media_id"] = archiver.media_source_id(mp4_path)
            return
        # 预期目标（后台填充完成后即为可播放 MP4）
        out["video_file"] = str(mp4_path)
        if signed_url:
            self.hass.async_create_task(
                self._archive_video(device_id, kind, ts, signed_url)
            )

    async def _archive_video(
        self,
        device_id: str,
        kind: str,
        ts: int | str,
        signed_url: str,
    ) -> None:
        """下载 + 转码（executor 中执行，避免阻塞事件循环）。"""
        archiver = self.video_archiver
        if archiver is None:
            return
        try:
            result = await self.hass.async_add_executor_job(
                archiver.archive_event, device_id, kind, ts, signed_url
            )
        except Exception as e:  # noqa: BLE001 - 归档失败不应影响事件流
            _LOGGER.warning(
                "录像归档失败 device=%s kind=%s: %s",
                fingerprint(device_id, self._redaction_salt),
                kind,
                e,
            )
            return
        if result and result.get("mp4_file") and Path(result["mp4_file"]).exists():
            _LOGGER.info(
                "录像已归档 device=%s -> %s",
                fingerprint(device_id, self._redaction_salt),
                result["mp4_file"],
            )
        else:
            _LOGGER.debug("录像下载/转码未完成（无 ffmpeg 或下载失败）")

    async def async_fetch_video(
        self,
        device_id: str,
        object_key: str,
        device_uid: str = "",
    ) -> Dict[str, Any]:
        """主动拉取事件录像：返回 {video_file, media_id, mp4_file, h264_file}。

        供 orvibohomebridge.fetch_video 服务调用；凭证缺失时自动换取。
        """
        archiver = self.video_archiver
        cos = self.cos_media
        if archiver is None or cos is None:
            return {"error": "录像归档未就绪"}
        dev = self.devices.get(device_id)
        if dev is None or classify_device(dev) != DeviceCategory.DOOR_LOCK:
            return {"error": "设备不存在或不是门锁"}
        from .video_archive import normalize_event_object_key

        object_key = normalize_event_object_key(object_key) or ""
        if not object_key:
            return {"error": "无效的事件录像对象键"}
        if not device_uid:
            device_uid = dev.get("uid", "")
        url = await cos.signed_url(device_id, device_uid, object_key)
        if not url:
            return {"error": "无法获取 COS 凭证或签名 URL"}
        result = await self.hass.async_add_executor_job(
            archiver.archive, device_id, object_key, url
        )
        return result or {"error": "录像下载失败"}

    async def async_list_events(
        self,
        device_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, str]]:
        """查询门锁事件历史（截图/录像），按时间倒序。"""
        from .history import list_history

        return await self.hass.async_add_executor_job(
            list_history,
            self.hass.config.path("media"),
            device_id or None,
            limit,
        )

    def start_history_cleanup(self) -> None:
        """启动历史清理：立即清理一次 + 每周定时清理（保留 7 天）。"""
        if self._history_cleanup_unsub is not None:
            return
        from homeassistant.helpers.event import async_track_time_interval
        from .history import cleanup

        media_root = self.hass.config.path("media")

        async def _run(_now=None) -> None:
            try:
                removed = await self.hass.async_add_executor_job(
                    cleanup, media_root, self.HISTORY_KEEP_DAYS
                )
                if removed:
                    _LOGGER.info("历史清理完成，删除 %s 个过期文件", removed)
            except Exception:  # noqa: BLE001 - 清理失败不应影响集成
                _LOGGER.warning("历史清理异常", exc_info=True)

        # 启动时清一次（处理升级前积压），随后每周执行
        self.hass.async_create_task(_run())
        self._history_cleanup_unsub = async_track_time_interval(
            self.hass, _run, timedelta(days=7)
        )

    def stop_history_cleanup(self) -> None:
        """取消历史清理定时任务（集成卸载时调用）。"""
        if self._history_cleanup_unsub is not None:
            self._history_cleanup_unsub()
            self._history_cleanup_unsub = None

    async def async_cleanup_history(
        self,
        keep_days: int = 7,
        device_id: str = "",
        max_entries: Optional[int] = None,
    ) -> int:
        """手动清理历史记录，返回删除文件数。"""
        from .history import cleanup

        return await self.hass.async_add_executor_job(
            cleanup,
            self.hass.config.path("media"),
            keep_days,
            device_id or None,
            max_entries,
        )

    async def async_grant_temp_password(
        self,
        device_id: str,
        auth_type: int = 2,
        minutes: int = 1440,
        number: int = 1,
        name: str = "",
        phone: str = "",
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        device_uid: str = "",
    ) -> Dict[str, Any]:
        """下发临时密码（cmd=246），记录并发布事件。

        返回 {error} 或归一化记录（含 password/authorized_id/有效期等）。
        """
        from .const import TEMP_PASSWORD_EVENT

        sslc = self.ssl_client
        if sslc is None:
            return {"error": "SSL 客户端未就绪"}
        dev = self.devices.get(device_id)
        if dev is None or classify_device(dev) != DeviceCategory.DOOR_LOCK:
            return {"error": "设备不存在或不是门锁"}
        if auth_type not in (1, 2):
            return {"error": "授权类型必须为 1 或 2"}
        if not 1 <= minutes <= 525600:
            return {"error": "有效期必须在 1 到 525600 分钟之间"}
        if not 0 <= number <= 100:
            return {"error": "可用次数必须在 0 到 100 之间"}
        # 先同步服务器端授权状态（App 删除/过期后 readtable 会反映），
        # 避免本地内存累积导致误判上限
        server_records = await self.async_fetch_server_temp_passwords()
        device_active = [
            r for r in server_records if r.get("device_id") == device_id
        ]
        if len(device_active) >= self.TEMP_PASSWORD_MAX:
            return {"error": f"临时密码已达上限（{self.TEMP_PASSWORD_MAX} 个），请先删除旧密码"}
        records = self._temp_passwords.setdefault(device_id, [])
        if not device_uid:
            device_uid = dev.get("uid", "")
        name = name or f"临时用户 {time.strftime('%m%d%H%M')}"
        resp = await sslc.send_temp_password(
            device_id=device_id,
            device_uid=device_uid,
            name=name,
            auth_type=auth_type,
            minutes=minutes,
            number=number,
            phone=phone,
            start_time=start_time,
            end_time=end_time,
        )
        if not resp:
            return {"error": "未收到 cmd=246 响应（超时）"}
        if resp.get("status") not in (None, 0, "0"):
            return {"error": f"下发失败 status={resp.get('status')} msg={resp.get('msg')}"}
        record = parse_grant_response(resp)
        if record is None:
            return {"error": "响应缺少密码或 authorizedId"}
        record["device_id"] = device_id
        records.append(record)
        # 只保留最近 10 条历史
        if len(records) > 10:
            self._temp_passwords[device_id] = records[-10:]
        info = describe_record(record)
        event_info = {key: value for key, value in info.items() if key != "password"}
        self.hass.bus.async_fire(
            TEMP_PASSWORD_EVENT,
            {"device_id": device_id, **event_info},
        )
        # 触发实体刷新（临时密码传感器重新读取）
        self.device_states.setdefault(device_id, {})["temp_password_ts"] = time.time()
        self.async_set_updated_data(self.device_states)
        _LOGGER.info(
            "临时密码已下发 device=%s authorizedId=%s",
            fingerprint(device_id, self._redaction_salt),
            record["authorized_id"],
        )
        return info

    async def async_revoke_temp_password(
        self,
        device_id: str,
        authorized_id: int,
        device_uid: str = "",
    ) -> Dict[str, Any]:
        """删除临时密码（cmd=247）。"""
        sslc = self.ssl_client
        if sslc is None:
            return {"error": "SSL 客户端未就绪"}
        dev = self.devices.get(device_id)
        if dev is None or classify_device(dev) != DeviceCategory.DOOR_LOCK:
            return {"error": "设备不存在或不是门锁"}
        if authorized_id <= 0:
            return {"error": "authorized_id 必须为正整数"}
        if not device_uid:
            device_uid = dev.get("uid", "")
        resp = await sslc.delete_authorization(
            device_id=device_id,
            device_uid=device_uid,
            authorized_id=authorized_id,
        )
        if not resp:
            return {"error": "未收到 cmd=247 响应（超时）"}
        if resp.get("status") not in (None, 0, "0"):
            return {"error": f"删除失败 status={resp.get('status')}"}
        records = self._temp_passwords.get(device_id, [])
        self._temp_passwords[device_id] = [
            r for r in records if int(r.get("authorized_id", -1)) != int(authorized_id)
        ]
        self.device_states.setdefault(device_id, {})["temp_password_ts"] = time.time()
        self.async_set_updated_data(self.device_states)
        _LOGGER.info(
            "临时密码已删除 device=%s authorizedId=%s",
            fingerprint(device_id, self._redaction_salt),
            authorized_id,
        )
        return {"ok": True, "authorized_id": authorized_id}

    async def async_list_temp_passwords(self, device_id: str = "") -> Dict[str, Any]:
        """列出服务器端全部临时密码（readtable authorizedUnlock，含过期状态）。"""
        records = await self.async_fetch_server_temp_passwords()
        if device_id:
            records = [r for r in records if r.get("device_id") == device_id]
        result: Dict[str, Any] = {}
        for r in records:
            did = r.get("device_id") or "unknown"
            info = describe_record(r)
            info.pop("password", None)
            result.setdefault(did, []).append(info)
        return result

    async def async_fetch_server_temp_passwords(self) -> list[dict]:
        """从 readtable（REST 全量同步）拉取 authorizedUnlock 表。"""
        client = self.https_client
        if client is None or not client.is_logged_in:
            _LOGGER.warning(
                "拉取临时密码列表跳过: client=%s logged_in=%s",
                client is not None,
                client.is_logged_in if client else None,
            )
            return []
        try:
            data = await client._readtable(device_flag=0)
        except Exception as e:  # noqa: BLE001 - 列表失败不影响其他功能
            _LOGGER.warning("拉取临时密码列表失败: %s", e)
            return []
        if not isinstance(data, dict):
            _LOGGER.warning("拉取临时密码列表: readtable 返回非 dict: %s", type(data))
            return []
        auth = data.get("authorizedUnlock")
        _LOGGER.info(
            "拉取临时密码列表: readtable keys=%s authorizedUnlock=%s",
            list(data.keys())[:12],
            f"list[{len(auth)}]" if isinstance(auth, list) else type(auth).__name__,
        )
        records = []
        for item in auth or []:
            rec = parse_authorization_item(item)
            if rec is None:
                continue
            rec["device_id"] = item.get("deviceId") or ""
            records.append(rec)
        # 同步内存记录（供传感器展示，保留下发时的 name）
        mem = self._temp_passwords
        self._temp_passwords = {}
        for rec in records:
            did = rec["device_id"]
            self._temp_passwords.setdefault(did, [])
            existing = next(
                (
                    m
                    for m in mem.get(did, [])
                    if int(m.get("authorized_id", -1)) == rec["authorized_id"]
                ),
                None,
            )
            merged = dict(rec)
            if existing:
                merged["name"] = existing.get("name") or ""
                merged["type"] = existing.get("type") or 0
            self._temp_passwords[did].append(merged)
        return records

    def temp_password_state(self, device_id: str) -> Optional[dict]:
        """给传感器用：返回最近一条有效临时密码的展示信息。"""
        records = self._temp_passwords.get(device_id, [])
        active = [r for r in records if not is_expired(r)]
        if not active:
            return None
        latest = active[-1]
        return describe_record(latest)

    def start_temp_password_cleanup(self) -> None:
        """启动临时密码自动回收：每 6 小时清理过期/次数用尽的密码。"""
        if self._temp_cleanup_unsub is not None:
            return
        from homeassistant.helpers.event import async_track_time_interval

        async def _run(_now=None) -> None:
            for device_id, records in list(self._temp_passwords.items()):
                for record in list(records):
                    if not is_expired(record):
                        continue
                    try:
                        await self.async_revoke_temp_password(
                            device_id, int(record["authorized_id"])
                        )
                    except Exception:  # noqa: BLE001 - 回收失败下次再试
                        _LOGGER.warning(
                            "临时密码自动回收失败 device=%s authorizedId=%s",
                            fingerprint(device_id, self._redaction_salt),
                            record.get("authorized_id"),
                        )

        self.hass.async_create_task(_run())
        self._temp_cleanup_unsub = async_track_time_interval(
            self.hass, _run, timedelta(hours=6)
        )

    def stop_temp_password_cleanup(self) -> None:
        """取消临时密码回收定时任务（集成卸载时调用）。"""
        if self._temp_cleanup_unsub is not None:
            self._temp_cleanup_unsub()
            self._temp_cleanup_unsub = None

    def lock_user_name(self, device_id: str, user_id: object) -> Optional[str]:
        """返回门锁 userId 配置的显示名称（无配置返回 None）。"""
        if not isinstance(user_id, (str, int)):
            return None
        key = str(user_id)
        name = self._lock_user_names.get(device_id, {}).get(key)
        if name is None:
            name = self._lock_user_names_shared.get(key)
        return name if isinstance(name, str) and name else None

    def set_lock_user_name(self, device_id: str, user_id: str, name: str) -> bool:
        """为门锁 userId 设置/清除显示名称。"""
        device_id = str(device_id or "")
        user_id = str(user_id or "")
        if not device_id or not user_id:
            return False
        names = self._lock_user_names.setdefault(device_id, {})
        if name:
            names[user_id] = str(name)
            self._lock_user_names_shared[user_id] = str(name)
        else:
            names.pop(user_id, None)
            self._lock_user_names_shared.pop(user_id, None)
        return True

    def _publish_lock_message(self, device_id: str, raw_status: dict) -> None:
        """发布 cmd=82 推送消息事件（门锁文本消息/告警，日志脱敏）。"""
        from .const import LOCK_EVENT

        event = self.lock_events.build_message(device_id, raw_status)
        if event is None:
            return
        event.update(self._attach_media_urls(device_id, raw_status, event))
        self.hass.bus.async_fire(LOCK_EVENT, event)
        _LOGGER.debug(
            "[门锁消息] device=%s kind=%s is_alarm=%s text=%s",
            fingerprint(device_id, self._redaction_salt),
            event.get("kind"),
            event.get("is_alarm"),
            event.get("text"),
        )

    def _parse_status_generic(self, dev_state: dict, raw_status: dict) -> None:
        """通用状态解析（未知设备类型）"""
        props = raw_status.get("properties", {})
        
        # 尝试提取常见字段
        onoff_obj = props.get("onoff", {})
        if onoff_obj and isinstance(onoff_obj, dict) and onoff_obj.get("status"):
            dev_state["state"] = onoff_obj.get("status") == "on"
        else:
            dev_state["state"] = raw_status.get("state", False)
        
        dev_state["brightness"] = raw_status.get("value2", props.get("brightness"))
        dev_state["color_temp"] = raw_status.get("value3", props.get("colortemp"))
        dev_state["position"] = raw_status.get("value1", props.get("percent"))
        
        _LOGGER.debug(f"[通用设备] state={dev_state['state']}")

    async def _discover_devices(self):
        """拉取并解析设备列表：readtable → getDeviceDesc → queryHomepageData 三层回退"""
        device_status_data = await self.https_client.fetch_device_status()
        if not device_status_data:
            return None, []

        devices = self.https_client.parse_device_status_list(device_status_data)
        if not devices:
            _LOGGER.warning("readtable 未解析到设备，回退到 getDeviceDesc 构建设备列表...")
            desc_data = await self.https_client.fetch_device_desc(last_update_time=0)
            if desc_data:
                desc_devices = desc_data.get("deviceDescList", desc_data.get("devices", []))
                if isinstance(desc_devices, list) and desc_devices:
                    devices = self.https_client.parse_device_status_list(
                        {"device": desc_devices, "deviceStatus": {}}
                    )
                    _LOGGER.info(f"getDeviceDesc 回退解析到 {len(devices)} 个设备")

        if not devices:
            _LOGGER.warning("getDeviceDesc 未构建到设备，回退到 queryHomepageData...")
            homepage_data = await self.https_client.fetch_homepage_data()
            if isinstance(homepage_data, dict):
                homepage_devices = homepage_data.get("deviceList", homepage_data.get("device", [])) or []
                if isinstance(homepage_devices, list) and homepage_devices:
                    devices = self.https_client.parse_device_status_list(
                        {"device": homepage_devices, "deviceStatus": {}}
                    )
                    _LOGGER.info(f"queryHomepageData 回退解析到 {len(devices)} 个设备")

        return device_status_data, devices

    async def _async_setup(self):
        try:
            if self.family_id:
                self.https_client.family_id = self.family_id
                self.https_client.family_name = None

            if not await self.https_client.async_detect_cloud(self.family_id):
                raise ConfigEntryAuthFailed("HTTPS登录失败")

            _LOGGER.debug("第一步：拉取设备列表（三层回退）...")
            device_status_data, devices = await self._discover_devices()

            if not devices:
                fallback_cloud = cloud_candidates(self.https_client.cloud)[1]
                _LOGGER.warning(
                    "%s 云端未发现设备，尝试 %s 云端 ...",
                    self.https_client.cloud.region.value,
                    fallback_cloud.region.value,
                )
                await self.https_client.switch_cloud(
                    fallback_cloud,
                    family_id=self.family_id or None,
                )
                if await self.https_client.ensure_login():
                    device_status_data, devices = await self._discover_devices()
                    if devices:
                        _LOGGER.warning(
                            "%s 云端发现 %s 个设备，本实例将使用该区域",
                            fallback_cloud.region.value,
                            len(devices),
                        )
                else:
                    _LOGGER.error("%s 云端登录失败", fallback_cloud.region.value)

            if device_status_data is None and not devices:
                raise UpdateFailed("获取设备列表失败")

            for device in devices:
                device_id = device["device_id"]
                device_name = device.get("device_name", "")
                device_type_raw = device.get("device_type_raw", "")

                # 过滤隐藏类别设备（MIXPAD_GATEWAY/MIX_SWITCH/BACH_SWITCH/WIFI_CAMERA/SMART_REMOTE/MIXPAD_4WAY_BASE）
                category = classify_device(device)
                _LOGGER.info(f"设备分类: deviceId={device_id}, name={device_name}, deviceType={device_type_raw}, category={category.name}")
                if is_hidden_category(category):
                    _LOGGER.debug(f"[过滤] 跳过隐藏类别设备: {device_id} category={category.name}")
                    continue

                self.devices[device_id] = device
                online_status = device.get("online", False)
                if isinstance(online_status, str):
                    online_status = online_status.strip().lower() in ("online", "1", "true", "yes")

                self.device_states[device_id] = {
                    "state": device.get("state", False),
                    "online": bool(online_status),
                    "position": device.get("position", 0),
                    "brightness": device.get("brightness"),
                    "color_temp": device.get("color_temp"),
                    "uid": device.get("uid", ""),
                    "status_id": device.get("status_id", ""),
                    "gateway_id": device.get("gateway_id", ""),
                    "ext_addr": device.get("ext_addr"),
                    "properties": {}  # 新增properties容器兼容mqtt cmd=42
                }

                # 门锁电池不随 cmd=42 常推（仅变化/上线时），初始化时
                # 从 readtable 设备属性补齐，后续推送增量更新
                if category == DeviceCategory.DOOR_LOCK:
                    from .lock_status import normalize_battery_properties as _norm_bat

                    battery = _norm_bat(device.get("properties") or {})
                    for bkey in (
                        "dry_battery_level",
                        "dry_battery_setup",
                        "lithium_battery_level",
                        "lithium_battery_setup",
                    ):
                        if bkey in battery:
                            self.device_states[device_id][bkey] = battery[bkey]

                # 晾衣架设备初始化专属字段（真实值由 cmd=100 查询后 cmd=99 推送回填）
                if category == DeviceCategory.CLOTHES_HORSE:
                    self.device_states[device_id].update({
                        "motor_state": "stop",
                        "lighting_state": False,
                        "heat_drying_state": False,
                        "wind_drying_state": False,
                        "sterilizing_state": False,
                        "main_switch_state": False,
                    })

                # 新风系统设备初始化专属字段
                if category == DeviceCategory.VENTILATION_SYSTEM:
                    self.device_states[device_id].update({
                        "fan_speed": device.get("fan_speed", "停"),
                        "temperature": device.get("temperature"),
                    })

            _LOGGER.info(f"设备列表拉取完成，共 {len(self.devices)} 个设备")

            await self._init_ssl_client()

            if self.ssl_client:
                ssl_ok = await self.ssl_client.connect_and_login()
                login_status = getattr(self.ssl_client, "_login_status", None)
                if not ssl_ok and login_status is not None and login_status != 0:
                    # 服务器明确拒绝了登录（如密码错误 status=12），触发 HA 重新认证
                    raise ConfigEntryAuthFailed(
                        f"SSL 登录被服务器拒绝 (status={login_status})"
                    )
                if ssl_ok:
                    await self._query_clothes_horse_initial_status()
                    # 后台预热门锁 COS 媒体凭证，事件到达时可直接签名出 URL
                    self.hass.async_create_task(self._prewarm_cos_credentials())
                else:
                    _LOGGER.warning(
                        "SSL 连接/登录未就绪（非认证拒绝），将在后台重试"
                    )

            _LOGGER.info(f"初始化完成，共 {len(self.devices)} 个设备")
            for device_id, dev in self.devices.items():
                state = self.device_states.get(device_id, {})
                category = classify_device(dev)
                
                if category == DeviceCategory.TEMP_HUMIDITY_SENSOR:
                    self._apply_registered_state_parser(category, state, {"properties": state.get("properties", {}), "value3": state.get("value3"), "value4": state.get("value4")})
                elif category == DeviceCategory.DOOR_WINDOW_SENSOR:
                    self._apply_registered_state_parser(category, state, {"value3": state.get("value3"), "value4": state.get("value4")})
                elif category == DeviceCategory.MOTION_SENSOR:
                    self._apply_motion_state_parser(state, {"value3": state.get("value3"), "value4": state.get("value4")}, device_id)
                elif category == DeviceCategory.SMOKE_SENSOR:
                    self._apply_registered_state_parser(category, state, {"value3": state.get("value3"), "value4": state.get("value4")})
                elif category == DeviceCategory.EMERGENCY_BUTTON:
                    self._apply_emergency_state_parser(state, {"value1": state.get("value1"), "value4": state.get("value4")}, device_id)
                elif category == DeviceCategory.WATER_LEAK_SENSOR:
                    self._apply_registered_state_parser(category, state, {"value1": state.get("value1"), "value4": state.get("value4")})
                elif category == DeviceCategory.GAS_SENSOR:
                    self._apply_registered_state_parser(category, state, {"value1": state.get("value1"), "value4": state.get("value4")})
                elif category == DeviceCategory.DOOR_LOCK:
                    self._apply_registered_state_parser(
                        category, state, {"properties": state.get("properties", {})}
                    )
                elif category == DeviceCategory.VENTILATION_SYSTEM:
                    self._apply_registered_state_parser(category, state, {"properties": state.get("properties", {}), "value1": state.get("value1")})
                
                _LOGGER.debug(
                    f"  设备: name={dev.get('device_name')}, device_id={device_id}, "
                    f"deviceType={dev.get('device_type_raw')}, uid={dev.get('uid')}, "
                    f"online={state.get('online')}, state={state.get('state')}"
                )

            self.async_set_updated_data(self.device_states)
        except ConfigEntryAuthFailed:
            raise
        except Exception as e:
            raise UpdateFailed(f"初始化失败: {str(e)}") from e

    async def _async_update_data(self) -> Dict[str, Any]:
        _LOGGER.debug("正在更新设备数据...")
        try:
            device_status_data = await self.https_client.fetch_device_status()
            if device_status_data:
                devices = self.https_client.parse_device_status_list(device_status_data)
                for device in devices:
                    device_id = device["device_id"]

                    # 过滤隐藏类别设备
                    category = classify_device(device)
                    if is_hidden_category(category):
                        # 已存在的隐藏设备从字典中移除
                        self.devices.pop(device_id, None)
                        self.device_states.pop(device_id, None)
                        self.state_store.remove(device_id)
                        self.lock_events.remove(device_id)
                        continue

                    self.devices[device_id] = device
                    if device_id not in self.device_states:
                        self.device_states[device_id] = {
                            "state": device.get("state", False),
                            "online": device.get("online", False),
                            "position": device.get("position", 0),
                            "brightness": device.get("brightness"),
                            "color_temp": device.get("color_temp"),
                            "properties": {}
                        }
                    cloud_state = {
                        field: device[field]
                        for field in (
                            "state",
                            "online",
                            "position",
                            "brightness",
                            "color_temp",
                            "temperature",
                            "humidity",
                        )
                        if field in device and device[field] is not None
                    }
                    status = device.get("status", {})
                    if isinstance(status, dict):
                        cloud_state.update(status)
                    if cloud_state:
                        self.state_store.merge(
                            device_id,
                            cloud_state,
                            StateSource.CLOUD,
                        )
                    # 电池低频推送：定期刷新时从 readtable 设备属性同步
                    if category == DeviceCategory.DOOR_LOCK:
                        from .lock_status import normalize_battery_properties as _nb

                        battery = _nb(device.get("properties") or {})
                        battery_updates = {}
                        for bkey in (
                            "dry_battery_level",
                            "dry_battery_setup",
                            "lithium_battery_level",
                            "lithium_battery_setup",
                        ):
                            if bkey in battery:
                                battery_updates[bkey] = battery[bkey]
                        self.state_store.merge(
                            device_id,
                            battery_updates,
                            StateSource.CLOUD,
                        )
            return self.device_states
        except Exception as e:
            raise UpdateFailed(f"更新失败: {str(e)}") from e

    async def _init_ssl_client(self):
        if self.ssl_client is not None:
            return

        while not self.https_client.family_id:
            _LOGGER.debug("等待family_id...")
            await asyncio.sleep(1)

        def on_session_id_obtained(session_id: str):
            _LOGGER.debug("设置session_id: %s", session_id)
            self.https_client.set_session_id(session_id)

        def on_status_update(device_id: str, raw_status: dict):
            """处理MQTT状态推送，根据设备类型调用对应的解析方法"""
            _LOGGER.debug(
                "收到MQTT状态更新: deviceId=%s cmd=%s",
                fingerprint(device_id, self._redaction_salt),
                raw_status.get("cmd"),
            )

            # 记录原始推送到内存（最多200条，仅诊断页脱敏展示，绝不写入日志）
            self._cmd42_log.append({
                "ts": __import__("time").time(),
                "device_id": device_id,
                "raw": dict(raw_status),
            })
            if len(self._cmd42_log) > 200:
                self._cmd42_log = self._cmd42_log[-200:]
            
            # 多重匹配逻辑
            matched_device_id = None
            uid = raw_status.get("uid", "")

            if device_id in self.device_states:
                matched_device_id = device_id
            elif uid and uid != device_id:
                for stored_id, dev_info in self.device_states.items():
                    if dev_info.get("uid") == uid:
                        matched_device_id = stored_id
                        break
            else:
                for stored_id, dev_info in self.device_states.items():
                    if dev_info.get("uid") == device_id or dev_info.get("status_id") == device_id or dev_info.get("ext_addr") == device_id:
                        matched_device_id = stored_id
                        break
                    if stored_id.startswith("w-") and stored_id[2:] == device_id:
                        matched_device_id = stored_id
                        break

            if not matched_device_id:
                _LOGGER.debug(f"MQTT推送设备 {device_id} 未匹配本地设备")
                return

            dev_state = self.device_states[matched_device_id]
            state_before = dict(dev_state)
            dev_state["properties"] = raw_status.get("properties", {})
            dev_state["online"] = True
            self._last_update_time[matched_device_id] = __import__("time").time()

            # 获取设备信息，根据 deviceType / category 调用对应的解析方法
            device_info = self.devices.get(matched_device_id)
            device_type = device_info.get("device_type_raw", 0) if device_info else 0
            sub_type = device_info.get("sub_device_type") if device_info else None
            category = classify_device(device_info) if device_info else DeviceCategory.UNKNOWN

            _LOGGER.debug(f"[设备类型] deviceType={device_type}, category={category.name}, deviceId={matched_device_id}")

            # cmd=82 推送消息（门锁文本消息/告警）：只发事件，不改设备状态属性
            if raw_status.get("cmd") == 82:
                self._publish_lock_message(matched_device_id, raw_status)
                self.state_store.mark(
                    matched_device_id,
                    ("online", "properties"),
                    StateSource.SSL,
                )
                return

            # 晾衣架专用协议（cmd=99 推送，带 is_clothes_horse 标志）
            if raw_status.get("is_clothes_horse"):
                self._apply_registered_state_parser(
                    DeviceCategory.CLOTHES_HORSE, dev_state, raw_status
                )
            elif raw_status.get("cmd") == 352:
                self._apply_lock_transient_event(
                    matched_device_id, dev_state, raw_status
                )
            elif device_type == 38:
                if sub_type == 6:
                    self._apply_registered_state_parser(
                        DeviceCategory.FAST_MOVE_DIM_COLOR_LIGHT,
                        dev_state,
                        raw_status,
                    )
                else:
                    self._apply_registered_state_parser(
                        DeviceCategory.DIM_COLOR_LIGHT, dev_state, raw_status
                    )
            elif device_type == 502:
                self._apply_registered_state_parser(
                    DeviceCategory.DIMMABLE_LIGHT, dev_state, raw_status
                )
            elif device_type == 0 and sub_type == -2:
                self._apply_registered_state_parser(
                    DeviceCategory.ZIGBEE_DIMMABLE_LIGHT, dev_state, raw_status
                )
            elif device_type == 503:
                self._apply_registered_state_parser(
                    DeviceCategory.CCT_LIGHT, dev_state, raw_status
                )
            elif device_type == 300 and sub_type == 491:
                self._apply_registered_state_parser(
                    DeviceCategory.TEMP_HUMIDITY_SENSOR, dev_state, raw_status
                )
            elif device_type == 46:
                self._apply_registered_state_parser(
                    DeviceCategory.DOOR_WINDOW_SENSOR, dev_state, raw_status
                )
            elif device_type == 26:
                self._apply_motion_state_parser(
                    dev_state, raw_status, matched_device_id
                )
            elif device_type == 27:
                self._apply_registered_state_parser(
                    DeviceCategory.SMOKE_SENSOR, dev_state, raw_status
                )
            elif device_type == 56:
                self._apply_emergency_state_parser(
                    dev_state, raw_status, matched_device_id
                )
            elif device_type == 54:
                self._apply_registered_state_parser(
                    DeviceCategory.WATER_LEAK_SENSOR, dev_state, raw_status
                )
            elif device_type == 522:
                self._apply_registered_state_parser(
                    DeviceCategory.DOOR_LOCK, dev_state, raw_status
                )
            elif device_type in (102, 501):
                self._apply_registered_state_parser(
                    DeviceCategory.LEGACY_LIGHT
                    if device_type == 102
                    else DeviceCategory.MONO_LIGHT,
                    dev_state,
                    raw_status,
                )
            elif device_type == 34:
                self._apply_registered_state_parser(
                    DeviceCategory.ZIGBEE_CURTAIN, dev_state, raw_status
                )
            elif device_type == 36:
                self._apply_registered_state_parser(
                    DeviceCategory.FAN_COIL_AC, dev_state, raw_status
                )
            elif device_type == 516:
                self._apply_registered_state_parser(
                    DeviceCategory.VENTILATION_SYSTEM, dev_state, raw_status
                )
            elif device_type in (135, 136):
                self._apply_registered_state_parser(
                    DeviceCategory.MIX_SWITCH, dev_state, raw_status
                )
            else:
                # 用 category 兜底路由
                if category in (DeviceCategory.SIMPLE_ZIGBEE_LIGHT, DeviceCategory.MONO_LIGHT, DeviceCategory.LIGHT_VIRTUAL_GROUP):
                    self._apply_registered_state_parser(category, dev_state, raw_status)
                elif category == DeviceCategory.ZIGBEE_CURTAIN:
                    self._apply_registered_state_parser(category, dev_state, raw_status)
                elif category in (DeviceCategory.CCT_LIGHT_STRIP, DeviceCategory.CCT_LIGHT):
                    self._apply_registered_state_parser(category, dev_state, raw_status)
                elif category == DeviceCategory.FAN_COIL_AC:
                    self._apply_registered_state_parser(category, dev_state, raw_status)
                elif category == DeviceCategory.VENTILATION_SYSTEM:
                    self._apply_registered_state_parser(category, dev_state, raw_status)
                elif category == DeviceCategory.DIMMABLE_LIGHT:
                    self._apply_registered_state_parser(category, dev_state, raw_status)
                elif category == DeviceCategory.ZIGBEE_DIMMABLE_LIGHT:
                    self._apply_registered_state_parser(category, dev_state, raw_status)
                elif category == DeviceCategory.FAST_MOVE_DIM_COLOR_LIGHT:
                    self._apply_registered_state_parser(category, dev_state, raw_status)
                elif category == DeviceCategory.TEMP_HUMIDITY_SENSOR:
                    self._apply_registered_state_parser(category, dev_state, raw_status)
                elif category == DeviceCategory.DOOR_WINDOW_SENSOR:
                    self._apply_registered_state_parser(category, dev_state, raw_status)
                elif category == DeviceCategory.MOTION_SENSOR:
                    self._apply_motion_state_parser(
                        dev_state, raw_status, matched_device_id
                    )
                elif category == DeviceCategory.SMOKE_SENSOR:
                    self._apply_registered_state_parser(category, dev_state, raw_status)
                elif category == DeviceCategory.EMERGENCY_BUTTON:
                    self._apply_emergency_state_parser(
                        dev_state, raw_status, matched_device_id
                    )
                elif category == DeviceCategory.WATER_LEAK_SENSOR:
                    self._apply_registered_state_parser(category, dev_state, raw_status)
                elif category == DeviceCategory.GAS_SENSOR:
                    self._apply_registered_state_parser(category, dev_state, raw_status)
                elif category == DeviceCategory.DOOR_LOCK:
                    self._apply_registered_state_parser(category, dev_state, raw_status)
                else:
                    self._parse_status_generic(dev_state, raw_status)

            changed_fields = {
                field
                for field in set(state_before) | set(dev_state)
                if state_before.get(field) != dev_state.get(field)
            }
            # 即使值未变化，SSL 回包也确认了当前快照；短保护窗口内
            # 不应被可能稍旧的 readtable 结果回滚。
            confirmed_fields = changed_fields | set(dev_state)
            self.state_store.mark(
                matched_device_id,
                confirmed_fields,
                StateSource.SSL,
            )

            # 门锁状态/事件归一化后发布到 HA 事件总线（供自动化订阅）
            if (
                device_type == 522
                or category == DeviceCategory.DOOR_LOCK
                or raw_status.get("cmd") == 352
            ):
                self._publish_lock_event(matched_device_id, raw_status)

            # 通知HA刷新实体状态
            self.hass.add_job(self.async_set_updated_data, self.device_states)
            _LOGGER.debug(f"[{matched_device_id}] MQTT状态同步完成: state={dev_state.get('state')}, bri={dev_state.get('brightness')}, ct={dev_state.get('color_temp')}, pos={dev_state.get('position')}")

        # SSL 控制通道跟随当前客户端的云端区域，不依赖模块级全局状态。
        ssl_host = self.https_client.cloud.ssl_host
        self.ssl_client = SSLClient(
            hass=self.hass,
            ssl_host=ssl_host,
            ssl_port=SSL_PORT,
            username=self.username,
            password_hash=self.password_hash,
            family_id=self.https_client.family_id,
            on_status_update=on_status_update,
            on_session_id_obtained=on_session_id_obtained,
        )
        self.cos_media = CosMediaManager(
            ssl_client=self.ssl_client,
            user_id=self.https_client.user_id or "",
            family_id=self.https_client.family_id or "",
        )
        self.video_archiver = VideoArchiver(
            media_root=self.hass.config.path("media"),
        )

    async def _prewarm_cos_credentials(self) -> None:
        """后台预热所有门锁的 COS 媒体凭证（cmd=313，36 小时有效）。"""
        cos = self.cos_media
        if cos is None:
            return
        for device_id, device in self.devices.items():
            if classify_device(device) != DeviceCategory.DOOR_LOCK:
                continue
            uid = device.get("uid", "")
            try:
                await cos.get_credentials(device_id, uid)
            except Exception as e:  # noqa: BLE001 - 预热失败不应阻断启动
                _LOGGER.warning(
                    "门锁媒体凭证预热失败 device=%s: %s",
                    fingerprint(device_id, self._redaction_salt),
                    e,
                )

    async def _query_clothes_horse_initial_status(self) -> None:
        """SSL 登录成功后，对所有晾衣架设备下发 cmd=100 查询初始状态。"""
        if not self.ssl_client:
            return
        for device_id, device in self.devices.items():
            category = classify_device(device)
            if category != DeviceCategory.CLOTHES_HORSE:
                continue
            try:
                await self.ssl_client.send_clothes_horse_query(device_id=device_id)
                _LOGGER.debug(f"[晾衣架初始查询] 已下发 cmd=100 device={device_id}")
            except Exception as e:
                _LOGGER.warning(f"[晾衣架初始查询] {device_id} 失败: {e}")

    async def _wait_for_control_response(self, device_id: str) -> dict | None:
        """发送控制后等待设备返回状态，3秒超时兜底。"""
        if not self.ssl_client:
            return None
        return await self.ssl_client._wait_for_control_response(device_id)

    def _apply_optimistic_state(
        self,
        device_id: str,
        values: Dict[str, Any],
    ) -> None:
        """Apply a local command fallback until SSL/cloud confirms it."""
        self.state_store.merge(
            device_id,
            values,
            StateSource.OPTIMISTIC,
            force=True,
        )

    async def _execute_control_route(
        self, device_id: str, device_uid: str, route: ControlRoute
    ) -> bool:
        """Execute an already-selected route while keeping I/O in the coordinator."""

        owner = self.ssl_client if route.scope == "ssl" else self
        if owner is None:
            return False
        method = getattr(owner, route.method)
        prefix = (
            (device_id, device_uid)
            if route.scope in ("ssl", "coordinator_uid")
            else (device_id,)
        )
        return await method(*prefix, *route.args, **route.kwargs)

    async def async_turn_on(self, device_id: str, brightness: int = None, color_temp: int = None) -> bool:
        """打开设备（基于 category 路由控制命令）。

        可选参数 brightness/color_temp 用于灯光一次性下发。
        """
        if not self.ssl_client:
            _LOGGER.error("SSL客户端未初始化")
            return False

        device = self.devices.get(device_id)
        if not device:
            _LOGGER.error(f"设备不存在: {device_id}")
            return False
        if get_device_profile(device).registration_only:
            _LOGGER.warning("未知设备仅注册展示，拒绝下发控制: %s", device_id)
            return False

        device_uid = device.get("uid", "")
        category = classify_device(device)
        _LOGGER.debug(f"打开设备: {device_id}, category={category.name}, uid={device_uid}")

        route = power_route(
            category,
            True,
            self.get_device_state(device_id) or {},
            brightness=brightness,
            color_temp=color_temp,
        )
        result = await self._execute_control_route(device_id, device_uid, route)

        if result:
            # ★ 等待设备响应来更新状态
            response = await self._wait_for_control_response(device_id)
            if response:
                # 让 SSL 推送过来的数据通过 on_status_update 更新 coordinator
                # _update_device_state 已经在 on_status_update 回调中被调用
                pass
            else:
                # 超时，保留乐观更新兜底
                optimistic = {"state": True}
                if brightness is not None:
                    optimistic["brightness"] = brightness
                if color_temp is not None:
                    optimistic["color_temp"] = color_temp
                self._apply_optimistic_state(device_id, optimistic)
            self.async_set_updated_data(self.device_states)
        return result

    async def async_turn_off(self, device_id: str) -> bool:
        """关闭设备（基于 category 路由控制命令）。"""
        if not self.ssl_client:
            _LOGGER.error("SSL客户端未初始化")
            return False

        device = self.devices.get(device_id)
        if not device:
            _LOGGER.error(f"设备不存在: {device_id}")
            return False
        if get_device_profile(device).registration_only:
            _LOGGER.warning("未知设备仅注册展示，拒绝下发控制: %s", device_id)
            return False

        device_uid = device.get("uid", "")
        category = classify_device(device)
        _LOGGER.debug(f"关闭设备: {device_id}, category={category.name}, uid={device_uid}")

        route = power_route(
            category,
            False,
            self.get_device_state(device_id) or {},
        )
        result = await self._execute_control_route(device_id, device_uid, route)

        if result:
            # ★ 等待设备响应
            response = await self._wait_for_control_response(device_id)
            if not response:
                # 超时，保留乐观更新兜底
                self._apply_optimistic_state(device_id, {"state": False})
            self.async_set_updated_data(self.device_states)
        return result

    async def async_set_cover_position(self, device_id: str, position: int) -> bool:
        if not self.ssl_client:
            _LOGGER.error("SSL客户端未初始化")
            return False
        device = self.devices.get(device_id)
        if not device:
            _LOGGER.error(f"设备不存在: {device_id}")
            return False
        if get_device_profile(device).registration_only:
            _LOGGER.warning("未知设备仅注册展示，拒绝下发窗帘控制: %s", device_id)
            return False
        device_uid = device.get("uid", "")
        _LOGGER.debug(f"设置窗帘位置: {device_id} position={position}")
        result = await self.ssl_client.send_control_cover(device_id, device_uid, position)
        if result:
            # ★ 等待设备响应
            response = await self._wait_for_control_response(device_id)
            if response:
                pos = response.get("value1")
                if pos is not None and 0 <= pos <= 100:
                    self.device_states.setdefault(device_id, {})["position"] = pos
                    self.device_states[device_id]["state"] = pos > 0
            else:
                self._apply_optimistic_state(
                    device_id,
                    {"position": position, "state": position > 0},
                )
            self.async_set_updated_data(self.device_states)
        return result

    async def async_stop_cover(self, device_id: str) -> bool:
        """停止窗帘电机。"""
        if not self.ssl_client:
            _LOGGER.error("SSL客户端未初始化")
            return False
        device = self.devices.get(device_id)
        if not device:
            _LOGGER.error(f"设备不存在: {device_id}")
            return False
        if get_device_profile(device).registration_only:
            _LOGGER.warning("未知设备仅注册展示，拒绝下发窗帘控制: %s", device_id)
            return False
        device_uid = device.get("uid", "")
        _LOGGER.debug(f"停止窗帘: {device_id}")
        return await self.ssl_client.send_control_cover(device_id, device_uid, "stop")

    async def async_set_brightness(self, device_id: str, brightness: int) -> bool:
        """设置亮度（HA LightEntity 使用 0-255 范围）。"""
        if not self.ssl_client:
            _LOGGER.error("SSL客户端未初始化")
            return False
        device = self.devices.get(device_id)
        if not device:
            _LOGGER.error(f"设备不存在: {device_id}")
            return False
        if get_device_profile(device).registration_only:
            _LOGGER.warning("未知设备仅注册展示，拒绝下发亮度控制: %s", device_id)
            return False
        uid = device.get("uid", "")
        category = classify_device(device)

        route = brightness_route(
            category,
            brightness,
            self.device_states.get(device_id, {}),
            device_type_raw=device.get("device_type_raw"),
        )
        result = await self._execute_control_route(device_id, uid, route)
        if result:
            response = await self._wait_for_control_response(device_id)
            if not response:
                self._apply_optimistic_state(device_id, dict(route.optimistic))
            self.async_set_updated_data(self.device_states)
        return result

    async def async_set_color_temp(self, device_id: str, color_temp_k: int) -> bool:
        """单独设置色温（Kelvin）"""
        if not self.ssl_client:
            _LOGGER.error("SSL客户端未初始化")
            return False
        device = self.devices.get(device_id)
        if not device:
            _LOGGER.error(f"设备不存在: {device_id}")
            return False
        if get_device_profile(device).registration_only:
            _LOGGER.warning("未知设备仅注册展示，拒绝下发色温控制: %s", device_id)
            return False
        uid = device.get("uid", "")
        category = classify_device(device)
        route = color_temp_route(
            category,
            color_temp_k,
            self.device_states.get(device_id, {}),
            device_type_raw=device.get("device_type_raw"),
        )
        result = await self._execute_control_route(device_id, uid, route)
        if result:
            response = await self._wait_for_control_response(device_id)
            if not response:
                self._apply_optimistic_state(device_id, dict(route.optimistic))
            self.async_set_updated_data(self.device_states)
        return result

    async def async_set_light_param(self, device_id: str, brightness: Optional[int], color_temp_k: Optional[int]) -> bool:
        """一次性下发亮度+色温（合并单条cmd15指令，避免两次请求不同步）"""
        if not self.ssl_client:
            _LOGGER.error("SSL未连接，无法下发灯光复合参数")
            return False
        device = self.devices.get(device_id)
        if not device:
            _LOGGER.error(f"找不到设备 {device_id}")
            return False
        uid = device.get("uid", "")
        return await self.ssl_client.send_light_bri_ct(device_id, uid, brightness, color_temp_k)

    # ------------------------------------------------------------------
    # 空调控制（deviceType=36，cmd=15 set property）
    # ------------------------------------------------------------------
    async def _async_ac_control_raw(self, device_id: str, device_uid: str,
                                    value1=None, value2=None, value3=None, value4=None,
                                    order: str = "set property") -> bool:
        """下发空调原始指令（value1~value4）。

        根据智家365 App 抓包实测（2026-07-17），AC 控制使用以下 order：
          - 开机:     order="on",               value1=0, value2=模式, value3=风速, value4=温度<<16
          - 关机:     order="off",              value1=1, value2=模式, value3=风速, value4=温度<<16
          - 切模式:   order="mode setting",     value1=0, value2=模式
          - 设温度:   order="temperature setting",  value1=0, value2=模式, value3=风速, value4=(temp*100)<<16
          - 设风速:   order="wind setting",     value1=0, value2=模式, value3=风速, value4=温度<<16
        """
        if not self.ssl_client:
            _LOGGER.error("SSL客户端未初始化")
            return False
        if not self.ssl_client.session_key or self.ssl_client.session_key == DEFAULT_KEY.encode("utf-8"):
            _LOGGER.warning("会话密钥无效，无法下发空调指令")
            return False

        from .packet import HomemateJsonData
        from .functions import generate_serial

        serial = generate_serial()
        uni_serial = generate_serial(use_time=True)

        payload = {
            "uid": device_uid,
            "userName": self.username,
            "deviceId": device_id,
            "groupId": "",
            "order": order,
            "value1": value1 if value1 is not None else 0,
            "value2": value2 if value2 is not None else 0,
            "value3": value3 if value3 is not None else 0,
            "value4": value4 if value4 is not None else 0,
            "delayTime": 0,
            "qualityOfService": 1,
            "defaultResponse": 1,
            "propertyResponse": 0,
            "cmd": CMD_CONTROL,
            "serial": serial,
            "clientType": 1,
            "uniSerial": uni_serial,
            "serverRecord": False,
            "ver": SOFTWARE_VER,
            "debugInfo": DEBUG_INFO,
        }

        _LOGGER.debug(f"下发空调控制 {device_id} order={order} v1={value1}, v2={value2}, v3={value3}, v4={value4}")
        await self.ssl_client._send_packet(payload, self.ssl_client.session_key)

        # ★ 等待空调设备响应
        response = await self._wait_for_control_response(device_id)
        if not response:
            # 超时，保留乐观更新兜底
            dev_state = self.get_device_state(device_id)
            if dev_state:
                optimistic_fields = set()
                if value1 is not None:
                    dev_state["state"] = value1 == 0
                    optimistic_fields.add("state")
                if value2 is not None:
                    ac_mode_map = {2: "dehumidify", 3: "cool", 4: "heat", 7: "fan_only"}
                    dev_state["ac_mode"] = ac_mode_map.get(value2, f"unknown({value2})")
                    dev_state["ac_mode_raw"] = value2
                    optimistic_fields.update(("ac_mode", "ac_mode_raw"))
                if value3 is not None:
                    fan_speed_map = {1: "low", 2: "medium", 3: "high"}
                    dev_state["fan_speed"] = fan_speed_map.get(value3, f"unknown({value3})")
                    dev_state["fan_speed_raw"] = value3
                    optimistic_fields.update(("fan_speed", "fan_speed_raw"))
                if value4 is not None:
                    try:
                        temp_raw = value4 >> 16
                        dev_state["temperature"] = round(temp_raw / 100.0, 1) if temp_raw else 0
                    except (TypeError, ValueError):
                        dev_state["temperature"] = value4
                    optimistic_fields.add("temperature")
                self.state_store.mark(
                    device_id,
                    optimistic_fields,
                    StateSource.OPTIMISTIC,
                )
                self.async_set_updated_data(self.device_states)
        else:
            # 有响应就触发 coordinator 更新（SSL 推送会回调 on_status_update）
            dev_state = self.get_device_state(device_id)
            if dev_state:
                self.async_set_updated_data(self.device_states)
        return True

    async def async_set_ac_mode(self, device_id: str, ac_mode: str) -> bool:
        """控制空调模式（cool/dehumidify/heat/fan_only）"""
        device = self.devices.get(device_id)
        if not device:
            return False
        mode_map = {"dehumidify": 2, "cool": 3, "heat": 4, "fan_only": 7}
        mode_value = mode_map.get(ac_mode.lower())
        if mode_value is None:
            _LOGGER.error(f"无效的空调模式: {ac_mode}")
            return False
        # 切模式：order="mode setting", value1=0, value2=模式码
        # 并继承当前温度
        dev_state = self.get_device_state(device_id) or {}
        cur_v4 = dev_state.get("value4", 0)
        if not cur_v4:
            cur_v4 = 2500 << 16
        return await self._async_ac_control_raw(device_id, device.get("uid", ""),
                                                value1=0, value2=mode_value,
                                                value4=cur_v4,
                                                order="mode setting")

    async def async_set_ac_temperature(self, device_id: str, temperature: float) -> bool:
        """控制空调温度（摄氏度）"""
        device = self.devices.get(device_id)
        if not device:
            return False
        
        # 温度设定：order="temperature setting", value2=当前模式, value3=当前风速, value4=(temp*100)<<16
        dev_state = self.get_device_state(device_id) or {}
        cur_v2 = dev_state.get("ac_mode_raw", 3)
        cur_v3 = dev_state.get("fan_speed_raw", 1)
        
        target_temp_scaled = int(temperature * 100)
        value4 = target_temp_scaled << 16
        
        return await self._async_ac_control_raw(device_id, device.get("uid", ""),
                                                value1=0, value2=cur_v2,
                                                value3=cur_v3, value4=value4,
                                                order="temperature setting")

    async def async_set_ac_fan_speed(self, device_id: str, fan_speed: str) -> bool:
        """控制空调风速（low/medium/high）"""
        device = self.devices.get(device_id)
        if not device:
            return False
        speed_map = {"low": 1, "medium": 2, "high": 3}
        speed_value = speed_map.get(fan_speed.lower())
        if speed_value is None:
            _LOGGER.error(f"无效的风速: {fan_speed}")
            return False
        # 风速设定：order="wind setting", value2=当前模式, value3=风速, value4=当前温度<<16
        dev_state = self.get_device_state(device_id) or {}
        cur_v2 = dev_state.get("ac_mode_raw", 3)
        cur_v4 = dev_state.get("value4", 0)
        if not cur_v4:
            cur_v4 = 2500 << 16
        return await self._async_ac_control_raw(device_id, device.get("uid", ""),
                                                value1=0, value2=cur_v2,
                                                value3=speed_value, value4=cur_v4,
                                                order="wind setting")

    # ------------------------------------------------------------------
    # 新风系统控制（deviceType=516, cmd=15 set property）
    # ------------------------------------------------------------------
    async def async_ventilation_state_update(self, device_id: str, value1: int) -> bool:
        """新风系统控制（value1 格式）。
        value1: 0=慢, 50=停, 100=快
        """
        if not self.ssl_client:
            _LOGGER.error("SSL客户端未初始化")
            return False
        device = self.devices.get(device_id)
        if not device:
            _LOGGER.error(f"设备不存在: {device_id}")
            return False
        device_uid = device.get("uid", "")

        result = await self.ssl_client.send_control_ventilation(device_id, device_uid, value1)
        if result:
            response = await self._wait_for_control_response(device_id)
            if not response:
                dev_state = self.device_states.setdefault(device_id, {})
                if value1 == 0:
                    dev_state["fan_speed"] = "慢"
                    dev_state["state"] = True
                elif value1 == 50:
                    dev_state["fan_speed"] = "停"
                    dev_state["state"] = False
                elif value1 == 100:
                    dev_state["fan_speed"] = "快"
                    dev_state["state"] = True
                dev_state["value1"] = value1
                self.state_store.mark(
                    device_id,
                    ("fan_speed", "state", "value1"),
                    StateSource.OPTIMISTIC,
                )
            self.async_set_updated_data(self.device_states)
        return result

    async def async_set_ventilation_preset_mode(self, device_id: str, preset_mode: str) -> bool:
        """设置新风系统预设模式（停/慢/快）"""
        preset_map = {"停": 50, "慢": 0, "快": 100}
        value1 = preset_map.get(preset_mode)
        if value1 is None:
            _LOGGER.error(f"无效的新风模式: {preset_mode}")
            return False
        return await self.async_ventilation_state_update(device_id, value1)

    # ------------------------------------------------------------------
    # 晾衣架控制（cmd=98）
    # ------------------------------------------------------------------
    _CLOTHES_HORSE_FIELD_MAP = {
        "lighting": "lightingCtrl",
        "sterilizing": "sterilizingCtrl",
        "wind_drying": "windDryingCtrl",
        "heat_drying": "heatDryingCtrl",
        "main_switch": "mainSwitchCtrl",
        "motor": "motorCtrl",
    }

    async def async_clothes_horse_control(self, device_id: str, feature: str, value: str) -> bool:
        """晾衣架控制。

        feature: lighting/sterilizing/wind_drying/heat_drying/main_switch/motor
        value: on/off (开关类) 或 up/down/stop (电机类)
        """
        if not self.ssl_client:
            _LOGGER.error("SSL客户端未初始化")
            return False

        device = self.devices.get(device_id)
        if not device:
            _LOGGER.error(f"设备不存在: {device_id}")
            return False

        device_uid = device.get("uid", "")
        ctrl_field = self._CLOTHES_HORSE_FIELD_MAP.get(feature)
        if not ctrl_field:
            _LOGGER.error(f"未知晾衣架功能: {feature}")
            return False

        # 消毒开关特殊判定：只有电机在最顶部（motorPosition=0）时才允许打开
        if feature == "sterilizing" and value == "on":
            dev_state = self.device_states.get(device_id, {})
            motor_position = dev_state.get("position", 0)
            if motor_position != 0:
                _LOGGER.warning(
                    f"[晾衣架] 拒绝消毒开启命令: 电机未在顶部 (motorPosition={motor_position})"
                )
                return False

        result = await self.ssl_client.send_clothes_horse_control(
            device_id=device_id,
            device_uid=device_uid,
            ctrl_field=ctrl_field,
            ctrl_value=value,
        )

        if result:
            # 晾衣架控制返回 cmd=99，不是 cmd=42，所以不等待标准控制响应，保留乐观更新
            dev_state = self.device_states.get(device_id)
            if dev_state:
                if feature == "motor":
                    dev_state["motor_state"] = value
                else:
                    state_key = f"{feature}_state"
                    dev_state[state_key] = (value == "on")
                    if feature == "main_switch":
                        dev_state["state"] = (value == "on")
                optimistic_fields = (
                    ("motor_state",)
                    if feature == "motor"
                    else (f"{feature}_state", "state")
                    if feature == "main_switch"
                    else (f"{feature}_state",)
                )
                self.state_store.mark(
                    device_id,
                    optimistic_fields,
                    StateSource.OPTIMISTIC,
                )
                self.async_set_updated_data(self.device_states)
            _LOGGER.debug(f"[控制成功] {device_id} {ctrl_field}={value}")
        return result

    def get_device(self, device_id: str) -> Optional[Dict[str, Any]]:
        return self.devices.get(device_id)

    def get_device_state(self, device_id: str) -> Optional[Dict[str, Any]]:
        state = self.device_states.get(device_id)
        if state is None:
            return None
        # 检查离线超时：最后更新超过 OFFLINE_TIMEOUT 秒则标记为离线
        last_time = self._last_update_time.get(device_id)
        if last_time is not None:
            elapsed = __import__("time").time() - last_time
            if elapsed > self.OFFLINE_TIMEOUT and state.get("online", False):
                state["online"] = False
        return state

    async def async_cleanup(self):
        if self.ssl_client:
            await self.ssl_client._disconnect()
            _LOGGER.debug("SSL连接已断开清理")
        if self.https_client:
            await self.https_client.close()
            _LOGGER.debug("HTTPS客户端已清理")
