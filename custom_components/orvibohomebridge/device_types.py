"""Orvibo 设备分类 taxonomy (HA 集成版)。

按 deviceTypeId + subType 列表定义设备分类。classify_device 接受
https_client.parse_device_status_list 输出的 device dict（含 device_type_raw /
class_id / sub_device_type / ui_model 等字段），返回 DeviceCategory 枚举值；
未识别返回 UNKNOWN。
"""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional, Tuple

from .known_device_models import identify_known_device


class DeviceCategory(Enum):
    UNKNOWN = "unknown"
    SIMPLE_ZIGBEE_LIGHT = "simple_zigbee_light"        # deviceTypeId=1
    ZIGBEE_CURTAIN = "zigbee_curtain"                  # deviceTypeId=34
    ZIGBEE_ROLLING_SHUTTER = "zigbee_rolling_shutter"   # deviceTypeId=35
    FAN_COIL_AC = "fan_coil_ac"                        # deviceTypeId=36
    MIXPAD_GATEWAY = "mixpad_gateway"                  # deviceTypeId=114
    MUSIC_HOST = "music_host"                          # deviceTypeId=128
    MIX_SWITCH = "mix_switch"                          # deviceTypeId=135/136/137/143
    MONO_LIGHT = "mono_light"                          # deviceTypeId=501
    CCT_LIGHT_STRIP = "cct_light_strip"                # deviceTypeId=503
    BACH_SWITCH = "bach_switch"                        # deviceTypeId=518
    DOOR_LOCK = "door_lock"                            # deviceTypeId=522, classId=463
    WIFI_CAMERA = "wifi_camera"                        # deviceTypeId=14
    LIGHT_VIRTUAL_GROUP = "light_virtual_group"        # deviceTypeId=10086
    DIM_COLOR_LIGHT = "dim_color_light"                # deviceTypeId=38
    LEGACY_LIGHT = "legacy_light"                      # deviceTypeId=102
    LEGACY_CURTAIN = "legacy_curtain"                  # deviceTypeId=52（旧映射，保留兼容）
    CLOTHES_HORSE = "clothes_horse"                    # deviceTypeId=52，智能电动晾衣机
    MOTION_SENSOR = "motion_sensor"                    # deviceTypeId=26
    SMOKE_SENSOR = "smoke_sensor"                      # deviceTypeId=27
    EMERGENCY_BUTTON = "emergency_button"              # deviceTypeId=56
    WATER_LEAK_SENSOR = "water_leak_sensor"            # deviceTypeId=54，水浸探测器
    GAS_SENSOR = "gas_sensor"                          # deviceTypeId=25，可燃气体探测器
    SMART_REMOTE = "smart_remote"                      # deviceTypeId=150
    MIXPAD_4WAY_BASE = "mixpad_4way_base"              # deviceTypeId=511
    DIMMABLE_LIGHT = "dimmable_light"                  # deviceTypeId=502，可调光灯（仅亮度，无色温）
    ZIGBEE_DIMMABLE_LIGHT = "zigbee_dimmable_light"    # deviceTypeId=0, subDeviceType=-2，0-10v调光灯
    CCT_LIGHT = "cct_light"                            # statusType=503 subDeviceType=461，0-10v色温灯
    TEMP_HUMIDITY_SENSOR = "temp_humidity_sensor"      # deviceTypeId=300 subType=491，温湿度传感器
    DOOR_WINDOW_SENSOR = "door_window_sensor"          # deviceTypeId=46，门窗传感器
    FAST_MOVE_DIM_COLOR_LIGHT = "fast_move_dim_color_light"  # statusType=2, subDeviceType=6，fast move to level调光调色灯
    VENTILATION_SYSTEM = "ventilation_system"                # deviceType=516, classId=1114，新风系统
    FLOOR_HEATING = "floor_heating"                          # deviceType=300, subType=481
    LEGACY_FLOOR_HEATING = "legacy_floor_heating"            # deviceType=112, orb_floorheat
    DREAM_CURTAIN = "dream_curtain"                          # deviceType=506, subType=408
    OTHER = "other"


@dataclass(frozen=True)
class CategoryInfo:
    category: DeviceCategory
    label: str
    description: str
    capabilities: Tuple[str, ...]
    is_container: bool = False


@dataclass(frozen=True)
class DeviceProfile:
    """Resolved capability and hardware-verification metadata."""

    category: DeviceCategory
    info: CategoryInfo
    hardware_verified: bool
    registration_only: bool


