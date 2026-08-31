"""Pure parsers for cover status packets."""

from __future__ import annotations

from typing import Any, Mapping

from .base import StatePatch, to_int


def parse_curtain(
    current_state: Mapping[str, Any], raw_status: Mapping[str, Any]
) -> StatePatch:
    """Parse a 0-100 curtain position while preserving partial-open state."""

    props = raw_status.get("properties", {})
    position = raw_status.get("value1")
    if position is None:
        position = props.get("percent")
    if position is not None:
        try:
            position = int(position)
        except (TypeError, ValueError):
            position = None

    if position == 100:
        state = True
    elif position == 0 or position is None:
        state = False
    else:
        state = current_state.get("state", False)
    return StatePatch({"position": position, "state": state})


def parse_dream_curtain(
    current_state: Mapping[str, Any], raw_status: Mapping[str, Any]
) -> StatePatch:
    """Parse independently reported dream-curtain position and blade angle."""
    props = raw_status.get("properties", {})
    curtain = props.get("curtain", {}) if isinstance(props, Mapping) else {}
    if not isinstance(curtain, Mapping):
        return StatePatch({})

    updates: dict[str, Any] = {}
    if "percent" in curtain:
        position = to_int(curtain["percent"])
        if position is not None:
            position = max(0, min(100, position))
            updates.update({"position": position, "state": position > 0})
    if "angle" in curtain:
        angle = to_int(curtain["angle"])
        if angle is not None:
            updates["angle"] = max(0, min(180, angle))
    if "action" in curtain:
        updates["cover_action"] = str(curtain["action"])
    return StatePatch(updates)
