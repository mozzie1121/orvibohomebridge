"""Orvibo 传感器平台。

支持设备类别：
- MOTION_SENSOR (deviceType=26) 人体传感器
- SMOKE_SENSOR (deviceType=27) 烟雾传感器
- EMERGENCY_BUTTON (deviceType=56) 紧急按钮
- DOOR_LOCK (deviceType=300) 智能门锁
- TEMP_HUMIDITY_SENSOR (deviceType=300 subType=491) 温湿度传感器
- DOOR_WINDOW_SENSOR (deviceType=46) 门窗传感器
"""
import logging
from typing import Optional

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, DEVICE_TYPE_SENSOR
from .coordinator import OrviboMeshCoordinator
from .device_types import DeviceCategory, classify_device
from .selection import selected_device_ids

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: OrviboMeshCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    selected_ids = selected_device_ids(config_entry.options, coordinator.devices)

    entities = []
    for device_id, device in coordinator.devices.items():
        if device_id not in selected_ids:
            continue
        if device.get("device_type") != DEVICE_TYPE_SENSOR:
            continue
        category = classify_device(device)
        if category == DeviceCategory.MOTION_SENSOR:
            entities.append(OrviboMotionBatterySensor(coordinator, device))
        elif category == DeviceCategory.TEMP_HUMIDITY_SENSOR:
            entities.append(OrviboTemperatureSensor(coordinator, device))
            entities.append(OrviboHumiditySensor(coordinator, device))
            entities.append(OrviboBatterySensor(coordinator, device))
        elif category == DeviceCategory.DOOR_WINDOW_SENSOR:
            entities.append(OrviboDoorWindowBatterySensor(coordinator, device))
        elif category == DeviceCategory.SMOKE_SENSOR:
            entities.append(OrviboSmokeBatterySensor(coordinator, device))
        elif category == DeviceCategory.EMERGENCY_BUTTON:
            entities.append(OrviboEmergencyButtonBatterySensor(coordinator, device))
        elif category == DeviceCategory.WATER_LEAK_SENSOR:
            entities.append(OrviboWaterLeakBatterySensor(coordinator, device))
        elif category == DeviceCategory.GAS_SENSOR:
            entities.append(OrviboGasBatterySensor(coordinator, device))
        elif category == DeviceCategory.DOOR_LOCK:
            entities.append(OrviboDoorLockDryBatterySensor(coordinator, device))
            entities.append(OrviboDoorLockLithiumBatterySensor(coordinator, device))
            entities.append(OrviboDoorLockStateSensor(coordinator, device))
            entities.append(OrviboDoorLockUnlockSensor(coordinator, device))

    async_add_entities(entities)


class OrviboSensorBase(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: OrviboMeshCoordinator, device: dict):
        super().__init__(coordinator)
        self._device = device
        self._device_id = device["device_id"]
        self._attr_unique_id = f"orvibohomebridge_sensor_{self._device_id}"
        self._attr_name = device.get("device_name", self._device_id)

    @property
    def available(self) -> bool:
        state = self.coordinator.get_device_state(self._device_id)
        return state.get("online", False) if state else False

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._device.get("device_name", self._device_id),
            "manufacturer": MANUFACTURER,
            "model": self._device.get("model", "Orvibo Sensor"),
            "sw_version": "1.0",
        }


class OrviboMotionBatterySensor(OrviboSensorBase):
    """人体传感器 - 电量（deviceType=26）。"""

    def __init__(self, coordinator: OrviboMeshCoordinator, device: dict):
        super().__init__(coordinator, device)
        self._attr_unique_id = f"orvibohomebridge_motion_battery_{self._device_id}"
        self._attr_name = "电量"
        self._attr_device_class = SensorDeviceClass.BATTERY
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = "%"

    @property
    def native_value(self) -> Optional[int]:
        state = self.coordinator.get_device_state(self._device_id)
        if state:
            bat = state.get("battery")
            if bat is not None:
                return int(bat)
        return None


class OrviboTemperatureSensor(OrviboSensorBase):
    """温湿度传感器 - 温度（deviceType=300 subType=491）。"""

    def __init__(self, coordinator: OrviboMeshCoordinator, device: dict):
        super().__init__(coordinator, device)
        self._attr_unique_id = f"orvibohomebridge_temp_{self._device_id}"
        self._attr_name = "温度"
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = "°C"

    @property
    def native_value(self) -> Optional[float]:
        state = self.coordinator.get_device_state(self._device_id)
        if state:
            temp = state.get("temperature")
            if temp is not None:
                return float(temp)
        return None


