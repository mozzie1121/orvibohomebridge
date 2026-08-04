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
        verified_devices = tuple(
            {"device_type_raw": value}
            for value in (
                0, 1, 25, 26, 27, 34, 36, 38, 46, 52, 54, 56, 102, 516, 518,
            )
        ) + (
            {"device_type_raw": 501, "sub_device_type": 426},
            {"device_type_raw": 502, "sub_device_type": 431},
            {"device_type_raw": 503, "sub_device_type": 461},
            {"device_type_raw": 522, "sub_device_type": 463},
            {"device_type_raw": 300, "sub_device_type": 491},
        )
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

    def test_tubular_motor_is_hardware_verified(self) -> None:
        profile = self.module.get_device_profile({"device_type_raw": 35})
        self.assertEqual(
            profile.category,
            self.module.DeviceCategory.ZIGBEE_ROLLING_SHUTTER,
        )
        self.assertTrue(profile.hardware_verified)
        self.assertFalse(profile.registration_only)

    def test_non_controllable_records_are_registration_only(self) -> None:
        for device in (
            {"device_type_raw": 10086, "sub_device_type": -2},
            {"device_type_raw": 300, "sub_device_type": 412},
        ):
            with self.subTest(device=device):
                profile = self.module.get_device_profile(device)
                self.assertTrue(profile.registration_only)

    def test_verified_combination_profiles(self) -> None:
        expected = (
            ({"device_type_raw": 0, "sub_device_type": -2}, self.module.DeviceCategory.ZIGBEE_DIMMABLE_LIGHT),
            ({"device_type_raw": 300, "sub_device_type": 481}, self.module.DeviceCategory.FLOOR_HEATING),
            ({"device_type_raw": 300, "sub_device_type": 491}, self.module.DeviceCategory.TEMP_HUMIDITY_SENSOR),
            ({"device_type_raw": 503, "sub_device_type": 436}, self.module.DeviceCategory.CCT_LIGHT_STRIP),
            ({"device_type_raw": 506, "sub_device_type": 408}, self.module.DeviceCategory.DREAM_CURTAIN),
            ({
                "device_type_raw": 112,
                "sub_device_type": -2,
                "model": "2ac836760da10748856a7e4eafb91efa",
            }, self.module.DeviceCategory.LEGACY_FLOOR_HEATING),
        )
        for device, category in expected:
            with self.subTest(device=device):
                profile = self.module.get_device_profile(device)
                self.assertEqual(profile.category, category)
                self.assertTrue(profile.hardware_verified)

    def test_audited_unsupported_models_are_named_but_not_controllable(self) -> None:
        examples = (
            (63, "bbfed49c738948b989911f9f9f73d759", "隐藏式智能开关"),
            (37, "82c167c95ed746cdbd21d6817f72c593", "多功能控制盒"),
            (152, "141516b297254e8080d98a89b046fded", "人体状态传感器"),
        )
        for device_type, model, label in examples:
            with self.subTest(model=model):
                profile = self.module.get_device_profile({
                    "device_type_raw": device_type,
                    "sub_device_type": -2,
                    "model": model,
                })
                self.assertEqual(profile.category, self.module.DeviceCategory.OTHER)
                self.assertIn(label, profile.info.label)
                self.assertFalse(profile.hardware_verified)
                self.assertTrue(profile.registration_only)

    def test_same_subtype_does_not_grant_another_device_capability(self) -> None:
        profile = self.module.get_device_profile({
            "device_type_raw": 129,
            "sub_device_type": -2,
            "model": "cacb1c9774a24a38bc640d4351192f9a",
        })
        self.assertNotEqual(
            profile.category,
            self.module.DeviceCategory.ZIGBEE_DIMMABLE_LIGHT,
        )

    def test_full_official_model_catalog_is_loaded(self) -> None:
        catalog = importlib.import_module(
            f"{self.module.__package__}.known_device_models"
        )
        self.assertEqual(len(catalog.KNOWN_DEVICE_MODELS), 1144)
        self.assertEqual(
            catalog.KNOWN_DEVICE_MODELS[
                "2ac836760da10748856a7e4eafb91efa"
            ].display_name,
            "地暖控制面板",
        )


if __name__ == "__main__":
    unittest.main()
