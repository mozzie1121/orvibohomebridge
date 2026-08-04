"""Tests for temporary-password orchestration boundaries."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
import sys
import types
import unittest

COMPONENT_PATH = Path(__file__).parents[1] / "custom_components" / "orvibohomebridge"


def _load_module():
    package_name = "orvibohomebridge_temp_password_manager_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT_PATH)]
    sys.modules[package_name] = package
    return importlib.import_module(f"{package_name}.temp_password_manager")


class FakeHass:
    class Bus:
        def __init__(self):
            self.events = []

        def async_fire(self, event_type, data):
            self.events.append((event_type, data))

    def __init__(self):
        self.bus = self.Bus()


class FakeHttps:
    def __init__(self, data=None):
        self.is_logged_in = data is not None
        self.data = data

    async def _readtable(self, device_flag=0):
        return self.data


class FakeSsl:
    def __init__(self):
        self.revoked = []

    async def send_temp_password(self, **kwargs):
        return {
            "status": 0,
            "code": "123456",
            "type": kwargs["auth_type"],
            "userName": kwargs["name"],
            "authorizedUnlock": {
                "authorizedId": 7,
                "password": "123456",
                "number": kwargs["number"],
                "startTime": 1,
                "endTime": 9999999999,
            },
        }

    async def delete_authorization(self, **kwargs):
        self.revoked.append(kwargs)
        return {"status": 0}


class TempPasswordManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load_module()

    def make_manager(self, ssl=None, https=None, devices=None):
        states = {}
        updates = []
        manager = self.module.TempPasswordManager(
            FakeHass(),
            devices or {},
            states,
            https or FakeHttps(),
            lambda: ssl,
            lambda: updates.append(dict(states)),
            b"salt",
        )
        return manager, updates

    def test_grant_fails_cleanly_before_ssl_ready(self) -> None:
        manager, _ = self.make_manager()
        result = asyncio.run(manager.grant("lock"))

        self.assertEqual(result, {"error": "SSL 客户端未就绪"})

    def test_list_never_exposes_password(self) -> None:
        manager, _ = self.make_manager(object())

        async def records():
            return [
                {
                    "device_id": "lock",
                    "authorized_id": 1,
                    "password": "123456",
                    "status": 0,
                    "start_time": 0,
                    "end_time": 0,
                    "number": 1,
                }
            ]

        manager.fetch_server_records = records
        result = asyncio.run(manager.list("lock"))

        self.assertNotIn("password", result["lock"][0])

    def test_grant_returns_password_but_public_event_does_not(self) -> None:
        ssl = FakeSsl()
        manager, updates = self.make_manager(
            ssl, devices={"lock": {"device_type_raw": 522, "sub_device_type": 463, "uid": "lock-uid"}}
        )
        manager.fetch_server_records = _empty_records

        result = asyncio.run(manager.grant("lock", name="访客"))

        self.assertEqual(result["password"], "123456")
        event_type, event_data = manager.hass.bus.events[0]
        self.assertEqual(event_type, self.module.TEMP_PASSWORD_EVENT)
        self.assertNotIn("password", event_data)
        self.assertEqual(event_data["authorized_id"], 7)
        self.assertTrue(updates)
        self.assertIn("temp_password_ts", manager.device_states["lock"])

    def test_fetch_server_records_preserves_local_name_and_type(self) -> None:
        https = FakeHttps(
            {
                "authorizedUnlock": [
                    {
                        "deviceId": "lock",
                        "authorizedId": 7,
                        "authorizedUnlockId": "auth-7",
                        "password": "123456",
                        "number": 1,
                        "unlockNum": 0,
                        "authorizeStatus": 0,
                    }
                ]
            }
        )
        manager, _ = self.make_manager(https=https)
        manager._records = {
            "lock": [{"authorized_id": 7, "name": "访客", "type": 2}]
        }

        records = asyncio.run(manager.fetch_server_records())

        self.assertEqual(records[0]["name"], "")
        self.assertEqual(manager._records["lock"][0]["name"], "访客")
        self.assertEqual(manager._records["lock"][0]["type"], 2)

    def test_revoke_removes_local_record_and_notifies(self) -> None:
        ssl = FakeSsl()
        manager, updates = self.make_manager(
            ssl, devices={"lock": {"device_type_raw": 522, "sub_device_type": 463, "uid": "lock-uid"}}
        )
        manager._records = {"lock": [{"authorized_id": 7}]}

        result = asyncio.run(manager.revoke("lock", 7))

        self.assertEqual(result, {"ok": True, "authorized_id": 7})
        self.assertEqual(manager._records["lock"], [])
        self.assertEqual(ssl.revoked[0]["device_uid"], "lock-uid")
        self.assertTrue(updates)


async def _empty_records():
    return []


if __name__ == "__main__":
    unittest.main()
