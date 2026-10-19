"""灯具状态数值的归一化与 active-low 判定（纯函数，无 HA 依赖）。

实测依据（见 docs 与 issue）：
- 老式灯（deviceType 0/1/38/43/102/503）的 ``value1`` 是 **active-low**：
  ``0`` = 开、``1`` = 关；Matter/Zigbee 简单灯则相反。
- 设备常用 ``-1`` 表示"该槽位未使用"，直接把 -1 当亮度会让实体出现负值。
- 色温在不同型号上可能是 Kelvin（2700–6500）或 mired（150–400）。
"""

from __future__ import annotations

import math
from typing import Any

#: 这些 deviceType 的 value1 是 active-low（0=开）。与 protocol._infer_initial_state 保持一致。
LEGACY_ACTIVE_LOW_TYPES = frozenset({0, 1, 38, 43, 102, 503})

#: 设备用 -1 / 空值表示槽位未使用。
UNUSED_SENTINEL = -1


def _to_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(text)
        except (ValueError, OverflowError):
            return None
    else:
        return None
    if not math.isfinite(number):
        return None
    return number


def normalize_brightness(value: Any, maximum: int = 255) -> int | None:
    """忽略未使用/非法读数，把有效读数钳制到量纲范围内。

    ``-1``（未使用）、负数、``None``、布尔值 → ``None``；超上限 → ``maximum``。
    """
    number = _to_number(value)
    if number is None or number < 0:
        return None
    if number > maximum:
        return maximum
    return int(number)


def percent_to_255(percent: Any) -> int | None:
    """把 0–100 的百分比换算到 HA 的 0–255 量纲。"""
    number = _to_number(percent)
    if number is None or number < 0:
        return None
    return int(min(100.0, number) * 255 / 100)


def normalize_color_temp(value: Any) -> int | None:
    """把色温读数统一成 Kelvin：mired（150–400）自动换算，其余钳制到 2700–6500K。"""
    number = _to_number(value)
    if number is None or number <= 0:
        return None
    if 150 <= number <= 400:
        return int(1_000_000 // int(number))
    if number < 1500:
        return None
    return int(min(6500, max(2700, number)))


def is_active_low(device_type: Any, sub_device_type: Any = None) -> bool:
    """判断 value1 的极性：True 表示 0=开。

    优先级：``sub_device_type == -2``（老式 0–10V 调光灯）→ active-low；
    其次看 deviceType 是否属于 ``LEGACY_ACTIVE_LOW_TYPES``。
    """
    if _to_number(sub_device_type) == -2:
        return True
    number = _to_number(device_type)
    if number is None:
        return False
    return int(number) in LEGACY_ACTIVE_LOW_TYPES


def power_state_from_value1(
    value1: Any, device_type: Any = None, sub_device_type: Any = None
) -> bool | None:
    """把 value1 解读成开关状态；无法解释（如打包值 49/4087）时返回 None。"""
    number = _to_number(value1)
    if number is None:
        return None
    integer = int(number)
    if integer not in (0, 1):
        return None
    return (integer == 0) if is_active_low(device_type, sub_device_type) else (integer == 1)
