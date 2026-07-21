"""Diagnostics support for orvibohomebridge.

在 HA 设备页面 → 三个点 → 诊断信息 中查看原始数据。
包含：设备列表、状态、最近 50 条 MQTT 推送原始记录。
"""

from __future__ import annotations
import time
from typing import Any
from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from .const import DOMAIN

TO_REDACT = {"userName", "user_id", "access_token", "password", "phoneToken"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """返回配置条目的诊断信息。"""
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not coordinator:
        return {"error": "coordinator not found"}

    now = time.time()

    # 整理设备列表
    devices_raw = {}
    for dev_id, dev in coordinator.devices.items():
        devices_raw[dev_id] = {
            "device_name": dev.get("device_name"),
            "device_type": dev.get("device_type"),
            "device_type_raw": dev.get("device_type_raw"),
            "model": dev.get("model"),
            "uid": dev.get("uid"),
            "online": dev.get("online"),
        }

    # 整理当前状态
    states_raw = {}
    for dev_id, st in coordinator.device_states.items():
        states_raw[dev_id] = dict(st)
        # 去掉 over-length 属性
        states_raw[dev_id].pop("properties", None)

    # 整理最近50条cmd42推送
    cmd42_entries = []
    for entry_ in getattr(coordinator, "_cmd42_log", [])[-50:]:
        cmd42_entries.append({
            "ago_s": round(now - entry_["ts"], 1) if entry_.get("ts") else None,
            "device_id": entry_["device_id"],
            "raw": async_redact_data(entry_.get("raw", {}), TO_REDACT),
        })

    info = {
        "device_count": len(devices_raw),
        "devices": devices_raw,
        "states": async_redact_data(states_raw, TO_REDACT),
        "recent_cmd42_push": cmd42_entries,
    }
    return info


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """返回某个设备专有的诊断信息。"""
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not coordinator:
        return {"error": "coordinator not found"}

    # 从 device identifier 反查 device_id
    device_id = None
    for domain, identifier in device.identifiers:
        if domain == DOMAIN:
            device_id = identifier
            break

    now = time.time()

    result: dict[str, Any] = {}

    # 设备原始信息
    if device_id and device_id in coordinator.devices:
        result["device_info"] = coordinator.devices[device_id]
    else:
        result["device_info"] = {"error": f"device {device_id} not found in coordinator"}

    # 当前状态
    if device_id and device_id in coordinator.device_states:
        result["current_state"] = dict(coordinator.device_states[device_id])
        result["current_state"].pop("properties", None)
    else:
        result["current_state"] = None

    # 该设备的最近cmd42推送
    device_cmd42 = []
    for entry_ in getattr(coordinator, "_cmd42_log", []):
        if entry_["device_id"] == device_id or entry_.get("raw", {}).get("uid") == device_id:
            device_cmd42.append({
                "ago_s": round(now - entry_["ts"], 1) if entry_.get("ts") else None,
                "raw": async_redact_data(entry_.get("raw", {}), TO_REDACT),
            })
    result["recent_cmd42_push"] = device_cmd42[-20:]

    return result
