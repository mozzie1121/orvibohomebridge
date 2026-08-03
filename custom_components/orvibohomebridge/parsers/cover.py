"""Pure parsers for cover status packets."""

from __future__ import annotations

from typing import Any, Mapping

from .base import StatePatch


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
