"""Tests for shared control execution and optimistic fallback behavior."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
import sys
import types
import unittest

COMPONENT_PATH = Path(__file__).parents[1] / "custom_components" / "orvibohomebridge"


def _load_module():
    package_name = "orvibohomebridge_control_executor_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT_PATH)]
    sys.modules[package_name] = package
    return importlib.import_module(f"{package_name}.control_executor")


class FakeSsl:
    def __init__(self, response=None):
        self.response = response
        self.calls = []

    async def send_control_light(self, *args, **kwargs):
        self.calls.append(("light", args, kwargs))
        return True

    async def send_control_cover(self, *args, **kwargs):
        self.calls.append(("cover", args, kwargs))
        return True

    async def send_clothes_horse_control(self, **kwargs):
        self.calls.append(("rack", (), kwargs))
        return True

    async def send_control_ventilation(self, *args, **kwargs):
        self.calls.append(("ventilation", args, kwargs))
        return True

    async def send_control_legacy_floor_heating_temperature(self, *args, **kwargs):
        self.calls.append(("legacy_floor_temperature", args, kwargs))
        return True

    async def _wait_for_control_response(self, device_id):
        return self.response


class ControlExecutorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load_module()

    def make_executor(self, device, state=None, response=None, target=None):
        devices = {"device": device}
        states = {"device": state or {}}
        updates = []
        ssl = FakeSsl(response)
        store = self.module.StateStore(states)
        # 这些用例验证 executor 逻辑（乐观回写/回执确认），走 SSL 路径：
        # 严格语义下非 cloud_only 设备走本地，故用仅云模式驱动 SSL 分支
        executor = self.module.ControlExecutor(
            devices,
            states,
            store,
            lambda: ssl,
            lambda: target or object(),
            states.get,
            lambda: updates.append(dict(states["device"])),
            transport_mode=self.module.TransportMode.CLOUD_ONLY,
        )
        return executor, ssl, states, updates

    def test_turn_on_applies_optimistic_state_after_timeout(self) -> None:
        executor, ssl, states, updates = self.make_executor(
            {"device_type_raw": 1, "uid": "uid"}
        )

        result = asyncio.run(executor.turn_on("device"))

        self.assertTrue(result)
        self.assertEqual(ssl.calls[0][0], "light")
        self.assertTrue(states["device"]["state"])
        self.assertTrue(updates)

    def test_cover_uses_confirmed_position_when_response_arrives(self) -> None:
        executor, _, states, _ = self.make_executor(
            {"device_type_raw": 34, "uid": "uid"},
            response={"value1": 37},
        )

        result = asyncio.run(executor.set_cover_position("device", 80))

        self.assertTrue(result)
        self.assertEqual(states["device"]["position"], 37)
        self.assertTrue(states["device"]["state"])

    def test_unknown_device_is_registration_only(self) -> None:
        executor, ssl, _, _ = self.make_executor(
            {"device_type_raw": 999, "uid": "uid"}
        )

        result = asyncio.run(executor.turn_on("device"))

        self.assertFalse(result)
        self.assertEqual(ssl.calls, [])

    def test_known_unsupported_device_cannot_use_light_fallback(self) -> None:
        executor, ssl, _, _ = self.make_executor({
            "device_type_raw": 37,
            "sub_device_type": -2,
            "model": "82c167c95ed746cdbd21d6817f72c593",
            "uid": "uid",
        })

        result = asyncio.run(executor.turn_on("device"))

        self.assertFalse(result)
        self.assertEqual(ssl.calls, [])

    def test_legacy_floor_temperature_uses_offset_protocol(self) -> None:
        executor, ssl, states, _ = self.make_executor(
            {
                "device_type_raw": 112,
                "sub_device_type": -2,
                "model": "2ac836760da10748856a7e4eafb91efa",
                "uid": "uid",
            },
            {"min_temperature": 10, "max_temperature": 35},
        )

        result = asyncio.run(
            executor.set_floor_heating_temperature("device", 25)
        )

        self.assertTrue(result)
        self.assertEqual(
            ssl.calls[0],
            ("legacy_floor_temperature", ("device", "uid", 25), {}),
        )
        self.assertEqual(states["device"]["target_temperature"], 25)

    def test_coordinator_uid_route_preserves_device_prefix(self) -> None:
        class Target:
            def __init__(self):
                self.calls = []

            async def raw(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return True

        target = Target()
        executor, _, _, _ = self.make_executor(
            {"device_type_raw": 36, "uid": "uid"}, target=target
        )
        route = self.module.ControlRoute(
            "coordinator_uid", "raw", (), {"order": "on"}
        )

        result = asyncio.run(executor.execute_route("device", "uid", route))

        self.assertTrue(result)
        self.assertEqual(target.calls, [(('device', 'uid'), {"order": "on"})])

    def test_ventilation_timeout_updates_normalized_state(self) -> None:
        executor, _, states, updates = self.make_executor(
            {"device_type_raw": 516, "uid": "uid"}
        )

        result = asyncio.run(executor.ventilation_state_update("device", 100))

        self.assertTrue(result)
        self.assertEqual(states["device"]["fan_speed"], "快")
        self.assertTrue(states["device"]["state"])
        self.assertEqual(states["device"]["value1"], 100)
        self.assertTrue(updates)

    def test_clothes_horse_rejects_sterilizing_away_from_top(self) -> None:
        executor, ssl, _, _ = self.make_executor(
            {"device_type_raw": 52, "uid": "uid"}, {"position": 30}
        )

        result = asyncio.run(
            executor.clothes_horse_control("device", "sterilizing", "on")
        )

        self.assertFalse(result)
        self.assertEqual(ssl.calls, [])

    def test_clothes_horse_main_switch_updates_both_fields(self) -> None:
        executor, ssl, states, updates = self.make_executor(
            {"device_type_raw": 52, "uid": "uid"}, {"position": 0}
        )

        result = asyncio.run(
            executor.clothes_horse_control("device", "main_switch", "on")
        )

        self.assertTrue(result)
        self.assertEqual(ssl.calls[0][2]["ctrl_field"], "mainSwitchCtrl")
        self.assertTrue(states["device"]["main_switch_state"])
        self.assertTrue(states["device"]["state"])
        self.assertTrue(updates)


if __name__ == "__main__":
    unittest.main()
