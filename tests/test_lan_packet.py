"""Tests for the ported LAN packet codec (Stage 1)."""

from __future__ import annotations

import importlib
from pathlib import Path
import sys
import types
import unittest

COMPONENT_PATH = Path(__file__).parents[1] / "custom_components" / "orvibohomebridge"


def _load_module(name: str):
    package_name = f"orvibohomebridge_{name}_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT_PATH)]
    sys.modules[package_name] = package
    return importlib.import_module(f"{package_name}.lan.{name}")


class LanPacketCodecTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.protocol = _load_module("protocol")
        except ModuleNotFoundError as err:
            if err.name == "cryptography":
                raise unittest.SkipTest("cryptography is not installed") from err
            raise

    def test_pk_roundtrip(self) -> None:
        payload = {"cmd": 42, "deviceId": "w-test", "properties": {"onoff": {}}}
        id_unset = b"\x20" * 32
        packet = self.protocol.build_packet(
            self.protocol.PK_TYPE,
            self.protocol.DEFAULT_KEY,
            id_unset,
            payload,
        )
        decoded = self.protocol.parse_packet(
            packet, {id_unset: self.protocol.DEFAULT_KEY}
        )
        self.assertEqual(decoded["cmd"], 42)
        self.assertEqual(decoded["deviceId"], "w-test")
        self.assertEqual(decoded["properties"], {"onoff": {}})

    def test_dk_roundtrip_with_session_key(self) -> None:
        session = b"s" * 32
        key = b"0123456789abcdef"
        packet = self.protocol.build_packet(
            self.protocol.DK_TYPE, key, session, {"cmd": 15}
        )
        decoded = self.protocol.parse_packet(packet, {session: key})
        self.assertEqual(decoded["cmd"], 15)

    def test_invalid_magic_rejected(self) -> None:
        id_unset = b"\x20" * 32
        packet = self.protocol.build_packet(
            self.protocol.PK_TYPE,
            self.protocol.DEFAULT_KEY,
            id_unset,
            {"cmd": 1},
        )
        bad = b"xx" + packet[2:]
        with self.assertRaises(self.protocol.InvalidMagicError):
            self.protocol.parse_packet(bad, {id_unset: self.protocol.DEFAULT_KEY})

    def test_crc_mismatch_rejected(self) -> None:
        id_unset = b"\x20" * 32
        packet = bytearray(
            self.protocol.build_packet(
                self.protocol.PK_TYPE,
                self.protocol.DEFAULT_KEY,
                id_unset,
                {"cmd": 1},
            )
        )
        packet[6] ^= 0xFF  # 破坏 CRC 字节
        with self.assertRaises(self.protocol.InvalidCrcError):
            self.protocol.parse_packet(
                bytes(packet),
                {id_unset: self.protocol.DEFAULT_KEY},
            )

    def test_unknown_dk_session_rejected(self) -> None:
        session = b"s" * 32
        packet = self.protocol.build_packet(
            self.protocol.DK_TYPE, b"0123456789abcdef", session, {"cmd": 1}
        )
        with self.assertRaises(self.protocol.InvalidSessionError):
            self.protocol.parse_packet(packet, {})

    def test_packet_too_short_rejected(self) -> None:
        id_unset = b"\x20" * 32
        with self.assertRaises(self.protocol.InvalidLengthError):
            self.protocol.parse_packet(
                b"hd\x00\x2a", {id_unset: self.protocol.DEFAULT_KEY}
            )


if __name__ == "__main__":
    unittest.main()
