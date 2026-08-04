"""Parsers for property-based thermostat devices."""

from __future__ import annotations

from typing import Any, Mapping

from .base import StatePatch


def parse_floor_heating(
    current_state: Mapping[str, Any], raw_status: Mapping[str, Any]
) -> StatePatch:
    """Parse a MixPad floor-heating panel (type=300/subType=481)."""
    props = raw_status.get("properties", {})
    updates: dict[str, Any] = {}
    onoff = props.get("onoff", {})
    if isinstance(onoff, Mapping) and onoff.get("status") in ("on", "off"):
        updates["state"] = onoff["status"] == "on"

    thermostat = props.get("thermostat", {})
    if isinstance(thermostat, Mapping):
        field_map = {
            "targetTemp": "target_temperature",
            "localTemp": "current_temperature",
            "localHumidity": "current_humidity",
            "minHeatingTemp": "min_temperature",
            "maxHeatingTemp": "max_temperature",
            "runMode": "run_mode",
        }
        for raw_key, state_key in field_map.items():
            if raw_key in thermostat:
                updates[state_key] = thermostat[raw_key]

    combined = props.get("combinedDevice", {})
    if isinstance(combined, Mapping):
        if "runStatus" in combined:
            updates["run_status"] = combined["runStatus"]
        if "combinedStatus" in combined:
            updates["combined_status"] = combined["combinedStatus"]
    return StatePatch(updates)


def parse_legacy_floor_heating(
    current_state: Mapping[str, Any], raw_status: Mapping[str, Any]
) -> StatePatch:
    """Parse the verified type=112 ``orb_floorheat`` packed-value protocol."""
    updates: dict[str, Any] = {
        "min_temperature": 10,
        "max_temperature": 35,
    }
    props = raw_status.get("properties", {})
    if isinstance(props, Mapping):
        power = props.get("onoff")
        if power in ("on", "off"):
            updates["state"] = power == "on"
        if isinstance(props.get("localTemperature"), (int, float)):
            updates["current_temperature"] = props["localTemperature"]
        if isinstance(props.get("temperature"), (int, float)):
            updates["target_temperature"] = props["temperature"]

    value1 = raw_status.get("value1")
    if isinstance(value1, (int, float)):
        value1 = int(value1)
        updates["raw_value1"] = value1
        updates["state"] = bool(value1 & 0x08)

    value2 = raw_status.get("value2")
    if isinstance(value2, (int, float)):
        value2 = int(value2)
        local_temperature = (value2 >> 8) & 0xFF
        target_offset = value2 & 0xFF
        updates["raw_value2"] = value2
        if 0 <= local_temperature <= 60:
            updates["current_temperature"] = local_temperature
        if 0 <= target_offset <= 25:
            updates["target_temperature"] = target_offset + 10
    return StatePatch(updates)
