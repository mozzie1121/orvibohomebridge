"""Tests for SSL request/response correlation."""

from __future__ import annotations

import importlib
from pathlib import Path
import sys
import types
import unittest

COMPONENT_PATH = Path(__file__).parents[1] / "custom_components" / "orvibohomebridge"


def _load_module():
    package_name = "orvibohomebridge_pending_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT_PATH)]
    sys.modules[package_name] = package
    return importlib.import_module(f"{package_name}.pending_requests")


class PendingRequestsTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load_module()

    async def test_resolve_response_registered_before_send(self) -> None:
        pending = self.module.PendingRequests()
        future = pending.register("request")

        self.assertTrue(pending.resolve("request", {"code": 0}))
        self.assertEqual(
            await pending.wait("request", future, timeout=0.1),
            {"code": 0},
        )

    async def test_duplicate_and_timeout(self) -> None:
        pending = self.module.PendingRequests()
        future = pending.register("request")
        with self.assertRaises(RuntimeError):
            pending.register("request")
        self.assertIsNone(await pending.wait("request", future, timeout=0.001))
        self.assertFalse(pending.resolve("request", {"late": True}))

    async def test_cancel_all_unblocks_waiter(self) -> None:
        pending = self.module.PendingRequests()
        future = pending.register("request")
        pending.cancel_all()
        self.assertIsNone(await future)

    async def test_replace_unblocks_previous_control_waiter(self) -> None:
        pending = self.module.PendingRequests()
        first = pending.register("control:device-1")
        second = pending.register("control:device-1", replace=True)

        self.assertIsNone(await first)
        self.assertIs(pending.get("control:device-1"), second)
        self.assertTrue(
            pending.resolve("control:device-1", {"deviceId": "device-1"})
        )
        self.assertEqual(
            await pending.wait("control:device-1", second, timeout=0.1),
            {"deviceId": "device-1"},
        )


if __name__ == "__main__":
    unittest.main()
