"""Orvibo 门锁截图 camera 平台。

每把门锁一个 camera 实体，显示最近一次事件截图（门铃/撬锁/离家告警等）。
事件到达时 coordinator 后台下载图片并推送到实体，前端门锁卡片直接显示。

Meari XTS 摄像机和录像机通过私有视频桥接输出 H.264，供 HA Stream 播放。
门锁仍只提供事件快照；旧摄像机的 SEP2P 分支不能证明门锁猫眼协议。
"""

from __future__ import annotations

import logging
import asyncio
from pathlib import Path
from typing import Any

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.exceptions import HomeAssistantError, PlatformNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .camera_metadata import camera_sdk_hint
from .meari_live import LiveCameraBootstrap, LiveCameraDescription
from .meari_stream import MeariVideoBridge, _finish_uninterrupted
from .meari_hdd import DATA_KEY, async_query_hdd, validate_request
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
    camera_devices = {
        device_id: device for device_id, device in coordinator.devices.items()
        if device_id in selected_ids
        and camera_sdk_hint(device) != "unknown"
    }
    unselected_cameras = [
        device.get("device_name") or device_id
        for device_id, device in coordinator.devices.items()
        if device_id not in selected_ids and camera_sdk_hint(device) != "unknown"
    ]
    if unselected_cameras:
        _LOGGER.info(
            "以下摄像头未在已选设备中，如需实体请在集成选项里重新勾选「摄像头」分组: %s",
            ", ".join(unselected_cameras),
        )

    video_devices = {
        device_id: device
        for device_id, device in camera_devices.items()
        if camera_sdk_hint(device) == "meari"
    }
    # 非 meari（danale/ezviz 等）暂无可用的建流实现：先给出占位实体，
    # 让设备在 HA 里可见，而不是静默消失。
    for device_id, device in camera_devices.items():
        if camera_sdk_hint(device) != "meari":
            entities.append(
                OrviboCameraUnavailable(
                    coordinator, device, f"sdk={camera_sdk_hint(device)} 暂未实现实时流"
                )
            )

    if video_devices:
        bootstrap = LiveCameraBootstrap(coordinator, async_get_clientsession(hass))
        try:
            descriptions = await bootstrap.discover(video_devices)
        except Exception as err:  # noqa: BLE001 - 任何建流失败都不应吞掉整平台
            _LOGGER.warning(
                "Meari 摄像机通道清单获取失败（%s: %s），先创建占位实体，稍后重试",
                type(err).__name__,
                err,
            )
            for device in video_devices.values():
                entities.append(
                    OrviboCameraUnavailable(
                        coordinator, device, f"P2P 通道不可用: {type(err).__name__}"
                    )
                )
        else:
            binary = get_ffmpeg_manager(hass).binary
            for description in descriptions:
                live = OrviboLiveCamera(
                    coordinator, video_devices[description.device_id], description,
                    bootstrap, binary,
                )
                entities.append(live)
                playback = OrviboHddCamera(
                    coordinator, video_devices[description.device_id], description,
                    bootstrap, binary,
                )
                live._playback = playback
                entities.append(playback)
    if entities:
        async_add_entities(entities)


class OrviboCameraUnavailable(CoordinatorEntity, Camera):
    """SDK 未实现或 P2P 建流失败时的占位摄像头。

    目的：设备在 HA 里可见可查（含 sdk_hint / 失败原因），而不是整平台静默失败。
    """

    _attr_icon = "mdi:cctv-off"

    def __init__(
        self,
        coordinator: OrviboMeshCoordinator,
        device: dict[str, Any],
        reason: str,
    ) -> None:
        super().__init__(coordinator)
        Camera.__init__(self)
        self._device = device
        self._device_id = device.get("device_id", "")
        self._reason = reason
        self._attr_unique_id = f"orvibohomebridge_camera_unavailable_{self._device_id}"
        self._attr_name = str(device.get("device_name") or "摄像机") + " 摄像头"

    @property
    def available(self) -> bool:
        return False

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        return _PLACEHOLDER_IMAGE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "reason": self._reason,
            "sdk_hint": camera_sdk_hint(self._device),
            "device_type_raw": self._device.get("device_type_raw"),
        }

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._device.get("device_name", "Orvibo Camera"),
            "manufacturer": "ORVIBO",
            "model": "Camera",
            "sw_version": "1.0",
        }


