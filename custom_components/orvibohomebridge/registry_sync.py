"""Keep Home Assistant registries aligned with the selected ORVIBO devices."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .selection import selected_device_ids


def _orvibo_device_id(device_entry: Any) -> str | None:
    """Return the ORVIBO identifier owned by this integration."""

    for domain, identifier in device_entry.identifiers:
        if domain == DOMAIN:
            return str(identifier)
    return None


def sync_selected_device_registries(
    hass: HomeAssistant,
    entry: ConfigEntry,
    devices: Mapping[str, Mapping[str, Any]],
) -> int:
    """Remove registry records for devices excluded by entry options."""

    selected = selected_device_ids(entry.options, devices)
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    entry_devices = dr.async_entries_for_config_entry(
        device_registry, entry.entry_id
    )
    entry_entities = er.async_entries_for_config_entry(
        entity_registry, entry.entry_id
    )
    entities_by_device: dict[str, list[str]] = {}
    for entity in entry_entities:
        if entity.device_id:
            entities_by_device.setdefault(entity.device_id, []).append(
                entity.entity_id
            )

    removed = 0
    for device_entry in entry_devices:
        device_id = _orvibo_device_id(device_entry)
        if device_id is None or device_id in selected:
            continue

        for entity_id in entities_by_device.get(device_entry.id, []):
            entity_registry.async_remove(entity_id)

        if len(device_entry.config_entries) > 1:
            device_registry.async_update_device(
                device_entry.id,
                remove_config_entry_id=entry.entry_id,
            )
        else:
            device_registry.async_remove_device(device_entry.id)
        removed += 1

    return removed
