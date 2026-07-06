import os
import ssl
import hashlib
import logging
import asyncio
from pathlib import Path
from datetime import datetime
from typing import Optional, Callable
from homeassistant.core import HomeAssistant
from .packet import HomematePacket, HomemateJsonData

from .const import (
    SSL_HOST, SSL_PORT, CLIENT_CERT, CLIENT_KEY, SERVER_CA, ID_UNSET, DEFAULT_KEY,
    SSL_MAX_RECONNECT_ATTEMPTS,
    CMD_HELLO, CMD_LOGIN, CMD_STATE_UPDATE, CMD_CONTROL, CMD_HEARTBEAT, CMD_HANDSHAKE,
    CMD_CLOTHES_HORSE_CONTROL, CMD_CLOTHES_HORSE_STATE, CMD_CLOTHES_HORSE_QUERY,
)

_LOGGER = logging.getLogger(__name__)


class SSLClient:
    _initial_keys = {}

    def __init__(
        self,
        hass: HomeAssistant,
        ssl_host: str,
        ssl_port: int,
        username: str,
        password: str,
        family_id: str,
        on_session_id_obtained: Callable[[str], None],
        on_status_update: Callable[[str, dict], None],
        heartbeat_interval: int = 30,
        retry_interval: int = 5
    ):
        self.hass = hass
        self.ssl_host = ssl_host
        self.ssl_port = ssl_port
        self.username = username
        self.password = password
        self.family_id = family_id

        self.on_session_id_obtained = on_session_id_obtained
        self.on_status_update = on_status_update
        self.heartbeat_interval = heartbeat_interval
        self.retry_interval = retry_interval

        BASE_DIR = Path(__file__).parent.resolve()
        self.certfile = BASE_DIR / CLIENT_CERT
        self.keyfile = BASE_DIR / CLIENT_KEY
        self.cafile = BASE_DIR / SERVER_CA

        self.ssl_context = None
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.session_id: Optional[str] = None
        self.session_key: Optional[bytes] = None
        self.connected: bool = False
        self._listening_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None

    @classmethod
    def add_key(cls, session_id: str, key: bytes):
        cls._initial_keys[session_id] = key

    @classmethod
    def get_key(cls, session_id: str) -> bytes:
        try:
            return cls._initial_keys[session_id]
        except KeyError:
            return DEFAULT_KEY.encode("utf-8")

    @property
    def is_connected(self):
        return self.connected

    async def _create_ssl_context(self):
        def _sync_create_context():
            try:
                if not os.path.exists(self.certfile):
                    raise FileNotFoundError(f"找不到证书文件: {self.certfile}")
                if not os.path.exists(self.keyfile):
                    raise FileNotFoundError(f"找不到密钥文件: {self.keyfile}")
                if not os.path.exists(self.cafile):
                    raise FileNotFoundError(f"找不到CA证书文件: {self.cafile}")
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.load_cert_chain(certfile=self.certfile, keyfile=self.keyfile)
                context.load_verify_locations(cafile=self.cafile)
                context.check_hostname = True
                context.verify_mode = ssl.CERT_REQUIRED
                return context
            except Exception as e:
                _LOGGER.error(f"创建SSL上下文失败: {str(e)}")
                raise

        return await self.hass.async_add_executor_job(_sync_create_context)

    async def _connect(self):
        if self.connected:
            return True
        try:
            if not self.ssl_context:
                self.ssl_context = await self._create_ssl_context()
            _LOGGER.debug("SSL正在连接...")
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(
                    host=self.ssl_host,
                    port=self.ssl_port,
                    ssl=self.ssl_context,
                    server_hostname=self.ssl_host
                ),
                timeout=10.0
            )
            self.connected = True
            _LOGGER.info("SSL连接成功")
            return True
        except asyncio.TimeoutError:
            _LOGGER.error("SSL连接服务器 [%s:%s] 超时", SSL_HOST, SSL_PORT)
            return False
        except OSError as e:
            _LOGGER.error("SSL连接发生IO错误: %s", e)
            return False
        except Exception as e:
            _LOGGER.error("SSL连接失败: %s", e)
            return False

    async def _disconnect(self):
        if self._listening_task and not self._listening_task.done():
            self._listening_task.cancel()
            try:
                await self._listening_task
            except asyncio.CancelledError:
                pass

        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        if self.writer and not self.writer.is_closing():
            _LOGGER.info("SSL正在断开已有连接...")
            self.writer.close()
            try:
                await asyncio.wait_for(self.writer.wait_closed(), timeout=2.0)
            except asyncio.TimeoutError:
                _LOGGER.debug("关闭SSL连接超时")
            except Exception as e:
                _LOGGER.debug("关闭SSL连接失败: %s", e)

        self.reader = None
        self.writer = None
        self.session_id = None
        self.session_key = None
        self.connected = False
        _LOGGER.info("SSL连接已断开")

    async def _reconnect(self):
        try:
            await self._disconnect()
        except Exception as e:
            _LOGGER.error("断开连接异常: %s", e)

        if self.retry_interval > 0:
            _LOGGER.info(f"{self.retry_interval}秒后尝试重连...")
            await asyncio.sleep(self.retry_interval)
            await self.connect_and_login()

    async def connect_and_login(self):
        if self.connected:
            return True
        for retry in range(SSL_MAX_RECONNECT_ATTEMPTS):
            try:
                _LOGGER.info("SSL正在连接和登录...")
                self.connected = await self._connect()
                if self.connected:
                    _LOGGER.info("SSL连接成功，发送Hello...")
                    await self._send_hello()
                    _LOGGER.info("创建后台监听任务...")
                    self._listening_task = self.hass.async_create_background_task(
                        self._listen_loop(),
                        name="orvibohomebridge_server_response_listener"
                    )
                    # 等待Hello密钥返回
                    await asyncio.sleep(3)
                    _LOGGER.info(f"等待后检查session_key={self.session_key}")
                    login_result = await self._send_login()
                    _LOGGER.info(f"SSL登录结果: {login_result}")
                    if login_result:
                        _LOGGER.info("启动心跳保活任务...")
                        self._heartbeat_task = self.hass.async_create_background_task(
                            self._heartbeat_loop(),
                            name="orvibohomebridge_heartbeat"
                        )
                    return login_result
            except Exception as e:
                _LOGGER.warning(f"连接/登录重试 {retry+1}/{SSL_MAX_RECONNECT_ATTEMPTS}: {e}")
                await asyncio.sleep(self.retry_interval * (retry + 1))
        return False

    async def _send_packet(self, data: dict, key: bytes):
        try:
            if key == DEFAULT_KEY.encode("utf-8"):
                packet_type = bytes([0x70, 0x6b])
                self.session_id = bytes(ID_UNSET).decode("utf-8")
            else:
                packet_type = bytes([0x64, 0x6b])

            ciphertext = HomematePacket.build_packet(
                packet_type=packet_type,
                key=key,
                session_id=self.session_id.encode("utf-8"),
                payload=data
            )
            if not self.writer:
                await self._reconnect()
                return

            self.writer.write(ciphertext)
            await self.writer.drain()
            _LOGGER.debug(f"发送数据包 cmd={data.get('cmd')}, deviceId={data.get('deviceId')}")
        except Exception as e:
            _LOGGER.error("发送数据包失败: %s", e)
            if "lost" in str(e) or "close" in str(e):
                await self._reconnect()

    async def _send_hello(self):
        payload = HomemateJsonData.ssl_get_session()
        _LOGGER.info(f"发送Hello包: {payload}")
        await self._send_packet(payload, DEFAULT_KEY.encode("utf-8"))

    async def _send_login(self):
        if not self.connected:
            _LOGGER.warning("未建立SSL连接，无法发起登录")
            return False
        _LOGGER.info(f"准备登录，当前session_key={self.session_key}, family_id={self.family_id}")
        password_md5 = hashlib.md5(self.password.encode()).hexdigest().upper()
        payload = HomemateJsonData.ssl_login(
            username=self.username,
            password_md5=password_md5,
            family_id=self.family_id
        )
        if self.session_key and self.session_key != DEFAULT_KEY.encode("utf-8"):
            await self._send_packet(payload, self.session_key)
            return True
        else:
            _LOGGER.warning("会话密钥未获取，暂不发送登录包")
            return False

    async def send_control_switch(self, device_id: str, device_uid: str, state: bool):
        await self.connect_and_login()
        if not self.session_key or self.session_key == DEFAULT_KEY.encode("utf-8"):
            _LOGGER.warning("会话密钥无效，无法下发")
            return False
        payload = HomemateJsonData.ssl_control_switch(
            username=self.username,
            device_id=device_id,
            device_uid=device_uid,
            state=state
        )
        _LOGGER.info(f"下发开关控制 {device_id} state={state} payload={payload}")
        await self._send_packet(payload, self.session_key)
        return True

    async def send_control_dimmable_light_brightness(self, device_id: str, device_uid: str, brightness_percent: int):
        """可调光灯亮度控制（set property 格式，type=502）。"""
        await self.connect_and_login()
        if not self.session_key or self.session_key == DEFAULT_KEY.encode("utf-8"):
            _LOGGER.warning("会话密钥无效，无法下发")
            return False
        payload = HomemateJsonData.ssl_control_dimmable_light_brightness(
            username=self.username,
            device_id=device_id,
            device_uid=device_uid,
            brightness_percent=brightness_percent
        )
        _LOGGER.info(f"下发可调光灯亮度 {device_id} brightness={brightness_percent}%")
        await self._send_packet(payload, self.session_key)
        return True

    async def send_control_light(self, device_id: str, device_uid: str, state: bool, brightness: int = 0, colortemp_mired: int = 0):
        await self.connect_and_login()
        if not self.session_key or self.session_key == DEFAULT_KEY.encode("utf-8"):
            _LOGGER.warning("会话密钥无效，无法下发")
            return False
        payload = HomemateJsonData.ssl_control_light(
            username=self.username,
            device_id=device_id,
            device_uid=device_uid,
            state=state,
            brightness=brightness,
            colortemp_mired=colortemp_mired
        )
        _LOGGER.info(f"下发灯光控制 {device_id} state={state} bri={brightness} ct_mired={colortemp_mired}")
        await self._send_packet(payload, self.session_key)
        return True

    async def send_control_light_brightness(self, device_id: str, device_uid: str, brightness: int):
        await self.connect_and_login()
        if not self.session_key or self.session_key == DEFAULT_KEY.encode("utf-8"):
            _LOGGER.warning("会话密钥无效，无法下发")
            return False
        payload = HomemateJsonData.ssl_control_light_brightness(
            username=self.username,
            device_id=device_id,
            device_uid=device_uid,
            brightness=brightness
        )
        _LOGGER.info(f"下发亮度 {device_id} value={brightness}")
        await self._send_packet(payload, self.session_key)
        return True

    async def send_control_light_colortemp(self, device_id: str, device_uid: str, colortemp_k: int, brightness: int = 0):
        await self.connect_and_login()
        if not self.session_key or self.session_key == DEFAULT_KEY.encode("utf-8"):
            _LOGGER.warning("会话密钥无效，无法下发")
            return False
        payload = HomemateJsonData.ssl_control_light_colortemp(
            username=self.username,
            device_id=device_id,
            device_uid=device_uid,
            colortemp_k=colortemp_k,
            brightness=brightness
        )
        _LOGGER.info(f"下发色温 {device_id} {colortemp_k}K bri={brightness}")
        await self._send_packet(payload, self.session_key)
        return True

    async def send_light_bri_ct(self, device_id: str, device_uid: str, brightness: Optional[int], color_temp_k: Optional[int], power: Optional[bool] = None):
        """一次性下发亮度+色温 复合cmd=15指令"""
        await self.connect_and_login()
        if not self.session_key or self.session_key == DEFAULT_KEY.encode("utf-8"):
            _LOGGER.warning("会话密钥无效，无法下发复合灯光指令")
            return False

        if power is None:
            power = brightness > 0 if brightness is not None else True

        payload = HomemateJsonData.ssl_control_light_full(
            username=self.username,
            device_id=device_id,
            device_uid=device_uid,
            brightness=brightness,
            colortemp_k=color_temp_k,
            power=power
        )
        _LOGGER.info(f"复合调光下发 device={device_id} power={power} bri={brightness} ct={color_temp_k}")
        await self._send_packet(payload, self.session_key)
        return True

    async def send_control_cover(self, device_id: str, device_uid: str, position: int):
        await self.connect_and_login()
        if not self.session_key or self.session_key == DEFAULT_KEY.encode("utf-8"):
            _LOGGER.warning("会话密钥无效，无法下发")
            return False
        payload = HomemateJsonData.ssl_control_cover(
            username=self.username,
            device_id=device_id,
            device_uid=device_uid,
            position=position
        )
        await self._send_packet(payload, self.session_key)
        return True

    async def send_clothes_horse_control(self, device_id: str, device_uid: str, ctrl_field: str, ctrl_value: str):
        """发送晾衣架控制命令(cmd=98)。

        Args:
            ctrl_field: lightingCtrl/sterilizingCtrl/windDryingCtrl/heatDryingCtrl/mainSwitchCtrl/motorCtrl
            ctrl_value: on/off/up/down/stop
        """
        await self.connect_and_login()
        if not self.session_key or self.session_key == DEFAULT_KEY.encode("utf-8"):
            _LOGGER.warning("会话密钥无效，无法下发")
            return False
        payload = HomemateJsonData.ssl_clothes_horse_control(
            username=self.username,
            device_id=device_id,
            device_uid=device_uid,
            ctrl_field=ctrl_field,
            ctrl_value=ctrl_value,
        )
        _LOGGER.info(f"下发晾衣架控制 {device_id} {ctrl_field}={ctrl_value}")
        await self._send_packet(payload, self.session_key)
        return True

    async def send_clothes_horse_query(self, device_id: str):
        """发送晾衣架状态查询命令(cmd=100)。"""
        await self.connect_and_login()
        if not self.session_key or self.session_key == DEFAULT_KEY.encode("utf-8"):
            _LOGGER.warning("会话密钥无效，无法下发")
            return False
        payload = HomemateJsonData.ssl_clothes_horse_query(device_id=device_id)
        _LOGGER.info(f"查询晾衣架状态 {device_id}")
        await self._send_packet(payload, self.session_key)
        return True

    async def _heartbeat_loop(self):
        """心跳保活循环，每隔 heartbeat_interval 秒发送一次心跳包。"""
        _LOGGER.debug("心跳保活循环启动，间隔%d秒", self.heartbeat_interval)
        while self.connected:
            try:
                await asyncio.sleep(self.heartbeat_interval)
                if not self.connected:
                    break
                if self.session_key and self.session_key != DEFAULT_KEY.encode("utf-8"):
                    payload = HomemateJsonData.ssl_heartbeat()
                    await self._send_packet(payload, self.session_key)
                    _LOGGER.debug("发送心跳包")
            except asyncio.CancelledError:
                _LOGGER.debug("心跳任务被取消，退出循环")
                return
            except Exception as e:
                _LOGGER.error(f"心跳发送异常: {str(e)}")
                await asyncio.sleep(1)
        _LOGGER.debug("心跳保活循环结束")

    async def _listen_loop(self):
        _LOGGER.debug("SSL后台监听循环启动")
        while True:
            try:
                header_data = await self.reader.readexactly(42)
                if not header_data:
                    await asyncio.sleep(1)
                    continue
                length = HomematePacket.parse_length(header_data)
                ciphertext = await self.reader.readexactly(length - 42)
                if self.session_key is None:
                    self.session_key = DEFAULT_KEY.encode("utf-8")
                packet = HomematePacket(header_data + ciphertext, {self.session_id: self.session_key})
                self.session_id = bytes(packet.session_id).decode("utf-8")
                data = packet.json_payload
                if data is None:
                    _LOGGER.warning("数据包JSON解析失败，丢弃")
                    continue
                cmd = data.get("cmd")
                _LOGGER.debug(f"收到服务端包 cmd={cmd}")
                if cmd == CMD_HELLO:
                    await self._handle_hello(data)
                elif cmd == CMD_LOGIN:
                    await self._handle_login(data)
                elif data.get("action") == "deviceStatusReport":
                    await self._handle_device_status_report(data)
                elif data.get("namespace") == "device_manage" and data.get("action") == "upLoadDeviceList":
                    await self._handle_upload_device_list(data)
                elif cmd == CMD_STATE_UPDATE:
                    await self._handle_state_update(data)
                elif cmd == CMD_CLOTHES_HORSE_STATE:
                    await self._handle_clothes_horse_state(data)
                elif cmd in (CMD_HEARTBEAT, CMD_HANDSHAKE):
                    continue
                else:
                    _LOGGER.debug(f"未知cmd包: {data}")
            except asyncio.IncompleteReadError:
                _LOGGER.warning("SSL流读取不完整，连接断开")
                break
            except asyncio.TimeoutError:
                continue
            except ConnectionError:
                _LOGGER.warning("网络连接中断")
                break
            except asyncio.CancelledError:
                _LOGGER.debug("监听任务被取消，退出循环")
                await self._disconnect()
                return
            except Exception as e:
                _LOGGER.error(f"监听循环异常: {str(e)}")
                await asyncio.sleep(1)
        await self._reconnect()

    async def _handle_hello(self, data: dict):
        key = data.get("key")
        self.session_key = str(key).encode("utf-8") if key else DEFAULT_KEY.encode("utf-8")
        SSLClient.add_key(self.session_id, self.session_key)
        _LOGGER.info(f"Hello响应成功，会话ID:{self.session_id} 密钥:{key}")
        self.on_session_id_obtained(self.session_id)

    async def _handle_login(self, data: dict):
        status = data.get("status")
        user_id = data.get("userId")
        if status == 0 or user_id:
            _LOGGER.info(f"SSL登录成功 userId={user_id}")
            return True
        _LOGGER.error(f"登录失败 status={status} msg={data.get('msg')}")
        return False

    async def _handle_upload_device_list(self, data: dict):
        device_list = data.get("data", {}).get("deviceList", [])
        _LOGGER.info(f"全量设备列表推送，共{len(device_list)}台")
        for dev_data in device_list:
            dev_id = dev_data.get("deviceId")
            if not dev_id:
                continue
            status_info = {}
            props = dev_data.get("properties", {})
            status_info["properties"] = props
            # 解析开关
            onoff = props.get("onoff", {})
            status_info["state"] = onoff.get("status") == "on"
            # 亮度兼容 brightness / value2
            status_info["brightness"] = props.get("brightness", props.get("value2"))
            # 色温兼容 colortemp / value3
            status_info["color_temp"] = props.get("colortemp", props.get("value3"))
            # 窗帘
            status_info["position"] = props.get("percent")
            # 在线状态
            online = dev_data.get("online", "")
            status_info["online"] = online.strip().lower() in ("online", "1", "true")
            self.on_status_update(dev_id, status_info)

    async def _handle_device_status_report(self, data: dict):
        """处理 deviceStatusReport 消息，只提取原始数据"""
        dev_data = data.get("data", {})
        dev_id = dev_data.get("deviceId")
        if not dev_id:
            return
        
        _LOGGER.info(f"[SSL接收] deviceStatusReport数据: {data}")
        
        # 只提取原始数据，不做解析
        raw_status = {
            "raw_data": data,  # 保留完整原始数据
            "properties": dev_data.get("properties", {}),
            "deviceId": dev_id,
            "uid": dev_data.get("uid", ""),
            "online": True,
        }
        
        _LOGGER.info(f"[SSL输出] deviceStatusReport原始数据: deviceId={dev_id}")
        self.on_status_update(dev_id, raw_status)

    async def _handle_state_update(self, data: dict):
        """处理cmd=42 MQTT设备状态推送，只提取原始数据，不做状态解析"""
        # 输出所有cmd=42消息，用于诊断
        _LOGGER.info(f"[SSL接收] cmd=42完整数据: {data}")
        
        if not data.get("respByAcc"):
            _LOGGER.info(f"[SSL过滤] respByAcc=false，跳过处理: deviceId={data.get('deviceId')}")
            return
        
        dev_id = data.get("deviceId", "")
        uid = data.get("uid", "")
        
        # 只提取原始数据，不做解析（解析逻辑由 coordinator 根据设备类型处理）
        raw_status = {
            "raw_data": data,  # 保留完整原始数据
            "properties": data.get("properties", {}),  # properties 字段
            "value1": data.get("value1"),  # 开关/窗帘位置
            "value2": data.get("value2"),  # 亮度
            "value3": data.get("value3"),  # 色温
            "value4": data.get("value4"),  # 其他参数
            "statusType": data.get("statusType"),  # 状态类型
            "subDeviceType": data.get("subDeviceType"),  # 子设备类型
            "deviceId": dev_id,
            "uid": uid,
            "online": True,  # MQTT推送的设备默认在线
        }
        
        _LOGGER.info(f"[SSL输出] 原始状态数据: deviceId={dev_id}, value1={raw_status['value1']}, value2={raw_status['value2']}, value3={raw_status['value3']}")
        
        self.on_status_update(dev_id, raw_status)

    async def _handle_clothes_horse_state(self, data: dict):
        """处理 cmd=99 晾衣架状态推送。"""
        _LOGGER.info(f"[SSL接收] cmd=99晾衣架状态: {data}")

        dev_id = data.get("deviceId", "")
        if not dev_id:
            return

        raw_status = {
            "raw_data": data,
            "is_clothes_horse": True,
            "motor_state": data.get("motorState", "stop"),
            "motor_position": data.get("motorPosition", 0),
            "lighting_state": data.get("lightingState", "off"),
            "heat_drying_state": data.get("heatDryingState", "off"),
            "wind_drying_state": data.get("windDryingState", "off"),
            "sterilizing_state": data.get("sterilizingState", "off"),
            "main_switch_state": data.get("mainSwitchState", "off"),
            "deviceId": dev_id,
            "uid": data.get("uid", ""),
            "online": True,
        }

        _LOGGER.info(
            f"[SSL输出] 晾衣架状态: deviceId={dev_id}, "
            f"lighting={raw_status['lighting_state']}, motor={raw_status['motor_state']}, "
            f"pos={raw_status['motor_position']}"
        )

        self.on_status_update(dev_id, raw_status)