_CATEGORY_INFO: Dict[DeviceCategory, CategoryInfo] = {
    DeviceCategory.SIMPLE_ZIGBEE_LIGHT: CategoryInfo(
        category=DeviceCategory.SIMPLE_ZIGBEE_LIGHT,
        label="简易 Zigbee 灯",
        description="deviceTypeId=1, subType=-2/1/13（筒灯/吊灯/灯带），仅开关",
        capabilities=("onoff",),
    ),
    DeviceCategory.ZIGBEE_CURTAIN: CategoryInfo(
        category=DeviceCategory.ZIGBEE_CURTAIN,
        label="Zigbee 开合窗帘",
        description="deviceTypeId=34, subType=-2，开度 0~100%",
        capabilities=("position",),
    ),
    DeviceCategory.ZIGBEE_ROLLING_SHUTTER: CategoryInfo(
        category=DeviceCategory.ZIGBEE_ROLLING_SHUTTER,
        label="Zigbee 卷帘",
        description="deviceTypeId=35，卷帘控制，同开合窗帘协议",
        capabilities=("position",),
    ),
    DeviceCategory.FAN_COIL_AC: CategoryInfo(
        category=DeviceCategory.FAN_COIL_AC,
        label="风机盘管空调面板",
        description="deviceTypeId=36，开关/温度/模式/风速",
        capabilities=("onoff", "temperature", "mode", "fan_speed"),
    ),
    DeviceCategory.MIXPAD_GATEWAY: CategoryInfo(
        category=DeviceCategory.MIXPAD_GATEWAY,
        label="MixPad 中控网关",
        description="deviceTypeId=114，Zigbee 中枢 + 背景音乐",
        capabilities=("gateway", "music"),
        is_container=True,
    ),
    DeviceCategory.MUSIC_HOST: CategoryInfo(
        category=DeviceCategory.MUSIC_HOST,
        label="独立背景音乐主机",
        description="deviceTypeId=128，播放/音量/切歌",
        capabilities=("music_play", "volume", "skip"),
    ),
    DeviceCategory.MIX_SWITCH: CategoryInfo(
        category=DeviceCategory.MIX_SWITCH,
        label="MixSwitch 超级开关",
        description="deviceTypeId=135/136/137/143，1/2/3/4 路，ui=MIXSWITCH",
        capabilities=("multi_channel_switch",),
        is_container=True,
    ),
    DeviceCategory.MONO_LIGHT: CategoryInfo(
        category=DeviceCategory.MONO_LIGHT,
        label="单色灯具",
        description="deviceTypeId=501, subType=426/429 (lightStd)，仅开关",
        capabilities=("onoff",),
    ),
    DeviceCategory.CCT_LIGHT_STRIP: CategoryInfo(
        category=DeviceCategory.CCT_LIGHT_STRIP,
        label="色温灯带",
        description="deviceTypeId=503, subType=436 (colorTempLightStd)，亮度 2700-6500K 色温",
        capabilities=("onoff", "brightness", "color_temp"),
    ),
    DeviceCategory.BACH_SWITCH: CategoryInfo(
        category=DeviceCategory.BACH_SWITCH,
        label="Bach 传统开关",
        description="deviceTypeId=518, subType=424 (8路)/1107 (4路)",
        capabilities=("multi_channel_switch",),
        is_container=True,
    ),
    DeviceCategory.DOOR_LOCK: CategoryInfo(
        category=DeviceCategory.DOOR_LOCK,
        label="智能门锁",
        description="deviceTypeId=522, classId=463，锁状态/门状态/电量",
        capabilities=("lock", "door_state", "battery"),
    ),
    DeviceCategory.WIFI_CAMERA: CategoryInfo(
        category=DeviceCategory.WIFI_CAMERA,
        label="WiFi 摄像机",
        description="deviceTypeId=14",
        capabilities=("camera",),
    ),
    DeviceCategory.LIGHT_VIRTUAL_GROUP: CategoryInfo(
        category=DeviceCategory.LIGHT_VIRTUAL_GROUP,
        label="灯光虚拟分组",
        description="deviceTypeId=10086",
        capabilities=("group_onoff",),
    ),
    DeviceCategory.DIM_COLOR_LIGHT: CategoryInfo(
        category=DeviceCategory.DIM_COLOR_LIGHT,
        label="调光调色灯",
        description="deviceTypeId=38（兼容字段），亮度+色温",
        capabilities=("onoff", "brightness", "color_temp"),
    ),
    DeviceCategory.LEGACY_LIGHT: CategoryInfo(
        category=DeviceCategory.LEGACY_LIGHT,
        label="单色普通灯",
        description="deviceTypeId=102（兼容字段），仅开关",
        capabilities=("onoff",),
    ),
    DeviceCategory.LEGACY_CURTAIN: CategoryInfo(
        category=DeviceCategory.LEGACY_CURTAIN,
        label="普通窗帘",
        description="deviceTypeId=52（兼容字段）",
        capabilities=("position",),
    ),
    DeviceCategory.CLOTHES_HORSE: CategoryInfo(
        category=DeviceCategory.CLOTHES_HORSE,
        label="智能晾衣机",
        description="deviceTypeId=52，电动晾衣机（照明/消毒/风干/热干/升降）",
        capabilities=("lighting", "sterilizing", "wind_drying", "heat_drying", "motor", "main_switch"),
    ),
    DeviceCategory.MOTION_SENSOR: CategoryInfo(
        category=DeviceCategory.MOTION_SENSOR,
        label="人体传感器",
        description="deviceTypeId=26，检测人体移动",
        capabilities=("motion_detection",),
    ),
    DeviceCategory.SMOKE_SENSOR: CategoryInfo(
        category=DeviceCategory.SMOKE_SENSOR,
        label="烟雾传感器",
        description="deviceTypeId=27，检测烟雾浓度",
        capabilities=("smoke_detection",),
    ),
    DeviceCategory.EMERGENCY_BUTTON: CategoryInfo(
        category=DeviceCategory.EMERGENCY_BUTTON,
        label="紧急按钮",
        description="deviceTypeId=56，紧急报警按钮",
        capabilities=("emergency_alarm",),
    ),
    DeviceCategory.WATER_LEAK_SENSOR: CategoryInfo(
        category=DeviceCategory.WATER_LEAK_SENSOR,
        label="水浸探测器",
        description="deviceTypeId=54，水浸传感器",
        capabilities=("water_leak_detection",),
    ),
    DeviceCategory.GAS_SENSOR: CategoryInfo(
        category=DeviceCategory.GAS_SENSOR,
        label="可燃气体探测器",
        description="deviceTypeId=25，检测可燃气体浓度",
        capabilities=("gas_detection",),
    ),
    DeviceCategory.SMART_REMOTE: CategoryInfo(
        category=DeviceCategory.SMART_REMOTE,
        label="智能遥控器",
        description="deviceTypeId=150，红外遥控设备",
        capabilities=("ir_control",),
    ),
    DeviceCategory.MIXPAD_4WAY_BASE: CategoryInfo(
        category=DeviceCategory.MIXPAD_4WAY_BASE,
        label="MixPad 四路底壳",
        description="deviceTypeId=511，MixPad 扩展底壳",
        capabilities=("multi_channel_switch",),
        is_container=True,
    ),
    DeviceCategory.DIMMABLE_LIGHT: CategoryInfo(
        category=DeviceCategory.DIMMABLE_LIGHT,
        label="可调光灯",
        description="deviceTypeId=502, subType=431，可调亮度，无色温",
        capabilities=("onoff", "brightness"),
    ),
    DeviceCategory.ZIGBEE_DIMMABLE_LIGHT: CategoryInfo(
        category=DeviceCategory.ZIGBEE_DIMMABLE_LIGHT,
        label="0-10v调光灯",
        description="deviceTypeId=0, subDeviceType=-2，可调亮度，value1/value2格式",
        capabilities=("onoff", "brightness"),
    ),
    DeviceCategory.TEMP_HUMIDITY_SENSOR: CategoryInfo(
        category=DeviceCategory.TEMP_HUMIDITY_SENSOR,
        label="温湿度传感器",
        description="deviceTypeId=300 subType=491，温度/湿度/电量",
        capabilities=("temperature", "humidity", "battery"),
    ),
    DeviceCategory.DOOR_WINDOW_SENSOR: CategoryInfo(
        category=DeviceCategory.DOOR_WINDOW_SENSOR,
        label="门窗传感器",
        description="deviceTypeId=46，门磁状态/电量",
        capabilities=("door_state", "battery"),
    ),
    DeviceCategory.FAST_MOVE_DIM_COLOR_LIGHT: CategoryInfo(
        category=DeviceCategory.FAST_MOVE_DIM_COLOR_LIGHT,
        label="Fast Move调光调色灯",
        description="statusType=2, subDeviceType=6，fast move to level协议，亮度+色温",
        capabilities=("onoff", "brightness", "color_temp"),
    ),
    DeviceCategory.VENTILATION_SYSTEM: CategoryInfo(
        category=DeviceCategory.VENTILATION_SYSTEM,
        label="新风系统",
        description="deviceType=516, classId=1114，新风系统（停/慢/快三档）",
        capabilities=("onoff", "fan_speed", "preset_mode"),
    ),
    DeviceCategory.FLOOR_HEATING: CategoryInfo(
        category=DeviceCategory.FLOOR_HEATING,
        label="地暖控制器",
        description="deviceType=300, subType=481, MixPadFloorHeat",
        capabilities=("onoff", "target_temperature", "temperature", "humidity"),
    ),
    DeviceCategory.LEGACY_FLOOR_HEATING: CategoryInfo(
        category=DeviceCategory.LEGACY_FLOOR_HEATING,
        label="地暖控制面板",
        description="deviceType=112, subType=-2, ui.model=orb_floorheat",
        capabilities=("onoff", "target_temperature", "temperature"),
    ),
    DeviceCategory.DREAM_CURTAIN: CategoryInfo(
        category=DeviceCategory.DREAM_CURTAIN,
        label="梦幻帘",
        description="deviceType=506, subType=408, 开合位置与叶片角度",
        capabilities=("position", "stop", "angle"),
    ),
    DeviceCategory.OTHER: CategoryInfo(
        category=DeviceCategory.OTHER,
        label="其他设备",
        description="已识别 deviceType 但未在用户 taxonomy 中",
        capabilities=(),
    ),
    DeviceCategory.UNKNOWN: CategoryInfo(
        category=DeviceCategory.UNKNOWN,
        label="未识别设备",
        description="无法匹配任何已知 deviceType / subType / ui.model",
        capabilities=(),
    ),
}


