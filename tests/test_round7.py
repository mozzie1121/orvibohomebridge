"""Round 7 (P2): 解析器/属性 int() 保护、_apply_generic 缺省映射、cover 未知位置。"""

from __future__ import annotations

import importlib
from pathlib import Path
import sys
import types
import unittest

COMPONENT_PATH = Path(__file__).parents[1] / "custom_components" / "orvibohomebridge"


def _module(name: str, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _load_parsers_light():
    package_name = "orvibohomebridge_p2_light"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT_PATH)]
    sys.modules[package_name] = package
    base = importlib.import_module(f"{package_name}.parsers.base")
    light = importlib.import_module(f"{package_name}.parsers.light")
    return base, light


class ToIntTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base, _ = _load_parsers_light()

    def test_to_int_guards_bad_types(self) -> None:
        to_int = self.base.to_int
        self.assertIsNone(to_int(None))
        self.assertIsNone(to_int(True))
        self.assertIsNone(to_int({"value": 5}))
        self.assertIsNone(to_int([5]))
        self.assertIsNone(to_int("abc"))
        self.assertEqual(to_int(42), 42)
        self.assertEqual(to_int("42"), 42)
        self.assertEqual(to_int(42.0), 42)


class ParserCrashResistanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _, cls.light = _load_parsers_light()

    def test_fast_move_parser_survives_dict_values(self) -> None:
        """P2: value1/value2 为 dict（历史 int(dict) 崩溃点）不再抛异常。"""
        patch = self.light.parse_fast_move_dim_color_light(
            {"state": False},
            {"value1": {"value": 1}, "value2": {"value": 50}, "value3": 270},
        )
        values = dict(patch.values)
        self.assertNotIn("state", values)  # 无法解析时不写
        self.assertNotIn("brightness", values)
        self.assertEqual(values["color_temp"], 3703)  # mired→kelvin 仍正常

    def test_fast_move_parser_normal_values_still_work(self) -> None:
        patch = self.light.parse_fast_move_dim_color_light(
            {"state": False},
            {"value1": 0, "value2": 128, "value3": 300},
        )
        values = dict(patch.values)
        self.assertIs(values["state"], True)
        self.assertEqual(values["brightness"], 128)
        self.assertEqual(values["color_temp"], 3333)


class GenericFallbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        package_name = "orvibohomebridge_p2_sd"
        package = types.ModuleType(package_name)
        package.__path__ = [str(COMPONENT_PATH)]
        sys.modules[package_name] = package

        class DeviceCategory:
            UNKNOWN = "unknown"

        _module(
            f"{package_name}.device_types",
            DeviceCategory=DeviceCategory,
            classify_device=lambda _d: DeviceCategory.UNKNOWN,
        )
        _module(f"{package_name}.parsers", get_state_parser=lambda _c: None)
        class StateSource:
            INITIAL = 0
            OPTIMISTIC = 10
            CLOUD = 20
            SSL = 30
            LAN = 40

        _module(
            f"{package_name}.state_store",
            StateSource=StateSource,
            StateStore=object,
        )
        cls.sd = importlib.import_module(f"{package_name}.status_dispatcher")

    def test_generic_missing_fields_not_written_as_defaults(self) -> None:
        """P2-M4: 缺失字段不再映射成 state=False/brightness=None。"""
        state: dict = {"state": True, "brightness": 80}
        self.sd.StatusUpdateDispatcher._apply_generic(
            state, {"properties": {}, "value1": None, "value2": None, "value3": None}
        )
        self.assertIs(state["state"], True)  # 未被覆盖为 False
        self.assertEqual(state["brightness"], 80)  # 未被清成 None

    def test_generic_present_fields_written(self) -> None:
        state: dict = {}
        self.sd.StatusUpdateDispatcher._apply_generic(
            state,
            {
                "properties": {"onoff": {"status": "on"}},
                "value1": 50,
                "value2": None,
                "value3": None,
            },
        )
        self.assertIs(state["state"], True)
        self.assertEqual(state["position"], 50)


if __name__ == "__main__":
    unittest.main()
