"""Tests for inbound SSL status dispatch without Home Assistant."""

from __future__ import annotations

import importlib
from pathlib import Path
import sys
import types
import unittest

COMPONENT_PATH = Path(__file__).parents[1] / "custom_components" / "orvibohomebridge"


def _load_modules():
    package_name = "orvibohomebridge_status_dispatcher_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT_PATH)]
    sys.modules[package_name] = package
    dispatcher = importlib.import_module(f"{package_name}.status_dispatcher")
    state_store = importlib.import_module(f"{package_name}.state_store")
    return dispatcher, state_store


class StatusDispatcherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module, cls.state_store = _load_modules()

    def make_dispatcher(self, devices=None, states=None, diagnostic_limit=200):
        devices = devices or {}
        states = states or {}
        calls = {
            "motion": [],
            "emergency": [],
            "transient": [],
            "message": [],
            "lock": [],
            "updated": 0,
        }
        diagnostics = []
        updates = {}

        def updated():
            calls["updated"] += 1

        dispatcher = self.module.StatusUpdateDispatcher(
            devices,
            states,
            self.state_store.StateStore(states),
            updates,
            diagnostics,
            on_motion=lambda state, raw, device_id: calls["motion"].append(device_id),
            on_emergency=lambda state, raw, device_id: calls["emergency"].append(device_id),
            on_lock_transient=lambda state, raw, device_id: calls["transient"].append(device_id),
            on_lock_message=lambda device_id, raw: calls["message"].append(device_id),
            on_lock_event=lambda device_id, raw: calls["lock"].append(device_id),
            on_updated=updated,
            clock=lambda: 123.0,
            diagnostic_limit=diagnostic_limit,
        )
        return dispatcher, calls, diagnostics, updates

    def test_resolves_w_prefixed_local_id_after_uid_miss(self) -> None:
        states = {"w-device": {"uid": "different"}}
        dispatcher, _, _, _ = self.make_dispatcher(states=states)

        resolved = dispatcher.resolve_device_id(
            "device", {"uid": "unmatched-but-present"}
        )

        self.assertEqual(resolved, "w-device")

    def test_gateway_uid_is_never_used_as_device_identity(self) -> None:
        states = {
            "lamp-a": {"uid": "shared-gateway"},
            "lamp-b": {"uid": "shared-gateway"},
        }
        dispatcher, _, _, _ = self.make_dispatcher(states=states)
        self.assertIsNone(
            dispatcher.resolve_device_id("unmatched", {"uid": "shared-gateway"})
        )

    def test_status_alias_resolves_without_uid(self) -> None:
        devices = {"curtain": {"status_id": "status-curtain"}}
        states = {"curtain": {"uid": "gateway"}}
        dispatcher, _, _, _ = self.make_dispatcher(devices, states)
        self.assertEqual(
            dispatcher.resolve_device_id("status-curtain", {"uid": "gateway"}),
            "curtain",
        )

    def test_partial_properties_are_deep_merged(self) -> None:
        devices = {"lamp": {"device_type_raw": 503, "sub_device_type": 461}}
        states = {"lamp": {"properties": {"brightness": {"percent": 50}}}}
        dispatcher, _, _, _ = self.make_dispatcher(devices, states)
        dispatcher.dispatch(
            "lamp", {"properties": {"colorTemp": {"value": 3500}}}
        )
        self.assertEqual(states["lamp"]["properties"]["brightness"]["percent"], 50)
        self.assertEqual(states["lamp"]["properties"]["colorTemp"]["value"], 3500)

    def test_lock_event_carries_source(self) -> None:
        devices = {"lock": {"device_type_raw": 522, "sub_device_type": 463}}
        states = {"lock": {"properties": {}}}
        captured: list[str] = []

        def on_lock_event(device_id: str, raw: dict) -> None:
            captured.append(raw.get("source", ""))

        diagnostics: list[dict] = []
        updates: dict[str, float] = {}
        dispatcher = self.module.StatusUpdateDispatcher(
            devices,
            states,
            self.state_store.StateStore(states),
            updates,
            diagnostics,
            on_motion=lambda *_: None,
            on_emergency=lambda *_: None,
            on_lock_transient=lambda *_: None,
            on_lock_message=lambda *_: None,
            on_lock_event=on_lock_event,
            on_updated=lambda: None,
        )
        dispatcher.dispatch(
            "lock",
            {"cmd": 42, "properties": {"doorLock": {"doorState": "on"}}},
            source=self.state_store.StateSource.LAN,
        )
        self.assertEqual(captured, ["lan"])

    def test_light_packet_uses_registered_parser_and_notifies(self) -> None:
        devices = {"lamp": {"device_type_raw": 501}}
        states = {"lamp": {"state": False}}
        dispatcher, calls, _, updates = self.make_dispatcher(devices, states)

        dispatcher.dispatch(
            "lamp", {"properties": {"onoff": {"status": "on"}}}
        )

        self.assertTrue(states["lamp"]["state"])
        self.assertTrue(states["lamp"]["online"])
        self.assertEqual(calls["updated"], 1)
        self.assertEqual(updates["lamp"], 123.0)

    def test_motion_and_lock_callbacks_remain_coordinator_owned(self) -> None:
        devices = {
            "motion": {"device_type_raw": 26},
            "lock": {"device_type_raw": 522},
        }
        states = {"motion": {}, "lock": {}}
        dispatcher, calls, _, _ = self.make_dispatcher(devices, states)

        dispatcher.dispatch("motion", {"value3": 1})
        dispatcher.dispatch("lock", {"properties": {"doorLock": {}}})

        self.assertEqual(calls["motion"], ["motion"])
        self.assertEqual(calls["lock"], ["lock"])

    def test_cmd82_publishes_message_without_entity_update(self) -> None:
        devices = {"lock": {"device_type_raw": 522}}
        states = {"lock": {}}
        dispatcher, calls, _, _ = self.make_dispatcher(devices, states)

        dispatcher.dispatch("lock", {"cmd": 82, "properties": {}})

        self.assertEqual(calls["message"], ["lock"])
        self.assertEqual(calls["updated"], 0)

    def test_diagnostics_are_bounded(self) -> None:
        dispatcher, _, diagnostics, _ = self.make_dispatcher(diagnostic_limit=2)

        for index in range(3):
            dispatcher.dispatch(f"missing-{index}", {"index": index})

        self.assertEqual([item["raw"]["index"] for item in diagnostics], [1, 2])


if __name__ == "__main__":
    unittest.main()
