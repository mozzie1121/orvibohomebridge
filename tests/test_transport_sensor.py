"""Tests for the per-device transport diagnostic entity."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


MODULE_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "orvibohomebridge"
    / "sensor.py"
)


def _module(name: str, **values) -> ModuleType:
    module = ModuleType(name)
    for key, value in values.items():
        setattr(module, key, value)
    return module


def _load_sensor_module():
    package_name = "orvibohomebridge_transport_sensor_test"
    package = _module(package_name)
    package.__path__ = [str(MODULE_PATH.parent)]

    class CoordinatorEntity:
        def __init__(self, coordinator):
            self.coordinator = coordinator

    class SensorEntity:
        pass

    modules = {
        package_name: package,
        "homeassistant": _module("homeassistant"),
        "homeassistant.components": _module("homeassistant.components"),
        "homeassistant.components.sensor": _module(
            "homeassistant.components.sensor",
            SensorEntity=SensorEntity,
            SensorDeviceClass=SimpleNamespace(
                BATTERY="battery",
                TEMPERATURE="temperature",
                HUMIDITY="humidity",
                ENUM="enum",
            ),
            SensorStateClass=SimpleNamespace(MEASUREMENT="measurement"),
        ),
        "homeassistant.config_entries": _module(
            "homeassistant.config_entries", ConfigEntry=object
        ),
        "homeassistant.const": _module(
            "homeassistant.const",
            EntityCategory=SimpleNamespace(DIAGNOSTIC="diagnostic"),
        ),
        "homeassistant.core": _module(
            "homeassistant.core", HomeAssistant=object
        ),
        "homeassistant.helpers": _module("homeassistant.helpers"),
        "homeassistant.helpers.entity_platform": _module(
            "homeassistant.helpers.entity_platform",
            AddEntitiesCallback=object,
        ),
        "homeassistant.helpers.update_coordinator": _module(
            "homeassistant.helpers.update_coordinator",
            CoordinatorEntity=CoordinatorEntity,
        ),
        f"{package_name}.const": _module(
            f"{package_name}.const",
            DOMAIN="orvibohomebridge",
            MANUFACTURER="ORVIBO",
            DEVICE_TYPE_SENSOR="sensor",
        ),
        f"{package_name}.coordinator": _module(
            f"{package_name}.coordinator",
            OrviboMeshCoordinator=object,
        ),
        f"{package_name}.selection": _module(
            f"{package_name}.selection",
            selected_device_ids=lambda _options, devices: set(devices),
        ),
    }

    module_name = f"{package_name}.sensor"
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    sensor = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        sys.modules[module_name] = sensor
        spec.loader.exec_module(sensor)
    return sensor


class _Control:
    def last_transport(self, device_id: str) -> str | None:
        return "lan" if device_id == "light-1" else None


class _Coordinator:
    def __init__(self, module):
        mode_type = module.transport_path_for.__globals__["TransportMode"]
        self.mode_type = mode_type
        self.transport_mode = mode_type.AUTO
        self.control = _Control()
        self.devices = {
            "light-1": {
                "device_id": "light-1",
                "device_name": "Light",
                "device_type": "light",
                "device_type_raw": 38,
                "uid": "gateway-1",
            },
            "clothes-1": {
                "device_id": "clothes-1",
                "device_name": "Clothes horse",
                "device_type": "sensor",
                "device_type_raw": 52,
                "uid": "wifi-device",
            },
        }

    def lan_gateway_connected(self, device_id: str) -> bool:
        return device_id == "light-1"

    def get_device_state(self, _device_id: str) -> dict:
        return {"online": True}


class TransportSensorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_sensor_module()

    def test_setup_adds_one_transport_marker_per_selected_device(self):
        coordinator = _Coordinator(self.module)
        hass = SimpleNamespace(
            data={
                "orvibohomebridge": {
                    "entry-1": coordinator,
                }
            }
        )
        entry = SimpleNamespace(entry_id="entry-1", options={})
        entities = []

        asyncio.run(
            self.module.async_setup_entry(hass, entry, entities.extend)
        )

        markers = [
            entity
            for entity in entities
            if isinstance(entity, self.module.OrviboTransportPathSensor)
        ]
        self.assertEqual(len(markers), 2)
        by_device = {entity._device_id: entity for entity in markers}
        self.assertEqual(by_device["light-1"].native_value, "lan_cloud")
        self.assertEqual(by_device["clothes-1"].native_value, "cloud")
        self.assertEqual(
            by_device["light-1"].extra_state_attributes,
            {
                "configured_mode": "auto",
                "lan_control_supported": True,
                "cloud_control_supported": True,
                "cloud_only": False,
                "gateway_connected": True,
                "last_control_transport": "lan",
            },
        )

        coordinator.transport_mode = coordinator.mode_type.LAN_ONLY
        self.assertEqual(by_device["light-1"].native_value, "lan")
        self.assertEqual(by_device["clothes-1"].native_value, "unavailable")


if __name__ == "__main__":
    unittest.main()

