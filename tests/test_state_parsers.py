"""Contract tests for side-effect-free device-state parsers."""

from __future__ import annotations

import importlib
from pathlib import Path
import sys
import types
import unittest

COMPONENT_PATH = Path(__file__).parents[1] / "custom_components" / "orvibohomebridge"


def _load_modules():
    package_name = "orvibohomebridge_parser_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT_PATH)]
    sys.modules[package_name] = package
    parsers = importlib.import_module(f"{package_name}.parsers")
    light = importlib.import_module(f"{package_name}.parsers.light")
    cover = importlib.import_module(f"{package_name}.parsers.cover")
    sensor = importlib.import_module(f"{package_name}.parsers.sensor")
    appliance = importlib.import_module(f"{package_name}.parsers.appliance")
    lock = importlib.import_module(f"{package_name}.parsers.lock")
    device_types = importlib.import_module(f"{package_name}.device_types")
    return parsers, light, cover, sensor, appliance, lock, device_types


class StateParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        (
            cls.parsers,
            cls.light,
            cls.cover,
            cls.sensor,
            cls.appliance,
            cls.lock,
            cls.device_types,
        ) = _load_modules()

    def test_parser_returns_patch_without_mutating_inputs(self) -> None:
        current = {"state": False, "name": "lamp"}
        raw = {"properties": {"onoff": {"status": "on"}}, "value1": 1}

        patch = self.light.parse_light(current, raw)

        self.assertEqual(patch.values, {"state": True})
        self.assertEqual(current, {"state": False, "name": "lamp"})
        self.assertEqual(
            raw, {"properties": {"onoff": {"status": "on"}}, "value1": 1}
        )

    def test_dim_color_light_converts_mireds_and_clamps_kelvin(self) -> None:
        patch = self.light.parse_dim_color_light(
            {}, {"value1": "1", "value2": "128", "value3": "400"}
        )

        self.assertEqual(
            patch.values,
            {"state": True, "brightness": 128, "color_temp": 2700},
        )

    def test_fast_move_zero_brightness_overrides_on_state(self) -> None:
        patch = self.light.parse_fast_move_dim_color_light(
            {"state": True}, {"value1": 0, "value2": -2, "value3": 200}
        )

        self.assertEqual(
            patch.values,
            {"state": False, "brightness": 0, "color_temp": 5000},
        )

    def test_property_dimmable_light_clamps_percentage(self) -> None:
        patch = self.light.parse_dimmable_light(
            {},
            {
                "properties": {
                    "onoff": {"status": "on"},
                    "brightness": {"percent": 120},
                }
            },
        )

        self.assertEqual(patch.values, {"state": True, "brightness": 100})

    def test_cct_light_clamps_color_temperature(self) -> None:
        patch = self.light.parse_cct_light(
            {"state": True},
            {"properties": {"colorTemp": {"value": 8000}}},
        )

        self.assertEqual(patch.values, {"color_temp": 6500})

    def test_zigbee_dimmer_honors_inverted_subdevice(self) -> None:
        patch = self.light.parse_zigbee_dimmable_light(
            {}, {"value1": 0, "value2": 255, "subDeviceType": "-2"}
        )

        self.assertEqual(patch.values, {"state": True, "brightness": 255})

    def test_curtain_preserves_state_at_partial_position(self) -> None:
        patch = self.cover.parse_curtain(
            {"state": True}, {"properties": {"percent": "42"}}
        )

        self.assertEqual(patch.values, {"position": 42, "state": True})

    def test_curtain_rejects_malformed_position(self) -> None:
        patch = self.cover.parse_curtain(
            {"state": True}, {"properties": {"percent": "unknown"}}
        )

        self.assertEqual(patch.values, {"position": None, "state": False})

    def test_switch_uses_inverted_value_for_minus_two_subdevice(self) -> None:
        patch = self.light.parse_switch(
            {}, {"value1": "0", "subDeviceType": "-2"}
        )

        self.assertEqual(patch.values, {"state": True})

    def test_temp_humidity_parser_normalizes_numeric_values(self) -> None:
        patch = self.sensor.parse_temp_humidity_sensor(
            {},
            {
                "properties": {
                    "temperature": {"value": "23.5"},
                    "humidity": "61",
                    "battery": {"power": "89.8"},
                }
            },
        )

        self.assertEqual(
            patch.values,
            {"state": True, "temperature": 23.5, "humidity": 61.0, "battery": 89},
        )

    def test_door_window_parser_preserves_raw_invalid_battery(self) -> None:
        patch = self.sensor.parse_door_window_sensor(
            {}, {"value1": "1", "value4": "unknown"}
        )

        self.assertEqual(
            patch.values,
            {"state": True, "door_state": True, "battery": "unknown"},
        )

    def test_alarm_sensor_parsers_share_the_wire_contract(self) -> None:
        cases = (
            (self.sensor.parse_smoke_sensor, "smoke_detected"),
            (self.sensor.parse_water_leak_sensor, "water_leak_detected"),
            (self.sensor.parse_gas_sensor, "gas_detected"),
        )

        for parser, field in cases:
            with self.subTest(field=field):
                patch = parser({}, {"value1": "1", "value4": "76"})
                self.assertEqual(
                    patch.values, {"state": True, field: True, "battery": 76}
                )

    def test_resettable_sensor_parsers_have_no_scheduler_side_effects(self) -> None:
        motion = self.sensor.parse_motion_sensor(
            {}, {"value3": "1", "value4": "54"}
        )
        emergency = self.sensor.parse_emergency_button(
            {}, {"value1": "1", "value4": "53"}
        )

        self.assertEqual(
            motion.values,
            {"state": True, "motion_detected": True, "battery": 54},
        )
        self.assertEqual(
            emergency.values, {"emergency_state": True, "battery": 53}
        )

    def test_fan_coil_parser_decodes_packed_temperatures(self) -> None:
        value4 = (2250 << 16) | 1980
        patch = self.appliance.parse_fan_coil_ac(
            {}, {"value1": 0, "value2": 3, "value3": 2, "value4": value4}
        )

        self.assertEqual(patch.values["state"], True)
        self.assertEqual(patch.values["ac_mode"], "cool")
        self.assertEqual(patch.values["fan_speed"], "medium")
        self.assertEqual(patch.values["target_temperature"], 22.5)
        self.assertEqual(patch.values["current_temperature"], 19.8)

    def test_ventilation_properties_override_legacy_value(self) -> None:
        patch = self.appliance.parse_ventilation(
            {},
            {
                "value1": 100,
                "properties": {
                    "fanControl": {"fanMode": "off"},
                    "temperature": {"value": "21.5"},
                },
            },
        )

        self.assertEqual(
            patch.values,
            {"value1": 100, "fan_speed": "停", "state": False, "temperature": 21.5},
        )

    def test_clothes_horse_parser_normalizes_all_channels(self) -> None:
        patch = self.appliance.parse_clothes_horse(
            {},
            {
                "motor_state": "down",
                "motor_position": 65,
                "lighting_state": "on",
                "heat_drying_state": "off",
                "wind_drying_state": "on",
                "sterilizing_state": "on",
                "main_switch_state": "on",
            },
        )

        self.assertEqual(patch.values["position"], 65)
        self.assertTrue(patch.values["lighting_state"])
        self.assertTrue(patch.values["wind_drying_state"])
        self.assertTrue(patch.values["state"])

    def test_lock_parser_keeps_fields_missing_from_partial_push(self) -> None:
        current = {
            "locked": True,
            "lock_state": False,
            "door_state": False,
            "inside_lock_state": False,
        }
        patch = self.lock.parse_door_lock(
            current,
            {"properties": {"doorLock": {"insideLockState": "off"}}},
        )

        self.assertNotIn("locked", patch.values)
        self.assertNotIn("door_state", patch.values)
        self.assertTrue(patch.values["inside_lock_state"])
        self.assertFalse(patch.values["state"])

    def test_registry_covers_first_migration_categories(self) -> None:
        categories = self.device_types.DeviceCategory
        expected = {
            categories.SIMPLE_ZIGBEE_LIGHT,
            categories.MONO_LIGHT,
            categories.LIGHT_VIRTUAL_GROUP,
            categories.LEGACY_LIGHT,
            categories.DIM_COLOR_LIGHT,
            categories.FAST_MOVE_DIM_COLOR_LIGHT,
            categories.DIMMABLE_LIGHT,
            categories.ZIGBEE_DIMMABLE_LIGHT,
            categories.CCT_LIGHT_STRIP,
            categories.CCT_LIGHT,
            categories.ZIGBEE_CURTAIN,
            categories.MIX_SWITCH,
            categories.TEMP_HUMIDITY_SENSOR,
            categories.DOOR_WINDOW_SENSOR,
            categories.MOTION_SENSOR,
            categories.EMERGENCY_BUTTON,
            categories.SMOKE_SENSOR,
            categories.WATER_LEAK_SENSOR,
            categories.GAS_SENSOR,
            categories.FAN_COIL_AC,
            categories.VENTILATION_SYSTEM,
            categories.CLOTHES_HORSE,
            categories.DOOR_LOCK,
        }

        self.assertTrue(all(self.parsers.get_state_parser(item) for item in expected))
        self.assertIsNone(self.parsers.get_state_parser(categories.UNKNOWN))


if __name__ == "__main__":
    unittest.main()
