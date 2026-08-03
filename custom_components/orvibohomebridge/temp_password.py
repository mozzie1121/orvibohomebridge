"""临时密码管理核心：响应解析 / 过期判断 / 回收规则（HA 无关）。"""

from __future__ import annotations

import time
from typing import Any, Mapping, Optional


def parse_grant_response(resp: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """解析 cmd=246 下发响应，返回归一化记录；失败返回 None。

    记录字段：password/authorized_id/authorized_unlock_id/type/start_time/
    end_time/number/unlock_num/phone/name。
    """
    if not isinstance(resp, Mapping):
        return None
    status = resp.get("status")
    if status not in (None, 0, "0"):
        return None
    password = resp.get("code") or ""
    auth = resp.get("authorizedUnlock")
    if isinstance(auth, Mapping):
        password = password or auth.get("password") or ""
        authorized_id = auth.get("authorizedId")
        authorized_unlock_id = auth.get("authorizedUnlockId") or ""
        start_time = auth.get("startTime")
        end_time = auth.get("endTime")
        number = auth.get("number")
        unlock_num = auth.get("unlockNum")
    else:
        authorized_id = None
        authorized_unlock_id = ""
        start_time = resp.get("startTime")
        end_time = resp.get("endTime")
        number = resp.get("number")
        unlock_num = 0
    if not password or authorized_id is None:
        return None
    return {
        "password": str(password),
        "authorized_id": int(authorized_id),
        "authorized_unlock_id": str(authorized_unlock_id),
        "type": int(resp.get("type") or 2),
        "start_time": int(start_time) if start_time else int(time.time()),
        "end_time": int(end_time) if end_time else 0,
        "number": int(number) if number is not None else 0,
        "unlock_num": int(unlock_num) if unlock_num is not None else 0,
        "phone": resp.get("phone") or "",
        "name": resp.get("userName") or "",
    }


def parse_authorization_item(item: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """解析 readtable authorizedUnlock 单条记录（服务器端完整列表）。"""
    if not isinstance(item, Mapping):
        return None
    if item.get("delFlag"):  # 已删除的授权跳过
        return None
    # App 实测：authorizeStatus=0 为有效，3 为已删除/失效（delFlag 恒为 0）
    status = item.get("authorizeStatus")
    if status not in (None, 0, "0"):
        return None
    password = item.get("password") or ""
    authorized_id = item.get("authorizedId")
    if not password or authorized_id is None:
        return None
    return {
        "password": str(password),
        "authorized_id": int(authorized_id),
        "authorized_unlock_id": item.get("authorizedUnlockId") or "",
        "type": 0,  # 服务器列表无 type 字段（下发响应才有），保留字段占位
        "start_time": int(item.get("startTime") or 0),
        "end_time": int(item.get("endTime") or 0),
        "number": int(item.get("number") or 0),
        "unlock_num": int(item.get("unlockNum") or 0),
        "phone": item.get("phone") or "",
        "name": "",
        "authorize_status": item.get("authorizeStatus"),
    }


def is_expired(record: Mapping[str, Any], now: Optional[int] = None) -> bool:
    """临时密码是否已过期（结束时间到 或 次数用尽）。"""
    now = int(time.time()) if now is None else int(now)
    end = int(record.get("end_time") or 0)
    if end > 0 and now >= end:
        return True
    number = int(record.get("number") or 0)
    unlock_num = int(record.get("unlock_num") or 0)
    if number > 0 and unlock_num >= number:
        return True
    return False


def describe_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """生成事件/传感器展示用的友好字段。"""
    start = int(record.get("start_time") or 0)
    end = int(record.get("end_time") or 0)
    return {
        "password": record.get("password"),
        "authorized_id": record.get("authorized_id"),
        "name": record.get("name"),
        "phone": record.get("phone"),
        "type": record.get("type"),
        "number": record.get("number"),
        "unlock_num": record.get("unlock_num"),
        "start_time": start,
        "end_time": end,
        "expired": is_expired(record),
    }
