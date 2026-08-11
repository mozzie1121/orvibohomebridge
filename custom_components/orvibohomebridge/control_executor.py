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
from .capabilities import ControlChannel, TransportMode, capability_for
from .device_types import DeviceCategory, classify_device, get_device_profile
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
        lan_adapter: Callable[[], Any] = lambda: None,
        gateway_connected: Callable[[str], bool] = lambda _uid: False,
        transport_mode: TransportMode = TransportMode.AUTO,
    ) -> None:
        self.devices = devices
        self.states = states
        self.state_store = state_store
        self._ssl_client = ssl_client
        self._route_target = route_target
        self._get_state = get_state
        self._on_updated = on_updated
        self._lan_adapter = lan_adapter
        self._gateway_connected = gateway_connected
        self._transport_mode = transport_mode
        self._last_transport: dict[str, str] = {}

    def last_transport(self, device_id: str) -> str | None:
        """Return ``lan`` or ``cloud`` for the latest successful control."""

        return self._last_transport.get(device_id)

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
        ok, _scope = await self._execute_route(
            device_id, device_uid, route
        )
        return ok

    async def _execute_route(
        self, device_id: str, device_uid: str, route: ControlRoute
    ) -> tuple[bool, str]:
        if route.scope == "ssl":
            owner, selected_scope = self._transport(device_id)
            return await self._invoke_with_fallback(
                device_id,
                owner,
                selected_scope,
                route.method,
                device_id,
                device_uid,
                *route.args,
                **route.kwargs,
            )
        owner = self._route_target()
        if owner is None:
            return False, route.scope
        method = getattr(owner, route.method)
        prefix = (
            (device_id, device_uid)
            if route.scope == "coordinator_uid"
            else (device_id,)
        )
        return bool(await method(*prefix, *route.args, **route.kwargs)), route.scope

    async def _invoke_with_fallback(
        self,
        device_id: str,
        owner: Any,
        scope: str,
        method_name: str,
        *args: Any,
        **kwargs: Any,
    ) -> tuple[bool, str]:
        """Invoke a selected transport and retry once on SSL after LAN failure."""

        if owner is None:
            return False, scope
        try:
            result = bool(
                await getattr(owner, method_name)(*args, **kwargs)
            )
        except Exception as error:  # noqa: BLE001 - LAN failure is recoverable
            if scope != "lan":
                raise
            _LOGGER.debug(
                "LAN 控制 %s 失败，回退云端: device=%s error=%s",
                method_name,
                device_id,
                type(error).__name__,
            )
            result = False
        if (
            result
            or scope != "lan"
            or self._transport_mode == TransportMode.LAN_ONLY
        ):
            if result and scope in {"lan", "ssl"}:
                self._last_transport[device_id] = (
                    "cloud" if scope == "ssl" else scope
                )
            return result, scope

        ssl = self._ssl_client()
        if ssl is None:
            return False, scope
        result = bool(await getattr(ssl, method_name)(*args, **kwargs))
        if result:
            self._last_transport[device_id] = "cloud"
        return result, "ssl"

    async def _send_selected(
        self,
        device_id: str,
        method_name: str,
        *args: Any,
        **kwargs: Any,
    ) -> tuple[bool, str]:
        owner, scope = self._transport(device_id)
        return await self._invoke_with_fallback(
            device_id, owner, scope, method_name, *args, **kwargs
        )

    def _transport(self, device_id: str) -> tuple[Any, str]:
        """按能力表 + 网关可达性选择控制通道（LAN 优先 / 云兜底）。"""
        device = self.devices.get(device_id) or {}
        device_uid = device.get("uid", "")
        if self._transport_mode != TransportMode.CLOUD_ONLY:
            lan = self._lan_adapter()
            if lan is not None and self._gateway_connected(device_uid):
                try:
                    capability = capability_for(device)
                except Exception:  # noqa: BLE001
                    capability = None
                if (
                    capability is not None
                    and ControlChannel.LAN in capability.channels
                ):
                    return lan, "lan"
            if self._transport_mode == TransportMode.LAN_ONLY:
                return None, "lan"
        return self._ssl_client(), "ssl"

    async def _wait_if_ssl(self, device_id: str, scope: str) -> dict | None:
        """Wait only for controls that directly used the SSL client."""
        if scope != "ssl":
            return None
        return await self.wait_for_response(device_id)

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
        result, scope = await self._execute_route(
            device_id, device.get("uid", ""), route
        )
        if result:
            response = await self._wait_if_ssl(device_id, scope)
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
        result, scope = await self._execute_route(
            device_id, device.get("uid", ""), route
        )
        if result:
            if not await self._wait_if_ssl(device_id, scope):
                self.apply_optimistic(device_id, {"state": False})
            self._on_updated()
        return result

    async def set_cover_position(self, device_id: str, position: int) -> bool:
        device = self._controllable_device(device_id, "窗帘")
        if device is None:
            return False
        client, selected_scope = self._transport(device_id)
        if client is None:
            return False
        category = classify_device(device)
        if category == DeviceCategory.DREAM_CURTAIN:
            current = self.states.get(device_id, {})
            if current.get("angle") != 90:
                if current.get("position") != 0:
                    _LOGGER.warning("梦幻帘叶片未居中，拒绝直接设置开合位置: %s", device_id)
                    return False
                centered, selected_scope = await self._invoke_with_fallback(
                    device_id,
                    client,
                    selected_scope,
                    "send_control_dream_curtain_angle",
                    device_id,
                    device.get("uid", ""),
                    90,
                )
                if not centered:
                    return False
                response = await self._wait_if_ssl(device_id, selected_scope)
                reported_angle = (
                    response.get("properties", {}).get("curtain", {}).get("angle")
                    if isinstance(response, dict)
                    else None
                )
                if reported_angle != 90:
                    _LOGGER.warning("梦幻帘叶片居中未确认，取消位置控制: %s", device_id)
                    return False
                if selected_scope == "ssl":
                    client = self._ssl_client()
                    if client is None:
                        return False
            result, selected_scope = await self._invoke_with_fallback(
                device_id,
                client,
                selected_scope,
                "send_control_dream_curtain_percent",
                device_id,
                device.get("uid", ""),
                position,
            )
        else:
            result, selected_scope = await self._invoke_with_fallback(
                device_id,
                client,
                selected_scope,
                "send_control_cover",
                device_id,
                device.get("uid", ""),
                position,
            )
        if result:
            response = await self._wait_if_ssl(device_id, selected_scope)
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
        category = classify_device(device)
        client, scope = self._transport(device_id)
        if client is None:
            return False
        if category == DeviceCategory.DREAM_CURTAIN:
            result, _scope = await self._invoke_with_fallback(
                device_id,
                client,
                scope,
                "send_control_dream_curtain_action",
                device_id,
                device.get("uid", ""),
                "pause",
            )
            return result
        stop_value2 = 255 if category == DeviceCategory.ZIGBEE_ROLLING_SHUTTER else 0
        result, _scope = await self._invoke_with_fallback(
            device_id,
            client,
            scope,
            "send_control_cover",
            device_id,
            device.get("uid", ""),
            "stop",
            stop_value2=stop_value2,
        )
        return result

    async def set_dream_curtain_angle(self, device_id: str, angle: int) -> bool:
        device = self._controllable_device(device_id, "梦幻帘角度")
        if device is None or classify_device(device) != DeviceCategory.DREAM_CURTAIN:
            return False
        if self.states.get(device_id, {}).get("position") != 0:
            _LOGGER.warning("梦幻帘仅在完全关闭时允许调节角度: %s", device_id)
            return False
        result, _scope = await self._send_selected(
            device_id,
            "send_control_dream_curtain_angle",
            device_id,
            device.get("uid", ""),
            angle,
        )
        if result:
            self.apply_optimistic(device_id, {"angle": max(0, min(180, int(angle)))})
            self._on_updated()
        return result

    async def dream_curtain_action(self, device_id: str, action: str) -> bool:
        device = self._controllable_device(device_id, "梦幻帘动作")
        if device is None or classify_device(device) != DeviceCategory.DREAM_CURTAIN:
            return False
        result, _scope = await self._send_selected(
            device_id,
            "send_control_dream_curtain_action",
            device_id,
            device.get("uid", ""),
            action,
        )
        if result:
            self.apply_optimistic(device_id, {"cover_action": action})
            self._on_updated()
        return result

    async def set_floor_heating_temperature(self, device_id: str, temperature: int) -> bool:
        device = self._controllable_device(device_id, "地暖温度")
        if device is None or classify_device(device) not in {
            DeviceCategory.FLOOR_HEATING,
            DeviceCategory.LEGACY_FLOOR_HEATING,
        }:
            return False
        category = classify_device(device)
        low_default = 10 if category == DeviceCategory.LEGACY_FLOOR_HEATING else 8
        low = int(self.states.get(device_id, {}).get("min_temperature") or low_default)
        high = int(self.states.get(device_id, {}).get("max_temperature") or 35)
        target = max(low, min(high, int(round(temperature))))
        if category == DeviceCategory.LEGACY_FLOOR_HEATING:
            result, _scope = await self._send_selected(
                device_id,
                "send_control_legacy_floor_heating_temperature",
                device_id,
                device.get("uid", ""),
                target,
            )
        else:
            result, _scope = await self._send_selected(
                device_id,
                "send_control_floor_heating_temperature",
                device_id,
                device.get("uid", ""),
                target,
            )
        if result:
            self.apply_optimistic(device_id, {"target_temperature": target})
            self._on_updated()
        return result

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
        device = self.devices.get(device_id)
        if device is None:
            _LOGGER.error("找不到设备 %s", device_id)
            return False
        result, _scope = await self._send_selected(
            device_id,
            "send_light_bri_ct",
            device_id,
            device.get("uid", ""),
            brightness,
            color_temp_k,
        )
        return result

    async def ventilation_state_update(self, device_id: str, value1: int) -> bool:
        device = self._connected_device(device_id)
        if device is None:
            return False
        result, scope = await self._send_selected(
            device_id,
            "send_control_ventilation",
            device_id,
            device.get("uid", ""),
            value1,
        )
        if result:
            if not await self._wait_if_ssl(device_id, scope):
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

        client = self._ssl_client()
        if client is None:
            return False
        result = await client.send_clothes_horse_control(
            device_id=device_id,
            device_uid=device.get("uid", ""),
            ctrl_field=control_field,
            ctrl_value=value,
        )
        if result:
            self._last_transport[device_id] = "cloud"
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
        result, scope = await self._execute_route(
            device_id, device.get("uid", ""), route
        )
        if result:
            if not await self._wait_if_ssl(device_id, scope):
                self.apply_optimistic(device_id, route.optimistic)
            self._on_updated()
        return result

    def _connected_device(self, device_id: str) -> dict[str, Any] | None:
        if self._ssl_client() is None and self._lan_adapter() is None:
            _LOGGER.error("控制通道未初始化")
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
