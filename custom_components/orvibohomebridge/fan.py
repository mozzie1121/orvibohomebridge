import logging
from typing import Optional

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, DEVICE_TYPE_FAN
from .coordinator import OrviboMeshCoordinator
from .device_types import classify_device, DeviceCategory

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
        if category == DeviceCategory.VENTILATION_SYSTEM:
            entities.append(OrviboVentilationFan(coordinator, device))

    async_add_entities(entities)
    _LOGGER.debug(f"添加了{len(entities)}个新风实体")


class OrviboVentilationFan(CoordinatorEntity, FanEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: OrviboMeshCoordinator, device: dict):
        super().__init__(coordinator)
        self._device = device
        self._device_id = device["device_id"]
        self._attr_unique_id = f"{DOMAIN}_fan_{self._device_id}"
        self._attr_name = device.get("device_name", self._device_id)
        self._attr_icon = "mdi:air-filter"

        self._attr_supported_features = (
            FanEntityFeature.PRESET_MODE |
            FanEntityFeature.TURN_ON |
            FanEntityFeature.TURN_OFF
        )
        self._attr_preset_modes = ["停", "慢", "快"]
        self._attr_oscillating = False
        self._attr_percentage = None
        self._attr_percentage_step = None
        self._attr_speed_list = None

        self._attr_device_info = {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._attr_name,
            "model": device.get("model", ""),
            "manufacturer": MANUFACTURER,
        }

    @property
    def available(self) -> bool:
        if not self.coordinator.device_states:
            return False
        device_state = self.coordinator.device_states.get(self._device_id, {})
        if not device_state:
            return False
        return device_state.get('online', True)

    @property
    def is_on(self) -> bool:
        if not self.coordinator.device_states:
            return False
        device_state = self.coordinator.device_states.get(self._device_id, {})
        return device_state.get("state", False)

    @property
    def preset_mode(self) -> str:
        if not self.coordinator.device_states:
            return "停"
        device_state = self.coordinator.device_states.get(self._device_id, {})
        return device_state.get("fan_speed", "停")

    @property
    def extra_state_attributes(self) -> dict:
        device_state = self.coordinator.device_states.get(self._device_id, {})
        attrs = {}
        if "temperature" in device_state:
            attrs["temperature"] = device_state["temperature"]
        return attrs

    async def async_turn_on(self, speed: Optional[str] = None, percentage: Optional[int] = None, preset_mode: Optional[str] = None, **kwargs) -> None:
        if preset_mode:
            await self.async_set_preset_mode(preset_mode)
        elif speed:
            await self.async_set_preset_mode(speed)
        else:
            await self.async_set_preset_mode("慢")

    async def async_turn_off(self, **kwargs) -> None:
        await self.async_set_preset_mode("停")

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        _LOGGER.debug(f"设置新风{self._device_id}预设模式为{preset_mode}")
        await self.coordinator.async_set_ventilation_preset_mode(self._device_id, preset_mode)

    async def async_toggle(self, **kwargs) -> None:
        if self.is_on:
            await self.async_turn_off()
        else:
            await self.async_turn_on()

    @property
    def should_poll(self) -> bool:
        return False
