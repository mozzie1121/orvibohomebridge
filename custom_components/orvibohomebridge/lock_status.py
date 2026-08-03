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
        # V5 Eyes（type=522/classId=463）实机验证（反语义，与 App 显示一致）：
        # lockState="on"=已解锁、"off"=已锁定；
        # insideLockState="off"=门内反锁中、"on"=反锁解除（181826 反锁/解除抓包）。
        lock_raw = _normalize_state(door_lock.get("lockState"))
        result["locked"] = None if lock_raw is None else not lock_raw
        result["door_open"] = _normalize_state(door_lock.get("doorState"))
        inside_raw = _normalize_state(door_lock.get("insideLockState"))
        result["inside_locked"] = None if inside_raw is None else not inside_raw
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
            # isSetupBattery=off 表示电池被取出/未安装（实测：扣电池触发推送），
            # level=0 无意义，置为未知
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


def resolve_opened_by(
    last_unlock: Any,
    elapsed: float,
    window: float = 30,
) -> Optional[dict[str, Any]]:
    """把一次开门事件归属到窗口内最近一次开锁（返回 user_id + 开锁方式）。"""
    if not isinstance(last_unlock, Mapping):
        return None
    if not isinstance(elapsed, (int, float)) or elapsed < 0 or elapsed > window:
        return None
    user_id = last_unlock.get("user_id")
    if user_id is None:
        return None
    return {
        "user_id": str(user_id),
        "unlock_type": last_unlock.get("unlock_type"),
    }


def derive_lock_status(
    locked: Optional[bool],
    door_open: Optional[bool],
    inside_locked: Optional[bool],
) -> Optional[str]:
    """从归一化字段推导面向用户的锁状态。

    规则（绑定门磁）：门磁开 + 锁上锁 = 异常（异常关门/测试状态）；
    门磁开 = 未上锁；门磁关 = 门内反锁/上锁/未上锁。
    """
    if door_open is True and locked is True:
        return "abnormal"
    if door_open is True:
        return "unlocked"
    if inside_locked is True:
        return "inside_locked"
    if locked is True:
        return "locked"
    if locked is False:
        return "unlocked"
    return None


def format_unlock_label(user_id: Any, name: Optional[str]) -> str:
    """生成"xxx开门"显示文本（未配置名称时回退为 用户{id}）。"""
    if name and str(name).strip():
        return f"{name}开门"
    if user_id not in (None, ""):
        return f"用户{user_id}开门"
    return "无"


def parse_lock_user_names(text: Any) -> dict[str, str]:
    """解析多行"用户ID=名称"文本为映射，兼容冒号分隔，忽略空行/坏行。"""
    result: dict[str, str] = {}
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        separator = "=" if "=" in line else (":" if ":" in line else None)
        if separator is None:
            continue
        user_id, name = line.split(separator, 1)
        user_id = user_id.strip()
        name = name.strip()
        if user_id and name:
            result[user_id] = name
    return result


def format_lock_user_names(mapping: Any) -> str:
    """把映射格式化为多行"用户ID=名称"，数字 ID 优先排序。"""
    if not isinstance(mapping, Mapping):
        return ""
    def sort_key(item: tuple[str, Any]) -> tuple[int, str]:
        user_id = str(item[0])
        return (0 if user_id.isdigit() else 1, user_id)

    return "\n".join(
        f"{user_id}={name}"
        for user_id, name in sorted(mapping.items(), key=sort_key)
    )
