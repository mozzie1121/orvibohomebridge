"""Round 4 (P1): SSL 心跳请求-响应式假死检测测试。"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
import sys
import types
import unittest

COMPONENT_PATH = Path(__file__).parents[1] / "custom_components" / "orvibohomebridge"


def _module(name: str, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _load_ssl_client():
    package_name = "orvibohomebridge_ssl_hb_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT_PATH)]
    sys.modules[package_name] = package

    _module("homeassistant")
    _module("homeassistant.core", HomeAssistant=object)

    for name in (
        "const",
        "functions",
        "packet",
        "protocol",
        "ssl_transport",
        "pending_requests",
    ):
        spec = importlib.util.spec_from_file_location(
            f"{package_name}.{name}", COMPONENT_PATH / f"{name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"{package_name}.{name}"] = module
        spec.loader.exec_module(module)

    return importlib.import_module(f"{package_name}.ssl_client")


class FakeTransport:
    """模拟 TCP 半开：写成功但永远没有回包。"""

    def __init__(self):
        self.connected = True
        self.closed = False

    async def close(self):
        self.closed = True
        self.connected = False


class _BackgroundTask:
    def cancel(self):
        return None

    def done(self):
        return True


class _FakeHass:
    def __init__(self):
        self.tasks = []

    def async_create_background_task(self, coro, *, name):
        del name
        coro.close()
        return _BackgroundTask()

    def async_create_task(self, coro):
        coro.close()
        return _BackgroundTask()


class HeartbeatDetectionTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_ssl_client()

    def make_client(self, transport):
        client = self.mod.SSLClient(
            object(),
            "cloud.example",
            10002,
            "user",
            "A" * 32,
            "family-1",
            lambda _sid: None,
            lambda _did, _raw: None,
            heartbeat_interval=0.05,
            retry_interval=0.01,
        )
        client.transport = transport
        client.session_key = b"k" * 16  # 非 DEFAULT_KEY，才会发心跳
        client.heartbeat_response_timeout = 0.05
        client.HEARTBEAT_MAX_FAILURES = 2
        return client

    async def test_heartbeat_without_reply_detects_dead_connection(self) -> None:
        """半开连接：心跳发出但无回包 → 连续失败后关闭连接（触发重连）。"""
        transport = FakeTransport()
        client = self.make_client(transport)

        async def fake_send(*_args, **_kwargs):
            return True  # 写"成功"（半开连接的典型表现）

        client._send_packet = fake_send
        await client._heartbeat_loop()

        self.assertFalse(client.connected)
        self.assertTrue(transport.closed)

    async def test_heartbeat_reply_keeps_connection(self) -> None:
        """正常连接：回包持续到达（监听循环 resolve _heartbeat_pending）→ 保持在线。"""
        transport = FakeTransport()
        client = self.make_client(transport)

        async def fake_send(*_args, **_kwargs):
            return True

        client._send_packet = fake_send

        async def resolver():
            # 模拟监听循环：收到 cmd=32 回包后 resolve 在途心跳
            while True:
                await asyncio.sleep(0.005)
                pending = client._heartbeat_pending
                if pending is not None and not pending.done():
                    pending.set_result(True)

        loop_task = asyncio.get_running_loop().create_task(client._heartbeat_loop())
        resolver_task = asyncio.get_running_loop().create_task(resolver())
        try:
            await asyncio.sleep(0.4)  # 跑多个心跳周期
            self.assertTrue(client.connected)
            self.assertFalse(transport.closed)
            self.assertFalse(loop_task.done())  # 循环仍在运行（未被判死）
        finally:
            loop_task.cancel()
            resolver_task.cancel()


    async def test_reconnect_after_first_login_triggers_resync(self) -> None:
        """Round 5: 首次登录不触发重同步；之后每次登录成功触发 on_reconnected。"""
        from unittest.mock import AsyncMock

        transport = FakeTransport()
        transport.connected = False
        calls = []
        client = self.mod.SSLClient(
            _FakeHass(),
            "cloud.example",
            10002,
            "user",
            "A" * 32,
            "family-1",
            lambda _sid: None,
            lambda _did, _raw: None,
            on_reconnected=lambda: calls.append("resync"),
        )
        client.transport = transport
        client.session_key = b"k" * 16

        async def fake_connect():
            client.transport.connected = True
            return True

        client._connect = fake_connect
        client._send_hello = AsyncMock()
        client._send_login = AsyncMock(return_value=True)

        await client.connect_and_login(hello_wait=0.01)
        self.assertEqual(calls, [])  # 首次登录：不触发

        # 模拟断线后再重连
        client.transport.connected = False
        await client.connect_and_login(hello_wait=0.01)
        self.assertEqual(calls, ["resync"])  # 重连：触发全量重同步


if __name__ == "__main__":
    unittest.main()
