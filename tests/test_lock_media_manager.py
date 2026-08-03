"""Tests for lock media orchestration boundaries."""

from __future__ import annotations

import importlib
from pathlib import Path
import sys
import types
import unittest

COMPONENT_PATH = Path(__file__).parents[1] / "custom_components" / "orvibohomebridge"


def _load_module():
    package_name = "orvibohomebridge_lock_media_manager_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT_PATH)]
    sys.modules[package_name] = package
    return importlib.import_module(f"{package_name}.lock_media_manager")


class FakeConfig:
    def path(self, name):
        return str(Path("test-media"))


class FakeHass:
    config = FakeConfig()

    def __init__(self):
        self.scheduled = 0

    def async_create_task(self, coroutine):
        self.scheduled += 1
        coroutine.close()


class FakeCos:
    def try_signed_url(self, device_id, uid, key):
        return f"https://example.invalid/{key}"

    def cached_credentials(self, device_id):
        return object()


class LockMediaManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load_module()

    def test_attach_urls_uses_cached_signature_and_schedules_snapshot(self) -> None:
        hass = FakeHass()
        manager = self.module.LockMediaManager(
            hass, {"lock": {"uid": "uid"}}, b"salt"
        )
        manager.cos = FakeCos()

        result = manager.attach_urls(
            "lock", {}, {"kind": "ring", "pic_url": "picture.jpg", "time": 1}
        )

        self.assertEqual(
            result["pic_media_url"], "https://example.invalid/picture.jpg"
        )
        self.assertEqual(hass.scheduled, 1)

    def test_fetch_video_rejects_unknown_device_before_network(self) -> None:
        manager = self.module.LockMediaManager(FakeHass(), {}, b"salt")
        manager.cos = object()
        manager.archiver = object()

        result = __import__("asyncio").run(
            manager.fetch_video("missing", "video.h264")
        )

        self.assertEqual(result, {"error": "设备不存在或不是门锁"})


if __name__ == "__main__":
    unittest.main()
