"""Tests for LAN-first control transport selection (Stage 2)."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
import sys
import types
import unittest

COMPONENT_PATH = Path(__file__).parents[1] / "custom_components" / "orvibohomebridge"


def _load_module():
    package_name = "orvibohomebridge_transport_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT_PATH)]
    sys.modules[package_name] = package
    return importlib.import_module(f"{package_name}.control_executor")


class FakeSsl:
    def __init__(self) -> None:
        self.calls = []

    async def send_control_light_colortemp(self, *args, **kwargs):
        self.calls.append(("ssl_light_colortemp", args, kwargs))
        return True

    async def send_control_switch(self, *args, **kwargs):
        self.calls.append(("ssl_switch", args, kwargs))
        return True

    async def send_control_light(self, *args, **kwargs):
        self.calls.append(("ssl_light", args, kwargs))
        return True

    async def _wait_for_control_response(self, device_id):
        del device_id
        return None


class FakeLan:
    def __init__(self) -> None:
        self.calls = []

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        async def method(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return True

        return method


class ControlTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load_module()

    def make_executor(self, device, lan=None, gateway_connected=True, mode="auto"):
        devices = {"device": device}
        states = {"device": {}}
        ssl = FakeSsl()
        store = self.module.StateStore(states)
        executor = self.module.ControlExecutor(
            devices,
            states,
            store,
            lambda: ssl,
            lambda: object(),
            states.get,
            lambda: None,
            lambda: lan,
            lambda _uid: gateway_connected,
            self.module.TransportMode(mode),
        )
        return executor, ssl

    def test_lan_preferred_when_gateway_connected(self) -> None:
        device = {"device_type_raw": 38, "uid": "gw-1"}
        lan = FakeLan()
        executor, ssl = self.make_executor(device, lan=lan)

        ok = asyncio.run(executor.turn_on("device"))
        self.assertTrue(ok)
        self.assertTrue(lan.calls)
        self.assertFalse(ssl.calls)

    def test_ssl_fallback_when_gateway_disconnected(self) -> None:
        device = {"device_type_raw": 38, "uid": "gw-1"}
        lan = FakeLan()
        executor, ssl = self.make_executor(device, lan=lan, gateway_connected=False)

        ok = asyncio.run(executor.turn_on("device"))
        self.assertTrue(ok)
        self.assertFalse(lan.calls)
        self.assertTrue(ssl.calls)

    def test_cloud_only_mode_forces_ssl(self) -> None:
        device = {"device_type_raw": 38, "uid": "gw-1"}
        lan = FakeLan()
        executor, ssl = self.make_executor(
            device, lan=lan, gateway_connected=True, mode="cloud_only"
        )

        ok = asyncio.run(executor.turn_on("device"))
        self.assertTrue(ok)
        self.assertFalse(lan.calls)
        self.assertTrue(ssl.calls)

    def test_cloud_only_lock_never_uses_lan(self) -> None:
        device = {"device_type_raw": 522, "sub_device_type": 463, "uid": "gw-1"}
        lan = FakeLan()
        executor, ssl = self.make_executor(device, lan=lan, gateway_connected=True)

        ok = asyncio.run(executor.turn_on("device"))
        self.assertTrue(ok)
        self.assertFalse(lan.calls)
        self.assertTrue(ssl.calls)


if __name__ == "__main__":
    unittest.main()
