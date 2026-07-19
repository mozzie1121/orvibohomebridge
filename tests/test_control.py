"""Tests for command mappings captured from the Orvibo app."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest

MODULE_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "orvibohomebridge"
    / "control.py"
)
SPEC = importlib.util.spec_from_file_location("orvibohomebridge_control", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
control = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = control
SPEC.loader.exec_module(control)


class ControlTests(unittest.TestCase):
    # ---- 窗帘 ----

    def test_curtain_positions(self) -> None:
        self.assertEqual(control.curtain_position_command(100).value1, 100)
        self.assertEqual(control.curtain_position_command(0).value1, 0)
        self.assertEqual(control.curtain_position_command(67).value1, 67)
        self.assertEqual(control.curtain_stop_command().order, "stop")
        self.assertEqual(control.curtain_position_from_orvibo(100), 100)
        self.assertEqual(control.curtain_position_from_orvibo(0), 0)
        self.assertIsNone(control.curtain_position_from_orvibo(101))

    # ---- 开关（set property） ----

    def test_switch_power(self) -> None:
        on = control.switch_power_command(True)
        off = control.switch_power_command(False)
        self.assertEqual(on.order, "set property")
        self.assertEqual(on.properties, {"onoff": {"status": "on"}})
        self.assertEqual(off.properties, {"onoff": {"status": "off"}})

    # ---- 灯控制（order=on/off） ----

    def test_light_power_mapping_active_low(self) -> None:
        """验证 active-low 控制：value1=0 开, value1=1 关。"""
        on = control.light_power_command(True, 128, 262)
        off = control.light_power_command(False, 128, 262)
        self.assertEqual((on.order, on.value1, on.value2, on.value3),
                         ("on", 0, 128, 262))
        self.assertEqual((off.order, off.value1, off.value2, off.value3),
                         ("off", 1, 128, 262))
        self.assertTrue(control.light_is_on_from_value1(0))
        self.assertFalse(control.light_is_on_from_value1(1))

    def test_light_full_command(self) -> None:
        cmd = control.light_full_command(True, 146, 250)
        self.assertEqual((cmd.order, cmd.value1, cmd.value2, cmd.value3),
                         ("on", 0, 146, 250))
        cmd_off = control.light_full_command(False, 0, 0)
        self.assertEqual((cmd_off.order, cmd_off.value1),
                         ("off", 1))

    def test_light_brightness_command(self) -> None:
        cmd = control.light_brightness_command(146)
        self.assertEqual((cmd.order, cmd.value1, cmd.value2),
                         ("fast move to level", 0, 146))

    def test_light_colortemp_command(self) -> None:
        cmd = control.light_colortemp_command(146, 250)
        self.assertEqual((cmd.order, cmd.value1, cmd.value2, cmd.value3),
                         ("fast color temperature", 0, 146, 250))

    # ---- Zigbee 调光灯（type=0, subType=-2） ----

    def test_zigbee_dimmable_light_power(self) -> None:
        on = control.zigbee_dimmable_light_power_command(True, 200)
        off = control.zigbee_dimmable_light_power_command(False, 255)
        self.assertEqual((on.order, on.value1, on.value2),
                         ("on", 0, 200))
        self.assertEqual((off.order, off.value1, off.value2),
                         ("off", 1, 255))

    def test_zigbee_dimmable_brightness(self) -> None:
        cmd = control.zigbee_dimmable_light_brightness_command(128)
        self.assertEqual((cmd.order, cmd.value1, cmd.value2),
                         ("move to level", 0, 128))

    def test_zigbee_dimmable_is_on(self) -> None:
        self.assertTrue(control.zigbee_dimmable_is_on(0, -2))
        self.assertFalse(control.zigbee_dimmable_is_on(1, -2))
        # subType != -2 时 active-high
        self.assertTrue(control.zigbee_dimmable_is_on(1, 0))
        self.assertFalse(control.zigbee_dimmable_is_on(0, 0))
        self.assertIsNone(control.zigbee_dimmable_is_on(None, -2))
        self.assertIsNone(control.zigbee_dimmable_is_on(2, -2))

    # ---- Fast Move 调光调色灯 ----

    def test_fast_move_light_power(self) -> None:
        on = control.fast_move_light_power_command(True, 128, 250)
        off = control.fast_move_light_power_command(False, 0, 0)
        self.assertEqual((on.order, on.value1, on.value2, on.value3),
                         ("on", 0, 128, 250))
        self.assertEqual((off.order, off.value1),
                         ("off", 1))

    def test_fast_move_brightness(self) -> None:
        cmd = control.fast_move_light_brightness_command(128, 250)
        self.assertEqual((cmd.order, cmd.value1, cmd.value2, cmd.value3),
                         ("fast move to level", 0, 128, 250))

    def test_fast_move_colortemp(self) -> None:
        cmd = control.fast_move_light_colortemp_command(128, 250)
        self.assertEqual((cmd.order, cmd.value1, cmd.value2, cmd.value3),
                         ("fast color temperature", 0, 128, 250))

    # ---- 色温灯（set property, type=503） ----

    def test_cct_light_power(self) -> None:
        self.assertEqual(control.cct_light_power_command(True).properties,
                         {"onoff": {"status": "on"}})
        self.assertEqual(control.cct_light_power_command(False).properties,
                         {"onoff": {"status": "off"}})

    def test_cct_light_brightness(self) -> None:
        cmd = control.cct_light_brightness_command(50)
        self.assertEqual(cmd.properties, {"brightness": {"percent": 50}})

    def test_cct_light_colortemp(self) -> None:
        cmd = control.cct_light_colortemp_command(4000)
        self.assertEqual(cmd.properties,
                         {"colorTemp": {"value": control.kelvin_to_mired(4000)}})

    # ---- 可调光灯（type=502） ----

    def test_dimmable_light_brightness(self) -> None:
        cmd = control.dimmable_light_brightness_command(75)
        self.assertEqual(cmd.properties, {"brightness": {"percent": 75}})

    # ---- 空调 ----

    def test_fan_coil_ac_power(self) -> None:
        on = control.fan_coil_ac_power_command(True, 3, 1, 2500 << 16)
        off = control.fan_coil_ac_power_command(False, 3, 1, 2500 << 16)
        self.assertEqual((on.order, on.value1, on.value2, on.value3),
                         ("on", 0, 3, 1))
        self.assertEqual((off.order, off.value1),
                         ("off", 1))

    def test_ac_temperature_encode(self) -> None:
        self.assertEqual(control.ac_temperature_encode(25.0), 2500 << 16)
        self.assertEqual(control.ac_temperature_encode(20.0), 2000 << 16)

    def test_ac_mode_fan_speed_maps(self) -> None:
        self.assertEqual(control.AC_MODE_MAP["cool"], 3)
        self.assertEqual(control.AC_MODE_REVERSE[3], "cool")
        self.assertEqual(control.AC_FAN_SPEED_MAP["high"], 3)
        self.assertEqual(control.AC_FAN_SPEED_REVERSE[3], "high")

    def test_ac_mode_command(self) -> None:
        cmd = control.fan_coil_ac_mode_command(4, 2800 << 16)
        self.assertEqual((cmd.order, cmd.value1, cmd.value2, cmd.value4),
                         ("mode setting", 0, 4, 2800 << 16))

    def test_ac_temperature_command(self) -> None:
        cmd = control.fan_coil_ac_temperature_command(3, 2, 2500 << 16)
        self.assertEqual((cmd.order, cmd.value1, cmd.value2, cmd.value3, cmd.value4),
                         ("temperature setting", 0, 3, 2, 2500 << 16))

    def test_ac_fan_speed_command(self) -> None:
        cmd = control.fan_coil_ac_fan_speed_command(3, 1, 2500 << 16)
        self.assertEqual((cmd.order, cmd.value1, cmd.value2, cmd.value3, cmd.value4),
                         ("wind setting", 0, 3, 1, 2500 << 16))

    # ---- 新风 ----

    def test_ventilation_command(self) -> None:
        self.assertEqual(control.ventilation_command(0).value1, 0)
        self.assertEqual(control.ventilation_command(50).value1, 50)
        self.assertEqual(control.ventilation_command(100).value1, 100)

    def test_ventilation_fan_speed_from_value1(self) -> None:
        self.assertEqual(control.ventilation_fan_speed_from_value1(0), "慢")
        self.assertEqual(control.ventilation_fan_speed_from_value1(50), "停")
        self.assertEqual(control.ventilation_fan_speed_from_value1(100), "快")
        self.assertIsNone(control.ventilation_fan_speed_from_value1(None))

    # ---- 色温转换 ----

    def test_kelvin_to_mired(self) -> None:
        self.assertEqual(control.kelvin_to_mired(4000), 250)
        self.assertEqual(control.kelvin_to_mired(2700), 370)
        self.assertEqual(control.kelvin_to_mired(6500), 154)

    def test_mired_to_kelvin(self) -> None:
        self.assertEqual(control.mired_to_kelvin(250), 4000)
        self.assertEqual(control.mired_to_kelvin(0), None)
        self.assertEqual(control.mired_to_kelvin(-1), None)

    # ---- build_control_payload ----

    def test_build_control_payload(self) -> None:
        cmd = control.light_power_command(True, 128, 262)
        payload = control.build_control_payload(
            cmd, device_id="dev-001", device_uid="uid-001",
            username="test@test.com", serial=1700000,
        )
        self.assertEqual(payload["cmd"], 15)
        self.assertEqual(payload["uid"], "uid-001")
        self.assertEqual(payload["deviceId"], "dev-001")
        self.assertEqual(payload["order"], "on")
        self.assertEqual(payload["value1"], 0)
        self.assertEqual(payload["ver"], "5.1.3.309")

    def test_build_payload_with_properties(self) -> None:
        cmd = control.switch_power_command(True)
        payload = control.build_control_payload(
            cmd, "dev-001", "uid-001", "u", 1,
        )
        self.assertIn("properties", payload)
        self.assertEqual(payload["properties"], {"onoff": {"status": "on"}})

    # ---- 状态解析函数 ----

    def test_light_is_on_from_value1(self) -> None:
        self.assertTrue(control.light_is_on_from_value1(0))
        self.assertFalse(control.light_is_on_from_value1(1))
        self.assertIsNone(control.light_is_on_from_value1(None))
        self.assertIsNone(control.light_is_on_from_value1(2))

    def test_switch_is_on_from_value1(self) -> None:
        self.assertFalse(control.switch_is_on_from_value1(0))
        self.assertTrue(control.switch_is_on_from_value1(1))
        self.assertIsNone(control.switch_is_on_from_value1(None))

    def test_curtain_position_from_value1(self) -> None:
        self.assertIsNone(control.curtain_position_from_value1(None))
        self.assertEqual(control.curtain_position_from_value1(50), 50)
        self.assertIsNone(control.curtain_position_from_value1(101))
        self.assertEqual(control.curtain_position_from_value1(0), 0)
        self.assertEqual(control.curtain_position_from_value1(100), 100)

    # ---- OrviboControlCommand frozen ----

    def test_command_is_frozen(self) -> None:
        cmd = control.OrviboControlCommand("on", 0)
        with self.assertRaises(AttributeError):
            cmd.order = "off"  # type: ignore[misc]

    # ---- control_payload_to_command ----

    def test_control_payload_to_command(self) -> None:
        payload = {
            "order": "fast move to level",
            "value1": 0,
            "value2": 146,
            "value3": 250,
            "value4": 0,
            "properties": None,
        }
        cmd = control.control_payload_to_command(payload)
        self.assertEqual(cmd.order, "fast move to level")
        self.assertEqual(cmd.value2, 146)

    def test_clamp_brightness(self) -> None:
        self.assertEqual(control.clamp_brightness(0), 1)
        self.assertEqual(control.clamp_brightness(128), 128)
        self.assertEqual(control.clamp_brightness(300), 255)
        self.assertEqual(control.clamp_brightness(-5), 1)
        self.assertEqual(control.clamp_brightness(255, max_val=100), 100)


    # ---- 空调控制 order 验证（v0.2.0 修复：对齐抓包 order） ----

    def test_ac_power_on_uses_order_on(self) -> None:
        """AC 开机使用 order='on'（非 'set property'）。"""
        cmd = control.fan_coil_ac_power_command(True, 3, 1, 2500 << 16)
        self.assertEqual(cmd.order, "on")
        self.assertEqual(cmd.value1, 0)

    def test_ac_power_off_uses_order_off(self) -> None:
        """AC 关机使用 order='off'（非 'set property'）。"""
        cmd = control.fan_coil_ac_power_command(False, 3, 1, 2500 << 16)
        self.assertEqual(cmd.order, "off")
        self.assertEqual(cmd.value1, 1)

    def test_ac_mode_setting_order(self) -> None:
        """AC 切模式使用 order='mode setting'。"""
        cmd = control.fan_coil_ac_mode_command(4, 2800 << 16)
        self.assertEqual(cmd.order, "mode setting")
        self.assertEqual(cmd.value2, 4)

    def test_ac_temperature_setting_order(self) -> None:
        """AC 设温使用 order='temperature setting'。"""
        cmd = control.fan_coil_ac_temperature_command(3, 1, 2500 << 16)
        self.assertEqual(cmd.order, "temperature setting")

    def test_ac_fan_speed_setting_order(self) -> None:
        """AC 风速使用 order='wind setting'。"""
        cmd = control.fan_coil_ac_fan_speed_command(3, 2, 2500 << 16)
        self.assertEqual(cmd.order, "wind setting")
        self.assertEqual(cmd.value3, 2)


if __name__ == "__main__":
    unittest.main()
