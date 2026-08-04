import logging
import asyncio
import secrets
from typing import Dict, Any, Optional
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .ssl_client import SSLClient
from .https_client import HttpsClient
from .device_types import (
    DeviceCategory,
    classify_device,
)
from .redact import fingerprint, redact_packet
from .models import AccountCredentials
from .cloud import CHINA_CLOUD, CloudEndpoint, cloud_candidates
from .state_store import StateSource, StateStore
from .parsers import get_state_parser
from .lock_manager import LockEventManager
from .status_dispatcher import StatusUpdateDispatcher
from .lock_media_manager import LockMediaManager
from .temp_password_manager import TempPasswordManager
from .device_inventory import DeviceInventory
from .control_executor import ControlExecutor
from .const import (
    SSL_PORT,
    UPDATE_INTERVAL,
    CMD_CONTROL,
    DEFAULT_KEY,
    SOFTWARE_VER, DEBUG_INFO,
)

_LOGGER = logging.getLogger(__name__)


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
        self.inventory = DeviceInventory(
            self.https_client,
            self.devices,
            self.device_states,
            self.state_store,
            self.lock_events.remove,
        )
        self.control = ControlExecutor(
            self.devices,
            self.device_states,
            self.state_store,
            lambda: self.ssl_client,
            lambda: self,
            self.get_device_state,
            lambda: self.async_set_updated_data(self.device_states),
        )
        self.lock_media = LockMediaManager(
            self.hass, self.devices, self._redaction_salt
        )
        self.temp_passwords = TempPasswordManager(
            self.hass,
            self.devices,
            self.device_states,
            self.https_client,
            lambda: self.ssl_client,
            lambda: self.async_set_updated_data(self.device_states),
            self._redaction_salt,
        )
        self.status_dispatcher = StatusUpdateDispatcher(
            self.devices,
            self.device_states,
            self.state_store,
            self._last_update_time,
            self._cmd42_log,
            on_motion=self._apply_motion_state_parser,
            on_emergency=self._apply_emergency_state_parser,
            on_lock_transient=lambda state, raw, device_id: self._apply_lock_transient_event(
                device_id, state, raw
            ),
            on_lock_message=self._publish_lock_message,
            on_lock_event=self._publish_lock_event,
            on_updated=lambda: self.hass.add_job(
                self.async_set_updated_data, self.device_states
            ),
            device_label=lambda device_id: fingerprint(
                device_id, self._redaction_salt
            ),
        )

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
            data.update(self.lock_media.attach_urls(device_id, raw_status, data))
        self.hass.bus.async_fire(LOCK_EVENT, data)
        _LOGGER.debug(
            "[门锁事件总线] device=%s locked=%s door=%s kind=%s",
            fingerprint(device_id, self._redaction_salt),
            data.get("locked"),
            data.get("door_open"),
            data.get("kind"),
        )

    def register_lock_camera(self, device_id: str, camera: Any) -> None:
        """Compatibility facade for the camera platform."""

        self.lock_media.register_camera(device_id, camera)

    async def async_fetch_video(
        self,
        device_id: str,
        object_key: str,
        device_uid: str = "",
    ) -> Dict[str, Any]:
        """Compatibility facade for the fetch-video service."""

        return await self.lock_media.fetch_video(
            device_id, object_key, device_uid
        )

    async def async_list_events(
        self,
        device_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, str]]:
        return await self.lock_media.list_events(device_id, limit)

    def start_history_cleanup(self) -> None:
        self.lock_media.start_cleanup()

    def stop_history_cleanup(self) -> None:
        self.lock_media.stop_cleanup()

    async def async_cleanup_history(
        self,
        keep_days: int = 7,
        device_id: str = "",
        max_entries: Optional[int] = None,
    ) -> int:
        return await self.lock_media.cleanup_history(
            keep_days, device_id, max_entries
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
        """下发临时密码并返回归一化结果。"""
        return await self.temp_passwords.grant(
            device_id,
            auth_type,
            minutes,
            number,
            name,
            phone,
            start_time,
            end_time,
            device_uid,
        )

    async def async_revoke_temp_password(
        self,
        device_id: str,
        authorized_id: int,
        device_uid: str = "",
    ) -> Dict[str, Any]:
        """删除临时密码（cmd=247）。"""
        return await self.temp_passwords.revoke(
            device_id, authorized_id, device_uid
        )

    async def async_list_temp_passwords(self, device_id: str = "") -> Dict[str, Any]:
        """列出服务器端全部临时密码（readtable authorizedUnlock，含过期状态）。"""
        return await self.temp_passwords.list(device_id)

    async def async_fetch_server_temp_passwords(self) -> list[dict]:
        """从 readtable（REST 全量同步）拉取 authorizedUnlock 表。"""
        return await self.temp_passwords.fetch_server_records()

    def temp_password_state(self, device_id: str) -> Optional[dict]:
        """给传感器用：返回最近一条有效临时密码的展示信息。"""
        return self.temp_passwords.state(device_id)

    def start_temp_password_cleanup(self) -> None:
        """启动临时密码自动回收：每 6 小时清理过期/次数用尽的密码。"""
        self.temp_passwords.start_cleanup()

    def stop_temp_password_cleanup(self) -> None:
        """取消临时密码回收定时任务（集成卸载时调用）。"""
        self.temp_passwords.stop_cleanup()

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
        event.update(self.lock_media.attach_urls(device_id, raw_status, event))
        self.hass.bus.async_fire(LOCK_EVENT, event)
        _LOGGER.debug(
            "[门锁消息] device=%s kind=%s is_alarm=%s text=%s",
            fingerprint(device_id, self._redaction_salt),
            event.get("kind"),
            event.get("is_alarm"),
            event.get("text"),
        )

    async def _discover_devices(self):
        """拉取并解析设备列表：readtable → getDeviceDesc → queryHomepageData 三层回退"""
        return await self.inventory.discover()

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

            self.inventory.initialize(devices)

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
                self.inventory.merge_cloud(devices)
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

        # SSL 控制通道跟随当前客户端的云端区域，不依赖模块级全局状态。
        ssl_host = self.https_client.cloud.ssl_host
        self.ssl_client = SSLClient(
            hass=self.hass,
            ssl_host=ssl_host,
            ssl_port=SSL_PORT,
            username=self.username,
            password_hash=self.password_hash,
            family_id=self.https_client.family_id,
            on_status_update=self.status_dispatcher.dispatch,
            on_session_id_obtained=on_session_id_obtained,
        )
        self.lock_media.configure(
            self.ssl_client,
            self.https_client.user_id or "",
            self.https_client.family_id or "",
        )

    async def _prewarm_cos_credentials(self) -> None:
        await self.lock_media.prewarm()

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
        return await self.control.wait_for_response(device_id)

    def _apply_optimistic_state(
        self,
        device_id: str,
        values: Dict[str, Any],
    ) -> None:
        """Apply a local command fallback until SSL/cloud confirms it."""
        self.control.apply_optimistic(device_id, values)

    async def async_turn_on(self, device_id: str, brightness: int = None, color_temp: int = None) -> bool:
        """打开设备（基于 category 路由控制命令）。

        可选参数 brightness/color_temp 用于灯光一次性下发。
        """
        return await self.control.turn_on(
            device_id, brightness, color_temp
        )

    async def async_turn_off(self, device_id: str) -> bool:
        """关闭设备（基于 category 路由控制命令）。"""
        return await self.control.turn_off(device_id)

    async def async_set_cover_position(self, device_id: str, position: int) -> bool:
        return await self.control.set_cover_position(device_id, position)

    async def async_stop_cover(self, device_id: str) -> bool:
        """停止窗帘电机。"""
        return await self.control.stop_cover(device_id)

    async def async_dream_curtain_action(self, device_id: str, action: str) -> bool:
        return await self.control.dream_curtain_action(device_id, action)

    async def async_set_dream_curtain_angle(self, device_id: str, angle: int) -> bool:
        return await self.control.set_dream_curtain_angle(device_id, angle)

    async def async_set_floor_heating_temperature(self, device_id: str, temperature: int) -> bool:
        return await self.control.set_floor_heating_temperature(device_id, temperature)

    async def async_set_brightness(self, device_id: str, brightness: int) -> bool:
        """设置亮度（HA LightEntity 使用 0-255 范围）。"""
        return await self.control.set_brightness(device_id, brightness)

    async def async_set_color_temp(self, device_id: str, color_temp_k: int) -> bool:
        """单独设置色温（Kelvin）"""
        return await self.control.set_color_temp(device_id, color_temp_k)

    async def async_set_light_param(self, device_id: str, brightness: Optional[int], color_temp_k: Optional[int]) -> bool:
        """一次性下发亮度+色温（合并单条cmd15指令，避免两次请求不同步）"""
        return await self.control.set_light_param(
            device_id, brightness, color_temp_k
        )

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
        return await self.control.ventilation_state_update(device_id, value1)

    async def async_set_ventilation_preset_mode(self, device_id: str, preset_mode: str) -> bool:
        """设置新风系统预设模式（停/慢/快）"""
        return await self.control.set_ventilation_preset_mode(
            device_id, preset_mode
        )

    # ------------------------------------------------------------------
    # 晾衣架控制（cmd=98）
    # ------------------------------------------------------------------
    async def async_clothes_horse_control(self, device_id: str, feature: str, value: str) -> bool:
        """晾衣架控制。

        feature: lighting/sterilizing/wind_drying/heat_drying/main_switch/motor
        value: on/off (开关类) 或 up/down/stop (电机类)
        """
        return await self.control.clothes_horse_control(
            device_id, feature, value
        )

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
