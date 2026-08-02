import logging
import asyncio
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN, CONF_FAMILY_ID, CONF_LOCK_USER_NAMES
from .coordinator import OrviboMeshCoordinator
from .selection import CONF_DEVICE_AREAS, selected_device_ids

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ("switch", "light", "cover", "sensor", "binary_sensor", "climate", "fan", "camera")

SERVICE_REFRESH = "refresh_devices"
SERVICE_SET_LOCK_USER_NAME = "set_lock_user_name"
SERVICE_FETCH_VIDEO = "fetch_video"
SERVICE_LIST_EVENTS = "list_events"
SERVICE_CLEANUP_HISTORY = "cleanup_history"

# 本集成仅通过配置项使用，不读取 configuration.yaml。
# 新版 HA 要求 empty_config_schema 传入 domain 参数。
CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: dict):
    """设置服务"""
    async def handle_refresh(call: ServiceCall):
        """处理手动刷新设备请求"""
        entry_id = call.data.get("entry_id")
        if not entry_id:
            _LOGGER.error("未提供 entry_id")
            return

        coordinator = hass.data.get(DOMAIN, {}).get(entry_id)
        if not coordinator:
            _LOGGER.error(f"找不到 coordinator: {entry_id}")
            return

        _LOGGER.info("手动刷新设备...")
        await coordinator.async_request_refresh()
        _LOGGER.info("设备刷新完成")

    hass.services.async_register(DOMAIN, SERVICE_REFRESH, handle_refresh)

    async def handle_set_lock_user_name(call: ServiceCall):
        """为门锁 userId 设置显示名称，用于区分"谁开的门"。"""
        entry_id = call.data.get("entry_id")
        device_id = call.data.get("device_id", "")
        user_id = call.data.get("user_id", "")
        name = call.data.get("name", "")
        if not device_id or user_id in (None, ""):
            _LOGGER.error("set_lock_user_name 需要 device_id 和 user_id")
            return
        targets = []
        if entry_id:
            coordinator = hass.data.get(DOMAIN, {}).get(entry_id)
            if coordinator:
                targets.append(coordinator)
        else:
            targets.extend(coordinator for coordinator in hass.data.get(DOMAIN, {}).values())
        updated = False
        for coordinator in targets:
            if coordinator.set_lock_user_name(str(device_id), str(user_id), str(name)):
                updated = True
        if updated:
            _LOGGER.info(
                "门锁用户名称已设置: device=%s user=%s name=%s",
                str(device_id)[-12:],
                user_id,
                name,
            )
        else:
            _LOGGER.warning("未找到可应用 set_lock_user_name 的配置项")

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_LOCK_USER_NAME,
        handle_set_lock_user_name,
    )

    async def handle_fetch_video(call: ServiceCall):
        """拉取门锁事件录像（.h264 → MP4），返回本地路径与媒体 ID。"""
        entry_id = call.data.get("entry_id")
        device_id = str(call.data.get("device_id", ""))
        object_key = str(call.data.get("object_key", ""))
        if not device_id or not object_key:
            _LOGGER.error("fetch_video 需要 device_id 和 object_key（事件里的 video_url）")
            return {"error": "需要 device_id 和 object_key"}
        targets = []
        if entry_id:
            coordinator = hass.data.get(DOMAIN, {}).get(entry_id)
            if coordinator:
                targets.append(coordinator)
        else:
            targets.extend(
                coordinator for coordinator in hass.data.get(DOMAIN, {}).values()
            )
        for coordinator in targets:
            result = await coordinator.async_fetch_video(
                device_id, object_key
            )
            if result and "error" not in result:
                _LOGGER.info(
                    "录像已拉取 device=%s -> %s",
                    str(device_id)[-12:],
                    result.get("video_file"),
                )
                return result
        return {"error": "未找到可用的配置项或拉取失败"}

    hass.services.async_register(
        DOMAIN,
        SERVICE_FETCH_VIDEO,
        handle_fetch_video,
    )

    async def handle_list_events(call: ServiceCall):
        """查询门锁事件历史（截图/录像），按时间倒序返回。"""
        from pathlib import Path

        entry_id = call.data.get("entry_id")
        device_id = str(call.data.get("device_id", ""))
        limit = int(call.data.get("limit", 100))
        targets = []
        if entry_id:
            coordinator = hass.data.get(DOMAIN, {}).get(entry_id)
            if coordinator:
                targets.append(coordinator)
        else:
            targets.extend(
                coordinator for coordinator in hass.data.get(DOMAIN, {}).values()
            )
        result = []
        for coordinator in targets:
            result.extend(await coordinator.async_list_events(device_id, limit))
            if len(result) >= limit:
                result = result[:limit]
                break
        media_root = Path(hass.config.path("media"))
        history_root = media_root / "orvibohomebridge"

        def _scan_device_dirs() -> list[str]:
            if not history_root.is_dir():
                return []
            return sorted(d.name for d in history_root.iterdir() if d.is_dir())

        device_dirs = await hass.async_add_executor_job(_scan_device_dirs)
        return {
            "events": result,
            "media_root": str(media_root),
            "history_root": str(history_root),
            "device_dirs": device_dirs,
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_LIST_EVENTS,
        handle_list_events,
    )

    async def handle_cleanup_history(call: ServiceCall):
        """手动清理门锁历史记录（截图/录像）。"""
        entry_id = call.data.get("entry_id")
        keep_days = int(call.data.get("keep_days", 7))
        device_id = str(call.data.get("device_id", ""))
        max_entries = call.data.get("max_entries")
        targets = []
        if entry_id:
            coordinator = hass.data.get(DOMAIN, {}).get(entry_id)
            if coordinator:
                targets.append(coordinator)
        else:
            targets.extend(
                coordinator for coordinator in hass.data.get(DOMAIN, {}).values()
            )
        total = 0
        for coordinator in targets:
            total += await coordinator.async_cleanup_history(
                keep_days=keep_days,
                device_id=device_id,
                max_entries=int(max_entries) if max_entries else None,
            )
        _LOGGER.info("历史清理完成，共删除 %s 个文件", total)
        return {"removed": total}

    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEANUP_HISTORY,
        handle_cleanup_history,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]
    family_id = entry.data.get(CONF_FAMILY_ID)

    coordinator = OrviboMeshCoordinator(
        hass,
        username,
        password,
        family_id,
        lock_user_names=entry.options.get(CONF_LOCK_USER_NAMES),
    )

    try:
        _LOGGER.info("开始设置 Orvibo Mesh...")
        await coordinator._async_setup()
        _LOGGER.info("Coordinator 设置完成")
    except ConfigEntryAuthFailed:
        raise
    except Exception as e:
        _LOGGER.error(f"Coordinator 设置失败: {e}", exc_info=True)
        raise ConfigEntryNotReady from e

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator
    _LOGGER.info("Coordinator 已注册到 hass.data")

    # 历史记录自动清理：保留 7 天，每周执行；卸载时取消定时任务
    coordinator.start_history_cleanup()
    entry.async_on_unload(coordinator.stop_history_cleanup)

    # 使用 async_forward_entry_setups 一次性加载所有平台
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _apply_after_refresh():
        """等待 coordinator 第一次刷新完成后再应用区域映射。"""
        if not coordinator.last_update_success:
            await coordinator.async_refresh()
        await _async_assign_areas(hass, entry)
        await _apply_device_areas(hass, entry)
    
    hass.async_create_task(_apply_after_refresh())

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    _LOGGER.info("Orvibo Mesh 设置完成")
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry):
    """当配置条目更新时重新应用区域映射。"""
    _LOGGER.info("配置条目已更新，重新应用设备区域映射")
    await _async_assign_areas(hass, entry)
    await _apply_device_areas(hass, entry)
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_assign_areas(hass: HomeAssistant, entry: ConfigEntry):
    """将云端房间自动映射为 Home Assistant 区域（orvibo-lan 同款）。"""
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not coordinator:
        return
    area_registry = ar.async_get(hass)
    device_registry = dr.async_get(hass)
    selected = selected_device_ids(entry.options, coordinator.devices)
    for device_id, device in coordinator.devices.items():
        if device_id not in selected:
            continue
        room_name = device.get("room_name")
        if not room_name:
            continue
        area = area_registry.async_get_area_by_name(room_name)
        if area is None:
            area = area_registry.async_create(room_name)
        device_entry = device_registry.async_get_device(
            identifiers={(DOMAIN, device_id)}
        )
        if device_entry is not None and device_entry.area_id != area.id:
            device_registry.async_update_device(device_entry.id, area_id=area.id)


