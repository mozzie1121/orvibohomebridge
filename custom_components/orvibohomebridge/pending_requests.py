"""Small request/response correlation registry for the SSL protocol."""

from __future__ import annotations

import asyncio
from typing import Any


class PendingRequests:
    """Track at most one in-flight response for each correlation key."""

    def __init__(self) -> None:
        self._futures: dict[str, asyncio.Future[dict[str, Any] | None]] = {}

    def register(
        self,
        key: str,
        *,
        replace: bool = False,
    ) -> asyncio.Future[dict[str, Any] | None]:
        current = self._futures.get(key)
        if current is not None and not current.done():
            if not replace:
                raise RuntimeError(f"request already pending: {key}")
            current.set_result(None)
        future = asyncio.get_running_loop().create_future()
        self._futures[key] = future
        return future

    def get(self, key: str) -> asyncio.Future[dict[str, Any] | None] | None:
        return self._futures.get(key)

    def resolve(self, key: str, result: dict[str, Any] | None) -> bool:
        future = self._futures.get(key)
        if future is None or future.done():
            return False
        future.set_result(result)
        return True

    async def wait(
        self,
        key: str,
        future: asyncio.Future[dict[str, Any] | None],
        timeout: float,
    ) -> dict[str, Any] | None:
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            if self._futures.get(key) is future:
                self._futures.pop(key, None)

    def cancel_all(self) -> None:
        for future in self._futures.values():
            if not future.done():
                future.set_result(None)
        self._futures.clear()
