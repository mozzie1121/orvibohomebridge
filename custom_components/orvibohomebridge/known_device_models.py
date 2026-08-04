"""Exact model identities generated from the official ORVIBO device catalog."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class KnownDeviceModel:
    names: tuple[str, ...]
    internal_models: tuple[str, ...]

    @property
    def display_name(self) -> str:
        if self.names:
            return self.names[0]
        if self.internal_models:
            return self.internal_models[0]
        return "官方目录已登记设备"


def _load_official_catalog() -> dict[str, KnownDeviceModel]:
    path = Path(__file__).with_name("known_device_catalog.json")
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported known-device catalogue schema")
    raw_models = payload.get("models")
    if not isinstance(raw_models, dict):
        raise ValueError("known-device catalogue does not contain models")
    models: dict[str, KnownDeviceModel] = {}
    for model, value in raw_models.items():
        if not isinstance(model, str) or not isinstance(value, dict):
            continue
        models[model] = KnownDeviceModel(
            names=tuple(str(item) for item in value.get("names", [])),
            internal_models=tuple(
                str(item) for item in value.get("internal_models", [])
            ),
        )
    expected = payload.get("model_count")
    if expected != len(models):
        raise ValueError(
            f"known-device catalogue model count mismatch: {len(models)} != {expected}"
        )
    return models


# Full official catalogue.  A hit means identity only; device_types.py grants
# platform/control capabilities independently from verified protocol profiles.
KNOWN_DEVICE_MODELS = _load_official_catalog()


def identify_known_device(device: Mapping[str, Any]) -> Optional[KnownDeviceModel]:
    """Return official metadata only for an exact model match."""
    model = device.get("model")
    if not isinstance(model, str):
        return None
    return KNOWN_DEVICE_MODELS.get(model.strip())
