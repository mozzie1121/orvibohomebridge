"""Build a private, bounded worker input from authenticated Meari responses.

This module performs no I/O and imports no Home Assistant code. Its return
value contains credentials and must only be passed to the worker over stdin;
it is deliberately separate from every public diagnostic response.
"""
from __future__ import annotations

from collections.abc import Mapping
import ipaddress
import json
import math
import re
from typing import Any
from urllib.parse import urlsplit

APP_VERSION = "5.2.6.307a13"
INVENTORY_GROUPS = (
    "doorbell", "snap", "voiceBell", "fourthGeneration", "light", "nvr",
    "ipc", "chime", "nvr-neutral", "base", "pictureDoorBell", "jingle",
    "cellular", "locator", "lock", "jingle-bridge", "waterFountain", "base-station",
)
NUMERIC_CAPS = (
    "p2p", "dpc", "gal", "idt", "idt2", "nct", "vtk", "bps", "adb", "vst",
    "mcps", "ppsb", "sme", "pbe", "pbv", "pbs2", "sdp", "venc", "aenc",
)
DEVICE_STRINGS = {
    "deviceID": 128, "hostKey1": 256, "deviceUUID": 256,
    "deviceSignature": 8192, "relayLicenseID": 63, "p2pInit": 512,
    "deviceP2P": 32, "region": 128, "timeZone": 128,
}


def _object(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str) and len(value) <= 2_000_000:
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            return {}
        if isinstance(value, Mapping):
            return value
    return {}


def _text(value: object, maximum: int, error: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value):
        raise ValueError(error)
    if any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise ValueError(error)
    return value


def _number(value: object, maximum: int = 65535) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= maximum:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,19}", value):
        number = int(value)
        return number if number <= maximum else None
    return None


def format_sn(serial: str) -> str:
    """SdkUtils.formatSn: 9 characters get seven zeroes, others lose four."""
    if not isinstance(serial, str):
        raise ValueError("serial_invalid")
    if not serial:
        return ""
    if len(serial) == 9:
        return "0000000" + serial
    if len(serial) < 4:
        raise ValueError("serial_invalid")
    return serial[4:]


def format_licence_id(serial: str) -> str:
    """SdkUtils.formatLicenceId: pad a nine-character ID to 20 characters."""
    if not isinstance(serial, str):
        raise ValueError("serial_invalid")
    return "00000000000" + serial if len(serial) == 9 else serial


def _endpoint(value: object, *, optional: bool = False, hostname: bool = False) -> str | None:
    if optional and value in (None, ""):
        return None
    value = _text(value, 2048, "endpoint_invalid")
    try:
        url = urlsplit(value if "://" in value else "https://" + value if hostname else value)
        host = url.hostname or ""
        if (
            url.scheme != "https" or url.username is not None or url.password is not None
            or url.port not in (None, 443) or url.query or url.fragment
            or url.path not in ("", "/", "/ppstrongs", "/ppstrongs/")
            or "." not in host or not re.fullmatch(r"[A-Za-z0-9.-]+", host)
            or host.endswith((".local", ".localhost", ".internal"))
        ):
            raise ValueError("endpoint_invalid")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError("endpoint_invalid")
    except (ValueError, OverflowError):
        raise ValueError("endpoint_invalid") from None
    return value.rstrip("/")


def _capability(value: object) -> dict[str, Any]:
    value = _object(value)
    caps = _object(value.get("caps"))
    result: dict[str, Any] = {"caps": {
        key: number for key in NUMERIC_CAPS if (number := _number(caps.get(key), 0xFFFFFFFF)) is not None
    }}
    version = _number(value.get("ver"))
    if version is not None:
        result["ver"] = version
    # Preserve only native quality profile maps. Arbitrary capability members,
    # camera names, coordinates, and unrelated SDK settings stay in the parent.
    bps2 = _object(caps.get("bps2"))
    profiles = {}
    for key, profile in list(bps2.items())[:128]:
        profile_id = _number(key)
        if profile_id is not None and isinstance(profile, str) and len(profile) <= 64:
            profiles[str(profile_id)] = profile
    if profiles:
        result["caps"]["bps2"] = profiles
    return result


def _selected_device(payload: object, index: int) -> tuple[Mapping[str, Any], str]:
    raw = _object(payload)
    if _number(raw.get("resultCode")) != 1001:
        raise ValueError("inventory_invalid")
    root = raw if any(isinstance(raw.get(group), list) for group in INVENTORY_GROUPS) else _object(raw.get("result"))
    if not any(isinstance(root.get(group), list) for group in INVENTORY_GROUPS):
        raise ValueError("inventory_invalid")
    seen = 0
    for group in INVENTORY_GROUPS:
        devices = root.get(group)
        if not isinstance(devices, list):
            continue
        for device in devices:
            if not isinstance(device, Mapping):
                continue
            seen += 1
            if seen == index:
                return device, group
    raise ValueError("device_not_found")


def _selected_channel(payload: object, serial: str, channel_id: int) -> Mapping[str, Any]:
    raw = _object(payload)
    if _number(raw.get("resultCode")) != 1001:
        raise ValueError("channel_response_invalid")
    properties = _object(_object(raw.get("result")).get(serial))
    state = _object(properties.get("1015")).get("state")
    if not isinstance(state, list):
        raise ValueError("channel_response_invalid")
    matching = [row for row in state if isinstance(row, Mapping) and _number(row.get("channel")) == channel_id]
    if len(matching) != 1:
        raise ValueError("channel_not_found")
    selected_state = _number(matching[0].get("state"))
    if selected_state is None:
        raise ValueError("channel_response_invalid")
    if selected_state == 0:
        raise ValueError("channel_offline")
    raw_caps = properties.get("67")
    if isinstance(raw_caps, str) and len(raw_caps) <= 512_000:
        try:
            raw_caps = json.loads(raw_caps)
        except (ValueError, RecursionError):
            raw_caps = None
    if isinstance(raw_caps, list):
        for raw_capability in raw_caps:
            capability = _object(raw_capability)
            if _number(capability.get("channel")) == channel_id:
                return capability
    return {}