class OrviboHumiditySensor(OrviboSensorBase):
    """温湿度传感器 - 湿度（deviceType=300 subType=491）。"""

    def __init__(self, coordinator: OrviboMeshCoordinator, device: dict):
        super().__init__(coordinator, device)
        self._attr_unique_id = f"orvibohomebridge_humidity_{self._device_id}"
        self._attr_name = "湿度"
        self._attr_device_class = SensorDeviceClass.HUMIDITY
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = "%"

    @property
    def native_value(self) -> Optional[float]:
        state = self.coordinator.get_device_state(self._device_id)
        if state:
            hum = state.get("humidity")
            if hum is not None:
                return float(hum)
        return None


class OrviboBatterySensor(OrviboSensorBase):
    """温湿度传感器 - 电量（deviceType=300 subType=491）。"""

    def __init__(self, coordinator: OrviboMeshCoordinator, device: dict):
        super().__init__(coordinator, device)
        self._attr_unique_id = f"orvibohomebridge_battery_{self._device_id}"
        self._attr_name = "电量"
        self._attr_device_class = SensorDeviceClass.BATTERY
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = "%"

    @property
    def native_value(self) -> Optional[int]:
        state = self.coordinator.get_device_state(self._device_id)
        if state:
            bat = state.get("battery")
            if bat is not None:
                return int(bat)
        return None


class OrviboDoorWindowBatterySensor(OrviboSensorBase):
    """门窗传感器 - 电量（deviceType=46）。"""

    def __init__(self, coordinator: OrviboMeshCoordinator, device: dict):
        super().__init__(coordinator, device)
        self._attr_unique_id = f"orvibohomebridge_door_battery_{self._device_id}"
        self._attr_name = "电量"
        self._attr_device_class = SensorDeviceClass.BATTERY
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = "%"

    @property
    def native_value(self) -> Optional[int]:
        state = self.coordinator.get_device_state(self._device_id)
        if state:
            bat = state.get("battery")
            if bat is not None:
                return int(bat)
        return None


class OrviboSmokeBatterySensor(OrviboSensorBase):
    """烟雾传感器 - 电量（deviceType=27）。"""

    def __init__(self, coordinator: OrviboMeshCoordinator, device: dict):
        super().__init__(coordinator, device)
        self._attr_unique_id = f"orvibohomebridge_smoke_battery_{self._device_id}"
        self._attr_name = "电量"
        self._attr_device_class = SensorDeviceClass.BATTERY
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = "%"

    @property
    def native_value(self) -> Optional[int]:
        state = self.coordinator.get_device_state(self._device_id)
        if state:
            bat = state.get("battery")
            if bat is not None:
                return int(bat)
        return None


class OrviboEmergencyButtonBatterySensor(OrviboSensorBase):
    """紧急按钮 - 电量（deviceType=56）。"""

    def __init__(self, coordinator: OrviboMeshCoordinator, device: dict):
        super().__init__(coordinator, device)
        self._attr_unique_id = f"orvibohomebridge_emergency_battery_{self._device_id}"
        self._attr_name = "电量"
        self._attr_device_class = SensorDeviceClass.BATTERY
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = "%"

    @property
    def native_value(self) -> Optional[int]:
        state = self.coordinator.get_device_state(self._device_id)
        if state:
            bat = state.get("battery")
            if bat is not None:
                return int(bat)
        return None


class OrviboWaterLeakBatterySensor(OrviboSensorBase):
    """水浸探测器 - 电量（deviceType=54）。"""

    def __init__(self, coordinator: OrviboMeshCoordinator, device: dict):
        super().__init__(coordinator, device)
        self._attr_unique_id = f"orvibohomebridge_water_leak_battery_{self._device_id}"
        self._attr_name = "电量"
        self._attr_device_class = SensorDeviceClass.BATTERY
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = "%"

    @property
    def native_value(self) -> Optional[int]:
        state = self.coordinator.get_device_state(self._device_id)
        if state:
            bat = state.get("battery")
            if bat is not None:
                return int(bat)
        return None


class OrviboGasBatterySensor(OrviboSensorBase):
    """可燃气体探测器 - 电量（deviceType=25）。"""

    def __init__(self, coordinator: OrviboMeshCoordinator, device: dict):
        super().__init__(coordinator, device)
        self._attr_unique_id = f"orvibohomebridge_gas_battery_{self._device_id}"
        self._attr_name = "电量"
        self._attr_device_class = SensorDeviceClass.BATTERY
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = "%"

    @property
    def native_value(self) -> Optional[int]:
        state = self.coordinator.get_device_state(self._device_id)
        if state:
            bat = state.get("battery")
            if bat is not None:
                return int(bat)
        return None


