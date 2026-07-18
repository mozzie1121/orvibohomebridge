"""纯命令映射：Orvibo 设备控制指令构造。

零外部依赖（仅 Python 标准库），可在无 Home Assistant 环境下独立测试。

包含：
- OrviboControlCommand dataclass（命令参数封装）
- 各种设备类型的控制命令构造函数
- value 状态解析函数（value1→开关状态等）
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


# ---- 色温常量 ----

KELVIN_MIN = 2700
KELVIN_MAX = 6500
MIRED_MIN = 154  # 6500K
MIRED_MAX = 370  # 2700K


def kelvin_to_mired(kelvin: int) -> int:
    """将开尔文色温转换为 mired 单位。"""
    return round(1_000_000 / max(1, int(kelvin)))


def mired_to_kelvin(mired: int) -> int | None:
    """将 mired 值转换为开尔文，无效值返回 None。"""
    if mired <= 0:
        return None
    k = round(1_000_000 / mired)
    if k < KELVIN_MIN:
        return KELVIN_MIN
    if k > KELVIN_MAX:
        return KELVIN_MAX
    return k


def clamp_brightness(value: int, max_val: int = 255) -> int:
    """将亮度限制在有效范围内。"""
    return max(1, min(max_val, int(value)))


# ---- 控制命令数据模型 ----

@dataclass(frozen=True, slots=True)
class OrviboControlCommand:
    """封装一条 cmd=15 控制命令的 payload 参数。"""

    order: str
    value1: int = 0
    value2: int = 0
    value3: int = 0
    value4: int = 0
    properties: Mapping[str, Any] | None = None


# ---- 窗帘控制 ----

def curtain_position_command(ha_position: int) -> OrviboControlCommand:
    """窗帘位置命令，0-100 开度。"""
    position = max(0, min(100, int(ha_position)))
    return OrviboControlCommand("open", position)


def curtain_stop_command() -> OrviboControlCommand:
    """窗帘停止命令。"""
    return OrviboControlCommand("stop", 0)


def curtain_position_from_orvibo(value: int | None) -> int | None:
    """将 Orvibo 的窗帘位置值转为 HA 标准（0-100）。"""
    if value is None or not 0 <= value <= 100:
        return None
    return value


# ---- 开关控制（set property 格式） ----

def switch_power_command(is_on: bool) -> OrviboControlCommand:
    """开关控制（set property 格式，type=501/135/136/502）。"""
    return OrviboControlCommand(
        "set property",
        0, 0, 0, 0,
        properties={"onoff": {"status": "on" if is_on else "off"}},
    )


def dimmable_light_brightness_command(percent: int) -> OrviboControlCommand:
    """可调光灯亮度（set property 格式，type=502）。"""
    bri = max(0, min(100, int(percent)))
    return OrviboControlCommand(
        "set property",
        0, 0, 0, 0,
        properties={"brightness": {"percent": bri}},
    )


# ---- 色温灯控制（set property 格式，type=503） ----

def cct_light_power_command(is_on: bool) -> OrviboControlCommand:
    """色温灯开关（set property 格式，type=503）。"""
    return OrviboControlCommand(
        "set property",
        0, 0, 0, 0,
        properties={"onoff": {"status": "on" if is_on else "off"}},
    )


def cct_light_brightness_command(percent: int) -> OrviboControlCommand:
    """色温灯亮度（set property 格式，type=503）。"""
    bri = max(0, min(100, int(percent)))
    return OrviboControlCommand(
        "set property",
        0, 0, 0, 0,
        properties={"brightness": {"percent": bri}},
    )


def cct_light_colortemp_command(kelvin: int) -> OrviboControlCommand:
    """色温灯色温（set property 格式，type=503）。"""
    k = max(KELVIN_MIN, min(KELVIN_MAX, int(kelvin)))
    return OrviboControlCommand(
        "set property",
        0, 0, 0, 0,
        properties={"colorTemp": {"value": kelvin_to_mired(k)}},
    )


# ---- 灯光控制（order=on/off 格式，type=1/38/102） ----

def light_power_command(is_on: bool, brightness: int = 0, color_temp_mired: int = 0) -> OrviboControlCommand:
    """灯开关（order=on/off，active-low）。

    type=38/102: value1=0 开, value1=1 关
    type=1: 同上
    """
    return OrviboControlCommand(
        "on" if is_on else "off",
        0 if is_on else 1,
        brightness,
        color_temp_mired,
    )


def light_brightness_command(brightness: int, color_temp_mired: int = 0) -> OrviboControlCommand:
    """灯亮度（fast move to level 格式）。"""
    return OrviboControlCommand(
        "fast move to level",
        0,
        clamp_brightness(brightness),
        color_temp_mired,
    )


def light_colortemp_command(brightness: int, color_temp_mired: int) -> OrviboControlCommand:
    """灯色温（fast color temperature 格式）。"""
    return OrviboControlCommand(
        "fast color temperature",
        0,
        clamp_brightness(brightness),
        color_temp_mired,
    )


def light_full_command(
    is_on: bool, brightness: int, color_temp_mired: int,
) -> OrviboControlCommand:
    """灯全量控制（同时设置开关+亮度+色温）。"""
    return OrviboControlCommand(
        "on" if is_on else "off",
        0 if is_on else 1,
        clamp_brightness(brightness),
        color_temp_mired,
    )


# ---- Zigbee 调光灯控制（type=0, subDeviceType=-2） ----

def zigbee_dimmable_light_power_command(is_on: bool, brightness: int = 255) -> OrviboControlCommand:
    """Zigbee调光灯开关（on/off + brightness）。"""
    return OrviboControlCommand(
        "on" if is_on else "off",
        0 if is_on else 1,
        clamp_brightness(brightness),
    )


def zigbee_dimmable_light_brightness_command(brightness: int) -> OrviboControlCommand:
    """Zigbee调光灯亮度（move to level 格式）。"""
    return OrviboControlCommand(
        "move to level",
        0,
        clamp_brightness(brightness),
    )


# ---- Fast Move 调光调色灯控制（type=2, subType=6） ----

def fast_move_light_power_command(
    is_on: bool, brightness: int = 0, colortemp_mired: int = 0,
) -> OrviboControlCommand:
    """Fast Move 灯开关（on/off + brightness + mired）。"""
    return OrviboControlCommand(
        "on" if is_on else "off",
        0 if is_on else 1,
        clamp_brightness(brightness),
        colortemp_mired,
    )


def fast_move_light_brightness_command(brightness: int, colortemp_mired: int = 0) -> OrviboControlCommand:
    """Fast Move 灯亮度（fast move to level）。"""
    return OrviboControlCommand(
        "fast move to level",
        0,
        clamp_brightness(brightness),
        colortemp_mired,
    )


def fast_move_light_colortemp_command(brightness: int, colortemp_mired: int) -> OrviboControlCommand:
    """Fast Move 灯色温（fast color temperature）。"""
    return OrviboControlCommand(
        "fast color temperature",
        0,
        clamp_brightness(brightness),
        colortemp_mired,
    )


# ---- 新风控制（type=516） ----

def ventilation_command(value1: int) -> OrviboControlCommand:
    """新风系统控制。value1: 0=慢, 50=停, 100=快。"""
    return OrviboControlCommand(
        "set property", int(value1),
    )


# ---- 空调控制（type=36） ----

def fan_coil_ac_power_command(
    is_on: bool, mode: int = 3, fan_speed: int = 1, temp_value4: int = 0,
) -> OrviboControlCommand:
    """风机盘管空调开关。

    Args:
        is_on: True=开, False=关
        mode: 模式码 (2=除湿, 3=制冷, 4=制热, 7=送风)
        fan_speed: 风速码 (1=低, 2=中, 3=高)
        temp_value4: 温度编码 (temp*100)<<16
    """
    return OrviboControlCommand(
        "on" if is_on else "off",
        0 if is_on else 1,
        int(mode),
        int(fan_speed),
        int(temp_value4),
    )


def fan_coil_ac_mode_command(mode: int, temp_value4: int) -> OrviboControlCommand:
    """空调模式设置。"""
    return OrviboControlCommand(
        "mode setting",
        0, int(mode),
        value4=int(temp_value4),
    )


def fan_coil_ac_temperature_command(
    mode: int, fan_speed: int, temp_value4: int,
) -> OrviboControlCommand:
    """空调温度设置。"""
    return OrviboControlCommand(
        "temperature setting",
        0, int(mode), int(fan_speed),
        int(temp_value4),
    )


def fan_coil_ac_fan_speed_command(
    mode: int, fan_speed: int, temp_value4: int,
) -> OrviboControlCommand:
    """空调风速设置。"""
    return OrviboControlCommand(
        "wind setting",
        0, int(mode), int(fan_speed),
        int(temp_value4),
    )


def ac_temperature_encode(temperature_celsius: float) -> int:
    """将空调温度编码为 value4 格式。"""
    return int(temperature_celsius * 100) << 16


AC_MODE_MAP = {"dehumidify": 2, "cool": 3, "heat": 4, "fan_only": 7}
AC_MODE_REVERSE = {2: "dehumidify", 3: "cool", 4: "heat", 7: "fan_only"}
AC_FAN_SPEED_MAP = {"low": 1, "medium": 2, "high": 3}
AC_FAN_SPEED_REVERSE = {1: "low", 2: "medium", 3: "high"}


# ---- 状态解析函数 ----

def light_is_on_from_value1(value1: int | None) -> bool | None:
    """从 value1 推断 active-low 灯开关状态。

    type=38/102/503: value1=0→开, value1=1→关
    """
    if value1 not in (0, 1):
        return None
    return value1 == 0


def switch_is_on_from_value1(value1: int | None) -> bool | None:
    """从 value1 推断 active-high 开关状态。

    type=135/136: value1=0→关, value1=1→开
    """
    if value1 not in (0, 1):
        return None
    return value1 == 1


def curtain_position_from_value1(value1: int | None) -> int | None:
    """从 value1 推断窗帘位置（0-100）。"""
    if value1 is None or not 0 <= value1 <= 100:
        return None
    return int(value1)


def zigbee_dimmable_is_on(value1: int | None, sub_device_type: int | None) -> bool | None:
    """Zigbee 调光灯开关（subDeviceType=-2 时 active-low）。"""
    if value1 not in (0, 1):
        return None
    if sub_device_type == -2:
        return value1 == 0  # active-low
    return value1 == 1  # active-high


def ventilation_fan_speed_from_value1(value1: int | None) -> str | None:
    """新风速度：0=慢, 50=停, 100=快。"""
    if value1 is None:
        return None
    return {0: "慢", 50: "停", 100: "快"}.get(int(value1))


# ---- 构造成完整 payload（含 header 字段） ----

BUILD_PAYLOAD_FIELDS = [
    "uid", "userName", "deviceId", "groupId",
    "order", "value1", "value2", "value3", "value4",
    "delayTime", "qualityOfService", "defaultResponse", "propertyResponse",
    "cmd", "serial", "clientType", "uniSerial",
    "serverRecord", "ver", "debugInfo",
]

DEFAULT_PAYLOAD_HEADERS = {
    "groupId": "",
    "delayTime": 0,
    "qualityOfService": 1,
    "defaultResponse": 1,
    "propertyResponse": 0,
    "cmd": 15,  # CMD_CONTROL
    "clientType": 1,
    "serverRecord": False,
    "ver": "5.1.3.309",
    "debugInfo": "Android_ZhiJia365_34_5.1.3.309",
}


def build_control_payload(
    cmd: OrviboControlCommand,
    device_id: str,
    device_uid: str,
    username: str,
    serial: int,
    uni_serial: int | None = None,
) -> dict[str, Any]:
    """将 OrviboControlCommand 组装为完整的 SSL 控制 payload。

    结果可直接用于 packet.py 的 build_packet() 或 ssl_client._send_packet()。
    """
    payload: dict[str, Any] = {
        "uid": device_uid,
        "userName": username,
        "deviceId": device_id,
        "order": cmd.order,
        "value1": cmd.value1,
        "value2": cmd.value2,
        "value3": cmd.value3,
        "value4": cmd.value4,
        "serial": serial,
        "uniSerial": uni_serial if uni_serial is not None else serial,
        **DEFAULT_PAYLOAD_HEADERS,
    }
    if cmd.properties:
        payload["properties"] = cmd.properties
    return payload


def control_payload_to_command(payload: dict[str, Any]) -> OrviboControlCommand:
    """从完整的 control payload 中提取 OrviboControlCommand。"""
    return OrviboControlCommand(
        order=payload.get("order", ""),
        value1=payload.get("value1", 0),
        value2=payload.get("value2", 0),
        value3=payload.get("value3", 0),
        value4=payload.get("value4", 0),
        properties=payload.get("properties"),
    )
