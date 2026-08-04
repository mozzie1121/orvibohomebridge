"""Regression tests derived from hardware capture packets."""

from __future__ import annotations

import importlib
from pathlib import Path
import sys
import types
import unittest

COMPONENT_PATH = Path(__file__).parents[1] / "custom_components" / "orvibohomebridge"


def _load_packet():
    package_name = "orvibohomebridge_verified_packet_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT_PATH)]
    sys.modules[package_name] = package
    return importlib.import_module(f"{package_name}.packet")


class VerifiedDevicePacketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.packet = _load_packet().HomemateJsonData

    def test_legacy_cover_uses_open_for_zero_position(self) -> None:
        payload = self.packet.ssl_control_cover("user", "device", "gateway", 0)
        self.assertEqual((payload["order"], payload["value1"]), ("open", 0))

    def test_tubular_motor_stop_carries_255(self) -> None:
        payload = self.packet.ssl_control_cover(
            "user", "device", "gateway", "stop", stop_value2=255
        )
        self.assertEqual((payload["order"], payload["value2"]), ("stop", 255))

    def test_dream_curtain_property_packets(self) -> None:
        action = self.packet.ssl_control_dream_curtain_action(
            "user", "device", "gateway", "pause"
        )
        percent = self.packet.ssl_control_dream_curtain_percent(
            "user", "device", "gateway", 39
        )
        angle = self.packet.ssl_control_dream_curtain_angle(
            "user", "device", "gateway", 170
        )
        self.assertEqual(action["properties"], {"curtain": {"action": "pause"}})
        self.assertEqual(percent["properties"], {"curtain": {"percent": 39}})
        self.assertEqual(angle["properties"], {"curtain": {"angle": 170}})

    def test_floor_heating_property_packets(self) -> None:
        power = self.packet.ssl_control_floor_heating_power(
            "user", "device", "gateway", True
        )
        target = self.packet.ssl_control_floor_heating_temperature(
            "user", "device", "gateway", 21
        )
        self.assertEqual(power["properties"], {"onoff": {"status": "on"}})
        self.assertEqual(
            target["properties"], {"thermostat": {"targetTemp": 21}}
        )

    def test_legacy_floor_heating_packets_match_capture(self) -> None:
        power_on = self.packet.ssl_control_legacy_floor_heating(
            "user", "device", "gateway", order="on", value1=0, value2=0
        )
        power_off = self.packet.ssl_control_legacy_floor_heating(
            "user", "device", "gateway", order="off", value1=1, value2=6932
        )
        target_25 = self.packet.ssl_control_legacy_floor_heating(
            "user",
            "device",
            "gateway",
            order="temperature setting",
            value1=8,
            value2=15,
        )
        self.assertEqual((power_on["order"], power_on["value1"], power_on["value2"]), ("on", 0, 0))
        self.assertEqual((power_off["order"], power_off["value1"], power_off["value2"]), ("off", 1, 6932))
        self.assertEqual((target_25["order"], target_25["value1"], target_25["value2"]), ("temperature setting", 8, 15))


if __name__ == "__main__":
    unittest.main()
