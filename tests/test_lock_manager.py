"""Tests for smart-lock event orchestration."""

from __future__ import annotations

import importlib
from pathlib import Path
import sys
import types
import unittest

COMPONENT_PATH = Path(__file__).parents[1] / "custom_components" / "orvibohomebridge"


def _load_module():
    package_name = "orvibohomebridge_lock_manager_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT_PATH)]
    sys.modules[package_name] = package
    return importlib.import_module(f"{package_name}.lock_manager")


class LockEventManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load_module()

    def test_duplicate_state_event_is_suppressed(self) -> None:
        manager = self.module.LockEventManager(lambda device_id, user_id: None)
        raw = {"properties": {"doorLock": {"lockState": "on"}}}

        self.assertIsNotNone(manager.build_event("lock", raw))
        self.assertIsNone(manager.build_event("lock", raw))

    def test_recent_unlock_attributes_open_door(self) -> None:
        now = [100.0]
        manager = self.module.LockEventManager(
            lambda device_id, user_id: "张三" if str(user_id) == "7" else None,
            clock=lambda: now[0],
        )
        unlock = {
            "event": {
                "server": "doorLock",
                "name": "unlockEvent",
                "value": {"type": "fingerprint", "userId": 7},
            },
            "time": 123,
        }
        opened = {"properties": {"doorLock": {"doorState": "on"}}}

        unlock_event = manager.build_event("lock", unlock)
        now[0] = 105.0
        opened_event = manager.build_event("lock", opened)

        self.assertEqual(unlock_event["unlock_user_name"], "张三")
        self.assertEqual(opened_event["opened_by_user_id"], "7")
        self.assertEqual(opened_event["opened_by_name"], "张三")

    def test_stale_unlock_does_not_attribute_open_door(self) -> None:
        now = [100.0]
        manager = self.module.LockEventManager(
            lambda device_id, user_id: None, clock=lambda: now[0]
        )
        manager.build_event(
            "lock",
            {
                "event": {
                    "server": "doorLock",
                    "name": "unlockEvent",
                    "value": {"type": "card", "userId": 2},
                }
            },
        )
        now[0] = 131.0

        event = manager.build_event(
            "lock", {"properties": {"doorLock": {"doorState": "on"}}}
        )

        self.assertNotIn("opened_by_user_id", event)

    def test_transient_ring_returns_patch_and_reset_kind(self) -> None:
        update = self.module.LockEventManager.transient_update(
            {
                "event": {
                    "server": "doorbell",
                    "name": "ring",
                    "value": {"url": "pic.jpg", "doorbell_local_Ip": "10.0.0.2"},
                }
            }
        )

        self.assertEqual(update.reset_kind, "doorbell")
        self.assertEqual(update.patch.values["doorbell_url"], "pic.jpg")

    def test_message_snapshot_kind_is_normalized(self) -> None:
        event = self.module.LockEventManager.build_message(
            "lock",
            {
                "cmd": 82,
                "infoType": 12,
                "text": "检测到访客来访",
                "data": {"deviceId": "wire-id"},
            },
        )

        self.assertEqual(event["device_id"], "lock")
        self.assertEqual(event["snapshot_kind"], "visit")


if __name__ == "__main__":
    unittest.main()
