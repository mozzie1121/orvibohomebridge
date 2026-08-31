import logging
import asyncio
import time
from pathlib import Path
from datetime import datetime
from typing import Optional, Callable
from homeassistant.core import HomeAssistant
from .packet import HomematePacket, HomemateJsonData
from .protocol import normalize_password_hash
from .ssl_transport import SSLTransport, TlsFiles
from .pending_requests import PendingRequests

from .const import (
    CLIENT_CERT, CLIENT_KEY, SERVER_CA, ID_UNSET, DEFAULT_KEY,
    SSL_MAX_RECONNECT_ATTEMPTS,
    CMD_HELLO, CMD_LOGIN, CMD_STATE_UPDATE, CMD_CONTROL, CMD_HEARTBEAT, CMD_HANDSHAKE,
    CMD_CLOTHES_HORSE_CONTROL, CMD_CLOTHES_HORSE_STATE, CMD_CLOTHES_HORSE_QUERY,
    CMD_COS_AUTH, CMD_TEMP_PASSWORD, CMD_DELETE_AUTHORIZATION,
)

_LOGGER = logging.getLogger(__name__)


class SSLClient:
    _initial_keys = {}

    RECONNECT_TIMEOUT = 30

    def __init__(
        self,
        hass: HomeAssistant,
        ssl_host: str,
        ssl_port: int,
        username: str,
        password_hash: str,
        family_id: str,
        on_session_id_obtained: Callable[[str], None],
        on_status_update: Callable[[str, dict], None],
        on_reconnected: Optional[Callable[[], None]] = None,
        heartbeat_interval: int = 120,
        retry_interval: int = 5
    ):
        self.hass = hass
        self.ssl_host = ssl_host
        self.ssl_port = ssl_port
        self.username = username
        self.password_hash = normalize_password_hash(password_hash)
        self.family_id = family_id

        self.on_session_id_obtained = on_session_id_obtained
        self.on_status_update = on_status_update
        self.on_reconnected = on_reconnected
        self.heartbeat_interval = heartbeat_interval
        self.retry_interval = retry_interval

        self.certfile = Path(CLIENT_CERT)
        self.keyfile = Path(CLIENT_KEY)
        self.cafile = Path(SERVER_CA)

        self.transport = SSLTransport(
            hass,
            ssl_host,
            ssl_port,
            TlsFiles(self.certfile, self.keyfile, self.cafile),
        )
        self.session_id: Optional[str] = None
        self.session_key: Optional[bytes] = None
        self._closed: bool = False
        self._reconnect_lock = asyncio.Lock()
        self._listening_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._heartbeat_failures: int = 0
        self.HEARTBEAT_MAX_FAILURES = 2
        # 是否已完成过首次登录：此后每次登录成功都视为"重连"，触发 on_reconnected
        self._has_logged_in_once: bool = False

        self._pending_requests = PendingRequests()
        # 控制响应超时（秒）
        self._control_response_timeout: float = 3.0
        # 心跳回包等待超时（秒）：服务器对 cmd=32 心跳有回包（实测确认），
        # 改请求-响应式心跳后，无回包即视为连接假死。
        self.heartbeat_response_timeout: float = 15.0
        # 监听循环读超时（秒）：半开连接上 readexactly 不再无限阻塞。
        self.read_timeout: float = 300.0
        # 当前在途心跳回包（一次只有一个）
        self._heartbeat_pending: Optional[asyncio.Future] = None
        # 登录等待机制
        self._login_event: Optional[asyncio.Event] = None
        self._login_result: bool = False
        self._login_status: Optional[int] = None
        self._login_msg: Optional[str] = None

    def _get_key(self) -> bytes:
        """获取实例自己的 session_key，降级到类变量或默认 key"""
        if self.session_key:
            return self.session_key
        if self.session_id and self.session_id in self._initial_keys:
            return self._initial_keys[self.session_id]
        return DEFAULT_KEY.encode("utf-8")

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
    def connected(self) -> bool:
        return self.transport.connected

    @connected.setter
    def connected(self, value: bool) -> None:
        self.transport.connected = value

    @property
    def reader(self):
        return self.transport.reader

    @reader.setter
    def reader(self, value) -> None:
        self.transport.reader = value

    @property
    def writer(self):
        return self.transport.writer

    @writer.setter
    def writer(self, value) -> None:
        self.transport.writer = value

    @property
    def ssl_context(self):
        return self.transport.ssl_context

    @ssl_context.setter
    def ssl_context(self, value) -> None:
        self.transport.ssl_context = value

    @property
    def is_connected(self):
        return self.connected

    async def _create_ssl_context(self):
        return await self.transport.create_ssl_context()

    async def _connect(self):
        if self.connected:
            return True
        try:
            _LOGGER.debug("SSL正在连接...")
            await self.transport.connect()
            _LOGGER.debug("SSL连接成功")
            return True
        except asyncio.TimeoutError:
            _LOGGER.error("SSL连接服务器 [%s:%s] 超时", self.ssl_host, self.ssl_port)
            return False
        except OSError as e:
            _LOGGER.error("SSL连接发生IO错误: %s", e)
            return False
        except Exception as e:
            _LOGGER.error("SSL连接失败: %s", e)
            return False

    async def _disconnect(self):
        current_task = asyncio.current_task()
        for task in (self._listening_task, self._heartbeat_task):
            if task is None or task.done() or task is current_task:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._listening_task = None
        self._heartbeat_task = None

        if self.writer and not self.writer.is_closing():
            _LOGGER.debug("SSL正在断开已有连接...")
            try:
                await self.transport.close()
            except Exception as e:
                _LOGGER.debug("关闭SSL连接失败: %s", e)

        else:
            await self.transport.close()
        self.session_id = None
        self.session_key = None
        self._closed = True
        self._pending_requests.cancel_all()
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

    async def connect_and_login(
        self,
        max_attempts: int = SSL_MAX_RECONNECT_ATTEMPTS,
        hello_wait: float = 3.0,
    ):
        """连接并登录。max_attempts/hello_wait 供配置流程的轻量探针使用。"""
        if self.connected:
            return True
        self._closed = False
        
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
        
        for retry in range(max_attempts):
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
                    await asyncio.sleep(hello_wait)
                    _LOGGER.debug("等待后检查 session_key 是否就绪: %s", self.session_key is not None)
                    login_result = await self._send_login()
                    _LOGGER.debug("SSL登录结果: %s", login_result)
                    if login_result:
                        _LOGGER.debug("启动心跳保活任务...")
                        self._heartbeat_task = self.hass.async_create_background_task(
                            self._heartbeat_loop(),
                            name="orvibohomebridge_heartbeat"
                        )
                        # 重连成功（非首次）后通知协调器做全量状态重同步：
                        # 实测服务端重登后不主动推送设备列表，断线期间的增量推送
                        # 会永久丢失，必须客户端主动重拉。
                        if self._has_logged_in_once and self.on_reconnected is not None:
                            self.on_reconnected()
                        self._has_logged_in_once = True
                        return True
                    else:
                        _LOGGER.error("SSL登录失败，断开连接等待重试")
                        await self._disconnect()
                        raise ConnectionError("SSL登录失败")
            except Exception as e:
                _LOGGER.debug(f"连接/登录重试 {retry+1}/{SSL_MAX_RECONNECT_ATTEMPTS}: {e}")
                await asyncio.sleep(self.retry_interval * (retry + 1))
        return False

    async def _send_packet(self, data: dict, key: bytes) -> bool:
        """发送一个协议包，返回是否真正写入了传输层。

        之前该函数吞掉异常且不返回结果，导致所有 send_control_* 无条件
        return True（"假成功"）。现在失败返回 False，调用方不再写乐观状态。
        """
        control_key = None
        try:
            device_id = str(data.get("deviceId") or "")
            if data.get("cmd") == CMD_CONTROL and device_id:
                control_key = f"control:{device_id}"
                self._pending_requests.register(control_key, replace=True)
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
                _LOGGER.debug("发送失败：SSL 连接未建立，触发重连（本次包丢弃）")
                await self._reconnect()
                return False

            await self.transport.write(ciphertext)
            _LOGGER.debug(f"发送数据包 cmd={data.get('cmd')}, deviceId={data.get('deviceId')}")
            return True
        except Exception as e:
            if control_key is not None:
                self._pending_requests.resolve(control_key, None)
            _LOGGER.error("发送数据包失败: %s", e)
            if "lost" in str(e) or "close" in str(e):
                await self._reconnect()
            return False

    async def _send_hello(self):
        payload = HomemateJsonData.ssl_get_session()
        _LOGGER.debug("发送Hello包: cmd=%s keys=%s", payload.get("cmd"), sorted(payload))
        await self._send_packet(payload, DEFAULT_KEY.encode("utf-8"))

    async def _send_login(self):
        if not self.connected:
            _LOGGER.debug("未建立SSL连接，无法发起登录")
            return False
        _LOGGER.debug(
            "准备登录: session_key_ready=%s, family_id=%s",
            self.session_key is not None,
            self.family_id,
        )
        payload = HomemateJsonData.ssl_login(
            username=self.username,
            password_md5=self.password_hash,
            family_id=self.family_id,
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
        return await self._send_packet(payload, self.session_key)

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
        return await self._send_packet(payload, self.session_key)

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
        return await self._send_packet(payload, self.session_key)

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
        return await self._send_packet(payload, self.session_key)

    async def _send_property_control(self, payload: dict) -> bool:
        await self.connect_and_login()
        if not self.session_key or self.session_key == DEFAULT_KEY.encode("utf-8"):
            _LOGGER.debug("会话密钥无效，无法下发")
            return False
        return await self._send_packet(payload, self.session_key)

    async def send_control_floor_heating_power(self, device_id: str, device_uid: str, state: bool):
        return await self._send_property_control(
            HomemateJsonData.ssl_control_floor_heating_power(
                self.username, device_id, device_uid, state
            )
        )

    async def send_control_floor_heating_temperature(self, device_id: str, device_uid: str, temperature: int):
        return await self._send_property_control(
            HomemateJsonData.ssl_control_floor_heating_temperature(
                self.username, device_id, device_uid, temperature
            )
        )

    async def send_control_dream_curtain_action(self, device_id: str, device_uid: str, action: str):
        return await self._send_property_control(
            HomemateJsonData.ssl_control_dream_curtain_action(
                self.username, device_id, device_uid, action
            )
        )

    async def send_control_dream_curtain_percent(self, device_id: str, device_uid: str, percent: int):
        return await self._send_property_control(
            HomemateJsonData.ssl_control_dream_curtain_percent(
                self.username, device_id, device_uid, percent
            )
        )

    async def send_control_dream_curtain_angle(self, device_id: str, device_uid: str, angle: int):
        return await self._send_property_control(
            HomemateJsonData.ssl_control_dream_curtain_angle(
                self.username, device_id, device_uid, angle
            )
        )

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
        return await self._send_packet(payload, self.session_key)

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
        return await self._send_packet(payload, self.session_key)

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
        return await self._send_packet(payload, self.session_key)

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
        return await self._send_packet(payload, self.session_key)

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
        return await self._send_packet(payload, self.session_key)

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
        return await self._send_packet(payload, self.session_key)

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
        return await self._send_packet(payload, self.session_key)

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
        return await self._send_packet(payload, self.session_key)

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
        return await self._send_packet(payload, self.session_key)

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
        return await self._send_packet(payload, self.session_key)

    async def send_control_cover(self, device_id: str, device_uid: str, position: int, stop_value2: int = 0):
        await self.connect_and_login()
        if not self.session_key or self.session_key == DEFAULT_KEY.encode("utf-8"):
            _LOGGER.debug("会话密钥无效，无法下发")
            return False
        payload = HomemateJsonData.ssl_control_cover(
            username=self.username,
            device_id=device_id,
            device_uid=device_uid,
            position=position,
            stop_value2=stop_value2,
        )
        return await self._send_packet(payload, self.session_key)

    async def _send_legacy_floor_heating(
        self,
        device_id: str,
        device_uid: str,
        *,
        order: str,
        value1: int,
        value2: int,
    ) -> bool:
        await self.connect_and_login()
        if not self.session_key or self.session_key == DEFAULT_KEY.encode("utf-8"):
            _LOGGER.debug("会话密钥无效，无法下发旧协议地暖控制")
            return False
        payload = HomemateJsonData.ssl_control_legacy_floor_heating(
            username=self.username,
            device_id=device_id,
            device_uid=device_uid,
            order=order,
            value1=value1,
            value2=value2,
        )
        return await self._send_packet(payload, self.session_key)

    async def send_control_legacy_floor_heating_power(
        self,
        device_id: str,
        device_uid: str,
        is_on: bool,
        *,
        packed_state: int = 0,
    ) -> bool:
        return await self._send_legacy_floor_heating(
            device_id,
            device_uid,
            order="on" if is_on else "off",
            value1=0 if is_on else 1,
            value2=0 if is_on else max(0, int(packed_state)),
        )

    async def send_control_legacy_floor_heating_temperature(
        self, device_id: str, device_uid: str, temperature: int
    ) -> bool:
        target = max(10, min(35, int(round(temperature))))
        return await self._send_legacy_floor_heating(
            device_id,
            device_uid,
            order="temperature setting",
            value1=8,
            value2=target - 10,
        )

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
        return await self._send_packet(payload, self.session_key)

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
        return await self._send_packet(payload, self.session_key)

    async def send_clothes_horse_query(self, device_id: str):
        """发送晾衣架状态查询命令(cmd=100)。"""
        await self.connect_and_login()
        if not self.session_key or self.session_key == DEFAULT_KEY.encode("utf-8"):
            _LOGGER.debug("会话密钥无效，无法下发")
            return False
        payload = HomemateJsonData.ssl_clothes_horse_query(device_id=device_id)
        _LOGGER.debug(f"查询晾衣架状态 {device_id}")
        return await self._send_packet(payload, self.session_key)

    async def send_cos_auth(
        self,
        user_id: str,
        device_id: str,
        device_uid: str,
        timeout: float = 15.0,
    ) -> Optional[dict]:
        """请求门锁媒体 COS 凭证（cmd=313, Skill.GetCOSAuthorization）。

        返回完整响应 dict（含 response 字段，值为 QueryTxAuthResponse JSON
        字符串）；超时或连接失败返回 None。凭证由调用方缓存。
        """
        await self.connect_and_login()
        if not self.session_key or self.session_key == DEFAULT_KEY.encode("utf-8"):
            _LOGGER.debug("会话密钥无效，无法请求 COS 授权")
            return None
        payload = HomemateJsonData.ssl_cos_auth(
            user_id=user_id,
            device_id=device_id,
            device_uid=device_uid,
            family_id=self.family_id,
        )
        try:
            future = self._pending_requests.register("cos_auth")
        except RuntimeError:
            _LOGGER.debug("已有 COS 授权请求正在等待响应")
            return None
        try:
            _LOGGER.debug("发送 COS 授权请求 device=%s", device_id)
            if not await self._send_packet(payload, self.session_key):
                _LOGGER.error("COS 授权请求发送失败 device=%s", device_id)
                self._pending_requests.cancel("cos_auth", future)
                return None
            result = await self._pending_requests.wait("cos_auth", future, timeout)
            if result is None:
                _LOGGER.debug("等待 COS 授权响应超时")
            return result
        except Exception:
            self._pending_requests.cancel_all()
            raise

    async def _wait_temp_response(self, future, timeout: float) -> Optional[dict]:
        """等待 cmd=246/247 响应（由监听循环填充）。"""
        result = await self._pending_requests.wait("temp_authorization", future, timeout)
        if result is None:
            _LOGGER.debug("等待临时密码响应超时")
        return result

    async def send_temp_password(
        self,
        device_id: str,
        device_uid: str,
        name: str,
        auth_type: int,
        minutes: int,
        number: int,
        phone: str = "",
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        timeout: float = 15.0,
    ) -> Optional[dict]:
        """下发临时密码（cmd=246），返回完整响应（含 code/password）。"""
        await self.connect_and_login()
        if not self.session_key or self.session_key == DEFAULT_KEY.encode("utf-8"):
            _LOGGER.debug("会话密钥无效，无法下发临时密码")
            return None
        now = int(time.time())
        if auth_type == 1 and start_time is not None and end_time is not None:
            start_ts, end_ts = start_time, end_time
        else:
            start_ts = now
            end_ts = now + minutes * 60 if auth_type == 1 else 0
        payload = HomemateJsonData.ssl_temp_password(
            device_id=device_id,
            device_uid=device_uid,
            name=name,
            auth_type=auth_type,
            minutes=minutes,
            number=number,
            phone=phone,
            start_time=start_ts,
            end_time=end_ts,
        )
        _LOGGER.debug("下发临时密码 device=%s type=%s minutes=%s", device_id, auth_type, minutes)
        try:
            future = self._pending_requests.register("temp_authorization")
        except RuntimeError:
            _LOGGER.debug("已有临时密码请求正在等待响应")
            return None
        if not await self._send_packet(payload, self.session_key):
            _LOGGER.error("临时密码下发发送失败 device=%s", device_id)
            self._pending_requests.cancel("temp_authorization", future)
            return None
        return await self._wait_temp_response(future, timeout)

    async def delete_authorization(
        self,
        device_id: str,
        device_uid: str,
        authorized_id: int,
        timeout: float = 15.0,
    ) -> Optional[dict]:
        """删除授权（cmd=247，authorizedId 来自下发响应）。"""
        await self.connect_and_login()
        if not self.session_key or self.session_key == DEFAULT_KEY.encode("utf-8"):
            _LOGGER.debug("会话密钥无效，无法删除授权")
            return None
        payload = HomemateJsonData.ssl_delete_authorization(
            device_id=device_id,
            device_uid=device_uid,
            authorized_id=authorized_id,
        )
        _LOGGER.debug("删除授权 device=%s authorizedId=%s", device_id, authorized_id)
        try:
            future = self._pending_requests.register("temp_authorization")
        except RuntimeError:
            _LOGGER.debug("已有临时密码请求正在等待响应")
            return None
        if not await self._send_packet(payload, self.session_key):
            _LOGGER.error("删除授权发送失败 device=%s", device_id)
            self._pending_requests.cancel("temp_authorization", future)
            return None
        return await self._wait_temp_response(future, timeout)

    async def _wait_for_control_response(self, device_id: str, timeout: float | None = None) -> dict | None:
        """发送控制后等待设备返回 cmd=42 状态响应。

        在对应的 send_control_* 方法之后调用。如果设备在超时内返回了 cmd=42，
        返回完整的数据包 dict（含 value1~4 / properties 等），否则返回 None。
        """
        control_key = f"control:{device_id}"
        future = self._pending_requests.get(control_key)
        if future is None:
            _LOGGER.debug("设备 %s 没有待匹配的控制请求", device_id)
            return None
        effective_timeout = timeout if timeout is not None else self._control_response_timeout

        result = await self._pending_requests.wait(
            control_key,
            future,
            effective_timeout,
        )
        if result:
            _LOGGER.debug(f"[控制响应] device={device_id} 在 {effective_timeout}s 内收到响应: "
                          f"value1={result.get('value1')}, value2={result.get('value2')}, "
                          f"value3={result.get('value3')}, value4={result.get('value4')}")
        else:
            _LOGGER.debug(f"[控制响应] device={device_id} 在 {effective_timeout}s 内未收到响应")
        return result

    async def _heartbeat_loop(self):
        """心跳保活循环：请求-响应式（发送 cmd=32 并等待服务端回包）。

        半开/黑洞连接上写缓冲会"成功"但永远收不到回包，因此必须等回包确认；
        连续 HEARTBEAT_MAX_FAILURES 次无回包即关闭传输触发重连。
        """
        _LOGGER.debug("心跳保活循环启动，间隔%d秒", self.heartbeat_interval)
        while self.connected:
            try:
                await asyncio.sleep(self.heartbeat_interval)
                if not self.connected:
                    break
                if self.session_key and self.session_key != DEFAULT_KEY.encode("utf-8"):
                    payload = HomemateJsonData.ssl_heartbeat()
                    sent = await self._send_packet(payload, self.session_key)
                    if not sent:
                        # _send_packet 不再抛异常，失败以 False 返回，必须计入失败次数
                        raise ConnectionError("heartbeat send returned failure")
                    future: asyncio.Future = asyncio.get_running_loop().create_future()
                    self._heartbeat_pending = future
                    try:
                        await asyncio.wait_for(
                            future, timeout=self.heartbeat_response_timeout
                        )
                        self._heartbeat_failures = 0  # 收到回包重置计数
                        _LOGGER.debug("心跳回包确认")
                    except asyncio.TimeoutError:
                        raise ConnectionError("heartbeat response timeout")
                    finally:
                        self._heartbeat_pending = None
            except asyncio.CancelledError:
                _LOGGER.debug("心跳任务被取消，退出循环")
                return
            except Exception as e:
                _LOGGER.error(f"心跳发送异常: {str(e)}")
                self._heartbeat_failures += 1
                if self._heartbeat_failures >= self.HEARTBEAT_MAX_FAILURES:
                    _LOGGER.error(
                        f"连续{self._heartbeat_failures}次心跳失败，关闭连接触发重连"
                    )
                    self._heartbeat_failures = 0
                    self.connected = False
                    # 关闭传输以解除监听循环的阻塞读，_listen_loop 随即进入重连循环
                    try:
                        await self.transport.close()
                    except Exception:  # noqa: BLE001
                        pass
                    return
                await asyncio.sleep(1)
        _LOGGER.debug("心跳保活循环结束")

    async def _listen_loop(self):
        _LOGGER.debug("SSL后台监听循环启动")
        while True:
            try:
                # 读超时：半开/黑洞连接不再无限阻塞，超时即断开走重连
                header_data = await asyncio.wait_for(
                    self.transport.readexactly(42), timeout=self.read_timeout
                )
                if not header_data:
                    await asyncio.sleep(1)
                    continue
                length = HomematePacket.parse_length(header_data)
                ciphertext = await asyncio.wait_for(
                    self.transport.readexactly(length - 42),
                    timeout=self.read_timeout,
                )
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
                elif cmd == 82:
                    await self._handle_push_message(data)
                elif cmd == 352:
                    # 门锁事件（unlockEvent/errorUnlockEvent/picklockEvent/
                    # leaveHomeEvent/doorUnclose/doorbell ring）走同一状态通道
                    await self._handle_state_update(data)
                elif cmd == CMD_COS_AUTH:
                    # Skill.GetCOSAuthorization 响应（门锁媒体 COS 凭证）
                    if not self._pending_requests.resolve("cos_auth", data):
                        _LOGGER.debug("收到未期待的 cmd=313 响应")
                elif cmd in (CMD_TEMP_PASSWORD, CMD_DELETE_AUTHORIZATION):
                    # 临时密码下发/删除授权响应
                    if not self._pending_requests.resolve("temp_authorization", data):
                        _LOGGER.debug("收到未期待的 cmd=%s 响应", cmd)
                elif cmd in (CMD_HEARTBEAT, CMD_HANDSHAKE):
                    if cmd == CMD_HEARTBEAT:
                        pending = self._heartbeat_pending
                        if pending is not None and not pending.done():
                            pending.set_result(True)
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
        # 无限重连（指数退避封顶 60s）：不再"5 次失败后永久退出"，
        # 云推送通道不能因一次长时间断网就永久死亡直到重载集成。
        while not self._closed:
            if self.reader is None:
                _LOGGER.debug("reader 已丢失，放弃重连")
                return
            try:
                await self._reconnect()
                _LOGGER.debug("SSL重连成功，继续监听")
                return  # _reconnect 成功后 connect_and_login 已启动了新的 _listen_loop
            except ConnectionError:
                reconnect_count += 1
                backoff = min(
                    self.retry_interval * (2 ** min(reconnect_count - 1, 4)), 60
                )
                _LOGGER.warning(
                    "SSL重连失败（第%d次），%d秒后重试...", reconnect_count, backoff
                )
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                _LOGGER.debug("重连任务被取消")
                return
        _LOGGER.debug("SSL重连循环结束（_closed）")

    async def _handle_hello(self, data: dict):
        key = data.get("key")
        self.session_key = str(key).encode("utf-8") if key else DEFAULT_KEY.encode("utf-8")
        SSLClient.add_key(self.session_id, self.session_key)
        _LOGGER.debug("Hello响应成功: session_key_len=%s", len(self.session_key))
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
            "cmd": data.get("cmd"),
            "action": data.get("action"),
            "event": data.get("event"),
            "online": True,
        }
        
        _LOGGER.debug(f"[SSL输出] deviceStatusReport原始数据: deviceId={dev_id}")
        self.on_status_update(dev_id, raw_status)

    async def _handle_state_update(self, data: dict):
        """处理cmd=42 MQTT设备状态推送，只提取原始数据，不做状态解析"""
        # 输出所有cmd=42消息，用于诊断
        _LOGGER.debug(f"[SSL接收] cmd=42完整数据: {data}")

        dev_id = data.get("deviceId", "")
        if self._pending_requests.resolve(f"control:{dev_id}", data):
            _LOGGER.debug(f"[控制响应匹配] device={dev_id} 收到控制响应，唤醒等待")

        # 控制回显（respByAcc=false 且非主动事件）：不再直接丢弃（P0-3）。
        # - 携带状态字段（properties/value1-4）→ 并入状态管线，用真实回显更新实体，
        #   避免"控制后状态完全依赖设备再发一条推送"；
        # - 裸回执（无状态字段）→ 仅作为控制确认（等待者已在上方 resolve），不写状态。
        if data.get("respByAcc") is False and not isinstance(data.get("event"), dict):
            has_state = bool(data.get("properties")) or any(
                data.get(key) is not None
                for key in ("value1", "value2", "value3", "value4")
            )
            if not has_state:
                _LOGGER.debug(
                    f"[SSL] 控制回显无状态字段，仅回执确认: deviceId={data.get('deviceId')}"
                )
                return
            _LOGGER.debug(
                f"[SSL] 控制回显携带状态，并入状态管线: deviceId={data.get('deviceId')}"
            )
        
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
            "cmd": data.get("cmd"),
            "action": data.get("action"),
            "event": data.get("event"),
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

    async def _handle_push_message(self, data: dict):
        """处理 cmd=82 推送消息（门锁文本消息/告警等）。"""
        raw_status = {
            "raw_data": data,
            "cmd": 82,
            "data": data.get("data"),
            "text": data.get("text"),
            "infoType": data.get("infoType"),
            "messageType": data.get("messageType"),
            "deviceId": data.get("deviceId", ""),
            "uid": data.get("uid", ""),
            "online": True,
        }
        self.on_status_update(raw_status.get("deviceId") or "", raw_status)