def get_category_info(category: DeviceCategory) -> CategoryInfo:
    return _CATEGORY_INFO.get(category, _CATEGORY_INFO[DeviceCategory.UNKNOWN])


# deviceTypeId → Category 主映射
_DEVICE_TYPE_MAP: Dict[int, DeviceCategory] = {
    1: DeviceCategory.SIMPLE_ZIGBEE_LIGHT,
    34: DeviceCategory.ZIGBEE_CURTAIN,
    35: DeviceCategory.ZIGBEE_ROLLING_SHUTTER,
    36: DeviceCategory.FAN_COIL_AC,
    81: DeviceCategory.FAN_COIL_AC,
    114: DeviceCategory.MIXPAD_GATEWAY,
    128: DeviceCategory.MUSIC_HOST,
    135: DeviceCategory.MIX_SWITCH,
    136: DeviceCategory.MIX_SWITCH,
    137: DeviceCategory.MIX_SWITCH,
    143: DeviceCategory.MIX_SWITCH,
    501: DeviceCategory.MONO_LIGHT,
    503: DeviceCategory.CCT_LIGHT,
    518: DeviceCategory.BACH_SWITCH,
    522: DeviceCategory.DOOR_LOCK,
    45: DeviceCategory.MIXPAD_GATEWAY,  # Zigbee Mini Hub (Hub100)，隐藏
    14: DeviceCategory.WIFI_CAMERA,
    10086: DeviceCategory.LIGHT_VIRTUAL_GROUP,
    38: DeviceCategory.DIM_COLOR_LIGHT,
    52: DeviceCategory.CLOTHES_HORSE,
    43: DeviceCategory.SIMPLE_ZIGBEE_LIGHT,   # COCO智能插线板 (order=on/off格式，同light控制)
    26: DeviceCategory.MOTION_SENSOR,
    27: DeviceCategory.SMOKE_SENSOR,
    25: DeviceCategory.GAS_SENSOR,
    56: DeviceCategory.EMERGENCY_BUTTON,
    54: DeviceCategory.WATER_LEAK_SENSOR,
    150: DeviceCategory.SMART_REMOTE,
    511: DeviceCategory.MIXPAD_4WAY_BASE,
    502: DeviceCategory.DIMMABLE_LIGHT,
    46: DeviceCategory.DOOR_WINDOW_SENSOR,
    516: DeviceCategory.VENTILATION_SYSTEM,
    22: DeviceCategory.TEMP_HUMIDITY_SENSOR,
    23: DeviceCategory.TEMP_HUMIDITY_SENSOR,
}

