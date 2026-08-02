"""Orvibo 门锁截图 camera 平台。

每把门锁一个 camera 实体，显示最近一次事件截图（门铃/撬锁/离家告警等）。
事件到达时 coordinator 后台下载图片并推送到实体，前端门锁卡片直接显示。

实时视频流暂不支持：门锁猫眼走 SEP2P 私有 P2P 协议（闭源 SDK），本实体
仅提供事件快照，camera 属性不声明 stream 能力。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import OrviboMeshCoordinator
from .device_types import DeviceCategory, classify_device
from .selection import selected_device_ids

_LOGGER = logging.getLogger(__name__)

try:
    _PLACEHOLDER_IMAGE = (
        Path(__file__).with_name("snapshot_placeholder.png").read_bytes()
    )
except OSError:
    _PLACEHOLDER_IMAGE = None


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
        if classify_device(device) == DeviceCategory.DOOR_LOCK:
            entities.append(OrviboLockSnapshotCamera(coordinator, device))
    if entities:
        async_add_entities(entities)


class OrviboLockSnapshotCamera(CoordinatorEntity, Camera):
    """门锁事件截图摄像头（静态快照，无实时流）。"""

    _attr_icon = "mdi:cctv"

    def __init__(self, coordinator: OrviboMeshCoordinator, device: dict[str, Any]):
        super().__init__(coordinator)
        Camera.__init__(self)
        self._device = device
        self._device_id = device.get("device_id", "")
        self._attr_unique_id = f"orvibohomebridge_camera_{self._device_id}"
        self._attr_name = "门锁截图"
        self._attr_device_class = None
        self._image: bytes | None = None
        coordinator.register_lock_camera(self._device_id, self)

    def camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """返回最近一次事件截图；无截图时显示默认占位图。"""
        return self._image if self._image is not None else _PLACEHOLDER_IMAGE

    async def async_set_image(self, image: bytes | None) -> None:
        """事件到达时由 coordinator 推送最新截图。"""
        self._image = image
        self.async_write_ha_state()

    @property
    def device_info(self) -> dict[str, Any]:
        """关联到门锁设备（与其他平台实体一致）。"""
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._device.get("device_name", "Orvibo Door Lock"),
            "manufacturer": "ORVIBO",
            "model": "Smart Lock",
            "sw_version": "1.0",
        }
