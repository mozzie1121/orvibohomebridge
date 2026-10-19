"""Static camera SDK hints from HomeMate 5.2.6.307; no network or playback.

CameraInfo.type is separate from Device.deviceType and catalogue productType.
The native application also has model overrides; these hints do not validate a
device's firmware, credentials, stream endpoint, or runtime SDK route.
"""

from __future__ import annotations

from typing import Any

from .device_types import DeviceCategory, classify_device

CAMERA_CATEGORIES = frozenset({DeviceCategory.WIFI_CAMERA, DeviceCategory.CAMERA_NVR})
CAMERA_SDK_OPTIONS = ("unknown", "danale", "meari", "ezviz")


def camera_sdk_hint(device: dict[str, Any]) -> str:
    """Describe only confirmed type-based branches, without guessing models."""
    category = classify_device(device)
    if category == DeviceCategory.CAMERA_NVR:
        return "meari"
    if category != DeviceCategory.WIFI_CAMERA:
        return "unknown"
    camera_type = device.get("camera_type")
    if not isinstance(camera_type, int) or isinstance(camera_type, bool):
        return "unknown"
    if 3 <= camera_type <= 7:
        return "danale"
    if 8 <= camera_type <= 10:
        return "meari"
    if camera_type in (-1, 1, 2):
        return "ezviz"
    # Missing/other types may use SEP2P or model-specific overrides in the app.
    return "unknown"


def camera_diagnostics(device: dict[str, Any]) -> dict[str, Any]:
    """Return a small public allowlist, without camera credentials or IDs."""
    category = classify_device(device)
    if category not in CAMERA_CATEGORIES:
        return {}
    camera_type = device.get("camera_type")
    if not isinstance(camera_type, int) or isinstance(camera_type, bool):
        camera_type = None
    return {
        "camera_type": camera_type,
        "camera_sdk_hint": camera_sdk_hint(device),
        "sdk_hint_source": (
            "device_type" if category == DeviceCategory.CAMERA_NVR
            else "camera_info_type" if camera_type is not None else "unknown"
        ),
        "stream_supported": False,
        "hardware_verified": False,
    }
