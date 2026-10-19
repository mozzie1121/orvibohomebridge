"""light_values 单元测试（unittest，与 CI 的 unittest discover 一致）。

向量来自实测：色温灯8（type 38，value1=0 开 / 1 关，value2=亮度%，value3=色温）、
灯5（properties: onoff/brightness/colorTemp）、以及云端常见的 -1 未使用槽位。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest

COMPONENT = pathlib.Path(__file__).parents[1] / "custom_components" / "orvibohomebridge"


def _load_light_values():
    spec = importlib.util.spec_from_file_location(
        "orvibo_light_values_test", COMPONENT / "light_values.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class LightValuesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load_light_values()

    # ---- brightness ----
    def test_unused_sentinel_is_ignored(self) -> None:
        """云端用 -1 表示槽位未使用，不能变成负亮度。"""
        self.assertIsNone(self.m.normalize_brightness(-1))
        self.assertIsNone(self.m.normalize_brightness(None))
        self.assertIsNone(self.m.normalize_brightness(True))

    def test_brightness_is_clamped_to_range(self) -> None:
        self.assertEqual(self.m.normalize_brightness(0), 0)
        self.assertEqual(self.m.normalize_brightness(128), 128)
        self.assertEqual(self.m.normalize_brightness(300), 255)
        self.assertEqual(self.m.normalize_brightness("50"), 50)
        self.assertEqual(self.m.normalize_brightness("abc"), None)
        self.assertEqual(self.m.normalize_brightness(float("nan")), None)

    def test_percent_to_255(self) -> None:
        self.assertEqual(self.m.percent_to_255(0), 0)
        self.assertEqual(self.m.percent_to_255(100), 255)
        self.assertEqual(self.m.percent_to_255(50), 127)
        self.assertEqual(self.m.percent_to_255(150), 255)
        self.assertIsNone(self.m.percent_to_255(-1))

    # ---- color temperature ----
    def test_color_temp_kelvin_and_mired(self) -> None:
        self.assertEqual(self.m.normalize_color_temp(4000), 4000)
        self.assertEqual(self.m.normalize_color_temp(357), 2801)  # mired -> K
        self.assertEqual(self.m.normalize_color_temp(9000), 6500)  # 钳制
        self.assertEqual(self.m.normalize_color_temp(100), None)   # 非法（<150 且 <1500）
        self.assertIsNone(self.m.normalize_color_temp(-1))

    # ---- active-low ----
    def test_legacy_types_are_active_low(self) -> None:
        """实测：type=38 的灯 value1=0 为开、1 为关。"""
        for device_type in (0, 1, 38, 43, 102, 503):
            with self.subTest(device_type=device_type):
                self.assertTrue(self.m.is_active_low(device_type))
                self.assertIs(self.m.power_state_from_value1(0, device_type), True)
                self.assertIs(self.m.power_state_from_value1(1, device_type), False)

    def test_other_types_are_active_high(self) -> None:
        for device_type in (501, 502, 135, 136, 137, 143, 34):
            with self.subTest(device_type=device_type):
                self.assertFalse(self.m.is_active_low(device_type))
                self.assertIs(self.m.power_state_from_value1(0, device_type), False)
                self.assertIs(self.m.power_state_from_value1(1, device_type), True)

    def test_sub_device_type_minus_two_forces_active_low(self) -> None:
        self.assertTrue(self.m.is_active_low(999, -2))
        self.assertIs(self.m.power_state_from_value1(0, 999, -2), True)

    def test_packed_values_are_unknown_not_false(self) -> None:
        """老面板会报打包值（49/81/4087），不能猜成"关"。"""
        for value in (49, 81, 4087, 6927):
            with self.subTest(value=value):
                self.assertIsNone(self.m.power_state_from_value1(value, 36))
        self.assertIsNone(self.m.power_state_from_value1(None, 38))
        self.assertIsNone(self.m.power_state_from_value1("x", 38))


if __name__ == "__main__":
    unittest.main()
