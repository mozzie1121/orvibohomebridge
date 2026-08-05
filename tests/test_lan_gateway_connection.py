"""Tests for the ported LAN gateway connection (Stage 1)."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
import sys
import types
from typing import Any, cast
import unittest

COMPONENT_PATH = Path(__file__).parents[1] / "custom_components" / "orvibohomebridge"

SESSION = b"s" * 32
SESSION_KEY = b"0123456789abcdef"


class FakeWriter:
    def __init__(self, reader: asyncio.StreamReader) -> None:
        self.reader = reader
        self.writes: list[bytes] = []
        self.closed = False
        self.drain_gate: asyncio.Event | None = None

    def write(self, packet: bytes) -> None:
        self.writes.append(packet)

    async def drain(self) -> None:
        if self.drain_gate is not None:
            await self.drain_gate.wait()

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    def respond(
        self,
        payload: dict[str, Any],
        *,
        packet_type: bytes | None = None,
        key: bytes | None = None,
        session_id: bytes = SESSION,
    ) -> None:
        self.reader.feed_data(
            self.test.protocol.build_packet(
                packet_type or self.test.protocol.DK_TYPE,
                key or SESSION_KEY,
                session_id,
                payload,
            )
        )


class LanGatewayConnectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        package_name = "orvibohomebridge_langw_test"
        package = types.ModuleType(package_name)
        package.__path__ = [str(COMPONENT_PATH)]
        sys.modules[package_name] = package
        cls.protocol = importlib.import_module(f"{package_name}.lan.protocol")
        cls.gw = importlib.import_module(f"{package_name}.lan.gateway_connection")
        FakeWriter.test = cls

    def _activate(
        self, connection: Any
    ) -> tuple[asyncio.StreamReader, FakeWriter]:
        reader = asyncio.StreamReader()
        writer = FakeWriter(reader)
        connection.reader = reader
        connection.writer = cast(asyncio.StreamWriter, writer)
        connection.session_id = SESSION
        connection.session_key = SESSION_KEY
        connection._keys[SESSION] = SESSION_KEY
        connection._closed = False
        connection._ready = True
        connection.generation = 1
        connection._reader_task = asyncio.create_task(
            connection._reader_loop(1, reader, cast(asyncio.StreamWriter, writer))
        )
        return reader, writer

    def test_login_rejection_raises_gateway_login_rejected_error(self) -> None:
        async def scenario() -> None:
            async def open_connection(
                host: str, port: int
            ) -> tuple[asyncio.StreamReader, FakeWriter]:
                del host, port
                reader = asyncio.StreamReader()
                writer = FakeWriter(reader)

                async def server() -> None:
                    while len(writer.writes) < 1:
                        await asyncio.sleep(0)
                    hello = self.protocol.parse_packet(writer.writes[0], {})
                    writer.respond(
                        {
                            "cmd": hello["cmd"],
                            "serial": hello["serial"],
                            "uid": "expected",
                            "sessionKey": SESSION_KEY.hex(),
                        },
                        packet_type=self.protocol.PK_TYPE,
                        key=self.protocol.DEFAULT_KEY,
                    )
                    while len(writer.writes) < 2:
                        await asyncio.sleep(0)
                    login = self.protocol.parse_packet(
                        writer.writes[1], {SESSION: SESSION_KEY}
                    )
                    writer.respond(
                        {"cmd": login["cmd"], "serial": login["serial"], "status": 12}
                    )

                asyncio.create_task(server())
                return reader, writer

            connection = self.gw.GatewayConnection(
                "192.168.1.2", open_connection=open_connection
            )
            with self.assertRaises(self.gw.GatewayLoginRejectedError):
                await connection.connect(
                    "user", "password", expected_uid="expected"
                )
            await connection.close()

        asyncio.run(scenario())

    def test_unsolicited_state_update_routes_to_push_callback(self) -> None:
        async def scenario() -> list[dict[str, Any]]:
            pushes: list[dict[str, Any]] = []
            connection = self.gw.GatewayConnection(
                "192.168.1.2", push_callback=pushes.append
            )
            _, writer = self._activate(connection)
            writer.respond({"cmd": 42, "deviceId": "door", "value1": 1})
            for _ in range(100):
                if pushes:
                    break
                await asyncio.sleep(0)
            await connection.close()
            return pushes

        pushes = asyncio.run(scenario())
        self.assertEqual(len(pushes), 1)
        self.assertEqual(pushes[0]["deviceId"], "door")

    def test_string_command_update_still_routes_to_push(self) -> None:
        async def scenario() -> list[dict[str, Any]]:
            pushes: list[dict[str, Any]] = []
            connection = self.gw.GatewayConnection(
                "192.168.1.2", push_callback=pushes.append
            )
            _, writer = self._activate(connection)
            writer.respond({"cmd": "42", "deviceId": "door", "value1": 1})
            for _ in range(100):
                if pushes:
                    break
                await asyncio.sleep(0)
            await connection.close()
            return pushes

        pushes = asyncio.run(scenario())
        self.assertEqual(len(pushes), 1)

    def test_correlated_response_not_mistaken_for_push(self) -> None:
        async def scenario() -> tuple[dict[str, Any], list[dict[str, Any]]]:
            pushes: list[dict[str, Any]] = []
            connection = self.gw.GatewayConnection(
                "192.168.1.2", push_callback=pushes.append
            )
            _, writer = self._activate(connection)
            request = asyncio.create_task(connection.send({"cmd": 15, "serial": 101}))
            while len(writer.writes) < 1:
                await asyncio.sleep(0)
            writer.respond(
                {"cmd": 15, "serial": 101, "deviceId": "door", "status": 0}
            )
            response = await request
            await connection.close()
            return response, pushes

        response, pushes = asyncio.run(scenario())
        self.assertEqual(response["status"], 0)
        self.assertEqual(pushes, [])


if __name__ == "__main__":
    unittest.main()
