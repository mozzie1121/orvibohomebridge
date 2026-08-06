"""Device discovery and cloud inventory reconciliation."""

from __future__ import annotations

import logging
from typing import Any, Callable, MutableMapping

from .device_types import DeviceCategory, classify_device, is_hidden_category
from .lock_status import normalize_battery_properties
from .parsers import get_state_parser
from .state_store import StateSource, StateStore


_LOGGER = logging.getLogger(__name__)

_BATTERY_FIELDS = (
    "dry_battery_level",
    "dry_battery_setup",
    "lithium_battery_level",
    "lithium_battery_setup",
)


class DeviceInventory:
    """Own device discovery, initial state construction, and cloud merges."""

    def __init__(
        self,
        client: Any,
        devices: MutableMapping[str, dict[str, Any]],
        states: MutableMapping[str, dict[str, Any]],
        state_store: StateStore,
        on_removed: Callable[[str], None],
    ) -> None:
        self.client = client
        self.devices = devices
        self.states = states
        self.state_store = state_store
        self._on_removed = on_removed

    async def discover(self) -> tuple[Any, list[dict[str, Any]]]:
        """Discover devices via readtable, description, then homepage data."""
        status_data = await self.client.fetch_device_status()
        if not status_data:
            return None, []

        devices = self.client.parse_device_status_list(status_data)
        if not devices:
            _LOGGER.warning(
                "readtable 未解析到设备，回退到 getDeviceDesc 构建设备列表..."
            )
            description = await self.client.fetch_device_desc(last_update_time=0)
            if description:
                described = description.get(
                    "deviceDescList", description.get("devices", [])
                )
                if isinstance(described, list) and described:
                    devices = self.client.parse_device_status_list(
                        {"device": described, "deviceStatus": {}}
                    )
                    _LOGGER.info("getDeviceDesc 回退解析到 %s 个设备", len(devices))

        if not devices:
            _LOGGER.warning(
                "getDeviceDesc 未构建到设备，回退到 queryHomepageData..."
            )
            homepage = await self.client.fetch_homepage_data()
            if isinstance(homepage, dict):
                homepage_devices = homepage.get(
                    "deviceList", homepage.get("device", [])
                ) or []
                if isinstance(homepage_devices, list) and homepage_devices:
                    devices = self.client.parse_device_status_list(
                        {"device": homepage_devices, "deviceStatus": {}}
                    )
                    _LOGGER.info(
                        "queryHomepageData 回退解析到 %s 个设备", len(devices)
                    )

        return status_data, devices

    def initialize(self, discovered: list[dict[str, Any]]) -> None:
        """Populate visible devices and their category-specific initial state."""
        for device in discovered:
            device_id = device["device_id"]
            category = classify_device(device)
            _LOGGER.info(
                "设备分类: deviceId=%s, name=%s, deviceType=%s, category=%s",
                device_id,
                device.get("device_name", ""),
                device.get("device_type_raw", ""),
                category.name,
            )
            if is_hidden_category(category):
                _LOGGER.debug(
                    "[过滤] 跳过隐藏类别设备: %s category=%s",
                    device_id,
                    category.name,
                )
                continue

            self.devices[device_id] = device
            state = self._initial_state(device)
            parser = get_state_parser(category)
            if parser is not None:
                parser(
                    state,
                    {
                        "properties": device.get("properties", {}),
                        "value1": device.get("value1"),
                        "value2": device.get("value2"),
                        "value3": device.get("value3"),
                        "value4": device.get("value4"),
                    },
                ).apply_to(state)
            if category == DeviceCategory.DOOR_LOCK:
                state.update(self._battery_state(device))
            elif category == DeviceCategory.CLOTHES_HORSE:
                state.update(
                    {
                        "motor_state": "stop",
                        "lighting_state": False,
                        "heat_drying_state": False,
                        "wind_drying_state": False,
                        "sterilizing_state": False,
                        "main_switch_state": False,
                    }
                )
            elif category == DeviceCategory.VENTILATION_SYSTEM:
                state.update(
                    {
                        "fan_speed": device.get("fan_speed", "停"),
                        "temperature": device.get("temperature"),
                    }
                )
            self.states[device_id] = state

    def merge_cloud(self, discovered: list[dict[str, Any]]) -> None:
        """Merge a periodic cloud snapshot without overwriting fresh SSL fields."""
        for device in discovered:
            device_id = device["device_id"]
            category = classify_device(device)
            if is_hidden_category(category):
                self._remove(device_id)
                continue

            self.devices[device_id] = device
            if device_id not in self.states:
                self.states[device_id] = self._periodic_initial_state(device)

            cloud_state = {
                field: device[field]
                for field in (
                    "state",
                    "online",
                    "position",
                    "brightness",
                    "color_temp",
                    "temperature",
                    "humidity",
                )
                if field in device and device[field] is not None
            }
            status = device.get("status", {})
            if isinstance(status, dict):
                cloud_state.update(status)
            if cloud_state:
                self.state_store.merge(
                    device_id, cloud_state, StateSource.CLOUD
                )
            if category == DeviceCategory.DOOR_LOCK:
                self.state_store.merge(
                    device_id,
                    self._battery_state(device),
                    StateSource.CLOUD,
                )
                # 门锁为 cloud_only：周期云端快照需覆盖门磁/锁状态字段，
                # 仅靠 SSL 推送维护会在推送丢失/重启后残留旧值。
                # StateStore guard 保证 30s 内新的 SSL 值不被覆盖。
                lock_state: dict[str, Any] = {}
                parser = get_state_parser(category)
                if parser is not None:
                    parser(
                        lock_state,
                        {
                            "properties": device.get("properties", {}),
                            "value1": device.get("value1"),
                            "value2": device.get("value2"),
                            "value3": device.get("value3"),
                            "value4": device.get("value4"),
                        },
                    ).apply_to(lock_state)
                if lock_state:
                    self.state_store.merge(
                        device_id, lock_state, StateSource.CLOUD
                    )

    def _remove(self, device_id: str) -> None:
        self.devices.pop(device_id, None)
        self.states.pop(device_id, None)
        self.state_store.remove(device_id)
        self._on_removed(device_id)

    @staticmethod
    def _initial_state(device: dict[str, Any]) -> dict[str, Any]:
        online = device.get("online", False)
        if isinstance(online, str):
            online = online.strip().lower() in ("online", "1", "true", "yes")
        return {
            "state": device.get("state", False),
            "online": bool(online),
            "position": device.get("position", 0),
            "brightness": device.get("brightness"),
            "color_temp": device.get("color_temp"),
            "uid": device.get("uid", ""),
            "status_id": device.get("status_id", ""),
            "app_device_id": device.get("app_device_id", ""),
            "gateway_id": device.get("gateway_id", ""),
            "ext_addr": device.get("ext_addr"),
            "properties": dict(device.get("properties") or {}),
            "raw_value1": device.get("value1"),
            "raw_value2": device.get("value2"),
            "raw_value3": device.get("value3"),
            "raw_value4": device.get("value4"),
        }

    @staticmethod
    def _periodic_initial_state(device: dict[str, Any]) -> dict[str, Any]:
        return {
            "state": device.get("state", False),
            "online": device.get("online", False),
            "position": device.get("position", 0),
            "brightness": device.get("brightness"),
            "color_temp": device.get("color_temp"),
            "properties": dict(device.get("properties") or {}),
        }

    @staticmethod
    def _battery_state(device: dict[str, Any]) -> dict[str, Any]:
        normalized = normalize_battery_properties(device.get("properties") or {})
        return {
            field: normalized[field]
            for field in _BATTERY_FIELDS
            if field in normalized
        }
