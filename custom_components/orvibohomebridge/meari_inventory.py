"""Read-only vendor inventory probe based on the HomeMate Meari SDK.

No device connections, MQTT, media, or persistence. Returned data is an allowlist
of counters and presence flags; credentials and server bodies stay in memory.
"""
from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import time
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from .meari_session import _object, _present

DEVICE_LIST_PATH = "/v1/app/device/info/get"
MAX_BODY_BYTES = 2_000_000
MAX_DEVICES = 512
GROUPS = (
    "doorbell", "snap", "voiceBell", "fourthGeneration", "light", "nvr",
    "ipc", "chime", "nvr-neutral", "base", "pictureDoorBell", "jingle",
    "cellular", "locator", "lock", "jingle-bridge", "waterFountain", "base-station",
)


def _hmac_sha1(text: str, key: str) -> str:
    return base64.b64encode(hmac.new(key.encode(), text.encode(), hashlib.sha1).digest()).decode("ascii")


def _api_url(value: object, path: str = DEVICE_LIST_PATH) -> str:
    """Accept a clean HTTPS base from the authenticated Orvibo response only."""
    if not isinstance(value, str) or len(value) > 2048 or any(c.isspace() for c in value):
        raise ValueError("invalid_endpoint")
    url = urlsplit(value)
    host = url.hostname or ""
    if (
        url.scheme != "https" or url.username is not None or url.password is not None
        or url.query or url.fragment or url.port not in (None, 443)
        or not re.fullmatch(r"[A-Za-z0-9.-]+", host)
        or "." not in host or host.endswith((".local", ".localhost", ".internal"))
        or url.path not in ("", "/", "/ppstrongs", "/ppstrongs/")
    ):
        raise ValueError("invalid_endpoint")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("ip_endpoint")
    if path not in (DEVICE_LIST_PATH, "/v1/app/nvr/iot/model/get/batch"):
        raise ValueError("unsupported_api_path")
    return value.rstrip("/") + path


