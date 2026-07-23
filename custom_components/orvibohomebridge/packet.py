import json
import struct
import binascii
import logging
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import (
    Cipher, algorithms, modes
)
from cryptography.hazmat.primitives import padding
from .functions import (
    text_utils_is_empty,
    hmac_sha256,
    generate_timestamp,
    generate_serial,
    generate_uuid,
)

_LOGGER = logging.getLogger(__name__)

from .const import (
    DEFAULT_KEY, SIGN_KEY, MAGIC,
    HTTPS_HOST, HTTP_HEADERS,
    SOFTWARE_NAME, SOFTWARE_VER, SOFTWARE_VERSION,
    SYS_VERSION, HARDWARE_VERSION, LANGUAGE, PHONE_NAME, DEBUG_INFO,
    CMD_HELLO, CMD_LOGIN, CMD_CONTROL, CMD_HEARTBEAT,
    CMD_CLOTHES_HORSE_CONTROL, CMD_CLOTHES_HORSE_QUERY,
)

# 当前 HTTPS API 主机（模块级，可在运行时切换：中国区/国际区）
_api_host = HTTPS_HOST


def set_api_host(host: str) -> None:
    global _api_host
    _api_host = host


def get_api_host() -> str:
    return _api_host
class HomematePacket:
    def __init__(self, data: bytes, keys: dict):
        self.raw = data
        if not data:
            self.magic = MAGIC
            self.length = 0
            self.packet_type = bytes([0x70, 0x6b])
            self.crc = None
            self.session_id = None
            self.json_payload = None
            return

        try:
            self.magic = data[0:2]
            assert self.magic == MAGIC

            self.length = struct.unpack(">H", data[2:4])[0]
            assert self.length == len(data)

            self.packet_type = data[4:6]
            assert self.packet_type == bytes([0x70, 0x6b]) or \
                self.packet_type == bytes([0x64, 0x6b])

            self.crc = binascii.crc32(data[42:]) & 0xFFFFFFFF
            data_crc = struct.unpack(">I", data[6:10])[0]
            assert self.crc == data_crc
        except AssertionError:
            _LOGGER.error("Bad packet (len=%d): %s", len(data), data.hex())
            raise

        self.session_id = data[10:42]

        current_key = DEFAULT_KEY.encode("utf-8")
        if self.packet_type == bytes([0x64, 0x6b]):
            current_key = keys[self.session_id.decode('utf-8')]

        if data[42:]:
            self.json_payload = self.decrypt_payload(current_key, data[42:])
        else:
            self.json_payload = None

    @classmethod
    def parse_length(cls, data: bytes):
        try:
            magic = data[0:2]
            assert magic == MAGIC
            length = struct.unpack(">H", data[2:4])[0]
            return length
        except Exception as e:
            _LOGGER.error("Bad packet: %s", str(e))
            raise

    @classmethod
    def decrypt_payload(cls, key: bytes, encrypted_payload: bytes):
        decryptor = Cipher(
            algorithms.AES(key),
            modes.ECB(),
            backend=default_backend()
        ).decryptor()
        data = decryptor.update(encrypted_payload)
        unpadder = padding.PKCS7(128).unpadder()
        unpad = unpadder.update(data)
        unpad += unpadder.finalize()

        if unpad[-1] == 0x00:
            unpad = unpad[:-1]
        return json.loads(unpad.decode('utf-8'))

    @classmethod
    def encrypt_payload(cls, key: bytes, payload: str):
        data = payload.encode('utf-8')

        padder = padding.PKCS7(128).padder()
        padded_data = padder.update(data)
        padded_data += padder.finalize()

        encryptor = Cipher(
            algorithms.AES(key),
            modes.ECB(),
            backend=default_backend()
        ).encryptor()

        encrypted_payload = encryptor.update(padded_data)
        return encrypted_payload

    @classmethod
    def build_packet(cls, packet_type: bytes, key: bytes, session_id: bytes, payload: dict):
        payload_str = json.dumps(payload, separators=(',', ':'))
        encrypted_payload = cls.encrypt_payload(key, payload_str)
        crc = struct.pack('>I', binascii.crc32(encrypted_payload) & 0xFFFFFFFF)
        length = struct.pack('>H', len(encrypted_payload) + len(MAGIC + packet_type + crc + session_id) + 2)

        packet = MAGIC + length + packet_type + crc + session_id + encrypted_payload
        return packet


