import logging
from typing import Optional

from homeassistant.components.cover import CoverEntity, CoverDeviceClass, CoverEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, DEVICE_TYPE_COVER, DEVICE_TYPE_CLOTHES_HORSE
from .coordinator import OrviboMeshCoordinator
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
        if device.get("device_type") == DEVICE_TYPE_COVER:
            entities.append(OrviboCover(coordinator, device))
        elif device.get("device_type") == DEVICE_TYPE_CLOTHES_HORSE:
            entities.append(OrviboClothesHorseMotor(coordinator, device))

    async_add_entities(entities)


class OrviboCover(CoordinatorEntity, CoverEntity):
    _attr_has_entity_name = True
    _attr_device_class = CoverDeviceClass.CURTAIN
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
    )

    def __init__(self, coordinator: OrviboMeshCoordinator, device: dict):
        super().__init__(coordinator)
        self._device = device
        self._device_id = device["device_id"]
        self._attr_unique_id = f"orvibohomebridge_cover_{self._device_id}"
        self._attr_name = device.get("device_name", self._device_id)

    @property
    def current_cover_position(self) -> int:
        state = self.coordinator.get_device_state(self._device_id)
        return state.get("position", 0) if state else 0

    @property
    def is_closed(self) -> bool:
        position = self.current_cover_position
        return position == 0

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
            "model": self._device.get("model", "Orvibo Curtain"),
            "sw_version": "1.0",
        }

    async def async_open_cover(self, **kwargs) -> None:
        await self.coordinator.async_set_cover_position(self._device_id, 100)

    async def async_close_cover(self, **kwargs) -> None:
        await self.coordinator.async_set_cover_position(self._device_id, 0)

    async def async_stop_cover(self, **kwargs) -> None:
        await self.coordinator.async_stop_cover(self._device_id)

    async def async_set_cover_position(self, **kwargs) -> None:
        position = kwargs.get("position", 0)
        await self.coordinator.async_set_cover_position(self._device_id, position)


class OrviboClothesHorseMotor(CoordinatorEntity, CoverEntity):
    """晾衣架电机 Cover 实体（升降）。"""
    _attr_has_entity_name = True
    _attr_device_class = CoverDeviceClass.AWNING
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
    )

    def __init__(self, coordinator: OrviboMeshCoordinator, device: dict):
        super().__init__(coordinator)
        self._device = device
        self._device_id = device["device_id"]
        self._attr_unique_id = f"orvibohomebridge_ch_motor_{self._device_id}"
        self._attr_name = "晾杆"
        self._attr_icon = "mdi:hanger"

    @property
    def current_cover_position(self) -> int:
        state = self.coordinator.get_device_state(self._device_id)
        # 晾衣架 position=0 顶部（收起），HA cover position=0 关闭
        # 所以直接映射即可：顶部=0=关闭，底部=100=打开
        return state.get("position", 0) if state else 0

    @property
    def is_closed(self) -> bool:
        position = self.current_cover_position
        return position == 0

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
            "model": self._device.get("model", "Orvibo Clothes Horse"),
            "sw_version": "1.0",
        }

    async def async_open_cover(self, **kwargs) -> None:
        """打开晾衣杆（下降到底部）。"""
        await self.coordinator.async_clothes_horse_control(self._device_id, "motor", "down")

    async def async_close_cover(self, **kwargs) -> None:
        """关闭晾衣杆（上升到顶部）。"""
        await self.coordinator.async_clothes_horse_control(self._device_id, "motor", "up")

    async def async_stop_cover(self, **kwargs) -> None:
        """停止电机。"""
        await self.coordinator.async_clothes_horse_control(self._device_id, "motor", "stop")

    async def async_set_cover_position(self, **kwargs) -> None:
        """设置位置。晾衣架不支持精确定位，转换为升降操作。"""
        position = kwargs.get("position", 0)
        current = self.current_cover_position
        if position == current:
            return
        if position > current:
            # HA position 变大 = 更打开 = 晾衣架下降
            await self.coordinator.async_clothes_horse_control(self._device_id, "motor", "down")
        else:
            # HA position 变小 = 更关闭 = 晾衣架上升
            await self.coordinator.async_clothes_horse_control(self._device_id, "motor", "up")