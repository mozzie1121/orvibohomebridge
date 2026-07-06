"""Orvibo 二元传感器平台。

支持设备类别：
- MOTION_SENSOR (deviceType=26) 人体传感器
- DOOR_WINDOW_SENSOR (deviceType=46) 门窗传感器
"""
import logging
from typing import Optional

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import OrviboMeshCoordinator
from .device_types import DeviceCategory, classify_device

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: OrviboMeshCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    entities = []
    for device_id, device in coordinator.devices.items():
        category = classify_device(device)
        if category == DeviceCategory.MOTION_SENSOR:
            entities.append(OrviboMotionSensor(coordinator, device))
        elif category == DeviceCategory.DOOR_WINDOW_SENSOR:
            entities.append(OrviboDoorWindowSensor(coordinator, device))
        elif category == DeviceCategory.DOOR_LOCK:
            entities.append(OrviboDoorLockDoorSensor(coordinator, device))
            entities.append(OrviboDoorLockLockSensor(coordinator, device))
            entities.append(OrviboDoorLockDoorbellSensor(coordinator, device))
            entities.append(OrviboDoorLockUnlockSensor(coordinator, device))
        elif category == DeviceCategory.SMOKE_SENSOR:
            entities.append(OrviboSmokeSensor(coordinator, device))
        elif category == DeviceCategory.EMERGENCY_BUTTON:
            entities.append(OrviboEmergencyButton(coordinator, device))

    async_add_entities(entities)


class OrviboMotionSensor(CoordinatorEntity, BinarySensorEntity):
    """人体传感器（deviceType=26）。"""

    def __init__(self, coordinator: OrviboMeshCoordinator, device: dict):
        super().__init__(coordinator)
        self._device = device
        self._device_id = device.get("device_id", "")
        self._attr_unique_id = f"orvibohomebridge_motion_{self._device_id}"
        self._attr_name = "人体检测"
        self._attr_device_class = BinarySensorDeviceClass.MOTION
        self._attr_icon = "mdi:motion-sensor"

    @property
    def is_on(self) -> Optional[bool]:
        state = self.coordinator.get_device_state(self._device_id)
        return state.get("motion_detected", False) if state else False

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._device.get("device_name", "Orvibo Motion Sensor"),
            "manufacturer": MANUFACTURER,
            "model": "Motion Sensor",
            "sw_version": "1.0",
        }


class OrviboDoorWindowSensor(CoordinatorEntity, BinarySensorEntity):
    """门窗传感器（deviceType=46）。"""

    def __init__(self, coordinator: OrviboMeshCoordinator, device: dict):
        super().__init__(coordinator)
        self._device = device
        self._device_id = device.get("device_id", "")
        self._attr_unique_id = f"orvibohomebridge_door_{self._device_id}"
        self._attr_name = "门磁状态"
        self._attr_device_class = BinarySensorDeviceClass.DOOR
        self._attr_icon = "mdi:door-open"

    @property
    def is_on(self) -> Optional[bool]:
        state = self.coordinator.get_device_state(self._device_id)
        return state.get("door_state", False) if state else False

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._device.get("device_name", "Orvibo Door Window Sensor"),
            "manufacturer": MANUFACTURER,
            "model": "Door Window Sensor",
            "sw_version": "1.0",
        }


class OrviboDoorLockDoorSensor(CoordinatorEntity, BinarySensorEntity):
    """智能门锁 - 门磁状态（deviceType=522）。"""

    def __init__(self, coordinator: OrviboMeshCoordinator, device: dict):
        super().__init__(coordinator)
        self._device = device
        self._device_id = device.get("device_id", "")
        self._attr_unique_id = f"orvibohomebridge_door_lock_door_{self._device_id}"
        self._attr_name = "门磁状态"
        self._attr_device_class = BinarySensorDeviceClass.DOOR
        self._attr_icon = "mdi:door-open"

    @property
    def is_on(self) -> Optional[bool]:
        state = self.coordinator.get_device_state(self._device_id)
        return state.get("door_state", False) if state else False

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._device.get("device_name", "Orvibo Door Lock"),
            "manufacturer": MANUFACTURER,
            "model": "Smart Lock",
            "sw_version": "1.0",
        }