class HomemateJsonData:
    @classmethod
    def ssl_get_session(cls):
        serial = generate_serial()
        uniSerial = generate_serial(use_time=True)
        identifier = generate_uuid()[:12]

        payload = {
            "source": SOFTWARE_NAME,
            "softwareVersion": SOFTWARE_VERSION,
            "sysVersion": SYS_VERSION,
            "hardwareVersion": HARDWARE_VERSION,
            "language": LANGUAGE,
            "identifier": identifier,
            "phoneName": PHONE_NAME,
            "cmd": CMD_HELLO,
            "serial": serial,
            "clientType": 1,
            "uniSerial": uniSerial,
            "serverRecord": False,
            "ver": SOFTWARE_VER,
            "debugInfo": DEBUG_INFO,
        }
        return payload

    @classmethod
    def ssl_control_switch(cls, username: str, device_id: str, device_uid: str, state: bool):
        """开关控制（set property 格式，适用于 type=501/135/136 等）。"""
        serial = generate_serial()
        uniSerial = generate_serial(use_time=True)
        payload = {
            "uid": device_uid,
            "userName": username,
            "deviceId": device_id,
            "groupId": "",
            "order": "set property",
            "value1": 0,
            "value2": 0,
            "value3": 0,
            "value4": 0,
            "delayTime": 0,
            "qualityOfService": 1,
            "defaultResponse": 1,
            "propertyResponse": 0,
            "properties": {"onoff": {"status": "on" if state else "off"}},
            "cmd": CMD_CONTROL,
            "serial": serial,
            "clientType": 1,
            "uniSerial": uniSerial,
            "serverRecord": False,
            "ver": SOFTWARE_VER,
            "debugInfo": DEBUG_INFO,
        }
        return payload

    @classmethod
    def ssl_control_dimmable_light_brightness(cls, username: str, device_id: str, device_uid: str, brightness_percent: int):
        """可调光灯亮度控制（set property 格式，适用于 type=502）。

        Args:
            brightness_percent: 亮度百分比 0-100
        """
        serial = generate_serial()
        uniSerial = generate_serial(use_time=True)
        bri = max(0, min(int(brightness_percent), 100))
        payload = {
            "uid": device_uid,
            "userName": username,
            "deviceId": device_id,
            "groupId": "",
            "order": "set property",
            "value1": 0,
            "value2": 0,
            "value3": 0,
            "value4": 0,
            "delayTime": 0,
            "qualityOfService": 1,
            "defaultResponse": 1,
            "propertyResponse": 0,
            "properties": {"brightness": {"percent": bri}},
            "cmd": CMD_CONTROL,
            "serial": serial,
            "clientType": 1,
            "uniSerial": uniSerial,
            "serverRecord": False,
            "ver": SOFTWARE_VER,
            "debugInfo": DEBUG_INFO,
        }
        return payload

    @classmethod
    def ssl_control_zigbee_dimmable_light_onoff(cls, username: str, device_id: str, device_uid: str, state: bool, brightness: int = 255):
        """Zigbee调光灯开关控制（on/off 格式，适用于 deviceType=0, subDeviceType=-2）"""
        serial = generate_serial()
        uniSerial = generate_serial(use_time=True)
        bri = max(0, min(int(brightness), 255))
        payload = {
            "uid": device_uid,
            "userName": username,
            "deviceId": device_id,
            "groupId": "",
            "order": "on" if state else "off",
            "value1": 0 if state else 1,
            "value2": bri,
            "value3": 0,
            "value4": 0,
            "delayTime": 0,
            "qualityOfService": 1,
            "defaultResponse": 1,
            "propertyResponse": 0,
            "cmd": CMD_CONTROL,
            "serial": serial,
            "clientType": 1,
            "uniSerial": uniSerial,
            "serverRecord": False,
            "ver": SOFTWARE_VER,
            "debugInfo": DEBUG_INFO,
        }
        return payload

    @classmethod
    def ssl_control_zigbee_dimmable_light_brightness(cls, username: str, device_id: str, device_uid: str, brightness_255: int):
        """Zigbee调光灯亮度控制（move to level 格式，适用于 deviceType=0, subDeviceType=-2）"""
        serial = generate_serial()
        uniSerial = generate_serial(use_time=True)
        bri = max(1, min(int(brightness_255), 255))
        payload = {
            "uid": device_uid,
            "userName": username,
            "deviceId": device_id,
            "groupId": "",
            "order": "move to level",
            "value1": 0,
            "value2": bri,
            "value3": 0,
            "value4": 0,
            "delayTime": 0,
            "qualityOfService": 1,
            "defaultResponse": 1,
            "propertyResponse": 0,
            "cmd": CMD_CONTROL,
            "serial": serial,
            "clientType": 1,
            "uniSerial": uniSerial,
            "serverRecord": False,
            "ver": SOFTWARE_VER,
            "debugInfo": DEBUG_INFO,
        }
        return payload

    @classmethod
    def ssl_control_fast_move_dim_color_light_onoff(cls, username: str, device_id: str, device_uid: str, state: bool, brightness: int = 0, colortemp_mired: int = 0):
        """Fast Move调光调色灯开关控制（on/off 格式，适用于 statusType=2, subDeviceType=6）"""
        serial = generate_serial()
        uniSerial = generate_serial(use_time=True)
        payload = {
            "uid": device_uid,
            "userName": username,
            "deviceId": device_id,
            "groupId": "",
            "order": "on" if state else "off",
            "value1": 0 if state else 1,
            "value2": int(brightness),
            "value3": int(colortemp_mired),
            "value4": 0,
            "delayTime": 0,
            "qualityOfService": 1,
            "defaultResponse": 1,
            "propertyResponse": 0,
            "properties": {},
            "cmd": CMD_CONTROL,
            "serial": serial,
            "clientType": 1,
            "uniSerial": uniSerial,
            "serverRecord": False,
            "ver": SOFTWARE_VER,
            "debugInfo": DEBUG_INFO,
        }
        return payload

    @classmethod
    def ssl_control_fast_move_dim_color_light_brightness(cls, username: str, device_id: str, device_uid: str, brightness: int, colortemp_mired: int = 0):
        """Fast Move调光调色灯亮度控制（fast move to level 格式，适用于 statusType=2, subDeviceType=6）"""
        serial = generate_serial()
        uniSerial = generate_serial(use_time=True)
        bri = max(0, min(int(brightness), 255))
        payload = {
            "uid": device_uid,
            "userName": username,
            "deviceId": device_id,
            "groupId": "",
            "order": "fast move to level",
            "value1": 0,
            "value2": bri,
            "value3": int(colortemp_mired),
            "value4": 0,
            "delayTime": 0,
            "qualityOfService": 1,
            "defaultResponse": 1,
            "propertyResponse": 0,
            "properties": {},
            "cmd": CMD_CONTROL,
            "serial": serial,
            "clientType": 1,
            "uniSerial": uniSerial,
            "serverRecord": False,
            "ver": SOFTWARE_VER,
            "debugInfo": DEBUG_INFO,
        }
        return payload

    @classmethod
    def ssl_control_fast_move_dim_color_light_colortemp(cls, username: str, device_id: str, device_uid: str, brightness: int, colortemp_mired: int):
        """Fast Move调光调色灯色温控制（fast color temperature 格式，适用于 statusType=2, subDeviceType=6）"""
        serial = generate_serial()
        uniSerial = generate_serial(use_time=True)
        bri = max(0, min(int(brightness), 255))
        payload = {
            "uid": device_uid,
            "userName": username,
            "deviceId": device_id,
            "groupId": "",
            "order": "fast color temperature",
            "value1": 0,
            "value2": bri,
            "value3": int(colortemp_mired),
            "value4": 0,
            "delayTime": 0,
            "qualityOfService": 1,
            "defaultResponse": 1,
            "propertyResponse": 0,
            "properties": {},
            "cmd": CMD_CONTROL,
            "serial": serial,
            "clientType": 1,
            "uniSerial": uniSerial,
            "serverRecord": False,
            "ver": SOFTWARE_VER,
            "debugInfo": DEBUG_INFO,
        }
        return payload

    @classmethod
    def ssl_control_cct_light_onoff(cls, username: str, device_id: str, device_uid: str, state: bool):
        """色温灯开关控制（set property 格式，适用于 statusType=503）"""
        serial = generate_serial()
        uniSerial = generate_serial(use_time=True)
        payload = {
            "uid": device_uid,
            "userName": username,
            "deviceId": device_id,
            "groupId": "",
            "order": "set property",
            "value1": 0,
            "value2": 0,
            "value3": 0,
            "value4": 0,
            "delayTime": 0,
            "qualityOfService": 1,
            "defaultResponse": 1,
            "propertyResponse": 0,
            "properties": {"onoff": {"status": "on" if state else "off"}},
            "cmd": CMD_CONTROL,
            "serial": serial,
            "clientType": 1,
            "uniSerial": uniSerial,
            "serverRecord": False,
            "ver": SOFTWARE_VER,
            "debugInfo": DEBUG_INFO,
        }
        return payload

    @classmethod
    def ssl_control_cct_light_brightness(cls, username: str, device_id: str, device_uid: str, brightness_percent: int):
        """色温灯亮度控制（set property 格式，适用于 statusType=503）"""
        serial = generate_serial()
        uniSerial = generate_serial(use_time=True)
        bri = max(0, min(int(brightness_percent), 100))
        payload = {
            "uid": device_uid,
            "userName": username,
            "deviceId": device_id,
            "groupId": "",
            "order": "set property",
            "value1": 0,
            "value2": 0,
            "value3": 0,
            "value4": 0,
            "delayTime": 0,
            "qualityOfService": 1,
            "defaultResponse": 1,
            "propertyResponse": 0,
            "properties": {"brightness": {"percent": bri}},
            "cmd": CMD_CONTROL,
            "serial": serial,
            "clientType": 1,
            "uniSerial": uniSerial,
            "serverRecord": False,
            "ver": SOFTWARE_VER,
            "debugInfo": DEBUG_INFO,
        }
        return payload

    @classmethod
    def ssl_control_cct_light_colortemp(cls, username: str, device_id: str, device_uid: str, colortemp_k: int):
        """色温灯色温控制（set property 格式，适用于 statusType=503）"""
        serial = generate_serial()
        uniSerial = generate_serial(use_time=True)
        ct = max(2700, min(int(colortemp_k), 6500))
        payload = {
            "uid": device_uid,
            "userName": username,
            "deviceId": device_id,
            "groupId": "",
            "order": "set property",
            "value1": 0,
            "value2": 0,
            "value3": 0,
            "value4": 0,
            "delayTime": 0,
            "qualityOfService": 1,
            "defaultResponse": 1,
            "propertyResponse": 0,
            "properties": {"colorTemp": {"value": ct}},
            "cmd": CMD_CONTROL,
            "serial": serial,
            "clientType": 1,
            "uniSerial": uniSerial,
            "serverRecord": False,
            "ver": SOFTWARE_VER,
            "debugInfo": DEBUG_INFO,
        }
        return payload

    @classmethod
    def ssl_control_light(cls, username: str, device_id: str, device_uid: str, state: bool, brightness: int = 0, colortemp_mired: int = 0):
        """灯光开关控制（order=on/off + value1 格式，适用于 type=102/38 等）。"""
        serial = generate_serial()
        uniSerial = generate_serial(use_time=True)
        payload = {
            "uid": device_uid,
            "userName": username,
            "deviceId": device_id,
            "order": "on" if state else "off",
            "value1": 0 if state else 1,
            "value2": brightness,
            "value3": colortemp_mired,
            "value4": 0,
            "delayTime": 0,
            "qualityOfService": 1,
            "defaultResponse": 1,
            "propertyResponse": 0,
            "cmd": CMD_CONTROL,
            "serial": serial,
            "clientType": 1,
            "uniSerial": uniSerial,
            "serverRecord": False,
            "ver": SOFTWARE_VER,
            "debugInfo": DEBUG_INFO,
        }
        return payload

    @classmethod
    def ssl_control_cover(cls, username: str, device_id: str, device_uid: str, position: int):
        """控制窗帘（position: 0-100，或使用字符串 'stop' 停止）"""
        serial = generate_serial()
        uniSerial = generate_serial(use_time=True)
        if position == 0:
            order = "close"
            value1 = 0
        elif isinstance(position, str) and position == "stop":
            order = "stop"
            value1 = 0
        else:
            order = "open"
            value1 = int(position)
        payload = {
            "uid": device_uid,
            "userName": username,
            "deviceId": device_id,
            "groupId": "",
            "order": order,
            "value1": value1,  # 位置百分比 0-100
            "value2": 0,
            "value3": 0,
            "value4": 0,
            "delayTime": 0,
            "qualityOfService": 1,
            "defaultResponse": 1,
            "propertyResponse": 0,
            "cmd": CMD_CONTROL,
            "serial": serial,
            "clientType": 1,
            "uniSerial": uniSerial,
            "serverRecord": False,
            "ver": SOFTWARE_VER,
            "debugInfo": DEBUG_INFO,
        }
        return payload

    @classmethod
    def ssl_control_ventilation(cls, username: str, device_id: str, device_uid: str, value1: int):
        """新风系统控制（set property 格式，适用于 deviceType=516, classId=1114）。
        value1: 0=慢, 50=停, 100=快
        """
        serial = generate_serial()
        uniSerial = generate_serial(use_time=True)
        payload = {
            "uid": device_uid,
            "userName": username,
            "deviceId": device_id,
            "groupId": "",
            "order": "set property",
            "value1": value1,
            "value2": 0,
            "value3": 0,
            "value4": 0,
            "delayTime": 0,
            "qualityOfService": 1,
            "defaultResponse": 1,
            "propertyResponse": 0,
            "cmd": CMD_CONTROL,
            "serial": serial,
            "clientType": 1,
            "uniSerial": uniSerial,
            "serverRecord": False,
            "ver": SOFTWARE_VER,
            "debugInfo": DEBUG_INFO,
        }
        return payload

    @classmethod
    def ssl_control_light_brightness(cls, username: str, device_id: str, device_uid: str, brightness: int):
        """设置灯光亮度（范围：0-255）"""
        serial = generate_serial()
        uniSerial = generate_serial(use_time=True)
        brightness_val = min(int(brightness), 255)
        payload = {
            "uid": device_uid,
            "userName": username,
            "deviceId": device_id,
            "groupId": "",
            "order": "on",
            "value1": 0,
            "value2": brightness_val,
            "value3": 0,
            "value4": 0,
            "delayTime": 0,
            "qualityOfService": 1,
            "defaultResponse": 1,
            "propertyResponse": 0,
            "cmd": CMD_CONTROL,
            "serial": serial,
            "clientType": 1,
            "uniSerial": uniSerial,
            "serverRecord": False,
            "ver": SOFTWARE_VER,
            "debugInfo": DEBUG_INFO,
        }
        return payload

    @classmethod
    def ssl_control_light_colortemp(cls, username: str, device_id: str, device_uid: str, colortemp_k: int, brightness: int = 0):
        """设置灯光色温（单位：Kelvin，内部转为 Mired 发送）。

        注意：type=38 控制命令不能包含 properties 字段，order 使用 "fast color temperature"。
        """
        serial = generate_serial()
        uniSerial = generate_serial(use_time=True)
        colortemp_mired = 1000000 // colortemp_k if colortemp_k > 0 else 0
        brightness_val = min(int(brightness), 255) if brightness else 0
        payload = {
            "uid": device_uid,
            "userName": username,
            "deviceId": device_id,
            "groupId": "",
            "order": "fast color temperature",
            "value1": 0,
            "value2": brightness_val,
            "value3": colortemp_mired,
            "value4": 0,
            "delayTime": 0,
            "qualityOfService": 1,
            "defaultResponse": 1,
            "propertyResponse": 0,
            "cmd": CMD_CONTROL,
            "serial": serial,
            "clientType": 1,
            "uniSerial": uniSerial,
            "serverRecord": False,
            "ver": SOFTWARE_VER,
            "debugInfo": DEBUG_INFO,
        }
        return payload

    @classmethod
    def ssl_control_light_full(cls, username: str, device_id: str, device_uid: str,
                                brightness: int = None, colortemp_k: int = None, power: bool = True):
        """设置灯光完整参数（亮度+色温+开关）。

        注意：type=38 控制命令 value3 直接使用 Kelvin，order 使用 "on"/"off"，
        不能包含 properties 字段。
        """
        serial = generate_serial()
        uniSerial = generate_serial(use_time=True)
        colortemp_val = colortemp_k if colortemp_k and colortemp_k > 0 else 0
        brightness_val = brightness if brightness else 0
        payload = {
            "uid": device_uid,
            "userName": username,
            "deviceId": device_id,
            "groupId": "",
            "order": "on" if power else "off",
            "value1": 0 if power else 1,
            "value2": brightness_val,
            "value3": colortemp_val,
            "value4": 0,
            "delayTime": 0,
            "qualityOfService": 1,
            "defaultResponse": 1,
            "propertyResponse": 0,
            "cmd": CMD_CONTROL,
            "serial": serial,
            "clientType": 1,
            "uniSerial": uniSerial,
            "serverRecord": False,
            "ver": SOFTWARE_VER,
            "debugInfo": DEBUG_INFO,
        }
        return payload

    @classmethod
    def ssl_clothes_horse_control(cls, username: str, device_id: str, device_uid: str, ctrl_field: str, ctrl_value: str):
        """构建晾衣架控制命令(cmd=98)。

        Args:
            ctrl_field: 控制字段名，如 lightingCtrl/sterilizingCtrl/windDryingCtrl/heatDryingCtrl/mainSwitchCtrl/motorCtrl
            ctrl_value: 控制值，如 on/off/up/down/stop
        """
        serial = generate_serial()
        uniSerial = generate_serial(use_time=True)
        payload = {
            "uid": device_uid,
            "userName": username,
            "deviceId": device_id,
            ctrl_field: ctrl_value,
            "cmd": CMD_CLOTHES_HORSE_CONTROL,
            "serial": serial,
            "clientType": 1,
            "uniSerial": uniSerial,
            "serverRecord": False,
            "ver": SOFTWARE_VER,
            "debugInfo": DEBUG_INFO,
        }
        return payload

    @classmethod
    def ssl_clothes_horse_query(cls, device_id: str):
        """构建晾衣架状态查询命令(cmd=100)。"""
        serial = generate_serial()
        uniSerial = generate_serial(use_time=True)
        payload = {
            "deviceList": [{"deviceId": device_id}],
            "cmd": CMD_CLOTHES_HORSE_QUERY,
            "serial": serial,
            "clientType": 1,
            "uniSerial": uniSerial,
            "serverRecord": False,
            "ver": SOFTWARE_VER,
            "debugInfo": DEBUG_INFO,
        }
        return payload

    @classmethod
    def ssl_login(cls, username: str, password_md5: str, family_id: str):
        serial = generate_serial()
        payload = {
            "userName": username,
            "password": password_md5,
            "cmd": CMD_LOGIN,
            "serial": serial,
            "clientType": 1,
            "source": SOFTWARE_VER,
        }
        return payload

    @classmethod
    def ssl_heartbeat(cls):
        """构建心跳包(cmd=32)。"""
        serial = generate_serial()
        uniSerial = generate_serial(use_time=True)
        payload = {
            "cmd": CMD_HEARTBEAT,
            "serial": serial,
            "clientType": 1,
            "uniSerial": uniSerial,
            "serverRecord": False,
            "ver": SOFTWARE_VER,
            "debugInfo": DEBUG_INFO,
        }
        return payload

    @classmethod
    def create_sign(cls, params, key=SIGN_KEY):
        sorted_keys = sorted(params.keys())
        sb = []
        for k in sorted_keys:
            value = params[k]
            if not text_utils_is_empty(value):
                sb.append(f"{k}={value}&")
        sb.append(f"key={key}")
        sign_str = ''.join(sb)
        sign = hmac_sha256(key, sign_str)
        return sign

    @classmethod
    def get_access_token_by_password(cls, username: str, password: str):
        url = f"https://{get_api_host()}/getOauthToken?userName={username}&type=0&password={password}"
        _LOGGER.debug(f"请求access_token: userName={username}, type=0 (password masked)")
        return {"url": url, "data": None}

    @classmethod
    def get_access_token_by_session_id(cls, session_id):
        url = f"https://{get_api_host()}/getOauthToken?type=0&sessionId={session_id}"
        return {"url": url, "data": None}

    @classmethod
    def get_family_statistics_users(cls, user_id, access_token):
        url = f"https://{get_api_host()}/v2/family/statistics/users"

        timestamp = generate_timestamp()
        random_str = generate_uuid()
        req_data = {
            "accessToken": access_token,
            "random": random_str,
            "userId": user_id,
            "sign": "1234567890",
            "timestamp": timestamp,
            "requestId": generate_uuid()
        }
        params = {
            "requestId": req_data["requestId"],
            "userId": req_data["userId"],
            "accessToken": req_data["accessToken"],
            "random": req_data["random"],
            "timestamp": req_data["timestamp"]
        }

        sign = cls.create_sign(params)
        req_data["sign"] = sign
        postData_str = json.dumps(req_data, ensure_ascii=False, indent=None)
        return {"url": url, "data": postData_str}

    @classmethod
    def get_homepage_data(cls, family_id, user_id, access_token):
        url = f"https://{get_api_host()}/v2/family/config/queryHomepageData"

        timestamp = generate_timestamp()
        random_str = generate_uuid()
        req_data = {
            "accessToken": access_token,
            "random": random_str,
            "userId": user_id,
            "familyId": family_id,
            "sign": "1234567890",
            "timestamp": timestamp,
            "requestId": generate_uuid()
        }
        params = {
            "requestId": req_data["requestId"],
            "userId": req_data["userId"],
            "accessToken": req_data["accessToken"],
            "familyId": req_data["familyId"],
            "random": req_data["random"],
            "timestamp": req_data["timestamp"]
        }

        sign = cls.create_sign(params)
        req_data["sign"] = sign
        postData_str = json.dumps(req_data, ensure_ascii=False, indent=None)

        return {"url": url, "data": postData_str}

    @classmethod
    def get_devices_status(cls, access_token, session_id, user_id, user_name, family_id, device_flag=0):
        """获取设备状态列表，通过 /v2/cmd/app/readtable API"""
        url = f"https://{get_api_host()}/v2/cmd/app/readtable"

        random_str = generate_uuid()
        serial = generate_serial()
        timestamp = generate_timestamp()

        # lastUpdateTime 设置为一个较早的时间，以确保获取所有设备
        lastUpdateTime = 0

        req_data = {
            "accessToken": access_token,
            "random": random_str,
            "serial": serial,
            "userId": user_id,
            "userName": user_name,
            "lastUpdateTime": lastUpdateTime,
            "ver": SOFTWARE_VER,
            "sign": "1234567890",
            "timestamp": timestamp,
            "sessionId": session_id,
            "deviceFlag": device_flag,
            "familyId": family_id,
            "pageIndex": 0,
            "dataType": "all"
        }
        # 参数加密前排序
        params = {
            "accessToken": req_data["accessToken"],
            "dataType": req_data["dataType"],
            "deviceFlag": req_data["deviceFlag"],
            "familyId": req_data["familyId"],
            "lastUpdateTime": req_data["lastUpdateTime"],
            "pageIndex": req_data["pageIndex"],
            "random": req_data["random"],
            "serial": req_data["serial"],
            "sessionId": req_data["sessionId"],
            "timestamp": req_data["timestamp"],
            "userId": req_data["userId"],
            "userName": req_data["userName"],
            "ver": req_data["ver"]
        }

        sign = cls.create_sign(params)
        req_data["sign"] = sign
        postData_str = json.dumps(req_data, ensure_ascii=False, indent=None)

        return {"url": url, "data": postData_str}