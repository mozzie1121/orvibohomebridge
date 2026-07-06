import logging
import json
import ssl
import asyncio
import aiohttp
from typing import Optional, Any, Dict, List
from .packet import HomemateJsonData
from .const import (
    ID_UNSET,
    HTTP_HEADERS,
    DEVICE_TYPE_MAP,
    CLASS_ID_MAP,
    DEVICE_TYPE_LIGHT,
    DEVICE_TYPE_COVER,
    DEVICE_TYPE_SWITCH,
)

_LOGGER = logging.getLogger(__name__)


class HttpsClient:
    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password

        self.user_id = None
        self.session_id: Optional[str] = None
        self.access_token: Optional[str] = None
        self.family_id: Optional[str] = None
        self.family_name: Optional[str] = None
        self.family_list: List[Dict[str, str]] = []  # 所有家庭列表

        self.session: aiohttp.ClientSession = None
        self.ssl_context: ssl.SSLContext = None

    @property
    def is_logged_in(self) -> bool:
        return self.access_token is not None and self.user_id is not None

    async def _create_ssl_context(self):
        if self.ssl_context:
            return self.ssl_context
        def _sync_create_context():
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            return ssl_context
        self.ssl_context = await asyncio.to_thread(_sync_create_context)
        return self.ssl_context

    async def _connect(self):
        if self.session:
            return

        await self._create_ssl_context()
        connector = aiohttp.TCPConnector(ssl=self.ssl_context)

        self.session = aiohttp.ClientSession(connector=connector)
        _LOGGER.info("HTTPS 会话创建成功")

    async def _disconnect(self):
        if self.session and not self.session.closed:
            await self.session.close()
            self.session = None
            _LOGGER.info("HTTPS 会话关闭")
        self.access_token = None

    def set_session_id(self, session_id: str):
        self.session_id = session_id

    async def _send_request(self, url, data):
        if not self.session:
            raise ConnectionError("客户端未连接")
        if not data:
            resp = await self.session.get(
                url=url,
                headers=HTTP_HEADERS,
                skip_auto_headers=["Accept", "Connection"],
                ssl=False
            )
        else:
            resp = await self.session.post(
                url=url,
                timeout=aiohttp.ClientTimeout(total=10),
                data=data,
                headers=HTTP_HEADERS,
                skip_auto_headers=["Accept", "Connection"],
                ssl=False
            )
        resp.raise_for_status()
        data = await resp.text()
        return json.loads(data)

    async def ensure_login(self) -> bool:
        if not self.session:
            await self._connect()

        if not self.access_token or not self.user_id:
            data = await self._fetch_access_token()
            if data:
                self.access_token = data.get("access_token", "")
                self.user_id = data.get("user_id", "")

        # 获取家庭信息（即使已有family_id，也需要获取family_name和family_list）
        data = await self._fetch_family()
        if data:
            # 如果之前没有family_id，使用获取到的
            if not self.family_id:
                self.family_id = data.get("familyId", "")
            self.family_name = data.get("familyName", "")

        return self.access_token and self.user_id and self.family_id

    async def _fetch_access_token(self) -> dict:
        try:
            if self.session_id is None or self.session_id == bytes(ID_UNSET).decode('utf-8'):
                ret = HomemateJsonData.get_access_token_by_password(self.username, self.password)
            else:
                ret = HomemateJsonData.get_access_token_by_session_id(self.session_id)
            resp = await self._send_request(ret['url'], ret['data'])
            if "message" in resp:
                _LOGGER.error(resp["message"])
                return {}
            if "data" not in resp:
                _LOGGER.error("响应包中未找到[data]")
                return {}
            if "access_token" not in resp["data"]:
                _LOGGER.error("响应包中未找到[access_token]")
                return {}
            _LOGGER.info("HTTPS 申请ACCESS_TOKEN成功")
            return resp["data"]
        except Exception as e:
            _LOGGER.error("HTTPS请求失败: %s", e)
            return {}

    async def _fetch_family(self) -> dict:
        try:
            if not self.user_id or not self.access_token:
                _LOGGER.error("缺少[userId]或[accessToken]")
                return {}
            ret = HomemateJsonData.get_family_statistics_users(self.user_id, self.access_token)
            resp = await self._send_request(ret['url'], ret['data'])
            if "message" in resp:
                _LOGGER.error(resp["message"])
                return {}
            if "data" not in resp:
                _LOGGER.error("响应包中未找到[data]")
                return {}
            data = resp["data"]
            if not isinstance(data, list):
                _LOGGER.error("响应包中[data]不是数组格式")
                return {}
            
            # 保存所有家庭列表
            self.family_list = [
                {"familyId": item.get("familyId", ""), "familyName": item.get("familyName", "未知家庭")}
                for item in data if item.get("familyId")
            ]
            _LOGGER.info(f"获取到 {len(self.family_list)} 个家庭")
            
            # 如果还没有选定家庭，选择第一个
            if not self.family_id and self.family_list:
                first_family = self.family_list[0]
                self.family_id = first_family["familyId"]
                self.family_name = first_family["familyName"]
            
            if not self.family_id:
                _LOGGER.error("响应包中未找到[familyId]")
                return {}
            return {"familyId": self.family_id, "familyName": self.family_name}
        except Exception as e:
            _LOGGER.error("HTTPS 请求失败: %s", e)
            return {}

    def set_family(self, family_id: str):
        """设置当前选定的家庭"""
        self.family_id = family_id
        for family in self.family_list:
            if family["familyId"] == family_id:
                self.family_name = family["familyName"]
                break
        _LOGGER.info(f"切换到家庭: {self.family_name} ({self.family_id})")

    async def fetch_device_status(self) -> Optional[Dict[str, Any]]:
        """通过 /v2/cmd/app/readtable API 获取设备状态列表"""
        try:
            if not await self.ensure_login():
                _LOGGER.error("HTTPS 未登录")
                return None

            ret = HomemateJsonData.get_devices_status(
                access_token=self.access_token,
                session_id=self.session_id or "",
                user_id=self.user_id,
                user_name=self.username,
                family_id=self.family_id
            )
            resp = await self._send_request(ret['url'], ret['data'])

            if "message" in resp:
                _LOGGER.error(resp["message"])
                return None
            if "data" not in resp:
                _LOGGER.error("响应包中未找到[data]")
                return None

            # 解析设备状态数据
            data = resp["data"]
            # 如果 data 是列表，取第一个元素
            if isinstance(data, list) and len(data) > 0:
                data = data[0]

            _LOGGER.info(f"获取到原始设备数据，keys: {list(data.keys()) if isinstance(data, dict) else 'not a dict'}")
            return data
        except Exception as e:
            _LOGGER.error("获取设备状态失败: %s", e)
            return None

    async def fetch_device_desc(self, last_update_time: int = 0) -> Optional[Dict[str, Any]]:
        """通过 getDeviceDesc API 获取设备描述和状态（用于初始化全量状态）"""
        try:
            if not await self.ensure_login():
                _LOGGER.error("HTTPS 未登录")
                return None

            if not self.access_token:
                _LOGGER.error("accessToken 为空")
                return None

            url = f"https://china.orvibo.com/getDeviceDesc?source=ZhiJia365&lastUpdateTime={last_update_time}&accessToken={self.access_token}"
            
            headers = {
                **HTTP_HEADERS,
            }

            _LOGGER.info(f"请求 getDeviceDesc API: {url}")
            
            async with self.session.get(url, headers=headers, ssl=self.ssl_context) as response:
                if response.status != 200:
                    _LOGGER.error(f"getDeviceDesc API 失败: {response.status}")
                    try:
                        error_text = await response.text()
                        _LOGGER.error(f"getDeviceDesc 错误响应: {error_text}")
                    except Exception as e:
                        _LOGGER.error(f"获取错误响应失败: {e}")
                    return None
                
                data = await response.json()
                _LOGGER.info(f"getDeviceDesc API 返回数据类型: {type(data)}")
                if isinstance(data, dict):
                    _LOGGER.info(f"getDeviceDesc 返回键: {list(data.keys())}")
                    if "devices" in data:
                        devices = data.get("devices", [])
                        _LOGGER.info(f"设备数量: {len(devices)}")
                        if len(devices) > 0 and isinstance(devices[0], dict):
                            _LOGGER.info(f"第一个设备键: {list(devices[0].keys())[:15]}")
                    if "status" in data:
                        _LOGGER.info(f"status: {data.get('status')}")
                
                return data
        except Exception as e:
            _LOGGER.error(f"getDeviceDesc API 调用失败: {e}")
            return None

    def parse_device_desc(self, device_desc_data: dict) -> Dict[str, Dict[str, Any]]:
        """解析 getDeviceDesc 返回的数据，提取设备状态"""
        device_status_map = {}
        
        devices = device_desc_data.get("deviceDescList", device_desc_data.get("devices", []))
        if not isinstance(devices, list):
            _LOGGER.warning(f"getDeviceDesc 返回的 deviceDescList 不是列表: {type(devices)}")
            return device_status_map
        
        for device in devices:
            if not isinstance(device, dict):
                continue
            
            device_id = device.get("deviceId", "")
            if not device_id:
                continue
            
            status_info = {}
            
            properties = device.get("properties", {})
            if isinstance(properties, dict):
                onoff_val = properties.get("onoff")
                if onoff_val is not None:
                    if isinstance(onoff_val, dict):
                        status_info["state"] = onoff_val.get("status", "off") == "on"
                    elif isinstance(onoff_val, str):
                        status_info["state"] = onoff_val == "on"
                    elif isinstance(onoff_val, bool):
                        status_info["state"] = onoff_val
                    elif isinstance(onoff_val, int):
                        status_info["state"] = onoff_val == 1
                
                brightness_val = properties.get("brightness")
                if brightness_val is not None:
                    if isinstance(brightness_val, dict):
                        val = brightness_val.get("brightness", brightness_val.get("value"))
                        if val is not None:
                            status_info["brightness"] = int(val)
                    else:
                        if isinstance(brightness_val, (int, float)):
                            status_info["brightness"] = int(brightness_val)
                
                colortemp_val = properties.get("colortemp")
                if colortemp_val is not None:
                    if isinstance(colortemp_val, dict):
                        val = colortemp_val.get("colortemp", colortemp_val.get("value"))
                        if val is not None and val > 0:
                            status_info["color_temp"] = int(val)
                    else:
                        if isinstance(colortemp_val, (int, float)) and colortemp_val > 0:
                            status_info["color_temp"] = int(colortemp_val)
                
                percent_val = properties.get("percent")
                if percent_val is not None:
                    if isinstance(percent_val, dict):
                        val = percent_val.get("percent", percent_val.get("value"))
                        if val is not None:
                            status_info["position"] = int(val)
                    else:
                        if isinstance(percent_val, (int, float)):
                            status_info["position"] = int(percent_val)
                
                temp_val = properties.get("temperature")
                if temp_val is not None:
                    if isinstance(temp_val, dict):
                        val = temp_val.get("value")
                        if val is not None:
                            status_info["temperature"] = float(val)
                    else:
                        if isinstance(temp_val, (int, float)):
                            status_info["temperature"] = float(temp_val)
                
                hum_val = properties.get("humidity")
                if hum_val is not None:
                    if isinstance(hum_val, dict):
                        val = hum_val.get("value")
                        if val is not None:
                            status_info["humidity"] = float(val)
                    else:
                        if isinstance(hum_val, (int, float)):
                            status_info["humidity"] = float(hum_val)
                
                bat_val = properties.get("battery")
                if bat_val is not None:
                    if isinstance(bat_val, dict):
                        val = bat_val.get("power", bat_val.get("value"))
                        if val is not None:
                            status_info["battery"] = int(float(val))
                    else:
                        if isinstance(bat_val, (int, float)):
                            status_info["battery"] = int(float(bat_val))
                
                value3_val = properties.get("value3")
                if value3_val is not None:
                    if isinstance(value3_val, dict):
                        val = value3_val.get("value")
                        if val is not None:
                            status_info["value3"] = int(val)
                    else:
                        if isinstance(value3_val, (int, float)):
                            status_info["value3"] = int(value3_val)
                
                value4_val = properties.get("value4")
                if value4_val is not None:
                    if isinstance(value4_val, dict):
                        val = value4_val.get("value")
                        if val is not None:
                            status_info["value4"] = int(val)
                    else:
                        if isinstance(value4_val, (int, float)):
                            status_info["value4"] = int(value4_val)
            
            online_val = device.get("online")
            if online_val is not None:
                status_info["online"] = self._parse_online_status(online_val)
            
            uid = device.get("uid", "")
            if uid:
                status_info["uid"] = uid
            
            status_id = device.get("statusId", "")
            if status_id:
                status_info["status_id"] = status_id
            
            ext_addr = device.get("extAddr", "")
            if ext_addr:
                status_info["ext_addr"] = ext_addr
            
            _LOGGER.info(f"解析 getDeviceDesc 设备: deviceId={device_id}, state={status_info.get('state', 'N/A')}, online={status_info.get('online', 'N/A')}, brightness={status_info.get('brightness')}, color_temp={status_info.get('color_temp')}, position={status_info.get('position')}")
            
            device_status_map[device_id] = status_info
        
        _LOGGER.info(f"从 getDeviceDesc 解析到 {len(device_status_map)} 个设备状态")
        return device_status_map

    def parse_device_status_list(self, device_status_data: dict) -> List[Dict[str, Any]]:
        """从 /v2/cmd/app/readtable 返回的数据中解析设备列表"""
        devices = []

        # deviceStatus 可能包含设备状态信息，格式可能为 {deviceId: status} 或 list
        raw_device_status = device_status_data.get("deviceStatus", {})
        
        # 打印 deviceStatus 的结构以便调试
        _LOGGER.info(f"deviceStatus 类型: {type(raw_device_status)}")
        if isinstance(raw_device_status, dict):
            _LOGGER.info(f"deviceStatus 键数量: {len(raw_device_status)}")
            # 打印前3个键值对
            count = 0
            for k, v in raw_device_status.items():
                if count >= 3:
                    break
                _LOGGER.info(f"deviceStatus[{k}]: {type(v)}")
                if isinstance(v, dict):
                    _LOGGER.info(f"  keys: {list(v.keys())[:10]}")
                    if "properties" in v:
                        _LOGGER.info(f"  properties: {v['properties']}")
                count += 1
        elif isinstance(raw_device_status, list):
            _LOGGER.info(f"deviceStatus 列表长度: {len(raw_device_status)}")
            if len(raw_device_status) > 0:
                _LOGGER.info(f"第一个元素类型: {type(raw_device_status[0])}")
                if isinstance(raw_device_status[0], dict):
                    _LOGGER.info(f"  keys: {list(raw_device_status[0].keys())[:10]}")
        
        # 建立 deviceStatus 映射
        device_status_map = {}
        if isinstance(raw_device_status, dict):
            device_status_map = raw_device_status
        elif isinstance(raw_device_status, list):
            for status_item in raw_device_status:
                if isinstance(status_item, dict):
                    status_device_id = status_item.get("deviceId", "")
                    if status_device_id:
                        device_status_map[status_device_id] = status_item
        
        _LOGGER.info(f"device_status_map 构建完成，共 {len(device_status_map)} 个设备状态")
        
        # device 包含设备基本信息
        device_info_list = device_status_data.get("device", [])

        # 如果 device 是列表
        if isinstance(device_info_list, list):
            for item in device_info_list:
                # 确保 item 是字典
                if not isinstance(item, dict):
                    _LOGGER.warning(f"跳过无效的设备项: {type(item)}")
                    continue

                device_id = item.get("deviceId", "")
                if not device_id:
                    continue
                
                # 尝试从多个来源获取 deviceType 和 classId
                # 1. 从 item 本身获取
                device_type_raw = item.get("deviceType")
                class_id = item.get("classId")
                
                # 2. 从 deviceStatus 获取（优先）
                status_data = device_status_map.get(device_id, {})
                if not device_type_raw:
                    device_type_raw = status_data.get("deviceType")
                if not class_id:
                    class_id = status_data.get("classId")
                    # classId 可能是 subDeviceType 字段
                    if not class_id:
                        class_id = status_data.get("subDeviceType")
                
                # 3. 从 properties.Descriptor 获取（备用）
                if not class_id:
                    properties = item.get("properties", {})
                    if isinstance(properties, dict):
                        descriptor = properties.get("Descriptor", {})
                        if isinstance(descriptor, dict):
                            class_id = descriptor.get("classId")
                
                _LOGGER.info(f"设备 {device_id}: deviceType={device_type_raw}, classId={class_id}, status_deviceType={status_data.get('deviceType')}")
                
                # 跳过 deviceType=135/136 的父开关设备（物理容器，properties为空，不可控）
                # 只有其子设备(deviceType=102等)才应创建为实体
                if device_type_raw in (135, 136):
                    properties = item.get("properties", {})
                    if isinstance(properties, dict) and len(properties) == 0:
                        _LOGGER.info(f"跳过父开关设备: deviceId={device_id}, deviceType={device_type_raw}, name={item.get('deviceName')}")
                        continue
                
                # 构造临时 item 用于类型判断
                temp_item = {
                    "deviceType": device_type_raw,
                    "classId": class_id,
                }
                
                device_type = self._get_device_type(temp_item)
                if device_type is None:
                    # 如果无法通过 deviceType 或 classId 识别，尝试通过设备名称或其他特征推断
                    device_name = item.get("deviceName", "")
                    properties = item.get("properties", {})
                    
                    # 通过设备名称关键词推断
                    if any(keyword in device_name for keyword in ["灯", "light", "吸顶灯", "平板灯", "主灯", "射灯", "筒灯", "灯泡", "led", "LED"]):
                        device_type = DEVICE_TYPE_LIGHT
                        _LOGGER.info(f"通过名称推断为灯: {device_name}")
                    elif any(keyword in device_name for keyword in ["窗帘", "窗帘机", "遮阳帘", "百叶窗", "卷帘", "curtain", "blind", "shade"]):
                        device_type = DEVICE_TYPE_COVER
                        _LOGGER.info(f"通过名称推断为窗帘: {device_name}")
                    elif any(keyword in device_name for keyword in ["开关", "插座", "switch", "socket", "排插"]):
                        device_type = DEVICE_TYPE_SWITCH
                        _LOGGER.info(f"通过名称推断为开关: {device_name}")
                    elif any(keyword in device_name for keyword in ["晾衣架", "晾衣机", "晾衣"]):
                        device_type = DEVICE_TYPE_COVER
                        _LOGGER.info(f"通过名称推断为晾衣架: {device_name}")
                    elif any(keyword in device_name for keyword in ["紧急按钮", "emergency"]):
                        device_type = DEVICE_TYPE_SENSOR
                        _LOGGER.info(f"通过名称推断为紧急按钮: {device_name}")
                    # 通过 properties 中的特征推断
                    elif isinstance(properties, dict):
                        # 有 percent 属性 -> 窗帘或晾衣架
                        if "percent" in properties:
                            device_type = DEVICE_TYPE_COVER
                            _LOGGER.info(f"通过 percent 属性推断为窗帘/晾衣架: {device_id}")
                        # 有 onoff 属性但无其他属性 -> 开关灯
                        elif "onoff" in properties and not any(k in properties for k in ["brightness", "colortemp", "percent"]):
                            device_type = DEVICE_TYPE_LIGHT
                            _LOGGER.info(f"通过 onoff 属性推断为灯: {device_id}")
                        # 有 brightness 或 colortemp 属性 -> 调光灯
                        elif "brightness" in properties or "colortemp" in properties:
                            device_type = DEVICE_TYPE_LIGHT
                            _LOGGER.info(f"通过 brightness/colortemp 属性推断为灯: {device_id}")
                    
                    if device_type is None:
                        _LOGGER.warning(f"设备类型未识别，跳过: {device_id}, name={device_name}, deviceType={device_type_raw}, classId={class_id}")
                        continue

                uid = item.get("uid", "")
                status_id = item.get("statusId", "")
                gateway_id = item.get("gatewayId", "")
                ext_addr = item.get("extAddr", "")
                
                # 获取设备名称（可能在多个位置）
                device_name = item.get("deviceName", "")
                if not device_name:
                    properties = item.get("properties", {})
                    descriptor = properties.get("Descriptor", {}) if isinstance(properties, dict) else {}
                    device_name = descriptor.get("deviceName", "")
                
                # 获取 ui.model（用于判断灯的类型）
                ui = item.get("ui", {})
                ui_model = ui.get("model", "") if isinstance(ui, dict) else ""
                
                _LOGGER.info(f"解析设备: deviceId={device_id}, uid={uid}, ui.model={ui_model}, name={device_name}, online_raw={item.get('online')}")
                
                # 从 deviceStatus 中获取状态数据（优先）
                status_data = device_status_map.get(device_id, {})
                status_properties = status_data.get("properties", {})

                # 从 item.properties 获取备用状态
                item_properties = item.get("properties", {})
                if isinstance(item_properties, str):
                    item_properties = {}

                # online 状态：优先从 deviceStatus 取（readtable 的 device 列表没有 online 字段）
                online_val = status_data.get("online")
                if online_val is None:
                    online_val = item.get("online")
                online = self._parse_online_status(online_val)

                # 初始状态：优先用 deviceStatus 的 value1~value4（readtable 接口主要格式）
                value1 = status_data.get("value1")
                value2 = status_data.get("value2")
                value3 = status_data.get("value3")
                sub_type_raw = item.get("subDeviceType") or status_data.get("subDeviceType")

                initial_state = False
                initial_position = None
                initial_brightness = None
                initial_color_temp = None

                # 根据 deviceType 推断 value1~value3 的语义
                if device_type_raw in (1, 102):
                    if value1 is not None:
                        v1 = int(value1)
                        initial_state = v1 == 0
                elif device_type_raw == 501:
                    pass
                elif device_type_raw in (135, 136, 137, 143, 518):
                    if value1 is not None:
                        v1 = int(value1)
                        initial_state = v1 == 1
                elif device_type_raw in (34, 52):
                    if value1 is not None:
                        v1 = int(value1)
                        initial_position = v1
                        initial_state = v1 > 0
                elif device_type_raw in (38, 503):
                    if value1 is not None:
                        initial_state = int(value1) == 0
                    if value2 is not None and int(value2) >= 0:
                        initial_brightness = int(value2)
                    if value3 is not None and int(value3) > 0:
                        ct = int(value3)
                        if 150 <= ct <= 400:
                            ct = 1000000 // ct
                        initial_color_temp = ct

                # 兜底：从 properties 取（SSL 推送或其他接口可能返回 properties 格式）
                onoff_value = None
                if "onoff" in item_properties:
                    onoff_value = item_properties["onoff"]
                elif "onoff" in status_properties:
                    onoff_value = status_properties["onoff"]

                if onoff_value is not None:
                    if isinstance(onoff_value, dict):
                        initial_state = onoff_value.get("status", "off") == "on"
                    elif isinstance(onoff_value, str):
                        initial_state = onoff_value == "on"
                    elif isinstance(onoff_value, bool):
                        initial_state = onoff_value
                    elif isinstance(onoff_value, int):
                        initial_state = onoff_value == 1

                if not initial_state and isinstance(item_properties.get("onoff_status"), str):
                    initial_state = item_properties["onoff_status"] == "on"
                if not initial_state and isinstance(status_properties.get("onoff_status"), str):
                    initial_state = status_properties["onoff_status"] == "on"

                if initial_position is None:
                    if "percent" in item_properties:
                        percent_val = item_properties["percent"]
                        initial_position = int(percent_val) if isinstance(percent_val, (int, float)) else 0
                    elif "percent" in status_properties:
                        percent_val = status_properties["percent"]
                        initial_position = int(percent_val) if isinstance(percent_val, (int, float)) else 0
                    else:
                        initial_position = 0

                if initial_brightness is None:
                    brightness_val = None
                    if "brightness" in item_properties:
                        brightness_val = item_properties["brightness"]
                    elif "brightness" in status_properties:
                        brightness_val = status_properties["brightness"]
                    if brightness_val is not None:
                        if isinstance(brightness_val, dict):
                            initial_brightness = brightness_val.get("brightness", brightness_val.get("value"))
                        else:
                            initial_brightness = int(brightness_val) if isinstance(brightness_val, (int, float)) else None

                if initial_color_temp is None:
                    colortemp_val = None
                    if "colortemp" in item_properties:
                        colortemp_val = item_properties["colortemp"]
                    elif "colortemp" in status_properties:
                        colortemp_val = status_properties["colortemp"]
                    if colortemp_val is not None:
                        if isinstance(colortemp_val, dict):
                            ct = colortemp_val.get("colortemp", colortemp_val.get("value"))
                            if ct and ct > 0:
                                initial_color_temp = int(ct)
                        else:
                            if isinstance(colortemp_val, (int, float)) and colortemp_val > 0:
                                initial_color_temp = int(colortemp_val)

                _LOGGER.info(f"设备 {device_id} 初始状态: online={online}, state={initial_state}, brightness={initial_brightness}, color_temp={initial_color_temp}, position={initial_position}")

                devices.append({
                    "device_id": device_id,
                    "device_name": device_name,
                    "device_type": device_type,
                    "device_type_raw": device_type_raw,
                    "class_id": class_id,
                    "sub_device_type": sub_type_raw,
                    "uid": uid,
                    "status_id": status_id,
                    "gateway_id": gateway_id,
                    "ext_addr": ext_addr,
                    "model": item.get("model", ""),
                    "ui_model": ui_model,
                    "room_id": item.get("roomId", ""),
                    "room_name": item.get("roomName", ""),
                    "online": online,
                    "state": initial_state,
                    "position": initial_position,
                    "brightness": initial_brightness,
                    "color_temp": initial_color_temp,
                    "properties": item_properties,
                    "endpoint": item.get("endpoint", 0),
                    "status": status_data,
                })
        # 如果 device 是字典
        elif isinstance(device_info_list, dict):
            for device_id, item in device_info_list.items():
                if not device_id:
                    continue
                if isinstance(item, str):
                    continue

                device_type = self._get_device_type(item)
                if device_type is None:
                    continue

                devices.append({
                    "device_id": device_id,
                    "device_name": item.get("deviceName", ""),
                    "device_type": device_type,
                    "device_type_raw": item.get("deviceType"),
                    "class_id": item.get("classId"),
                    "uid": item.get("uid", ""),
                    "model": item.get("model", ""),
                    "room_id": item.get("roomId", ""),
                    "room_name": item.get("roomName", ""),
                    "online": self._parse_online_status(item.get("online")),
                    "properties": item.get("properties", {}),
                    "endpoint": item.get("endpoint", 0),
                    "status": device_status.get(device_id, {}) if isinstance(device_status, dict) else {},
                })

        _LOGGER.info(f"从 device_status 解析到 {len(devices)} 个设备")
        return devices

    async def fetch_homepage_data(self) -> Optional[Dict[str, Any]]:
        try:
            if not await self.ensure_login():
                _LOGGER.error("HTTPS 未登录")
                return None

            ret = HomemateJsonData.get_homepage_data(self.family_id, self.user_id, self.access_token)
            resp = await self._send_request(ret['url'], ret['data'])

            if "message" in resp:
                _LOGGER.error(resp["message"])
                return None
            if "data" not in resp:
                _LOGGER.error("响应包中未找到[data]")
                return None

            return resp["data"]
        except Exception as e:
            _LOGGER.error("获取主页数据失败：%s", e)
            return None

    def parse_device_list(self, homepage_data: dict) -> List[Dict[str, Any]]:
        devices = []
        # 尝试多个可能的字段名
        device_data = homepage_data.get("deviceList", []) or homepage_data.get("device", []) or []
        if not isinstance(device_data, list):
            device_data = []

        for item in device_data:
            device_id = item.get("deviceId", "")
            if not device_id:
                continue

            device_type = self._get_device_type(item)
            if device_type is None:
                _LOGGER.debug(f"未识别的设备类型: deviceType={item.get('deviceType')}, classId={item.get('classId')}")
                continue

            devices.append({
                "device_id": device_id,
                "device_name": item.get("deviceName", ""),
                "device_type": device_type,
                "device_type_raw": item.get("deviceType"),
                "class_id": item.get("classId"),
                "uid": item.get("uid", ""),
                "model": item.get("model", ""),
                "room_id": item.get("roomId", ""),
                "room_name": item.get("roomName", ""),
                "online": self._parse_online_status(item.get("online")),
                "properties": item.get("properties", {}),
                "endpoint": item.get("endpoint", 0),
            })

        _LOGGER.info(f"解析到 {len(devices)} 个设备")
        return devices

    def _get_device_type(self, item: dict) -> Optional[str]:
        device_type_raw = item.get("deviceType")
        class_id = item.get("classId")

        if class_id in CLASS_ID_MAP:
            return CLASS_ID_MAP[class_id]

        if device_type_raw in DEVICE_TYPE_MAP:
            return DEVICE_TYPE_MAP[device_type_raw]

        return None

    def _parse_online_status(self, online_value) -> bool:
        """解析 online 状态，支持多种格式"""
        if online_value is None:
            return False
        if isinstance(online_value, bool):
            return online_value
        if isinstance(online_value, int):
            return online_value == 1
        if isinstance(online_value, str):
            value = online_value.strip().lower()
            return value in ("online", "1", "true", "yes")
        return False