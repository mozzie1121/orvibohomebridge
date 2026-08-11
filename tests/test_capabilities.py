"""Tests for the unified device capability registry."""

from __future__ import annotations

import importlib
from pathlib import Path
import sys
import types
import unittest

COMPONENT_PATH = Path(__file__).parents[1] / "custom_components" / "orvibohomebridge"


def _load_module():
    package_name = "orvibohomebridge_capabilities_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT_PATH)]
    sys.modules[package_name] = package
    return importlib.import_module(f"{package_name}.capabilities")


class CapabilityRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cap = _load_module()

    def test_locks_are_cloud_only_and_status_only(self) -> None:
        for device_type, sub_type in ((107, None), (522, 463)):
            with self.subTest(device_type=device_type):
                cap = self.cap.capability_for_type(device_type, sub_type)
                self.assertTrue(cap.status_only)
                self.assertTrue(cap.cloud_only)
                self.assertEqual(cap.channels, frozenset())
                self.assertFalse(cap.controllable)

    def test_type300_follows_verified_cloud_device_semantics(self) -> None:
        # 300/491 温湿度传感器：只读、不云专属
        sensor = self.cap.capability_for_type(300, 491)
        self.assertTrue(sensor.status_only)
        self.assertFalse(sensor.cloud_only)
        self.assertEqual(sensor.channels, frozenset())
        # 300/481 地暖：可云控、不云专属（云端实测定义）
        heating = self.cap.capability_for_type(300, 481)
        self.assertFalse(heating.status_only)
        self.assertFalse(heating.cloud_only)
        self.assertEqual(
            heating.channels, frozenset({self.cap.ControlChannel.SSL})
        )

    def test_lan_state_allowed_filters_cloud_only_devices(self) -> None:
        self.assertFalse(
            self.cap.lan_state_allowed({"device_type_raw": 522, "sub_device_type": 463})
        )
        self.assertFalse(
            self.cap.lan_state_allowed({"device_type_raw": 52})
        )
        self.assertTrue(
            self.cap.lan_state_allowed({"device_type_raw": 38})
        )

    def test_cloud_only_mode_filters_every_lan_state(self) -> None:
        self.assertFalse(
            self.cap.lan_state_allowed(
                {"device_type_raw": 38},
                self.cap.TransportMode.CLOUD_ONLY,
            )
        )

    def test_lan_only_mode_accepts_lan_state_except_cloud_only_devices(self) -> None:
        self.assertTrue(
            self.cap.lan_state_allowed(
                {"device_type_raw": 38},
                self.cap.TransportMode.LAN_ONLY,
            )
        )
        self.assertFalse(
            self.cap.lan_state_allowed(
                {"device_type_raw": 522, "sub_device_type": 463},
                self.cap.TransportMode.LAN_ONLY,
            )
        )

    def test_transport_path_matches_mode_and_device_capability(self) -> None:
        lan_light = {"device_type_raw": 38}
        cloud_lock = {"device_type_raw": 522, "sub_device_type": 463}
        cloud_heating = {"device_type_raw": 300, "sub_device_type": 481}
        status_sensor = {"device_type_raw": 26}

        self.assertEqual(
            self.cap.transport_path_for(lan_light),
            self.cap.TransportPath.LAN_CLOUD,
        )
        self.assertEqual(
            self.cap.transport_path_for(
                lan_light, self.cap.TransportMode.LAN_ONLY
            ),
            self.cap.TransportPath.LAN,
        )
        self.assertEqual(
            self.cap.transport_path_for(
                lan_light, self.cap.TransportMode.CLOUD_ONLY
            ),
            self.cap.TransportPath.CLOUD,
        )
        self.assertEqual(
            self.cap.transport_path_for(cloud_lock),
            self.cap.TransportPath.CLOUD,
        )
        self.assertEqual(
            self.cap.transport_path_for(
                cloud_lock, self.cap.TransportMode.LAN_ONLY
            ),
            self.cap.TransportPath.UNAVAILABLE,
        )
        self.assertEqual(
            self.cap.transport_path_for(
                cloud_heating, self.cap.TransportMode.LAN_ONLY
            ),
            self.cap.TransportPath.UNAVAILABLE,
        )
        self.assertEqual(
            self.cap.transport_path_for(status_sensor),
            self.cap.TransportPath.LAN_CLOUD,
        )

    def test_door_lock_platforms_include_camera(self) -> None:
        cap = self.cap.capability_for_type(522, 463)
        self.assertIn(self.cap.PLATFORM_SENSOR, cap.platforms)
        self.assertIn(self.cap.PLATFORM_BINARY_SENSOR, cap.platforms)
        self.assertIn(self.cap.PLATFORM_CAMERA, cap.platforms)

    def test_clothes_horse_is_cloud_only_but_controllable(self) -> None:
        cap = self.cap.capability_for_type(52)
        self.assertTrue(cap.cloud_only)
        self.assertFalse(cap.status_only)
        self.assertEqual(cap.channels, frozenset({self.cap.ControlChannel.SSL}))

    def test_lan_controllable_types_prefer_lan_with_ssl_fallback(self) -> None:
        subtypes = {
            501: 426,  # lightStd
            502: 431,  # dimmable
            503: 436,  # colorTempLightStd
        }
        for device_type in (0, 1, 34, 35, 36, 38, 81, 102, 501, 502, 503, 516):
            with self.subTest(device_type=device_type):
                cap = self.cap.capability_for_type(
                    device_type, subtypes.get(device_type)
                )
                self.assertIn(self.cap.ControlChannel.LAN, cap.channels)
                self.assertIn(self.cap.ControlChannel.SSL, cap.channels)
                self.assertFalse(cap.status_only)
                self.assertFalse(cap.cloud_only)

    def test_status_only_sensors_have_no_control_channels(self) -> None:
        for device_type in (22, 23, 25, 26, 27, 46, 54, 56):
            with self.subTest(device_type=device_type):
                cap = self.cap.capability_for_type(device_type)
                self.assertTrue(cap.status_only)
                self.assertFalse(cap.cloud_only)
                self.assertEqual(cap.channels, frozenset())

    def test_unknown_or_unverified_devices_are_registration_only(self) -> None:
        for device in ({"device_type_raw": 9999}, {"device_type_raw": 10086}):
            with self.subTest(device=device):
                cap = self.cap.capability_for(device)
                self.assertEqual(cap.channels, frozenset())

    def test_common_platforms_match_lan_profiles(self) -> None:
        expected = {
            1: {self.cap.PLATFORM_LIGHT},
            34: {self.cap.PLATFORM_COVER},
            35: {self.cap.PLATFORM_COVER},
            36: {self.cap.PLATFORM_CLIMATE},
            38: {self.cap.PLATFORM_LIGHT},
            46: {self.cap.PLATFORM_SENSOR, self.cap.PLATFORM_BINARY_SENSOR},
            516: {self.cap.PLATFORM_FAN},
        }
        for device_type, platforms in expected.items():
            with self.subTest(device_type=device_type):
                self.assertEqual(
                    self.cap.capability_for_type(device_type).platforms,
                    frozenset(platforms),
                )


if __name__ == "__main__":
    unittest.main()
