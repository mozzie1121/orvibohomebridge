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

    _reconnect_lock = asyncio.Lock()
    RECONNECT_TIMEOUT = 30

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
        heartbeat_interval: int = 120,
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

        self.certfile = Path(CLIENT_CERT)
        self.keyfile = Path(CLIENT_KEY)
        self.cafile = Path(SERVER_CA)

        self.ssl_context = None
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.session_id: Optional[str] = None
        self.session_key: Optional[bytes] = None
        self.connected: bool = False
        self._closed: bool = False
        self._listening_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._heartbeat_failures: int = 0
        self.HEARTBEAT_MAX_FAILURES = 2

        # 控制等待响应机制：device_id → asyncio.Event（等待 cmd=42 回复）
        self._pending_control: dict[str, asyncio.Event] = {}
        # device_id → 完整的 cmd=42 dict
        self._pending_results: dict[str, dict] = {}
        # 控制响应超时（秒）
        self._control_response_timeout: float = 3.0
        # 登录等待机制
        self._login_event: Optional[asyncio.Event] = None
        self._login_result: bool = False
        self._login_status: Optional[int] = None
        self._login_msg: Optional[str] = None

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
            _LOGGER.debug("SSL连接成功")
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
            _LOGGER.debug("SSL正在断开已有连接...")
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
        self._closed = True
        # 清空控制等待
        for event in self._pending_control.values():
            event.set()
        self._pending_control.clear()
        self._pending_results.clear()
        _LOGGER.debug("SSL连接已断开")

    async def _reconnect(self):
        async with self._reconnect_lock:
            if self.connected:
                return True
            try:
                await self._disconnect()
            except Exception as e:
                _LOGGER.error("断开连接异常: %s", e)

            if self.retry_interval > 0:
                _LOGGER.debug(f"{self.retry_interval}秒后尝试重连...")
                await asyncio.sleep(self.retry_interval)
                try:
                    success = await asyncio.wait_for(
                        self.connect_and_login(),
                        timeout=self.RECONNECT_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    _LOGGER.error(f"SSL重连超时({self.RECONNECT_TIMEOUT}秒)，放弃本次重连")
                    raise ConnectionError("SSL重连超时")
                if not success:
                    _LOGGER.error("SSL重连失败，将在下次重试")
                    raise ConnectionError("SSL重连失败")
                return True
            else:
                raise ConnectionError("重连间隔为0，放弃重连")

    async def connect_and_login(self):
        if self.connected:
            return True
        
        # 取消旧的 listen/heartbeat 任务，避免并发 listener
        if self._listening_task and not self._listening_task.done():
            self._listening_task.cancel()
            try:
                await self._listening_task
            except asyncio.CancelledError:
                pass
            self._listening_task = None
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None
        
        for retry in range(SSL_MAX_RECONNECT_ATTEMPTS):
            try:
                _LOGGER.debug("SSL正在连接和登录...")
                self.connected = await self._connect()
                if self.connected:
                    _LOGGER.debug("SSL连接成功，发送Hello...")
                    await self._send_hello()
                    _LOGGER.debug("创建后台监听任务...")
                    self._listening_task = self.hass.async_create_background_task(
                        self._listen_loop(),
                        name="orvibohomebridge_server_response_listener"
                    )
                    # 等待Hello密钥返回
                    await asyncio.sleep(3)
                    _LOGGER.debug(f"等待后检查session_key={self.session_key}")
                    login_result = await self._send_login()
                    _LOGGER.debug(f"SSL登录结果: {login_result}")
                    if login_result:
                        _LOGGER.debug("启动心跳保活任务...")
                        self._heartbeat_task = self.hass.async_create_background_task(
                            self._heartbeat_loop(),
                            name="orvibohomebridge_heartbeat"
                        )
                        return True
                    else:
                        _LOGGER.error("SSL登录失败，断开连接等待重试")
                        await self._disconnect()
                        raise ConnectionError("SSL登录失败")
            except Exception as e:
                _LOGGER.debug(f"连接/登录重试 {retry+1}/{SSL_MAX_RECONNECT_ATTEMPTS}: {e}")
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
        _LOGGER.debug(f"发送Hello包: {payload}")
        await self._send_packet(payload, DEFAULT_KEY.encode("utf-8"))

    async def _send_login(self):
        if not self.connected:
            _LOGGER.debug("未建立SSL连接，无法发起登录")
            return False
        _LOGGER.debug(f"准备登录，当前session_key={self.session_key}, family_id={self.family_id}")
        password_md5 = hashlib.md5(self.password.encode()).hexdigest().upper()
        payload = HomemateJsonData.ssl_login(
            username=self.username,
            password_md5=password_md5,
            family_id=self.family_id
        )
        if self.session_key and self.session_key != DEFAULT_KEY.encode("utf-8"):
            # 设置登录等待事件
            self._login_event = asyncio.Event()
            await self._send_packet(payload, self.session_key)
            try:
                await asyncio.wait_for(self._login_event.wait(), timeout=10)
                login_ok = self._login_result
                if not login_ok:
                    _LOGGER.error(f"服务器返回登录失败 status={self._login_status} msg={self._login_msg}")
                return login_ok
            except asyncio.TimeoutError:
                _LOGGER.error("等待登录响应超时")
                return False
            finally:
                self._login_event = None
        else:
            _LOGGER.debug("会话密钥未获取，暂不发送登录包")
            return False

    async def send_control_switch(self, device_id: str, device_uid: str, state: bool):
        await self.connect_and_login()
        if not self.session_key or self.session_key == DEFAULT_KEY.encode("utf-8"):
            _LOGGER.debug("会话密钥无效，无法下发")
            return False
        payload = HomemateJsonData.ssl_control_switch(
            username=self.username,
            device_id=device_id,
            device_uid=device_uid,
            state=state
        )
        _LOGGER.debug(f"下发开关控制 {device_id} state={state} payload={payload}")
        await self._send_packet(payload, self.session_key)
        return True

    async def send_control_cct_light_onoff(self, device_id: str, device_uid: str, state: bool):
        """色温灯开关控制（set property 格式，适用于 statusType=503）"""
        await self.connect_and_login()
        if not self.session_key or self.session_key == DEFAULT_KEY.encode("utf-8"):
            _LOGGER.debug("会话密钥无效，无法下发")
            return False
        payload = HomemateJsonData.ssl_control_cct_light_onoff(
            username=self.username,
            device_id=device_id,
            device_uid=device_uid,
            state=state
        )
        _LOGGER.debug(f"下发色温灯开关 {device_id} state={state}")
        await self._send_packet(payload, self.session_key)
        return True

    async def send_control_cct_light_brightness(self, device_id: str, device_uid: str, brightness_percent: int):
        """色温灯亮度控制（set property 格式，适用于 statusType=503）"""
        await self.connect_and_login()
        if not self.session_key or self.session_key == DEFAULT_KEY.encode("utf-8"):
            _LOGGER.debug("会话密钥无效，无法下发")
            return False
        payload = HomemateJsonData.ssl_control_cct_light_brightness(
            username=self.username,
            device_id=device_id,
            device_uid=device_uid,
            brightness_percent=brightness_percent
        )
        _LOGGER.debug(f"下发色温灯亮度 {device_id} {brightness_percent}%")
        await self._send_packet(payload, self.session_key)
        return True

    async def send_control_cct_light_colortemp(self, device_id: str, device_uid: str, colortemp_k: int):
        """色温灯色温控制（set property 格式，适用于 statusType=503）"""
        await self.connect_and_login()
        if not self.session_key or self.session_key == DEFAULT_KEY.encode("utf-8"):
            _LOGGER.debug("会话密钥无效，无法下发")
            return False
        payload = HomemateJsonData.ssl_control_cct_light_colortemp(
            username=self.username,
            device_id=device_id,
            device_uid=device_uid,
            colortemp_k=colortemp_k
        )
        _LOGGER.debug(f"下发色温灯色温 {device_id} {colortemp_k}K")
        await self._send_packet(payload, self.session_key)
        return True

    async def send_control_dimmable_light_brightness(self, device_id: str, device_uid: str, brightness_percent: int):
        """可调光灯亮度控制（set property 格式，type=502）。"""
        await self.connect_and_login()
        if not self.session_key or self.session_key == DEFAULT_KEY.encode("utf-8"):
            _LOGGER.debug("会话密钥无效，无法下发")
            return False
        payload = HomemateJsonData.ssl_control_dimmable_light_brightness(
            username=self.username,
            device_id=device_id,
            device_uid=device_uid,
            brightness_percent=brightness_percent
        )
        _LOGGER.debug(f"下发可调光灯亮度 {device_id} brightness={brightness_percent}%")
        await self._send_packet(payload, self.session_key)
        return True

    async def send_control_zigbee_dimmable_light_onoff(self, device_id: str, device_uid: str, state: bool, brightness: int = 255):
        """Zigbee调光灯开关控制（on/off 格式，适用于 deviceType=0, subDeviceType=-2）"""
        await self.connect_and_login()
        if not self.session_key or self.session_key == DEFAULT_KEY.encode("utf-8"):
            _LOGGER.debug("会话密钥无效，无法下发")
            return False
        payload = HomemateJsonData.ssl_control_zigbee_dimmable_light_onoff(
            username=self.username,
            device_id=device_id,
            device_uid=device_uid,
            state=state,
            brightness=brightness
        )
        _LOGGER.debug(f"下发Zigbee调光灯开关 {device_id} state={state} brightness={brightness}")
        await self._send_packet(payload, self.session_key)
        return True

    async def send_control_zigbee_dimmable_light_brightness(self, device_id: str, device_uid: str, brightness_255: int):
        """Zigbee调光灯亮度控制（set property 格式，适用于 deviceType=0, subDeviceType=-2）"""
        await self.connect_and_login()
        if not self.session_key or self.session_key == DEFAULT_KEY.encode("utf-8"):
            _LOGGER.debug("会话密钥无效，无法下发")
            return False
        payload = HomemateJsonData.ssl_control_zigbee_dimmable_light_brightness(
            username=self.username,
            device_id=device_id,
            device_uid=device_uid,
            brightness_255=brightness_255
        )
        _LOGGER.debug(f"下发Zigbee调光灯亮度 {device_id} brightness={brightness_255}")
        await self._send_packet(payload, self.session_key)
        return True

    async def send_control_fast_move_dim_color_light_onoff(self, device_id: str, device_uid: str, state: bool, brightness: int = 0, colortemp_mired: int = 0):
        """Fast Move调光调色灯开关控制（on/off 格式，适用于 statusType=2, subDeviceType=6）"""
        await self.connect_and_login()
        if not self.session_key or self.session_key == DEFAULT_KEY.encode("utf-8"):
            _LOGGER.debug("会话密钥无效，无法下发")
            return False
        payload = HomemateJsonData.ssl_control_fast_move_dim_color_light_onoff(
            username=self.username,
            device_id=device_id,
            device_uid=device_uid,
            state=state,
            brightness=brightness,
            colortemp_mired=colortemp_mired
        )
        _LOGGER.debug(f"下发Fast Move调光调色灯开关 {device_id} state={state}")
        await self._send_packet(payload, self.session_key)
        return True

    async def send_control_fast_move_dim_color_light_brightness(self, device_id: str, device_uid: str, brightness: int, colortemp_mired: int = 0):
        """Fast Move调光调色灯亮度控制（fast move to level 格式，适用于 statusType=2, subDeviceType=6）"""
        await self.connect_and_login()
        if not self.session_key or self.session_key == DEFAULT_KEY.encode("utf-8"):
            _LOGGER.debug("会话密钥无效，无法下发")
            return False
        payload = HomemateJsonData.ssl_control_fast_move_dim_color_light_brightness(
            username=self.username,
            device_id=device_id,
            device_uid=device_uid,
            brightness=brightness,
            colortemp_mired=colortemp_mired
        )
        _LOGGER.debug(f"下发Fast Move调光调色灯亮度 {device_id} brightness={brightness}, colortemp={colortemp_mired}")
        await self._send_packet(payload, self.session_key)
        return True

    async def send_control_fast_move_dim_color_light_colortemp(self, device_id: str, device_uid: str, brightness: int, colortemp_mired: int):
        """Fast Move调光调色灯色温控制（fast color temperature 格式，适用于 statusType=2, subDeviceType=6）"""
        await self.connect_and_login()
        if not self.session_key or self.session_key == DEFAULT_KEY.encode("utf-8"):
            _LOGGER.debug("会话密钥无效，无法下发")
            return False
        payload = HomemateJsonData.ssl_control_fast_move_dim_color_light_colortemp(
            username=self.username,
            device_id=device_id,
            device_uid=device_uid,
            brightness=brightness,
            colortemp_mired=colortemp_mired
        )
        _LOGGER.debug(f"下发Fast Move调光调色灯色温 {device_id} brightness={brightness}, colortemp={colortemp_mired}")
        await self._send_packet(payload, self.session_key)
        return True

    async def send_control_light(self, device_id: str, device_uid: str, state: bool, brightness: int = 0, colortemp_mired: int = 0):
        await self.connect_and_login()
        if not self.session_key or self.session_key == DEFAULT_KEY.encode("utf-8"):
            _LOGGER.debug("会话密钥无效，无法下发")
            return False
        payload = HomemateJsonData.ssl_control_light(
            username=self.username,
            device_id=device_id,
            device_uid=device_uid,
            state=state,
            brightness=brightness,
            colortemp_mired=colortemp_mired
        )
        _LOGGER.debug(f"下发灯光控制 {device_id} state={state} bri={brightness} ct_mired={colortemp_mired}")
        await self._send_packet(payload, self.session_key)
        return True

    async def send_control_light_brightness(self, device_id: str, device_uid: str, brightness: int):
        await self.connect_and_login()
        if not self.session_key or self.session_key == DEFAULT_KEY.encode("utf-8"):
            _LOGGER.debug("会话密钥无效，无法下发")
            return False
        payload = HomemateJsonData.ssl_control_light_brightness(
            username=self.username,
            device_id=device_id,
            device_uid=device_uid,
            brightness=brightness
        )
        _LOGGER.debug(f"下发亮度 {device_id} value={brightness}")
        await self._send_packet(payload, self.session_key)
        return True

    async def send_control_light_colortemp(self, device_id: str, device_uid: str, colortemp_k: int, brightness: int = 0):
        await self.connect_and_login()
        if not self.session_key or self.session_key == DEFAULT_KEY.encode("utf-8"):
            _LOGGER.debug("会话密钥无效，无法下发")
            return False
        payload = HomemateJsonData.ssl_control_light_colortemp(
            username=self.username,
            device_id=device_id,
            device_uid=device_uid,
            colortemp_k=colortemp_k,
            brightness=brightness
        )
        _LOGGER.debug(f"下发色温 {device_id} {colortemp_k}K bri={brightness}")
        await self._send_packet(payload, self.session_key)
        return True

    async def send_light_bri_ct(self, device_id: str, device_uid: str, brightness: Optional[int], color_temp_k: Optional[int], power: Optional[bool] = None):
        """一次性下发亮度+色温 复合cmd=15指令"""
        await self.connect_and_login()
        if not self.session_key or self.session_key == DEFAULT_KEY.encode("utf-8"):
            _LOGGER.debug("会话密钥无效，无法下发复合灯光指令")
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
        _LOGGER.debug(f"复合调光下发 device={device_id} power={power} bri={brightness} ct={color_temp_k}")
        await self._send_packet(payload, self.session_key)
        return True

    async def send_control_cover(self, device_id: str, device_uid: str, position: int):
        await self.connect_and_login()
        if not self.session_key or self.session_key == DEFAULT_KEY.encode("utf-8"):
            _LOGGER.debug("会话密钥无效，无法下发")
            return False
        payload = HomemateJsonData.ssl_control_cover(
            username=self.username,
            device_id=device_id,
            device_uid=device_uid,
            position=position
        )
        await self._send_packet(payload, self.session_key)
        return True

    async def send_control_ventilation(self, device_id: str, device_uid: str, value1: int):
        """发送新风系统控制命令(cmd=15 set property)。
        value1: 0=慢, 50=停, 100=快
        """
        await self.connect_and_login()
        if not self.session_key or self.session_key == DEFAULT_KEY.encode("utf-8"):
            _LOGGER.debug("会话密钥无效，无法下发")
            return False
        payload = HomemateJsonData.ssl_control_ventilation(
            username=self.username,
            device_id=device_id,
            device_uid=device_uid,
            value1=value1
        )
        _LOGGER.debug(f"下发新风系统控制 {device_id} value1={value1}")
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
            _LOGGER.debug("会话密钥无效，无法下发")
            return False
        payload = HomemateJsonData.ssl_clothes_horse_control(
            username=self.username,
            device_id=device_id,
            device_uid=device_uid,
            ctrl_field=ctrl_field,
            ctrl_value=ctrl_value,
        )
        _LOGGER.debug(f"下发晾衣架控制 {device_id} {ctrl_field}={ctrl_value}")
        await self._send_packet(payload, self.session_key)
        return True

    async def send_clothes_horse_query(self, device_id: str):
        """发送晾衣架状态查询命令(cmd=100)。"""
        await self.connect_and_login()
        if not self.session_key or self.session_key == DEFAULT_KEY.encode("utf-8"):
            _LOGGER.debug("会话密钥无效，无法下发")
            return False
        payload = HomemateJsonData.ssl_clothes_horse_query(device_id=device_id)
        _LOGGER.debug(f"查询晾衣架状态 {device_id}")
        await self._send_packet(payload, self.session_key)
        return True

    async def _wait_for_control_response(self, device_id: str, timeout: float | None = None) -> dict | None:
        """发送控制后等待设备返回 cmd=42 状态响应。

        在对应的 send_control_* 方法之后调用。如果设备在超时内返回了 cmd=42，
        返回完整的数据包 dict（含 value1~4 / properties 等），否则返回 None。
        """
        if device_id in self._pending_control:
            _LOGGER.debug(f"设备 {device_id} 已有等待中的控制响应，跳过")
            return None

        event = asyncio.Event()
        self._pending_control[device_id] = event
        effective_timeout = timeout if timeout is not None else self._control_response_timeout

        try:
            await asyncio.wait_for(event.wait(), timeout=effective_timeout)
            result = self._pending_results.pop(device_id, None)
            if result:
                _LOGGER.debug(f"[控制响应] device={device_id} 在 {effective_timeout}s 内收到响应: "
                              f"value1={result.get('value1')}, value2={result.get('value2')}, "
                              f"value3={result.get('value3')}, value4={result.get('value4')}")
            return result
        except asyncio.TimeoutError:
            _LOGGER.debug(f"[控制响应] device={device_id} 在 {effective_timeout}s 内未收到响应")
            return None
        finally:
            self._pending_control.pop(device_id, None)
            self._pending_results.pop(device_id, None)

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
                    self._heartbeat_failures = 0  # 成功发送重置计数
                    _LOGGER.debug("发送心跳包")
            except asyncio.CancelledError:
                _LOGGER.debug("心跳任务被取消，退出循环")
                return
            except Exception as e:
                _LOGGER.error(f"心跳发送异常: {str(e)}")
                self._heartbeat_failures += 1
                if self._heartbeat_failures >= self.HEARTBEAT_MAX_FAILURES:
                    _LOGGER.error(f"连续{self._heartbeat_failures}次心跳失败，触发重连")
                    self._heartbeat_failures = 0
                    self.connected = False
                    return  # 退出心跳，_listen_loop 会处理重连
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
                try:
                    packet = HomematePacket(header_data + ciphertext, {self.session_id: self.session_key})
                except (AssertionError, Exception) as e:
                    _LOGGER.error(f"坏包解析失败，丢弃: {e}")
                    continue
                self.session_id = bytes(packet.session_id).decode("utf-8")
                data = packet.json_payload
                if data is None:
                    _LOGGER.debug("数据包JSON解析失败，丢弃")
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
                _LOGGER.debug("SSL流读取不完整，连接断开")
                break
            except asyncio.TimeoutError:
                continue
            except (ConnectionError, OSError) as e:
                _LOGGER.debug(f"网络连接中断: {type(e).__name__}: {e}")
                break
            except asyncio.CancelledError:
                _LOGGER.debug("监听任务被取消，退出循环")
                await self._disconnect()
                return
            except Exception as e:
                import traceback
                _LOGGER.error(f"监听循环异常: {str(e)}\n{traceback.format_exc()}")
                if self.reader is None:
                    _LOGGER.debug("reader 已丢失，跳出监听循环")
                    break
                await asyncio.sleep(1)
        _LOGGER.debug("SSL监听循环结束，开始重连循环...")
        reconnect_count = 0
        max_reconnect = 5
        while not self._closed and reconnect_count < max_reconnect:
            if self.reader is None:
                _LOGGER.debug("reader 已丢失，放弃重连")
                return
            try:
                await self._reconnect()
                _LOGGER.debug("SSL重连成功，继续监听")
                return  # _reconnect 成功后 connect_and_login 已启动了新的 _listen_loop
            except ConnectionError:
                reconnect_count += 1
                backoff = min(self.retry_interval * (2 ** (reconnect_count - 1)), 60)
                _LOGGER.debug(f"SSL重连失败（{reconnect_count}/{max_reconnect}），{backoff}秒后重试...")
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                _LOGGER.debug("重连任务被取消")
                await self._disconnect()
                return
        if reconnect_count >= max_reconnect:
            _LOGGER.warning(f"SSL重连已达上限 {max_reconnect} 次，停止重连")

    async def _handle_hello(self, data: dict):
        key = data.get("key")
        self.session_key = str(key).encode("utf-8") if key else DEFAULT_KEY.encode("utf-8")
        SSLClient.add_key(self.session_id, self.session_key)
        _LOGGER.debug(f"Hello响应成功，会话ID:{self.session_id} 密钥:{key} hex={self.session_key.hex()} len={len(self.session_key)}")
        self.on_session_id_obtained(self.session_id)

    async def _handle_login(self, data: dict):
        status = data.get("status")
        user_id = data.get("userId")
        result = bool(status == 0 or user_id)
        # 保存结果供 _send_login 获取
        self._login_result = result
        self._login_status = status
        self._login_msg = data.get("msg")
        if self._login_event:
            self._login_event.set()
        if result:
            _LOGGER.debug(f"SSL登录成功 userId={user_id}")
        else:
            _LOGGER.error(f"登录失败 status={status} msg={data.get('msg')}")
        return result

    async def _handle_upload_device_list(self, data: dict):
        device_list = data.get("data", {}).get("deviceList", [])
        _LOGGER.debug(f"全量设备列表推送，共{len(device_list)}台")
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
        
        _LOGGER.debug(f"[SSL接收] deviceStatusReport数据: {data}")
        
        # 只提取原始数据，不做解析
        raw_status = {
            "raw_data": data,  # 保留完整原始数据
            "properties": dev_data.get("properties", {}),
            "deviceId": dev_id,
            "uid": dev_data.get("uid", ""),
            "online": True,
        }
        
        _LOGGER.debug(f"[SSL输出] deviceStatusReport原始数据: deviceId={dev_id}")
        self.on_status_update(dev_id, raw_status)

    async def _handle_state_update(self, data: dict):
        """处理cmd=42 MQTT设备状态推送，只提取原始数据，不做状态解析"""
        # 输出所有cmd=42消息，用于诊断
        _LOGGER.debug(f"[SSL接收] cmd=42完整数据: {data}")
        
        if not data.get("respByAcc"):
            _LOGGER.debug(f"[SSL过滤] respByAcc=false，跳过处理: deviceId={data.get('deviceId')}")
            return
        
        dev_id = data.get("deviceId", "")
        uid = data.get("uid", "")
        
        # ★ 检查：是否有控制操作正在等这个设备的响应
        if dev_id in self._pending_control:
            _LOGGER.debug(f"[控制响应匹配] device={dev_id} 收到控制响应，唤醒等待")
            self._pending_results[dev_id] = data
            self._pending_control[dev_id].set()
        
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
        
        _LOGGER.debug(f"[SSL输出] 原始状态数据: deviceId={dev_id}, value1={raw_status['value1']}, value2={raw_status['value2']}, value3={raw_status['value3']}")
        
        self.on_status_update(dev_id, raw_status)

    async def _handle_clothes_horse_state(self, data: dict):
        """处理 cmd=99 晾衣架状态推送。"""
        _LOGGER.debug(f"[SSL接收] cmd=99晾衣架状态: {data}")

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

        _LOGGER.debug(
            f"[SSL输出] 晾衣架状态: deviceId={dev_id}, "
            f"lighting={raw_status['lighting_state']}, motor={raw_status['motor_state']}, "
            f"pos={raw_status['motor_position']}"
        )

        self.on_status_update(dev_id, raw_status)