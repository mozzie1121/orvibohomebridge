"""Gateway lifecycle, reconnect, and trusted endpoint management."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from .discovery import DiscoveryCandidate, GatewayDiscovery
from .gateway_connection import (
    GatewayConnection,
    GatewayConnectionError,
    PushCallback,
)

_LOGGER = logging.getLogger(__name__)

ConnectionFactory = Callable[[str], GatewayConnection]
ManagerPushCallback = Callable[[str, Mapping[str, Any]], Awaitable[None] | None]


@dataclass(slots=True)
class _GatewayRecord:
    cloud_host: str
    active_host: str
    connection: GatewayConnection | None = None


class GatewayManager:
    """Own one serialized connection lifecycle per known gateway UID."""

    def __init__(
        self,
        username: str,
        password: str,
        cloud_gateways: Mapping[str, str],
        *,
        discovery: GatewayDiscovery | None = None,
        connection_factory: ConnectionFactory | None = None,
        push_callback: ManagerPushCallback | None = None,
        password_is_hash: bool = False,
    ) -> None:
        self.username = username
        self.password = password
        self.password_is_hash = password_is_hash
        self._discovery = discovery or GatewayDiscovery()
        self._connection_factory = connection_factory or GatewayConnection
        self._push_callback = push_callback
        self._records: dict[str, _GatewayRecord] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._closed = False
        self.update_cloud_gateways(cloud_gateways)

    @property
    def gateway_hosts(self) -> dict[str, str]:
        """Return a detached mapping of trusted active endpoints."""

        return {uid: record.active_host for uid, record in self._records.items()}

    def is_connected(self, uid: str) -> bool:
        """Return whether the currently managed generation is ready."""

        record = self._records.get(uid)
        return bool(
            record is not None and record.connection is not None and record.connection.connected
        )

    def update_cloud_gateways(self, gateways: Mapping[str, str]) -> None:
        """Refresh known private cloud endpoints without trusting UDP candidates."""

        for uid, raw_host in gateways.items():
            host = self._private_host(raw_host)
            if not uid or host is None:
                continue
            record = self._records.get(uid)
            if record is None:
                self._records[uid] = _GatewayRecord(host, host)
                continue
            old_cloud = record.cloud_host
            record.cloud_host = host
            if record.active_host == old_cloud:
                record.active_host = host

    async def ensure(self, uid: str) -> GatewayConnection:
        """Return the connected active generation for a known UID."""

        self._raise_if_closed()
        async with self._lock(uid):
            self._raise_if_closed()
            record = self._record(uid)
            return await self._ensure_locked(uid, record)

    async def reconnect(self, uid: str) -> GatewayConnection:
        """Force a fresh connection generation at the trusted endpoint."""

        self._raise_if_closed()
        async with self._lock(uid):
            self._raise_if_closed()
            record = self._record(uid)
            return await self._reconnect_locked(uid, record, record.connection)

    async def async_update_cloud_gateways(
        self,
        gateways: Mapping[str, str],
    ) -> None:
        """Reconcile records with the latest authoritative cloud snapshot."""

        self._raise_if_closed()
        trusted_uids = {uid for uid, host in gateways.items() if uid and self._private_host(host)}
        self.update_cloud_gateways(gateways)
        for uid in set(self._records) - trusted_uids:
            async with self._lock(uid):
                record = self._records.pop(uid, None)
                connection = record.connection if record is not None else None
            if connection is not None:
                await connection.close()

    async def send(
        self,
        uid: str,
        payload: Mapping[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send exactly once; an unknown response outcome is never auto-retried."""

        self._raise_if_closed()
        connection = await self.ensure(uid)
        self._raise_if_closed()
        return await connection.send(payload, timeout=timeout)

    async def discover(self) -> dict[str, DiscoveryCandidate]:
        """Promote a UDP candidate only after TCP login confirms its UID."""

        self._raise_if_closed()
        candidates = await self._discovery.discover(set(self._records))
        self._raise_if_closed()

        for uid, candidate in candidates.items():
            record = self._records.get(uid)
            if record is None or candidate.host == record.active_host:
                continue

            previous: GatewayConnection | None = None
            async with self._lock(uid):
                self._raise_if_closed()
                candidate_connection: GatewayConnection | None = None
                try:
                    candidate_connection = await self._connect(
                        uid,
                        candidate.host,
                        enable_pushes=False,
                    )
                    if not candidate_connection.identity_confirmed:
                        await candidate_connection.close()
                        continue
                except GatewayConnectionError:
                    if self._closed:
                        raise
                    if candidate_connection is not None:
                        await candidate_connection.close()
                    continue

                if self._closed:
                    await candidate_connection.close()
                    raise GatewayConnectionError(
                        "gateway manager is closed",
                        reason="manager_closed",
                    )

                previous = record.connection
                record.connection = candidate_connection
                record.active_host = candidate.host
                self._set_push_callback(
                    candidate_connection,
                    self._bound_push_callback(uid),
                )

            if previous is not None:
                await previous.close()

        return candidates

    async def close(self) -> None:
        """Idempotently detach and close every managed connection."""

        self._closed = True

        async def close_record(uid: str, record: _GatewayRecord) -> None:
            async with self._lock(uid):
                connection, record.connection = record.connection, None
            if connection is not None:
                await connection.close()

        results = await asyncio.gather(
            *(close_record(uid, record) for uid, record in self._records.items()),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                _LOGGER.warning(
                    "Failed to close an ORVIBO gateway connection (%s)",
                    type(result).__name__,
                )

    async def _ensure_locked(
        self,
        uid: str,
        record: _GatewayRecord,
    ) -> GatewayConnection:
        self._raise_if_closed()
        connection = record.connection
        if connection is not None and connection.connected:
            return connection
        return await self._reconnect_locked(uid, record, connection)

    async def _reconnect_locked(
        self,
        uid: str,
        record: _GatewayRecord,
        failed: GatewayConnection | None,
    ) -> GatewayConnection:
        self._raise_if_closed()
        current = record.connection
        if current is not None and current is not failed and current.connected:
            return current
        if current is not None:
            await current.close()

        connection = await self._connect(
            uid,
            record.active_host,
            allow_missing_uid=record.active_host == record.cloud_host,
        )
        if self._closed:
            await connection.close()
            raise GatewayConnectionError(
                "gateway manager is closed",
                reason="manager_closed",
            )
        record.connection = connection
        return connection

    async def _connect(
        self,
        uid: str,
        host: str,
        *,
        enable_pushes: bool = True,
        allow_missing_uid: bool = False,
    ) -> GatewayConnection:
        self._raise_if_closed()
        connection = self._connection_factory(host)
        self._set_push_callback(
            connection,
            self._bound_push_callback(uid) if enable_pushes else None,
        )
        try:
            await connection.connect(
                self.username,
                self.password,
                expected_uid=uid,
                allow_missing_uid=allow_missing_uid,
                password_is_hash=self.password_is_hash,
            )
        except BaseException:
            with suppress(Exception):
                await connection.close()
            raise
        return connection

    def _raise_if_closed(self) -> None:
        if self._closed:
            raise GatewayConnectionError(
                "gateway manager is closed",
                reason="manager_closed",
            )

    def _record(self, uid: str) -> _GatewayRecord:
        try:
            return self._records[uid]
        except KeyError as error:
            raise KeyError("unknown gateway UID") from error

    def _lock(self, uid: str) -> asyncio.Lock:
        return self._locks.setdefault(uid, asyncio.Lock())

    def _bound_push_callback(self, uid: str) -> PushCallback | None:
        callback = self._push_callback
        if callback is None:
            return None

        def bound(payload: Mapping[str, Any]) -> Awaitable[None] | None:
            return callback(uid, payload)

        return bound

    @staticmethod
    def _set_push_callback(
        connection: GatewayConnection,
        callback: PushCallback | None,
    ) -> None:
        setter = getattr(connection, "set_push_callback", None)
        if callable(setter):
            setter(callback)

    @staticmethod
    def _private_host(raw_host: str) -> str | None:
        host = raw_host.rsplit(":", 1)[0] if raw_host.count(":") == 1 else raw_host
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return None
        if (
            address.version != 4
            or not address.is_private
            or address.is_loopback
            or address.is_unspecified
            or address.is_multicast
        ):
            return None
        return host
