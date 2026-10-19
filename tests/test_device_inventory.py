"""Tests for device discovery and cloud inventory reconciliation."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
import sys
import time
import types
import unittest

COMPONENT_PATH = Path(__file__).parents[1] / "custom_components" / "orvibohomebridge"


def _load_module():
    package_name = "orvibohomebridge_device_inventory_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT_PATH)]
    sys.modules[package_name] = package
    return importlib.import_module(f"{package_name}.device_inventory")


class FakeClient:
    def __init__(self, status=None, description=None, homepage=None):
        self.status = status
        self.description = description
        self.homepage = homepage

    async def fetch_device_status(self):
        return self.status

    async def fetch_device_desc(self, last_update_time=0):
        return self.description

    async def fetch_homepage_data(self):
        return self.homepage

    def parse_device_status_list(self, payload):
        return payload.get("parsed", payload.get("device", []))


class DeviceInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load_module()

    def make_inventory(self, client=None):
        devices = {}
        states = {}
        removed = []
        store = self.module.StateStore(states)
        inventory = self.module.DeviceInventory(
            client or FakeClient(), devices, states, store, removed.append
        )
        return inventory, devices, states, removed

    def test_discover_uses_description_fallback(self) -> None:
        client = FakeClient(
            status={"parsed": []},
            description={"deviceDescList": [{"device_id": "light"}]},
        )
        inventory, _, _, _ = self.make_inventory(client)

        status, devices = asyncio.run(inventory.discover())

        self.assertEqual(status, {"parsed": []})
        self.assertEqual(devices, [{"device_id": "light"}])

    def test_initialize_filters_hidden_and_adds_category_defaults(self) -> None:
        inventory, devices, states, _ = self.make_inventory()

        inventory.initialize(
            [
                {
                    "device_id": "gateway",
                    "device_type_raw": 114,
                },
                {
                    "device_id": "lock",
                    "device_type_raw": 522,
                    "sub_device_type": 463,
                    "online": "online",
                    "properties": {
                        "batteryManager": {
                            "level": 80,
                            "isSetupBattery": "on",
                        }
                    },
                },
                {
                    "device_id": "rack",
                    "device_type_raw": 52,
                },
            ]
        )

        self.assertNotIn("gateway", devices)
        self.assertTrue(states["lock"]["online"])
        self.assertIn("dry_battery_level", states["lock"])
        self.assertEqual(states["rack"]["motor_state"], "stop")

    def test_merge_cloud_removes_hidden_and_merges_status(self) -> None:
        inventory, devices, states, removed = self.make_inventory()
        devices["old"] = {"device_id": "old"}
        states["old"] = {"state": True}

        inventory.merge_cloud(
            [
                {"device_id": "old", "device_type_raw": 114},
                {
                    "device_id": "light",
                    "device_type_raw": 1,
                    "online": True,
                    "status": {"value1": 1},
                },
            ]
        )

        self.assertNotIn("old", devices)
        self.assertEqual(removed, ["old"])
        self.assertTrue(states["light"]["online"])
        self.assertEqual(states["light"]["value1"], 1)

    def test_environmental_snapshot_initialize_and_refresh(self) -> None:
        for method in ("initialize", "merge_cloud"):
            with self.subTest(method=method):
                inventory, _, states, _ = self.make_inventory()
                readings = [
                    {"device_id": "temperature", "device_type_raw": 22,
                     "value1": 2290, "value2": 0, "value4": 45},
                    {"device_id": "humidity", "device_type_raw": 23,
                     "value1": 0, "value2": 6012, "value4": 45},
                ]
                for reading in readings:
                    reading.update(online_time=time.time(), properties={})
                getattr(inventory, method)(readings)
                self.assertEqual(states["temperature"]["temperature"], 22.9)
                self.assertEqual(states["humidity"]["humidity"], 60.12)
                self.assertIsNone(states["humidity"].get("temperature"))
                self.assertIsNone(states["temperature"].get("humidity"))
                self.assertEqual(states["temperature"]["battery"], 45)
                self.assertEqual(states["humidity"]["battery"], 45)
                readings[0]["value1"] = 2400
                inventory.merge_cloud(readings)
                self.assertEqual(states["temperature"]["temperature"], 24.0)

    def test_environmental_stale_snapshot_does_not_seed_or_overwrite(self) -> None:
        inventory, _, states, _ = self.make_inventory()
        reading = {"device_id": "temperature", "device_type_raw": 22,
                   "online_time": 1, "value1": 2290, "value4": 45}
        inventory.initialize([reading])
        self.assertIsNone(states["temperature"].get("temperature"))
        states["temperature"]["temperature"] = 25.0
        inventory.merge_cloud([reading])
        self.assertEqual(states["temperature"]["temperature"], 25.0)

    def test_environmental_cloud_snapshot_respects_lan_priority(self) -> None:
        inventory, _, states, _ = self.make_inventory()
        inventory.state_store.merge(
            "temperature", {"temperature": 25.0}, self.module.StateSource.LAN
        )
        inventory.merge_cloud([
            {"device_id": "temperature", "device_type_raw": 22,
             "online_time": time.time(), "value1": 2290, "value4": 45}
        ])
        self.assertEqual(states["temperature"]["temperature"], 25.0)
        self.assertEqual(states["temperature"]["battery"], 45)

    def test_merge_cloud_refreshes_door_lock_state(self) -> None:
        inventory, _devices, states, _removed = self.make_inventory()

        inventory.merge_cloud(
            [
                {
                    "device_id": "lock",
                    "device_type_raw": 522,
                    "sub_device_type": 463,
                    "online": True,
                    "online_time": int(time.time()),  # 新鲜记录（门锁走 SSL 推送）
                    "properties": {
                        "doorLock": {"doorState": "on", "lockState": "on"}
                    },
                }
            ]
        )

        self.assertTrue(states["lock"]["door_state"])
        self.assertFalse(states["lock"]["locked"])
        self.assertEqual(states["lock"]["lock_status"], "unlocked")

    def test_merge_cloud_removes_devices_after_consecutive_missing_snapshots(self) -> None:
        inventory, devices, states, removed = self.make_inventory()
        devices["old-ac"] = {"device_id": "old-ac", "device_type_raw": 36}
        states["old-ac"] = {"state": True}

        inventory.merge_cloud([])
        inventory.merge_cloud([])

        self.assertIn("old-ac", devices)
        self.assertIn("old-ac", states)

        inventory.merge_cloud([])

        self.assertNotIn("old-ac", devices)
        self.assertNotIn("old-ac", states)
        self.assertEqual(removed, ["old-ac"])

    def test_merge_cloud_missing_count_resets_when_device_reappears(self) -> None:
        inventory, devices, states, removed = self.make_inventory()
        devices["fresh-air"] = {"device_id": "fresh-air", "device_type_raw": 516}
        states["fresh-air"] = {"state": True}

        inventory.merge_cloud([])
        inventory.merge_cloud(
            [{"device_id": "fresh-air", "device_type_raw": 516, "online": True}]
        )
        inventory.merge_cloud([])
        inventory.merge_cloud([])

        self.assertIn("fresh-air", devices)
        self.assertIn("fresh-air", states)
        self.assertEqual(removed, [])


if __name__ == "__main__":
    unittest.main()
