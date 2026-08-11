import logging
import asyncio
from datetime import timedelta
try:
    from homeassistant.components.frontend import add_extra_js_url
except ImportError:  # 旧版 HA 兼容
    add_extra_js_url = None  # type: ignore[assignment]
try:
    from homeassistant.components.http import StaticPathConfig
except ImportError:  # 旧版 HA 兼容
    StaticPathConfig = None  # type: ignore[assignment]
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import CONF_USERNAME
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .const import (
    DOMAIN,
    CONF_FAMILY_ID,
    CONF_LOCK_USER_NAMES,
    CONF_TRANSPORT_MODE,
    CONF_PASSWORD_HASH,
    CONF_CLOUD_REGION,
    CONF_USE_INDEPENDENT_LAN_CREDENTIALS,
    CONF_LAN_USERNAME,
    CONF_LAN_PASSWORD_HASH,
    CONF_POLL_INTERVAL_MINUTES,
    DEFAULT_POLL_INTERVAL_MINUTES,
    MIN_POLL_INTERVAL_MINUTES,
    MAX_POLL_INTERVAL_MINUTES,
)
from .capabilities import TransportMode
from .protocol import migrate_password_credentials
from .models import AccountCredentials
from .cloud import cloud_for_region
from .coordinator import OrviboMeshCoordinator
from .selection import CONF_DEVICE_AREAS, selected_device_ids
from .service_handlers import async_register_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ("switch", "light", "cover", "sensor", "binary_sensor", "climate", "fan", "camera")

# 本集成仅通过配置项使用，不读取 configuration.yaml。
# 新版 HA 要求 empty_config_schema 传入 domain 参数。
CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Replace legacy plaintext passwords with replayable protocol digests."""
    if entry.version > 3:
        return False
    try:
        migrated_data = migrate_password_credentials(entry.data)
    except ValueError:
        _LOGGER.error("配置项缺少可迁移的 ORVIBO 登录凭据")
        return False
    if entry.version < 3 or migrated_data != dict(entry.data):
        hass.config_entries.async_update_entry(
            entry,
            data=migrated_data,
            version=3,
        )
    return True


async def async_setup(hass: HomeAssistant, config: dict):
    """设置服务"""
    # 注册内嵌门锁卡片资源（纯原生 JS，无需第三方插件）
    try:
        from pathlib import Path

        www_dir = Path(__file__).parent / "www"
        _LOGGER.info(
            "门锁卡片资源注册检查: www_dir=%s exists=%s",
            www_dir,
            www_dir.is_dir(),
        )
        if www_dir.is_dir():
            try:
                if StaticPathConfig is not None:
                    await hass.http.async_register_static_paths(
                        [StaticPathConfig(
                            url_path="/orvibohomebridge/www",
                            path=str(www_dir),
                            cache_headers=True,
                        )]
                    )
                else:
                    hass.http.register_static_path("/orvibohomebridge/www", str(www_dir))
            except (AttributeError, TypeError):
                hass.http.register_static_path("/orvibohomebridge/www", str(www_dir))
            try:
                _js_ver = str(int((www_dir / "orvibo-door-lock-card.js").stat().st_mtime))
            except OSError:
                _js_ver = "0"
            js_url = f"/orvibohomebridge/www/orvibo-door-lock-card.js?v={_js_ver}"
            try:
                if add_extra_js_url is not None:
                    add_extra_js_url(hass, js_url)
                else:
                    hass.components.frontend.add_extra_js_url(hass, js_url)
            except (AttributeError, TypeError):
                # 旧版 HA：hass.components 方式
                hass.components.frontend.add_extra_js_url(hass, js_url)
            _LOGGER.info("门锁卡片资源已注册")
    except Exception:  # noqa: BLE001 - 卡片注册失败不影响核心功能
        _LOGGER.warning("门锁卡片资源注册失败（不影响其他功能）", exc_info=True)

    await async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    username = entry.data[CONF_USERNAME]
    password_hash = entry.data[CONF_PASSWORD_HASH]
    family_id = entry.data.get(CONF_FAMILY_ID)

    options = entry.options
    try:
        transport_mode = TransportMode(
            options.get(CONF_TRANSPORT_MODE, TransportMode.AUTO.value)
        )
    except ValueError:
        transport_mode = TransportMode.AUTO
    try:
        poll_minutes = int(
            options.get(CONF_POLL_INTERVAL_MINUTES, DEFAULT_POLL_INTERVAL_MINUTES)
        )
    except (TypeError, ValueError):
        poll_minutes = DEFAULT_POLL_INTERVAL_MINUTES
    poll_minutes = max(
        MIN_POLL_INTERVAL_MINUTES,
        min(MAX_POLL_INTERVAL_MINUTES, poll_minutes),
    )

    lan_credentials = None
    if options.get(CONF_USE_INDEPENDENT_LAN_CREDENTIALS, False):
        lan_credentials = AccountCredentials(
            username=str(options.get(CONF_LAN_USERNAME, "")),
            password_hash=str(options.get(CONF_LAN_PASSWORD_HASH, "")),
            family_id=str(family_id or ""),
        )

    coordinator = OrviboMeshCoordinator(
        hass,
        AccountCredentials(
            username=username,
            password_hash=password_hash,
            family_id=str(family_id or ""),
        ),
        lock_user_names=options.get(CONF_LOCK_USER_NAMES),
        cloud=cloud_for_region(entry.data.get(CONF_CLOUD_REGION)),
        transport_mode=transport_mode,
        lan_credentials=lan_credentials,
        poll_interval=timedelta(minutes=poll_minutes),
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

    detected_region = coordinator.https_client.cloud.region.value
    if entry.data.get(CONF_CLOUD_REGION) != detected_region:
        updated_data = dict(entry.data)
        updated_data[CONF_CLOUD_REGION] = detected_region
        hass.config_entries.async_update_entry(entry, data=updated_data)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator
    _LOGGER.info("Coordinator 已注册到 hass.data")

    # 历史记录自动清理：保留 7 天，每周执行；卸载时取消定时任务
    coordinator.start_history_cleanup()
    entry.async_on_unload(coordinator.stop_history_cleanup)
    coordinator.start_temp_password_cleanup()
    entry.async_on_unload(coordinator.stop_temp_password_cleanup)

    # 使用 async_forward_entry_setups 一次性加载所有平台
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _apply_after_refresh():
        """等待 coordinator 第一次刷新完成后再应用区域映射。"""
        if not coordinator.last_update_success:
            await coordinator.async_refresh()
        await _async_assign_areas(hass, entry)
        await _apply_device_areas(hass, entry)
    
    area_task = hass.async_create_task(_apply_after_refresh())
    entry.async_on_unload(area_task.cancel)

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
    
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    _LOGGER.info(f"卸载结果: {unload_ok}")

    if unload_ok:
        coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if coordinator:
            await coordinator.async_cleanup()
            _LOGGER.info("Coordinator 清理完成")
        hass_data = hass.data.get(DOMAIN, {})
        hass_data.pop(entry.entry_id, None)
        _LOGGER.info("已从 hass.data 移除")

    return unload_ok
