"""Shared immutable models for the LAN transport package."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class DecodedPacket:
    """Validated packet contents and routing metadata."""

    packet_type: bytes
    session_id: bytes
    payload: Mapping[str, Any]


def immutable_value(value: Any) -> Any:
    """Recursively detach and freeze a value for a public snapshot."""

    if isinstance(value, Mapping):
        return MappingProxyType({key: immutable_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(immutable_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(immutable_value(item) for item in value)
    return value
