"""Process-wide request serial allocation for Orvibo protocol messages."""

from __future__ import annotations

import threading
import time

_MAX_SERIAL = 999_999
_lock = threading.Lock()
_value = int(time.time() * 1000) % _MAX_SERIAL


def next_serial() -> int:
    """Return a non-zero serial unique within the next million allocations."""

    global _value
    with _lock:
        _value = _value % _MAX_SERIAL + 1
        return _value
