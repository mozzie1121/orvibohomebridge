"""Pure parsers for environmental and safety sensor packets."""

from __future__ import annotations

from typing import Any, Mapping

from .base import StatePatch


def _raw_or_nested_value(value: Any, key: str = "value") -> Any:
    return value.get(key) if isinstance(value, dict) else value


def _battery_value(value: Any) -> Any:
    if isinstance(value, dict):
        value = value.get("power") or value.get("value")
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return value


def _integer_or_raw(value: Any) -> Any:
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def parse_temp_humidity_sensor(
    current_state: Mapping[str, Any], raw_status: Mapping[str, Any]
) -> StatePatch:
    """Parse temperature, humidity, and property-based battery values."""

    props = raw_status.get("properties", {})
    updates: dict[str, Any] = {"state": True}

    temperature = _raw_or_nested_value(props.get("temperature", {}))
    if temperature is not None:
        try:
            updates["temperature"] = float(temperature)
        except (TypeError, ValueError):
            updates["temperature"] = temperature

    humidity = _raw_or_nested_value(props.get("humidity", {}))
    if humidity is not None:
        try:
            updates["humidity"] = float(humidity)
        except (TypeError, ValueError):
            updates["humidity"] = humidity

    battery = _battery_value(props.get("battery", {}))
    if battery is not None:
        updates["battery"] = battery
    return StatePatch(updates)


def parse_door_window_sensor(
    current_state: Mapping[str, Any], raw_status: Mapping[str, Any]
) -> StatePatch:
    """Parse a door/window contact and its battery percentage."""

    updates: dict[str, Any] = {"state": True}
    value1 = raw_status.get("value1")
    if value1 is not None:
        try:
            updates["door_state"] = int(value1) == 1
        except (TypeError, ValueError):
            updates["door_state"] = False

    value4 = raw_status.get("value4")
    if value4 is not None:
        updates["battery"] = _integer_or_raw(value4)
    return StatePatch(updates)


def parse_motion_sensor(
    current_state: Mapping[str, Any], raw_status: Mapping[str, Any]
) -> StatePatch:
    """Parse motion state; delayed reset scheduling remains with the coordinator."""

    updates: dict[str, Any] = {"state": True}
    value3 = raw_status.get("value3")
    if value3 is not None:
        try:
            updates["motion_detected"] = int(value3) == 1
        except (TypeError, ValueError):
            updates["motion_detected"] = False
    value4 = raw_status.get("value4")
    if value4 is not None:
        updates["battery"] = _integer_or_raw(value4)
    return StatePatch(updates)


def parse_emergency_button(
    current_state: Mapping[str, Any], raw_status: Mapping[str, Any]
) -> StatePatch:
    """Parse emergency-button state without scheduling its delayed reset."""

    updates: dict[str, Any] = {}
    value1 = raw_status.get("value1")
    if value1 is not None:
        try:
            updates["emergency_state"] = int(value1) == 1
        except (TypeError, ValueError):
            updates["emergency_state"] = False
    value4 = raw_status.get("value4")
    if value4 is not None:
        updates["battery"] = _integer_or_raw(value4)
    return StatePatch(updates)


def _parse_alarm_sensor(
    raw_status: Mapping[str, Any], alarm_field: str
) -> StatePatch:
    updates: dict[str, Any] = {"state": True}
    value1 = raw_status.get("value1")
    if value1 is not None:
        try:
            updates[alarm_field] = int(value1) == 1
        except (TypeError, ValueError):
            updates[alarm_field] = False

    value4 = raw_status.get("value4")
    if value4 is not None:
        updates["battery"] = _integer_or_raw(value4)
    return StatePatch(updates)


def parse_smoke_sensor(
    current_state: Mapping[str, Any], raw_status: Mapping[str, Any]
) -> StatePatch:
    """Parse a smoke alarm status packet."""

    return _parse_alarm_sensor(raw_status, "smoke_detected")


def parse_water_leak_sensor(
    current_state: Mapping[str, Any], raw_status: Mapping[str, Any]
) -> StatePatch:
    """Parse a water-leak alarm status packet."""

    return _parse_alarm_sensor(raw_status, "water_leak_detected")


def parse_gas_sensor(
    current_state: Mapping[str, Any], raw_status: Mapping[str, Any]
) -> StatePatch:
    """Parse a combustible-gas alarm status packet."""

    return _parse_alarm_sensor(raw_status, "gas_detected")
