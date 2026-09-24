"""Unit tests for the control-command evidence probe (no network).

The contract under test is the judgement: a candidate may only be called
``verified`` when a device state push actually shows the intended value.  A
server acknowledgement is not evidence, and an ambiguous numeric state must be
reported as inconclusive rather than assumed.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


probe = _load("control_probe_under_test", ROOT / "tools" / "control_probe.py")


def _candidate(**overrides):
    base = {
        "label": "order=on value1=0",
        "order": "on",
        "value1": 0,
        "intent": "on",
        "expected": True,
    }
    base.update(overrides)
    return probe.Candidate(**base)


class CandidateGenerationTests(unittest.TestCase):
    def test_on_intent_covers_both_polarities(self):
        candidates = probe.generate_candidates(platform="switch", intent="on")
        value1s = {
            candidate.value1
            for candidate in candidates
            if candidate.order == "on" and candidate.value2 == 0
        }
        self.assertEqual(value1s, {0, 1})

    def test_on_intent_includes_set_property_form(self):
        candidates = probe.generate_candidates(platform="switch", intent="on")
        self.assertTrue(any(candidate.order == "set property" for candidate in candidates))
        property_candidate = next(c for c in candidates if c.order == "set property")
        self.assertEqual(property_candidate.properties, {"onoff": {"status": "on"}})

    def test_off_intent_uses_off_status(self):
        candidates = probe.generate_candidates(platform="switch", intent="off")
        property_candidate = next(c for c in candidates if c.order == "set property")
        self.assertEqual(property_candidate.properties, {"onoff": {"status": "off"}})
        self.assertFalse(property_candidate.expected)

    def test_brightness_covers_both_scales_and_slots(self):
        candidates = probe.generate_candidates(platform="light", intent="brightness")
        orders = {candidate.order for candidate in candidates}
        self.assertIn("move to level", orders)
        self.assertIn("fast move to level", orders)
        self.assertIn("set property", orders)
        self.assertIn("on", orders)  # 老协议把亮度放在 on 的 value2

    def test_brightness_target_limits_candidates(self):
        candidates = probe.generate_candidates(
            platform="light", intent="brightness", target=200
        )
        levels = {candidate.expected for candidate in candidates}
        self.assertIn(200, levels)
        self.assertNotIn(255, levels)  # 给了 target 就不再猜上限

    def test_color_temp_offers_mired_and_kelvin(self):
        candidates = probe.generate_candidates(platform="light", intent="color_temp")
        values = {candidate.expected for candidate in candidates}
        self.assertIn(370, values)    # mired
        self.assertIn(2700, values)   # Kelvin

    def test_position_uses_open_order(self):
        candidates = probe.generate_candidates(
            platform="cover", intent="position", target=30
        )
        self.assertEqual(candidates[0].order, "open")
        self.assertEqual(candidates[0].value1, 30)

    def test_unknown_intent_raises(self):
        with self.assertRaises(ValueError):
            probe.generate_candidates(platform="switch", intent="explode")

    def test_max_candidates_is_respected(self):
        candidates = probe.generate_candidates(
            platform="light", intent="brightness", max_candidates=3
        )
        self.assertLessEqual(len(candidates), 3)


class HandWrittenCandidateTests(unittest.TestCase):
    def test_accepts_a_raw_cmd15_payload(self):
        candidates = probe.load_candidates(
            [
                {
                    "order": "on",
                    "value1": 1,
                    "value2": 255,
                    "cmd": 15,
                    "serial": 123,
                    "label": "反编译得到的帧",
                }
            ]
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].value2, 255)
        self.assertEqual(candidates[0].label, "反编译得到的帧")

    def test_accepts_wrapper_object(self):
        candidates = probe.load_candidates({"candidates": [{"order": "off"}]})
        self.assertEqual(candidates[0].order, "off")

    def test_rejects_unknown_order(self):
        with self.assertRaises(ValueError):
            probe.load_candidates([{"order": "explode"}])

    def test_rejects_properties_without_set_property(self):
        with self.assertRaises(ValueError):
            probe.load_candidates([{"order": "on", "properties": {"a": 1}}])

    def test_rejects_non_list(self):
        with self.assertRaises(ValueError):
            probe.load_candidates({"nope": 1})


class JudgementTests(unittest.TestCase):
    """The core guarantee: verdicts come from device feedback only."""

    def test_verified_when_push_shows_the_intent(self):
        verdict = probe.judge(
            _candidate(),
            baseline=False,
            observations=[{"properties": {"onoff": {"status": "on"}}}],
        )
        self.assertEqual(verdict.status, "verified")
        self.assertTrue(verdict.verified)

    def test_ineffective_when_push_shows_the_opposite(self):
        verdict = probe.judge(
            _candidate(),
            baseline=False,
            observations=[{"properties": {"onoff": {"status": "off"}}}],
        )
        self.assertEqual(verdict.status, "ineffective")

    def test_no_push_is_inconclusive_not_verified(self):
        verdict = probe.judge(_candidate(), baseline=False, observations=[])
        self.assertEqual(verdict.status, "inconclusive")
        self.assertFalse(verdict.verified)

    def test_push_without_relevant_field_is_inconclusive(self):
        verdict = probe.judge(
            _candidate(), baseline=False, observations=[{"value4": 9}]
        )
        self.assertEqual(verdict.status, "inconclusive")

    def test_numeric_value1_state_is_ambiguous(self):
        # 0/1 carries no polarity; the probe must refuse to guess.
        verdict = probe.judge(
            _candidate(), baseline=None, observations=[{"value1": 0}]
        )
        self.assertEqual(verdict.status, "inconclusive")
        self.assertIn("value1", verdict.detail)

    def test_off_intent_verified_by_off_push(self):
        verdict = probe.judge(
            _candidate(intent="off", expected=False, order="off", value1=1),
            baseline=True,
            observations=[{"properties": {"onoff": {"status": "off"}}}],
        )
        self.assertEqual(verdict.status, "verified")

    def test_brightness_verified_within_tolerance(self):
        verdict = probe.judge(
            _candidate(intent="brightness", expected=200, order="fast move to level"),
            baseline=10,
            observations=[{"value2": 201}],
        )
        self.assertEqual(verdict.status, "verified")

    def test_brightness_wrong_value_is_ineffective(self):
        verdict = probe.judge(
            _candidate(intent="brightness", expected=200, order="fast move to level"),
            baseline=10,
            observations=[{"value2": 40}],
        )
        self.assertEqual(verdict.status, "ineffective")

    def test_position_verified(self):
        verdict = probe.judge(
            _candidate(intent="position", expected=30, order="open", value1=30),
            baseline=0,
            observations=[{"properties": {"percent": 30}}],
        )
        self.assertEqual(verdict.status, "verified")

    def test_boolean_payload_is_understood(self):
        verdict = probe.judge(
            _candidate(),
            baseline=False,
            observations=[{"properties": {"onoff": {"status": True}}}],
        )
        self.assertEqual(verdict.status, "verified")

    def test_candidate_without_expected_is_inconclusive(self):
        verdict = probe.judge(
            _candidate(intent="brightness", expected=None),
            baseline=None,
            observations=[{"value2": 50}],
        )
        self.assertEqual(verdict.status, "inconclusive")

    def test_verdict_row_is_json_friendly(self):
        import json

        verdict = probe.judge(
            _candidate(), baseline=False, observations=[{"value1": 1}]
        )
        json.dumps(verdict.as_row())  # must not raise


class RenderingTests(unittest.TestCase):
    def _verdicts(self):
        verified_on = probe.judge(
            _candidate(),
            baseline=False,
            observations=[{"properties": {"onoff": {"status": "on"}}}],
        )
        verified_off = probe.judge(
            _candidate(intent="off", expected=False, order="off", value1=1),
            baseline=True,
            observations=[{"properties": {"onoff": {"status": "off"}}}],
        )
        failed = probe.judge(
            _candidate(order="on", value1=1, label="order=on value1=1"),
            baseline=False,
            observations=[{"properties": {"onoff": {"status": "off"}}}],
        )
        return [failed, verified_on, verified_off]

    def test_only_verified_candidates_are_emitted(self):
        control = probe.render_control_block(self._verdicts(), "switch")
        self.assertIn("on", control)
        self.assertIn("off", control)
        self.assertEqual(control["on"]["order"], "on")
        self.assertEqual(control["on"]["value1"], 0)
        # the ineffective value1=1 candidate must not survive
        self.assertNotEqual(control["on"].get("value1"), 1)

    def test_rendered_control_is_accepted_by_the_real_profile_loader(self):
        """The generated block must be usable as-is in a profile.

        Asserts through the integration's own loader rather than raw
        ``yaml.safe_load``: YAML 1.1 turns a bare ``on:`` key into boolean True,
        and the loader is what normalises that back to the action name.
        """

        import importlib
        import tempfile
        import types

        component = ROOT / "custom_components" / "orvibohomebridge"
        package_name = "orvibohomebridge_probe_loader_test"
        if package_name not in sys.modules:
            package = types.ModuleType(package_name)
            package.__path__ = [str(component)]
            sys.modules[package_name] = package
        custom_devices = importlib.import_module(f"{package_name}.custom_devices")

        control = probe.render_control_block(self._verdicts(), "switch")
        text = (
            "profile_version: 1\nid: probe_rendered\nplatform: switch\n"
            "hardware_verified: true\nmatch: {device_type: 9911}\n"
            "state: {state: {from: 'properties.onoff.status', true_values: ['on'], false_values: ['off']}}\n"
        ) + probe.render_yaml(control)

        directory = Path(tempfile.mkdtemp())
        path = directory / "p.yaml"
        path.write_text(text, encoding="utf-8")
        profile = custom_devices.load_profile_file(path)

        self.assertIn("on", profile.control)
        self.assertIn("off", profile.control)
        self.assertEqual(profile.control["on"].order, "on")
        self.assertEqual(profile.control["off"].order, "off")

    def test_properties_survive_rendering(self):
        verdict = probe.judge(
            probe.Candidate(
                label="set property onoff.status=on",
                order="set property",
                properties={"onoff": {"status": "on"}},
                intent="on",
                expected=True,
            ),
            baseline=False,
            observations=[{"properties": {"onoff": {"status": "on"}}}],
        )
        control = probe.render_control_block([verdict], "switch")
        self.assertEqual(control["on"]["properties"], {"onoff": {"status": "on"}})

    def test_no_verified_candidate_yields_empty_block(self):
        control = probe.render_control_block(
            [
                probe.judge(_candidate(), baseline=False, observations=[]),
            ],
            "switch",
        )
        self.assertEqual(control, {})

    def test_summary_marks_statuses(self):
        text = probe.summarise(self._verdicts())
        self.assertIn("✅", text)
        self.assertIn("❌", text)


class BaselineTests(unittest.TestCase):
    def test_baseline_reads_onoff_status(self):
        self.assertIs(
            probe.baseline_from_payload({"properties": {"onoff": {"status": "on"}}}, "on"),
            True,
        )

    def test_baseline_reads_numeric_slot(self):
        self.assertEqual(probe.baseline_from_payload({"value2": 42}, "brightness"), 42.0)

    def test_baseline_is_none_without_evidence(self):
        self.assertIsNone(probe.baseline_from_payload({"value4": 1}, "on"))


class RiskyDeviceTests(unittest.TestCase):
    def test_locks_and_horses_are_flagged(self):
        for device_type in (522, 107, 52, 34, 35):
            with self.subTest(device_type=device_type):
                self.assertIn(device_type, probe.RISKY_DEVICE_TYPES)

    def test_plain_switch_is_not_flagged(self):
        self.assertNotIn(38, probe.RISKY_DEVICE_TYPES)
        self.assertNotIn(501, probe.RISKY_DEVICE_TYPES)


if __name__ == "__main__":
    unittest.main()
