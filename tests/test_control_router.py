"""Tests for pure category-to-control-method routing."""

from __future__ import annotations

import importlib
from pathlib import Path
import sys
import types
import unittest

COMPONENT_PATH = Path(__file__).parents[1] / "custom_components" / "orvibohomebridge"


def _load_modules():
    package_name = "orvibohomebridge_control_router_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT_PATH)]
    sys.modules[package_name] = package
    router = importlib.import_module(f"{package_name}.control_router")
    device_types = importlib.import_module(f"{package_name}.device_types")
    return router, device_types


class ControlRouterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.router, cls.device_types = _load_modules()
        cls.category = cls.device_types.DeviceCategory

    def test_dim_color_defaults_are_computed_in_route(self) -> None:
        route = self.router.power_route(
            self.category.DIM_COLOR_LIGHT, True, {"brightness": 0, "color_temp": 0}
        )

        self.assertEqual(route.method, "send_control_light_colortemp")
        self.assertEqual(route.args, (2700,))
        self.assertEqual(route.kwargs, {"brightness": 255})

    def test_fast_move_off_remembers_brightness_and_temperature(self) -> None:
        route = self.router.power_route(
            self.category.FAST_MOVE_DIM_COLOR_LIGHT,
            False,
            {"brightness": 128, "color_temp": 4000},
        )

        self.assertEqual(route.args, (False,))
        self.assertEqual(route.kwargs["brightness"], 128)
        self.assertEqual(route.kwargs["colortemp_mired"], 250)

    def test_ac_route_preserves_mode_speed_and_temperature(self) -> None:
        route = self.router.power_route(
            self.category.FAN_COIL_AC,
            False,
            {"ac_mode_raw": 4, "fan_speed_raw": 3, "value4": 12345},
        )

        self.assertEqual(route.scope, "coordinator_uid")
        self.assertEqual(route.method, "_async_ac_control_raw")
        self.assertEqual(
            route.kwargs,
            {"value1": 1, "value2": 4, "value3": 3, "value4": 12345, "order": "off"},
        )

    def test_cover_and_ventilation_routes_use_distinct_positions(self) -> None:
        cover = self.router.power_route(
            self.category.ZIGBEE_CURTAIN, True, {}
        )
        ventilation = self.router.power_route(
            self.category.VENTILATION_SYSTEM, False, {}
        )

        self.assertEqual(cover.args, (100,))
        self.assertEqual(ventilation.args, (50,))

    def test_legacy_floor_heating_preserves_packed_state_when_powering_off(self) -> None:
        route = self.router.power_route(
            self.category.LEGACY_FLOOR_HEATING,
            False,
            {"raw_value2": 6932},
        )
        self.assertEqual(route.method, "send_control_legacy_floor_heating_power")
        self.assertEqual(route.args, (False,))
        self.assertEqual(route.kwargs, {"packed_state": 6932})

    def test_registration_fallback_is_legacy_light_transport(self) -> None:
        route = self.router.power_route(self.category.OTHER, True, {})

        self.assertEqual(route.scope, "ssl")
        self.assertEqual(route.method, "send_control_light")

    def test_brightness_route_keeps_transport_and_optimistic_units_separate(self) -> None:
        route = self.router.brightness_route(
            self.category.CCT_LIGHT_STRIP,
            50,
            {"color_temp": 4000},
            device_type_raw=503,
        )

        self.assertEqual(route.method, "send_control_cct_light_brightness")
        self.assertEqual(route.args, (50,))
        self.assertEqual(route.optimistic["brightness"], 50)

    def test_fast_move_color_temp_route_converts_to_mired(self) -> None:
        route = self.router.color_temp_route(
            self.category.FAST_MOVE_DIM_COLOR_LIGHT,
            4000,
            {"brightness": 123},
        )

        self.assertEqual(route.args, (123,))
        self.assertEqual(route.kwargs, {"colortemp_mired": 250})
        self.assertEqual(route.optimistic, {"color_temp": 4000})


if __name__ == "__main__":
    unittest.main()
