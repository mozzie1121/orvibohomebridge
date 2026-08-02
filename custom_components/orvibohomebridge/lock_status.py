"""Normalized door-lock state and event parsing (pure stdlib, no HA deps).

门锁状态存在两种已观察到的协议形态：

- 形态 A（type=522 / classId=463，V5 Eyes 门锁）::

    properties.doorLock = {
        "lockState": "on" | "off",
        "doorState": "on" | "off",
        "insideLockState": "on" | "off",
    }

- 形态 B（属性型 ThingModel，type=300/522 子类型）::

    properties = {
        "door_status":  "open" | "closed",
        "reverse_lock": "locked" | "unlocked",
        "handle":       "locked" | "unlocked",
        "clild_lock":   "on" | "off",
    }

电池（形态 A）: ``properties.batteryManager``（干电池）与
``properties.batteryManager1``（锂电池），值为 ``{"level": int, "isSetupBattery": "on"|"off"}``。

事件（cmd=352）::

    {"event": {"server": "doorLock" | "doorbell",
               "name": "unlockEvent" | "ring" | "answered" | "bye",
               "value": {...}}}
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Optional


def _normalize_state(value: Any) -> Optional[bool]:
    """Map protocol state strings to booleans; return None for unknown shapes."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized in {"on", "open", "locked", "up", "1", "true"}:
        return True
    if normalized in {"off", "closed", "unlocked", "down", "0", "false"}:
        return False
    return None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def normalize_door_lock_properties(properties: Any) -> dict[str, Any]:
    """Normalize either lock property morphology to one boolean dict."""
    props = _mapping(properties)
    result: dict[str, Any] = {
        "locked": None,
        "door_open": None,
        "inside_locked": None,
        "child_locked": None,
        "leave_home_armed": None,
        "raw": {},
    }

    door_lock = _mapping(props.get("doorLock"))
    if door_lock:
        result["locked"] = _normalize_state(door_lock.get("lockState"))
        result["door_open"] = _normalize_state(door_lock.get("doorState"))
        result["inside_locked"] = _normalize_state(door_lock.get("insideLockState"))
        result["raw"]["doorLock"] = dict(door_lock)

    # 形态 B 的扁平权威字段优先（部分固件两种形态同时出现）
    for key in ("reverse_lock", "lock_state", "lockStatus"):
        value = _normalize_state(props.get(key))
        if value is not None:
            result["locked"] = value
    for key in ("door_status",):
        value = _normalize_state(props.get(key))
        if value is not None:
            result["door_open"] = value
    child = _normalize_state(props.get("clild_lock"))
    if child is not None:
        result["child_locked"] = child
    armed = _normalize_state(door_lock.get("leaveHomeAlarmCfg"))
    if armed is None:
        armed = _normalize_state(props.get("leaveHomeAlarmCfg"))
    if armed is not None:
        result["leave_home_armed"] = armed
    result["raw"]["flat"] = {
        key: props[key]
        for key in (
            "door_status",
            "reverse_lock",
            "handle",
            "clild_lock",
            "lock_state",
            "leaveHomeAlarmCfg",
        )
        if key in props
    }
    return result


def normalize_battery_properties(properties: Any) -> dict[str, Any]:
    """Normalize dry/lithium battery managers to level + setup flags."""
    props = _mapping(properties)
    result: dict[str, Any] = {}
    for source_key, out_prefix in (
        ("batteryManager", "dry"),
        ("batteryManager1", "lithium"),
    ):
        manager = _mapping(props.get(source_key))
        if not manager:
            continue
        level = manager.get("level")
        try:
            level = int(level) if level not in (None, "") else None
        except (TypeError, ValueError):
            level = None
        result[f"{out_prefix}_battery_level"] = level
        setup = _normalize_state(manager.get("isSetupBattery"))
        if setup is not None:
            result[f"{out_prefix}_battery_setup"] = setup
            # isSetupBattery=off 表示该电池槽未安装电池，level=0 无意义，置为未知
            if not setup:
                result[f"{out_prefix}_battery_level"] = None
    return result


def normalize_lock_event(payload: Any) -> Optional[dict[str, Any]]:
    """Normalize cmd=352 doorbell/unlock events for the HA event bus."""
    if not isinstance(payload, Mapping):
        return None
    event = payload.get("event")
    if not isinstance(event, Mapping):
        return None
    server = event.get("server")
    name = event.get("name")
    value = _mapping(event.get("value"))

    if server == "doorLock" and name == "unlockEvent":
        return {
            "kind": "unlock",
            "unlock_type": value.get("type"),
            "unlock_user_id": value.get("userId"),
            "time": payload.get("time"),
        }
    if server == "doorLock" and name == "errorUnlockEvent":
        return {
            "kind": "error_unlock",
            "unlock_type": value.get("type"),
            "time": payload.get("time"),
        }
    if server == "doorLock" and name == "doorUnclose":
        return {"kind": "door_unclose", "time": payload.get("time")}
    if server == "doorLock" and name == "picklockEvent":
        return {
            "kind": "picklock",
            "video_url": value.get("videoUrl"),
            "pic_url": value.get("url"),
            "time": payload.get("time"),
        }
    if server == "doorLock" and name == "leaveHomeEvent":
        return {
            "kind": "leave_home",
            "video_url": value.get("videoUrl"),
            "pic_url": value.get("url"),
            "time": payload.get("time"),
        }
    if server == "doorbell" and name == "ring":
        return {
            "kind": "ring",
            "doorbell_url": value.get("url"),
            "doorbell_ip": value.get("doorbell_local_Ip"),
            "time": payload.get("time"),
        }
    if server == "doorbell" and name == "answered":
        return {"kind": "answered", "time": payload.get("time")}
    if server == "doorbell" and name == "bye":
        return {"kind": "bye", "time": payload.get("time")}
    return None


def normalize_message_event(payload: Any) -> Optional[dict[str, Any]]:
    """Normalize cmd=82 push-message packets (门锁等文本消息/告警)。"""
    if not isinstance(payload, Mapping):
        return None
    if payload.get("cmd") != 82:
        return None
    data = payload.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (ValueError, TypeError):
            data = {}
    if not isinstance(data, Mapping):
        data = {}

    info_type = payload.get("infoType")
    device_type = data.get("deviceType")
    if info_type != 12 and device_type not in (522, 107, 300):
        return None
    return {
        "kind": "message",
        "device_id": data.get("deviceId") or payload.get("deviceId") or "",
        "uid": data.get("uid") or payload.get("uid") or "",
        "is_alarm": data.get("isAlarm"),
        "message_type": payload.get("messageType"),
        "text": payload.get("text"),
        "pic_url": data.get("picUrl"),
        "time": data.get("time"),
    }
