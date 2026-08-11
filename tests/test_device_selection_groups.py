from __future__ import annotations

import importlib.util
from enum import Enum
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


MODULE_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "orvibohomebridge"
    / "device_selection.py"
)


def _module(name: str, **values) -> ModuleType:
    module = ModuleType(name)
    for key, value in values.items():
        setattr(module, key, value)
    return module


def _load_module():
    package_name = "orvibohomebridge_device_selection_test"
    package = _module(package_name)
    package.__path__ = [str(MODULE_PATH.parent)]

    class DeviceCategory(Enum):
        DOOR_LOCK = "door_lock"
        OTHER = "other"

    def classify_device(device):
        return (
            DeviceCategory.DOOR_LOCK
            if device.get("device_type_raw") == 522
            else DeviceCategory.OTHER
        )

    def capability_for(device):
        platforms = {
            "light": frozenset({"light"}),
            "switch": frozenset({"switch"}),
            "cover": frozenset({"cover"}),
            "climate": frozenset({"climate"}),
            "sensor": frozenset({"sensor"}),
        }.get(device.get("device_type"), frozenset())
        return SimpleNamespace(platforms=platforms)

    modules = {
        package_name: package,
        f"{package_name}.capabilities": _module(
            f"{package_name}.capabilities", capability_for=capability_for
        ),
        f"{package_name}.device_types": _module(
            f"{package_name}.device_types",
            DeviceCategory=DeviceCategory,
            classify_device=classify_device,
        ),
        f"{package_name}.selection": _module(
            f"{package_name}.selection",
            CONF_SELECTED_DEVICE_IDS="selected_device_ids",
        ),
    }
    name = f"{package_name}.device_selection"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


device_selection = _load_module()
device_selection_groups = device_selection.device_selection_groups
infer_group = device_selection.infer_group
merge_grouped_selection = device_selection.merge_grouped_selection


def _device(device_id: str, device_type: str, raw_type: int) -> dict:
    return {
        "device_id": device_id,
        "device_name": device_id,
        "device_type": device_type,
        "device_type_raw": raw_type,
    }


class DeviceSelectionGroupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.devices = [
            _device("light-1", "light", 38),
            _device("switch-1", "switch", 102),
            _device("cover-1", "cover", 34),
            _device("ac-1", "climate", 36),
            _device("sensor-1", "sensor", 26),
            _device("lock-1", "sensor", 522),
            _device("other-1", "unknown", 9999),
        ]

    def test_devices_are_assigned_to_six_broad_groups(self):
        self.assertEqual(infer_group(self.devices[0]), "lights")
        self.assertEqual(infer_group(self.devices[1]), "lights")
        self.assertEqual(infer_group(self.devices[2]), "covers")
        self.assertEqual(infer_group(self.devices[3]), "climate")
        self.assertEqual(infer_group(self.devices[4]), "sensors")
        self.assertEqual(infer_group(self.devices[5]), "locks")
        self.assertEqual(infer_group(self.devices[6]), "other")
        self.assertEqual(
            [group.label for group in device_selection_groups(self.devices)],
            ["灯光", "窗帘", "空调", "传感器", "门锁", "其他"],
        )

    def test_group_all_selects_every_device_in_group(self):
        selected = merge_grouped_selection(
            {"device_group_lights": ["__all__:lights"]}, self.devices
        )
        self.assertEqual(selected, ["light-1", "switch-1"])

    def test_individual_selection_cancels_group_all(self):
        selected = merge_grouped_selection(
            {
                "device_group_lights": [
                    "__all__:lights",
                    "light-1",
                ]
            },
            self.devices,
        )
        self.assertEqual(selected, ["light-1"])

    def test_individual_devices_can_be_selected_across_groups(self):
        selected = merge_grouped_selection(
            {
                "device_group_lights": ["switch-1"],
                "device_group_sensors": ["sensor-1"],
                "device_group_locks": ["lock-1"],
            },
            self.devices,
        )
        self.assertEqual(selected, ["switch-1", "sensor-1", "lock-1"])


if __name__ == "__main__":
    unittest.main()
