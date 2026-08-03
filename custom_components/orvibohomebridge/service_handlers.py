"""Home Assistant service registration for lock, media, and history features."""

from __future__ import annotations

import logging
import re
from typing import Any

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse

from .const import DOMAIN
from .device_types import DeviceCategory, classify_device
from .video_archive import normalize_event_object_key

_LOGGER = logging.getLogger(__name__)

SERVICE_REFRESH = "refresh_devices"
SERVICE_SET_LOCK_USER_NAME = "set_lock_user_name"
SERVICE_FETCH_VIDEO = "fetch_video"
SERVICE_LIST_EVENTS = "list_events"
SERVICE_CLEANUP_HISTORY = "cleanup_history"
SERVICE_GRANT_TEMP_PASSWORD = "grant_temp_password"
SERVICE_REVOKE_TEMP_PASSWORD = "revoke_temp_password"
SERVICE_LIST_TEMP_PASSWORDS = "list_temp_passwords"


def _targets(hass: HomeAssistant, entry_id: object) -> list[Any]:
    coordinators = hass.data.get(DOMAIN, {})
    if entry_id:
        coordinator = coordinators.get(str(entry_id))
        return [coordinator] if coordinator is not None else []
    return list(coordinators.values())


def _bounded_int(
    value: object,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


def _is_lock(coordinator: Any, device_id: str) -> bool:
    device = coordinator.devices.get(device_id)
    return device is not None and classify_device(device) == DeviceCategory.DOOR_LOCK


def _public_event(event: dict[str, Any]) -> dict[str, Any]:
    """Do not expose Home Assistant filesystem paths in service responses."""
    return {
        key: event[key]
        for key in ("device_id", "kind", "time", "type", "media_id")
        if key in event
    }


def _public_authorizations(result: dict[str, Any]) -> dict[str, Any]:
    """Defense in depth: never expose stored passwords from list services."""
    public: dict[str, Any] = {}
    for device_id, records in result.items():
        if not isinstance(records, list):
            continue
        public[device_id] = [
            {key: value for key, value in record.items() if key != "password"}
            for record in records
            if isinstance(record, dict)
        ]
    return public


async def async_register_services(hass: HomeAssistant) -> None:
    """Register integration services once during domain setup."""

    async def handle_refresh(call: ServiceCall) -> None:
        entry_id = str(call.data.get("entry_id") or "")
        targets = _targets(hass, entry_id)
        if not entry_id or not targets:
            _LOGGER.error("refresh_devices 需要有效的 entry_id")
            return
        await targets[0].async_request_refresh()

    async def handle_set_lock_user_name(call: ServiceCall) -> None:
        device_id = str(call.data.get("device_id") or "").strip()
        user_id = str(call.data.get("user_id") or "").strip()
        name = str(call.data.get("name") or "").strip()[:64]
        if not device_id or not user_id:
            _LOGGER.error("set_lock_user_name 需要 device_id 和 user_id")
            return
        updated = False
        for coordinator in _targets(hass, call.data.get("entry_id")):
            if _is_lock(coordinator, device_id):
                updated |= coordinator.set_lock_user_name(device_id, user_id[:64], name)
        if not updated:
            _LOGGER.warning("未找到对应门锁或配置项")

    async def handle_fetch_video(call: ServiceCall) -> dict[str, Any]:
        device_id = str(call.data.get("device_id") or "").strip()
        object_key = normalize_event_object_key(
            str(call.data.get("object_key") or "")
        )
        if not device_id or object_key is None:
            return {"error": "需要有效的门锁 device_id 和事件录像对象键"}
        for coordinator in _targets(hass, call.data.get("entry_id")):
            if not _is_lock(coordinator, device_id):
                continue
            result = await coordinator.async_fetch_video(device_id, object_key)
            if result and "error" not in result:
                return {
                    "ok": True,
                    "media_id": result.get("media_id", ""),
                }
        return {"error": "未找到门锁配置项或录像拉取失败"}

    async def handle_list_events(call: ServiceCall) -> dict[str, Any]:
        device_id = str(call.data.get("device_id") or "").strip()
        limit = _bounded_int(call.data.get("limit"), 100, 1, 500)
        events: list[dict[str, Any]] = []
        for coordinator in _targets(hass, call.data.get("entry_id")):
            if device_id and not _is_lock(coordinator, device_id):
                continue
            events.extend(await coordinator.async_list_events(device_id, limit))
            if len(events) >= limit:
                break
        return {"events": [_public_event(item) for item in events[:limit]]}

    async def handle_cleanup_history(call: ServiceCall) -> dict[str, int]:
        device_id = str(call.data.get("device_id") or "").strip()
        keep_days = _bounded_int(call.data.get("keep_days"), 7, 0, 3650)
        raw_max_entries = call.data.get("max_entries")
        max_entries = (
            _bounded_int(raw_max_entries, 500, 1, 10000)
            if raw_max_entries not in (None, "")
            else None
        )
        total = 0
        for coordinator in _targets(hass, call.data.get("entry_id")):
            if device_id and not _is_lock(coordinator, device_id):
                continue
            total += await coordinator.async_cleanup_history(
                keep_days=keep_days,
                device_id=device_id,
                max_entries=max_entries,
            )
        return {"removed": total}

    async def handle_grant_temp_password(call: ServiceCall) -> dict[str, Any]:
        device_id = str(call.data.get("device_id") or "").strip()
        auth_type = _bounded_int(call.data.get("type"), 2, 1, 2)
        minutes = _bounded_int(call.data.get("minutes"), 1440, 1, 525600)
        number = _bounded_int(call.data.get("number"), 1, 0, 100)
        name = str(call.data.get("name") or "").strip()[:64]
        phone = str(call.data.get("phone") or "").strip()[:32]
        if phone and not re.fullmatch(r"\+?[0-9]{6,20}", phone):
            return {"error": "phone 必须为 6 到 20 位电话号码"}
        start_time = call.data.get("start_time")
        end_time = call.data.get("end_time")
        try:
            start = int(start_time) if start_time not in (None, "") else None
            end = int(end_time) if end_time not in (None, "") else None
        except (TypeError, ValueError):
            return {"error": "start_time/end_time 必须为 Unix 秒时间戳"}
        if start is not None and end is not None and end <= start:
            return {"error": "end_time 必须晚于 start_time"}
        for coordinator in _targets(hass, call.data.get("entry_id")):
            if not device_id:
                device_id = next(
                    (
                        did
                        for did, device in coordinator.devices.items()
                        if classify_device(device) == DeviceCategory.DOOR_LOCK
                    ),
                    "",
                )
            if not device_id or not _is_lock(coordinator, device_id):
                continue
            return await coordinator.async_grant_temp_password(
                device_id=device_id,
                auth_type=auth_type,
                minutes=minutes,
                number=number,
                name=name,
                phone=phone,
                start_time=start,
                end_time=end,
            )
        return {"error": "未找到可用的配置项或门锁设备"}

    async def handle_revoke_temp_password(call: ServiceCall) -> dict[str, Any]:
        device_id = str(call.data.get("device_id") or "").strip()
        authorized_id = _bounded_int(call.data.get("authorized_id"), 0, 0, 2**31 - 1)
        if not device_id or authorized_id <= 0:
            return {"error": "需要门锁 device_id 和有效的 authorized_id"}
        for coordinator in _targets(hass, call.data.get("entry_id")):
            if _is_lock(coordinator, device_id):
                return await coordinator.async_revoke_temp_password(
                    device_id, authorized_id
                )
        return {"error": "未找到门锁设备或配置项"}

    async def handle_list_temp_passwords(call: ServiceCall) -> dict[str, Any]:
        device_id = str(call.data.get("device_id") or "").strip()
        result: dict[str, Any] = {}
        for coordinator in _targets(hass, call.data.get("entry_id")):
            if device_id and not _is_lock(coordinator, device_id):
                continue
            result.update(await coordinator.async_list_temp_passwords(device_id))
        return _public_authorizations(result)

    hass.services.async_register(DOMAIN, SERVICE_REFRESH, handle_refresh)
    hass.services.async_register(
        DOMAIN, SERVICE_SET_LOCK_USER_NAME, handle_set_lock_user_name
    )
    for service, handler in (
        (SERVICE_FETCH_VIDEO, handle_fetch_video),
        (SERVICE_LIST_EVENTS, handle_list_events),
        (SERVICE_CLEANUP_HISTORY, handle_cleanup_history),
        (SERVICE_GRANT_TEMP_PASSWORD, handle_grant_temp_password),
        (SERVICE_REVOKE_TEMP_PASSWORD, handle_revoke_temp_password),
        (SERVICE_LIST_TEMP_PASSWORDS, handle_list_temp_passwords),
    ):
        hass.services.async_register(
            DOMAIN,
            service,
            handler,
            supports_response=SupportsResponse.OPTIONAL,
        )
