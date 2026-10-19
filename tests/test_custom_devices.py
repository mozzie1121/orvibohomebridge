"""Tests for declarative custom-device profiles.

Covers the profile loader/validator (``custom_devices``), the compiled runtime
behaviour (``custom_control``) and every integration hook that now consults the
custom registry (``device_types`` / ``capabilities`` / ``parsers`` /
``control_router`` / ``protocol`` / ``https_client``).

The component package imports Home Assistant, so the package is faked exactly
like ``tests/test_capabilities.py`` does.
"""

from __future__ import annotations

import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

COMPONENT_PATH = Path(__file__).parents[1] / "custom_components" / "orvibohomebridge"

_BASE_PROFILE = """
profile_version: 1
id: synth_light
display_name: 合成灯
platform: light
match:
  device_type: 9001
hardware_verified: true
capabilities: [onoff]
state:
  state: {from: value1, true_values: [0], false_values: [1]}
control:
  on:
    order: on
"""

_DEVICE_PLATFORM = "orvibohomebridge_custom_devices_test"


def _load_module(module_name: str):
    """Import a component submodule without importing the HA-bound package."""

    if _DEVICE_PLATFORM not in sys.modules:
        package = types.ModuleType(_DEVICE_PLATFORM)
        package.__path__ = [str(COMPONENT_PATH)]
        sys.modules[_DEVICE_PLATFORM] = package
    return importlib.import_module(f"{_DEVICE_PLATFORM}.{module_name}")


