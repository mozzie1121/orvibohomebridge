"""Declare Home Assistant platforms and transport policy for each device.

Profiles identify device semantics; this module adds the runtime decisions used
by entity registration, LAN push filtering, and control transport selection.
Unknown devices remain registration-only and never receive inferred commands.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, FrozenSet, Optional

from .device_types import DeviceCategory, get_device_profile

# HA 平台名（与 __init__.PLATFORMS 对齐）
PLATFORM_LIGHT = "light"
PLATFORM_COVER = "cover"
PLATFORM_CLIMATE = "climate"
PLATFORM_FAN = "fan"
PLATFORM_SENSOR = "sensor"
PLATFORM_BINARY_SENSOR = "binary_sensor"
PLATFORM_CAMERA = "camera"
PLATFORM_SWITCH = "switch"


class ControlChannel(str, Enum):
    """设备可用的控制通道。"""

    LAN = "lan"  # 网关直连（本地优先）
    SSL = "ssl"  # 云端长连接


class TransportMode(str, Enum):
    """Select which realtime/control transports may be used."""

    AUTO = "auto"
    LAN_ONLY = "lan_only"
    CLOUD_ONLY = "cloud_only"


class TransportPath(str, Enum):
    """User-facing transport policy for one device in the active mode."""

    LAN = "lan"
    CLOUD = "cloud"
    LAN_CLOUD = "lan_cloud"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class DeviceCapability:
    """一类设备的融合能力声明。"""

    category: DeviceCategory
    platforms: FrozenSet[str]
    channels: FrozenSet[ControlChannel]
    status_only: bool = False
    cloud_only: bool = False
    hardware_verified: bool = False

    @property
    def controllable(self) -> bool:
        return bool(self.channels)


# ---- 类型级传输与只读策略 ----

# LAN 可直接控制的设备类型（gateway 转发；源自 lan profiles 的非只读类型）
_LAN_CONTROLLABLE_TYPES = frozenset(
    {0, 1, 34, 35, 36, 38, 81, 102, 501, 502, 503, 516}
)

# 类型级只读标记：仅保留云端实现未映射为分类的 WiFi 门锁（107）。
# 其余只读类型（传感器类、522 门锁）由 _STATUS_ONLY_CATEGORIES 按分类判定，
# type 300 按云端实测子类型定义（481 地暖可云控 / 491 温湿度只读）。
_STATUS_ONLY_TYPES = frozenset({107})

# 必须走云的设备（WiFi 直连 / 门锁 LAN 走不通；ADR-4）
_CLOUD_ONLY_TYPES = frozenset({52, 107, 522})

# 只读分类（无设备控制通道，状态仍正常推送）
_STATUS_ONLY_CATEGORIES = frozenset(
    {
        DeviceCategory.MOTION_SENSOR,
        DeviceCategory.TEMP_HUMIDITY_SENSOR,
        DeviceCategory.DOOR_WINDOW_SENSOR,
        DeviceCategory.SMOKE_SENSOR,
        DeviceCategory.EMERGENCY_BUTTON,
        DeviceCategory.WATER_LEAK_SENSOR,
        DeviceCategory.GAS_SENSOR,
        DeviceCategory.DOOR_LOCK,
    }
)


# ---- 分类 → Home Assistant 平台 ----

_CATEGORY_PLATFORMS: Dict[DeviceCategory, FrozenSet[str]] = {
    DeviceCategory.SIMPLE_ZIGBEE_LIGHT: frozenset({PLATFORM_LIGHT}),
    DeviceCategory.MONO_LIGHT: frozenset({PLATFORM_LIGHT}),
    DeviceCategory.LEGACY_LIGHT: frozenset({PLATFORM_LIGHT}),
    DeviceCategory.DIM_COLOR_LIGHT: frozenset({PLATFORM_LIGHT}),
    DeviceCategory.DIMMABLE_LIGHT: frozenset({PLATFORM_LIGHT}),
    DeviceCategory.CCT_LIGHT: frozenset({PLATFORM_LIGHT}),
    DeviceCategory.CCT_LIGHT_STRIP: frozenset({PLATFORM_LIGHT}),
    DeviceCategory.ZIGBEE_DIMMABLE_LIGHT: frozenset({PLATFORM_LIGHT}),
    DeviceCategory.FAST_MOVE_DIM_COLOR_LIGHT: frozenset({PLATFORM_LIGHT}),
    DeviceCategory.LIGHT_VIRTUAL_GROUP: frozenset({PLATFORM_LIGHT}),
    DeviceCategory.ZIGBEE_CURTAIN: frozenset({PLATFORM_COVER}),
    DeviceCategory.ZIGBEE_ROLLING_SHUTTER: frozenset({PLATFORM_COVER}),
    DeviceCategory.LEGACY_CURTAIN: frozenset({PLATFORM_COVER}),
    DeviceCategory.DREAM_CURTAIN: frozenset({PLATFORM_COVER}),
    DeviceCategory.FAN_COIL_AC: frozenset({PLATFORM_CLIMATE}),
    DeviceCategory.FLOOR_HEATING: frozenset({PLATFORM_CLIMATE}),
    DeviceCategory.LEGACY_FLOOR_HEATING: frozenset({PLATFORM_CLIMATE}),
    DeviceCategory.VENTILATION_SYSTEM: frozenset({PLATFORM_FAN}),
    DeviceCategory.MOTION_SENSOR: frozenset(
        {PLATFORM_SENSOR, PLATFORM_BINARY_SENSOR}
    ),
    DeviceCategory.TEMP_HUMIDITY_SENSOR: frozenset({PLATFORM_SENSOR}),
    DeviceCategory.DOOR_WINDOW_SENSOR: frozenset(
        {PLATFORM_SENSOR, PLATFORM_BINARY_SENSOR}
    ),
    DeviceCategory.SMOKE_SENSOR: frozenset(
        {PLATFORM_SENSOR, PLATFORM_BINARY_SENSOR}
    ),
    DeviceCategory.EMERGENCY_BUTTON: frozenset(
        {PLATFORM_SENSOR, PLATFORM_BINARY_SENSOR}
    ),
    DeviceCategory.WATER_LEAK_SENSOR: frozenset(
        {PLATFORM_SENSOR, PLATFORM_BINARY_SENSOR}
    ),
    DeviceCategory.GAS_SENSOR: frozenset(
        {PLATFORM_SENSOR, PLATFORM_BINARY_SENSOR}
    ),
    DeviceCategory.DOOR_LOCK: frozenset(
        {PLATFORM_SENSOR, PLATFORM_BINARY_SENSOR, PLATFORM_CAMERA}
    ),
    DeviceCategory.CLOTHES_HORSE: frozenset({PLATFORM_SENSOR}),
    DeviceCategory.MIX_SWITCH: frozenset({PLATFORM_SWITCH}),
    DeviceCategory.BACH_SWITCH: frozenset({PLATFORM_SWITCH}),
}


def _safe_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _device_type_key(device: Any) -> int:
    """从设备 dict 提取归一化 deviceType（缺省按 0 处理）。"""
    if not isinstance(device, dict):
        return 0
    raw = device.get("device_type_raw")
    if raw is None:
        raw = device.get("deviceType")
    return _safe_int(raw) or 0


def capability_for(device: Any) -> DeviceCapability:
    """解析单台设备的融合能力（不触发任何 I/O）。"""
    profile = get_device_profile(device)
    category = profile.category
    device_type = _device_type_key(device)

    status_only = (
        device_type in _STATUS_ONLY_TYPES
        or category in _STATUS_ONLY_CATEGORIES
    )
    cloud_only = device_type in _CLOUD_ONLY_TYPES

    if cloud_only:
        # cloud_only 表示状态/富功能走云；channels 只描述"设备控制"通道。
        # 门锁等只读设备没有控制通道；晾衣机（52）有云控制命令。
        channels = (
            frozenset({ControlChannel.SSL})
            if not status_only
            else frozenset()
        )
    elif status_only:
        channels = frozenset()
    elif profile.registration_only:
        # 未知/未验证类别只展示、不下发控制（项目默认的保守策略）
        channels = frozenset()
    elif device_type in _LAN_CONTROLLABLE_TYPES:
        # 本地优先：LAN 与云都支持，路由层按模式/可达性选择
        channels = frozenset({ControlChannel.LAN, ControlChannel.SSL})
    else:
        channels = frozenset({ControlChannel.SSL})

    return DeviceCapability(
        category=category,
        platforms=_CATEGORY_PLATFORMS.get(category, frozenset()),
        channels=channels,
        status_only=status_only,
        cloud_only=cloud_only,
        hardware_verified=profile.hardware_verified,
    )


def capability_for_type(
    device_type: int,
    sub_type: Optional[int] = None,
) -> DeviceCapability:
    """按类型解析能力（测试与诊断用；运行时请用 capability_for）。"""
    return capability_for(
        {
            "device_type_raw": device_type,
            "sub_device_type": sub_type,
        }
    )


def lan_state_allowed(
    device: Any,
    transport_mode: TransportMode = TransportMode.AUTO,
) -> bool:
    """Return whether a LAN push may update this device in the active mode."""

    return (
        transport_mode != TransportMode.CLOUD_ONLY
        and not capability_for(device).cloud_only
    )


def transport_path_for(
    device: Any,
    transport_mode: TransportMode = TransportMode.AUTO,
) -> TransportPath:
    """Describe the configured runtime path without probing network state."""

    capability = capability_for(device)
    if transport_mode == TransportMode.CLOUD_ONLY:
        return TransportPath.CLOUD

    if transport_mode == TransportMode.LAN_ONLY:
        if capability.cloud_only:
            return TransportPath.UNAVAILABLE
        if capability.status_only or ControlChannel.LAN in capability.channels:
            return TransportPath.LAN
        return TransportPath.UNAVAILABLE

    if capability.cloud_only:
        return TransportPath.CLOUD
    if capability.status_only or ControlChannel.LAN in capability.channels:
        return TransportPath.LAN_CLOUD
    if ControlChannel.SSL in capability.channels:
        return TransportPath.CLOUD
    return TransportPath.UNAVAILABLE
