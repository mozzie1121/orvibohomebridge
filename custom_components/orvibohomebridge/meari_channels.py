"""Read-only NVR channel inventory from the HomeMate SDK batch API.

The batch endpoint is HTTPS metadata retrieval. It does not establish the
native/P2P connection required for live media and hard disk playback.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
import json
import re
from typing import Any

import aiohttp

from .meari_inventory import GROUPS, MAX_BODY_BYTES, _code, build_vendor_request
from .meari_session import _object

NVR_CHANNEL_PATH = "/v1/app/nvr/iot/model/get/batch"
NVR_PROPERTIES = "213,1015,67"
MAX_NVRS = 32
MAX_CHANNELS = 128
CAPABILITY_CODES = ("p2p", "nct", "dpc", "idt", "idt2", "vtk")
Snapshot = list[tuple[int, str, dict[str, int]]]


def _integer(value: object, *, minimum: int = 0) -> int | None:
    number = _code(value)
    return number if number is not None and minimum <= number <= 65535 else None


def _nvr_snapshot(payload: object) -> tuple[Snapshot, dict[str, Any]]:
    """Keep serials in memory; indexes match the public inventory ordering."""
    raw = _object(payload)
    if _code(raw.get("resultCode")) != 1001:
        raise ValueError("invalid_inventory")
    root = raw if any(isinstance(raw.get(group), list) for group in GROUPS) else _object(raw.get("result"))
    if not any(isinstance(root.get(group), list) for group in GROUPS):
        raise ValueError("invalid_inventory")
    selected: Snapshot = []
    seen: set[str] = set()
    index = total = missing = 0
    truncated = False
    for group in GROUPS:
        devices = root.get(group)
        if not isinstance(devices, list):
            continue
        for device in devices:
            if not isinstance(device, Mapping):
                continue
            index += 1
            # MeariDeviceUtil.isNvrOrBase(int), SDK devTypeID values.
            if _code(device.get("devTypeID")) not in (11, 17, 21):
                continue
            total += 1
            serial = device.get("snNum")
            if not isinstance(serial, str) or not re.fullmatch(r"[!-~]{1,256}", serial):
                missing += 1
                continue
            if serial in seen:
                continue
            seen.add(serial)
            if len(selected) >= MAX_NVRS:
                truncated = True
                continue
            caps = _object(_object(device.get("capability")).get("caps"))
            parent_codes = {key: number for key in CAPABILITY_CODES if (number := _integer(caps.get(key))) is not None}
            selected.append((index, serial, parent_codes))
    return selected, {
        "nvr_count": total, "queried_nvr_count": len(selected),
        "nvrs_truncated": truncated, "nvr_missing_serial_count": missing,
    }


def _channel_capabilities(value: object) -> tuple[dict[int, dict[str, int]], str, bool]:
    if value is None:
        return {}, "missing", False
    if isinstance(value, str) and len(value) <= 512_000:
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            return {}, "invalid", False
    if not isinstance(value, list):
        return {}, "invalid", False
    result = {}
    truncated = False
    # JsonUtil.getCaps + dealNvrChannel: each item is a JSON string with
    # channel and caps. Accept a decoded object as well, without echoing it.
    for item in value:
        item = _object(item)
        channel_id = _integer(item.get("channel"))
        caps = _object(item.get("caps"))
        if channel_id is None or not caps:
            continue
        if channel_id not in result and len(result) >= MAX_CHANNELS:
            truncated = True
            continue
        result[channel_id] = {key: number for key in CAPABILITY_CODES if (number := _integer(caps.get(key))) is not None}
    return result, "received", truncated


def _channel_summary(index: int, value: object, parent_codes: dict[str, int]) -> dict[str, Any]:
    row: dict[str, Any] = {"index": index}
    if not isinstance(value, Mapping):
        return {**row, "channel_stage": "nvr_result_missing"}
    capabilities, capability_stage, capabilities_truncated = _channel_capabilities(value.get("67"))
    row["channel_capabilities_stage"] = capability_stage
    row["channel_capability_count"] = len(capabilities)
    row["channel_capabilities_truncated"] = capabilities_truncated
    maximum = _integer(value.get("213"))
    if maximum is not None:
        row["maximum_channel_count"] = maximum
    if "1015" not in value:
        return {**row, "channel_stage": "channel_state_missing"}
    state = _object(value.get("1015"))
    channels = state.get("state")
    if not isinstance(channels, list):
        return {**row, "channel_stage": "invalid_channel_state"}
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    invalid = 0
    for channel in channels:
        if not isinstance(channel, Mapping) or (channel_id := _integer(channel.get("channel"))) is None:
            invalid += 1
            continue
        if channel_id in seen:
            invalid += 1
            continue
        seen.add(channel_id)
        if len(rows) >= MAX_CHANNELS:
            continue
        public: dict[str, Any] = {"channel_id": channel_id}
        codes = dict(capabilities.get(channel_id, parent_codes))
        # dealNvrChannel explicitly restores these recorder-level codes after
        # parsing per-channel capabilities; do not replace missing values by 0.
        for key in ("dpc", "idt", "idt2", "nct"):
            if key in parent_codes:
                codes[key] = parent_codes[key]
            else:
                codes.pop(key, None)
        if codes:
            public["capability_codes"] = codes
        for key in ("state", "type"):
            number = _integer(channel.get(key))
            if number is not None:
                public[key] = number
        rows.append(public)
    return {
        **row, "channel_stage": "channel_state_received",
        "channel_count": len(seen), "channels": rows,
        "channels_truncated": len(seen) > len(rows), "invalid_channel_rows": invalid,
    }


def summarize_nvr_channels(payload: object, snapshot: Snapshot) -> dict[str, Any]:
    """Allowlist only counts and bounded numeric channel attributes."""
    raw = _object(payload)
    code = _code(raw.get("resultCode"))
    public: dict[str, Any] = {"channel_stage": "invalid_channel_response", "channel_query_implemented": True}
    if code is None:
        return public
    public["channel_result_code"] = code
    if code != 1001:
        return {**public, "channel_stage": "channel_request_rejected"}
    result = raw.get("result")
    if not isinstance(result, Mapping):
        return public
    return {
        **public, "channel_stage": "nvr_channels_received",
        "nvrs": [_channel_summary(index, result.get(serial), parent_codes) for index, serial, parent_codes in snapshot],
    }


async def async_inspect_nvr_channels(
    session: aiohttp.ClientSession, raw: object, inventory_payload: object, *,
    country_code: str = "CN", phone_code: str = "86", language: str = "zh",
) -> dict[str, Any]:
    public: dict[str, Any] = {"channel_query_implemented": True}
    try:
        snapshot, counts = _nvr_snapshot(inventory_payload)
        public.update(counts)
        if not snapshot:
            stage = "no_nvr_devices" if counts["nvr_count"] == 0 else "nvr_serial_required"
            return {**public, "channel_stage": stage}
        # _1IB.m947Z51OSo: raw SN keys and comma-separated fixed property IDs.
        identifier = json.dumps({serial: NVR_PROPERTIES for _, serial, _ in snapshot}, separators=(",", ":"))
        url, params, headers = build_vendor_request(
            raw, NVR_CHANNEL_PATH, {"snIdentifier": identifier},
            country_code=country_code, phone_code=phone_code, language=language,
        )
    except (ValueError, OverflowError, RecursionError):
        return {**public, "channel_stage": "channel_request_data_invalid"}
    try:
        async with session.get(
            url, params=params, headers=headers, allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=20), raise_for_status=False,
        ) as response:
            public["channel_http_status"] = response.status
            if response.status != 200:
                return {**public, "channel_stage": "channel_http_error"}
            body = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                body.extend(chunk)
                if len(body) > MAX_BODY_BYTES:
                    return {**public, "channel_stage": "channel_response_too_large"}
            return {**public, **summarize_nvr_channels(json.loads(body), snapshot)}
    except (aiohttp.ClientError, OSError, ValueError, RecursionError, asyncio.TimeoutError):
        # Exception text and response bodies may contain credentials and serials.
        return {**public, "channel_stage": "channel_request_failed"}