_UI_MODEL_MAP: Dict[str, DeviceCategory] = {
    "lightStd": DeviceCategory.MONO_LIGHT,
    "colorTempLightStd": DeviceCategory.CCT_LIGHT_STRIP,
    "MIXSWITCH": DeviceCategory.MIX_SWITCH,
    "orb_wifi_clotheshorse_ultimate": DeviceCategory.CLOTHES_HORSE,
}

_CLASS_ID_MAP: Dict[int, DeviceCategory] = {
    426: DeviceCategory.MONO_LIGHT,
    429: DeviceCategory.MONO_LIGHT,
    436: DeviceCategory.CCT_LIGHT_STRIP,
    424: DeviceCategory.BACH_SWITCH,
    1107: DeviceCategory.BACH_SWITCH,
    463: DeviceCategory.DOOR_LOCK,
    1114: DeviceCategory.VENTILATION_SYSTEM,
}

# 不需要展示、也不需要加入 HA 的设备类别
HIDDEN_CATEGORIES: set = {
    DeviceCategory.MIXPAD_GATEWAY,      # deviceTypeId=114
    DeviceCategory.MIX_SWITCH,          # deviceTypeId=135/136/137/143
    DeviceCategory.BACH_SWITCH,         # deviceTypeId=518
    DeviceCategory.WIFI_CAMERA,         # deviceTypeId=14
    DeviceCategory.SMART_REMOTE,        # deviceTypeId=150
    DeviceCategory.MIXPAD_4WAY_BASE,    # deviceTypeId=511
}

