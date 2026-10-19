"""Private cloud bootstrap and stable device/channel selection for live cameras.

Only this module's descriptors reach the entity layer. Cloud credentials are
refreshed in memory for each connection; inventory positions are never saved.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
import json
from typing import Any

import aiohttp

from .meari_channels import NVR_CHANNEL_PATH, NVR_PROPERTIES, _nvr_snapshot, summarize_nvr_channels
from .meari_inventory import GROUPS, MAX_BODY_BYTES, build_inventory_request, build_vendor_request
from .meari_probe_context import build_probe_context
from .meari_session import _object, summarize_session_response


@dataclass(frozen=True)
class LiveCameraDescription:
    """A channel anchored to a selected ORVIBO device and stable vendor ID."""

    device_id: str
    vendor_id: str = field(repr=False)
    channel_id: int
    is_nvr: bool


def _devices(payload: object) -> list[tuple[int, Mapping[str, Any]]]:
    raw = _object(payload)
    if str(raw.get("resultCode")) != "1001":
        raise ValueError("inventory_unavailable")
    root = raw if any(isinstance(raw.get(group), list) for group in GROUPS) else _object(raw.get("result"))
    if not any(isinstance(root.get(group), list) for group in GROUPS):
        raise ValueError("inventory_unavailable")
    rows = []
    for group in GROUPS:
        values = root.get(group)
        if isinstance(values, list):
            for value in values:
                if isinstance(value, Mapping):
                    rows.append((len(rows) + 1, value))
                    if len(rows) > 512:
                        raise ValueError("inventory_limit")
    return rows


def _vendor_id(row: Mapping[str, Any]) -> str:
    value = row.get("deviceID")
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    return value if isinstance(value, str) and 0 < len(value) <= 128 else ""


def describe_live_cameras(
    raw: object, inventory: object, channel_payload: object,
    selected_devices: Mapping[str, Mapping[str, Any]],
) -> list[LiveCameraDescription]:
    """Join Device.uid to CameraInfo.deviceID, as the Android app does.

    Names, SN similarity and the current inventory index are not identity keys.
    Ambiguous IDs and unsupported transports never produce camera entities.
    """
    rows = _devices(inventory)
    snapshot, _ = _nvr_snapshot(inventory)
    summaries = summarize_nvr_channels(channel_payload, snapshot)
    nvrs = {row["index"]: row for row in summaries.get("nvrs", [])}
    result = []
    for device_id, device in selected_devices.items():
        uid = device.get("uid")
        if not isinstance(uid, str) or not uid:
            continue
        matches = [(index, row) for index, row in rows if _vendor_id(row) == uid]
        if len(matches) != 1:
            continue
        index, row = matches[0]
        is_nvr = str(row.get("devTypeID")) == "11"
        channels = nvrs.get(index, {}).get("channels", []) if is_nvr else [{"channel_id": 0}]
        for channel in channels:
            channel_id = channel["channel_id"]
            if is_nvr and (channel.get("state") != 1 or channel.get("capability_codes", {}).get("p2p") != 2):
                continue
            try:
                build_probe_context(
                    raw, inventory, device_index=index, channel_id=channel_id,
                    channel_payload=channel_payload if is_nvr else None,
                )
            except (ValueError, TypeError, OverflowError):
                continue
            result.append(LiveCameraDescription(str(device_id), uid, channel_id, is_nvr))
    return result


async def _request_json(session: Any, method: str, request: tuple) -> Mapping[str, Any]:
    url, params, headers = request
    kwargs = {"params" if method == "get" else "data": params}
    try:
        async with getattr(session, method)(
            url, **kwargs, headers=headers, allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=20), raise_for_status=False,
        ) as response:
            if response.status != 200:
                raise ValueError("video_cloud_unavailable")
            body = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                body.extend(chunk)
                if len(body) > MAX_BODY_BYTES:
                    raise ValueError("video_cloud_response_limit")
            payload = json.loads(body)
            if not isinstance(payload, Mapping) or str(payload.get("resultCode")) != "1001":
                raise ValueError("video_cloud_unavailable")
            return payload
    except (aiohttp.ClientError, OSError, ValueError, RecursionError, TimeoutError):
        # Never let URLs, signed headers or server response text reach HA logs.
        raise ValueError("video_cloud_unavailable") from None


class LiveCameraBootstrap:
    """Serialize cmd148, refresh credentials, and resolve IDs again on playback."""

    def __init__(self, coordinator: Any, http_session: Any) -> None:
        self.coordinator = coordinator
        self.http_session = http_session
        self._lock = asyncio.Lock()

    async def _inventory(self) -> tuple[object, object]:
        client = self.coordinator.ssl_client
        if client is None or not client.connected:
            raise ValueError("video_cloud_connection_required")
        try:
            raw = await client.send_meari_session(country_code="CN", phone_code="86", language="zh")
        except (ConnectionError, OSError, ValueError, RuntimeError):
            raise ValueError("video_session_unavailable") from None
        if summarize_session_response(raw).get("stage") != "external_session_received":
            raise ValueError("video_session_unavailable")
        inventory = await _request_json(self.http_session, "post", build_inventory_request(raw))
        return raw, inventory

    async def _channels(self, raw: object, serials: list[str]) -> object:
        if not serials:
            return {"resultCode": 1001, "result": {}}
        identifier = json.dumps({sn: NVR_PROPERTIES for sn in serials}, separators=(",", ":"))
        return await _request_json(self.http_session, "get", build_vendor_request(
            raw, NVR_CHANNEL_PATH, {"snIdentifier": identifier},
        ))

    async def discover(self, devices: Mapping[str, Mapping[str, Any]]) -> list[LiveCameraDescription]:
        async with self._lock, asyncio.timeout(60):
            raw, inventory = await self._inventory()
            snapshot, _ = _nvr_snapshot(inventory)
            # Query only recorder IDs selected in this entry.
            wanted = {device.get("uid") for device in devices.values() if isinstance(device.get("uid"), str)}
            indexes = {index for index, row in _devices(inventory) if _vendor_id(row) in wanted}
            channels = await self._channels(raw, [sn for index, sn, _ in snapshot if index in indexes])
            return describe_live_cameras(raw, inventory, channels, devices)

    async def context(self, description: LiveCameraDescription) -> dict[str, Any]:
        async with self._lock, asyncio.timeout(60):
            raw, inventory = await self._inventory()
            matches = [(index, row) for index, row in _devices(inventory) if _vendor_id(row) == description.vendor_id]
            if len(matches) != 1:
                raise ValueError("video_device_unavailable")
            index, row = matches[0]
            if (str(row.get("devTypeID")) == "11") != description.is_nvr:
                raise ValueError("video_device_changed")
            channels = await self._channels(raw, [row.get("snNum")]) if description.is_nvr else None
            return build_probe_context(
                raw, inventory, device_index=index, channel_id=description.channel_id,
                channel_payload=channels, timeout_seconds=45,
            )
