"""Tests for hardware-verified and conservative device profiles."""

from __future__ import annotations

import importlib
from pathlib import Path
import sys
import types
import unittest

COMPONENT_PATH = Path(__file__).parents[1] / "custom_components" / "orvibohomebridge"


def _load_module():
    package_name = "orvibohomebridge_profiles_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT_PATH)]
    sys.modules[package_name] = package
    return importlib.import_module(f"{package_name}.device_types")


class DeviceProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load_module()

    def test_readme_device_types_are_hardware_verified(self) -> None:
        verified_devices = (
            {"device_type_raw": value}
            for value in (
                1, 25, 26, 27, 34, 36, 38, 46, 52, 54, 56,
                102, 501, 502, 503, 516, 518, 522,
            )
        )
        verified_devices = (*verified_devices, {
            "device_type_raw": 300,
            "sub_device_type": 491,
        })
        for device in verified_devices:
            with self.subTest(device=device):
                profile = self.module.get_device_profile(device)
                self.assertTrue(profile.hardware_verified)
                self.assertFalse(profile.registration_only)

    def test_unknown_device_is_registration_only(self) -> None:
        profile = self.module.get_device_profile({"device_type_raw": 999999})
        self.assertEqual(profile.category, self.module.DeviceCategory.UNKNOWN)
        self.assertFalse(profile.hardware_verified)
        self.assertTrue(profile.registration_only)

    def test_known_but_unlisted_device_is_not_claimed_as_verified(self) -> None:
        profile = self.module.get_device_profile({"device_type_raw": 35})
        self.assertEqual(
            profile.category,
            self.module.DeviceCategory.ZIGBEE_ROLLING_SHUTTER,
        )
        self.assertFalse(profile.hardware_verified)
        self.assertFalse(profile.registration_only)


if __name__ == "__main__":
    unittest.main()
