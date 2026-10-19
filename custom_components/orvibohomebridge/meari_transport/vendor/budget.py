"""Shared monotonic soft deadline; the parent process enforces a hard limit."""
from __future__ import annotations

import time

_deadline = float("inf")


def configure(seconds: float) -> None:
    global _deadline
    _deadline = time.monotonic() + max(0.0, seconds)


def remaining(limit: float = 3.0) -> float:
    left = _deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("probe_deadline")
    return min(limit, left)
