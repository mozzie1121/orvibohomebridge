"""Shared contracts for pure device-state parsers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping


@dataclass(frozen=True)
class StatePatch:
    """A side-effect-free set of normalized state values."""

    values: Mapping[str, Any]

    def apply_to(self, state: Dict[str, Any]) -> None:
        """Apply this patch to a coordinator-owned mutable state mapping."""

        state.update(self.values)


StateParser = Callable[[Mapping[str, Any], Mapping[str, Any]], StatePatch]