# README 支持表由维护者使用真机验证；未列入此集合不等于不兼容，
# 仅表示当前不作“真机验证通过”的能力承诺。
HARDWARE_VERIFIED_CATEGORIES = frozenset({
    DeviceCategory.SIMPLE_ZIGBEE_LIGHT,
    DeviceCategory.ZIGBEE_CURTAIN,
    DeviceCategory.FAN_COIL_AC,
    DeviceCategory.MIX_SWITCH,
    DeviceCategory.MONO_LIGHT,
    DeviceCategory.CCT_LIGHT_STRIP,
    DeviceCategory.CCT_LIGHT,
    DeviceCategory.BACH_SWITCH,
    DeviceCategory.DOOR_LOCK,
    DeviceCategory.DIM_COLOR_LIGHT,
    DeviceCategory.LEGACY_LIGHT,
    DeviceCategory.CLOTHES_HORSE,
    DeviceCategory.MOTION_SENSOR,
    DeviceCategory.SMOKE_SENSOR,
    DeviceCategory.EMERGENCY_BUTTON,
    DeviceCategory.WATER_LEAK_SENSOR,
    DeviceCategory.GAS_SENSOR,
    DeviceCategory.DIMMABLE_LIGHT,
    DeviceCategory.ZIGBEE_DIMMABLE_LIGHT,
    DeviceCategory.TEMP_HUMIDITY_SENSOR,
    DeviceCategory.DOOR_WINDOW_SENSOR,
    DeviceCategory.FAST_MOVE_DIM_COLOR_LIGHT,
    DeviceCategory.VENTILATION_SYSTEM,
    DeviceCategory.ZIGBEE_ROLLING_SHUTTER,
    DeviceCategory.FLOOR_HEATING,
    DeviceCategory.LEGACY_FLOOR_HEATING,
    DeviceCategory.DREAM_CURTAIN,
})


def is_hidden_category(category: DeviceCategory) -> bool:
    """判断该类别是否应被隐藏（不展示、不加入 HA）。"""
    return category in HIDDEN_CATEGORIES


def get_device_profile(device: Dict[str, Any]) -> DeviceProfile:
    """Resolve a device without granting control to unknown categories."""
    category = classify_device(device)
    known_model = identify_known_device(device)
    if category in {DeviceCategory.UNKNOWN, DeviceCategory.OTHER} and known_model:
        category = DeviceCategory.OTHER
        info = CategoryInfo(
            category=category,
            label=known_model.display_name,
            description=(
                "家庭批量审计中已识别型号；尚未验证控制协议"
                + (
                    f"（内部型号：{', '.join(known_model.internal_models)}）"
                    if known_model.internal_models
                    else ""
                )
            ),
            capabilities=(),
        )
    else:
        info = get_category_info(category)
    hardware_verified = category in HARDWARE_VERIFIED_CATEGORIES
    return DeviceProfile(
        category=category,
        info=info,
        hardware_verified=hardware_verified,
        # A catalogue/category match is identification evidence, not control
        # evidence.  Only hardware-verified profiles may leave registration-only.
        registration_only=(
            not hardware_verified
            or category == DeviceCategory.LIGHT_VIRTUAL_GROUP
        ),
    )


