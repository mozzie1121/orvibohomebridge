"""Tests for the protocol-agnostic SSL stream transport."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import importlib
from pathlib import Path
import sys
import types
import unittest

COMPONENT_PATH = Path(__file__).parents[1] / "custom_components" / "orvibohomebridge"


def _load_transport_module():
    package_name = "orvibohomebridge_transport_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT_PATH)]
    sys.modules[package_name] = package
    return importlib.import_module(f"{package_name}.ssl_transport")


class _Writer:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closing = False
        self.drained = False
        self.closed = False

    def is_closing(self) -> bool:
        return self.closing

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        self.drained = True

    def close(self) -> None:
        self.closing = True

    async def wait_closed(self) -> None:
        self.closed = True


class _Reader:
    async def readexactly(self, size: int) -> bytes:
        return b"x" * size


class SSLTransportTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load_transport_module()

    def _transport(self):
        files = self.module.TlsFiles(Path("cert"), Path("key"), Path("ca"))
        return self.module.SSLTransport(object(), "host", 10002, files)

    async def test_stream_read_write_and_close(self) -> None:
        transport = self._transport()
        writer = _Writer()
        transport.writer = writer
        transport.reader = _Reader()
        transport.connected = True

        await transport.write(b"packet")
        self.assertEqual(writer.data, b"packet")
        self.assertTrue(writer.drained)
        self.assertEqual(await transport.readexactly(3), b"xxx")

        await transport.close()
        self.assertTrue(writer.closed)
        self.assertFalse(transport.connected)
        self.assertIsNone(transport.reader)
        self.assertIsNone(transport.writer)

    async def test_disconnected_stream_operations_fail(self) -> None:
        transport = self._transport()
        with self.assertRaises(ConnectionError):
            await transport.write(b"packet")
        with self.assertRaises(ConnectionError):
            await transport.readexactly(1)

    def test_tls_paths_are_immutable(self) -> None:
        files = self.module.TlsFiles(Path("cert"), Path("key"), Path("ca"))
        with self.assertRaises(FrozenInstanceError):
            files.private_key = Path("other")


if __name__ == "__main__":
    unittest.main()