class OrviboDoorLockLockSensor(CoordinatorEntity, BinarySensorEntity):
    """智能门锁 - 锁状态（deviceType=522）。"""

    def __init__(self, coordinator: OrviboMeshCoordinator, device: dict):
        super().__init__(coordinator)
        self._device = device
        self._device_id = device.get("device_id", "")
        self._attr_unique_id = f"orvibohomebridge_door_lock_lock_{self._device_id}"
        self._attr_name = "锁状态"
        self._attr_device_class = BinarySensorDeviceClass.LOCK
        self._attr_icon = "mdi:lock"

    @property
    def is_on(self) -> Optional[bool]:
        state = self.coordinator.get_device_state(self._device_id)
        return state.get("lock_state", False) if state else False

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._device.get("device_name", "Orvibo Door Lock"),
            "manufacturer": MANUFACTURER,
            "model": "Smart Lock",
            "sw_version": "1.0",
        }


class OrviboSmokeSensor(CoordinatorEntity, BinarySensorEntity):
    """烟雾传感器（deviceType=27）。"""

    def __init__(self, coordinator: OrviboMeshCoordinator, device: dict):
        super().__init__(coordinator)
        self._device = device
        self._device_id = device.get("device_id", "")
        self._attr_unique_id = f"orvibohomebridge_smoke_{self._device_id}"
        self._attr_name = "烟雾检测"
        self._attr_device_class = BinarySensorDeviceClass.SMOKE
        self._attr_icon = "mdi:smoke-detector"

    @property
    def is_on(self) -> Optional[bool]:
        state = self.coordinator.get_device_state(self._device_id)
        return state.get("smoke_detected", False) if state else False

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._device.get("device_name", "Orvibo Smoke Sensor"),
            "manufacturer": MANUFACTURER,
            "model": "Smoke Sensor",
            "sw_version": "1.0",
        }


class OrviboEmergencyButton(CoordinatorEntity, BinarySensorEntity):
    """紧急按钮（deviceType=93）。"""

    def __init__(self, coordinator: OrviboMeshCoordinator, device: dict):
        super().__init__(coordinator)
        self._device = device
        self._device_id = device.get("device_id", "")
        self._attr_unique_id = f"orvibohomebridge_emergency_{self._device_id}"
        self._attr_name = "紧急按钮"
        self._attr_device_class = BinarySensorDeviceClass.PROBLEM
        self._attr_icon = "mdi:alert-octagon"

    @property
    def is_on(self) -> Optional[bool]:
        state = self.coordinator.get_device_state(self._device_id)
        return state.get("state", False) if state else False

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._device.get("device_name", "Orvibo Emergency Button"),
            "manufacturer": MANUFACTURER,
            "model": "Emergency Button",
            "sw_version": "1.0",
        }


class OrviboDoorLockDoorbellSensor(CoordinatorEntity, BinarySensorEntity):
    """智能门锁 - 门铃事件（deviceType=522）。"""

    def __init__(self, coordinator: OrviboMeshCoordinator, device: dict):
        super().__init__(coordinator)
        self._device = device
        self._device_id = device.get("device_id", "")
        self._attr_unique_id = f"orvibohomebridge_door_lock_doorbell_{self._device_id}"
        self._attr_name = "门铃"
        self._attr_device_class = BinarySensorDeviceClass.OCCUPANCY
        self._attr_icon = "mdi:bell-ring"

    @property
    def is_on(self) -> Optional[bool]:
        state = self.coordinator.get_device_state(self._device_id)
        return state.get("doorbell_ring", False) if state else False

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._device.get("device_name", "Orvibo Door Lock"),
            "manufacturer": MANUFACTURER,
            "model": "Smart Lock",
            "sw_version": "1.0",
        }


class OrviboDoorLockUnlockSensor(CoordinatorEntity, BinarySensorEntity):
    """智能门锁 - 开锁事件（deviceType=522）。"""

    def __init__(self, coordinator: OrviboMeshCoordinator, device: dict):
        super().__init__(coordinator)
        self._device = device
        self._device_id = device.get("device_id", "")
        self._attr_unique_id = f"orvibohomebridge_door_lock_unlock_{self._device_id}"
        self._attr_name = "开锁事件"
        self._attr_device_class = BinarySensorDeviceClass.OCCUPANCY
        self._attr_icon = "mdi:key"

    @property
    def is_on(self) -> Optional[bool]:
        state = self.coordinator.get_device_state(self._device_id)
        return state.get("unlock_event", False) if state else False

    @property
    def extra_state_attributes(self):
        state = self.coordinator.get_device_state(self._device_id)
        if not state:
            return {}
        return {
            "unlock_type": state.get("unlock_type"),
            "unlock_user_id": state.get("unlock_user_id"),
        }

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._device.get("device_name", "Orvibo Door Lock"),
            "manufacturer": MANUFACTURER,
            "model": "Smart Lock",
            "sw_version": "1.0",
        }