class OrviboLockSnapshotCamera(CoordinatorEntity, Camera):
    """门锁事件截图摄像头（静态快照，无实时流）。"""

    _attr_icon = "mdi:cctv"

    def __init__(self, coordinator: OrviboMeshCoordinator, device: dict[str, Any]):
        super().__init__(coordinator)
        Camera.__init__(self)
        self._device = device
        self._device_id = device.get("device_id", "")
        self._attr_unique_id = f"orvibo_smart_control_camera_{self._device_id}"
        self._attr_name = "门锁截图"
        self._attr_device_class = None
        self._image: bytes | None = None
        self._snapshot_revision = 0
        coordinator.register_lock_camera(self._device_id, self)

    def camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """返回最近一次事件截图；无截图时显示默认占位图。"""
        return self._image if self._image is not None else _PLACEHOLDER_IMAGE

    async def async_set_image(self, image: bytes | None) -> None:
        """事件到达时由 coordinator 推送最新截图。"""
        self._image = image
        self._snapshot_revision += 1
        if getattr(self, "hass", None) is not None:
            self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        manager = getattr(self.coordinator, "lock_media", None)
        if manager is not None:
            manager.unregister_camera(self._device_id, self)
        await super().async_will_remove_from_hass()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # Camera state remains "idle" for stills. Changing an attribute also
        # refreshes last_updated, so dashboards reload newly persisted history.
        return {"snapshot_revision": self._snapshot_revision}

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


class OrviboLiveCamera(CoordinatorEntity, Camera):
    """One selected Meari IPC or online NVR channel, using HA's HLS player."""

    _attr_icon = "mdi:cctv"
    _attr_supported_features = CameraEntityFeature.STREAM
    _attr_should_poll = False
    _attr_brand = "ORVIBO"

    def __init__(
        self, coordinator: OrviboMeshCoordinator, device: dict[str, Any],
        description: LiveCameraDescription, bootstrap: LiveCameraBootstrap,
        ffmpeg_binary: str,
    ) -> None:
        super().__init__(coordinator)
        Camera.__init__(self)
        self._device = device
        self._description = description
        self._attr_unique_id = (
            f"orvibo_smart_control_live_{description.device_id}_{description.channel_id}"
        )
        self._attr_name = (
            f"{device.get('device_name') or '录像机'} 通道 {description.channel_id}"
            if description.is_nvr else f"{device.get('device_name') or '摄像机'} 实时画面"
        )
        self._bridge = MeariVideoBridge(
            ffmpeg_binary, lambda: bootstrap.context(description),
        )
        self._closed = False
        self._playback = None

    @property
    def use_stream_for_stills(self) -> bool:
        """Reuse HA's decoded keyframe instead of opening another P2P session."""
        return True

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None,
    ) -> bytes | None:
        stream = await self.async_create_stream()
        if stream is None:
            return None
        return await stream.async_get_image(width=width, height=height)

    async def async_create_stream(self):
        if self._closed:
            return None
        try:
            # HA limits stream_source to 10s and PyAV source open to 30s.
            # Establish slow P2P signaling before either of those clocks starts.
            await self._bridge.async_prepare()
        except (ValueError, OSError, RuntimeError, TimeoutError):
            raise HomeAssistantError("摄像机暂时无法建立视频连接，请稍后重试") from None
        return await super().async_create_stream()

    async def stream_source(self) -> str | None:
        if self._closed:
            return None
        return await self._bridge.async_start()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, self._async_stop,
        ))

    async def _async_stop(self, _event: Any = None) -> None:
        self._closed = True

        async def cleanup() -> None:
            try:
                # Interrupt the localhost reader before joining HA's stream thread.
                await self._bridge.async_close()
            finally:
                if self.stream is not None:
                    settings = getattr(self.stream, "dynamic_stream_settings", None)
                    if settings is not None:
                        settings.preload_stream = False
                    elif hasattr(self.stream, "keepalive"):
                        self.stream.keepalive = False
                    await self.stream.stop()

        await _finish_uninterrupted(cleanup())

    async def async_will_remove_from_hass(self) -> None:
        await self._async_stop()
        await super().async_will_remove_from_hass()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "channel_id": self._description.channel_id,
            "video_transport": "meari_xts",
            "video_stage": self._bridge.last_stage,
            "output_codec": "h264",
            "audio_supported": False,
            "video_mode": "live",
            "hdd_playback_supported": self._description.is_nvr,
            "hdd_playback_entity_id": (
                getattr(self._playback, "entity_id", None) if self._description.is_nvr else None
            ),
            "playback_supported": True,
            "playback_entity_id": getattr(self._playback, "entity_id", None),
            "recording_storage": "hdd" if self._description.is_nvr else "sd_card",
            "video_diagnostics": self._bridge.diagnostics,
        }

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._description.device_id)},
            "name": self._device.get("device_name") or "ORVIBO Camera",
            "manufacturer": "ORVIBO",
            "model": "NVR" if self._description.is_nvr else "Camera",
        }


