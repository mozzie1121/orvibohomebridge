"""摄像头类别不能被隐藏，否则 config flow 与实体平台都拿不到设备。"""

from __future__ import annotations

import importlib
import pathlib
import sys
import types
import unittest

COMPONENT = pathlib.Path(__file__).parents[1] / "custom_components" / "orvibohomebridge"


def _load_device_types():
    """device_types 有包内相对导入，必须按包加载。"""
    package = types.ModuleType("orvibo_hidden_pkg")
    package.__path__ = [str(COMPONENT)]
    sys.modules[package.__name__] = package
    return importlib.import_module(f"{package.__name__}.device_types")


class HiddenCategoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load_device_types()

    def test_camera_categories_are_visible(self) -> None:
        self.assertFalse(self.m.is_hidden_category(self.m.DeviceCategory.WIFI_CAMERA))
        self.assertFalse(self.m.is_hidden_category(self.m.DeviceCategory.CAMERA_NVR))

    def test_gateway_and_base_are_still_hidden(self) -> None:
        for name in ("MIXPAD_GATEWAY", "MIXPAD_4WAY_BASE", "SMART_REMOTE"):
            with self.subTest(category=name):
                self.assertTrue(
                    self.m.is_hidden_category(getattr(self.m.DeviceCategory, name))
                )


if __name__ == "__main__":
    unittest.main()
