"""Pure parser for normalized smart-lock state."""

from __future__ import annotations

from typing import Any, Mapping

from ..lock_status import (
    derive_lock_status,
    normalize_battery_properties,
    normalize_door_lock_properties,
)
from .base import StatePatch


def parse_door_lock(
    current_state: Mapping[str, Any], raw_status: Mapping[str, Any]
) -> StatePatch:
    """Parse partial lock properties without resetting absent fields."""

    properties = raw_status.get("properties")
    lock = normalize_door_lock_properties(properties)
    updates: dict[str, Any] = {}
    if lock["locked"] is not None:
        updates["locked"] = lock["locked"]
        updates["lock_state"] = not lock["locked"]
    if lock["door_open"] is not None:
        updates["door_state"] = lock["door_open"]
    if lock["inside_locked"] is not None:
        updates["inside_lock_state"] = lock["inside_locked"]
    if lock["child_locked"] is not None:
        updates["child_lock_state"] = lock["child_locked"]
    if lock["leave_home_armed"] is not None:
        updates["leave_home_armed"] = lock["leave_home_armed"]

    battery = normalize_battery_properties(properties)
    for key in (
        "dry_battery_level",
        "dry_battery_setup",
        "lithium_battery_level",
        "lithium_battery_setup",
    ):
        if key in battery:
            updates[key] = battery[key]

    effective = dict(current_state)
    effective.update(updates)
    updates["lock_status"] = derive_lock_status(
        effective.get("locked"),
        effective.get("door_state"),
        effective.get("inside_lock_state"),
    )
    updates["state"] = effective.get("lock_state", False)
    return StatePatch(updates)
