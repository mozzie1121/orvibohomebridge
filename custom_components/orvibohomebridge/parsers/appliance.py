"""Pure parsers for HVAC and clothes-horse status packets."""

from __future__ import annotations

from typing import Any, Mapping

from .base import StatePatch


def parse_fan_coil_ac(
    current_state: Mapping[str, Any], raw_status: Mapping[str, Any]
) -> StatePatch:
    """Parse a fan-coil AC value1-value4 status packet."""

    updates: dict[str, Any] = {}
    value1 = raw_status.get("value1")
    value2 = raw_status.get("value2")
    value3 = raw_status.get("value3")
    value4 = raw_status.get("value4")

    if value1 is not None:
        value1 = int(value1)
        updates.update({"state": value1 == 0, "value1": value1})
    if value2 is not None:
        value2 = int(value2)
        mode = {2: "dehumidify", 3: "cool", 4: "heat", 7: "fan_only"}
        updates.update(
            {
                "ac_mode": mode.get(value2, f"unknown({value2})"),
                "ac_mode_raw": value2,
                "value2": value2,
            }
        )
    if value3 is not None:
        value3 = int(value3)
        speed = {1: "low", 2: "medium", 3: "high"}
        updates.update(
            {
                "fan_speed": speed.get(value3, f"unknown({value3})"),
                "fan_speed_raw": value3,
                "value3": value3,
            }
        )
    if value4 is not None:
        value4 = int(value4)
        target_temp = (value4 >> 16) / 100.0
        current_temp = (value4 & 0xFFFF) / 100.0
        updates.update(
            {
                "value4": value4,
                "temperature": target_temp,
                "target_temperature": target_temp,
                "current_temperature": current_temp,
            }
        )
    return StatePatch(updates)


def parse_ventilation(
    current_state: Mapping[str, Any], raw_status: Mapping[str, Any]
) -> StatePatch:
    """Parse value-based and property-based ventilation status."""

    props = raw_status.get("properties", {})
    updates: dict[str, Any] = {}
    value1 = raw_status.get("value1")
    if value1 is not None:
        value1 = int(value1)
        value_state = {0: ("慢", True), 50: ("停", False), 100: ("快", True)}
        if value1 in value_state:
            updates["fan_speed"], updates["state"] = value_state[value1]
        updates["value1"] = value1

    fan_control = props.get("fanControl", {}) if isinstance(props, dict) else {}
    if isinstance(fan_control, dict):
        fan_mode = fan_control.get("fanMode")
        if fan_mode:
            updates["fan_speed"] = {
                "off": "停",
                "low": "慢",
                "high": "快",
            }.get(fan_mode, fan_mode)
            updates["state"] = fan_mode != "off"

    temperature = props.get("temperature", {}) if isinstance(props, dict) else {}
    if isinstance(temperature, dict) and temperature.get("value") is not None:
        try:
            updates["temperature"] = float(temperature["value"])
        except (TypeError, ValueError):
            pass
    return StatePatch(updates)


def parse_clothes_horse(
    current_state: Mapping[str, Any], raw_status: Mapping[str, Any]
) -> StatePatch:
    """Parse a clothes-horse cmd=99 status packet."""

    main_switch = raw_status.get("main_switch_state", "off") == "on"
    return StatePatch(
        {
            "motor_state": raw_status.get("motor_state", "stop"),
            "position": raw_status.get("motor_position", 0),
            "lighting_state": raw_status.get("lighting_state", "off") == "on",
            "heat_drying_state": raw_status.get("heat_drying_state", "off")
            == "on",
            "wind_drying_state": raw_status.get("wind_drying_state", "off")
            == "on",
            "sterilizing_state": raw_status.get("sterilizing_state", "off")
            == "on",
            "main_switch_state": main_switch,
            "state": main_switch,
        }
    )
