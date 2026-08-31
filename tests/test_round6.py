"""Round 6 (P1): binary_sensor 可用性语义统一 + LAN 网关记录缺失容错。"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
import sys
import types
import unittest

COMPONENT_PATH = Path(__file__).parents[1] / "custom_components" / "orvibohomebridge"


def _module(name: str, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _load_binary_sensor():
    package_name = "orvibohomebridge_bs_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT_PATH)]
    sys.modules[package_name] = package

    _module("homeassistant")
    _module("homeassistant.components")

    class _CoordinatorEntity:
        def __init__(self, *args, **kwargs):
            if args:
                self.coordinator = args[0]

    class _BinarySensorEntity:
        def __init__(self, *args, **kwargs):
            pass

    class _BinarySensorDeviceClass:
        MOTION = "motion"
        DOOR = "door"
        LOCK = "lock"
        SMOKE = "smoke"
        SAFETY = "safety"
        GAS = "gas"
        PROBLEM = "problem"
        CONNECTIVITY = "connectivity"
        TAMPER = "tamper"
        OPENING = "opening"
        VIBRATION = "vibration"

    _module(
        "homeassistant.components.binary_sensor",
        BinarySensorDeviceClass=_BinarySensorDeviceClass,
        BinarySensorEntity=_BinarySensorEntity,
    )
    _module("homeassistant.config_entries", ConfigEntry=object)
    _module("homeassistant.core", HomeAssistant=object)
    _module("homeassistant.helpers")
    _module(
        "homeassistant.helpers.update_coordinator",
        CoordinatorEntity=_CoordinatorEntity,
    )
    _module("homeassistant.helpers.entity_platform", AddEntitiesCallback=object)

    class DeviceCategory:
        MOTION_SENSOR = "motion"
        DOOR_WINDOW_SENSOR = "door_window"
        DOOR_LOCK = "door_lock"
        SMOKE_SENSOR = "smoke"
        EMERGENCY_BUTTON = "emergency"
        WATER_LEAK_SENSOR = "water_leak"
        GAS_SENSOR = "gas"

    _module(
        f"{package_name}.device_types",
        DeviceCategory=DeviceCategory,
        classify_device=lambda _d: DeviceCategory.MOTION_SENSOR,
    )
    _module(
        f"{package_name}.selection",
        selected_device_ids=lambda _opts, _devices: {"dev-1"},
    )
    _module(
        f"{package_name}.const",
        DOMAIN="orvibohomebridge",
        MANUFACTURER="ORVIBO",
    )

    class Coordinator:
        def __init__(self, states):
            self.states = states
            self.device_states = states

        def get_device_state(self, device_id):
            return self.states.get(device_id)

    _module(
        f"{package_name}.coordinator",
        OrviboMeshCoordinator=Coordinator,
    )
    return importlib.import_module(f"{package_name}.binary_sensor")


def _load_gateway_manager():
    package_name = "orvibohomebridge_gw_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT_PATH)]
    sys.modules[package_name] = package

    _module(
        f"{package_name}.discovery",
        DiscoveryCandidate=object,
        GatewayDiscovery=object,
    )
    _module(
        f"{package_name}.gateway_connection",
        GatewayConnection=object,
        GatewayConnectionError=Exception,
        PushCallback=object,
    )
    return importlib.import_module(f"{package_name}.lan.gateway_manager")


class BinarySensorAvailabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_binary_sensor()

    def make_sensor(self, online):
        coordinator = type(
            "Coord",
            (),
            {
                "device_states": {"dev-1": {"online": online}},
                "get_device_state": lambda self, did: self.device_states.get(did),
            },
        )()
        device = {"device_id": "dev-1", "device_name": "人体"}
        return self.mod.OrviboMotionSensor(coordinator, device)

    def test_available_follows_device_online(self) -> None:
        self.assertTrue(self.make_sensor(True).available)
        self.assertFalse(self.make_sensor(False).available)


class GatewayMissToleranceTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_gateway_manager()

    async def test_single_snapshot_miss_keeps_record(self) -> None:
        manager = self.mod.GatewayManager(
            "user", "hash", {"gw-1": "192.168.1.100"}, password_is_hash=True
        )
        await manager.async_update_cloud_gateways({})  # 缺失 1 轮
        self.assertIn("gw-1", manager._records)

    async def test_recovery_resets_miss_count(self) -> None:
        manager = self.mod.GatewayManager(
            "user", "hash", {"gw-1": "192.168.1.100"}, password_is_hash=True
        )
        await manager.async_update_cloud_gateways({})  # 缺失 1
        await manager.async_update_cloud_gateways({})  # 缺失 2
        await manager.async_update_cloud_gateways({"gw-1": "192.168.1.100"})  # 恢复
        await manager.async_update_cloud_gateways({})  # 再缺失 1
        self.assertIn("gw-1", manager._records)

    async def test_removed_only_after_tolerance_misses(self) -> None:
        manager = self.mod.GatewayManager(
            "user", "hash", {"gw-1": "192.168.1.100"}, password_is_hash=True
        )
        await manager.async_update_cloud_gateways({})  # 1
        await manager.async_update_cloud_gateways({})  # 2
        self.assertIn("gw-1", manager._records)
        await manager.async_update_cloud_gateways({})  # 3 ≥ tolerance
        self.assertNotIn("gw-1", manager._records)


if __name__ == "__main__":
    unittest.main()
