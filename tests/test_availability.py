"""Round 1 (P0-1): availability derivation & cloud-online freshness tests."""

from __future__ import annotations

import importlib
from datetime import timedelta
from enum import Enum
from pathlib import Path
import sys
import time
import types
import unittest

COMPONENT_PATH = (
    Path(__file__).parents[1] / "custom_components" / "orvibohomebridge"
)


def _module(name: str, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


# ---------------------------------------------------------------------------
# 1) protocol: online/online_time 解析与透传
# ---------------------------------------------------------------------------

class ProtocolAvailabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        package_name = "orvibohomebridge_proto_test"
        package = types.ModuleType(package_name)
        package.__path__ = [str(COMPONENT_PATH)]
        sys.modules[package_name] = package
        cls.protocol = importlib.import_module(f"{package_name}.protocol")

    def test_parse_readtable_devices_threads_online_time(self):
        payload = {
            "data": {
                "device": [
                    {
                        "deviceId": "dev-1",
                        "deviceName": "灯",
                        "deviceType": "501",
                        "uid": "uid-1",
                    }
                ],
                "deviceStatus": [
                    {
                        "deviceId": "dev-1",
                        "online": 1,
                        "updateTimeSec": 1788162782,
                        "value1": 0,
                        "value2": 0,
                        "value3": 0,
                        "value4": 0,
                    }
                ],
            }
        }
        devices = self.protocol.parse_readtable_devices(payload)
        self.assertEqual(len(devices), 1)
        device = devices[0]
        self.assertIs(device.online, True)
        self.assertEqual(device.online_time, 1788162782)
        dumped = self.protocol.device_to_dict(device)
        self.assertIs(dumped["online"], True)
        self.assertEqual(dumped["online_time"], 1788162782)

    def test_online_missing_is_unknown_not_false(self):
        """H3: deviceStatus 无 online 字段/无状态行 → online 保持 None，不强制 False。"""
        payload = {
            "data": {
                "device": [
                    {
                        "deviceId": "dev-2",
                        "deviceName": "无状态设备",
                        "deviceType": "501",
                        "uid": "uid-2",
                    }
                ],
                "deviceStatus": [],
            }
        }
        devices = self.protocol.parse_readtable_devices(payload)
        self.assertEqual(len(devices), 1)
        dumped = self.protocol.device_to_dict(devices[0])
        self.assertIsNone(dumped["online"])
        self.assertIsNone(dumped["online_time"])

    def test_online_zero_is_explicit_offline(self):
        payload = {
            "data": {
                "device": [
                    {
                        "deviceId": "dev-3",
                        "deviceName": "离线设备",
                        "deviceType": "501",
                        "uid": "uid-3",
                    }
                ],
                "deviceStatus": [
                    {
                        "deviceId": "dev-3",
                        "online": 0,
                        "updateTimeSec": 1788162782,
                        "value1": 0,
                        "value2": 0,
                        "value3": 0,
                        "value4": 0,
                    }
                ],
            }
        }
        devices = self.protocol.parse_readtable_devices(payload)
        dumped = self.protocol.device_to_dict(devices[0])
        self.assertIs(dumped["online"], False)
        self.assertEqual(dumped["online_time"], 1788162782)


# ---------------------------------------------------------------------------
# 2) coordinator: _derive_online / get_device_state 纯函数化
# ---------------------------------------------------------------------------

def _load_coordinator():
    package_name = "orvibohomebridge_coord_avail"
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

    @staticmethod
    def _classify(_device):
        return DeviceCategory.UNKNOWN

    class AccountCredentials:
        def __init__(self, username, password_hash, family_id=""):
            self.username = username
            self.password_hash = password_hash
            self.family_id = family_id

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
            self.gateway_ips = {}
            self.user_id = "user-id"

    class Passive:
        def __init__(self, *_args, **_kwargs):
            pass

        def __getattr__(self, name):
            return lambda *_a, **_k: None

    class FakeGatewayManager:
        connected = False

        def __init__(self, *_args, **_kwargs):
            self.gateway_hosts = {}

        def is_connected(self, _uid):
            return type(self).connected

    class StateStore:
        def __init__(self, states):
            self.states = states

    _module(f"{package_name}.ssl_client", SSLClient=Passive)
    _module(
        f"{package_name}.lan",
        GatewayManager=FakeGatewayManager,
        LanControlAdapter=Passive,
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
        classify_device=_classify,
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
    _module(f"{package_name}.lock_manager", LockEventManager=Passive)
    _module(f"{package_name}.status_dispatcher", StatusUpdateDispatcher=Passive)
    _module(f"{package_name}.lock_media_manager", LockMediaManager=Passive)
    _module(f"{package_name}.temp_password_manager", TempPasswordManager=Passive)
    _module(f"{package_name}.device_inventory", DeviceInventory=Passive)
    _module(f"{package_name}.control_executor", ControlExecutor=Passive)
    _module(
        f"{package_name}.const",
        DOMAIN="orvibohomebridge",
        SSL_PORT=10002,
        UPDATE_INTERVAL=timedelta(minutes=30),
        DEFAULT_KEY="test-key",
    )
    _module(f"{package_name}.lock_status", normalize_battery_properties=lambda _p: {})
    return importlib.import_module(f"{package_name}.coordinator")


class CoordinatorAvailabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_coordinator()

    def make_coordinator(self):
        coordinator = object.__new__(self.module.OrviboMeshCoordinator)
        coordinator.device_states = {}
        coordinator.devices = {}
        coordinator._last_update_time = {}
        coordinator.OFFLINE_TIMEOUT = 600
        coordinator.CLOUD_RECORD_STALE_SECONDS = 7200
        coordinator.gateway_manager = None
        return coordinator

    def test_push_fresh_keeps_online(self):
        coordinator = self.make_coordinator()
        device_id = "dev-1"
        coordinator._last_update_time[device_id] = time.time() - 60
        coordinator.device_states[device_id] = {
            "online": True,
            "cloud_online": False,
            "cloud_online_time": time.time() - 100,
        }
        state = coordinator.get_device_state(device_id)
        self.assertIs(state["online"], True)

    def test_stale_push_and_fresh_cloud_offline_flips_offline(self):
        coordinator = self.make_coordinator()
        device_id = "dev-2"
        coordinator._last_update_time[device_id] = time.time() - 3600
        coordinator.device_states[device_id] = {
            "online": True,
            "cloud_online": False,
            "cloud_online_time": time.time() - 100,
        }
        state = coordinator.get_device_state(device_id)
        self.assertIs(state["online"], False)

    def test_fresh_cloud_online_keeps_idle_device_online(self):
        """核心修复：推送安静但云端记录新鲜且在线 → 保持在线（不再 600s 翻转）。"""
        coordinator = self.make_coordinator()
        device_id = "dev-3"
        coordinator._last_update_time[device_id] = time.time() - 3600
        coordinator.device_states[device_id] = {
            "online": True,
            "cloud_online": True,
            "cloud_online_time": time.time() - 120,
        }
        state = coordinator.get_device_state(device_id)
        self.assertIs(state["online"], True)

    def test_stale_cloud_record_falls_back_to_last_known(self):
        """纱帘场景：云端记录陈旧（online=0 但 updateTime 很久前）→ 不判离线。"""
        coordinator = self.make_coordinator()
        device_id = "dev-4"
        coordinator.device_states[device_id] = {
            "online": True,
            "cloud_online": False,
            "cloud_online_time": time.time() - 30 * 24 * 3600,  # 30 天前
        }
        state = coordinator.get_device_state(device_id)
        self.assertIs(state["online"], True)

    def test_lan_gateway_connected_keeps_online(self):
        coordinator = self.make_coordinator()
        device_id = "dev-5"
        coordinator.devices[device_id] = {"uid": "gateway-1"}
        coordinator.gateway_manager = type(
            "GM", (), {"is_connected": lambda self, uid: uid == "gateway-1"}
        )()
        coordinator._last_update_time[device_id] = time.time() - 3600
        coordinator.device_states[device_id] = {
            "online": True,
            "cloud_online": False,
            "cloud_online_time": time.time() - 30 * 24 * 3600,
        }
        state = coordinator.get_device_state(device_id)
        self.assertIs(state["online"], True)

    def test_get_device_state_is_pure_no_mutation(self):
        coordinator = self.make_coordinator()
        device_id = "dev-6"
        coordinator._last_update_time[device_id] = time.time() - 3600
        coordinator.device_states[device_id] = {
            "online": True,
            "cloud_online": False,
            "cloud_online_time": time.time() - 100,
        }
        before = dict(coordinator.device_states[device_id])
        coordinator.get_device_state(device_id)
        self.assertEqual(coordinator.device_states[device_id], before)
        # 共享状态里的 online 保持原值（False 由读取侧派生，不落库）
        self.assertIs(coordinator.device_states[device_id]["online"], True)


if __name__ == "__main__":
    unittest.main()