def build_probe_context(
    raw: object, inventory_payload: object, *, device_index: int, channel_id: int,
    timeout_seconds: float = 35, channel_payload: object = None,
    country_code: str = "CN",
) -> dict[str, Any]:
    """Select exactly one explicitly requested NVR and channel, without I/O."""
    if isinstance(device_index, bool) or not isinstance(device_index, int) or not 1 <= device_index <= 512:
        raise ValueError("device_index_invalid")
    if isinstance(channel_id, bool) or not isinstance(channel_id, int) or not 0 <= channel_id <= 128:
        raise ValueError("channel_invalid")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or not 10 <= timeout_seconds <= 60:
        raise ValueError("timeout_invalid")
    raw = _object(raw)
    if isinstance(raw.get("status"), bool) or raw.get("status") != 0 or str(raw.get("thirdAppType", "")) != "1":
        raise ValueError("session_invalid")
    config = _object(_object(raw.get("miruiConfigResult")).get("result"))
    account_raw = _object(_object(raw.get("miruiTokenResult")).get("result"))
    user_id = account_raw.get("userID")
    brand = config.get("partnerId")
    if isinstance(user_id, bool) or not isinstance(user_id, (str, int)) or not re.fullmatch(r"[1-9][0-9]{0,19}", str(user_id)):
        raise ValueError("account_invalid")
    if isinstance(brand, bool) or not isinstance(brand, (str, int)) or not re.fullmatch(r"[0-9]{1,10}", str(brand)):
        raise ValueError("account_invalid")
    # Reuse the region used for the authenticated cmd148 request if omitted.
    country = account_raw.get("countryCode") or country_code
    if not isinstance(country, str) or not re.fullmatch(r"[A-Za-z]{2}", country):
        raise ValueError("account_invalid")
    phone_code = account_raw.get("phoneCode")
    if phone_code is not None and (not isinstance(phone_code, str) or not re.fullmatch(r"[0-9]{1,4}", phone_code)):
        raise ValueError("account_invalid")
    pf_api = _object(config.get("pfApi"))
    platform = _object(pf_api.get("platform"))
    client_id = account_raw.get("clientId") or account_raw.get("clientID")
    account = {
        "user_id": str(user_id), "user_token": _text(account_raw.get("userToken"), 8192, "account_invalid"),
        "country_code": country.upper(), "phone_code": phone_code, "brand": str(brand),
        "api_server": _endpoint(config.get("apiServer")),
        "openapi_server": _endpoint(_object(pf_api.get("openapi")).get("domain")),
        "platform_domain": _endpoint(platform.get("domain"), optional=True, hostname=True),
        "platform_signature": _text(platform["signature"], 8192, "account_invalid", empty=True) if "signature" in platform else None,
        "client_id": _text(client_id, 256, "account_invalid") if client_id is not None else None,
        "app_version": APP_VERSION,
    }
    source, category = _selected_device(inventory_payload, device_index)
    device_type = _number(source.get("devTypeID"))
    if _number(source.get("iotType")) != 3 or device_type not in (2, 11):
        raise ValueError("unsupported_device")
    if device_type == 2 and channel_id != 0:
        raise ValueError("channel_invalid")
    # The App normally stores this state locally. Missing fields do not prove
    # that no video password exists; the probe attempts hostKey mode once.
    if source.get("isVideoSetPwd") not in (None, 0, "0", False, "") or source.get("videoPwd") not in (None, ""):
        raise ValueError("video_password_required")
    serial = _text(source.get("snNum"), 63, "serial_invalid")
    if not format_sn(serial):
        raise ValueError("serial_invalid")
    device: dict[str, Any] = {
        "snNum": serial, "hostKey": _text(source.get("hostKey"), 63, "host_key_invalid"),
        "iotType": 3, "devTypeID": device_type, "_category": category, "nvrChannelId": channel_id,
    }
    for key, maximum in DEVICE_STRINGS.items():
        if key in source and source[key] is not None:
            value = source[key]
            if key == "deviceID" and isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 0x7FFFFFFFFFFFFFFF:
                value = str(value)
            device[key] = _text(value, maximum, "device_data_invalid", empty=True)
    parent_capability = _capability(source.get("capability"))
    capability = parent_capability
    if channel_payload is not None:
        selected = _selected_channel(channel_payload, serial, channel_id)
        if selected:
            capability = _capability(selected)
            for key in ("dpc", "gal", "idt", "idt2", "nct"):
                if key in parent_capability["caps"]:
                    capability["caps"][key] = parent_capability["caps"][key]
                else:
                    capability["caps"].pop(key, None)
    if capability["caps"].get("p2p") != 2:
        raise ValueError("unsupported_transport")
    device["capability"] = capability
    for key in ("t", "protocolVersion"):
        if key in source:
            number = _number(source[key], 0x7FFFFFFFFFFFFFFF)
            if number is None:
                raise ValueError("device_data_invalid")
            device[key] = number
    return {"account": account, "device": device, "channel_id": channel_id,
            "timeout_seconds": float(timeout_seconds),
            "vvp_auth": {"full_password": False, "include_licence": True}}
