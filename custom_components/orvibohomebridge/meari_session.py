"""External Meari session inspection; this does not claim video support.

The request matches HomeMate 5.2.6's model/meari/a (cmd=148). The
response is used only in memory and the public result is an explicit allowlist.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from .const import DEBUG_INFO, SOFTWARE_VER
from .functions import generate_serial

CMD_MEARI_SESSION = 148


def build_session_request(
    family_id: str, *, country_code: str = "CN", phone_code: str = "86",
    language: str = "zh",
) -> dict[str, Any]:
    if not family_id:
        raise ValueError("family_required")
    return {
        "cmd": CMD_MEARI_SESSION,
        "familyId": family_id,
        "thirdAppType": "1",
        "clientType": 1,
        "countryCode": country_code,
        "phoneCode": phone_code,
        "iotType": "4",
        "lngType": language,
        "serial": generate_serial(),
        "uniSerial": generate_serial(use_time=True),
        "serverRecord": False,
        "ver": SOFTWARE_VER,
        "debugInfo": DEBUG_INFO,
    }


def _object(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str) and len(value) <= 512_000:
        try:
            parsed = json.loads(value)
        except (ValueError, RecursionError):
            return {}
        if isinstance(parsed, Mapping):
            return parsed
    return {}


def _present(value: object) -> bool:
    return isinstance(value, (str, int)) and not isinstance(value, bool) and bool(str(value).strip())


def _endpoint_present(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        url = urlsplit(value)
        return bool(url.hostname) and url.scheme in ("https", "http") and not (url.username or url.password)
    except ValueError:
        return False


def summarize_session_response(response: object) -> dict[str, Any]:
    """Return presence/count facts, never tokens, account data or raw JSON."""
    public: dict[str, Any] = {
        "video_playback_implemented": True,
        "real_device_video_verified": False,
    }
    raw = _object(response)
    if not raw:
        return {**public, "stage": "no_response"}
    status = raw.get("status")
    if not isinstance(status, int) or isinstance(status, bool):
        return {**public, "stage": "invalid_status"}
    public["orvibo_status"] = status
    if status != 0:
        return {**public, "stage": "session_rejected"}
    if str(raw.get("thirdAppType", "")) != "1":
        return {**public, "stage": "unexpected_vendor"}
    config = _object(raw.get("miruiConfigResult"))
    token = _object(raw.get("miruiTokenResult"))
    config_result = _object(config.get("result"))
    token_result = _object(token.get("result"))
    pf_api = _object(config_result.get("pfApi"))
    iot = _object(token_result.get("iot"))
    public.update({
        "config_result_present": bool(config_result),
        "token_result_present": bool(token_result),
        "api_endpoint_present": _endpoint_present(config_result.get("apiServer")),
        "partner_id_present": _present(config_result.get("partnerId")),
        "user_id_present": _present(token_result.get("userID")) and str(token_result.get("userID")) != "0",
        "user_token_present": isinstance(token_result.get("userToken"), str) and bool(token_result["userToken"].strip()),
        "client_id_present": _present(token_result.get("clientId")) or _present(token_result.get("clientID")),
        "iot_config_present": bool(iot),
        "openapi_endpoint_present": _endpoint_present(_object(pf_api.get("openapi")).get("domain")),
        "platform_endpoint_present": _endpoint_present(_object(pf_api.get("platform")).get("domain")),
    })
    ready = all(public[k] for k in ("api_endpoint_present", "user_id_present", "user_token_present"))
    public["stage"] = "external_session_received" if ready else "external_session_incomplete"
    return public


async def async_inspect_video_session(
    coordinator: Any, *, country_code: str = "CN", phone_code: str = "86",
    language: str = "zh", http_session: Any = None, include_channels: bool = False,
    probe_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    client = coordinator.ssl_client
    if client is None or not client.connected:
        return {**summarize_session_response(None), "stage": "cloud_connection_required"}
    try:
        response = await client.send_meari_session(
            country_code=country_code, phone_code=phone_code, language=language,
        )
    except RuntimeError:
        return {**summarize_session_response(None), "stage": "request_in_progress"}
    except (ConnectionError, OSError, ValueError):
        return {**summarize_session_response(None), "stage": "session_request_failed"}
    public = summarize_session_response(response)
    if http_session is not None and public["stage"] == "external_session_received":
        from .meari_inventory import async_inspect_device_inventory

        public.update(await async_inspect_device_inventory(
            http_session, response, country_code=country_code,
            phone_code=phone_code, language=language, include_channels=include_channels,
            **({"probe_options": probe_options} if probe_options is not None else {}),
        ))
    return public
