"""Tests for the LAN control adapter (Stage 2)."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
import sys
import types
import unittest

COMPONENT_PATH = Path(__file__).parents[1] / "custom_components" / "orvibohomebridge"


def _load_package() -> types.ModuleType:
    package_name = "orvibohomebridge_lanctl_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT_PATH)]
    sys.modules[package_name] = package
    return package


class FakeGatewayManager:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def send(
        self, uid: str, payload: dict, *, timeout: float | None = None
    ) -> dict:
        del timeout
        self.sent.append((uid, payload))
        return {"status": 0}


class LanControlAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        package = _load_package()
        cls.adapter_mod = importlib.import_module(
            f"{package.__name__}.lan.control_adapter"
        )
        cls.packet = importlib.import_module(f"{package.__name__}.packet")

    def test_switch_control_reuses_homebridge_payload_builder(self) -> None:
        manager = FakeGatewayManager()
        adapter = self.adapter_mod.LanControlAdapter("user", manager)
        asyncio.run(adapter.send_control_switch("dev-1", "gw-1", True))

        self.assertEqual(len(manager.sent), 1)
        uid, payload = manager.sent[0]
        self.assertEqual(uid, "gw-1")
        expected = self.packet.HomemateJsonData.ssl_control_switch(
            "user", "dev-1", "gw-1", True
        )
        payload.pop("serial", None)
        payload.pop("uniSerial", None)
        expected.pop("serial", None)
        expected.pop("uniSerial", None)
        for field in (
            "groupId",
            "qualityOfService",
            "defaultResponse",
            "propertyResponse",
            "debugInfo",
        ):
            payload.pop(field, None)
            expected.pop(field, None)
        self.assertEqual(payload, expected)

    def test_cover_control_reuses_homebridge_payload_builder(self) -> None:
        manager = FakeGatewayManager()
        adapter = self.adapter_mod.LanControlAdapter("user", manager)
        asyncio.run(adapter.send_control_cover("dev-1", "gw-1", 60))

        self.assertEqual(len(manager.sent), 1)
        uid, payload = manager.sent[0]
        self.assertEqual(uid, "gw-1")
        expected = self.packet.HomemateJsonData.ssl_control_cover(
            "user", "dev-1", "gw-1", 60
        )
        payload.pop("serial", None)
        payload.pop("uniSerial", None)
        expected.pop("serial", None)
        expected.pop("uniSerial", None)
        for field in (
            "groupId",
            "qualityOfService",
            "defaultResponse",
            "propertyResponse",
            "debugInfo",
        ):
            payload.pop(field, None)
            expected.pop(field, None)
        self.assertEqual(payload, expected)

    def test_send_failure_returns_false(self) -> None:
        class BrokenManager(FakeGatewayManager):
            async def send(self, uid, payload, *, timeout=None):  # type: ignore[override]
                del uid, payload, timeout
                raise TimeoutError("gateway timeout")

        adapter = self.adapter_mod.LanControlAdapter("user", BrokenManager())
        ok = asyncio.run(adapter.send_control_switch("dev-1", "gw-1", False))
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