def _safe_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def classify_device(device: Dict[str, Any]) -> DeviceCategory:
    """根据 device dict 字段判定分类。

    优先级：
    1. deviceType 主映射（type=300 需根据 subType 进一步区分）
    2. 特殊组合：deviceType=0, subDeviceType=-2 为 Zigbee调光灯
    3. ui.model 兜底
    4. classId / subDeviceType 兜底
    5. 返回 UNKNOWN
    """
    if not isinstance(device, dict):
        return DeviceCategory.UNKNOWN

    raw_type_value = device.get("device_type_raw")
    if raw_type_value is None:
        raw_type_value = device.get("deviceType")
    raw_sub_value = device.get("sub_device_type")
    if raw_sub_value is None:
        raw_sub_value = device.get("subDeviceType")
    device_type_raw = _safe_int(raw_type_value)
    sub_type = _safe_int(raw_sub_value)

    # 真机 Profile 必须先于裸 deviceType 映射。
    if device_type_raw == 0:
        return DeviceCategory.ZIGBEE_DIMMABLE_LIGHT
    if device_type_raw == 102:
        return DeviceCategory.LEGACY_LIGHT
    if device_type_raw == 112:
        ui = device.get("ui")
        ui_model = device.get("ui_model")
        if not ui_model and isinstance(ui, dict):
            ui_model = ui.get("model")
        if sub_type == -2 and (
            ui_model == "orb_floorheat"
            or device.get("model") == "2ac836760da10748856a7e4eafb91efa"
        ):
            return DeviceCategory.LEGACY_FLOOR_HEATING
        return DeviceCategory.UNKNOWN
    if device_type_raw == 300:
        if sub_type == 481:
            return DeviceCategory.FLOOR_HEATING
        if sub_type == 491:
            return DeviceCategory.TEMP_HUMIDITY_SENSOR
        return DeviceCategory.UNKNOWN
    if device_type_raw == 503:
        if sub_type == 436:
            return DeviceCategory.CCT_LIGHT_STRIP
        if sub_type == 461:
            return DeviceCategory.CCT_LIGHT
        return DeviceCategory.UNKNOWN
    if device_type_raw == 522:
        return (
            DeviceCategory.DOOR_LOCK
            if sub_type == 463
            else DeviceCategory.UNKNOWN
        )
    if device_type_raw == 501:
        return (
            DeviceCategory.MONO_LIGHT
            if sub_type in (426, 429)
            else DeviceCategory.UNKNOWN
        )
    if device_type_raw == 502:
        return (
            DeviceCategory.DIMMABLE_LIGHT
            if sub_type == 431
            else DeviceCategory.UNKNOWN
        )
    if device_type_raw == 506 and sub_type == 408:
        return DeviceCategory.DREAM_CURTAIN

    if device_type_raw is not None and device_type_raw in _DEVICE_TYPE_MAP:
        # deviceType=38 且 subDeviceType=6 时，使用 fast move 协议
        if device_type_raw == 38 and sub_type == 6:
            return DeviceCategory.FAST_MOVE_DIM_COLOR_LIGHT
        return _DEVICE_TYPE_MAP[device_type_raw]

    # 特殊判断：statusType=2, subDeviceType=6 为fast move调光调色灯
    status_type = _safe_int(device.get("status_type") or device.get("statusType"))
    if status_type == 2 and sub_type == 6:
        return DeviceCategory.FAST_MOVE_DIM_COLOR_LIGHT

    ui_model = device.get("ui_model") or device.get("ui", {}).get("model") if isinstance(device.get("ui"), dict) else device.get("ui_model")
    if isinstance(ui_model, str) and ui_model in _UI_MODEL_MAP:
        return _UI_MODEL_MAP[ui_model]

    raw_class_id = device.get("class_id")
    if raw_class_id is None:
        raw_class_id = device.get("classId")
    class_id = _safe_int(raw_class_id)
    if class_id is not None and class_id in _CLASS_ID_MAP:
        return _CLASS_ID_MAP[class_id]

    # subDeviceType 不是 classId。跨 deviceType 用它兜底会把地暖、网关、
    # 人体传感器等大量 subType=-2 记录误识别成调光灯。
    return (
        DeviceCategory.OTHER
        if identify_known_device(device) is not None
        else DeviceCategory.UNKNOWN
    )
