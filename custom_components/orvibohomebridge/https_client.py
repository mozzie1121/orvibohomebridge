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
    DEVICE_TYPE_SENSOR,
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
        if self.session:
            try:
                await self.session.close()
            except Exception:
                pass
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

    async def switch_host(self, host: str) -> None:
        """切换云端主机（中国区/国际区数据独立分区），清空登录态以便重新认证"""
        from .packet import set_api_host
        set_api_host(host)
        self.access_token = None
        self.user_id = None
        self.session_id = None
        self.family_id = None
        self.family_name = None
        self.family_list = []
        _LOGGER.warning(f"已切换云端主机为 {host}，将重新登录")

    async def _readtable(self, device_flag: int) -> Optional[Dict[str, Any]]:
        """发送一次 readtable 请求并返回合并后的数据字典"""
        ret = HomemateJsonData.get_devices_status(
            access_token=self.access_token,
            session_id=self.session_id or "",
            user_id=self.user_id,
            user_name=self.username,
            family_id=self.family_id,
            device_flag=device_flag
        )
        resp = await self._send_request(ret['url'], ret['data'])

        if "message" in resp:
            _LOGGER.error(f"readtable(deviceFlag={device_flag}) 失败: {resp['message']}")
            return None
        if "data" not in resp:
            _LOGGER.error(f"readtable(deviceFlag={device_flag}) 响应包中未找到[data]")
            return None

        data = resp["data"]
        # data 可能是列表：服务器按表分块返回，必须合并全部块
        if isinstance(data, list):
            _LOGGER.info(f"readtable(deviceFlag={device_flag}) 返回 {len(data)} 个数据块")
            merged: Dict[str, Any] = {}
            for element in data:
                if not isinstance(element, dict):
                    continue
                for key, value in element.items():
                    if key not in merged:
                        merged[key] = value
                    elif isinstance(merged[key], list) and isinstance(value, list):
                        merged[key] = merged[key] + value
                    elif isinstance(merged[key], dict) and isinstance(value, dict):
                        merged[key] = {**merged[key], **value}
            data = merged

        _LOGGER.info(f"readtable(deviceFlag={device_flag}) 数据keys: "
                     f"{list(data.keys()) if isinstance(data, dict) else 'not a dict'}")
        return data if isinstance(data, dict) else None

    async def fetch_device_status(self) -> Optional[Dict[str, Any]]:
        """通过 /v2/cmd/app/readtable API 获取设备状态列表

        deviceFlag=0 可能只返回账户级表(account/userGatewayBind/gateway)，
        此时用 deviceFlag=1 再次请求设备级表(device/deviceStatus)并合并。
        """
        try:
            if not await self.ensure_login():
                _LOGGER.error("HTTPS 未登录")
                return None

            data = await self._readtable(device_flag=0)
            if data is None:
                return None

            if "device" not in data:
                _LOGGER.warning("readtable(deviceFlag=0) 未返回 device 表，尝试 deviceFlag=1 ...")
                data_flag1 = await self._readtable(device_flag=1)
                if data_flag1:
                    data = {**data, **data_flag1}

            if "device" not in data:
                _LOGGER.warning(
                    f"readtable 响应中始终缺少 device 表: gateway表={data.get('gateway')}, "
                    f"userGatewayBind表={data.get('userGatewayBind')}"
                )
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

            from .packet import get_api_host
            url = f"https://{get_api_host()}/getDeviceDesc?source=ZhiJia365&lastUpdateTime={last_update_time}&accessToken={self.access_token}"
            
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
        """从 /v2/cmd/app/readtable 返回的数据中解析设备列表

        注：内部解析逻辑已迁移到 protocol.py，此处为保持接口兼容的包装。
        device_status_data 是 fetch_device_status() 返回的 resp["data"]（readtable 的内层 data）。
        """
        from .protocol import parse_readtable_devices, device_to_dict, _safe_int

        raw_devices = device_status_data.get("device", [])
        if isinstance(raw_devices, dict):
            # dict 格式：key=deviceId, value=item
            result = []
            for device_id, item in raw_devices.items():
                if not isinstance(item, dict):
                    continue
                device_type_raw = _safe_int(item.get("deviceType"))
                result.append({
                    "device_id": device_id,
                    "device_name": item.get("deviceName", ""),
                    "device_type": self._get_device_type(item) or "light",
                    "device_type_raw": device_type_raw,
                    "class_id": _safe_int(item.get("classId")),
                    "uid": item.get("uid", ""),
                    "model": item.get("model", ""),
                    "room_id": item.get("roomId", ""),
                    "room_name": item.get("roomName", ""),
                    "online": self._parse_online_status(item.get("online")),
                    "properties": item.get("properties", {}),
                    "endpoint": item.get("endpoint", 0),
                    "status": item.get("status", {}),
                })
            _LOGGER.info(f"从 device_status dict 解析到 {len(result)} 个设备")
            return result

        # 列表格式 → 使用 protocol.py 解析
        # 注意：device_status_data 已经是 resp["data"]，不能再套一层 payload.get("data")
        # 所以直接构造一个模拟 payload 传给 parse_readtable_devices
        wrapped = {"code": 0, "data": device_status_data}
        devices = [device_to_dict(d) for d in parse_readtable_devices(wrapped)]
        
        # protocol.parse_readtable_devices 已经做了 room/status 联合查询
        # 但仍需补充 properties（从 deviceStatus 获取额外的 properties）
        raw_statuses = device_status_data.get("deviceStatus", [])
        status_map: dict[str, dict] = {}
        if isinstance(raw_statuses, list):
            for s in raw_statuses:
                if isinstance(s, dict):
                    sid = s.get("deviceId", "")
                    if sid:
                        status_map[sid] = s
        elif isinstance(raw_statuses, dict):
            status_map = raw_statuses
        
        result = []
        for dev in devices:
            status_data = status_map.get(dev["device_id"], {})
            dev["status"] = status_data
            dev["properties"] = dev.get("properties") or status_data.get("properties", {})
            result.append(dev)
        
        # 跳过父开关设备（type=135/136，properties 为空）
        filtered = []
        for dev in result:
            dt = dev.get("device_type_raw")
            if dt in (135, 136):
                props = dev.get("properties", {})
                if isinstance(props, dict) and len(props) == 0:
                    _LOGGER.info(f"跳过父开关设备: deviceId={dev['device_id']}, name={dev.get('device_name')}")
                    continue
            filtered.append(dev)
        
        _LOGGER.info(f"从 device_status 解析到 {len(filtered)} 个设备")
        return filtered

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

        try:
            device_type_raw = int(device_type_raw) if device_type_raw is not None else None
            class_id = int(class_id) if class_id is not None else None
        except (TypeError, ValueError):
            device_type_raw = None
            class_id = None

        if class_id in CLASS_ID_MAP:
            return CLASS_ID_MAP[class_id]

        if device_type_raw in DEVICE_TYPE_MAP:
            return DEVICE_TYPE_MAP[device_type_raw]

        return None

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
            _LOGGER.error("获取主页数据失败: %s", e)
            return None

    def parse_device_list(self, homepage_data: dict) -> List[Dict[str, Any]]:
        devices = []
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

        _LOGGER.info(f"从家庭主页解析到 {len(devices)} 个设备")
        return devices

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