def build_vendor_request(
    raw: object, path: str, extra_params: Mapping[str, str], *,
    country_code: str = "CN", phone_code: str = "86", language: str = "zh",
) -> tuple[str, dict[str, str], dict[str, str]]:
    """Match i.e(5).b(), l$S2o.subscribe and SdkUtils.getOKHttpHeader."""
    raw = _object(raw)
    config = _object(_object(raw.get("miruiConfigResult")).get("result"))
    account = _object(_object(raw.get("miruiTokenResult")).get("result"))
    token = account.get("userToken")
    uid = str(account.get("userID", ""))
    partner = str(config.get("partnerId", ""))
    if (
        not isinstance(token, str) or not token.strip() or len(token) > 8192
        or any(ord(c) < 33 or ord(c) > 126 for c in token)
        or not re.fullmatch(r"[1-9][0-9]{0,19}", uid)
        or not re.fullmatch(r"[0-9]{1,10}", partner)
    ):
        raise ValueError("invalid_credentials")
    country = account.get("countryCode") or country_code
    phone = account.get("phoneCode") or phone_code
    if (
        not isinstance(country, str) or not re.fullmatch(r"[A-Za-z]{2}", country)
        or not isinstance(phone, str) or not re.fullmatch(r"[0-9]{1,4}", phone)
        or language not in ("zh", "en")
    ):
        raise ValueError("invalid_region")
    url = _api_url(config.get("apiServer"), path)
    now_ms = int(time.time() * 1000)
    timestamp = datetime.fromtimestamp(now_ms / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    params = {
        "phoneType": "a", "appVer": "5.2.6.307", "appVerCode": "50206307",
        "lngType": language, "t": str(now_ms), "sourceApp": partner,
        "userID": uid, "countryCode": country, "phoneCode": phone,
        "signatureMethod": "HMAC-SHA1", "signatureVersion": "1.0",
        "timestamp": timestamp, "signatureNonce": str(now_ms),
    }
    expected_keys = {"funSwitch"} if path == DEVICE_LIST_PATH else {"snIdentifier"}
    if set(extra_params) != expected_keys or any(
        not isinstance(key, str) or key in params or key == "signature"
        or not isinstance(value, str) or len(value) > 16384
        for key, value in extra_params.items()
    ):
        raise ValueError("invalid_api_parameters")
    params.update(extra_params)
    params["signature"] = _hmac_sha1("&".join(f"{k}={params[k]}" for k in sorted(params)), token)
    nonce = str(secrets.randbelow(899999) + 100000)
    headers = {
        "X-Ca-Key": token, "X-Ca-Timestamp": str(now_ms), "X-Ca-Nonce": nonce,
        "X-Ca-Sign": _hmac_sha1(
            f"api=/ppstrongs/{path}|X-Ca-Key={token}|X-Ca-Timestamp={now_ms}|X-Ca-Nonce={nonce}", token,
        ),
    }
    return url, params, headers


def build_inventory_request(
    raw: object, *, country_code: str = "CN", phone_code: str = "86", language: str = "zh",
) -> tuple[str, dict[str, str], dict[str, str]]:
    """Build the existing inventory request without changing its wire shape."""
    return build_vendor_request(
        raw, DEVICE_LIST_PATH, {"funSwitch": "1"}, country_code=country_code,
        phone_code=phone_code, language=language,
    )


def _code(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 999999:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,6}", value):
        return int(value)
    return None


def summarize_inventory(payload: object) -> dict[str, Any]:
    """Summarize only fixed SDK groups; never echo arbitrary server strings."""
    raw = _object(payload)
    code = _code(raw.get("resultCode"))
    public: dict[str, Any] = {"vendor_stage": "invalid_device_response", "channel_query_implemented": False}
    if code is None:
        return public
    public["vendor_result_code"] = code
    if code != 1001:
        return {**public, "vendor_stage": "vendor_request_rejected"}
    # ResponseData.getJsonResult() passes the entire JSON to getMeariDevice.
    # Some API generations wrap groups in result; mark that separately.
    sdk_root = any(isinstance(raw.get(group), list) for group in GROUPS)
    result = raw if sdk_root else _object(raw.get("result"))
    recognized = [group for group in GROUPS if isinstance(result.get(group), list)]
    if not recognized:
        return {**public, "vendor_stage": "unrecognized_inventory_shape"}
    rows = []
    total = 0
    counts = {}
    for group in recognized:
        devices = result[group]
        counts[group] = sum(isinstance(item, Mapping) for item in devices)
        for device in devices:
            if not isinstance(device, Mapping):
                continue
            total += 1
            if len(rows) >= MAX_DEVICES:
                continue
            caps = _object(_object(device.get("capability")).get("caps"))
            transport = device.get("deviceP2P")
            # These are raw SDK capability codes, not verified playback support.
            row: dict[str, Any] = {
                "index": len(rows) + 1, "category": group,
                "device_id_present": _present(device.get("deviceID")),
                "serial_present": _present(device.get("snNum")),
                "uuid_present": _present(device.get("deviceUUID")),
                "host_key_present": _present(device.get("hostKey")),
                "device_signature_present": _present(device.get("deviceSignature")),
                "iot_thing_present": _present(device.get("awsThingName")),
                "capability_present": bool(caps),
                "transport": transport if isinstance(transport, str) and transport in ("ppcs", "meari", "p2p") else "other_or_unspecified",
            }
            for field in ("iotType", "devTypeID"):
                value = _code(device.get(field))
                if value is not None:
                    row[field] = value
            row["capability_codes"] = {key: code for key in ("p2p", "nct", "dpc", "idt", "idt2", "vtk") if (code := _code(caps.get(key))) is not None}
            rows.append(row)
    return {
        **public, "vendor_stage": "vendor_devices_received", "device_count": total,
        "inventory_shape": "sdk_root" if sdk_root else "result_wrapper",
        "category_counts": counts, "devices": rows, "devices_truncated": total > len(rows),
    }


async def async_inspect_device_inventory(
    session: aiohttp.ClientSession, raw: object, *, country_code: str = "CN",
    phone_code: str = "86", language: str = "zh", include_channels: bool = False,
    probe_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        url, params, headers = build_inventory_request(raw, country_code=country_code, phone_code=phone_code, language=language)
    except (ValueError, OverflowError):
        return {"vendor_stage": "inventory_request_data_invalid", "channel_query_implemented": False}
    try:
        async with session.post(
            url, data=params, headers=headers, allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=20), raise_for_status=False,
        ) as response:
            status = response.status
            if status != 200:
                return {"vendor_stage": "vendor_http_error", "vendor_http_status": status, "channel_query_implemented": False}
            body = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                body.extend(chunk)
                if len(body) > MAX_BODY_BYTES:
                    return {"vendor_stage": "vendor_response_too_large", "channel_query_implemented": False}
            payload = json.loads(body)
            public = {**summarize_inventory(payload), "vendor_http_status": status}
            if include_channels and public["vendor_stage"] == "vendor_devices_received":
                from .meari_channels import async_inspect_nvr_channels

                public.update(await async_inspect_nvr_channels(
                    session, raw, payload, country_code=country_code,
                    phone_code=phone_code, language=language,
                ))
        # Release the HTTP response before the longer media probe starts.
        if probe_options is not None and public["vendor_stage"] == "vendor_devices_received":
            from .meari_probe import async_probe_inventory

            public.update(await async_probe_inventory(raw, payload, **probe_options))
        return public
    except (aiohttp.ClientError, OSError, ValueError, RecursionError, asyncio.TimeoutError):
        # Do not include exception messages: they can contain credentials/URLs.
        return {"vendor_stage": "vendor_request_failed", "channel_query_implemented": False}