def _write(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def _write_json(directory: Path, name: str, payload: object) -> Path:
    path = directory / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _load_temp(directory: Path):
    """Load profiles from exactly one temp directory (never the real config)."""

    return _load_module("custom_devices").load_profiles([directory])


class _CustomDeviceTestBase(unittest.TestCase):
    """Shared imports + temp-directory helpers for every custom-device test."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.custom_devices = _load_module("custom_devices")
        cls.custom_control = _load_module("custom_control")
        cls.device_types = _load_module("device_types")
        cls.capabilities = _load_module("capabilities")
        cls.parsers = _load_module("parsers")
        cls.control_router = _load_module("control_router")
        cls.protocol = _load_module("protocol")
        cls.https_client = _load_module("https_client")

    def setUp(self) -> None:
        # Never let one test inherit another test's registry generation.
        self.custom_devices.load_profiles([])
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.addCleanup(self.custom_devices.load_profiles, [])

    @property
    def tmpdir(self) -> Path:
        return Path(self._tempdir.name)

    # -- helpers ---------------------------------------------------------- #

    def write(self, name: str, text: str) -> Path:
        return _write(self.tmpdir, name, text)

    def write_json(self, name: str, payload: object) -> Path:
        return _write_json(self.tmpdir, name, payload)

    def load(self, directory: Path | None = None):
        return self.custom_devices.load_profiles([directory or self.tmpdir])

    def load_file(self, name: str):
        return self.custom_devices.load_profile_file(self.tmpdir / name)

    def assert_profile_error(self, name: str, *fragments: str):
        """``load_profile_file`` must raise ``ProfileError`` mentioning fragments."""

        with self.assertRaises(self.custom_devices.ProfileError) as ctx:
            self.load_file(name)
        message = str(ctx.exception)
        for fragment in fragments:
            self.assertIn(fragment, message, f"{name}: {message}")
        return message

    def assert_registry_error(self, *fragments: str):
        """Reload and assert the failure is collected instead of raised."""

        registry = self.load()
        self.assertEqual(len(registry.errors), 1, registry.errors)
        for fragment in fragments:
            self.assertIn(fragment, registry.errors[0])
        return registry.errors[0]

    @staticmethod
    def device(name: str, **fields):
        """Device dict with a stable id so registry memoisation counts once."""

        device = {"device_id": name, "device_name": name}
        device.update(fields)
        return device


# --------------------------------------------------------------------------- #
# 1. Loading & validation
# --------------------------------------------------------------------------- #


class ProfileLoadingTests(_CustomDeviceTestBase):
    def test_valid_yaml_profile_loads_and_compiles(self) -> None:
        self.write("a.yaml", _BASE_PROFILE)
        profile = self.load_file("a.yaml")
        self.assertEqual(profile.profile_id, "synth_light")
        self.assertEqual(profile.display_name, "合成灯")
        self.assertEqual(profile.platform, "light")
        self.assertTrue(profile.hardware_verified)
        self.assertEqual(profile.capabilities, frozenset({"onoff"}))
        self.assertTrue(profile.match.matches({"device_type_raw": 9001}))
        self.assertEqual(profile.category_key, "custom:synth_light")

    def test_json_profile_loads(self) -> None:
        self.write_json(
            "a.json",
            {
                "profile_version": 1,
                "id": "json_switch",
                "platform": "switch",
                "match": {"device_type": 9002},
                "hardware_verified": True,
                "state": {"state": {"from": "value1"}},
                "control": {"on": {"order": "on"}, "off": {"order": "off"}},
            },
        )
        registry = self.load()
        self.assertEqual(registry.errors, ())
        self.assertEqual([item.profile_id for item in registry.profiles], ["json_switch"])
        self.assertEqual(self.load_file("a.json").platform, "switch")

    def test_yaml_suffix_variants_are_scanned(self) -> None:
        self.write("a.yml", _BASE_PROFILE)
        registry = self.load()
        self.assertEqual([item.profile_id for item in registry.profiles], ["synth_light"])

    def test_yaml_extension_is_case_insensitive(self) -> None:
        _write(self.tmpdir, "a.YAML", _BASE_PROFILE)
        registry = self.load()
        self.assertEqual([item.profile_id for item in registry.profiles], ["synth_light"])

    def test_missing_profile_version_is_rejected(self) -> None:
        self.write(
            "a.yaml",
            "id: no_version\nplatform: light\nmatch: {device_type: 9001}\n",
        )
        self.assert_profile_error("a.yaml", "profile_version")
        self.assert_registry_error("profile_version")

    def test_wrong_profile_version_is_rejected(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 2\nid: bad_version\nplatform: light\n"
            "match: {device_type: 9001}\n",
        )
        self.assert_profile_error("a.yaml", "profile_version")
        self.assert_registry_error("profile_version")

    def test_bad_profile_id_is_rejected(self) -> None:
        for bad_id in ("SynthLight", "synth light", "_synth", "synth.light", ""):
            with self.subTest(bad_id=bad_id):
                _write(
                    self.tmpdir,
                    "a.yaml",
                    f"profile_version: 1\nid: '{bad_id}'\nplatform: light\n"
                    "match: {device_type: 9001}\n",
                )
                message = self.assert_profile_error("a.yaml")
                self.assertIn("id", message)
                self.assert_registry_error()

    def test_unknown_platform_is_rejected(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: bad_platform\nplatform: toaster\n"
            "match: {device_type: 9001}\n",
        )
        self.assert_profile_error("a.yaml", "platform")
        self.assert_registry_error("platform")

    def test_unknown_state_field_is_rejected(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: bad_state\nplatform: light\n"
            "match: {device_type: 9001}\n"
            "state:\n  illumination: {from: value4}\n",
        )
        self.assert_profile_error("a.yaml", "illumination")
        self.assert_registry_error("illumination")

    def test_unknown_control_action_is_rejected(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: bad_action\nplatform: light\n"
            "match: {device_type: 9001}\n"
            "control:\n  explode:\n    order: on\n",
        )
        self.assert_profile_error("a.yaml", "explode")
        self.assert_registry_error("explode")

    def test_unknown_control_order_is_rejected(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: bad_order\nplatform: light\n"
            "match: {device_type: 9001}\n"
            "control:\n  on:\n    order: self destruct\n",
        )
        self.assert_profile_error("a.yaml", "order")
        self.assert_registry_error("order")

    def test_unknown_match_key_is_rejected(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: bad_match\nplatform: light\n"
            "match:\n  magic_number: 7\n",
        )
        self.assert_profile_error("a.yaml", "magic_number")
        self.assert_registry_error("magic_number")

    def test_empty_match_is_rejected(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: no_match\nplatform: light\nmatch: {}\n",
        )
        self.assert_profile_error("a.yaml", "match")
        self.assert_registry_error("match")

    def test_broken_yaml_syntax_is_reported_not_raised(self) -> None:
        self.write("a.yaml", "profile_version: 1\nid: [unclosed\n  platform: light\n")
        self.assert_profile_error("a.yaml", "a.yaml")
        self.assert_registry_error("a.yaml")
        # The registry stays usable and simply has no profiles.
        registry = self.custom_devices.registry()
        self.assertEqual(registry.profiles, ())

    def test_non_mapping_top_level_is_reported(self) -> None:
        self.write("a.yaml", "- just\n- a\n- list\n")
        self.assert_profile_error("a.yaml", "顶层")
        self.assert_registry_error("顶层")

    def test_unreadable_file_is_reported(self) -> None:
        missing = self.tmpdir / "a.yaml"
        with self.assertRaises(self.custom_devices.ProfileError) as ctx:
            self.custom_devices.load_profile_file(missing)
        self.assertIn("a.yaml", str(ctx.exception))

    def test_unknown_channel_is_rejected(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: bad_channel\nplatform: light\n"
            "match: {device_type: 9001}\nchannels: [lan, telepathy]\n",
        )
        self.assert_profile_error("a.yaml", "telepathy")
        self.assert_registry_error("telepathy")

    def test_channel_scalar_is_accepted(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: scalar_channel\nplatform: light\n"
            "match: {device_type: 9001}\nchannels: ssl\nhardware_verified: true\n"
            "control:\n  on: {order: on}\n",
        )
        profile = self.load_file("a.yaml")
        self.assertEqual(profile.channels, frozenset({"ssl"}))

    def test_cloud_only_forces_ssl_channel(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: cloud_only_light\nplatform: light\n"
            "match: {device_type: 9001}\nchannels: [lan, ssl]\ncloud_only: true\n"
            "hardware_verified: true\ncontrol:\n  on: {order: on}\n",
        )
        self.assertEqual(self.load_file("a.yaml").channels, frozenset({"ssl"}))

    def test_duplicate_id_without_override_is_rejected(self) -> None:
        self.write("a.yaml", _BASE_PROFILE)
        self.write("b.yaml", _BASE_PROFILE.replace("合成灯", "重复灯"))
        registry = self.load()
        self.assertEqual([item.profile_id for item in registry.profiles], ["synth_light"])
        self.assertEqual(len(registry.errors), 1, registry.errors)
        self.assertIn("id 重复", registry.errors[0])
        self.assertIn("override: true", registry.errors[0])
        # The first file wins.
        self.assertEqual(registry.get("synth_light").display_name, "合成灯")

    def test_duplicate_id_with_override_lets_the_later_file_win(self) -> None:
        self.write("a.yaml", _BASE_PROFILE)
        later = _BASE_PROFILE.replace("合成灯", "后到灯").replace(
            "platform: light", "platform: light\noverride: true"
        )
        self.write("b.yaml", later)
        registry = self.load()
        self.assertEqual(registry.errors, ())
        self.assertEqual(len(registry.profiles), 1)
        self.assertEqual(registry.get("synth_light").display_name, "后到灯")
        self.assertTrue(registry.get("synth_light").source.endswith("b.yaml"))
        self.assertTrue(registry.get("synth_light").match.override)

    def test_duplicate_id_regardless_of_nested_override_placement(self) -> None:
        """``override`` is a top-level key; ``match.override`` has no effect."""

        self.write("a.yaml", _BASE_PROFILE)
        nested = _BASE_PROFILE.replace("合成灯", "嵌套灯").replace(
            "match:\n  device_type: 9001",
            "match:\n  device_type: 9001\n  override: true",
        )
        self.write("b.yaml", nested)
        registry = self.load()
        self.assertEqual(len(registry.errors), 1, registry.errors)
        self.assertIn("id 重复", registry.errors[0])
        # The nested key never reaches MatchSpec.override.
        self.assertEqual(registry.get("synth_light").display_name, "合成灯")
        self.assertFalse(registry.get("synth_light").match.override)


# --------------------------------------------------------------------------- #
# 2. YAML 1.1 boolean-key handling (regression)
# --------------------------------------------------------------------------- #


class YAMLBooleanKeyTests(_CustomDeviceTestBase):
    _BOOLEAN_KEY_PROFILE = """
profile_version: 1
id: bool_keys
platform: light
match:
  device_type: 9001
hardware_verified: true
state:
  state:
    from: value1
    true_values: [on]
    false_values: [off]
control:
  on:
    order: on
  off:
    order: off
"""

    def test_bare_on_off_control_keys_and_order_are_normalised(self) -> None:
        self.write("a.yaml", self._BOOLEAN_KEY_PROFILE)
        profile = self.load_file("a.yaml")
        self.assertEqual(set(profile.control), {"on", "off"})
        self.assertEqual(profile.control["on"].order, "on")
        self.assertEqual(profile.control["off"].order, "off")

    def test_quoted_on_off_keys_behave_identically(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: quoted_keys\nplatform: light\n"
            "match: {device_type: 9001}\nhardware_verified: true\n"
            'control:\n  "on":\n    order: "on"\n  "off":\n    order: "off"\n',
        )
        profile = self.load_file("a.yaml")
        self.assertEqual(set(profile.control), {"on", "off"})
        self.assertEqual(profile.control["off"].order, "off")

    def test_yaml_boolean_true_values_still_match_on(self) -> None:
        self.write("a.yaml", self._BOOLEAN_KEY_PROFILE)
        profile = self.load_file("a.yaml")
        spec = profile.state_specs["state"]
        # ``true_values: [on]`` is a YAML 1.1 boolean; it must still match "on".
        self.assertIn("on", spec.true_values)
        self.assertIs(spec.resolve({"value1": "on"}), True)
        self.assertIs(spec.resolve({"value1": "off"}), False)
        self.assertIsNone(spec.resolve({"value1": "unknown"}))

    def test_quoted_map_keys_are_addressable_as_strings(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: quoted_map\nplatform: light\n"
            "match: {device_type: 9001}\n"
            "state:\n  state:\n    from: value1\n"
            "    map:\n      'on': 1\n      'off': 0\n",
        )
        spec = self.load_file("a.yaml").state_specs["state"]
        self.assertEqual(spec.value_map, {"on": 1, "off": 0})
        self.assertEqual(spec.resolve({"value1": "on"}), 1)
        self.assertEqual(spec.resolve({"value1": "OFF"}), 0)
        self.assertIsNone(spec.resolve({"value1": "unknown"}))

    def test_numeric_map_values_are_returned(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: numeric_map\nplatform: light\n"
            "match: {device_type: 9001}\n"
            "state:\n  state:\n    from: value1\n    map:\n      0: 10\n      1: 20\n",
        )
        spec = self.load_file("a.yaml").state_specs["state"]
        self.assertEqual(spec.resolve({"value1": 0}), 10)
        self.assertEqual(spec.resolve({"value1": "1"}), 20)

    def test_boolean_map_values_are_returned(self) -> None:
        """A mapped boolean/string is a final value, not a number to coerce.

        ``map: {'on': true}`` is the obvious spelling for a boolean field and
        must resolve to ``True`` instead of being dropped by numeric coercion.
        """

        self.write(
            "a.yaml",
            "profile_version: 1\nid: bool_map\nplatform: light\n"
            "match: {device_type: 9001}\n"
            "state:\n  state:\n    from: value1\n"
            "    map:\n      'on': true\n      'off': false\n",
        )
        spec = self.load_file("a.yaml").state_specs["state"]
        self.assertEqual(spec.value_map, {"on": True, "off": False})
        self.assertIn("on", spec.value_map)
        self.assertIs(spec.resolve({"value1": "on"}), True)
        self.assertIs(spec.resolve({"value1": "off"}), False)
        # An unmapped payload value still falls back to the declared default.
        self.assertIsNone(spec.resolve({"value1": "unknown"}))

        # The equivalent explicit spelling keeps working.
        self.write(
            "b.yaml",
            "profile_version: 1\nid: bool_values\nplatform: light\n"
            "match: {device_type: 9002}\n"
            "state:\n  state:\n    from: value1\n"
            "    true_values: ['on']\n    false_values: ['off']\n",
        )
        spec = self.load_file("b.yaml").state_specs["state"]
        self.assertIs(spec.resolve({"value1": "on"}), True)
        self.assertIs(spec.resolve({"value1": "off"}), False)

    def test_unquoted_boolean_map_keys_become_string_true_and_false(self) -> None:
        """YAML 1.1 turns an unquoted ``on:`` map *key* into a boolean.

        The key is normalised to ``"true"``, so a payload carrying the string
        ``"on"`` cannot address it.  Quote such keys (or use
        ``true_values``/``false_values``) when the payload uses word literals.
        """

        self.write(
            "a.yaml",
            "profile_version: 1\nid: unquoted_map\nplatform: light\n"
            "match: {device_type: 9001}\n"
            "state:\n  state:\n    from: value1\n    map:\n      on: 1\n      off: 0\n",
        )
        spec = self.load_file("a.yaml").state_specs["state"]
        self.assertEqual(set(spec.value_map), {"true", "false"})
        # A boolean payload still addresses the key ...
        self.assertEqual(spec.resolve({"value1": True}), 1)
        # ... but the obvious string spelling does not.
        self.assertIsNone(spec.resolve({"value1": "on"}))


# --------------------------------------------------------------------------- #
# 3. Matching
# --------------------------------------------------------------------------- #


class ProfileMatchingTests(_CustomDeviceTestBase):
    def _profile(self, name: str, match_yaml: str, *, extra: str = ""):
        self.write(
            name,
            "profile_version: 1\n"
            f"id: {name.replace('.', '_')}\n"
            "platform: light\n"
            f"match:\n{match_yaml}\n"
            f"{extra}",
        )
        return self.load_file(name)

    def test_device_type_matches_normalised_raw_field(self) -> None:
        profile = self._profile("a.yaml", "  device_type: 38")
        self.assertTrue(profile.match.matches({"device_type_raw": 38}))
        self.assertFalse(profile.match.matches({"device_type_raw": 39}))
        # The raw readtable spelling is accepted as an alias.
        self.assertTrue(profile.match.matches({"deviceType": "38"}))

    def test_device_type_accepts_a_list(self) -> None:
        profile = self._profile("a.yaml", "  device_type: [38, 39]")
        self.assertTrue(profile.match.matches({"device_type_raw": 39}))
        self.assertFalse(profile.match.matches({"device_type_raw": 40}))

    def test_sub_device_type_list_is_or(self) -> None:
        profile = self._profile("a.yaml", "  device_type: 9001\n  sub_device_type: [7, 8]")
        self.assertTrue(profile.match.matches({"device_type_raw": 9001, "sub_device_type": 8}))
        self.assertFalse(
            profile.match.matches({"device_type_raw": 9001, "sub_device_type": 9})
        )

    def test_ui_model_model_and_class_name_conditions(self) -> None:
        ui = self._profile("ui.yaml", "  ui_model: ORVIBO_SYNTH")
        self.assertTrue(ui.match.matches({"ui_model": "ORVIBO_SYNTH"}))
        self.assertFalse(ui.match.matches({"ui_model": "OTHER"}))

        model = self._profile("model.yaml", "  model: abc123")
        self.assertTrue(model.match.matches({"model": "abc123"}))
        self.assertFalse(model.match.matches({"model": "abc124"}))

        class_name = self._profile("class_name.yaml", "  class_name: orb_synth")
        self.assertTrue(class_name.match.matches({"class_name": "orb_synth"}))
        self.assertFalse(class_name.match.matches({"class_name": "orb_other"}))

    def test_model_regex_condition(self) -> None:
        profile = self._profile("regex.yaml", "  model: 're:^synth-[0-9]+$'")
        self.assertTrue(profile.match.matches({"model": "synth-42"}))
        self.assertFalse(profile.match.matches({"model": "synth-x"}))
        self.assertFalse(profile.match.matches({}))

    def test_invalid_regex_is_rejected_at_load_time(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: bad_regex\nplatform: light\n"
            "match:\n  model: 're:[unclosed'\n",
        )
        self.assert_profile_error("a.yaml", "正则")
        self.assert_registry_error("正则")

    def test_property_present_condition(self) -> None:
        profile = self._profile("a.yaml", "  property_present: properties.power.status")
        self.assertTrue(
            profile.match.matches({"properties": {"power": {"status": "on"}}})
        )
        self.assertFalse(profile.match.matches({"properties": {"power": {}}}))
        self.assertFalse(profile.match.matches({}))

    def test_property_equals_condition(self) -> None:
        profile = self._profile(
            "a.yaml", "  property_equals:\n    properties.power.mode: eco"
        )
        self.assertTrue(
            profile.match.matches({"properties": {"power": {"mode": "eco"}}})
        )
        self.assertFalse(
            profile.match.matches({"properties": {"power": {"mode": "boost"}}})
        )

    def test_property_equals_accepts_a_status_object(self) -> None:
        # The expected value must be quoted: YAML reads a bare ``on`` as a bool.
        profile = self._profile(
            "a.yaml", "  property_equals:\n    properties.power: 'on'"
        )
        self.assertTrue(profile.match.matches({"properties": {"power": {"status": "on"}}}))
        self.assertFalse(profile.match.matches({"properties": {"power": {"status": "off"}}}))

    def test_priority_decides_between_two_matching_profiles(self) -> None:
        low = _BASE_PROFILE.replace("synth_light", "low_priority").replace(
            "match:\n  device_type: 9001",
            "match:\n  device_type: 9001\n  priority: 1",
        )
        high = _BASE_PROFILE.replace("synth_light", "high_priority").replace(
            "match:\n  device_type: 9001",
            "match:\n  device_type: 9001\n  priority: 9",
        )
        self.write("a_low.yaml", low)
        self.write("b_high.yaml", high)
        registry = self.load()
        self.assertEqual([item.profile_id for item in registry.profiles][0], "high_priority")
        found = registry.find_profile({"device_id": "dev-1", "device_type_raw": 9001})
        self.assertEqual(found.profile_id, "high_priority")

    def test_same_priority_falls_back_to_id_order(self) -> None:
        first = _BASE_PROFILE.replace("synth_light", "aaa_profile")
        second = _BASE_PROFILE.replace("synth_light", "bbb_profile")
        self.write("a.yaml", first)
        self.write("b.yaml", second)
        registry = self.load()
        self.assertEqual(
            [item.profile_id for item in registry.profiles],
            ["aaa_profile", "bbb_profile"],
        )
        found = registry.find_profile({"device_id": "dev-2", "device_type_raw": 9001})
        self.assertEqual(found.profile_id, "aaa_profile")

    def test_any_of_is_or_and_branch_conditions_are_and(self) -> None:
        profile = self._profile(
            "a.yaml",
            "  any_of:\n"
            "    - device_type: 9001\n"
            "      sub_device_type: 5\n"
            "    - ui_model: ORVIBO_SYNTH",
        )
        self.assertTrue(
            profile.match.matches({"device_type_raw": 9001, "sub_device_type": 5})
        )
        self.assertTrue(profile.match.matches({"ui_model": "ORVIBO_SYNTH"}))
        # device_type alone is not enough: the branch is ANDed.
        self.assertFalse(profile.match.matches({"device_type_raw": 9001}))
        self.assertFalse(profile.match.matches({"sub_device_type": 5}))

    def test_flat_match_shorthand_is_a_single_and_branch(self) -> None:
        profile = self._profile("a.yaml", "  device_type: 9001\n  sub_device_type: 5")
        self.assertTrue(
            profile.match.matches({"device_type_raw": 9001, "sub_device_type": 5})
        )
        self.assertFalse(
            profile.match.matches({"device_type_raw": 9001, "sub_device_type": 6})
        )

    def test_priority_scalar_must_be_an_integer(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: bad_priority\nplatform: light\n"
            "match:\n  device_type: 9001\n  priority: soon\n",
        )
        self.assert_profile_error("a.yaml", "priority")
        self.assert_registry_error("priority")


# --------------------------------------------------------------------------- #
# 4. Override protection
# --------------------------------------------------------------------------- #


class OverrideProtectionTests(_CustomDeviceTestBase):
    _BUILTIN_DEVICE = {
        "device_type_raw": 38,
        "sub_device_type": -2,
        "ui_model": "ORVIBO_SYNTH_OVERRIDE",
    }

    def _write_override_pair(self, *, override: bool) -> None:
        flag = "override: true\n" if override else ""
        self.write(
            "a.yaml",
            "profile_version: 1\n"
            "id: overrider\n"
            "platform: switch\n"
            f"{flag}"
            "match:\n  ui_model: ORVIBO_SYNTH_OVERRIDE\n"
            "hardware_verified: true\n"
            "capabilities: [onoff]\n"
            "control:\n  on: {order: on}\n  off: {order: off}\n",
        )

    def test_builtin_recognised_device_is_not_claimed_without_override(self) -> None:
        self._write_override_pair(override=False)
        registry = self.load()
        self.assertEqual(len(registry.profiles), 1)
        # The gate is the caller-supplied built-in verdict: device_types always
        # passes is_builtin_recognised(), and 38/-2 is a built-in combination.
        self.assertTrue(
            self.device_types.is_builtin_recognised(dict(self._BUILTIN_DEVICE))
        )
        self.assertIsNone(
            registry.find_profile(dict(self._BUILTIN_DEVICE), builtin_recognised=True)
        )
        self.assertNotIn("overrider", registry.match_counts)
        # When the flag is omitted the registry derives the built-in verdict
        # itself, so a public helper can never bypass the override guard.
        self.assertIsNone(registry.find_profile(dict(self._BUILTIN_DEVICE)))
        # Only an explicit caller override of the verdict bypasses the guard.
        self.assertIsNotNone(
            registry.find_profile(
                dict(self._BUILTIN_DEVICE), builtin_recognised=False
            )
        )
        self.assertIn("overrider", registry.match_counts)
        # A device the taxonomy does not know is claimed without any flag.
        self.assertIsNotNone(
            registry.find_profile(
                {"device_id": "d-fresh", "ui_model": "ORVIBO_SYNTH_OVERRIDE"}
            )
        )

        category = self.device_types.classify_device(dict(self._BUILTIN_DEVICE))
        self.assertEqual(category, self.device_types.DeviceCategory.DIM_COLOR_LIGHT)
        self.assertFalse(str(category.value).startswith("custom:"))

    def test_override_true_claims_the_builtin_recognised_device(self) -> None:
        self._write_override_pair(override=True)
        registry = self.load()
        profile = registry.find_profile(
            dict(self._BUILTIN_DEVICE), builtin_recognised=True
        )
        self.assertIsNotNone(profile)
        self.assertEqual(profile.profile_id, "overrider")

        category = self.device_types.classify_device(dict(self._BUILTIN_DEVICE))
        self.assertEqual(category.value, "custom:overrider")
        self.assertEqual(
            self.device_types.get_device_profile(dict(self._BUILTIN_DEVICE)).custom_profile,
            profile,
        )

    def test_unrecognised_device_is_claimed_without_override(self) -> None:
        self._write_override_pair(override=False)
        self.load()
        # 38/-2 is a built-in combination, so the override-free profile must not
        # claim it; a genuinely unknown device type is claimed normally.
        self.assertTrue(
            self.device_types.is_builtin_recognised(dict(self._BUILTIN_DEVICE))
        )
        device = {
            "device_id": "d-unknown-type",
            "device_type_raw": 9001,
            "ui_model": "ORVIBO_SYNTH_OVERRIDE",
        }
        self.assertFalse(self.device_types.is_builtin_recognised(device))
        self.assertIsNotNone(self.custom_devices.resolve_profile(device))

    def test_entity_stage_still_selects_the_custom_entity_class(self) -> None:
        """Regression: platform stamping must not hide the profile from entities.

        ``apply_platform`` overwrites ``device_type`` with the profile platform,
        so deriving "built-in recognised" from that field would make the entity
        platforms fall back to the built-in entity classes.  The verdict is
        recorded separately and must be what the override guard uses.
        """

        self.write(
            "a.yaml",
            "profile_version: 1\nid: entity_pick\nplatform: switch\n"
            "hardware_verified: true\nmatch: {device_type: 9001}\n"
            "state:\n  state:\n    from: value1\n",
        )
        self.load()

        # Discovery stage: an item the taxonomy does not know gets stamped.
        stamped: dict = {"deviceType": 9001, "deviceName": "合成开关"}
        stamped.setdefault("device_type", None)
        self.custom_devices.apply_platform(stamped, builtin_platform=None)
        self.assertEqual(stamped["device_type"], "switch")
        self.assertIs(stamped["_builtin_recognised"], False)

        # Entity stage: the very same dict must still resolve to the profile.
        device = {
            "device_id": "d-entity-pick",
            "device_type": stamped["device_type"],
            "device_type_raw": 9001,
            "_builtin_recognised": stamped["_builtin_recognised"],
        }
        profile = self.custom_devices.profile_for_device(device)
        self.assertIsNotNone(profile)
        self.assertEqual(profile.profile_id, "entity_pick")

        # A device the taxonomy DOES know keeps the built-in entity: the stamp is
        # not written for unclaimed devices, and re-derivation recognises 38/-2.
        builtin: dict = {"deviceType": 38, "subDeviceType": -2, "device_id": "d-38"}
        builtin.setdefault("device_type", "light")
        self.custom_devices.apply_platform(builtin, builtin_platform="light")
        self.assertNotIn("_builtin_recognised", builtin)
        self.assertIsNone(self.custom_devices.profile_for_device(builtin))

    def test_is_builtin_recognised_tracks_the_builtin_taxonomy(self) -> None:
        self.assertEqual(self.custom_devices.load_profiles([]).errors, ())
        self.assertTrue(self.device_types.is_builtin_recognised({"device_type_raw": 38}))
        self.assertTrue(
            self.device_types.is_builtin_recognised(
                {"device_type_raw": 501, "sub_device_type": 426}
            )
        )
        self.assertFalse(self.device_types.is_builtin_recognised({"device_type_raw": 9001}))

    def test_is_builtin_recognised_ignores_custom_profiles(self) -> None:
        """The override gate must not be defeated by the profile itself."""

        self.write("a.yaml", _BASE_PROFILE)
        self.load()
        device = {"device_id": "d1", "device_type_raw": 9001}
        self.assertEqual(self.device_types.classify_device(device).value, "custom:synth_light")
        # 9001 is not in the built-in taxonomy even though a profile claims it.
        self.assertFalse(self.device_types.is_builtin_recognised(device))

    def test_classify_builtin_values_ignores_profiles(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: builtin_38\nplatform: switch\noverride: true\n"
            "match: {device_type: 38}\nhardware_verified: true\n"
            "control:\n  on: {order: on}\n",
        )
        self.load()
        with_profile = self.device_types.classify_device({"device_type_raw": 38})
        self.assertEqual(with_profile.value, "custom:builtin_38")
        self.assertEqual(
            self.device_types._classify_builtin_values(
                {"device_type_raw": 38}, 38, None
            ),
            self.device_types.DeviceCategory.DIM_COLOR_LIGHT,
        )


# --------------------------------------------------------------------------- #
# 5. Resolution integration
# --------------------------------------------------------------------------- #


class ResolutionIntegrationTests(_CustomDeviceTestBase):
    def _light_profile(self, *, verified: bool, status_only: bool = False) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\n"
            "id: integration_light\n"
            "display_name: 集成灯\n"
            "platform: light\n"
            "match: {device_type: 9001}\n"
            f"hardware_verified: {'true' if verified else 'false'}\n"
            f"status_only: {'true' if status_only else 'false'}\n"
            "capabilities: [onoff, brightness]\n"
            "state:\n  state: {from: value1, true_values: [0], false_values: [1]}\n"
            "control:\n  on: {order: on}\n  off: {order: off}\n",
        )
        return self.load()

    def test_classify_device_returns_synthetic_custom_category(self) -> None:
        self._light_profile(verified=True)
        device = self.device("d-classify", device_type_raw=9001)
        category = self.device_types.classify_device(device)
        self.assertEqual(category.value, "custom:integration_light")
        self.assertEqual(category.name, "CUSTOM_INTEGRATION_LIGHT")
        self.assertIsInstance(category, self.device_types.DeviceCategory)
        # The existing category-keyed lookups still resolve.
        self.assertEqual(
            self.device_types.DeviceCategory("custom:integration_light"), category
        )

    def test_unmatched_device_keeps_the_builtin_enum_intact(self) -> None:
        baseline = len(list(self.device_types.DeviceCategory))
        self._light_profile(verified=True)
        device = self.device("d-unmatched", device_type_raw=9002)
        self.assertEqual(
            self.device_types.classify_device(device),
            self.device_types.DeviceCategory.UNKNOWN,
        )
        self.assertEqual(
            self.device_types.classify_device({"device_type_raw": 999999}),
            self.device_types.DeviceCategory.UNKNOWN,
        )
        self.assertEqual(len(list(self.device_types.DeviceCategory)), baseline)
        self.assertNotIn(
            "custom:integration_light",
            [member.value for member in self.device_types.DeviceCategory],
        )

    def test_synthetic_member_does_not_appear_in_builtin_enumeration(self) -> None:
        baseline = {member.value for member in self.device_types.DeviceCategory}
        self._light_profile(verified=True)
        self.device_types.classify_device({"device_id": "d1", "device_type_raw": 9001})
        self.assertEqual(
            {member.value for member in self.device_types.DeviceCategory}, baseline
        )

    def test_get_device_profile_exposes_the_loaded_profile(self) -> None:
        registry = self._light_profile(verified=True)
        profile = self.device_types.get_device_profile(
            self.device("d-profile", device_type_raw=9001)
        )
        self.assertIs(profile.custom_profile, registry.get("integration_light"))
        self.assertTrue(profile.is_custom)
        self.assertEqual(profile.info.label, "集成灯")
        self.assertEqual(profile.info.capabilities, ("brightness", "onoff"))

    def test_registration_only_follows_hardware_verified(self) -> None:
        self._light_profile(verified=False)
        unverified = self.device_types.get_device_profile(
            self.device("d-unverified", device_type_raw=9001)
        )
        self.assertFalse(unverified.hardware_verified)
        self.assertTrue(unverified.registration_only)

        self._light_profile(verified=True)
        verified = self.device_types.get_device_profile(
            self.device("d-verified", device_type_raw=9001)
        )
        self.assertTrue(verified.hardware_verified)
        self.assertFalse(verified.registration_only)

    def test_status_only_stays_registration_only_even_when_verified(self) -> None:
        self._light_profile(verified=True, status_only=True)
        profile = self.device_types.get_device_profile(
            self.device("d-status-only", device_type_raw=9001)
        )
        self.assertTrue(profile.hardware_verified)
        self.assertTrue(profile.registration_only)

    def test_capability_for_returns_the_profile_platform_and_channels(self) -> None:
        self._light_profile(verified=True)
        capability = self.capabilities.capability_for(
            self.device("d-cap", device_type_raw=9001)
        )
        self.assertEqual(capability.category.value, "custom:integration_light")
        self.assertEqual(capability.platforms, frozenset({"light"}))
        self.assertEqual(
            capability.channels,
            frozenset(
                {self.capabilities.ControlChannel.LAN, self.capabilities.ControlChannel.SSL}
            ),
        )
        self.assertFalse(capability.status_only)
        self.assertTrue(capability.hardware_verified)
        self.assertTrue(capability.controllable)

    def test_capability_for_honours_cloud_only(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: cloud_light\nplatform: light\n"
            "match: {device_type: 9001}\nhardware_verified: true\n"
            "cloud_only: true\nchannels: [lan, ssl]\n"
            "control:\n  on: {order: on}\n",
        )
        self.load()
        capability = self.capabilities.capability_for(
            self.device("d-cloud", device_type_raw=9001)
        )
        self.assertEqual(capability.channels, frozenset({self.capabilities.ControlChannel.SSL}))
        self.assertTrue(capability.cloud_only)

    def test_capability_for_unverified_profile_has_no_channels(self) -> None:
        self._light_profile(verified=False)
        capability = self.capabilities.capability_for(
            self.device("d-unverified-cap", device_type_raw=9001)
        )
        self.assertEqual(capability.channels, frozenset())
        self.assertFalse(capability.controllable)
        self.assertFalse(capability.hardware_verified)

    def test_capability_for_status_only_profile_has_no_channels(self) -> None:
        self._light_profile(verified=True, status_only=True)
        capability = self.capabilities.capability_for(
            self.device("d-status-cap", device_type_raw=9001)
        )
        self.assertEqual(capability.channels, frozenset())
        self.assertTrue(capability.status_only)

    def test_profile_for_device_resolves_from_a_device_dict(self) -> None:
        self._light_profile(verified=True)
        profile = self.custom_devices.profile_for_device(
            self.device("d-resolve", device_type_raw=9001)
        )
        self.assertEqual(profile.profile_id, "integration_light")
        self.assertIsNone(
            self.custom_devices.profile_for_device(
                self.device("d-none", device_type_raw=9002)
            )
        )

    def test_profile_for_device_never_raises_without_profiles(self) -> None:
        self.custom_devices.load_profiles([])
        self.assertIsNone(self.custom_devices.profile_for_device({"device_type_raw": 9001}))


# --------------------------------------------------------------------------- #
# 6. State parsing
# --------------------------------------------------------------------------- #


class StateParserTests(_CustomDeviceTestBase):
    _PARSER_PROFILE = """
profile_version: 1
id: parser_light
platform: light
match: {device_type: 9001}
hardware_verified: true
state:
  state:
    from: properties.onoff.status
    true_values: [on]
    false_values: [off]
  brightness:
    from: value2
  angle:
    from: properties.angle.value
control:
  on: {order: on}
"""

    def _parser(self, name: str = "a.yaml"):
        self.write(name, self._PARSER_PROFILE)
        registry = self.load()
        profile = registry.get("parser_light")
        return self.custom_control.build_state_parser(profile), profile

    def test_nested_properties_and_top_level_paths_resolve(self) -> None:
        parse, _ = self._parser()
        patch = parse({}, {"value2": 51, "properties": {"onoff": {"status": "on"}, "angle": {"value": 30}}})
        self.assertEqual(patch.values, {"state": True, "brightness": 51, "angle": 30})

    def test_off_payload_sets_state_false(self) -> None:
        parse, _ = self._parser()
        patch = parse({}, {"properties": {"onoff": {"status": "off"}}})
        self.assertEqual(patch.values, {"state": False})

    def test_unknown_payload_value_falls_back_to_default(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: default_profile\nplatform: light\n"
            "match: {device_type: 9001}\n"
            "state:\n  brightness:\n    from: value2\n    default: 7\n",
        )
        self.load()
        parse = self.custom_control.build_state_parser(
            self.custom_devices.registry().get("default_profile")
        )
        self.assertEqual(parse({}, {}).values, {"brightness": 7})
        self.assertEqual(parse({}, {"value2": 12}).values, {"brightness": 12})

    def test_scale_map_and_clamp(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: transform_profile\nplatform: light\n"
            "match: {device_type: 9001}\n"
            "state:\n"
            "  brightness:\n"
            "    from: value2\n"
            "    scale: {min: 0, max: 255, to_min: 0, to_max: 100}\n"
            "  state:\n"
            "    from: value1\n"
            "    true_values: [0]\n"
            "    false_values: [1]\n"
            "  temperature:\n"
            "    from: value4\n"
            "    map:\n"
            "      0: 10\n"
            "      1: 40\n"
            "  position:\n"
            "    from: value3\n"
            "    clamp: {min: 0, max: 100}\n",
        )
        self.load()
        parse = self.custom_control.build_state_parser(
            self.custom_devices.registry().get("transform_profile")
        )
        patch = parse({}, {"value1": 0, "value2": 255, "value3": 9999})
        self.assertEqual(patch.values, {"brightness": 100, "state": True, "position": 100})

        patch = parse({}, {"value1": 1, "value2": 0, "value3": -50})
        self.assertEqual(patch.values["brightness"], 0)
        self.assertIs(patch.values["state"], False)
        self.assertEqual(patch.values["position"], 0)

    def test_map_field_is_applied_by_the_state_parser(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: map_profile\nplatform: light\n"
            "match: {device_type: 9001}\n"
            "state:\n  temperature:\n    from: value4\n"
            "    map:\n      0: 10\n      1: 40\n",
        )
        self.load()
        parse = self.custom_control.build_state_parser(
            self.custom_devices.registry().get("map_profile")
        )
        self.assertEqual(parse({}, {"value4": 0}).values, {"temperature": 10})
        self.assertEqual(parse({}, {"value4": 1}).values, {"temperature": 40})
        # An unmapped payload value resolves to the spec default (absent here).
        self.assertEqual(parse({}, {"value4": 7}).values, {})

    def test_clamp_accepts_a_two_element_list(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: clamp_profile\nplatform: light\n"
            "match: {device_type: 9001}\n"
            "state:\n  brightness:\n    from: value2\n    clamp: [10, 20]\n",
        )
        self.load()
        parse = self.custom_control.build_state_parser(
            self.custom_devices.registry().get("clamp_profile")
        )
        self.assertEqual(parse({}, {"value2": 5}).values, {"brightness": 10})
        self.assertEqual(parse({}, {"value2": 500}).values, {"brightness": 20})

    def test_mired_input_unit_converts_to_kelvin(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: mired_profile\nplatform: light\n"
            "match: {device_type: 9001}\n"
            "state:\n  color_temp:\n    from: value3\n    input_unit: mired\n",
        )
        self.load()
        parse = self.custom_control.build_state_parser(
            self.custom_devices.registry().get("mired_profile")
        )
        self.assertEqual(parse({}, {"value3": 370}).values, {"color_temp": 2703})
        self.assertEqual(parse({}, {"value3": 0}).values, {})

    def test_kelvin_input_unit_converts_to_mired(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: kelvin_profile\nplatform: light\n"
            "match: {device_type: 9001}\n"
            "state:\n  color_temp:\n    from: value3\n    input_unit: kelvin\n"
            "    output_unit: mired\n",
        )
        self.load()
        parse = self.custom_control.build_state_parser(
            self.custom_devices.registry().get("kelvin_profile")
        )
        self.assertEqual(parse({}, {"value3": 2700}).values, {"color_temp": 370})

    def test_unknown_input_unit_is_rejected(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: bad_unit\nplatform: light\n"
            "match: {device_type: 9001}\n"
            "state:\n  color_temp:\n    from: value3\n    input_unit: furlong\n",
        )
        self.assert_profile_error("a.yaml", "input_unit")
        self.assert_registry_error("input_unit")

    def test_only_fields_present_in_the_payload_are_patched(self) -> None:
        parse, _ = self._parser()
        partial = parse({"state": True, "brightness": 80, "angle": 10}, {"value2": 42})
        self.assertEqual(partial.values, {"brightness": 42})
        self.assertNotIn("state", partial.values)
        self.assertNotIn("angle", partial.values)

    def test_brightness_zero_forces_state_false(self) -> None:
        parse, _ = self._parser()
        patch = parse(
            {}, {"value2": 0, "properties": {"onoff": {"status": "on"}}}
        )
        self.assertEqual(patch.values, {"state": False, "brightness": 0})

    def test_brightness_zero_does_not_flip_an_absent_state(self) -> None:
        parse, _ = self._parser()
        patch = parse({}, {"value2": 0})
        self.assertEqual(patch.values, {"brightness": 0})

    def test_true_false_values_win_over_as_bool(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: bool_profile\nplatform: binary_sensor\n"
            "match: {device_type: 9001}\n"
            "state:\n"
            "  state:\n    from: value1\n    true_values: ['1', detected]\n"
            "    false_values: ['0', idle]\n",
        )
        self.load()
        parse = self.custom_control.build_state_parser(
            self.custom_devices.registry().get("bool_profile")
        )
        self.assertIs(parse({}, {"value1": 1}).values["state"], True)
        self.assertIs(parse({}, {"value1": "idle"}).values["state"], False)
        self.assertEqual(parse({}, {"value1": "maybe"}).values, {})

    def test_constant_value_spec(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: constant_profile\nplatform: light\n"
            "match: {device_type: 9001}\n"
            "state:\n  brightness: 255\n",
        )
        self.load()
        parse = self.custom_control.build_state_parser(
            self.custom_devices.registry().get("constant_profile")
        )
        self.assertEqual(parse({}, {}).values, {"brightness": 255})

    def test_state_parsers_registry_resolves_custom_profiles(self) -> None:
        self._parser()
        category = self.device_types.classify_device(
            {"device_id": "d-parser", "device_type_raw": 9001}
        )
        parser = self.parsers.get_state_parser(category)
        self.assertIsNotNone(parser)
        patch = parser({}, {"value2": 11, "properties": {"onoff": {"status": "on"}}})
        self.assertEqual(patch.values, {"state": True, "brightness": 11})

    def test_state_parser_registry_returns_none_without_state_specs(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: stateless_profile\nplatform: light\n"
            "match: {device_type: 9001}\ncontrol:\n  on: {order: on}\n",
        )
        self.load()
        category = self.device_types.classify_device(
            {"device_id": "d-stateless", "device_type_raw": 9001}
        )
        self.assertIsNone(self.parsers.get_state_parser(category))

    def test_builtin_categories_still_use_builtin_parsers(self) -> None:
        self.custom_devices.load_profiles([])
        parser = self.parsers.get_state_parser(self.device_types.DeviceCategory.MONO_LIGHT)
        self.assertIsNotNone(parser)
        self.assertIs(
            parser, self.parsers.STATE_PARSERS[self.device_types.DeviceCategory.MONO_LIGHT]
        )

    def test_profile_state_parser_helper_matches_build_state_parser(self) -> None:
        _, profile = self._parser()
        patch = profile.state_parser()({}, {"value2": 9})
        self.assertEqual(patch.values, {"brightness": 9})


# --------------------------------------------------------------------------- #
# 7. Control routing
# --------------------------------------------------------------------------- #


class ControlRoutingTests(_CustomDeviceTestBase):
    _ROUTING_PROFILE = """
profile_version: 1
id: routing_light
platform: light
match: {device_type: 9001}
hardware_verified: true
state:
  state: {from: value1}
control:
  on:
    order: on
    value1: 0
    value2: {param: brightness, default: 255}
    value3: {param: color_temp, default: 2700}
  off:
    order: off
    value1: 1
  brightness:
    order: move to level
    value1: {param: brightness}
  color_temp:
    order: fast color temperature
    value1: {param: color_temp}
  position:
    order: set property
    value1: {param: position}
    properties: {percent: 50}
  stop:
    order: stop
"""

    def _category(self):
        self.write("a.yaml", self._ROUTING_PROFILE)
        self.load()
        return self.device_types.classify_device(
            {"device_id": "d-route", "device_type_raw": 9001}
        )

    def test_on_route_compiles_to_the_envelope_transport(self) -> None:
        category = self._category()
        route = self.control_router.power_route(category, True, {})
        self.assertEqual(route.scope, "ssl")
        self.assertEqual(route.method, "send_control_envelope")
        self.assertEqual(route.args, ("on", 0, 255, 2700, 0))
        self.assertEqual(route.optimistic, {"state": True})

    def test_off_route_compiles_to_the_envelope_transport(self) -> None:
        category = self._category()
        route = self.control_router.power_route(category, False, {})
        self.assertEqual(route.args, ("off", 1, 0, 0, 0))
        self.assertEqual(route.optimistic, {"state": False})

    def test_power_route_passes_call_parameters_into_the_envelope(self) -> None:
        category = self._category()
        route = self.control_router.power_route(
            category, True, {}, brightness=120, color_temp=3000
        )
        self.assertEqual(route.args, ("on", 0, 120, 3000, 0))

    def test_brightness_route_substitutes_the_param(self) -> None:
        category = self._category()
        route = self.control_router.brightness_route(category, 64, {})
        self.assertEqual(route.method, "send_control_envelope")
        self.assertEqual(route.args, ("move to level", 64, 0, 0, 0))
        self.assertEqual(route.optimistic, {"state": True})

    def test_color_temp_route_substitutes_the_param(self) -> None:
        category = self._category()
        route = self.control_router.color_temp_route(category, 4000, {})
        self.assertEqual(route.args, ("fast color temperature", 4000, 0, 0, 0))

    def test_declared_default_applies_when_a_param_is_absent(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: default_param\nplatform: light\n"
            "match: {device_type: 9001}\nhardware_verified: true\n"
            "control:\n  brightness:\n    order: move to level\n"
            "    value1: {param: brightness}\n    value2: {default: 5}\n",
        )
        self.load()
        category = self.device_types.classify_device(
            {"device_id": "d-default", "device_type_raw": 9001}
        )
        route = self.custom_control.build_route(category, "brightness", {}, brightness=9)
        self.assertEqual(route.args, ("move to level", 9, 5, 0, 0))

    def test_set_property_passes_properties_through(self) -> None:
        category = self._category()
        route = self.custom_control.build_route(
            category, "position", {}, position=42
        )
        self.assertEqual(route.args, ("set property", 42, 0, 0, 0))
        self.assertEqual(route.kwargs, {"properties": {"percent": 50}})

    def test_properties_only_allowed_with_set_property(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: bad_properties\nplatform: light\n"
            "match: {device_type: 9001}\n"
            "control:\n  on:\n    order: on\n    properties: {percent: 5}\n",
        )
        self.assert_profile_error("a.yaml", "properties")
        self.assert_registry_error("properties")

    def test_stop_route_records_the_cover_action(self) -> None:
        category = self._category()
        route = self.custom_control.build_route(category, "stop", {})
        self.assertEqual(route.method, "send_control_envelope")
        self.assertEqual(route.args, ("stop", 0, 0, 0, 0))
        self.assertEqual(route.optimistic, {"cover_action": "stop"})

    def test_optimistic_overrides_replace_the_defaults(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: optimistic_profile\nplatform: light\n"
            "match: {device_type: 9001}\nhardware_verified: true\n"
            "control:\n  on: {order: on}\n  off: {order: off}\n"
            "optimistic:\n"
            "  on:\n    brightness: {param: brightness, default: 3}\n"
            "  off:\n    state: false\n"
            "    brightness: 0\n",
        )
        self.load()
        category = self.device_types.classify_device(
            {"device_id": "d-optimistic", "device_type_raw": 9001}
        )
        route = self.custom_control.build_route(category, "on", {})
        # The declared brightness wins; the implied state=True still applies.
        self.assertEqual(route.optimistic, {"brightness": 3, "state": True})

        route = self.custom_control.build_route(category, "on", {}, brightness=88)
        self.assertEqual(route.optimistic, {"brightness": 88, "state": True})

        route = self.custom_control.build_route(category, "off", {})
        self.assertEqual(route.optimistic, {"brightness": 0, "state": False})

    def test_declared_optimistic_false_wins_over_the_implied_state(self) -> None:
        """A declared ``false`` is a value, not "absent".

        ``ValueSpec.resolve`` must return ``False`` for a stored constant of
        ``False``, and ``_resolve_optimistic`` must not replace it with the
        implied ``state: True`` for an ``on`` action.
        """

        self.write(
            "a.yaml",
            "profile_version: 1\nid: false_optimistic\nplatform: light\n"
            "match: {device_type: 9001}\nhardware_verified: true\n"
            "control:\n  on: {order: on}\n"
            "optimistic:\n  on:\n    state: false\n",
        )
        self.load()
        profile = self.custom_devices.registry().get("false_optimistic")
        spec = profile.optimistic["on"]["state"]
        self.assertEqual(spec.constant, False)
        self.assertIs(spec.resolve({}, {}), False)

        category = self.device_types.classify_device(
            {"device_id": "d-false-optimistic", "device_type_raw": 9001}
        )
        route = self.custom_control.build_route(category, "on", {})
        self.assertEqual(route.optimistic, {"state": False})

    def test_undeclared_action_is_rejected_loudly(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: onoff_only\nplatform: light\n"
            "match: {device_type: 9001}\nhardware_verified: true\n"
            "control:\n  on: {order: on}\n  off: {order: off}\n",
        )
        self.load()
        category = self.device_types.classify_device(
            {"device_id": "d-undeclared", "device_type_raw": 9001}
        )
        with self.assertRaises(self.custom_control.ControlNotDeclared) as ctx:
            self.control_router.brightness_route(category, 50, {})
        self.assertEqual(ctx.exception.profile_id, "onoff_only")
        self.assertEqual(ctx.exception.action, "brightness")
        self.assertIn("onoff_only", str(ctx.exception))
        self.assertIn("brightness", str(ctx.exception))

        with self.assertRaises(self.custom_control.ControlNotDeclared):
            self.control_router.color_temp_route(category, 3000, {})

        # Declared actions are of course still routed.
        self.assertEqual(
            self.control_router.power_route(category, True, {}).args, ("on", 0, 0, 0, 0)
        )

    def test_undeclared_cover_action_is_rejected(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: cover_onoff\nplatform: cover\n"
            "match: {device_type: 9001}\nhardware_verified: true\n"
            "control:\n  open: {order: open}\n  close: {order: off}\n",
        )
        self.load()
        category = self.device_types.classify_device(
            {"device_id": "d-cover-undeclared", "device_type_raw": 9001}
        )
        # open/close are declared and route to the verified envelopes.
        self.assertEqual(
            self.custom_control.build_route(category, "open", {}).args[0], "open"
        )
        self.assertEqual(
            self.custom_control.build_route(category, "close", {}).args[0], "off"
        )
        # "stop" is not declared and is refused instead of borrowing a command.
        with self.assertRaises(self.custom_control.ControlNotDeclared) as ctx:
            self.custom_control.build_route(category, "stop", {}, strict=True)
        self.assertEqual(ctx.exception.action, "stop")
        self.assertEqual(ctx.exception.profile_id, "cover_onoff")

    def test_undeclared_action_is_none_without_strict_mode(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: onoff_only2\nplatform: light\n"
            "match: {device_type: 9001}\nhardware_verified: true\n"
            "control:\n  on: {order: on}\n",
        )
        self.load()
        category = self.device_types.classify_device(
            {"device_id": "d-nonstrict", "device_type_raw": 9001}
        )
        self.assertIsNone(
            self.custom_control.build_route(category, "brightness", {}, brightness=1)
        )

    def test_builtin_category_is_unaffected(self) -> None:
        self.custom_devices.load_profiles([])
        route = self.control_router.power_route(
            self.device_types.DeviceCategory.MONO_LIGHT, True, {}
        )
        self.assertEqual(route.scope, "ssl")
        self.assertEqual(route.method, "send_control_switch")
        self.assertEqual(route.args, (True,))

        off = self.control_router.power_route(
            self.device_types.DeviceCategory.MONO_LIGHT, False, {}
        )
        self.assertEqual(off.args, (False,))

    def test_builtin_brightness_route_is_unaffected(self) -> None:
        self.custom_devices.load_profiles([])
        route = self.control_router.brightness_route(
            self.device_types.DeviceCategory.DIMMABLE_LIGHT, 30, {}
        )
        self.assertEqual(route.method, "send_control_dimmable_light_brightness")
        self.assertEqual(route.args, (30,))
        self.assertEqual(route.optimistic, {"brightness": 30, "state": True})

    def test_custom_route_for_device_returns_none_for_builtin_devices(self) -> None:
        self.write("a.yaml", self._ROUTING_PROFILE)
        self.load()
        self.assertIsNone(
            self.control_router.custom_route_for_device(
                self.device("d-builtin", device_type_raw=501, sub_device_type=426),
                "brightness",
                {},
                brightness=10,
            )
        )

    def test_custom_route_for_device_routes_a_custom_device(self) -> None:
        self.write("a.yaml", self._ROUTING_PROFILE)
        self.load()
        route = self.control_router.custom_route_for_device(
            self.device("d-custom", device_type_raw=9001), "brightness", {}, brightness=77
        )
        self.assertEqual(route.args, ("move to level", 77, 0, 0, 0))

    def test_custom_route_for_device_rejects_undeclared_actions(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: device_route_onoff\nplatform: light\n"
            "match: {device_type: 9001}\nhardware_verified: true\n"
            "control:\n  on: {order: on}\n",
        )
        self.load()
        with self.assertRaises(self.custom_control.ControlNotDeclared):
            self.control_router.custom_route_for_device(
                self.device("d-device-route", device_type_raw=9001),
                "brightness",
                {},
                brightness=10,
            )

    def test_supported_actions_and_category_lookup(self) -> None:
        category = self._category()
        self.assertEqual(
            self.custom_control.supported_actions(category),
            frozenset({"on", "off", "brightness", "color_temp", "position", "stop"}),
        )
        self.assertTrue(self.custom_control.is_custom_category(category))
        self.assertTrue(self.custom_control.is_custom_category("custom:routing_light"))
        self.assertFalse(
            self.custom_control.is_custom_category(self.device_types.DeviceCategory.MONO_LIGHT)
        )
        self.assertEqual(
            self.custom_control.profile_for_category(category).profile_id, "routing_light"
        )
        self.assertIsNone(
            self.custom_control.profile_for_category(self.device_types.DeviceCategory.MONO_LIGHT)
        )

    def test_build_route_returns_none_for_unknown_actions(self) -> None:
        category = self._category()
        self.assertIsNone(self.custom_control.build_route(category, "teleport", {}))

    def test_control_envelope_order_string_is_used_verbatim(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: order_string\nplatform: light\n"
            "match: {device_type: 9001}\nhardware_verified: true\n"
            "control:\n  on:\n    order: set property\n    value1: 1\n"
            "    properties: {onoff: 'on'}\n",
        )
        self.load()
        category = self.device_types.classify_device(
            {"device_id": "d-order", "device_type_raw": 9001}
        )
        route = self.custom_control.build_route(category, "on", {})
        self.assertEqual(route.args[0], "set property")
        self.assertEqual(route.kwargs, {"properties": {"onoff": "on"}})

    def test_set_property_normalises_yaml_boolean_values(self) -> None:
        """A bare ``on`` under an onoff-style key must reach the wire as "on"."""

        self.write(
            "a.yaml",
            "profile_version: 1\nid: bool_property\nplatform: light\n"
            "match: {device_type: 9001}\nhardware_verified: true\n"
            "control:\n  on:\n    order: set property\n"
            "    properties:\n      onoff:\n        status: on\n",
        )
        self.load()
        category = self.device_types.classify_device(
            {"device_id": "d-bool-property", "device_type_raw": 9001}
        )
        route = self.custom_control.build_route(category, "on", {})
        self.assertEqual(route.kwargs, {"properties": {"onoff": {"status": "on"}}})

    def test_properties_rejected_for_control_orders_other_than_set_property(self) -> None:
        for order in ("on", "off", "move to level", "fast color temperature", "stop"):
            with self.subTest(order=order):
                _write(
                    self.tmpdir,
                    "a.yaml",
                    "profile_version: 1\nid: bad_props\nplatform: light\n"
                    "match: {device_type: 9001}\n"
                    f"control:\n  on:\n    order: {order}\n"
                    "    properties: {percent: 5}\n",
                )
                self.assert_profile_error("a.yaml", "properties")
                self.assert_registry_error("properties")

    def test_route_scope_is_always_ssl(self) -> None:
        self.write("a.yaml", self._ROUTING_PROFILE)
        self.load()
        category = self.device_types.classify_device(
            {"device_id": "d-scope", "device_type_raw": 9001}
        )
        for action, kwargs in (
            ("on", {}),
            ("off", {}),
            ("brightness", {"brightness": 1}),
            ("color_temp", {"color_temp": 2700}),
            ("position", {"position": 1}),
            ("stop", {}),
        ):
            with self.subTest(action=action):
                route = self.custom_control.build_route(category, action, {}, **kwargs)
                self.assertEqual(route.scope, "ssl")
                self.assertEqual(route.method, "send_control_envelope")
                self.assertEqual(len(route.args), 5)


# --------------------------------------------------------------------------- #
# 8. Platform stamping
# --------------------------------------------------------------------------- #


class PlatformStampingTests(_CustomDeviceTestBase):
    def test_apply_platform_stamps_the_profile_platform(self) -> None:
        self.write("a.yaml", _BASE_PROFILE)
        registry = self.load()
        device = {"device_id": "d1", "device_type": "unknown", "device_type_raw": 9001}
        result = self.custom_devices.apply_platform(device, builtin_platform="unknown")
        self.assertIs(result, device)
        self.assertEqual(device["device_type"], "light")
        self.assertIs(device["custom_profile"], registry.get("synth_light"))

    def test_apply_platform_leaves_unmatched_dicts_untouched(self) -> None:
        self.write("a.yaml", _BASE_PROFILE)
        self.load()
        device = {"device_id": "d2", "device_type": "cover", "device_type_raw": 34}
        result = self.custom_devices.apply_platform(device, builtin_platform="cover")
        self.assertIs(result, device)
        self.assertEqual(device["device_type"], "cover")
        self.assertNotIn("custom_profile", device)

    def test_apply_platform_without_profiles_is_a_noop(self) -> None:
        self.custom_devices.load_profiles([])
        device = {"device_type": "sensor", "device_type_raw": 9001}
        self.custom_devices.apply_platform(device, builtin_platform=None)
        self.assertEqual(device["device_type"], "sensor")

    def test_apply_platform_respects_builtin_override_protection(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: protected\nplatform: switch\n"
            "match: {device_type: 38}\nhardware_verified: true\n"
            "control:\n  on: {order: on}\n",
        )
        self.load()
        device = {"device_id": "d3", "device_type": "light", "device_type_raw": 38}
        self.custom_devices.apply_platform(device, builtin_platform="light")
        self.assertEqual(device["device_type"], "light")
        self.assertNotIn("custom_profile", device)

    def test_protocol_device_to_dict_stamps_the_custom_platform(self) -> None:
        self.write("a.yaml", _BASE_PROFILE)
        self.load()
        record = self._record(device_type="9001", name="合成灯")
        self.assertEqual(
            self.protocol._infer_ha_device_type(record), "unknown"
        )
        device = self.protocol.device_to_dict(record)
        self.assertEqual(device["device_type"], "light")
        self.assertEqual(device["custom_profile"].profile_id, "synth_light")

    def test_protocol_device_to_dict_keeps_builtin_platform(self) -> None:
        self.write("a.yaml", _BASE_PROFILE)
        self.load()
        # type 1 is genuinely built-in (``simple_zigbee_light`` -> light), so a
        # profile that only claims 9001 must not touch it.
        record = self._record(device_type="1", name="普通灯")
        device = self.protocol.device_to_dict(record)
        self.assertEqual(device["device_type"], "light")
        self.assertNotIn("custom_profile", device)

    def test_protocol_device_to_dict_protects_builtin_devices(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: protocol_protected\nplatform: switch\n"
            "match: {device_type: 1}\nhardware_verified: true\n"
            "control:\n  on: {order: on}\n",
        )
        self.load()
        device = self.protocol.device_to_dict(self._record(device_type="1"))
        # Built-in recognition wins: the platform is unchanged.
        self.assertEqual(device["device_type"], "light")
        self.assertNotIn("custom_profile", device)

    def test_protocol_device_to_dict_override_true_replaces_builtin(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: protocol_override\nplatform: switch\n"
            "override: true\nmatch: {device_type: 1}\nhardware_verified: true\n"
            "control:\n  on: {order: on}\n",
        )
        self.load()
        device = self.protocol.device_to_dict(self._record(device_type="1"))
        self.assertEqual(device["device_type"], "switch")
        self.assertEqual(device["custom_profile"].profile_id, "protocol_override")

    def test_protocol_device_to_dict_without_profiles(self) -> None:
        self.custom_devices.load_profiles([])
        device = self.protocol.device_to_dict(self._record(device_type="9001"))
        self.assertEqual(device["device_type"], "unknown")
        self.assertNotIn("custom_profile", device)

    def _record(self, **overrides):
        fields = {
            "uid": "uid-1",
            "name": "合成设备",
            "model": "",
            "device_type": "9001",
            "sub_device_type": "",
            "room": "",
            "parent_uid": "",
            "online": True,
        }
        fields.update(overrides)
        return self.protocol.OrviboDevice(**fields)

    def _client(self):
        return self.https_client.HttpsClient.__new__(self.https_client.HttpsClient)

    def test_https_client_lets_a_profile_claim_an_unknown_item(self) -> None:
        self.write("a.yaml", _BASE_PROFILE)
        self.load()
        client = self._client()
        item = {"deviceId": "d-http", "deviceType": 9001, "deviceName": "未知灯"}
        self.assertIsNone(client._builtin_device_type(item, 9001, None, None))
        self.assertEqual(client._get_device_type(item), "light")

    def test_https_client_keeps_the_builtin_platform_without_override(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: http_protected\nplatform: switch\n"
            "match: {device_type: 38}\nhardware_verified: true\n"
            "control:\n  on: {order: on}\n",
        )
        self.load()
        client = self._client()
        item = {"deviceId": "d-http-38", "deviceType": 38}
        self.assertEqual(client._builtin_device_type(item, 38, None, None), "light")
        self.assertEqual(client._get_device_type(item), "light")

    def test_https_client_override_true_replaces_the_builtin_platform(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: http_override\nplatform: switch\n"
            "override: true\nmatch: {device_type: 38}\nhardware_verified: true\n"
            "control:\n  on: {order: on}\n",
        )
        self.load()
        client = self._client()
        item = {"deviceId": "d-http-38b", "deviceType": 38}
        self.assertEqual(client._get_device_type(item), "switch")

    def test_https_client_claims_an_item_with_no_builtin_type(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: fallback_light\nplatform: light\n"
            "match:\n  model: 're:^synth'  \nhardware_verified: true\n"
            "control:\n  on: {order: on}\n",
        )
        self.load()
        client = self._client()
        item = {"deviceId": "d-fallback", "deviceType": 9001, "model": "synth-7"}
        self.assertIsNone(client._builtin_device_type(item, 9001, None, None))
        self.assertEqual(client._get_device_type(item), "light")

    def test_https_client_without_profiles_is_unchanged(self) -> None:
        self.custom_devices.load_profiles([])
        client = self._client()
        self.assertEqual(
            client._get_device_type({"deviceId": "d", "deviceType": 1}), "light"
        )
        self.assertEqual(
            client._get_device_type({"deviceId": "d", "deviceType": 501, "subDeviceType": 426}),
            "light",
        )
        self.assertIsNone(client._get_device_type({"deviceId": "d", "deviceType": 9001}))

    def test_profile_for_device_never_uses_the_platform_field(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: field_protected\nplatform: switch\n"
            "match: {device_type: 38}\nhardware_verified: true\n"
            "control:\n  on: {order: on}\n",
        )
        self.load()
        # 38/-2 is a built-in combination, so the override-free profile must not
        # claim it -- regardless of what ``device_type`` currently says.  The
        # platform field is not evidence of built-in recognition: a profile
        # overwrites it with its own platform.
        for platform in ("light", "unknown", "switch"):
            with self.subTest(platform=platform):
                self.assertIsNone(
                    self.custom_devices.profile_for_device(
                        {
                            "device_id": f"d-field-{platform}",
                            "device_type": platform,
                            "device_type_raw": 38,
                            "sub_device_type": -2,
                        }
                    )
                )
        # A device the taxonomy does not know is still claimed.  Its platform
        # field is already stamped, but the raw fields prove no built-in match.
        self.write(
            "b.yaml",
            "profile_version: 1\nid: field_unknown_ok\nplatform: switch\n"
            "match: {device_type: 9001}\nhardware_verified: true\n"
            "control:\n  on: {order: on}\n",
        )
        self.load()
        self.assertIsNotNone(
            self.custom_devices.profile_for_device(
                {
                    "device_id": "d-field-ok",
                    "device_type": "switch",
                    "device_type_raw": 9001,
                }
            )
        )

    def test_platform_field_alone_never_blocks_a_profile(self) -> None:
        """Regression: the overriding guard must use raw fields, not ``device_type``.

        A profile that claims a genuinely unknown device has already had
        ``device_type`` stamped with its own platform.  Treating that as
        "built-in recognised" hid the profile from the entity platforms.
        """

        self.write(
            "a.yaml",
            "profile_version: 1\nid: stamped_ok\nplatform: switch\n"
            "match: {device_type: 9001}\nhardware_verified: true\n"
            "control:\n  on: {order: on}\n",
        )
        self.load()
        for platform in ("switch", "light", "unknown", ""):
            with self.subTest(platform=platform):
                profile = self.custom_devices.profile_for_device(
                    {
                        "device_id": f"d-stamped-{platform or 'empty'}",
                        "device_type": platform,
                        "device_type_raw": 9001,
                        "sub_device_type": -2,
                    }
                )
                self.assertIsNotNone(profile)
                self.assertEqual(profile.profile_id, "stamped_ok")


# --------------------------------------------------------------------------- #
# 9. Registry hygiene
# --------------------------------------------------------------------------- #


class RegistryHygieneTests(_CustomDeviceTestBase):
    def _two_profiles(self) -> None:
        self.write("a.yaml", _BASE_PROFILE)
        self.write(
            "b.yaml",
            "profile_version: 1\nid: unverified_sensor\nplatform: sensor\n"
            "match: {device_type: 9002}\n"
            "state:\n  temperature: {from: value1}\n",
        )

    def test_diagnostics_report_profiles_and_matched_counts(self) -> None:
        self._two_profiles()
        registry = self.load()
        report = {entry["id"]: entry for entry in registry.diagnostics()}
        self.assertEqual(set(report), {"synth_light", "unverified_sensor"})
        self.assertEqual(report["synth_light"]["platform"], "light")
        self.assertEqual(report["synth_light"]["matched_devices"], 0)
        self.assertEqual(report["synth_light"]["control_actions"], ["on"])
        self.assertEqual(report["synth_light"]["state_fields"], ["state"])
        self.assertEqual(report["synth_light"]["channels"], ["lan", "ssl"])
        self.assertTrue(report["synth_light"]["hardware_verified"])

        registry.find_profile({"device_id": "d-a", "device_type_raw": 9001})
        registry.find_profile({"device_id": "d-b", "device_type_raw": 9001})
        registry.find_profile({"device_id": "d-c", "device_type_raw": 9002})
        report = {entry["id"]: entry for entry in registry.diagnostics()}
        self.assertEqual(report["synth_light"]["matched_devices"], 2)
        self.assertEqual(report["unverified_sensor"]["matched_devices"], 1)

    def test_diagnostics_include_profile_warnings(self) -> None:
        self.write(
            "a.yaml",
            "profile_version: 1\nid: warned_light\nplatform: light\n"
            "match: {device_type: 9001}\ncontrol:\n  on: {order: on}\n",
        )
        registry = self.load()
        warnings = registry.diagnostics()[0]["warnings"]
        self.assertTrue(warnings)
        self.assertTrue(any("hardware_verified" in item for item in warnings))

    def test_second_load_resets_match_counts(self) -> None:
        self._two_profiles()
        registry = self.load()
        registry.find_profile({"device_id": "d-a", "device_type_raw": 9001})
        registry.find_profile({"device_id": "d-b", "device_type_raw": 9001})
        self.assertEqual(registry.match_counts["synth_light"], 2)

        registry.load([self.tmpdir])
        self.assertEqual(registry.match_counts, {})
        self.assertEqual(
            {entry["id"]: entry["matched_devices"] for entry in registry.diagnostics()},
            {"synth_light": 0, "unverified_sensor": 0},
        )

    def test_load_profiles_wrapper_returns_the_process_registry(self) -> None:
        self.write("a.yaml", _BASE_PROFILE)
        registry = self.custom_devices.load_profiles([self.tmpdir])
        self.assertIs(registry, self.custom_devices.registry())
        self.assertEqual([item.profile_id for item in registry.profiles], ["synth_light"])

    def test_empty_directory_list_clears_everything(self) -> None:
        self.write("a.yaml", _BASE_PROFILE)
        self.load()
        registry = self.custom_devices.load_profiles([])
        self.assertEqual(registry.profiles, ())
        self.assertEqual(registry.errors, ())
        self.assertIsNone(registry.get("synth_light"))
        self.assertIsNone(self.custom_devices.profile_for_device({"device_type_raw": 9001}))

    def test_load_never_raises_for_errors(self) -> None:
        self.write("bad.yaml", "profile_version: 1\nid: Bad\nplatform: nope\n")
        registry = self.load()
        self.assertEqual(registry.profiles, ())
        self.assertEqual(len(registry.errors), 1)
        self.assertIn("bad.yaml", registry.errors[0])

    def test_one_bad_file_does_not_hide_a_good_one(self) -> None:
        self.write("a_good.yaml", _BASE_PROFILE)
        self.write("b_bad.yaml", "profile_version: 1\nid: Bad\nplatform: nope\n")
        registry = self.load()
        self.assertEqual([item.profile_id for item in registry.profiles], ["synth_light"])
        self.assertEqual(len(registry.errors), 1)
        self.assertIn("b_bad.yaml", registry.errors[0])

    def test_directories_property_records_the_scan(self) -> None:
        registry = self.load()
        self.assertEqual(registry.directories, (self.tmpdir,))

    def test_by_category_key_and_lookup_helpers(self) -> None:
        self._two_profiles()
        registry = self.load()
        self.assertEqual(
            registry.by_category_key("custom:synth_light").profile_id, "synth_light"
        )
        self.assertEqual(
            registry.by_category_key("custom:nope"), None
        )
        self.assertIsNone(registry.by_category_key(self.device_types.DeviceCategory.MONO_LIGHT))
        self.assertIsNone(registry.by_category_key(None))
        self.assertEqual(registry.get("missing"), None)

    def test_find_profile_memoises_per_device(self) -> None:
        self.write("a.yaml", _BASE_PROFILE)
        registry = self.load()
        device = {"device_id": "d-memo", "device_type_raw": 9001}
        first = registry.find_profile(device)
        second = registry.find_profile(device)
        self.assertIs(first, second)
        self.assertEqual(registry.match_counts["synth_light"], 1)

    def test_identical_device_ids_are_counted_once(self) -> None:
        self.write("a.yaml", _BASE_PROFILE)
        registry = self.load()
        registry.find_profile({"device_id": "d-copy", "device_type_raw": 9001})
        registry.find_profile({"device_id": "d-copy", "device_type_raw": 9001})
        self.assertEqual(registry.match_counts["synth_light"], 1)

    def test_no_profiles_means_no_matches(self) -> None:
        registry = self.custom_devices.load_profiles([])
        self.assertIsNone(registry.find_profile({"device_id": "d", "device_type_raw": 9001}))
        self.assertEqual(registry.diagnostics(), [])


if __name__ == "__main__":
    unittest.main()
