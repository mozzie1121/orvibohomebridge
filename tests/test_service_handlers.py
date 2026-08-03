"""Tests for service input and response boundary helpers."""

from __future__ import annotations

import importlib
from pathlib import Path
import sys
import types
import unittest

COMPONENT_PATH = Path(__file__).parents[1] / "custom_components" / "orvibohomebridge"


def _load_module():
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    core.ServiceCall = object
    core.SupportsResponse = types.SimpleNamespace(OPTIONAL="optional")
    homeassistant = types.ModuleType("homeassistant")
    sys.modules.setdefault("homeassistant", homeassistant)
    sys.modules["homeassistant.core"] = core

    package_name = "orvibohomebridge_services_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT_PATH)]
    sys.modules[package_name] = package
    return importlib.import_module(f"{package_name}.service_handlers")


class ServiceBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load_module()

    def test_bounded_integer_never_escapes_service_limits(self) -> None:
        bounded = self.module._bounded_int
        self.assertEqual(bounded("bad", 7, 0, 10), 7)
        self.assertEqual(bounded(-5, 7, 0, 10), 0)
        self.assertEqual(bounded(999, 7, 0, 10), 10)

    def test_public_event_does_not_expose_local_path(self) -> None:
        event = {
            "device_id": "lock",
            "kind": "ring",
            "time": "1",
            "type": "image",
            "file": "C:/config/media/private.jpg",
            "media_id": "media-source://example",
        }
        public = self.module._public_event(event)
        self.assertNotIn("file", public)
        self.assertEqual(public["media_id"], "media-source://example")

    def test_list_authorizations_never_returns_password(self) -> None:
        public = self.module._public_authorizations(
            {
                "lock": [
                    {"authorized_id": 1, "password": "123456", "expired": False}
                ]
            }
        )
        self.assertEqual(
            public,
            {"lock": [{"authorized_id": 1, "expired": False}]},
        )


if __name__ == "__main__":
    unittest.main()
