import logging
import asyncio
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN, CONF_FAMILY_ID
from .coordinator import OrviboMeshCoordinator
from .selection import CONF_DEVICE_AREAS

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ("switch", "light", "cover", "sensor", "binary_sensor", "climate", "fan")

SERVICE_REFRESH = "refresh_devices"


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
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]
    family_id = entry.data.get(CONF_FAMILY_ID)

    coordinator = OrviboMeshCoordinator(hass, username, password, family_id)

    try:
        _LOGGER.info("开始设置 Orvibo Mesh...")
        await coordinator._async_setup()
        _LOGGER.info("Coordinator 设置完成")
    except Exception as e:
        _LOGGER.error(f"Coordinator 设置失败: {e}", exc_info=True)
        raise ConfigEntryNotReady from e

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator
    _LOGGER.info("Coordinator 已注册到 hass.data")

    # 使用 async_forward_entry_setups 一次性加载所有平台
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _apply_after_refresh():
        """等待 coordinator 第一次刷新完成后再应用区域映射。"""
        if not coordinator.last_update_success:
            await coordinator.async_refresh()
        await _apply_device_areas(hass, entry)
    
    hass.async_create_task(_apply_after_refresh())

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    _LOGGER.info("Orvibo Mesh 设置完成")
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry):
    """当配置条目更新时重新应用区域映射。"""
    _LOGGER.info("配置条目已更新，重新应用设备区域映射")
    await _apply_device_areas(hass, entry)


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
        result = await hass.config_entries.async_forward_entry_unload(entry, platform)
        if not result:
            _LOGGER.warning(f"卸载平台 {platform} 失败")
            unload_ok = False
    _LOGGER.info(f"卸载结果: {unload_ok}")

    if unload_ok:
        hass_data = hass.data.get(DOMAIN, {})
        hass_data.pop(entry.entry_id, None)
        _LOGGER.info("已从 hass.data 移除")

    return unload_ok