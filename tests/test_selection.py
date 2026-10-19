"""Tests for config-entry device selection."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest

MODULE_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "orvibohomebridge"
    / "selection.py"
)
SPEC = importlib.util.spec_from_file_location("orvibohomebridge_selection", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
selection = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = selection
SPEC.loader.exec_module(selection)


class SelectionTests(unittest.TestCase):
    def test_legacy_entries_select_all_available_devices(self) -> None:
        self.assertEqual(
            selection.selected_device_ids({}, ["curtain", "light"]),
            {"curtain", "light"},
        )
        self.assertTrue(selection.device_is_selected({}, "curtain"))

    def test_explicit_selection_filters_devices(self) -> None:
        options = {selection.CONF_SELECTED_DEVICE_IDS: ["light", "removed"]}
        self.assertEqual(
            selection.selected_device_ids(options, ["curtain", "light"]),
            {"light"},
        )
        self.assertTrue(selection.device_is_selected(options, "light"))
        self.assertFalse(selection.device_is_selected(options, "curtain"))

    def test_selected_group_includes_new_available_devices(self) -> None:
        devices = {
            "old-ac": {"device_id": "old-ac", "device_type_raw": 36},
            "new-ac": {"device_id": "new-ac", "device_type_raw": 36},
            "light": {"device_id": "light", "device_type_raw": 38},
        }
        options = {
            selection.CONF_SELECTED_DEVICE_IDS: ["old-ac"],
            selection.CONF_SELECTED_DEVICE_GROUPS: ["climate"],
        }

        self.assertEqual(
            selection.selected_device_ids(options, devices),
            {"old-ac", "new-ac"},
        )

    def test_cleaned_selection_options_removes_stale_ids_and_areas(self) -> None:
        options = {
            selection.CONF_SELECTED_DEVICE_IDS: ["light", "removed"],
            selection.CONF_DEVICE_AREAS: {
                "light": "living_room",
                "removed": "old_room",
            },
        }

        self.assertEqual(
            selection.cleaned_selection_options(options, ["light"]),
            {
                selection.CONF_SELECTED_DEVICE_IDS: ["light"],
                selection.CONF_DEVICE_AREAS: {"light": "living_room"},
            },
        )

    def test_empty_selection_adds_no_devices(self) -> None:
        options = {selection.CONF_SELECTED_DEVICE_IDS: []}
        self.assertEqual(selection.selected_device_ids(options, ["light"]), set())
        self.assertFalse(selection.device_is_selected(options, "light"))

    def test_area_mapping_keeps_ids_and_explicit_unassigned_values(self) -> None:
        options = {
            selection.CONF_DEVICE_AREAS: {
                "curtain": "living_room",
                "light": None,
                "invalid": 42,
            }
        }
        self.assertEqual(
            selection.configured_device_areas(options),
            {"curtain": "living_room", "light": None},
        )


if __name__ == "__main__":
    unittest.main()
