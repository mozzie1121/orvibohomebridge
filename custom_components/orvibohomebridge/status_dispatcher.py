"""Inbound SSL status matching, parsing, and event dispatch."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, MutableMapping, Optional

from .device_types import DeviceCategory, classify_device
from .parsers import get_state_parser
from .state_store import StateSource, StateStore


_LOGGER = logging.getLogger(__name__)

StateCallback = Callable[[dict[str, Any], dict[str, Any], str], None]
LockCallback = Callable[[str, dict[str, Any]], None]
UpdatedCallback = Callable[[], None]
DeviceLabel = Callable[[str], str]


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


class StatusUpdateDispatcher:
    """Own the synchronous portion of inbound SSL status processing."""

    def __init__(
        self,
        devices: MutableMapping[str, dict[str, Any]],
        device_states: MutableMapping[str, dict[str, Any]],
        state_store: StateStore,
        last_update_time: MutableMapping[str, float],
        diagnostics: list[dict[str, Any]],
        *,
        on_motion: StateCallback,
        on_emergency: StateCallback,
        on_lock_transient: StateCallback,
        on_lock_message: LockCallback,
        on_lock_event: LockCallback,
        on_updated: UpdatedCallback,
        device_label: Optional[DeviceLabel] = None,
        clock: Callable[[], float] = time.time,
        diagnostic_limit: int = 200,
    ) -> None:
        self._devices = devices
        self._states = device_states
        self._state_store = state_store
        self._last_update_time = last_update_time
        self._diagnostics = diagnostics
        self._on_motion = on_motion
        self._on_emergency = on_emergency
        self._on_lock_transient = on_lock_transient
        self._on_lock_message = on_lock_message
        self._on_lock_event = on_lock_event
        self._on_updated = on_updated
        self._device_label = device_label or (lambda value: value)
        self._clock = clock
        self._diagnostic_limit = diagnostic_limit

    def resolve_device_id(
        self, incoming_device_id: str, raw_status: dict[str, Any]
    ) -> Optional[str]:
        """Resolve direct, UID, status, extended-address, and ``w-`` IDs."""

        if incoming_device_id in self._states:
            return incoming_device_id

        candidates: set[str] = set()
        for stored_id, state in self._states.items():
            device = self._devices.get(stored_id, {})
            aliases = {
                state.get("status_id"),
                state.get("app_device_id"),
                state.get("ext_addr"),
                device.get("status_id"),
                device.get("app_device_id"),
                device.get("ext_addr"),
            }
            if incoming_device_id in aliases:
                candidates.add(stored_id)
            if stored_id.startswith("w-") and stored_id[2:] == incoming_device_id:
                candidates.add(stored_id)
        return next(iter(candidates)) if len(candidates) == 1 else None

    def dispatch(
        self,
        incoming_device_id: str,
        raw_status: dict[str, Any],
        source: StateSource = StateSource.SSL,
    ) -> None:
        """Apply one inbound status packet and notify coordinator listeners."""

        _LOGGER.debug(
            "收到状态更新(source=%s): device=%s cmd=%s",
            source.name,
            self._device_label(incoming_device_id),
            raw_status.get("cmd"),
        )
        self._diagnostics.append(
            {
                "ts": self._clock(),
                "device_id": incoming_device_id,
                "raw": dict(raw_status),
            }
        )
        overflow = len(self._diagnostics) - self._diagnostic_limit
        if overflow > 0:
            del self._diagnostics[:overflow]

        device_id = self.resolve_device_id(incoming_device_id, raw_status)
        if device_id is None:
            _LOGGER.debug(
                "SSL 推送设备 %s 未匹配本地设备",
                self._device_label(incoming_device_id),
            )
            return

        state = self._states[device_id]
        candidate = dict(state)
        incoming_properties = raw_status.get("properties")
        if isinstance(incoming_properties, dict):
            candidate["properties"] = _deep_merge(
                state.get("properties", {}), incoming_properties
            )
        candidate["online"] = True
        self._last_update_time[device_id] = self._clock()

        device = self._devices.get(device_id)
        device_type = device.get("device_type_raw", 0) if device else 0
        sub_type = device.get("sub_device_type") if device else None
        category = classify_device(device) if device else DeviceCategory.UNKNOWN

        if raw_status.get("cmd") == 82:
            raw_status["source"] = source.name.lower()
            self._on_lock_message(device_id, raw_status)
            self._state_store.merge(
                device_id,
                {
                    "online": candidate["online"],
                    "properties": candidate.get("properties", {}),
                },
                source,
            )
            return

        if raw_status.get("is_clothes_horse"):
            self._apply_parser(DeviceCategory.CLOTHES_HORSE, candidate, raw_status)
        elif raw_status.get("cmd") == 352:
            self._on_lock_transient(candidate, raw_status, device_id)
        elif device_type == 26 or category == DeviceCategory.MOTION_SENSOR:
            self._on_motion(candidate, raw_status, device_id)
        elif device_type == 56 or category == DeviceCategory.EMERGENCY_BUTTON:
            self._on_emergency(candidate, raw_status, device_id)
        else:
            parser_category = self._parser_category(device_type, sub_type, category)
            if parser_category is None:
                self._apply_generic(candidate, raw_status)
            else:
                self._apply_parser(parser_category, candidate, raw_status)

        self._state_store.merge(
            device_id,
            candidate,
            source,
        )

        if (
            device_type == 522
            or category == DeviceCategory.DOOR_LOCK
            or raw_status.get("cmd") == 352
        ):
            raw_status["source"] = source.name.lower()
            self._on_lock_event(device_id, raw_status)
        self._on_updated()

    @staticmethod
    def _parser_category(
        device_type: Any,
        sub_type: Any,
        category: DeviceCategory,
    ) -> Optional[DeviceCategory]:
        direct = {
            46: DeviceCategory.DOOR_WINDOW_SENSOR,
            27: DeviceCategory.SMOKE_SENSOR,
            54: DeviceCategory.WATER_LEAK_SENSOR,
            522: DeviceCategory.DOOR_LOCK,
            34: DeviceCategory.ZIGBEE_CURTAIN,
            36: DeviceCategory.FAN_COIL_AC,
            516: DeviceCategory.VENTILATION_SYSTEM,
            135: DeviceCategory.MIX_SWITCH,
            136: DeviceCategory.MIX_SWITCH,
        }
        if device_type == 38:
            return (
                DeviceCategory.FAST_MOVE_DIM_COLOR_LIGHT
                if sub_type == 6
                else DeviceCategory.DIM_COLOR_LIGHT
            )
        if device_type == 0 and sub_type == -2:
            return DeviceCategory.ZIGBEE_DIMMABLE_LIGHT
        if device_type == 300 and sub_type == 491:
            return DeviceCategory.TEMP_HUMIDITY_SENSOR
        if device_type in direct:
            return direct[device_type]
        if category == DeviceCategory.CLOTHES_HORSE:
            return None
        return category if get_state_parser(category) is not None else None

    @staticmethod
    def _apply_parser(
        category: DeviceCategory,
        state: dict[str, Any],
        raw_status: dict[str, Any],
    ) -> None:
        parser = get_state_parser(category)
        if parser is not None:
            parser(state, raw_status).apply_to(state)

    @staticmethod
    def _apply_generic(state: dict[str, Any], raw_status: dict[str, Any]) -> None:
        props = raw_status.get("properties", {})
        onoff = props.get("onoff", {})
        if isinstance(onoff, dict) and onoff.get("status"):
            state["state"] = onoff.get("status") == "on"
        else:
            state["state"] = raw_status.get("state", False)
        state["brightness"] = raw_status.get("value2", props.get("brightness"))
        state["color_temp"] = raw_status.get("value3", props.get("colortemp"))
        state["position"] = raw_status.get("value1", props.get("percent"))
