"""Field-level state reconciliation across cloud, SSL, and optimistic updates."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import time
from typing import Any, Iterable, Mapping, MutableMapping


class StateSource(IntEnum):
    INITIAL = 0
    OPTIMISTIC = 10
    CLOUD = 20
    SSL = 30
    LAN = 40  # 局域网网关实时推送（优先级最高，融合后本地优先）


@dataclass(frozen=True, slots=True)
class FieldRevision:
    source: StateSource
    updated_at: float


class StateStore:
    """Merge state per field while protecting fresh higher-priority values."""

    def __init__(
        self,
        states: MutableMapping[str, dict[str, Any]],
        *,
        priority_guard_seconds: float = 30.0,
    ) -> None:
        self.states = states
        self.priority_guard_seconds = priority_guard_seconds
        self._revisions: dict[tuple[str, str], FieldRevision] = {}

    def merge(
        self,
        device_id: str,
        values: Mapping[str, Any],
        source: StateSource,
        *,
        now: float | None = None,
        force: bool = False,
    ) -> set[str]:
        timestamp = time.monotonic() if now is None else now
        state = self.states.setdefault(device_id, {})
        changed: set[str] = set()
        for field, value in values.items():
            revision = self._revisions.get((device_id, field))
            if (
                not force
                and revision is not None
                and source < revision.source
                and timestamp - revision.updated_at < self.priority_guard_seconds
            ):
                continue
            if state.get(field) != value or field not in state:
                state[field] = value
                changed.add(field)
            self._revisions[(device_id, field)] = FieldRevision(source, timestamp)
        return changed

    def mark(
        self,
        device_id: str,
        fields: Iterable[str],
        source: StateSource,
        *,
        now: float | None = None,
    ) -> None:
        timestamp = time.monotonic() if now is None else now
        for field in fields:
            self._revisions[(device_id, field)] = FieldRevision(source, timestamp)

    def remove(self, device_id: str) -> None:
        for key in tuple(self._revisions):
            if key[0] == device_id:
                self._revisions.pop(key, None)
