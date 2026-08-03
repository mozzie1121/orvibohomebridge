"""Stateful orchestration for normalized smart-lock events."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Callable, Mapping, Optional

from .lock_status import (
    normalize_door_lock_properties,
    normalize_lock_event,
    normalize_message_event,
    resolve_opened_by,
)
from .parsers.base import StatePatch


UserNameResolver = Callable[[str, object], Optional[str]]
Clock = Callable[[], float]


@dataclass(frozen=True)
class TransientLockUpdate:
    """A transient entity-state patch plus its optional reset category."""

    patch: StatePatch
    reset_kind: Optional[str] = None


class LockEventManager:
    """Normalize, deduplicate, and attribute door-lock events."""

    def __init__(
        self,
        user_name: UserNameResolver,
        *,
        door_open_window: float = 30,
        clock: Clock = time.monotonic,
    ) -> None:
        self._user_name = user_name
        self._door_open_window = door_open_window
        self._clock = clock
        self._last_signatures: dict[str, tuple[Any, ...]] = {}
        self._last_unlocks: dict[str, dict[str, Any]] = {}

    def build_event(
        self, device_id: str, raw_status: Mapping[str, Any]
    ) -> Optional[dict[str, Any]]:
        """Build one public event, returning ``None`` for duplicates/no-op packets."""

        lock = normalize_door_lock_properties(raw_status.get("properties"))
        event = normalize_lock_event(raw_status)
        if event is None and all(
            lock[field] is None
            for field in (
                "locked",
                "door_open",
                "inside_locked",
                "child_locked",
                "leave_home_armed",
            )
        ):
            return None

        if event is not None:
            signature = (
                event.get("kind"),
                event.get("unlock_type"),
                event.get("unlock_user_id"),
                event.get("time"),
            )
        else:
            signature = (
                "state",
                lock["locked"],
                lock["door_open"],
                lock["inside_locked"],
                lock["child_locked"],
                lock["leave_home_armed"],
            )
        if self._last_signatures.get(device_id) == signature:
            return None
        self._last_signatures[device_id] = signature

        data: dict[str, Any] = {
            "device_id": device_id,
            "uid": raw_status.get("uid", ""),
            "locked": lock["locked"],
            "door_open": lock["door_open"],
            "inside_locked": lock["inside_locked"],
            "child_locked": lock["child_locked"],
            "leave_home_armed": lock["leave_home_armed"],
        }
        if event is not None:
            data.update(event)

        now = self._clock()
        if event is not None and event.get("kind") == "unlock":
            self._last_unlocks[device_id] = {
                "user_id": event.get("unlock_user_id"),
                "unlock_type": event.get("unlock_type"),
                "at": now,
            }
            name = self._user_name(device_id, event.get("unlock_user_id"))
            if name:
                data["unlock_user_name"] = name

        if lock["door_open"] is True:
            last_unlock = self._last_unlocks.get(device_id)
            opened = (
                resolve_opened_by(
                    last_unlock,
                    now - last_unlock["at"],
                    self._door_open_window,
                )
                if last_unlock is not None
                else None
            )
            if opened is not None:
                data["opened_by_user_id"] = opened["user_id"]
                data["opened_by_type"] = opened["unlock_type"]
                name = self._user_name(device_id, opened["user_id"])
                if name:
                    data["opened_by_name"] = name
        return data

    @staticmethod
    def build_message(
        device_id: str, raw_status: Mapping[str, Any]
    ) -> Optional[dict[str, Any]]:
        """Normalize a cmd=82 lock message for public event publication."""

        event = normalize_message_event(raw_status)
        if event is None:
            return None
        event["device_id"] = device_id or event.get("device_id")
        event["snapshot_kind"] = LockEventManager.snapshot_kind(
            event.get("text") or ""
        )
        return event

    @staticmethod
    def transient_update(raw_status: Mapping[str, Any]) -> TransientLockUpdate:
        """Convert cmd=352 UI flags to a pure patch and reset instruction."""

        event = normalize_lock_event(raw_status)
        if event is None:
            return TransientLockUpdate(StatePatch({}))
        kind = event.get("kind")
        if kind == "ring":
            return TransientLockUpdate(
                StatePatch(
                    {
                        "doorbell_ring": True,
                        "doorbell_url": event.get("doorbell_url"),
                        "doorbell_ip": event.get("doorbell_ip"),
                    }
                ),
                "doorbell",
            )
        if kind == "answered":
            return TransientLockUpdate(StatePatch({"doorbell_answered": True}))
        if kind == "bye":
            return TransientLockUpdate(StatePatch({"doorbell_answered": False}))
        if kind == "unlock":
            return TransientLockUpdate(
                StatePatch(
                    {
                        "unlock_event": True,
                        "unlock_type": event.get("unlock_type"),
                        "unlock_user_id": event.get("unlock_user_id"),
                        "unlock_time": event.get("time"),
                    }
                ),
                "unlock",
            )
        return TransientLockUpdate(StatePatch({}))

    @staticmethod
    def snapshot_kind(text: str) -> str:
        """Classify message snapshots for history-card grouping."""

        if "逗留" in text:
            return "loiter"
        if "来访" in text or "访客" in text:
            return "visit"
        return "message"

    def remove(self, device_id: str) -> None:
        """Discard event correlation state for a removed device."""

        self._last_signatures.pop(device_id, None)
        self._last_unlocks.pop(device_id, None)
