"""Tests for source-aware device state reconciliation."""

from __future__ import annotations

import importlib
from pathlib import Path
import sys
import types
import unittest

COMPONENT_PATH = Path(__file__).parents[1] / "custom_components" / "orvibohomebridge"


def _load_module():
    package_name = "orvibohomebridge_state_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT_PATH)]
    sys.modules[package_name] = package
    return importlib.import_module(f"{package_name}.state_store")


class StateStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load_module()

    def test_fresh_ssl_value_is_not_rolled_back_by_cloud(self) -> None:
        states = {"device": {"state": False}}
        store = self.module.StateStore(states, priority_guard_seconds=30)

        store.merge("device", {"state": True}, self.module.StateSource.SSL, now=100)
        changed = store.merge(
            "device", {"state": False}, self.module.StateSource.CLOUD, now=110
        )

        self.assertEqual(changed, set())
        self.assertTrue(states["device"]["state"])

    def test_cloud_reconciles_after_ssl_guard_expires(self) -> None:
        states = {"device": {"state": False}}
        store = self.module.StateStore(states, priority_guard_seconds=30)

        store.merge("device", {"state": True}, self.module.StateSource.SSL, now=100)
        changed = store.merge(
            "device", {"state": False}, self.module.StateSource.CLOUD, now=131
        )

        self.assertEqual(changed, {"state"})
        self.assertFalse(states["device"]["state"])

    def test_cloud_can_fill_unrelated_low_frequency_fields(self) -> None:
        states = {"lock": {"state": True}}
        store = self.module.StateStore(states, priority_guard_seconds=30)
        store.mark("lock", ("state",), self.module.StateSource.SSL, now=100)

        changed = store.merge(
            "lock",
            {"state": False, "dry_battery_level": 82},
            self.module.StateSource.CLOUD,
            now=110,
        )

        self.assertEqual(changed, {"dry_battery_level"})
        self.assertTrue(states["lock"]["state"])
        self.assertEqual(states["lock"]["dry_battery_level"], 82)

    def test_remove_discards_revision_history(self) -> None:
        states = {"device": {"state": True}}
        store = self.module.StateStore(states)
        store.mark("device", ("state",), self.module.StateSource.SSL, now=100)
        store.remove("device")

        changed = store.merge(
            "device", {"state": False}, self.module.StateSource.CLOUD, now=101
        )
        self.assertEqual(changed, {"state"})

    def test_optimistic_command_forces_new_intent_then_ssl_confirms(self) -> None:
        states = {"device": {"state": False}}
        store = self.module.StateStore(states, priority_guard_seconds=30)
        store.mark("device", ("state",), self.module.StateSource.SSL, now=100)

        store.merge(
            "device",
            {"state": True},
            self.module.StateSource.OPTIMISTIC,
            now=101,
            force=True,
        )
        self.assertTrue(states["device"]["state"])

        store.merge(
            "device", {"state": False}, self.module.StateSource.SSL, now=102
        )
        self.assertFalse(states["device"]["state"])


if __name__ == "__main__":
    unittest.main()