class OrviboDoorLockDryBatterySensor(OrviboSensorBase):
    """智能门锁 - 干电池电量（deviceType=522）。"""

    def __init__(self, coordinator: OrviboMeshCoordinator, device: dict):
        super().__init__(coordinator, device)
        self._attr_unique_id = f"orvibohomebridge_door_lock_dry_battery_{self._device_id}"
        self._attr_name = "干电池电量"
        self._attr_device_class = SensorDeviceClass.BATTERY
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = "%"

    @property
    def native_value(self) -> Optional[int]:
        state = self.coordinator.get_device_state(self._device_id)
        if state:
            bat = state.get("dry_battery_level")
            if bat is not None:
                return int(bat)
        return None

    @property
    def extra_state_attributes(self):
        state = self.coordinator.get_device_state(self._device_id)
        if not state:
            return {}
        return {
            "is_setup": state.get("dry_battery_setup"),
        }


class OrviboDoorLockLithiumBatterySensor(OrviboSensorBase):
    """智能门锁 - 锂电池电量（deviceType=522）。"""

    def __init__(self, coordinator: OrviboMeshCoordinator, device: dict):
        super().__init__(coordinator, device)
        self._attr_unique_id = f"orvibohomebridge_door_lock_lithium_battery_{self._device_id}"
        self._attr_name = "锂电池电量"
        self._attr_device_class = SensorDeviceClass.BATTERY
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = "%"

    @property
    def native_value(self) -> Optional[int]:
        state = self.coordinator.get_device_state(self._device_id)
        if state:
            bat = state.get("lithium_battery_level")
            if bat is not None:
                return int(bat)
        return None

    @property
    def extra_state_attributes(self):
        state = self.coordinator.get_device_state(self._device_id)
        if not state:
            return {}
        return {
            "is_setup": state.get("lithium_battery_setup"),
        }


class OrviboDoorLockStateSensor(OrviboSensorBase):
    """智能门锁 - 锁状态（上锁/未上锁/门内反锁/异常，绑定门磁）。"""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["locked", "unlocked", "inside_locked", "abnormal"]
    _attr_translation_key = "door_lock_state"

    def __init__(self, coordinator: OrviboMeshCoordinator, device: dict):
        super().__init__(coordinator, device)
        self._attr_unique_id = f"orvibohomebridge_door_lock_state_{self._device_id}"
        self._attr_name = "锁状态"
        self._attr_icon = "mdi:lock"

    @property
    def native_value(self) -> Optional[str]:
        state = self.coordinator.get_device_state(self._device_id)
        return state.get("lock_status") if state else None

    @property
    def extra_state_attributes(self) -> dict:
        state = self.coordinator.get_device_state(self._device_id) or {}
        return {
            "door_open": state.get("door_state"),
            "inside_locked": state.get("inside_lock_state"),
            "leave_home_armed": state.get("leave_home_armed"),
            "locked": state.get("locked"),
        }


class OrviboDoorLockUnlockSensor(OrviboSensorBase):
    """智能门锁 - 开锁事件（状态直接显示"xxx开门"）。"""

    def __init__(self, coordinator: OrviboMeshCoordinator, device: dict):
        super().__init__(coordinator, device)
        self._attr_unique_id = f"orvibohomebridge_door_lock_unlock_{self._device_id}"
        self._attr_name = "开锁事件"
        self._attr_icon = "mdi:key"

    @property
    def native_value(self) -> str:
        from .lock_status import format_unlock_label

        state = self.coordinator.get_device_state(self._device_id)
        if not state:
            return "无"
        user_id = state.get("unlock_user_id")
        name = self.coordinator.lock_user_name(self._device_id, user_id)
        return format_unlock_label(user_id, name)

    @property
    def extra_state_attributes(self) -> dict:
        state = self.coordinator.get_device_state(self._device_id) or {}
        attributes = {
            "unlock_type": state.get("unlock_type"),
            "unlock_user_id": state.get("unlock_user_id"),
        }
        user_name = self.coordinator.lock_user_name(
            self._device_id,
            state.get("unlock_user_id"),
        )
        if user_name:
            attributes["unlock_user_name"] = user_name
        if state.get("unlock_time") is not None:
            attributes["unlock_time"] = state.get("unlock_time")
        return attributes
