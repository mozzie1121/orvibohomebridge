"""Device selection and area mapping helpers for Orvibo config entries.

参照 orvibo-cloud 的设计，支持用户在配置流程中选择要暴露的设备，
并自动将 ORVIBO 房间映射到 Home Assistant 区域。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

CONF_SELECTED_DEVICE_IDS = "selected_device_ids"
CONF_SELECTED_DEVICE_GROUPS = "selected_device_groups"
CONF_DEVICE_AREAS = "device_areas"


def selected_device_ids(
    options: Mapping[str, Any],
    available_device_ids: Iterable[Any] | Mapping[str, Any],
) -> set[str]:
    """返回用户选择的设备 ID，默认（旧配置）返回全部可用设备。"""
    available, devices = _available_devices(available_device_ids)
    if CONF_SELECTED_DEVICE_IDS not in options:
        return available
    configured = options.get(CONF_SELECTED_DEVICE_IDS)
    selected = (
        {str(device_id) for device_id in configured}
        if isinstance(configured, (list, tuple, set))
        else set()
    )
    selected |= _selected_group_device_ids(options, devices)
    return selected & available


def device_is_selected(options: Mapping[str, Any], device_id: str) -> bool:
    """检查某个设备是否被选中。"""
    if CONF_SELECTED_DEVICE_IDS not in options:
        return True
    configured = options.get(CONF_SELECTED_DEVICE_IDS)
    if not isinstance(configured, (list, tuple, set)):
        return False
    return device_id in {str(value) for value in configured}


def configured_device_areas(options: Mapping[str, Any]) -> dict[str, str | None]:
    """返回配置的区域映射，过滤无效值。"""
    configured = options.get(CONF_DEVICE_AREAS)
    if not isinstance(configured, Mapping):
        return {}
    areas: dict[str, str | None] = {}
    for device_id, area_id in configured.items():
        if area_id is None:
            areas[str(device_id)] = None
        elif isinstance(area_id, str) and area_id:
            areas[str(device_id)] = area_id
    return areas


def cleaned_selection_options(
    options: Mapping[str, Any],
    available_device_ids: Iterable[Any] | Mapping[str, Any],
) -> dict[str, Any]:
    """Return options with stale device IDs removed from selection and areas."""

    available, _devices = _available_devices(available_device_ids)
    cleaned = dict(options)
    configured = cleaned.get(CONF_SELECTED_DEVICE_IDS)
    if isinstance(configured, (list, tuple, set)):
        cleaned[CONF_SELECTED_DEVICE_IDS] = [
            str(device_id)
            for device_id in configured
            if str(device_id) in available
        ]

    areas = cleaned.get(CONF_DEVICE_AREAS)
    if isinstance(areas, Mapping):
        cleaned[CONF_DEVICE_AREAS] = {
            str(device_id): area_id
            for device_id, area_id in areas.items()
            if str(device_id) in available
        }
    return cleaned


def _available_devices(
    available_device_ids: Iterable[Any] | Mapping[str, Any],
) -> tuple[set[str], dict[str, Mapping[str, Any]]]:
    """Normalize either an ID iterable, a device mapping, or device dicts."""

    if isinstance(available_device_ids, Mapping):
        devices = {
            str(device_id): device
            for device_id, device in available_device_ids.items()
            if isinstance(device, Mapping)
        }
        return {str(device_id) for device_id in available_device_ids}, devices

    available: set[str] = set()
    devices: dict[str, Mapping[str, Any]] = {}
    for item in available_device_ids:
        if isinstance(item, Mapping):
            device_id = str(item.get("device_id") or "")
            if not device_id:
                continue
            available.add(device_id)
            devices[device_id] = item
        else:
            available.add(str(item))
    return available, devices


def _selected_group_device_ids(
    options: Mapping[str, Any], devices: Mapping[str, Mapping[str, Any]]
) -> set[str]:
    groups = options.get(CONF_SELECTED_DEVICE_GROUPS)
    if not isinstance(groups, (list, tuple, set)) or not devices:
        return set()
    selected_groups = {str(group) for group in groups}
    try:
        from .device_selection import infer_group
    except Exception:  # noqa: BLE001 - tests may load this module standalone
        infer_group = _infer_group_fallback
    return {
        device_id
        for device_id, device in devices.items()
        if infer_group(device) in selected_groups
    }


def _infer_group_fallback(device: Mapping[str, Any]) -> str:
    """Small standalone fallback used when selection.py is loaded outside package."""

    raw = device.get("device_type_raw", device.get("device_type"))
    try:
        raw_type = int(raw)
    except (TypeError, ValueError):
        raw_type = -1
    if raw_type in {14, 162}:
        return "cameras"
    if raw_type == 522:
        return "locks"
    if raw_type in {34, 35}:
        return "covers"
    if raw_type in {36, 81, 516, 1114}:
        return "climate"
    if raw_type in {22, 23, 25, 26, 27, 46, 54, 56}:
        return "sensors"
    if raw_type in {1, 38, 43, 500, 501, 502, 503, 518}:
        return "lights"
    return "other"
