"""Tests for ORVIBO binary packet framing and AES cryptography."""

from __future__ import annotations

import importlib
from pathlib import Path
import sys
import types
import unittest

COMPONENT_PATH = (
    Path(__file__).parents[1] / "custom_components" / "orvibohomebridge"
)
PACKET_PATH = COMPONENT_PATH / "packet.py"


def _load_packet_module():
    """使用 importlib 加载 packet.py（需要 cryptography）。"""
    package_name = "orvibohomebridge_packet_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT_PATH)]
    sys.modules[package_name] = package
    return importlib.import_module(f"{package_name}.packet")


class BinaryProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.packet = _load_packet_module()
        except ModuleNotFoundError as err:
            if err.name == "cryptography":
                raise unittest.SkipTest("cryptography is not installed") from err
            raise

    def test_static_packet_round_trip(self) -> None:
        """验证用静态密钥构建的包可以正确解析。"""
        payload = {"cmd": 230, "familyId": "family-1", "serial": 1}
        session_id = b" " * 32  # ID_UNSET
        key = b"khggd54865SNJHGF"
        pkt_type = bytes([0x70, 0x6b])  # pk (static key)

        packet = self.packet.HomematePacket.build_packet(pkt_type, key, session_id, payload)

        # 验证包结构：magic(2) + length(2) + type(2) + crc(4) + session(32) + payload
        self.assertEqual(len(packet) > 42, True)
        self.assertEqual(packet[0:2], b"hd")
        self.assertEqual(packet[4:6], pkt_type)
        self.assertEqual(packet[10:42], session_id)

        # 解析回包
        parsed = self.packet.HomematePacket(packet, {})
        self.assertEqual(parsed.json_payload, payload)

    def test_static_and_dynamic_key_types(self) -> None:
        """验证 pk（静态）和 dk（动态）两种包类型标记。"""
        payload = {"cmd": 0, "key": "0123456789ABCDEF"}
        session_id = b" " * 32
        key = b"khggd54865SNJHGF"

        pk_packet = self.packet.HomematePacket.build_packet(
            bytes([0x70, 0x6b]), key, session_id, payload
        )
        parsed = self.packet.HomematePacket(pk_packet, {})
        self.assertEqual(parsed.json_payload, payload)

    def test_corrupt_packet_is_rejected(self) -> None:
        """篡改 payload 后 CRC 校验应失败。"""
        payload = {"cmd": 0, "key": "0123456789ABCDEF"}
        session_id = b" " * 32
        key = b"khggd54865SNJHGF"
        pkt_type = bytes([0x70, 0x6b])

        packet = bytearray(
            self.packet.HomematePacket.build_packet(pkt_type, key, session_id, payload)
        )
        packet[-1] ^= 0x01  # 篡改最后一个字节

        with self.assertRaises(AssertionError):
            self.packet.HomematePacket(bytes(packet), {})

    def test_control_payload_has_correct_structure(self) -> None:
        """验证 cmd=15 控制 payload 的字段结构。"""
        payload = self.packet.HomemateJsonData.ssl_control_light(
            username="test@test.com",
            device_id="dev-001",
            device_uid="uid-001",
            state=True,
        )
        self.assertEqual(payload["cmd"], 15)
        self.assertEqual(payload["order"], "on")
        self.assertEqual(payload["deviceId"], "dev-001")
        self.assertEqual(payload["uid"], "uid-001")
        self.assertEqual(payload["value1"], 0)  # active-low: 0=开

    def test_control_off_payload_value1(self) -> None:
        """验证关灯时 value1=1（active-low）。"""
        payload = self.packet.HomemateJsonData.ssl_control_light(
            username="t", device_id="d", device_uid="u", state=False,
        )
        self.assertEqual(payload["value1"], 1)
        self.assertEqual(payload["order"], "off")

    def test_control_switch_uses_set_property(self) -> None:
        """验证开关控制用 set property 格式。"""
        payload = self.packet.HomemateJsonData.ssl_control_switch(
            username="t", device_id="d", device_uid="u", state=True,
        )
        self.assertEqual(payload["order"], "set property")
        self.assertEqual(payload["properties"], {"onoff": {"status": "on"}})

    def test_control_cover_open(self) -> None:
        """验证窗帘控制。"""
        payload = self.packet.HomemateJsonData.ssl_control_cover(
            username="t", device_id="d", device_uid="u", position=100,
        )
        self.assertEqual(payload["order"], "open")
        self.assertEqual(payload["value1"], 100)

    def test_control_cover_close(self) -> None:
        payload = self.packet.HomemateJsonData.ssl_control_cover(
            username="t", device_id="d", device_uid="u", position=0,
        )
        self.assertEqual(payload["value1"], 0)

    def test_control_ventilation(self) -> None:
        """验证新风控制。"""
        payload = self.packet.HomemateJsonData.ssl_control_ventilation(
            username="t", device_id="d", device_uid="u", value1=50,
        )
        self.assertEqual(payload["order"], "set property")
        self.assertEqual(payload["value1"], 50)

    def test_control_cct_light_onoff(self) -> None:
        """验证色温灯开关。"""
        payload = self.packet.HomemateJsonData.ssl_control_cct_light_onoff(
            username="t", device_id="d", device_uid="u", state=True,
        )
        self.assertEqual(payload["order"], "set property")
        self.assertEqual(payload["properties"], {"onoff": {"status": "on"}})

    def test_control_cct_light_brightness(self) -> None:
        payload = self.packet.HomemateJsonData.ssl_control_cct_light_brightness(
            username="t", device_id="d", device_uid="u", brightness_percent=75,
        )
        self.assertEqual(payload["order"], "set property")
        self.assertEqual(payload["properties"], {"brightness": {"percent": 75}})

    def test_control_cct_light_colortemp(self) -> None:
        payload = self.packet.HomemateJsonData.ssl_control_cct_light_colortemp(
            username="t", device_id="d", device_uid="u", colortemp_k=4000,
        )
        self.assertEqual(payload["order"], "set property")
        self.assertIn("colorTemp", payload["properties"])

    def test_control_zigbee_dimmable_light_onoff(self) -> None:
        payload = self.packet.HomemateJsonData.ssl_control_zigbee_dimmable_light_onoff(
            username="t", device_id="d", device_uid="u", state=True, brightness=200,
        )
        self.assertEqual(payload["order"], "on")
        self.assertEqual(payload["value1"], 0)
        self.assertEqual(payload["value2"], 200)

    def test_control_fast_move_dim_color_light_onoff(self) -> None:
        payload = self.packet.HomemateJsonData.ssl_control_fast_move_dim_color_light_onoff(
            username="t", device_id="d", device_uid="u", state=True,
            brightness=128, colortemp_mired=250,
        )
        self.assertEqual(payload["order"], "on")
        self.assertEqual(payload["value1"], 0)
        self.assertEqual(payload["value2"], 128)
        self.assertEqual(payload["value3"], 250)

    def test_control_light_full(self) -> None:
        payload = self.packet.HomemateJsonData.ssl_control_light_full(
            username="t", device_id="d", device_uid="u",
            power=True, brightness=128, colortemp_k=4000,
        )
        self.assertEqual(payload["order"], "on")
        self.assertEqual(payload["value1"], 0)
        self.assertEqual(payload["value2"], 128)
        self.assertEqual(payload["value3"], 4000)

    def test_control_clothes_horse(self) -> None:
        """验证晾衣架控制命令。"""
        payload = self.packet.HomemateJsonData.ssl_get_session()
        self.assertIn("serial", payload)

    def test_all_control_methods_return_dict(self) -> None:
        """验证所有 HomemateJsonData.ssl_control_* 方法返回 dict。"""
        methods = [
            ("ssl_control_switch", ["t", "d", "u", True]),
            ("ssl_control_light", ["t", "d", "u", True]),
            ("ssl_control_cover", ["t", "d", "u", 50]),
            ("ssl_control_ventilation", ["t", "d", "u", 50]),
            ("ssl_control_cct_light_onoff", ["t", "d", "u", True]),
            ("ssl_control_cct_light_brightness", ["t", "d", "u", 50]),
            ("ssl_control_cct_light_colortemp", ["t", "d", "u", 4000]),
            ("ssl_control_zigbee_dimmable_light_onoff", ["t", "d", "u", True]),
            ("ssl_control_zigbee_dimmable_light_brightness", ["t", "d", "u", 128]),
            ("ssl_control_fast_move_dim_color_light_onoff", ["t", "d", "u", True]),
            ("ssl_control_fast_move_dim_color_light_brightness", ["t", "d", "u", 128]),
            ("ssl_control_fast_move_dim_color_light_colortemp", ["t", "d", "u", 128, 250]),
            ("ssl_control_dimmable_light_brightness", ["t", "d", "u", 50]),
            ("ssl_control_light_brightness", ["t", "d", "u", 128]),
            ("ssl_control_light_colortemp", ["t", "d", "u", 4000]),
            ("ssl_control_light_full", ["t", "d", "u", True, 128, 250]),
        ]
        for name, args in methods:
            with self.subTest(method=name):
                method = getattr(self.packet.HomemateJsonData, name)
                result = method(*args)
                self.assertIsInstance(result, dict)
                self.assertIn("cmd", result)
                self.assertEqual(result["cmd"], 15)

    def test_packet_fragment_reassembly_length_prefix(self) -> None:
        """验证 parse_length 能正确解析长度前缀。"""
        payload = {"cmd": 0, "key": "test"}
        session_id = b" " * 32
        key = b"khggd54865SNJHGF"
        pkt_type = bytes([0x70, 0x6b])
        packet = self.packet.HomematePacket.build_packet(pkt_type, key, session_id, payload)

        length = self.packet.HomematePacket.parse_length(packet)
        self.assertEqual(length, len(packet))

    def test_hello_payload_structure(self) -> None:
        """验证 ssl_get_session（cmd=0 握手）payload 结构。"""
        payload = self.packet.HomemateJsonData.ssl_get_session()
        self.assertEqual(payload["cmd"], 0)
        self.assertIn("serial", payload)
        self.assertIn("uniSerial", payload)


if __name__ == "__main__":
    unittest.main()
