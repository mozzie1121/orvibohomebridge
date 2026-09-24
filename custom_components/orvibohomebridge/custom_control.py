"""Compile declarative custom-device profiles into runtime behaviour.

Everything here is pure: no I/O, no Home Assistant imports.  The compiled
objects plug into the existing pipeline at exactly the same seams the built-in
categories use:

* :func:`build_state_parser` returns a ``parsers.base.StateParser``, so
  ``device_inventory`` / ``status_dispatcher`` apply it unchanged.
* :func:`build_route` returns a ``ControlRoute``, so ``control_executor``
  sends it through the normal transport selection and confirmation logic.

The actual wire envelope is built from the *verified* command set only.  A
profile can choose ``order``/``value1..4``/``properties``; it cannot invent
protocol semantics.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from .custom_devices import (
    CATEGORY_PREFIX,
    KNOWN_CONTROL_ACTIONS,
    CustomProfile,
    is_custom_category_key,
    profile_for_category_key,
)

#: State fields whose truthiness should also drive the binary ``state`` field.
_ON_ALIASES = ("state", "onoff", "on")


class ControlNotDeclared(RuntimeError):
    """Raised when a custom device is asked for an action its profile lacks.

    This is deliberately an error rather than a fallback: falling back to a
    built-in command would send a payload the profile author never verified.
    """

    def __init__(self, profile_id: str, action: str) -> None:
        super().__init__(
            f"自定义设备 profile {profile_id} 未声明 {action} 控制动作，已拒绝下发"
        )
        self.profile_id = profile_id
        self.action = action


def build_state_parser(profile: CustomProfile):
    """Return a ``StateParser`` for ``profile``.

    Only fields the payload actually carries are patched, so a partial push
    never resets unrelated state.
    """

    specs = dict(profile.state_specs)

    def parse(current_state: Mapping[str, Any], raw_status: Mapping[str, Any]):
        from .parsers.base import StatePatch

        updates: dict[str, Any] = {}
        for field_name, spec in specs.items():
            resolved = spec.resolve(raw_status)
            if resolved is None:
                continue
            updates[field_name] = resolved

        state_value = updates.get("state")
        if not isinstance(state_value, bool):
            for alias in _ON_ALIASES:
                if alias == "state":
                    continue
                candidate = updates.get(alias)
                if isinstance(candidate, bool):
                    state_value = candidate
                    break
        if isinstance(state_value, bool):
            brightness = updates.get("brightness")
            if state_value and isinstance(brightness, (int, float)) and brightness <= 0:
                # A zero brightness packet means the light is physically off.
                state_value = False
            updates["state"] = state_value

        return StatePatch(updates)

    return parse


def profile_for_category(category: Any) -> Optional[CustomProfile]:
    """Resolve a synthetic ``custom:<id>`` category back to its profile."""

    if not is_custom_category_key(category):
        return None
    return profile_for_category_key(category)


def is_custom_category(category: Any) -> bool:
    """Whether the resolved category belongs to a custom-device profile."""

    return is_custom_category_key(category)


def build_route(
    category: Any,
    action: str,
    current_state: Mapping[str, Any],
    *,
    value: int | None = None,
    brightness: int | None = None,
    color_temp: int | None = None,
    position: int | None = None,
    strict: bool = False,
):
    """Compile one profile control action into a ``ControlRoute``.

    Returns ``None`` when the profile does not declare the action so the caller
    can fall back to built-in behaviour.  With ``strict=True`` the caller has
    already established the device is custom, and an undeclared action raises
    :class:`ControlNotDeclared` instead of silently borrowing a built-in command.
    """

    from .control_router import ControlRoute

    if action not in KNOWN_CONTROL_ACTIONS:
        return None
    profile = profile_for_category(category)
    if profile is None:
        return None
    control_action = profile.control.get(action)
    if control_action is None:
        if strict:
            raise ControlNotDeclared(profile.profile_id, action)
        return None

    params: dict[str, Any] = {}
    if position is not None:
        params["position"] = position
    if brightness is not None:
        params["brightness"] = int(brightness)
    if color_temp is not None:
        params["color_temp"] = int(color_temp)
    if value is not None:
        params["value"] = int(value)
    params.setdefault("on", action in ("on", "open"))
    params.setdefault("off", action in ("off", "close"))

    order, numbers, properties = control_action.envelope(current_state, params)
    optimistic = _resolve_optimistic(profile, action, current_state, params)

    return ControlRoute(
        "ssl",
        "send_control_envelope",
        (order, numbers[0], numbers[1], numbers[2], numbers[3]),
        {"properties": properties},
        optimistic,
    )


def _resolve_optimistic(
    profile: CustomProfile,
    action: str,
    current_state: Mapping[str, Any],
    params: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve the profile's optimistic state for an action.

    A declared ``false`` is a real value, not "absent": only ``None`` means the
    field was not resolved.  The implied defaults below therefore never override
    an explicit declaration.
    """

    declared = profile.optimistic.get(action)
    updates: dict[str, Any] = {}
    if declared:
        for field_name, spec in declared.items():
            resolved = spec.resolve(current_state, params)
            if resolved is not None:
                updates[field_name] = resolved
    if action in ("on", "open") and "state" not in updates:
        updates["state"] = True
    if action in ("off", "close") and "state" not in updates:
        updates["state"] = False
    if action == "stop":
        updates.setdefault("cover_action", "stop")
    if action in ("brightness", "color_temp") and "state" not in updates:
        updates["state"] = True
    return updates


def supported_actions(category: Any) -> frozenset[str]:
    """Actions declared by the profile behind ``category`` (diagnostics/tests)."""

    profile = profile_for_category(category)
    return frozenset(profile.control) if profile is not None else frozenset()


__all__ = [
    "CATEGORY_PREFIX",
    "ControlNotDeclared",
    "build_route",
    "build_state_parser",
    "is_custom_category",
    "profile_for_category",
    "supported_actions",
]