async def _apply_device_areas(hass: HomeAssistant, entry: ConfigEntry):
    """将配置的区域映射应用到 HA 设备注册表。"""
    device_areas = entry.options.get(CONF_DEVICE_AREAS, {})
    if not device_areas:
        _LOGGER.debug("未配置设备区域映射")
        return

    _LOGGER.debug(f"应用设备区域映射: {device_areas}")
    
    device_registry = dr.async_get(hass)
    
    for device_id, area_id in device_areas.items():
        if not area_id:
            continue
        
        device = device_registry.async_get_device(identifiers={(DOMAIN, device_id)})
        if device:
            _LOGGER.info(f"设置设备 {device_id} 的区域为 {area_id}")
            device_registry.async_update_device(
                device.id,
                area_id=area_id,
            )
        else:
            _LOGGER.warning(f"未找到设备: {device_id}")


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    _LOGGER.info("开始卸载 Orvibo Mesh...")
    
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator:
        await coordinator.async_cleanup()
        _LOGGER.info("Coordinator 清理完成")

    unload_ok = True
    for platform in PLATFORMS:
        try:
            result = await hass.config_entries.async_forward_entry_unload(entry, platform)
            if not result:
                unload_ok = False
        except Exception as e:
            _LOGGER.error(f"卸载平台 {platform} 失败: {e}")
            unload_ok = False
    _LOGGER.info(f"卸载结果: {unload_ok}")

    if unload_ok:
        hass_data = hass.data.get(DOMAIN, {})
        hass_data.pop(entry.entry_id, None)
        _LOGGER.info("已从 hass.data 移除")

    return unload_ok
