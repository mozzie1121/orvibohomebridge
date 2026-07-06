import logging
import asyncio
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DOMAIN, CONF_FAMILY_ID
from .coordinator import OrviboMeshCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["switch", "light", "cover", "sensor", "binary_sensor", "climate"]

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

    _LOGGER.info("Orvibo Mesh 设置完成")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    _LOGGER.info("开始卸载 Orvibo Mesh...")
    
    coordinator = hass.data[DOMAIN].get(entry.entry_id)
    if coordinator:
        await coordinator.async_cleanup()
        _LOGGER.info("Coordinator 清理完成")

    unload_ok = await hass.config_entries.async_forward_entry_unload(entry, PLATFORMS)
    _LOGGER.info(f"卸载结果: {unload_ok}")

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        _LOGGER.info("已从 hass.data 移除")

    return unload_ok