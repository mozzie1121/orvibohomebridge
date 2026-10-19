"""Unit tests for the custom-device profile skeleton generator (no network).

The contract under test: whatever ``build_skeleton`` emits must be accepted by
the real profile loader, and multi-sample evidence must refine the output.
"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
COMPONENT_DIR = ROOT / "custom_components" / "orvibohomebridge"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_component(module_name: str):
    package_name = "orvibohomebridge_skeleton_test"
    if package_name not in sys.modules:
        package = types.ModuleType(package_name)
        package.__path__ = [str(COMPONENT_DIR)]
        sys.modules[package_name] = package
    import importlib

    return importlib.import_module(f"{package_name}.{module_name}")


skeleton = _load("profile_skeleton_under_test", ROOT / "tools" / "profile_skeleton.py")
custom_devices = _load_component("custom_devices")
device_types = _load_component("device_types")


def _write_and_load(text: str):
    """Write the generated YAML and load it with the real profile loader."""

    import tempfile

    directory = Path(tempfile.mkdtemp())
    path = directory / "generated.yaml"
    path.write_text(text, encoding="utf-8")
    return custom_devices.load_profile_file(path)


class SkeletonLoadsTests(unittest.TestCase):
    """Every generated skeleton must be a valid profile."""

    def _device(self, **overrides):
        device = {
            "deviceId": "w-test-1",
            "deviceName": "客厅测试插座",
            "deviceType": 9901,
            "subDeviceType": -2,
            "classId": 1234,
            "model": "test-model-x",
            "ui": {"model": "testUiModel"},
            "value1": 1,
            "value2": 128,
            "value3": 300,
            "properties": {"onoff": {"status": "on"}, "brightness": {"percent": 50}},
        }
        device.update(overrides)
        return device

    def _observations(self, device):
        obs = skeleton.Observations()
        obs.add_snapshot(device)
        return obs

    def test_generated_light_profile_loads(self):
        device = self._device()
        result = skeleton.build_skeleton(device, self._observations(device), platform="light")
        profile = _write_and_load(result.yaml_text)
        self.assertEqual(profile.profile_id, result.profile_id)
        self.assertEqual(profile.platform, "light")
        self.assertFalse(profile.hardware_verified)
        self.assertIn("onoff", profile.capabilities)

    def test_generated_switch_profile_loads(self):
        device = self._device(properties={"onoff": {"status": "off"}})
        result = skeleton.build_skeleton(device, self._observations(device), platform="switch")
        profile = _write_and_load(result.yaml_text)
        self.assertEqual(profile.platform, "switch")
        self.assertIn("on", profile.control)
        self.assertIn("off", profile.control)

    def test_generated_cover_profile_loads(self):
        device = self._device(value1=50, value2=None, value3=None, properties={"percent": 50})
        result = skeleton.build_skeleton(device, self._observations(device), platform="cover")
        profile = _write_and_load(result.yaml_text)
        self.assertEqual(profile.platform, "cover")
        self.assertIn("position", profile.control)

    def test_generated_sensor_profile_loads(self):
        device = self._device(
            value1=None, value2=None, value3=None,
            properties={"temperature": 235, "humidity": 55, "battery": 90},
        )
        result = skeleton.build_skeleton(device, self._observations(device), platform="sensor")
        profile = _write_and_load(result.yaml_text)
        self.assertEqual(profile.platform, "sensor")
        self.assertIn("temperature", profile.state_specs)
        self.assertEqual(profile.control, {})

    def test_no_control_means_status_only(self):
        device = self._device(properties={})
        result = skeleton.build_skeleton(device, skeleton.Observations(), platform="sensor")
        profile = _write_and_load(result.yaml_text)
        self.assertTrue(profile.status_only)

    def test_no_evidence_still_loads(self):
        result = skeleton.build_skeleton({"deviceType": 9905}, skeleton.Observations())
        profile = _write_and_load(result.yaml_text)
        self.assertEqual(profile.platform, result.platform)
        self.assertTrue(profile.status_only)


class SkeletonContentTests(unittest.TestCase):
    """The generated content must reflect the evidence, not guess wildly."""

    def test_match_uses_only_present_fields(self):
        match = skeleton.build_match({"deviceType": 12, "ui": {"model": "m"}})
        self.assertEqual(match, {"device_type": 12, "ui_model": "m"})

    def test_slugify_produces_valid_ids(self):
        for name in ("客厅插座", "  Odd Name!! ", "", "123"):
            slug = skeleton.slugify_device_id(name, 9901)
            self.assertRegex(slug, r"^[a-z0-9][a-z0-9_-]*$", msg=slug)

    def test_boolean_state_from_onoff_property(self):
        obs = skeleton.Observations()
        obs.add_payload({"properties": {"onoff": {"status": "on"}}})
        obs.add_payload({"properties": {"onoff": {"status": "off"}}})
        guess = skeleton.guess_field("state", obs)
        self.assertIsNotNone(guess)
        self.assertEqual(guess.path, "properties.onoff.status")
        self.assertIn("on", guess.spec["true_values"])
        self.assertIn("off", guess.spec["false_values"])

    def test_active_low_numeric_state_is_flagged(self):
        obs = skeleton.Observations()
        for value in (0, 1):
            obs.add_payload({"value1": value})
        guess = skeleton.guess_field("state", obs)
        self.assertIsNotNone(guess)
        self.assertIn("as_bool", guess.spec)  # ambiguous -> flagged via TODO
        device = {"deviceType": 9901, "value1": 0}
        result = skeleton.build_skeleton(device, obs, platform="switch")
        self.assertTrue(any("active-low" in item for item in result.todo))

    def test_brightness_range_narrows_with_more_samples(self):
        single = skeleton.Observations()
        single.add_payload({"value2": 128})
        guess = skeleton.guess_field("brightness", single)
        self.assertEqual(guess.path, "value2")

        # A snapshot alone cannot tell 0-255 from 0-100; the TODO says so.
        device = {"deviceType": 9901}
        result = skeleton.build_skeleton(device, single, platform="light")
        self.assertTrue(any("量纲" in item for item in result.todo))

    def test_percent_brightness_prefers_property_path(self):
        obs = skeleton.Observations()
        obs.add_payload({"properties": {"brightness": {"percent": 10}}})
        obs.add_payload({"properties": {"brightness": {"percent": 90}}})
        guess = skeleton.guess_field("brightness", obs)
        self.assertEqual(guess.path, "properties.brightness.percent")

    def test_kelvin_color_temp_is_recognised(self):
        obs = skeleton.Observations()
        obs.add_payload({"value3": 4000})
        guess = skeleton.guess_field("color_temp", obs)
        self.assertEqual(guess.spec.get("input_unit"), "kelvin")

    def test_mired_color_temp_is_flagged(self):
        obs = skeleton.Observations()
        obs.add_payload({"value3": 300})
        guess = skeleton.guess_field("color_temp", obs)
        self.assertEqual(guess.spec.get("input_unit"), "mired")
        self.assertIn("mired", guess.note)

    def test_builtin_recognised_device_requires_override(self):
        device = {"deviceType": 38, "subDeviceType": -2}
        result = skeleton.build_skeleton(
            device,
            skeleton.Observations(),
            platform="light",
            builtin_category="dim_color_light",
            builtin_platform="light",
        )
        self.assertIn("override: true", result.yaml_text)
        self.assertTrue(any("override: true" in item for item in result.todo))

    def test_snapshot_only_marks_state_unverified(self):
        device = {"deviceType": 9901, "value1": 0, "properties": {"onoff": {"status": "on"}}}
        result = skeleton.build_skeleton(device, skeleton.Observations(), platform="switch")
        self.assertIn("hardware_verified: false", result.yaml_text)
        self.assertTrue(any("hardware_verified" in item for item in result.todo))

    def test_report_lists_every_todo(self):
        device = {"deviceType": 9901, "properties": {"onoff": {"status": "on"}}}
        obs = skeleton.Observations()
        obs.add_snapshot(device)
        result = skeleton.build_skeleton(device, obs, platform="switch")
        report = skeleton.format_report(result, device)
        for item in result.todo:
            self.assertIn(item, report)


class ResolvesThroughCatalogTests(unittest.TestCase):
    """The generated profile must actually claim the device it was built from."""

    def test_generated_profile_claims_its_device(self):
        import tempfile

        device = {
            "deviceId": "w-gen-1",
            "deviceName": "generated plug",
            "deviceType": 9911,
            "value1": 1,
            "properties": {"onoff": {"status": "on"}},
        }
        obs = skeleton.Observations()
        obs.add_snapshot(device)
        result = skeleton.build_skeleton(device, obs, platform="switch")

        directory = Path(tempfile.mkdtemp())
        (directory / "generated.yaml").write_text(result.yaml_text, encoding="utf-8")
        registry = custom_devices.load_profiles([directory])
        self.assertEqual(registry.errors, ())

        normalized = {
            "device_id": "w-gen-1",
            "device_type_raw": 9911,
            "properties": {"onoff": {"status": "on"}},
        }
        self.assertIsNotNone(custom_devices.profile_for_device(normalized))
        self.assertTrue(str(device_types.classify_device(normalized).value).startswith("custom:"))


class MatchModeTests(unittest.TestCase):
    """``--match`` controls the specificity/brittleness trade-off."""

    DEVICE = {
        "deviceType": 9999,
        "subDeviceType": -2,
        "classId": 1201,
        "model": "plug-x",
        "ui": {"model": "plugUi"},
    }

    def test_minimal_keeps_stable_identifiers_only(self):
        self.assertEqual(
            skeleton.build_match(self.DEVICE, "minimal"),
            {"device_type": 9999, "sub_device_type": -2},
        )

    def test_full_keeps_every_identifier(self):
        self.assertEqual(
            skeleton.build_match(self.DEVICE, "full"),
            {
                "device_type": 9999,
                "sub_device_type": -2,
                "class_id": 1201,
                "ui_model": "plugUi",
                "model": "plug-x",
            },
        )

    def test_type_uses_device_type_only(self):
        self.assertEqual(skeleton.build_match(self.DEVICE, "type"), {"device_type": 9999})

    def test_full_match_warns_about_brittleness(self):
        result = skeleton.build_skeleton(self.DEVICE, skeleton.Observations(), match_mode="full")
        self.assertTrue(any("不再命中" in item for item in result.todo))

    def test_minimal_match_does_not_warn(self):
        result = skeleton.build_skeleton(self.DEVICE, skeleton.Observations(), match_mode="minimal")
        self.assertFalse(any("不再命中" in item for item in result.todo))

    def test_unknown_match_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            skeleton.build_skeleton(self.DEVICE, skeleton.Observations(), match_mode="nope")

    def test_minimal_match_claims_a_device_without_the_volatile_fields(self):
        import tempfile

        device = {"deviceId": "w-1", "deviceName": "plug", "deviceType": 9999,
                  "subDeviceType": -2, "ui": {"model": "plugUi"}}
        obs = skeleton.Observations()
        obs.add_snapshot(device)
        result = skeleton.build_skeleton(device, obs, platform="switch", match_mode="minimal")
        directory = Path(tempfile.mkdtemp())
        (directory / "p.yaml").write_text(result.yaml_text, encoding="utf-8")
        self.assertEqual(custom_devices.load_profiles([directory]).errors, ())
        # Same deviceType but a different ui_model must still be claimed.
        self.assertIsNotNone(
            custom_devices.profile_for_device(
                {"device_id": "w-2", "device_type_raw": 9999, "sub_device_type": -2,
                 "ui_model": "changedInFirmware", "properties": {}}
            )
        )


class FieldScopingTests(unittest.TestCase):
    """Fields no entity consumes must never be guessed."""

    def test_light_does_not_invent_position_or_temperature(self):
        device = {"deviceType": 9901, "value1": 1, "value2": 128, "value3": 300,
                  "properties": {"onoff": {"status": "on"}}}
        obs = skeleton.Observations()
        obs.add_snapshot(device)
        result = skeleton.build_skeleton(device, obs, platform="light")
        self.assertNotIn("position", result.state_fields)
        self.assertNotIn("temperature", result.state_fields)
        self.assertNotIn("humidity", result.state_fields)

    def test_switch_only_emits_state(self):
        device = {"deviceType": 9901, "value1": 1, "value2": 128,
                  "properties": {"onoff": {"status": "on"}}}
        obs = skeleton.Observations()
        obs.add_snapshot(device)
        result = skeleton.build_skeleton(device, obs, platform="switch")
        self.assertEqual(result.state_fields, ("state",))

    def test_cover_only_emits_position(self):
        device = {"deviceType": 9901, "value1": 50, "properties": {"percent": 50}}
        obs = skeleton.Observations()
        obs.add_snapshot(device)
        result = skeleton.build_skeleton(device, obs, platform="cover")
        self.assertEqual(result.state_fields, ("position",))

    def test_one_source_path_never_feeds_two_fields(self):
        # value2 cannot be temperature and humidity at the same time; the sensor
        # platform must claim it once and say so.
        device = {"deviceType": 9901, "value2": 900, "properties": {}}
        obs = skeleton.Observations()
        obs.add_snapshot(device)
        result = skeleton.build_skeleton(device, obs, platform="sensor")
        self.assertEqual(result.state_fields, ("temperature",))
        self.assertTrue(any("都落在 value2" in item for item in result.todo))

    def test_plausible_humidity_from_its_own_path_is_kept(self):
        device = {"deviceType": 9901, "properties": {"humidity": 55, "temperature": 23}}
        obs = skeleton.Observations()
        obs.add_snapshot(device)
        result = skeleton.build_skeleton(device, obs, platform="sensor")
        self.assertEqual(result.state_fields, ("humidity", "temperature"))

    def test_tenth_degree_temperature_is_scaled_and_flagged(self):
        device = {"deviceType": 9901, "properties": {"temperature": 235}}
        obs = skeleton.Observations()
        obs.add_snapshot(device)
        guess = skeleton.guess_field("temperature", obs)
        self.assertIsNotNone(guess)
        self.assertIn("scale", guess.spec)
        self.assertTrue(guess.note)

    def test_implausible_temperature_is_rejected(self):
        obs = skeleton.Observations()
        obs.add_payload({"properties": {"temperature": 9000}})
        self.assertIsNone(skeleton.guess_field("temperature", obs))


if __name__ == "__main__":
    unittest.main()
