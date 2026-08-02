"""Fixture-driven smoke tests: every real-world lock sample must parse cleanly."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import unittest

FIXTURE = Path(__file__).parent / "fixtures" / "lock_v5eyes_samples.jsonl"
MODULE_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "orvibohomebridge"
    / "lock_status.py"
)
SPEC = importlib.util.spec_from_file_location("orvibohomebridge_lock_status", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
lock_status = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = lock_status
SPEC.loader.exec_module(lock_status)


class CaptureFixtureTests(unittest.TestCase):
    def test_every_sample_parses_to_a_known_shape(self) -> None:
        kinds: list[str] = []
        with FIXTURE.open(encoding="utf-8") as f:
            for line in f:
                rec = json.loads(line)
                packet = rec["packet"]
                state = lock_status.normalize_door_lock_properties(packet.get("properties"))
                event = lock_status.normalize_lock_event(packet)
                message = lock_status.normalize_message_event(packet)
                has_state = any(
                    value is not None
                    for key, value in state.items()
                    if key != "raw"
                )
                self.assertTrue(
                    has_state or event is not None or message is not None,
                    f"sample did not parse: {line}",
                )
                if event is not None:
                    kinds.append(f"event:{event['kind']}")
                elif message is not None:
                    kinds.append(f"message:{message['kind']}")

        for expected in (
            "event:unlock",
            "event:error_unlock",
            "event:door_unclose",
            "event:picklock",
            "event:leave_home",
            "event:ring",
            "message:message",
        ):
            self.assertIn(expected, kinds)
        self.assertEqual(len(kinds), 15)

    def test_real_state_sequences_are_consistent(self) -> None:
        """doorLock 的 lockState=on 必须归一化为 locked=True。"""
        locked = lock_status.normalize_door_lock_properties(
            {"doorLock": {"doorState": "off", "lockState": "on"}}
        )
        self.assertIs(locked["locked"], True)
        self.assertIs(locked["door_open"], False)
        unlocked = lock_status.normalize_door_lock_properties(
            {"doorLock": {"doorState": "off", "lockState": "off"}}
        )
        self.assertIs(unlocked["locked"], False)


if __name__ == "__main__":
    unittest.main()
