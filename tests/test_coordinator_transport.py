"""Coordinator-level tests for transport mode initialization."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
import importlib
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock


COMPONENT_PATH = (
    Path(__file__).parents[1] / "custom_components" / "orvibohomebridge"
)


def _module(name: str, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _load_coordinator_module():
    package_name = "orvibohomebridge_coordinator_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT_PATH)]
    sys.modules[package_name] = package

    class DataUpdateCoordinator:
        @classmethod
        def __class_getitem__(cls, _item):
            return cls

        def __init__(self, hass, _logger, *, name, update_interval):
            self.hass = hass
            self.name = name
            self.update_interval = update_interval
            self.data = None

        def async_set_updated_data(self, data):
            self.data = data

        def async_update_listeners(self):
            return None

    class ConfigEntryAuthFailed(Exception):
        pass

    class UpdateFailed(Exception):
        pass

    homeassistant = _module("homeassistant")
    homeassistant.__path__ = []
    helpers = _module("homeassistant.helpers")
    helpers.__path__ = []
    _module("homeassistant.core", HomeAssistant=object)
    _module("homeassistant.exceptions", ConfigEntryAuthFailed=ConfigEntryAuthFailed)
    _module(
        "homeassistant.helpers.update_coordinator",
        DataUpdateCoordinator=DataUpdateCoordinator,
        UpdateFailed=UpdateFailed,
    )
    _module(
        "homeassistant.helpers.aiohttp_client",
        async_get_clientsession=lambda _hass: object(),
    )

    class TransportMode(str, Enum):
        AUTO = "auto"
        LAN_ONLY = "lan_only"
        CLOUD_ONLY = "cloud_only"

    class DeviceCategory:
        UNKNOWN = "unknown"
        TEMP_HUMIDITY_SENSOR = "temp_humidity"
        DOOR_WINDOW_SENSOR = "door_window"
        MOTION_SENSOR = "motion"
        SMOKE_SENSOR = "smoke"
        EMERGENCY_BUTTON = "emergency"
        WATER_LEAK_SENSOR = "water_leak"
        GAS_SENSOR = "gas"
        DOOR_LOCK = "door_lock"
        VENTILATION_SYSTEM = "ventilation"

    @dataclass(frozen=True)
    class AccountCredentials:
        username: str
        password_hash: str
        family_id: str = ""

    class CloudEndpoint:
        def __init__(self):
            self.ssl_host = "cloud.example"

    cloud = CloudEndpoint()

    class HttpsClient:
        def __init__(self, *, username, password_hash, session, cloud):
            self.username = username
            self.password_hash = password_hash
            self.session = session
            self.cloud = cloud
            self.family_id = None
            self.family_name = None
            self.gateway_ips = {"gateway-1": "192.0.2.1"}
            self.user_id = "user-id"

        async def async_detect_cloud(self, _family_id):
            return True

        async def fetch_device_status(self):
            raise AssertionError("LAN-only coordinator must not poll the cloud")

    class StateStore:
        def __init__(self, states):
            self.states = states

    class LockEventManager:
        def __init__(self, *_args, **_kwargs):
            pass

        def remove(self, _device_id):
            return None

    class StatusUpdateDispatcher:
        def __init__(self, *_args, **_kwargs):
            pass

        def dispatch(self, *_args, **_kwargs):
            return None

    class PassiveManager:
        def __init__(self, *_args, **_kwargs):
            pass

    class DeviceInventory:
        def __init__(
            self,
            _https_client,
            devices,
            states,
            _state_store,
            _remove_callback,
        ):
            self.devices = devices
            self.states = states

        async def discover(self):
            return {"status": "ok"}, [
                {"device_id": "device-1", "device_name": "Test light"}
            ]

        def initialize(self, devices):
            for device in devices:
                device_id = device["device_id"]
                self.devices[device_id] = dict(device)
                self.states[device_id] = {"online": True}

        def merge_cloud(self, _devices):
            return None

    class GatewayManager:
        last_call = None

        def __init__(self, *args, **kwargs):
            type(self).last_call = (args, kwargs)
            self.gateway_hosts = {}

        def is_connected(self, _uid):
            return False

    class LanControlAdapter:
        last_call = None

        def __init__(self, *args, **kwargs):
            type(self).last_call = (args, kwargs)

    _module(
        f"{package_name}.ssl_client",
        SSLClient=PassiveManager,
    )
    _module(
        f"{package_name}.lan",
        GatewayManager=GatewayManager,
        LanControlAdapter=LanControlAdapter,
    )
    _module(
        f"{package_name}.capabilities",
        TransportMode=TransportMode,
        lan_state_allowed=lambda *_args: True,
    )
    _module(f"{package_name}.https_client", HttpsClient=HttpsClient)
    _module(
        f"{package_name}.device_types",
        DeviceCategory=DeviceCategory,
        classify_device=lambda _device: DeviceCategory.UNKNOWN,
    )
    _module(f"{package_name}.redact", fingerprint=lambda value, _salt: value)
    _module(f"{package_name}.models", AccountCredentials=AccountCredentials)
    _module(
        f"{package_name}.cloud",
        CHINA_CLOUD=cloud,
        CloudEndpoint=CloudEndpoint,
        cloud_candidates=lambda current: (current, current),
    )
    _module(
        f"{package_name}.state_store",
        StateSource=object,
        StateStore=StateStore,
    )
    _module(f"{package_name}.parsers", get_state_parser=lambda _category: None)
    _module(f"{package_name}.lock_manager", LockEventManager=LockEventManager)
    _module(
        f"{package_name}.status_dispatcher",
        StatusUpdateDispatcher=StatusUpdateDispatcher,
    )
    _module(f"{package_name}.lock_media_manager", LockMediaManager=PassiveManager)
    _module(
        f"{package_name}.temp_password_manager",
        TempPasswordManager=PassiveManager,
    )
    _module(f"{package_name}.device_inventory", DeviceInventory=DeviceInventory)
    _module(f"{package_name}.control_executor", ControlExecutor=PassiveManager)
    _module(
        f"{package_name}.const",
        DOMAIN="orvibohomebridge",
        SSL_PORT=10002,
        UPDATE_INTERVAL=timedelta(minutes=30),
        DEFAULT_KEY="test-key",
        CLOUD_RECORD_STALE_SECONDS=7200,
    )

    loaded = importlib.import_module(f"{package_name}.coordinator")
    loaded.TestGatewayManager = GatewayManager
    loaded.TestLanControlAdapter = LanControlAdapter
    return loaded


class _BackgroundTask:
    def cancel(self):
        return None


class _FakeHass:
    def async_create_background_task(self, coro, *, name):
        del name
        coro.close()
        return _BackgroundTask()

    def async_create_task(self, coro):
        coro.close()
        return _BackgroundTask()

    def add_job(self, target, *args):
        return target(*args)


class CoordinatorTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_coordinator_module()

    def make_coordinator(self, mode, *, lan_credentials=None):
        credentials = self.module.AccountCredentials(
            "cloud-user",
            "cloud-hash",
            "family-1",
        )
        return self.module.OrviboMeshCoordinator(
            _FakeHass(),
            credentials,
            transport_mode=mode,
            lan_credentials=lan_credentials,
        )

    def test_lan_only_skips_cloud_realtime_and_polling(self):
        coordinator = self.make_coordinator(self.module.TransportMode.LAN_ONLY)
        coordinator._init_ssl_client = AsyncMock()
        coordinator._init_lan_gateways = AsyncMock()

        asyncio.run(coordinator._async_setup())

        self.assertIsNone(coordinator.update_interval)
        coordinator._init_ssl_client.assert_not_awaited()
        coordinator._init_lan_gateways.assert_awaited_once()
        states = asyncio.run(coordinator._async_update_data())
        self.assertIs(states, coordinator.device_states)

    def test_cloud_only_skips_lan_initialization(self):
        coordinator = self.make_coordinator(self.module.TransportMode.CLOUD_ONLY)
        coordinator._init_ssl_client = AsyncMock()
        coordinator._init_lan_gateways = AsyncMock()

        asyncio.run(coordinator._async_setup())

        coordinator._init_ssl_client.assert_awaited_once()
        coordinator._init_lan_gateways.assert_not_awaited()

    def test_auto_initializes_cloud_and_lan(self):
        coordinator = self.make_coordinator(self.module.TransportMode.AUTO)
        coordinator._init_ssl_client = AsyncMock()
        coordinator._init_lan_gateways = AsyncMock()

        asyncio.run(coordinator._async_setup())

        coordinator._init_ssl_client.assert_awaited_once()
        coordinator._init_lan_gateways.assert_awaited_once()

    def test_independent_credentials_reach_gateway_manager(self):
        lan_credentials = self.module.AccountCredentials(
            "mixpad-user",
            "mixpad-hash",
            "family-1",
        )
        coordinator = self.make_coordinator(
            self.module.TransportMode.AUTO,
            lan_credentials=lan_credentials,
        )

        asyncio.run(coordinator._init_lan_gateways())

        args, kwargs = self.module.TestGatewayManager.last_call
        self.assertEqual(args[:2], ("mixpad-user", "mixpad-hash"))
        self.assertTrue(kwargs["password_is_hash"])
        adapter_args, _ = self.module.TestLanControlAdapter.last_call
        self.assertEqual(adapter_args[0], "mixpad-user")


if __name__ == "__main__":
    unittest.main()

