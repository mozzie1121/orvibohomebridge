"""Registry for pure device-state parsers."""

from __future__ import annotations

from typing import Optional

from ..device_types import DeviceCategory
from .appliance import parse_clothes_horse, parse_fan_coil_ac, parse_ventilation
from .base import StateParser, StatePatch
from .cover import parse_curtain, parse_dream_curtain
from .light import (
    parse_cct_light,
    parse_dim_color_light,
    parse_dimmable_light,
    parse_fast_move_dim_color_light,
    parse_light,
    parse_switch,
    parse_zigbee_dimmable_light,
)
from .lock import parse_door_lock
from .sensor import (
    parse_door_window_sensor,
    parse_emergency_button,
    parse_gas_sensor,
    parse_motion_sensor,
    parse_smoke_sensor,
    parse_temp_humidity_sensor,
    parse_water_leak_sensor,
)
from .thermostat import parse_floor_heating, parse_legacy_floor_heating


STATE_PARSERS: dict[DeviceCategory, StateParser] = {
    DeviceCategory.SIMPLE_ZIGBEE_LIGHT: parse_light,
    DeviceCategory.MONO_LIGHT: parse_light,
    DeviceCategory.LIGHT_VIRTUAL_GROUP: parse_light,
    DeviceCategory.LEGACY_LIGHT: parse_light,
    DeviceCategory.DIM_COLOR_LIGHT: parse_dim_color_light,
    DeviceCategory.FAST_MOVE_DIM_COLOR_LIGHT: parse_fast_move_dim_color_light,
    DeviceCategory.DIMMABLE_LIGHT: parse_dimmable_light,
    DeviceCategory.ZIGBEE_DIMMABLE_LIGHT: parse_zigbee_dimmable_light,
    DeviceCategory.CCT_LIGHT_STRIP: parse_cct_light,
    DeviceCategory.CCT_LIGHT: parse_cct_light,
    DeviceCategory.ZIGBEE_CURTAIN: parse_curtain,
    DeviceCategory.ZIGBEE_ROLLING_SHUTTER: parse_curtain,
    DeviceCategory.DREAM_CURTAIN: parse_dream_curtain,
    DeviceCategory.FLOOR_HEATING: parse_floor_heating,
    DeviceCategory.LEGACY_FLOOR_HEATING: parse_legacy_floor_heating,
    DeviceCategory.MIX_SWITCH: parse_switch,
    DeviceCategory.TEMP_HUMIDITY_SENSOR: parse_temp_humidity_sensor,
    DeviceCategory.DOOR_WINDOW_SENSOR: parse_door_window_sensor,
    DeviceCategory.MOTION_SENSOR: parse_motion_sensor,
    DeviceCategory.EMERGENCY_BUTTON: parse_emergency_button,
    DeviceCategory.SMOKE_SENSOR: parse_smoke_sensor,
    DeviceCategory.WATER_LEAK_SENSOR: parse_water_leak_sensor,
    DeviceCategory.GAS_SENSOR: parse_gas_sensor,
    DeviceCategory.FAN_COIL_AC: parse_fan_coil_ac,
    DeviceCategory.VENTILATION_SYSTEM: parse_ventilation,
    DeviceCategory.CLOTHES_HORSE: parse_clothes_horse,
    DeviceCategory.DOOR_LOCK: parse_door_lock,
}


def get_state_parser(category: DeviceCategory) -> Optional[StateParser]:
    """Return the pure parser registered for a device category, if any."""

    return STATE_PARSERS.get(category)


__all__ = ["StatePatch", "get_state_parser"]
