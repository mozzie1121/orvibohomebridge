import os
from datetime import timedelta

_CERTS_DIR = os.path.join(os.path.dirname(__file__), "certs")

# 设置为 None 禁用自动更新，或设置一个很长的时间
UPDATE_INTERVAL = timedelta(minutes=30)  # 长间隔兜底轮询
SSL_MAX_RECONNECT_ATTEMPTS = 3

CMD_HELLO = 0
CMD_LOGIN = 2
CMD_CONTROL = 15
CMD_STATE_UPDATE = 42
CMD_HEARTBEAT = 32
CMD_HANDSHAKE = 6
CMD_GET_FAMILY = 201
CMD_GET_DEVICE_LIST = 263
CMD_CLOTHES_HORSE_CONTROL = 98
CMD_CLOTHES_HORSE_STATE = 99
CMD_CLOTHES_HORSE_QUERY = 100

SOFTWARE_NAME = "ZhiJia365"
SOFTWARE_VERSION = "50103309"
SYS_VERSION = "Android14_34"
HARDWARE_VERSION = "Google Pixel 8"
LANGUAGE = "zh"
PHONE_NAME = "Pixel 8"
SOFTWARE_VER = "5.1.3.309"
DEBUG_INFO = "Android_ZhiJia365_34_5.1.3.309"

DOMAIN = "orvibohomebridge"
MANUFACTURER = "ORVIBO"

HTTPS_HOST = "china.orvibo.com"
HTTP_HEADERS = {
    "Content-Type": "application/json; charset=utf-8",
    "User-Agent": "okhttp/3.12.8",
}
SIGN_KEY = "nQ45RjPtOws96jmH"

SSL_HOST = "china.orvibo.com"
SSL_PORT = 10002
CLIENT_CERT = os.path.join(_CERTS_DIR, "client_cert.pem")
CLIENT_KEY = os.path.join(_CERTS_DIR, "client_key.pem")
SERVER_CA = os.path.join(_CERTS_DIR, "server_ca.pem")
DEFAULT_KEY = "khggd54865SNJHGF"
MAGIC = bytes([0x68, 0x64])
ID_UNSET = b'\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20'

DEVICE_TYPE_COVER = "cover"
DEVICE_TYPE_SWITCH = "switch"
DEVICE_TYPE_LIGHT = "light"
DEVICE_TYPE_CLOTHES_HORSE = "clothes_horse"
DEVICE_TYPE_SENSOR = "sensor"
DEVICE_TYPE_CLIMATE = "climate"
DEVICE_TYPE_FAN = "fan"

# HA 平台路由映射（device_type_raw → HA 平台字符串）
# 注意：隐藏类别（114/135/136/137/143/518/14/150/511）不在本表，由
# device_types.HIDDEN_CATEGORIES 在 coordinator 层过滤。
DEVICE_TYPE_MAP = {
    1: DEVICE_TYPE_LIGHT,           # SIMPLE_ZIGBEE_LIGHT
    34: DEVICE_TYPE_COVER,          # ZIGBEE_CURTAIN
    36: DEVICE_TYPE_CLIMATE,        # FAN_COIL_AC
    38: DEVICE_TYPE_LIGHT,          # DIM_COLOR_LIGHT
    52: DEVICE_TYPE_CLOTHES_HORSE,  # CLOTHES_HORSE
    102: DEVICE_TYPE_LIGHT,         # LEGACY_LIGHT
    501: DEVICE_TYPE_LIGHT,         # MONO_LIGHT
    503: DEVICE_TYPE_LIGHT,         # CCT_LIGHT
    26: DEVICE_TYPE_SENSOR,         # MOTION_SENSOR
    27: DEVICE_TYPE_SENSOR,         # SMOKE_SENSOR
    25: DEVICE_TYPE_SENSOR,         # GAS_SENSOR
    56: DEVICE_TYPE_SENSOR,         # EMERGENCY_BUTTON
    54: DEVICE_TYPE_SENSOR,         # WATER_LEAK_SENSOR
    300: DEVICE_TYPE_SENSOR,        # TEMP_HUMIDITY_SENSOR / DOOR_LOCK
    522: DEVICE_TYPE_SENSOR,        # DOOR_LOCK (V5 Eyes)
    10086: DEVICE_TYPE_LIGHT,       # LIGHT_VIRTUAL_GROUP
    502: DEVICE_TYPE_LIGHT,         # DIMMABLE_LIGHT
    0: DEVICE_TYPE_LIGHT,           # 0-10V调光灯模块 调光模式 (deviceType=0, subDeviceType=-2)
    46: DEVICE_TYPE_SENSOR,         # DOOR_WINDOW_SENSOR
    516: DEVICE_TYPE_FAN,           # VENTILATION_SYSTEM 新风系统
}

CLASS_ID_MAP = {
    426: DEVICE_TYPE_LIGHT,
    429: DEVICE_TYPE_LIGHT,
    436: DEVICE_TYPE_LIGHT,         # CCT_LIGHT_STRIP
    1114: DEVICE_TYPE_FAN,          # VENTILATION_SYSTEM 新风系统
}

CONF_USERNAME = "username"
CONF_PASSWORD = "p455w0rd_zhijia365"
CONF_FAMILY_ID = "family_id"