class OrviboHddCamera(OrviboLiveCamera):
    """Demand driven HDD/SD player; choosing a timestamp creates a fresh session."""

    _attr_icon = "mdi:filmstrip"

    def __init__(self, coordinator, device, description, bootstrap, ffmpeg_binary):
        super().__init__(coordinator, device, description, bootstrap, ffmpeg_binary)
        storage = "hdd" if description.is_nvr else "sd"
        self._attr_unique_id = f"orvibo_smart_control_{storage}_{description.device_id}_{description.channel_id}"
        self._attr_name = (
            f"{device.get('device_name') or '录像机'} 通道 {description.channel_id} 硬盘回放"
            if description.is_nvr else f"{device.get('device_name') or '摄像机'} 存储卡回放"
        )
        self._bootstrap = bootstrap
        self._ffmpeg_binary = ffmpeg_binary
        self._position = None
        self._selection_lock = asyncio.Lock()
        self._query_task = None
        self._hdd_stage = "select_recording"
        self._bridge = self._new_bridge()

    def _new_bridge(self):
        async def context():
            if self._position is None:
                raise ValueError("hdd_selection_required")
            position = self._position
            result = await self._bootstrap.context(self._description)
            result["hdd"] = {"operation": "playback", "date": position}
            return result
        return MeariVideoBridge(self._ffmpeg_binary, context, source_mode="playback")

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.hass.data.setdefault(DATA_KEY, {})[self.entity_id] = self

    async def _release_player(self):
        try:
            await self._bridge.async_close()
        finally:
            if self.stream is not None:
                settings = getattr(self.stream, "dynamic_stream_settings", None)
                if settings is not None:
                    settings.preload_stream = False
                elif hasattr(self.stream, "keepalive"):
                    self.stream.keepalive = False
                await self.stream.stop()
                self.stream = None

    async def async_select_recording(self, date):
        request = validate_request({"operation": "playback", "date": date})
        async with self._selection_lock:
            if self._closed:
                raise ValueError("hdd_entity_unavailable")
            # Finish the entire transition if the service caller disconnects.
            async def select():
                await self._release_player()
                self._position = request["date"]
                self._bridge = self._new_bridge()
                self._hdd_stage = "playback_selected"
                self.async_write_ha_state()
            await _finish_uninterrupted(select())

    async def async_stop_recording(self):
        async with self._selection_lock:
            async def stop():
                self._position = None
                await self._release_player()
                self._hdd_stage = "select_recording"
                self.async_write_ha_state()
            await _finish_uninterrupted(stop())

    async def async_search_recordings(self, operation, date):
        request = validate_request({"operation": operation, "date": date})
        if operation not in ("month", "day"):
            raise ValueError("hdd_query_required")
        if self._closed:
            raise ValueError("hdd_entity_unavailable")
        if self._query_task is not None:
            return {"stage": "hdd_query_in_progress"}
        task = asyncio.current_task()
        self._query_task = task
        try:
            async with asyncio.timeout(110):
                context = await self._bootstrap.context(self._description)
                context["hdd"] = request
                return await async_query_hdd(context)
        except TimeoutError:
            return {"stage": "hdd_query_timeout"}
        finally:
            if self._query_task is task:
                self._query_task = None

    async def async_create_stream(self):
        if self._closed or self._position is None:
            return None
        bridge = self._bridge
        try:
            await bridge.async_prepare()
        except (ValueError, OSError, RuntimeError, TimeoutError):
            raise HomeAssistantError("录像回放暂时无法连接，请重新选择录像片段") from None
        async with self._selection_lock:
            if self._closed or self._position is None or self._bridge is not bridge:
                return None
            return await Camera.async_create_stream(self)

    async def stream_source(self):
        if self._closed or self._position is None:
            return None
        return await self._bridge.async_start()

    async def async_camera_image(self, width=None, height=None):
        # Dashboard thumbnails must not consume a selected playback session.
        return _PLACEHOLDER_IMAGE

    async def _async_stop(self, _event=None):
        self._closed = True
        registry = self.hass.data.get(DATA_KEY, {}) if getattr(self, "hass", None) else {}
        if registry.get(getattr(self, "entity_id", None)) is self:
            registry.pop(self.entity_id, None)
        async def stop():
            if self._query_task is not None and self._query_task is not asyncio.current_task():
                task = self._query_task
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            async with self._selection_lock:
                self._position = None
                await self._release_player()
        await _finish_uninterrupted(stop())

    @property
    def extra_state_attributes(self):
        return {
            "channel_id": self._description.channel_id,
            "video_mode": "hdd_playback" if self._description.is_nvr else "sd_playback",
            "video_transport": "meari_xts",
            "hdd_playback_supported": self._description.is_nvr,
            "playback_supported": True,
            "recording_storage": "hdd" if self._description.is_nvr else "sd_card",
            "audio_supported": False,
            "device_calendar_time": True, "recording_start": self._position,
            "video_stage": self._bridge.last_stage if self._position else self._hdd_stage,
            "video_diagnostics": self._bridge.diagnostics,
        }
