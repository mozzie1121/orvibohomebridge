"""Properties merge ported from the derivative repo, verified with real samples.

Samples come from live `readtable` snapshots:
  * 灯5 (classId 426): properties.onoff / brightness / colorTemp
  * MixPad virtual floor heating: properties.thermostat + onoff
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "orvibo_protocol",
    ROOT / "custom_components" / "orvibohomebridge" / "protocol.py",
)
protocol = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = protocol  # dataclasses need the module registered
SPEC.loader.exec_module(protocol)

LIGHT_PROPS = {
    "onoff": {"status": "on", "powerOnStatus": "memory"},
    "brightness": {"percent": 100, "slowBrightTime": 2},
    "colorTemp": {"value": 4000, "minValue": 2700, "maxValue": 6500},
    "Descriptor": {"classId": 426.0, "deviceId": "w-19ed9c2f", "deviceName": "灯5"},
}
FLOOR_PROPS = {
    "thermostat": {"targetTemp": 23.0, "localTemp": 24, "localHumidity": 43,
                   "runMode": "control", "minHeatingTemp": 8.0, "maxHeatingTemp": 35.0},
    "onoff": {"status": "off"},
    "PhysicalDevInfo": {"pid": "MixPadFloorHeat"},
}


def test_merge_properties_is_deep_and_non_mutating():
    base = {"onoff": {"status": "off"}, "brightness": {"percent": 10}}
    patch = {"onoff": {"status": "on"}, "colorTemp": {"value": 3000}}
    merged = protocol._merge_properties(base, patch)
    assert merged["onoff"] == {"status": "on"}
    assert merged["brightness"] == {"percent": 10}
    assert merged["colorTemp"] == {"value": 3000}
    assert base["onoff"]["status"] == "off", "base must not be mutated"


def test_apply_status_properties_light_and_flat_aliases():
    device = {"deviceId": "w-19ed9c2f", "deviceName": "灯5", "properties": {}}
    status = {"deviceId": "w-19ed9c2f", "value1": -1, "properties": LIGHT_PROPS,
              "updateTimeSec": 1773567730}
    out = protocol.apply_status_properties(device, status)
    assert out["properties"]["onoff"]["status"] == "on"
    assert out["properties"]["Descriptor"]["classId"] == 426.0
    flat = out["property_flat"]
    assert flat["onoff.status"] == "on"
    assert flat["brightness.percent"] == 100
    assert flat["colorTemp.value"] == 4000
    assert flat["Descriptor.classId"] == 426.0


def test_apply_status_properties_floor_heating():
    device = {"deviceId": "w-0f16e327", "deviceName": "地暖", "properties": {}}
    status = {"deviceId": "w-0f16e327", "properties": FLOOR_PROPS}
    out = protocol.apply_status_properties(device, status)
    flat = out["property_flat"]
    assert flat["thermostat.targetTemp"] == 23.0
    assert flat["thermostat.localTemp"] == 24
    assert flat["onoff.status"] == "off"


def test_apply_status_properties_noop_without_properties():
    device = {"deviceId": "x", "properties": {"a": 1}}
    out = protocol.apply_status_properties(device, {"deviceId": "x"})
    assert out["properties"] == {"a": 1}
    assert "property_flat" not in out


def test_parse_camera_types_discards_secrets_and_bad_types():
    rows = [
        {"uid": "u1", "type": 3, "username": "secret", "password": "secret"},
        {"uid": "u2", "type": "8"},
        {"uid": "u3", "type": True},
        {"uid": "", "type": 5},
        {"uid": "u4", "type": 1, "delFlag": 1},
    ]
    parsed = protocol.parse_camera_types(rows)
    assert parsed["u1"] == 3
    assert parsed["u2"] == 8
    assert parsed["u3"] is None
    assert "" not in parsed
    assert parsed["u4"] is None


def test_table_rows_and_row_update_time():
    rows = protocol._table_rows({"a": {"type": 1}, "b": {"uid": "b", "type": 2}}, "uid")
    assert {row["uid"] for row in rows} == {"a", "b"}
    assert protocol._row_update_time({"updateTimeSec": 1773567730}) == 1773567730000
    assert protocol._row_update_time({}) == 0


def test_property_state_aliases_light():
    flat = {"onoff.status": "on", "brightness.percent": 100, "colorTemp.value": 4000}
    assert protocol.property_state_aliases(flat) == {
        "state": True, "brightness": 100, "color_temp": 4000}


def test_property_state_aliases_floor_heating():
    aliases = protocol.property_state_aliases({
        "onoff.status": "off",
        "thermostat.targetTemp": 23.0,
        "thermostat.localTemp": 24,
        "thermostat.localHumidity": 43,
    })
    assert aliases["state"] is False
    assert aliases["target_temperature"] == 23.0
    assert aliases["current_temperature"] == 24
    assert aliases["humidity"] == 43


def test_apply_property_aliases_keeps_fresh_values():
    state = {"state": False, "brightness": None,
             "property_flat": {"onoff.status": "on", "brightness.percent": 55}}
    out = protocol.apply_property_aliases(dict(state))
    assert out["state"] is False
    assert out["brightness"] == 55


def test_property_state_aliases_ignores_unknown_shapes():
    assert protocol.property_state_aliases({"onoff.status": "control"}) == {}
    assert protocol.property_state_aliases({}) == {}
    assert protocol.property_state_aliases(None) == {}
