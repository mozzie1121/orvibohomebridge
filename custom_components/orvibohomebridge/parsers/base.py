"""Shared contracts for pure device-state parsers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping


def to_int(value: Any) -> int | None:
    """Safe int conversion：dict/list/非数字一律返回 None，不再抛异常。

    历史教训：v0.5.0 修过属性型 brightness/colortemp 为 dict 导致 int(dict)
    崩溃的 bug，但 value3/zigbee/fast-move 等分支仍有裸 int()。统一走这里。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class StatePatch:
    """A side-effect-free set of normalized state values."""

    values: Mapping[str, Any]

    def apply_to(self, state: Dict[str, Any]) -> None:
        """Apply this patch to a coordinator-owned mutable state mapping."""

        state.update(self.values)


StateParser = Callable[[Mapping[str, Any], Mapping[str, Any]], StatePatch]
