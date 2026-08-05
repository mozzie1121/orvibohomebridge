"""Pure parsers for light and switch status packets."""

from __future__ import annotations

from typing import Any, Mapping

from .base import StatePatch


def _subdevice_type(raw_status: Mapping[str, Any]) -> Any:
    value = raw_status.get("subDeviceType", 0)
    return int(value) if isinstance(value, str) else value


def parse_light(
    current_state: Mapping[str, Any], raw_status: Mapping[str, Any]
) -> StatePatch:
    """Parse an on/off light using properties first, then legacy value1."""

    props = raw_status.get("properties", {})
    onoff_obj = props.get("onoff", {})
    if isinstance(onoff_obj, dict) and onoff_obj.get("status"):
        state = onoff_obj.get("status") == "on"
    elif isinstance(props.get("onoff_status"), str):
        state = props["onoff_status"] == "on"
    elif raw_status.get("value1") is not None:
        state = int(raw_status["value1"]) == 0
    else:
        state = current_state.get("state", False)
    return StatePatch({"state": state})


def parse_dim_color_light(
    current_state: Mapping[str, Any], raw_status: Mapping[str, Any]
) -> StatePatch:
    """Parse the legacy value-based dimmable, tunable-white light."""

    props = raw_status.get("properties", {})
    brightness = raw_status.get("value2")
    if brightness is None:
        raw_brightness = props.get("brightness")
        if isinstance(raw_brightness, dict):
            # 属性型亮度可能是 {"percent": 0-100} / {"value": 0-255} / {"level": ...}
            percent = raw_brightness.get("percent")
            if percent is not None:
                try:
                    # type=38 量纲为 0-255，percent 换算后钳制
                    brightness = min(255, max(0, int(float(percent) * 255 / 100)))
                except (TypeError, ValueError):
                    brightness = None
            else:
                brightness = next(
                    (
                        raw_brightness[key]
                        for key in ("value", "level", "brightness")
                        if key in raw_brightness
                    ),
                    None,
                )
        else:
            brightness = raw_brightness
    if brightness is not None:
        try:
            brightness = int(brightness)
        except (TypeError, ValueError):
            brightness = None

    color_temp = raw_status.get("value3")
    if color_temp is not None:
        color_temp = int(color_temp)
        if 150 <= color_temp <= 400:
            color_temp = 1_000_000 // color_temp
    else:
        color_temp = props.get("colortemp")
        if color_temp is None:
            color_temp = props.get("colorTemp")
        if isinstance(color_temp, dict):
            color_temp = color_temp.get("value")
        if color_temp is not None:
            try:
                color_temp = int(color_temp)
            except (TypeError, ValueError):
                color_temp = None
    if color_temp is not None:
        color_temp = min(6500, max(2700, color_temp))

    value1 = raw_status.get("value1")
    if isinstance(value1, dict):
        value1 = value1.get("value")
    if value1 is not None:
        sub_device_type = _subdevice_type(raw_status)
        try:
            state = int(value1) == (0 if sub_device_type == -2 else 1)
        except (TypeError, ValueError):
            state = current_state.get("state", False)
    else:
        onoff_obj = props.get("onoff", {})
        if isinstance(onoff_obj, dict) and onoff_obj.get("status"):
            state = onoff_obj.get("status") == "on"
        elif brightness is not None:
            state = brightness > 0
        else:
            state = current_state.get("state", False)

    return StatePatch(
        {"state": state, "brightness": brightness, "color_temp": color_temp}
    )


def parse_fast_move_dim_color_light(
    current_state: Mapping[str, Any], raw_status: Mapping[str, Any]
) -> StatePatch:
    """Parse a Fast Move value1/value2/value3 light packet."""

    updates: dict[str, Any] = {}
    value1 = raw_status.get("value1")
    brightness = raw_status.get("value2")
    color_temp = raw_status.get("value3")

    if value1 is not None:
        updates["state"] = int(value1) == 0

    if brightness is not None:
        brightness = min(255, max(0, int(brightness)))
        updates["brightness"] = brightness
        if brightness == 0:
            updates["state"] = False

    if color_temp is not None:
        color_temp = int(color_temp)
        if 150 <= color_temp <= 400:
            updates["color_temp"] = min(6000, max(2700, 1_000_000 // color_temp))

    return StatePatch(updates)


def _property_brightness(props: Mapping[str, Any]) -> Any:
    brightness = props.get("brightness", {})
    return brightness.get("percent") if isinstance(brightness, dict) else brightness


def parse_dimmable_light(
    current_state: Mapping[str, Any], raw_status: Mapping[str, Any]
) -> StatePatch:
    """Parse a property-based dimmable light."""

    props = raw_status.get("properties", {})
    updates: dict[str, Any] = {}
    onoff_obj = props.get("onoff", {})
    if isinstance(onoff_obj, dict) and onoff_obj.get("status"):
        updates["state"] = onoff_obj.get("status") == "on"
    elif "state" not in current_state:
        updates["state"] = False

    brightness = _property_brightness(props)
    if brightness is not None:
        brightness = min(100, max(0, int(brightness)))
        updates["brightness"] = brightness
        if brightness == 0:
            updates["state"] = False
    return StatePatch(updates)


def parse_zigbee_dimmable_light(
    current_state: Mapping[str, Any], raw_status: Mapping[str, Any]
) -> StatePatch:
    """Parse a value-based 0-10V dimmable light."""

    updates: dict[str, Any] = {}
    value1 = raw_status.get("value1")
    brightness = raw_status.get("value2")

    if value1 is not None:
        sub_device_type = _subdevice_type(raw_status)
        updates["state"] = int(value1) == (0 if sub_device_type == -2 else 1)
    if brightness is not None:
        brightness = min(255, max(0, int(brightness)))
        updates["brightness"] = brightness
        if brightness == 0:
            updates["state"] = False
    return StatePatch(updates)


def parse_cct_light(
    current_state: Mapping[str, Any], raw_status: Mapping[str, Any]
) -> StatePatch:
    """Parse a property-based brightness and color-temperature light."""

    props = raw_status.get("properties", {})
    updates = dict(parse_dimmable_light(current_state, raw_status).values)
    color_temp = props.get("colorTemp", {})
    color_temp = color_temp.get("value") if isinstance(color_temp, dict) else color_temp
    if color_temp is not None:
        updates["color_temp"] = min(6500, max(2000, int(color_temp)))
    return StatePatch(updates)


def parse_switch(
    current_state: Mapping[str, Any], raw_status: Mapping[str, Any]
) -> StatePatch:
    """Parse a switch channel from property or legacy value fields."""

    props = raw_status.get("properties", {})
    onoff_obj = props.get("onoff", {})
    if isinstance(onoff_obj, dict) and onoff_obj.get("status"):
        state = onoff_obj.get("status") == "on"
    elif raw_status.get("value1") is not None:
        sub_device_type = _subdevice_type(raw_status)
        state = int(raw_status["value1"]) == (0 if sub_device_type == -2 else 1)
    else:
        state = False
    return StatePatch({"state": state})
