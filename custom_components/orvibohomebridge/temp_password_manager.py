"""Temporary lock-password lifecycle and server synchronization."""

from __future__ import annotations

from datetime import timedelta
import logging
import time
from typing import Any, Callable, Dict, MutableMapping, Optional

from .const import TEMP_PASSWORD_EVENT
from .device_types import DeviceCategory, classify_device
from .redact import fingerprint
from .temp_password import (
    describe_record,
    is_expired,
    parse_authorization_item,
    parse_grant_response,
)


_LOGGER = logging.getLogger(__name__)


class TempPasswordManager:
    """Own temporary-password records, commands, refresh, and cleanup."""

    MAX_ACTIVE = 4

    def __init__(
        self,
        hass: Any,
        devices: MutableMapping[str, dict[str, Any]],
        device_states: MutableMapping[str, dict[str, Any]],
        https_client: Any,
        ssl_client: Callable[[], Any],
        on_updated: Callable[[], None],
        redaction_salt: bytes,
    ) -> None:
        self.hass = hass
        self.devices = devices
        self.device_states = device_states
        self.https_client = https_client
        self._ssl_client = ssl_client
        self._on_updated = on_updated
        self._redaction_salt = redaction_salt
        self._records: Dict[str, list[dict[str, Any]]] = {}
        self._cleanup_unsub = None

    async def grant(
        self,
        device_id: str,
        auth_type: int = 2,
        minutes: int = 1440,
        number: int = 1,
        name: str = "",
        phone: str = "",
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        device_uid: str = "",
    ) -> Dict[str, Any]:
        ssl_client = self._ssl_client()
        if ssl_client is None:
            return {"error": "SSL 客户端未就绪"}
        device = self.devices.get(device_id)
        if device is None or classify_device(device) != DeviceCategory.DOOR_LOCK:
            return {"error": "设备不存在或不是门锁"}
        if auth_type not in (1, 2):
            return {"error": "授权类型必须为 1 或 2"}
        if not 1 <= minutes <= 525600:
            return {"error": "有效期必须在 1 到 525600 分钟之间"}
        if not 0 <= number <= 100:
            return {"error": "可用次数必须在 0 到 100 之间"}

        server_records = await self.fetch_server_records()
        active = [
            record
            for record in server_records
            if record.get("device_id") == device_id
        ]
        if len(active) >= self.MAX_ACTIVE:
            return {
                "error": f"临时密码已达上限（{self.MAX_ACTIVE} 个），请先删除旧密码"
            }

        records = self._records.setdefault(device_id, [])
        device_uid = device_uid or device.get("uid", "")
        name = name or f"临时用户 {time.strftime('%m%d%H%M')}"
        response = await ssl_client.send_temp_password(
            device_id=device_id,
            device_uid=device_uid,
            name=name,
            auth_type=auth_type,
            minutes=minutes,
            number=number,
            phone=phone,
            start_time=start_time,
            end_time=end_time,
        )
        if not response:
            return {"error": "未收到 cmd=246 响应（超时）"}
        if response.get("status") not in (None, 0, "0"):
            return {
                "error": f"下发失败 status={response.get('status')} msg={response.get('msg')}"
            }
        record = parse_grant_response(response)
        if record is None:
            return {"error": "响应缺少密码或 authorizedId"}
        record["device_id"] = device_id
        records.append(record)
        if len(records) > 10:
            self._records[device_id] = records[-10:]

        info = describe_record(record)
        public_info = {key: value for key, value in info.items() if key != "password"}
        self.hass.bus.async_fire(
            TEMP_PASSWORD_EVENT, {"device_id": device_id, **public_info}
        )
        self._touch(device_id)
        _LOGGER.info(
            "临时密码已下发 device=%s authorizedId=%s",
            fingerprint(device_id, self._redaction_salt),
            record["authorized_id"],
        )
        return info

    async def revoke(
        self, device_id: str, authorized_id: int, device_uid: str = ""
    ) -> Dict[str, Any]:
        ssl_client = self._ssl_client()
        if ssl_client is None:
            return {"error": "SSL 客户端未就绪"}
        device = self.devices.get(device_id)
        if device is None or classify_device(device) != DeviceCategory.DOOR_LOCK:
            return {"error": "设备不存在或不是门锁"}
        if authorized_id <= 0:
            return {"error": "authorized_id 必须为正整数"}
        response = await ssl_client.delete_authorization(
            device_id=device_id,
            device_uid=device_uid or device.get("uid", ""),
            authorized_id=authorized_id,
        )
        if not response:
            return {"error": "未收到 cmd=247 响应（超时）"}
        if response.get("status") not in (None, 0, "0"):
            return {"error": f"删除失败 status={response.get('status')}"}
        records = self._records.get(device_id, [])
        self._records[device_id] = [
            record
            for record in records
            if int(record.get("authorized_id", -1)) != int(authorized_id)
        ]
        self._touch(device_id)
        _LOGGER.info(
            "临时密码已删除 device=%s authorizedId=%s",
            fingerprint(device_id, self._redaction_salt),
            authorized_id,
        )
        return {"ok": True, "authorized_id": authorized_id}

    async def list(self, device_id: str = "") -> Dict[str, Any]:
        records = await self.fetch_server_records()
        if device_id:
            records = [
                record
                for record in records
                if record.get("device_id") == device_id
            ]
        result: Dict[str, Any] = {}
        for record in records:
            target = record.get("device_id") or "unknown"
            info = describe_record(record)
            info.pop("password", None)
            result.setdefault(target, []).append(info)
        return result

    async def fetch_server_records(self) -> list[dict[str, Any]]:
        client = self.https_client
        if client is None or not client.is_logged_in:
            _LOGGER.warning(
                "拉取临时密码列表跳过: client=%s logged_in=%s",
                client is not None,
                client.is_logged_in if client else None,
            )
            return []
        try:
            data = await client._readtable(device_flag=0)
        except Exception as error:  # noqa: BLE001
            _LOGGER.warning("拉取临时密码列表失败: %s", error)
            return []
        if not isinstance(data, dict):
            _LOGGER.warning(
                "拉取临时密码列表: readtable 返回非 dict: %s", type(data)
            )
            return []
        authorizations = data.get("authorizedUnlock")
        _LOGGER.info(
            "拉取临时密码列表: readtable keys=%s authorizedUnlock=%s",
            list(data.keys())[:12],
            (
                f"list[{len(authorizations)}]"
                if isinstance(authorizations, list)
                else type(authorizations).__name__
            ),
        )
        records = []
        for item in authorizations or []:
            record = parse_authorization_item(item)
            if record is None:
                continue
            record["device_id"] = item.get("deviceId") or ""
            records.append(record)

        previous = self._records
        self._records = {}
        for record in records:
            device_id = record["device_id"]
            existing = next(
                (
                    item
                    for item in previous.get(device_id, [])
                    if int(item.get("authorized_id", -1))
                    == record["authorized_id"]
                ),
                None,
            )
            merged = dict(record)
            if existing:
                merged["name"] = existing.get("name") or ""
                merged["type"] = existing.get("type") or 0
            self._records.setdefault(device_id, []).append(merged)
        return records

    def state(self, device_id: str) -> Optional[dict[str, Any]]:
        active = [
            record
            for record in self._records.get(device_id, [])
            if not is_expired(record)
        ]
        return describe_record(active[-1]) if active else None

    def start_cleanup(self) -> None:
        if self._cleanup_unsub is not None:
            return
        from homeassistant.helpers.event import async_track_time_interval

        async def run(_now=None) -> None:
            for device_id, records in list(self._records.items()):
                for record in list(records):
                    if not is_expired(record):
                        continue
                    try:
                        await self.revoke(
                            device_id, int(record["authorized_id"])
                        )
                    except Exception:  # noqa: BLE001
                        _LOGGER.warning(
                            "临时密码自动回收失败 device=%s authorizedId=%s",
                            fingerprint(device_id, self._redaction_salt),
                            record.get("authorized_id"),
                        )

        self.hass.async_create_task(run())
        self._cleanup_unsub = async_track_time_interval(
            self.hass, run, timedelta(hours=6)
        )

    def stop_cleanup(self) -> None:
        if self._cleanup_unsub is not None:
            self._cleanup_unsub()
            self._cleanup_unsub = None

    def _touch(self, device_id: str) -> None:
        self.device_states.setdefault(device_id, {})["temp_password_ts"] = time.time()
        self._on_updated()
