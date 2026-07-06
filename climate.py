"""Orvibo 气候平台。

支持设备类别：
- FAN_COIL_AC (deviceType=36) 风机盘管空调面板
  value1=0为开/1为关; value2模式(2除湿/3制冷/4制热/7送风);
  value3风速(1低/2中/3高); value4温度*10000000
"""
import logging
from typing import Optional

from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature, HVACMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, DEVICE_TYPE_CLIMATE
from .coordinator import OrviboMeshCoordinator

_LOGGER = logging.getLogger(__name__)


# Orvibo AC mode value → HA HVACMode
_AC_MODE_TO_HVAC = {
    2: HVACMode.DRY,
    3: HVACMode.COOL,
    4: HVACMode.HEAT,
    7: HVACMode.FAN_ONLY,
}
_HVAC_TO_AC_MODE = {v: k for k, v in _AC_MODE_TO_HVAC.items()}

# Orvibo AC fan speed value → HA fan mode string
_FAN_SPEED_TO_HA = {
    1: "low",
    2: "medium",
    3: "high",
}
_HA_TO_FAN_SPEED = {v: k for k, v in _FAN_SPEED_TO_HA.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: OrviboMeshCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    entities = []
    for device_id, device in coordinator.devices.items():
        if device.get("device_type") == DEVICE_TYPE_CLIMATE:
            entities.append(OrviboFanCoilAC(coordinator, device))

    async_add_entities(entities)


class OrviboFanCoilAC(CoordinatorEntity, ClimateEntity):
    """风机盘管空调面板（deviceType=36）。"""
    _attr_has_entity_name = True
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_min_temp = 16.0
    _attr_max_temp = 32.0
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.COOL, HVACMode.HEAT, HVACMode.DRY, HVACMode.FAN_ONLY]
    _attr_fan_modes = ["low", "medium", "high"]
    _attr_supported_features = (
        ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
        | ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
    )

    def __init__(self, coordinator: OrviboMeshCoordinator, device: dict):
        super().__init__(coordinator)
        self._device = device
        self._device_id = device["device_id"]
        self._attr_unique_id = f"orvibohomebridge_climate_{self._device_id}"
        self._attr_name = device.get("device_name", self._device_id)

    @property
    def available(self) -> bool:
        state = self.coordinator.get_device_state(self._device_id)
        return state.get("online", False) if state else False

    @property
    def current_temperature(self) -> Optional[float]:
        state = self.coordinator.get_device_state(self._device_id)
        return state.get("temperature") if state else None

    @property
    def target_temperature(self) -> Optional[float]:
        # 风机盘管无独立目标温度字段，使用当前温度作为目标温度
        state = self.coordinator.get_device_state(self._device_id)
        return state.get("temperature") if state else None

    @property
    def hvac_mode(self) -> HVACMode:
        state = self.coordinator.get_device_state(self._device_id)
        if not state or not state.get("state", False):
            return HVACMode.OFF
        ac_mode_raw = state.get("ac_mode_raw")
        return _AC_MODE_TO_HVAC.get(ac_mode_raw, HVACMode.FAN_ONLY)

    @property
    def fan_mode(self) -> Optional[str]:
        state = self.coordinator.get_device_state(self._device_id)
        if not state:
            return None
        fan_speed_raw = state.get("fan_speed_raw")
        return _FAN_SPEED_TO_HA.get(fan_speed_raw)

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._device.get("device_name", self._device_id),
            "manufacturer": MANUFACTURER,
            "model": self._device.get("model", "Orvibo AC"),
            "sw_version": "1.0",
        }

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            await self.coordinator.async_turn_off(self._device_id)
            return
        # 先开机
        state = self.coordinator.get_device_state(self._device_id)
        if not state or not state.get("state", False):
            await self.coordinator.async_turn_on(self._device_id)
        ac_mode = _HVAC_TO_AC_MODE.get(hvac_mode)
        if ac_mode is not None:
            await self.coordinator.async_set_ac_mode(self._device_id, _ac_mode_name(hvac_mode))

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        await self.coordinator.async_set_ac_fan_speed(self._device_id, fan_mode)

    async def async_set_temperature(self, **kwargs) -> None:
        temperature = kwargs.get("temperature")
        if temperature is not None:
            await self.coordinator.async_set_ac_temperature(self._device_id, temperature)

    async def async_turn_on(self) -> None:
        await self.coordinator.async_turn_on(self._device_id)

    async def async_turn_off(self) -> None:
        await self.coordinator.async_turn_off(self._device_id)


def _ac_mode_name(hvac_mode: HVACMode) -> str:
    """HA HVACMode → coordinator 接受的字符串模式名。"""
    return {
        HVACMode.DRY: "dehumidify",
        HVACMode.COOL: "cool",
        HVACMode.HEAT: "heat",
        HVACMode.FAN_ONLY: "fan_only",
    }.get(hvac_mode, "fan_only")
