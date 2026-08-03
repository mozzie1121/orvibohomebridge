"""Shared control execution, response waiting, and optimistic state handling."""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping, MutableMapping, Optional

from .control_router import (
    ControlRoute,
    brightness_route,
    color_temp_route,
    power_route,
)
from .device_types import classify_device, get_device_profile
from .state_store import StateSource, StateStore


_LOGGER = logging.getLogger(__name__)


class ControlExecutor:
    """Execute routed controls while owning common confirmation behavior."""

    CLOTHES_HORSE_FIELD_MAP = {
        "lighting": "lightingCtrl",
        "sterilizing": "sterilizingCtrl",
        "wind_drying": "windDryingCtrl",
        "heat_drying": "heatDryingCtrl",
        "main_switch": "mainSwitchCtrl",
        "motor": "motorCtrl",
    }

    def __init__(
        self,
        devices: MutableMapping[str, dict[str, Any]],
        states: MutableMapping[str, dict[str, Any]],
        state_store: StateStore,
        ssl_client: Callable[[], Any],
        route_target: Callable[[], Any],
        get_state: Callable[[str], Optional[dict[str, Any]]],
        on_updated: Callable[[], None],
    ) -> None:
        self.devices = devices
        self.states = states
        self.state_store = state_store
        self._ssl_client = ssl_client
        self._route_target = route_target
        self._get_state = get_state
        self._on_updated = on_updated

    async def wait_for_response(self, device_id: str) -> dict | None:
        client = self._ssl_client()
        if client is None:
            return None
        return await client._wait_for_control_response(device_id)

    def apply_optimistic(
        self, device_id: str, values: Mapping[str, Any]
    ) -> None:
        self.state_store.merge(
            device_id,
            values,
            StateSource.OPTIMISTIC,
            force=True,
        )

    async def execute_route(
        self, device_id: str, device_uid: str, route: ControlRoute
    ) -> bool:
        owner = self._ssl_client() if route.scope == "ssl" else self._route_target()
        if owner is None:
            return False
        method = getattr(owner, route.method)
        prefix = (
            (device_id, device_uid)
            if route.scope in ("ssl", "coordinator_uid")
            else (device_id,)
        )
        return await method(*prefix, *route.args, **route.kwargs)

    async def turn_on(
        self,
        device_id: str,
        brightness: int | None = None,
        color_temp: int | None = None,
    ) -> bool:
        device = self._controllable_device(device_id)
        if device is None:
            return False
        category = classify_device(device)
        route = power_route(
            category,
            True,
            self._get_state(device_id) or {},
            brightness=brightness,
            color_temp=color_temp,
        )
        result = await self.execute_route(
            device_id, device.get("uid", ""), route
        )
        if result:
            response = await self.wait_for_response(device_id)
            if not response:
                optimistic = {"state": True}
                if brightness is not None:
                    optimistic["brightness"] = brightness
                if color_temp is not None:
                    optimistic["color_temp"] = color_temp
                self.apply_optimistic(device_id, optimistic)
            self._on_updated()
        return result

    async def turn_off(self, device_id: str) -> bool:
        device = self._controllable_device(device_id)
        if device is None:
            return False
        route = power_route(
            classify_device(device),
            False,
            self._get_state(device_id) or {},
        )
        result = await self.execute_route(
            device_id, device.get("uid", ""), route
        )
        if result:
            if not await self.wait_for_response(device_id):
                self.apply_optimistic(device_id, {"state": False})
            self._on_updated()
        return result

    async def set_cover_position(self, device_id: str, position: int) -> bool:
        device = self._controllable_device(device_id, "窗帘")
        if device is None:
            return False
        client = self._ssl_client()
        result = await client.send_control_cover(
            device_id, device.get("uid", ""), position
        )
        if result:
            response = await self.wait_for_response(device_id)
            if response:
                actual = response.get("value1")
                if isinstance(actual, (int, float)) and 0 <= actual <= 100:
                    state = self.states.setdefault(device_id, {})
                    state["position"] = actual
                    state["state"] = actual > 0
            else:
                self.apply_optimistic(
                    device_id, {"position": position, "state": position > 0}
                )
            self._on_updated()
        return result

    async def stop_cover(self, device_id: str) -> bool:
        device = self._controllable_device(device_id, "窗帘")
        if device is None:
            return False
        return await self._ssl_client().send_control_cover(
            device_id, device.get("uid", ""), "stop"
        )

    async def set_brightness(self, device_id: str, brightness: int) -> bool:
        device = self._controllable_device(device_id, "亮度")
        if device is None:
            return False
        route = brightness_route(
            classify_device(device),
            brightness,
            self.states.get(device_id, {}),
            device_type_raw=device.get("device_type_raw"),
        )
        return await self._execute_confirmed_route(device_id, device, route)

    async def set_color_temp(self, device_id: str, color_temp_k: int) -> bool:
        device = self._controllable_device(device_id, "色温")
        if device is None:
            return False
        route = color_temp_route(
            classify_device(device),
            color_temp_k,
            self.states.get(device_id, {}),
            device_type_raw=device.get("device_type_raw"),
        )
        return await self._execute_confirmed_route(device_id, device, route)

    async def set_light_param(
        self,
        device_id: str,
        brightness: int | None,
        color_temp_k: int | None,
    ) -> bool:
        client = self._ssl_client()
        if client is None:
            _LOGGER.error("SSL未连接，无法下发灯光复合参数")
            return False
        device = self.devices.get(device_id)
        if device is None:
            _LOGGER.error("找不到设备 %s", device_id)
            return False
        return await client.send_light_bri_ct(
            device_id,
            device.get("uid", ""),
            brightness,
            color_temp_k,
        )

    async def ventilation_state_update(self, device_id: str, value1: int) -> bool:
        device = self._connected_device(device_id)
        if device is None:
            return False
        result = await self._ssl_client().send_control_ventilation(
            device_id, device.get("uid", ""), value1
        )
        if result:
            if not await self.wait_for_response(device_id):
                state = self.states.setdefault(device_id, {})
                if value1 == 0:
                    state.update({"fan_speed": "慢", "state": True})
                elif value1 == 50:
                    state.update({"fan_speed": "停", "state": False})
                elif value1 == 100:
                    state.update({"fan_speed": "快", "state": True})
                state["value1"] = value1
                self.state_store.mark(
                    device_id,
                    ("fan_speed", "state", "value1"),
                    StateSource.OPTIMISTIC,
                )
            self._on_updated()
        return result

    async def set_ventilation_preset_mode(
        self, device_id: str, preset_mode: str
    ) -> bool:
        value1 = {"停": 50, "慢": 0, "快": 100}.get(preset_mode)
        if value1 is None:
            _LOGGER.error("无效的新风模式: %s", preset_mode)
            return False
        return await self.ventilation_state_update(device_id, value1)

    async def clothes_horse_control(
        self, device_id: str, feature: str, value: str
    ) -> bool:
        device = self._connected_device(device_id)
        if device is None:
            return False
        control_field = self.CLOTHES_HORSE_FIELD_MAP.get(feature)
        if control_field is None:
            _LOGGER.error("未知晾衣架功能: %s", feature)
            return False
        if (
            feature == "sterilizing"
            and value == "on"
            and self.states.get(device_id, {}).get("position", 0) != 0
        ):
            _LOGGER.warning("[晾衣架] 拒绝消毒开启命令: 电机未在顶部")
            return False

        result = await self._ssl_client().send_clothes_horse_control(
            device_id=device_id,
            device_uid=device.get("uid", ""),
            ctrl_field=control_field,
            ctrl_value=value,
        )
        if result:
            state = self.states.get(device_id)
            if state is not None:
                if feature == "motor":
                    state["motor_state"] = value
                    optimistic_fields = ("motor_state",)
                else:
                    state_key = f"{feature}_state"
                    state[state_key] = value == "on"
                    optimistic_fields = (state_key,)
                    if feature == "main_switch":
                        state["state"] = value == "on"
                        optimistic_fields += ("state",)
                self.state_store.mark(
                    device_id, optimistic_fields, StateSource.OPTIMISTIC
                )
                self._on_updated()
            _LOGGER.debug(
                "[控制成功] %s %s=%s", device_id, control_field, value
            )
        return result

    async def _execute_confirmed_route(
        self,
        device_id: str,
        device: dict[str, Any],
        route: ControlRoute,
    ) -> bool:
        result = await self.execute_route(
            device_id, device.get("uid", ""), route
        )
        if result:
            if not await self.wait_for_response(device_id):
                self.apply_optimistic(device_id, route.optimistic)
            self._on_updated()
        return result

    def _connected_device(self, device_id: str) -> dict[str, Any] | None:
        if self._ssl_client() is None:
            _LOGGER.error("SSL客户端未初始化")
            return None
        device = self.devices.get(device_id)
        if device is None:
            _LOGGER.error("设备不存在: %s", device_id)
        return device

    def _controllable_device(
        self, device_id: str, control_name: str = ""
    ) -> dict[str, Any] | None:
        device = self._connected_device(device_id)
        if device is None:
            return None
        if get_device_profile(device).registration_only:
            suffix = f"{control_name}控制" if control_name else "控制"
            _LOGGER.warning("未知设备仅注册展示，拒绝下发%s: %s", suffix, device_id)
            return None
        return device
