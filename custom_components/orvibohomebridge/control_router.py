"""Pure routing decisions for category-specific control transports."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping

from .device_types import DeviceCategory

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ControlRoute:
    """One method invocation selected without performing any I/O."""

    scope: str
    method: str
    args: tuple[Any, ...] = ()
    kwargs: Mapping[str, Any] = field(default_factory=dict)
    optimistic: Mapping[str, Any] = field(default_factory=dict)


def _custom_route(category: Any, action: str, *args: Any, **kwargs: Any):
    """Compile a custom-device profile action, or None when not declared.

    Custom profiles live in their own category namespace, so a category no
    profile claims falls straight through to the built-in behaviour.  Once a
    profile claims the category, an action it does not declare is rejected
    loudly instead of borrowing a built-in command the author never verified.
    """

    from .custom_control import ControlNotDeclared, build_route, is_custom_category

    if not is_custom_category(category):
        return None
    try:
        return build_route(category, action, *args, strict=True, **kwargs)
    except ControlNotDeclared:
        raise
    except Exception as error:  # noqa: BLE001 - never break built-in routing
        _LOGGER.error("自定义设备控制路由失败: %s", error)
        return None


def _custom_power_route(
    category: Any,
    is_on: bool,
    current_state: Mapping[str, Any],
    *,
    brightness: int | None = None,
    color_temp: int | None = None,
):
    """Route a custom device's power action.

    When turning on with attributes, the profile's own ``on`` action decides
    what to send; the declared ``value2``/``value3`` specs pull the attribute
    from the parameters, so nothing is silently dropped.
    """

    action = "on" if is_on else "off"
    return _custom_route(
        category,
        action,
        current_state,
        brightness=brightness,
        color_temp=color_temp,
    )


def _custom_attribute_route(
    category: Any,
    action: str,
    current_state: Mapping[str, Any],
    **values: Any,
):
    return _custom_route(category, action, current_state, **values)


def custom_route_for_device(
    device: Mapping[str, Any],
    action: str,
    current_state: Mapping[str, Any],
    **values: Any,
):
    """Route an action for a device dict, using its custom profile category.

    Returns ``None`` for built-in devices so callers keep their existing path.
    Raises ``ControlNotDeclared`` when a custom device lacks the action.
    """

    from .custom_devices import profile_for_device
    from .device_types import classify_device

    profile = profile_for_device(device)
    if profile is None:
        return None
    return _custom_route(
        classify_device(device), action, current_state, **values
    )


def power_route(
    category: DeviceCategory,
    is_on: bool,
    current_state: Mapping[str, Any],
    *,
    brightness: int | None = None,
    color_temp: int | None = None,
) -> ControlRoute:
    """Select the exact existing power-control call for a device category."""

    custom = _custom_power_route(
        category,
        is_on,
        current_state,
        brightness=brightness,
        color_temp=color_temp,
    )
    if custom is not None:
        return custom

    if category == DeviceCategory.DIM_COLOR_LIGHT:
        if not is_on:
            return ControlRoute("ssl", "send_control_light", (False,))
        current_brightness = (
            brightness
            if brightness is not None
            else current_state.get("brightness", 0) or 0
        )
        current_color_temp = (
            color_temp
            if color_temp is not None
            else current_state.get("color_temp", 0) or 0
        )
        return ControlRoute(
            "ssl",
            "send_control_light_colortemp",
            (current_color_temp or 2700,),
            {"brightness": current_brightness or 255},
        )

    if category == DeviceCategory.BACH_SWITCH:
        return ControlRoute("ssl", "send_control_switch", (is_on,))
    if category in (DeviceCategory.MONO_LIGHT, DeviceCategory.DIMMABLE_LIGHT):
        return ControlRoute("ssl", "send_control_switch", (is_on,))

    if category == DeviceCategory.ZIGBEE_DIMMABLE_LIGHT:
        current_brightness = (
            brightness
            if is_on and brightness is not None
            else current_state.get("brightness", 0) or 0
        )
        if is_on and current_brightness == 0:
            current_brightness = 255
        return ControlRoute(
            "ssl",
            "send_control_zigbee_dimmable_light_onoff",
            (is_on,),
            {"brightness": current_brightness},
        )

    if category == DeviceCategory.FAST_MOVE_DIM_COLOR_LIGHT:
        current_brightness = (
            brightness
            if is_on and brightness is not None
            else current_state.get("brightness", 0) or 0
        )
        current_color_temp = (
            color_temp
            if is_on and color_temp is not None
            else current_state.get("color_temp", 0) or 0
        )
        if is_on:
            current_brightness = current_brightness or 255
            current_color_temp = current_color_temp or 2700
        mired = 1_000_000 // current_color_temp if current_color_temp > 0 else 370
        return ControlRoute(
            "ssl",
            "send_control_fast_move_dim_color_light_onoff",
            (is_on,),
            {"brightness": current_brightness, "colortemp_mired": mired},
        )

    if category in (DeviceCategory.CCT_LIGHT, DeviceCategory.CCT_LIGHT_STRIP):
        return ControlRoute("ssl", "send_control_cct_light_onoff", (is_on,))

    if category == DeviceCategory.FLOOR_HEATING:
        return ControlRoute("ssl", "send_control_floor_heating_power", (is_on,))

    if category == DeviceCategory.LEGACY_FLOOR_HEATING:
        packed_state = current_state.get("raw_value2")
        if not isinstance(packed_state, int):
            local = int(current_state.get("current_temperature") or 0)
            target = int(current_state.get("target_temperature") or 10)
            packed_state = (local << 8) | target
        return ControlRoute(
            "ssl",
            "send_control_legacy_floor_heating_power",
            (is_on,),
            {"packed_state": packed_state},
        )

    if category in (
        DeviceCategory.SIMPLE_ZIGBEE_LIGHT,
        DeviceCategory.LEGACY_LIGHT,
        DeviceCategory.CCT_LIGHT_STRIP,
        DeviceCategory.LIGHT_VIRTUAL_GROUP,
    ):
        return ControlRoute("ssl", "send_control_light", (is_on,))

    if category in (DeviceCategory.ZIGBEE_CURTAIN, DeviceCategory.LEGACY_CURTAIN):
        return ControlRoute("ssl", "send_control_cover", (100 if is_on else 0,))

    if category == DeviceCategory.FAN_COIL_AC:
        value4 = current_state.get("value4", 0) or (2500 << 16)
        return ControlRoute(
            "coordinator_uid",
            "_async_ac_control_raw",
            (),
            {
                "value1": 0 if is_on else 1,
                "value2": current_state.get("ac_mode_raw", 3),
                "value3": current_state.get("fan_speed_raw", 1),
                "value4": value4,
                "order": "on" if is_on else "off",
            },
        )

    if category == DeviceCategory.CLOTHES_HORSE:
        return ControlRoute(
            "coordinator",
            "async_clothes_horse_control",
            ("main_switch", "on" if is_on else "off"),
        )

    if category == DeviceCategory.VENTILATION_SYSTEM:
        return ControlRoute(
            "coordinator", "async_ventilation_state_update", (0 if is_on else 50,)
        )

    return ControlRoute("ssl", "send_control_light", (is_on,))


def brightness_route(
    category: DeviceCategory,
    brightness: int,
    current_state: Mapping[str, Any],
    *,
    device_type_raw: Any = None,
) -> ControlRoute:
    """Select a brightness transport and its optimistic normalized state."""

    custom = _custom_attribute_route(
        category, "brightness", current_state, brightness=brightness
    )
    if custom is not None:
        return custom

    if category == DeviceCategory.DIMMABLE_LIGHT:
        value = round(brightness)
        return ControlRoute(
            "ssl",
            "send_control_dimmable_light_brightness",
            (value,),
            optimistic={"brightness": value, "state": True},
        )
    if category == DeviceCategory.ZIGBEE_DIMMABLE_LIGHT:
        value = max(1, min(int(brightness), 255))
        return ControlRoute(
            "ssl",
            "send_control_zigbee_dimmable_light_brightness",
            (value,),
            optimistic={"brightness": value, "state": True},
        )
    if category == DeviceCategory.FAST_MOVE_DIM_COLOR_LIGHT:
        value = max(1, min(int(brightness), 255))
        color_temp = current_state.get("color_temp", 0) or 2700
        mired = 1_000_000 // color_temp if color_temp > 0 else 370
        return ControlRoute(
            "ssl",
            "send_control_fast_move_dim_color_light_brightness",
            (value,),
            {"colortemp_mired": mired},
            {"brightness": value, "state": True},
        )
    if category in (DeviceCategory.CCT_LIGHT, DeviceCategory.CCT_LIGHT_STRIP):
        return ControlRoute(
            "ssl",
            "send_control_cct_light_brightness",
            (brightness,),
            optimistic={"brightness": brightness, "state": True},
        )

    color_temp = current_state.get("color_temp", 0) or 2700
    transport_brightness = (
        round(brightness * 255 / 100)
        if device_type_raw == 503
        else brightness
    )
    return ControlRoute(
        "ssl",
        "send_control_light_colortemp",
        (color_temp,),
        {"brightness": transport_brightness},
        {"brightness": brightness, "state": True},
    )


def color_temp_route(
    category: DeviceCategory,
    color_temp_k: int,
    current_state: Mapping[str, Any],
    *,
    device_type_raw: Any = None,
) -> ControlRoute:
    """Select a color-temperature transport and optimistic state."""

    custom = _custom_attribute_route(
        category, "color_temp", current_state, color_temp=color_temp_k
    )
    if custom is not None:
        return custom

    optimistic = {"color_temp": color_temp_k}
    if category in (DeviceCategory.CCT_LIGHT, DeviceCategory.CCT_LIGHT_STRIP):
        return ControlRoute(
            "ssl",
            "send_control_cct_light_colortemp",
            (color_temp_k,),
            optimistic=optimistic,
        )
    if category == DeviceCategory.FAST_MOVE_DIM_COLOR_LIGHT:
        brightness = current_state.get("brightness", 255) or 255
        mired = 1_000_000 // color_temp_k if color_temp_k > 0 else 370
        return ControlRoute(
            "ssl",
            "send_control_fast_move_dim_color_light_colortemp",
            (brightness,),
            {"colortemp_mired": mired},
            optimistic,
        )

    brightness = current_state.get("brightness", 255)
    transport_brightness = (
        round(brightness * 255 / 100)
        if device_type_raw == 503
        else brightness
    )
    return ControlRoute(
        "ssl",
        "send_control_light_colortemp",
        (color_temp_k,),
        {"brightness": transport_brightness},
        optimistic,
    )
