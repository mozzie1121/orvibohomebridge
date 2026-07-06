import logging
import asyncio
from typing import Dict, Any, Optional
from datetime import timedelta
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from .ssl_client import SSLClient
from .https_client import HttpsClient
from .device_types import DeviceCategory, classify_device, is_hidden_category
from .const import (
    SSL_HOST, SSL_PORT,
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


class OrviboMeshCoordinator(DataUpdateCoordinator[Dict[str, Any]]):
    MOTION_RESET_DELAY = 30  # 人体传感器触发后恢复延时（秒）
    
    def __init__(self, hass: HomeAssistant, username: str, password: str, family_id: str = None):
        self.username = username
        self.password = password
        self.family_id = family_id
        self.hass = hass

        self.https_client = HttpsClient(username=username, password=password)
        self.ssl_client = None
        
        self._motion_reset_tasks: Dict[str, asyncio.Task] = {}  # 人体传感器重置任务

        super().__init__(
            hass,
            _LOGGER,
            name="Orvibo Mesh Coordinator",
            update_interval=UPDATE_INTERVAL,
        )

        self.devices: Dict[str, Any] = {}
        self.device_states: Dict[str, Any] = {}

    def _parse_status_light_colortemp(self, dev_state: dict, raw_status: dict) -> None:
        """解析调光调色灯状态 (deviceType 38)
        核心控制参数: value1开关、value2亮度、value3色温
        """
        props = raw_status.get("properties", {})

        bri = raw_status.get("value2")
        if bri is None:
            bri = props.get("brightness")
        if bri is not None:
            bri = int(bri)
        dev_state["brightness"] = bri

        ct = raw_status.get("value3")
        if ct is not None:
            ct = int(ct)
            if 150 <= ct <= 400:
                ct = 1000000 // ct
        else:
            ct = props.get("colortemp")
            if ct is not None:
                ct = int(ct)
        if ct is not None:
            if ct < 2700:
                ct = 2700
            elif ct > 6500:
                ct = 6500
        dev_state["color_temp"] = ct

        value1 = raw_status.get("value1")
        if value1 is not None:
            value1 = int(value1)
            sub_device_type = raw_status.get("subDeviceType", 0)
            if isinstance(sub_device_type, str):
                sub_device_type = int(sub_device_type)
            if sub_device_type == -2:
                dev_state["state"] = value1 == 0
            else:
                dev_state["state"] = value1 == 1
        else:
            onoff_obj = props.get("onoff", {})
            if onoff_obj and isinstance(onoff_obj, dict) and onoff_obj.get("status"):
                dev_state["state"] = onoff_obj.get("status") == "on"
            elif bri is not None:
                dev_state["state"] = bri > 0
            elif "state" not in dev_state:
                dev_state["state"] = False

        _LOGGER.info(f"[调光调色灯] state={dev_state['state']}, brightness={bri}, color_temp={ct}")

    def _parse_status_light(self, dev_state: dict, raw_status: dict) -> None:
        """解析单色普通灯状态 (deviceType 102, 501)
        核心控制参数: value1开关 (value1==0 为开)
        """
        props = raw_status.get("properties", {})
        parsed = False
        onoff_obj = props.get("onoff", {})
        if onoff_obj and isinstance(onoff_obj, dict) and onoff_obj.get("status"):
            dev_state["state"] = onoff_obj.get("status") == "on"
            parsed = True
        elif isinstance(props.get("onoff_status"), str):
            dev_state["state"] = props["onoff_status"] == "on"
            parsed = True
        if not parsed:
            value1 = raw_status.get("value1")
            if value1 is not None:
                value1 = int(value1)
                dev_state["state"] = value1 == 0
            elif "state" not in dev_state:
                dev_state["state"] = False
        _LOGGER.info(f"[单色灯] state={dev_state['state']}")

    def _parse_status_cct_light_strip(self, dev_state: dict, raw_status: dict) -> None:
        """解析色温灯带状态 (deviceType 503)
        使用 properties.onoff.status / brightness.percent / colorTemp.value
        """
        props = raw_status.get("properties", {})

        onoff_obj = props.get("onoff", {})
        if onoff_obj and isinstance(onoff_obj, dict) and onoff_obj.get("status"):
            dev_state["state"] = onoff_obj.get("status") == "on"
        else:
            if "state" not in dev_state:
                dev_state["state"] = False

        bri_obj = props.get("brightness", {})
        if isinstance(bri_obj, dict):
            bri = bri_obj.get("percent")
        else:
            bri = bri_obj
        if bri is not None:
            bri = int(bri)
            if bri < 0:
                bri = 0
            elif bri > 100:
                bri = 100
            dev_state["brightness"] = bri
            if bri == 0:
                dev_state["state"] = False

        ct_obj = props.get("colorTemp", {})
        if isinstance(ct_obj, dict):
            ct = ct_obj.get("value")
        else:
            ct = ct_obj
        if ct is not None:
            ct = int(ct)
            if ct < 2000:
                ct = 2000
            elif ct > 6500:
                ct = 6500
            dev_state["color_temp"] = ct

        _LOGGER.info(f"[色温灯带] state={dev_state.get('state')}, brightness={dev_state.get('brightness')}, color_temp={dev_state.get('color_temp')}")

    def _parse_status_dimmable_light(self, dev_state: dict, raw_status: dict) -> None:
        """解析可调光灯状态 (deviceType 502)
        使用 properties.onoff.status / properties.brightness.percent
        """
        props = raw_status.get("properties", {})

        onoff_obj = props.get("onoff", {})
        if onoff_obj and isinstance(onoff_obj, dict) and onoff_obj.get("status"):
            dev_state["state"] = onoff_obj.get("status") == "on"
        else:
            if "state" not in dev_state:
                dev_state["state"] = False

        bri_obj = props.get("brightness", {})
        if isinstance(bri_obj, dict):
            bri = bri_obj.get("percent")
        else:
            bri = bri_obj
        if bri is not None:
            bri = int(bri)
            if bri < 0:
                bri = 0
            elif bri > 100:
                bri = 100
            dev_state["brightness"] = bri
            if bri == 0:
                dev_state["state"] = False

        _LOGGER.info(f"[可调光灯] state={dev_state.get('state')}, brightness={dev_state.get('brightness')}")

    def _parse_status_temp_humidity_sensor(self, dev_state: dict, raw_status: dict) -> None:
        """解析温湿度传感器状态 (deviceType 300, subType=491)
        使用 properties.temperature.value / properties.humidity.value / properties.battery.power
        """
        props = raw_status.get("properties", {})

        temp_obj = props.get("temperature", {})
        if isinstance(temp_obj, dict):
            temp = temp_obj.get("value")
        else:
            temp = temp_obj
        if temp is not None:
            try:
                dev_state["temperature"] = float(temp)
            except (TypeError, ValueError):
                dev_state["temperature"] = temp

        hum_obj = props.get("humidity", {})
        if isinstance(hum_obj, dict):
            hum = hum_obj.get("value")
        else:
            hum = hum_obj
        if hum is not None:
            try:
                dev_state["humidity"] = float(hum)
            except (TypeError, ValueError):
                dev_state["humidity"] = hum

        bat_obj = props.get("battery", {})
        if isinstance(bat_obj, dict):
            bat = bat_obj.get("power") or bat_obj.get("value")
        else:
            bat = bat_obj
        if bat is not None:
            try:
                dev_state["battery"] = int(float(bat))
            except (TypeError, ValueError):
                dev_state["battery"] = bat

        dev_state["state"] = True
        _LOGGER.info(f"[温湿度传感器] temp={dev_state.get('temperature')}, humidity={dev_state.get('humidity')}, battery={dev_state.get('battery')}")

    def _parse_status_door_window_sensor(self, dev_state: dict, raw_status: dict) -> None:
        """解析门窗传感器状态 (deviceType 46)
        value1=0为开门, value1=1为关门; value3=1始终为1(传感器激活标志); value4为电量百分比
        """
        value1 = raw_status.get("value1")
        value4 = raw_status.get("value4")

        if value1 is not None:
            try:
                value1 = int(value1)
                dev_state["door_state"] = value1 == 1
            except (TypeError, ValueError):
                dev_state["door_state"] = False

        if value4 is not None:
            try:
                dev_state["battery"] = int(value4)
            except (TypeError, ValueError):
                dev_state["battery"] = value4

        dev_state["state"] = True
        _LOGGER.info(f"[门窗传感器] door_state={'OPEN' if dev_state.get('door_state') else 'CLOSED'}, battery={dev_state.get('battery')}%")

    def _parse_status_motion_sensor(self, dev_state: dict, raw_status: dict) -> None:
        """解析人体传感器状态 (deviceType 26)
        value3=1为检测到人体, value3=0为无检测; value4为电量百分比
        人体传感器只发送触发信号, 需要软件实现延时恢复(默认30秒)
        """
        value3 = raw_status.get("value3")
        value4 = raw_status.get("value4")
        device_id = raw_status.get("deviceId", "")

        if value3 is not None:
            try:
                value3 = int(value3)
                if value3 == 1:
                    dev_state["motion_detected"] = True
                    asyncio.create_task(self._schedule_motion_reset(device_id))
                else:
                    dev_state["motion_detected"] = False
                    self._cancel_motion_reset(device_id)
            except (TypeError, ValueError):
                dev_state["motion_detected"] = False

        if value4 is not None:
            try:
                dev_state["battery"] = int(value4)
            except (TypeError, ValueError):
                dev_state["battery"] = value4

        dev_state["state"] = True
        _LOGGER.info(f"[人体传感器] motion_detected={dev_state.get('motion_detected')}, battery={dev_state.get('battery')}%")

    async def _schedule_motion_reset(self, device_id: str) -> None:
        """安排人体传感器状态重置"""
        self._cancel_motion_reset(device_id)
        
        async def reset_motion():
            await asyncio.sleep(self.MOTION_RESET_DELAY)
            state = self.device_states.get(device_id)
            if state and state.get("motion_detected"):
                state["motion_detected"] = False
                _LOGGER.info(f"[人体传感器] {device_id[:12]}... 延时{self.MOTION_RESET_DELAY}秒后恢复为未触发")
                self.async_update_listeners()
        
        self._motion_reset_tasks[device_id] = asyncio.create_task(reset_motion())

    def _cancel_motion_reset(self, device_id: str) -> None:
        """取消人体传感器状态重置任务"""
        task = self._motion_reset_tasks.pop(device_id, None)
        if task and not task.done():
            task.cancel()

    def _parse_status_smoke_sensor(self, dev_state: dict, raw_status: dict) -> None:
        """解析烟雾传感器状态 (deviceType 27)
        value1=1为检测到烟雾, value1=0为正常; value3=1始终为1(传感器激活标志); value4为电量百分比
        """
        value1 = raw_status.get("value1")
        value4 = raw_status.get("value4")

        if value1 is not None:
            try:
                value1 = int(value1)
                dev_state["smoke_detected"] = value1 == 1
            except (TypeError, ValueError):
                dev_state["smoke_detected"] = False

        if value4 is not None:
            try:
                dev_state["battery"] = int(value4)
            except (TypeError, ValueError):
                dev_state["battery"] = value4

        dev_state["state"] = True
        _LOGGER.info(f"[烟雾传感器] smoke_detected={dev_state.get('smoke_detected')}, battery={dev_state.get('battery')}%")

    def _parse_status_fan_coil_ac(self, dev_state: dict, raw_status: dict) -> None:
        """解析风机盘管空调状态 (deviceType 36)
        value1=0为开/1为关; value2模式(2除湿/3制冷/4制热/7送风); value3风速(1低/2中/3高); value4温度*10000000
        """
        value1 = raw_status.get("value1")
        value2 = raw_status.get("value2")
        value3 = raw_status.get("value3")
        value4 = raw_status.get("value4")

        if value1 is not None:
            value1 = int(value1)
            dev_state["state"] = value1 == 0

        if value2 is not None:
            value2 = int(value2)
            ac_mode_map = {2: "dehumidify", 3: "cool", 4: "heat", 7: "fan_only"}
            dev_state["ac_mode"] = ac_mode_map.get(value2, f"unknown({value2})")
            dev_state["ac_mode_raw"] = value2

        if value3 is not None:
            value3 = int(value3)
            fan_speed_map = {1: "low", 2: "medium", 3: "high"}
            dev_state["fan_speed"] = fan_speed_map.get(value3, f"unknown({value3})")
            dev_state["fan_speed_raw"] = value3

        if value4 is not None:
            value4 = int(value4)
            try:
                temp_celsius = value4 / 10000000.0
                dev_state["temperature"] = round(temp_celsius, 1)
            except (TypeError, ValueError):
                dev_state["temperature"] = value4

        _LOGGER.info(f"[空调] state={dev_state.get('state')}, mode={dev_state.get('ac_mode')}, fan_speed={dev_state.get('fan_speed')}, temperature={dev_state.get('temperature')}")
    
    def _parse_status_curtain(self, dev_state: dict, raw_status: dict) -> None:
        """解析百分比窗帘状态 (deviceType 34)
        核心控制参数: value1 (0-100) 开度
        """
        props = raw_status.get("properties", {})
        
        # 窗帘位置 (value1 或 properties.percent)
        position = raw_status.get("value1")
        if position is None:
            position = props.get("percent")
        if position is not None:
            position = int(position)
        dev_state["position"] = position
        
        # 开关状态基于位置判断：100为全开，0为全关
        if position is not None:
            if position == 100:
                dev_state["state"] = True
            elif position == 0:
                dev_state["state"] = False
            else:
                # 部分打开时，保持当前状态
                dev_state["state"] = dev_state.get("state", False)
        else:
            dev_state["state"] = False
        
        _LOGGER.info(f"[窗帘] state={dev_state['state']}, position={position}")
    
    def _parse_status_switch(self, dev_state: dict, raw_status: dict) -> None:
        """解析开关状态 (deviceType 135, 136)
        核心控制参数: 回路通道区分
        """
        props = raw_status.get("properties", {})
        
        # 开关状态（properties.onoff 或 value1）
        onoff_obj = props.get("onoff", {})
        if onoff_obj and isinstance(onoff_obj, dict) and onoff_obj.get("status"):
            dev_state["state"] = onoff_obj.get("status") == "on"
        else:
            value1 = raw_status.get("value1")
            sub_device_type = raw_status.get("subDeviceType", 0)
            if value1 is not None:
                value1 = int(value1)
                sub_device_type = int(sub_device_type)
                # subDeviceType=-2 时反转
                if sub_device_type == -2:
                    dev_state["state"] = value1 == 0
                else:
                    dev_state["state"] = value1 == 1
            else:
                dev_state["state"] = False
        
        _LOGGER.info(f"[开关] state={dev_state['state']}")
    
    def _parse_status_door_lock(self, dev_state: dict, raw_status: dict) -> None:
        """解析智能门锁状态 (deviceType 522, classId 463)
        状态字段：lockState(锁状态), doorState(门状态), battery(电量)
        电池类型：batteryManager(干电池), batteryManager1(锂电池)
        """
        props = raw_status.get("properties", {})
        door_lock = props.get("doorLock", {})
        dry_battery = props.get("batteryManager", {})
        lithium_battery = props.get("batteryManager1", {})
        
        if isinstance(door_lock, dict):
            dev_state["lock_state"] = door_lock.get("lockState") == "on"
            dev_state["door_state"] = door_lock.get("doorState") == "on"
            dev_state["inside_lock_state"] = door_lock.get("insideLockState") == "on"
        
        if isinstance(dry_battery, dict):
            dev_state["dry_battery_level"] = dry_battery.get("level")
            dev_state["dry_battery_setup"] = dry_battery.get("isSetupBattery") == "on"
        
        if isinstance(lithium_battery, dict):
            dev_state["lithium_battery_level"] = lithium_battery.get("level")
            dev_state["lithium_battery_setup"] = lithium_battery.get("isSetupBattery") == "on"
        
        dev_state["state"] = dev_state.get("lock_state", False)
        _LOGGER.info(f"[智能门锁] lock_state={dev_state.get('lock_state')}, door_state={dev_state.get('door_state')}, dry_battery={dev_state.get('dry_battery_level')}%, lithium_battery={dev_state.get('lithium_battery_level')}%")

    def _parse_doorbell_event(self, dev_state: dict, raw_status: dict) -> None:
        """解析门铃和开锁事件 (cmd=352)
        门铃事件格式: {"event":{"server":"doorbell","name":"ring","value":{"url":"..."}}}
        开锁事件格式: {"event":{"server":"doorLock","name":"unlockEvent","value":{"type":"fingerprint","userId":1}}}
        门铃接听: {"event":{"server":"doorbell","name":"answered","value":{"uid":"..."}}}
        门铃挂断: {"event":{"server":"doorbell","name":"bye","value":{"uid":"..."}}}
        """
        event = raw_status.get("event", {})
        server = event.get("server")
        name = event.get("name")
        value = event.get("value", {})
        
        if server == "doorbell" and name == "ring":
            dev_state["doorbell_ring"] = True
            dev_state["doorbell_url"] = value.get("url")
            dev_state["doorbell_ip"] = value.get("doorbell_local_Ip")
            _LOGGER.info(f"[门铃事件] deviceId={raw_status.get('deviceId')}, url={value.get('url')}, ip={value.get('doorbell_local_Ip')}")
            import asyncio
            asyncio.create_task(self._schedule_doorbell_reset(raw_status.get("deviceId", "")))
        
        elif server == "doorbell" and name == "answered":
            dev_state["doorbell_answered"] = True
            _LOGGER.info(f"[门铃接听] deviceId={raw_status.get('deviceId')}, uid={value.get('uid')}")
        
        elif server == "doorbell" and name == "bye":
            dev_state["doorbell_answered"] = False
            _LOGGER.info(f"[门铃挂断] deviceId={raw_status.get('deviceId')}")
        
        elif server == "doorLock" and name == "unlockEvent":
            dev_state["unlock_event"] = True
            dev_state["unlock_type"] = value.get("type")
            dev_state["unlock_user_id"] = value.get("userId")
            _LOGGER.info(f"[开锁事件] deviceId={raw_status.get('deviceId')}, type={value.get('type')}, userId={value.get('userId')}")
            import asyncio
            asyncio.create_task(self._schedule_unlock_reset(raw_status.get("deviceId", "")))

    async def _schedule_doorbell_reset(self, device_id: str) -> None:
        """安排门铃状态重置（5秒后恢复为未触发）"""
        await asyncio.sleep(5)
        state = self.device_states.get(device_id)
        if state and state.get("doorbell_ring"):
            state["doorbell_ring"] = False
            _LOGGER.info(f"[门铃事件] {device_id[:12]}... 延时5秒后恢复")
            self.async_set_updated_data(self.device_states)

    async def _schedule_unlock_reset(self, device_id: str) -> None:
        """安排开锁事件状态重置（5秒后恢复为未触发）"""
        await asyncio.sleep(5)
        state = self.device_states.get(device_id)
        if state and state.get("unlock_event"):
            state["unlock_event"] = False
            _LOGGER.info(f"[开锁事件] {device_id[:12]}... 延时5秒后恢复")
            self.async_set_updated_data(self.device_states)

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
        
        _LOGGER.info(f"[通用设备] state={dev_state['state']}")

    def _parse_clothes_horse_state(self, dev_state: dict, raw_status: dict) -> None:
        """解析晾衣架 cmd=99 状态推送。"""
        dev_state["motor_state"] = raw_status.get("motor_state", "stop")
        dev_state["position"] = raw_status.get("motor_position", 0)
        dev_state["lighting_state"] = raw_status.get("lighting_state", "off") == "on"
        dev_state["heat_drying_state"] = raw_status.get("heat_drying_state", "off") == "on"
        dev_state["wind_drying_state"] = raw_status.get("wind_drying_state", "off") == "on"
        dev_state["sterilizing_state"] = raw_status.get("sterilizing_state", "off") == "on"
        dev_state["main_switch_state"] = raw_status.get("main_switch_state", "off") == "on"
        # state 字段用主开关状态兜底（兼容通用实体）
        dev_state["state"] = dev_state["main_switch_state"]
        _LOGGER.info(
            f"[晾衣架] motor={dev_state['motor_state']}, pos={dev_state['position']}, "
            f"lighting={dev_state['lighting_state']}, heat={dev_state['heat_drying_state']}, "
            f"wind={dev_state['wind_drying_state']}, steril={dev_state['sterilizing_state']}, "
            f"main={dev_state['main_switch_state']}"
        )

    async def _async_setup(self):
        try:
            if self.family_id:
                self.https_client.family_id = self.family_id
                self.https_client.family_name = None

            if not await self.https_client.ensure_login():
                raise UpdateFailed("HTTPS登录失败")

            _LOGGER.info("第一步：拉取设备列表...")
            device_status_data = await self.https_client.fetch_device_status()
            if not device_status_data:
                raise UpdateFailed("获取设备列表失败")

            devices = self.https_client.parse_device_status_list(device_status_data)
            if not devices:
                raise UpdateFailed("未解析到任何设备")

            for device in devices:
                device_id = device["device_id"]

                # 过滤隐藏类别设备（MIXPAD_GATEWAY/MIX_SWITCH/BACH_SWITCH/WIFI_CAMERA/SMART_REMOTE/MIXPAD_4WAY_BASE）
                category = classify_device(device)
                if is_hidden_category(category):
                    _LOGGER.info(f"[过滤] 跳过隐藏类别设备: {device_id} category={category.name}")
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

            _LOGGER.info(f"设备列表拉取完成，共 {len(self.devices)} 个设备")

            _LOGGER.info("第二步：通过 getDeviceDesc API 拉取全量设备状态...")
            device_desc_data = await self.https_client.fetch_device_desc(last_update_time=0)

            if device_desc_data:
                device_desc_status_map = self.https_client.parse_device_desc(device_desc_data)

                if device_desc_status_map:
                    _LOGGER.info(f"getDeviceDesc 解析到 {len(device_desc_status_map)} 个设备状态，开始更新...")

                    for device_id, status_info in device_desc_status_map.items():
                        matched_device_id = None

                        if device_id in self.device_states:
                            matched_device_id = device_id
                        elif device_id.startswith("w-"):
                            stripped_id = device_id[2:]
                            if stripped_id in self.device_states:
                                matched_device_id = stripped_id
                            else:
                                for stored_id in self.device_states:
                                    if stored_id.startswith("w-") and stored_id[2:] == device_id:
                                        matched_device_id = stored_id
                                        break
                        else:
                            for stored_id in self.device_states:
                                if stored_id.startswith("w-") and stored_id[2:] == device_id:
                                    matched_device_id = stored_id
                                    break

                        if matched_device_id:
                            old_state = self.device_states[matched_device_id]
                            new_state = {**old_state, **status_info}
                            self.device_states[matched_device_id] = new_state
                        else:
                            _LOGGER.info(f"getDeviceDesc 中的设备 {device_id} 未匹配到设备列表中的设备")
                else:
                    _LOGGER.warning("getDeviceDesc 未解析到任何设备状态")
            else:
                _LOGGER.warning("getDeviceDesc API 未返回数据")

            await self._init_ssl_client()

            if self.ssl_client:
                await self.ssl_client.connect_and_login()
                await self._query_clothes_horse_initial_status()

            _LOGGER.info(f"初始化完成，共 {len(self.devices)} 个设备")
            for device_id, dev in self.devices.items():
                state = self.device_states.get(device_id, {})
                category = classify_device(dev)
                
                if category == DeviceCategory.TEMP_HUMIDITY_SENSOR:
                    self._parse_status_temp_humidity_sensor(state, {"properties": state.get("properties", {}), "value3": state.get("value3"), "value4": state.get("value4")})
                elif category == DeviceCategory.DOOR_WINDOW_SENSOR:
                    self._parse_status_door_window_sensor(state, {"value3": state.get("value3"), "value4": state.get("value4")})
                elif category == DeviceCategory.MOTION_SENSOR:
                    self._parse_status_motion_sensor(state, {"value3": state.get("value3"), "value4": state.get("value4")})
                elif category == DeviceCategory.SMOKE_SENSOR:
                    self._parse_status_smoke_sensor(state, {"value3": state.get("value3"), "value4": state.get("value4")})
                elif category == DeviceCategory.DOOR_LOCK:
                    self._parse_status_door_lock(state, {"properties": state.get("properties", {})})
                
                _LOGGER.info(
                    f"  设备: name={dev.get('device_name')}, device_id={device_id}, "
                    f"deviceType={dev.get('device_type_raw')}, uid={dev.get('uid')}, "
                    f"online={state.get('online')}, state={state.get('state')}"
                )

            self.async_set_updated_data(self.device_states)
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
                    status = device.get("status", {})
                    if status:
                        self.device_states[device_id].update(status)
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
            _LOGGER.info(f"收到MQTT状态更新: deviceId={device_id}, raw_status={raw_status}")
            
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
                _LOGGER.warning(f"MQTT推送设备 {device_id} 未匹配本地设备")
                return

            dev_state = self.device_states[matched_device_id]
            dev_state["properties"] = raw_status.get("properties", {})
            dev_state["online"] = True

            # 获取设备信息，根据 deviceType / category 调用对应的解析方法
            device_info = self.devices.get(matched_device_id)
            device_type = device_info.get("device_type_raw", 0) if device_info else 0
            sub_type = device_info.get("sub_device_type") if device_info else None
            category = classify_device(device_info) if device_info else DeviceCategory.UNKNOWN

            _LOGGER.info(f"[设备类型] deviceType={device_type}, category={category.name}, deviceId={matched_device_id}")

            # 晾衣架专用协议（cmd=99 推送，带 is_clothes_horse 标志）
            if raw_status.get("is_clothes_horse"):
                self._parse_clothes_horse_state(dev_state, raw_status)
            elif raw_status.get("cmd") == 352:
                self._parse_doorbell_event(dev_state, raw_status)
            elif device_type == 38:
                self._parse_status_light_colortemp(dev_state, raw_status)
            elif device_type == 502:
                self._parse_status_dimmable_light(dev_state, raw_status)
            elif device_type == 503:
                self._parse_status_cct_light_strip(dev_state, raw_status)
            elif device_type == 300 and sub_type == 491:
                self._parse_status_temp_humidity_sensor(dev_state, raw_status)
            elif device_type == 46:
                self._parse_status_door_window_sensor(dev_state, raw_status)
            elif device_type == 26:
                self._parse_status_motion_sensor(dev_state, raw_status)
            elif device_type == 27:
                self._parse_status_smoke_sensor(dev_state, raw_status)
            elif device_type == 522:
                self._parse_status_door_lock(dev_state, raw_status)
            elif device_type in (102, 501):
                self._parse_status_light(dev_state, raw_status)
            elif device_type == 34:
                self._parse_status_curtain(dev_state, raw_status)
            elif device_type == 36:
                self._parse_status_fan_coil_ac(dev_state, raw_status)
            elif device_type in (135, 136):
                self._parse_status_switch(dev_state, raw_status)
            else:
                # 用 category 兜底路由
                if category in (DeviceCategory.SIMPLE_ZIGBEE_LIGHT, DeviceCategory.MONO_LIGHT, DeviceCategory.LIGHT_VIRTUAL_GROUP):
                    self._parse_status_light(dev_state, raw_status)
                elif category == DeviceCategory.ZIGBEE_CURTAIN:
                    self._parse_status_curtain(dev_state, raw_status)
                elif category == DeviceCategory.CCT_LIGHT_STRIP:
                    self._parse_status_cct_light_strip(dev_state, raw_status)
                elif category == DeviceCategory.FAN_COIL_AC:
                    self._parse_status_fan_coil_ac(dev_state, raw_status)
                elif category == DeviceCategory.DIMMABLE_LIGHT:
                    self._parse_status_dimmable_light(dev_state, raw_status)
                elif category == DeviceCategory.TEMP_HUMIDITY_SENSOR:
                    self._parse_status_temp_humidity_sensor(dev_state, raw_status)
                elif category == DeviceCategory.DOOR_WINDOW_SENSOR:
                    self._parse_status_door_window_sensor(dev_state, raw_status)
                elif category == DeviceCategory.MOTION_SENSOR:
                    self._parse_status_motion_sensor(dev_state, raw_status)
                elif category == DeviceCategory.SMOKE_SENSOR:
                    self._parse_status_smoke_sensor(dev_state, raw_status)
                else:
                    self._parse_status_generic(dev_state, raw_status)

            # 通知HA刷新实体状态
            self.hass.async_add_job(self.async_set_updated_data, self.device_states)
            _LOGGER.info(f"[{matched_device_id}] MQTT状态同步完成: state={dev_state.get('state')}, bri={dev_state.get('brightness')}, ct={dev_state.get('color_temp')}, pos={dev_state.get('position')}")

        self.ssl_client = SSLClient(
            hass=self.hass,
            ssl_host=SSL_HOST,
            ssl_port=SSL_PORT,
            username=self.username,
            password=self.password,
            family_id=self.https_client.family_id,
            on_status_update=on_status_update,
            on_session_id_obtained=on_session_id_obtained,
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
                _LOGGER.info(f"[晾衣架初始查询] 已下发 cmd=100 device={device_id}")
            except Exception as e:
                _LOGGER.warning(f"[晾衣架初始查询] {device_id} 失败: {e}")

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

        device_uid = device.get("uid", "")
        category = classify_device(device)
        _LOGGER.info(f"打开设备: {device_id}, category={category.name}, uid={device_uid}")

        result = False
        if category == DeviceCategory.DIM_COLOR_LIGHT:
            dev_state = self.get_device_state(device_id) or {}
            cur_bri = brightness if brightness is not None else dev_state.get("brightness", 0) or 0
            cur_ct = color_temp if color_temp is not None else dev_state.get("color_temp", 0) or 0
            if cur_bri == 0:
                cur_bri = 255
            if cur_ct == 0:
                cur_ct = 2700
            _LOGGER.info(f"[DIM_COLOR_LIGHT] 开/调节: bri={cur_bri}, ct={cur_ct}K")
            result = await self.ssl_client.send_control_light_colortemp(device_id, device_uid, cur_ct, brightness=cur_bri)
        elif category in (DeviceCategory.MONO_LIGHT, DeviceCategory.DIMMABLE_LIGHT):
            # type=501/502 使用 set property 格式
            result = await self.ssl_client.send_control_switch(device_id, device_uid, True)
        elif category in (DeviceCategory.SIMPLE_ZIGBEE_LIGHT, DeviceCategory.LEGACY_LIGHT,
                          DeviceCategory.CCT_LIGHT_STRIP, DeviceCategory.LIGHT_VIRTUAL_GROUP):
            # type=1/102/503/10086 使用 order=on/off + value1
            result = await self.ssl_client.send_control_light(device_id, device_uid, True)
        elif category in (DeviceCategory.ZIGBEE_CURTAIN, DeviceCategory.LEGACY_CURTAIN):
            result = await self.ssl_client.send_control_cover(device_id, device_uid, 100)
        elif category == DeviceCategory.FAN_COIL_AC:
            result = await self._async_ac_control_raw(device_id, device_uid, value1=0)
        elif category == DeviceCategory.CLOTHES_HORSE:
            result = await self.async_clothes_horse_control(device_id, "main_switch", "on")
        else:
            # 兜底用 light 控制
            result = await self.ssl_client.send_control_light(device_id, device_uid, True)

        if result:
            self.device_states.setdefault(device_id, {})["state"] = True
            if brightness is not None:
                self.device_states[device_id]["brightness"] = brightness
            if color_temp is not None:
                self.device_states[device_id]["color_temp"] = color_temp
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

        device_uid = device.get("uid", "")
        category = classify_device(device)
        _LOGGER.info(f"关闭设备: {device_id}, category={category.name}, uid={device_uid}")

        result = False
        if category == DeviceCategory.DIM_COLOR_LIGHT:
            _LOGGER.info(f"[DIM_COLOR_LIGHT] 关闭: order=off")
            result = await self.ssl_client.send_control_light(device_id, device_uid, False)
        elif category in (DeviceCategory.MONO_LIGHT, DeviceCategory.DIMMABLE_LIGHT):
            result = await self.ssl_client.send_control_switch(device_id, device_uid, False)
        elif category in (DeviceCategory.SIMPLE_ZIGBEE_LIGHT, DeviceCategory.LEGACY_LIGHT,
                          DeviceCategory.CCT_LIGHT_STRIP, DeviceCategory.LIGHT_VIRTUAL_GROUP):
            result = await self.ssl_client.send_control_light(device_id, device_uid, False)
        elif category in (DeviceCategory.ZIGBEE_CURTAIN, DeviceCategory.LEGACY_CURTAIN):
            result = await self.ssl_client.send_control_cover(device_id, device_uid, 0)
        elif category == DeviceCategory.FAN_COIL_AC:
            result = await self._async_ac_control_raw(device_id, device_uid, value1=1)
        elif category == DeviceCategory.CLOTHES_HORSE:
            result = await self.async_clothes_horse_control(device_id, "main_switch", "off")
        else:
            result = await self.ssl_client.send_control_light(device_id, device_uid, False)

        if result:
            self.device_states.setdefault(device_id, {})["state"] = False
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
        device_uid = device.get("uid", "")
        _LOGGER.info(f"设置窗帘位置: {device_id} position={position}")
        result = await self.ssl_client.send_control_cover(device_id, device_uid, position)
        if result:
            self.device_states.setdefault(device_id, {})["position"] = position
            self.device_states[device_id]["state"] = position > 0
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
        device_uid = device.get("uid", "")
        _LOGGER.info(f"停止窗帘: {device_id}")
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
        uid = device.get("uid", "")
        category = classify_device(device)

        if category == DeviceCategory.DIMMABLE_LIGHT:
            brightness_percent = round(brightness)
            _LOGGER.info(f"设置可调光灯亮度 {device_id} HA={brightness} → {brightness_percent}%")
            result = await self.ssl_client.send_control_dimmable_light_brightness(device_id, uid, brightness_percent)
            if result:
                self.device_states.setdefault(device_id, {})["brightness"] = brightness_percent
                self.device_states[device_id]["state"] = True
                self.async_set_updated_data(self.device_states)
            return result

        color_temp_k = self.device_states.get(device_id, {}).get("color_temp", 0) or 0
        if color_temp_k == 0:
            color_temp_k = 2700

        device_type_raw = device.get("device_type_raw")
        if device_type_raw in (503,):
            brightness_255 = round(brightness * 255 / 100)
            _LOGGER.info(f"下发色温灯带亮度 {device_id} 百分比={brightness} → 0-255={brightness_255}, color_temp={color_temp_k}K")
            result = await self.ssl_client.send_control_light_colortemp(device_id, uid, color_temp_k, brightness=brightness_255)
        else:
            _LOGGER.info(f"下发亮度 {device_id} bri={brightness} color_temp={color_temp_k}K (fast color temperature)")
            result = await self.ssl_client.send_control_light_colortemp(device_id, uid, color_temp_k, brightness=brightness)
        if result:
            self.device_states.setdefault(device_id, {})["brightness"] = brightness
            self.device_states[device_id]["state"] = True
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
        uid = device.get("uid", "")
        brightness = self.device_states.get(device_id, {}).get("brightness", 255)

        device_type_raw = device.get("device_type_raw")
        if device_type_raw in (503,):
            brightness_255 = round(brightness * 255 / 100)
            _LOGGER.info(f"设置色温 {color_temp_k}K, brightness(百分比)={brightness} → 0-255={brightness_255}")
            result = await self.ssl_client.send_control_light_colortemp(device_id, uid, color_temp_k, brightness=brightness_255)
        else:
            _LOGGER.info(f"设置色温 {color_temp_k}K, brightness={brightness}")
            result = await self.ssl_client.send_control_light_colortemp(device_id, uid, color_temp_k, brightness=brightness)
        if result:
            self.device_states.setdefault(device_id, {})["color_temp"] = color_temp_k
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
                                    value1=None, value2=None, value3=None, value4=None) -> bool:
        """下发空调原始指令（value1~value4）。"""
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
            "order": "set property",
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

        _LOGGER.info(f"下发空调控制 {device_id} value1={value1}, value2={value2}, value3={value3}, value4={value4}")
        await self.ssl_client._send_packet(payload, self.ssl_client.session_key)

        dev_state = self.get_device_state(device_id)
        if dev_state:
            if value1 is not None:
                dev_state["state"] = value1 == 0
            if value2 is not None:
                ac_mode_map = {2: "dehumidify", 3: "cool", 4: "heat", 7: "fan_only"}
                dev_state["ac_mode"] = ac_mode_map.get(value2, f"unknown({value2})")
                dev_state["ac_mode_raw"] = value2
            if value3 is not None:
                fan_speed_map = {1: "low", 2: "medium", 3: "high"}
                dev_state["fan_speed"] = fan_speed_map.get(value3, f"unknown({value3})")
                dev_state["fan_speed_raw"] = value3
            if value4 is not None:
                try:
                    dev_state["temperature"] = round(value4 / 10000000.0, 1)
                except (TypeError, ValueError):
                    dev_state["temperature"] = value4
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
        return await self._async_ac_control_raw(device_id, device.get("uid", ""), value2=mode_value)

    async def async_set_ac_temperature(self, device_id: str, temperature: float) -> bool:
        """控制空调温度（摄氏度）"""
        device = self.devices.get(device_id)
        if not device:
            return False
        value4 = int(temperature * 10000000)
        return await self._async_ac_control_raw(device_id, device.get("uid", ""), value4=value4)

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
        return await self._async_ac_control_raw(device_id, device.get("uid", ""), value3=speed_value)

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
            dev_state = self.device_states.get(device_id)
            if dev_state:
                if feature == "motor":
                    dev_state["motor_state"] = value
                else:
                    state_key = f"{feature}_state"
                    dev_state[state_key] = (value == "on")
                    if feature == "main_switch":
                        dev_state["state"] = (value == "on")
                self.async_set_updated_data(self.device_states)
            _LOGGER.info(f"[控制成功] {device_id} {ctrl_field}={value}")
        return result

    def get_device(self, device_id: str) -> Optional[Dict[str, Any]]:
        return self.devices.get(device_id)

    def get_device_state(self, device_id: str) -> Optional[Dict[str, Any]]:
        return self.device_states.get(device_id)

    async def async_cleanup(self):
        if self.ssl_client:
            await self.ssl_client._disconnect()
            _LOGGER.info("SSL连接已断开清理")
        if self.https_client and hasattr(self.https_client, "session"):
            await self.https_client._disconnect()
            _LOGGER.info("HTTPS会话已清理")