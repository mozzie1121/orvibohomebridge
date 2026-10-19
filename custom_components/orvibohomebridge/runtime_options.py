"""Runtime helpers for optional availability and release notifications."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
import re
from typing import Any, Mapping

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
_LATEST_RELEASE_URL = (
    "https://api.github.com/repos/maycode0-0/"
    "orvibohomebridge/releases/latest"
)
_VERSION_PATTERN = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def integration_version() -> str:
    """Read the installed version from the integration manifest."""

    manifest = Path(__file__).with_name("manifest.json")
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return "0.0.0"
    return str(payload.get("version") or "0.0.0")


def version_tuple(value: object) -> tuple[int, int, int] | None:
    """Return a stable semantic version tuple, ignoring prerelease tags."""

    match = _VERSION_PATTERN.fullmatch(str(value or "").strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


def _online_value(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"online", "on", "true", "1"}:
            return True
        if normalized in {"offline", "off", "false", "0"}:
            return False
    return None


class AvailabilityNotifier:
    """Notify only after a selected device changes its known online state."""

    def __init__(
        self,
        hass: Any,
        entry_id: str,
        devices: Mapping[str, Mapping[str, Any]],
        states: Mapping[str, Mapping[str, Any]],
        selected_ids: set[str],
        *,
        notify_online: bool = True,
        notify_offline: bool = True,
        notify_service: str = "",
    ) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.devices = devices
        self.states = states
        self.selected_ids = selected_ids
        self.notify_online = notify_online
        self.notify_offline = notify_offline
        self.notify_service = notify_service
        self._known: dict[str, bool] = {}
        for device_id in selected_ids:
            online = _online_value((states.get(device_id) or {}).get("online"))
            if online is not None:
                self._known[device_id] = online

    def process(self) -> None:
        """Compare current state and schedule notifications for transitions."""

        for device_id in self.selected_ids:
            online = _online_value(
                (self.states.get(device_id) or {}).get("online")
            )
            if online is None:
                continue
            previous = self._known.get(device_id)
            self._known[device_id] = online
            if previous is None or previous == online:
                continue
            if (online and not self.notify_online) or (
                not online and not self.notify_offline
            ):
                continue
            self.hass.async_create_task(
                self._send(device_id, online)
            )

    async def _send(self, device_id: str, online: bool) -> None:
        device = self.devices.get(device_id) or {}
        name = str(device.get("device_name") or "ORVIBO 设备")
        state_text = "上线" if online else "离线"
        title = f"ORVIBO 设备{state_text}"
        message = f"{name} 已{state_text}"

        if self.notify_service and "." in self.notify_service:
            domain, service = self.notify_service.split(".", 1)
            data = {"title": title, "message": message}
        else:
            domain, service = "persistent_notification", "create"
            digest = hashlib.sha256(
                f"{self.entry_id}:{device_id}".encode()
            ).hexdigest()[:16]
            data = {
                "title": title,
                "message": message,
                "notification_id": f"{DOMAIN}_availability_{digest}",
            }
        await self.hass.services.async_call(
            domain,
            service,
            data,
            blocking=False,
        )


class IntegrationUpdateChecker:
    """Periodically notify when a newer stable GitHub release exists."""

    def __init__(
        self,
        hass: Any,
        *,
        interval_hours: int,
        current_version: str | None = None,
        session: Any = None,
    ) -> None:
        self.hass = hass
        self.interval_seconds = max(1, int(interval_hours)) * 3600
        self.current_version = current_version or integration_version()
        self.session = session or async_get_clientsession(hass)

    async def run(self) -> None:
        """Check immediately and then at the configured interval."""

        while True:
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001
                _LOGGER.debug(
                    "ORVIBO Smart Control 更新检查失败: %s",
                    type(error).__name__,
                )
            await asyncio.sleep(self.interval_seconds)

    async def check_once(self) -> str | None:
        """Return the newer tag after notifying, otherwise return ``None``."""

        async with self.session.get(
            _LATEST_RELEASE_URL,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "orvibohomebridge-update-check",
            },
            timeout=15,
        ) as response:
            if response.status != 200:
                return None
            payload = await response.json(content_type=None)

        latest = str(payload.get("tag_name") or "")
        latest_tuple = version_tuple(latest)
        current_tuple = version_tuple(self.current_version)
        if (
            latest_tuple is None
            or current_tuple is None
            or latest_tuple <= current_tuple
        ):
            return None

        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": "ORVIBO Smart Control 有可用更新",
                "message": (
                    f"已安装 v{self.current_version}，最新稳定版本为 "
                    f"{latest}。请通过 HACS 或项目发布页更新。"
                ),
                "notification_id": f"{DOMAIN}_update_available",
            },
            blocking=False,
        )
        return latest
