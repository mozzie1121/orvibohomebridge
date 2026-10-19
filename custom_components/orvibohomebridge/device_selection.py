"""Group devices into broad categories for config-flow selection."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .capabilities import capability_for
from .device_types import DeviceCategory, classify_device
from .selection import CONF_SELECTED_DEVICE_IDS

_LOGGER = logging.getLogger(__name__)

GROUP_FIELD_PREFIX = "device_group_"
GROUP_ALL_VALUE_PREFIX = "__all__:"

GROUPS: tuple[tuple[str, str], ...] = (
    ("lights", "灯光"),
    ("covers", "窗帘"),
    ("climate", "空调"),
    ("sensors", "传感器"),
    ("locks", "门锁"),
    ("cameras", "摄像头"),
    ("other", "其他"),
)


@dataclass(frozen=True, slots=True)
class DeviceSelectionGroup:
    key: str
    label: str
    devices: tuple[Mapping[str, Any], ...]

    @property
    def field(self) -> str:
        return f"{GROUP_FIELD_PREFIX}{self.key}"

    @property
    def all_value(self) -> str:
        return f"{GROUP_ALL_VALUE_PREFIX}{self.key}"

    @property
    def device_ids(self) -> tuple[str, ...]:
        return tuple(str(device.get("device_id") or "") for device in self.devices)


def infer_group(device: Mapping[str, Any]) -> str:
    """Classify a device into one of the six user-facing groups."""

    category = classify_device(device)
    if category == DeviceCategory.DOOR_LOCK:
        return "locks"
    camera_categories = {
        getattr(DeviceCategory, name, None)
        for name in ("WIFI_CAMERA", "CAMERA_NVR")
    }
    if category in camera_categories - {None}:
        return "cameras"

    platforms = capability_for(device).platforms
    if "camera" in platforms:
        return "cameras"
    if platforms & {"light", "switch"}:
        return "lights"
    if "cover" in platforms:
        return "covers"
    if platforms & {"climate", "fan"}:
        return "climate"
    if platforms & {"sensor", "binary_sensor"}:
        return "sensors"
    return "other"


def device_selection_groups(
    devices: Iterable[Mapping[str, Any]],
) -> tuple[DeviceSelectionGroup, ...]:
    buckets: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for device in devices:
        buckets[infer_group(device)].append(device)
    groups = tuple(
        DeviceSelectionGroup(key, label, tuple(buckets[key]))
        for key, label in GROUPS
        if buckets[key]
    )
    if groups:
        _LOGGER.info(
            "设备选择分组: %s",
            ", ".join(f"{g.label}({len(g.devices)})" for g in groups),
        )
        other = next((g for g in groups if g.key == "other"), None)
        if other is not None:
            _LOGGER.info(
                "未归类设备(%s): %s",
                len(other.devices),
                ", ".join(
                    f"{d.get('device_name') or d.get('device_id')}"
                    f"[type={d.get('device_type_raw')},platform={d.get('device_type')}]"
                    for d in other.devices[:10]
                ),
            )
    return groups


def merge_grouped_selection(
    user_input: Mapping[str, Any],
    devices: Iterable[Mapping[str, Any]],
) -> list[str]:
    """Flatten grouped fields, keeping group-all and individuals mutually exclusive."""

    ordered = list(devices)
    available = {str(device.get("device_id") or "") for device in ordered}
    if CONF_SELECTED_DEVICE_IDS in user_input:
        values = user_input.get(CONF_SELECTED_DEVICE_IDS, [])
        requested = {str(value) for value in values} if isinstance(
            values, (list, tuple, set)
        ) else set()
    else:
        requested: set[str] = set()
        for group in device_selection_groups(ordered):
            values = user_input.get(group.field, [])
            selected = {str(value) for value in values} if isinstance(
                values, (list, tuple, set)
            ) else set()
            individuals = selected & set(group.device_ids)
            # Individual selections win when the UI submits both forms. This
            # makes the persisted state mutually exclusive after submission.
            if individuals:
                requested.update(individuals)
            elif group.all_value in selected:
                requested.update(group.device_ids)

    requested &= available
    return [
        str(device.get("device_id") or "")
        for device in ordered
        if str(device.get("device_id") or "") in requested
    ]


__all__ = [
    "DeviceSelectionGroup",
    "device_selection_groups",
    "infer_group",
    "merge_grouped_selection",
]
