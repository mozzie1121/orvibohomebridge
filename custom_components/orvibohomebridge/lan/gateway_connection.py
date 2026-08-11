"""Concurrency-safe TCP connection for one Orvibo gateway."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from typing import Any

from .exceptions import InvalidLengthError, InvalidMagicError, OrviboLanError, ProtocolError
from .privacy import mask_host, mask_identifier
from .packet import (
    CMD_HEARTBEAT,
    CMD_HELLO,
    CMD_LOGIN,
    CMD_STATE_UPDATE,
    DEBUG_INFO,
    HARDWARE_VERSION,
    ID_UNSET,
    LANGUAGE,
    PHONE_NAME,
    SOFTWARE_NAME,
    SOFTWARE_VER,
    SOFTWARE_VERSION,
    SYS_VERSION,
    TCP_PORT,
)
from .protocol import (
    DEFAULT_KEY,
    DK_TYPE,
    HEADER_LENGTH,
    MAGIC,
    PK_TYPE,
    build_packet,
    decode_packet,
)
from .serial import next_serial

_LOGGER = logging.getLogger(__name__)
Payload = dict[str, Any]
PushCallback = Callable[[Mapping[str, Any]], Awaitable[None] | None]
OpenConnection = Callable[
    [str, int],
    Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]],
]


class GatewayConnectionError(OrviboLanError):
    """Raised when a gateway connection cannot complete an operation."""

    def __init__(self, message: str, *, reason: str = "connection_error") -> None:
        super().__init__(message)
        self.reason = reason


class GatewayDisconnectedError(GatewayConnectionError):
    """Raised when an operation loses its transport."""


class GatewayRequestTimeoutError(GatewayConnectionError, TimeoutError):
    """Raised when a complete request exceeds its deadline."""


class GatewayLoginRejectedError(GatewayConnectionError):
    """Raised when the gateway explicitly rejects the login credentials."""


def _serial() -> int:
    return next_serial()


class GatewayConnection:
    """Own the sole reader and correlate responses for one TCP generation."""

    def __init__(
        self,
        host: str,
        port: int = TCP_PORT,
        *,
        request_timeout: float = 5.0,
        heartbeat_interval: float = 60.0,
        heartbeat_timeout: float = 5.0,
        open_connection: OpenConnection = asyncio.open_connection,
        push_callback: PushCallback | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.request_timeout = request_timeout
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_timeout = heartbeat_timeout
        self._open_connection = open_connection
        self._push_callback = push_callback
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.session_id: bytes | None = None
        self.session_key: bytes | None = None
        self.peer_uid: str | None = None
        self.identity_confirmed = False
        self.generation = 0
        self._keys: dict[bytes, bytes] = {ID_UNSET: DEFAULT_KEY}
        self._reader_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._pending: dict[tuple[str, str], asyncio.Future[Payload]] = {}
        self._single_pending: asyncio.Future[Payload] | None = None
        self._closed = True
        self._ready = False

    @property
    def connected(self) -> bool:
        return self._transport_active and self._ready

    def set_push_callback(self, callback: PushCallback | None) -> None:
        """Enable pushes only after this connection becomes the trusted active one."""

        self._push_callback = callback

    @property
    def _transport_active(self) -> bool:
        return self.reader is not None and self.writer is not None and not self._closed

    async def connect(
        self,
        username: str,
        password: str,
        *,
        timeout: float = 5.0,
        expected_uid: str | None = None,
        allow_missing_uid: bool = False,
        password_is_hash: bool = False,
    ) -> None:
        """Open, negotiate, and authenticate a fresh transport generation."""

        if timeout <= 0:
            raise ValueError("timeout must be positive")
        start_generation = self.generation
        try:
            await asyncio.wait_for(
                self._connect_locked(
                    username,
                    password,
                    expected_uid=expected_uid,
                    allow_missing_uid=allow_missing_uid,
                    request_timeout=timeout,
                    password_is_hash=password_is_hash,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError as error:
            await self._close_if_generation_changed(start_generation)
            raise GatewayRequestTimeoutError(
                f"gateway connect timed out after {timeout:g}s",
                reason="connect_timeout",
            ) from error
        except BaseException:
            await self._close_if_generation_changed(start_generation)
            raise

    async def _close_if_generation_changed(self, start_generation: int) -> None:
        """Clean up only the unready generation created by this connect attempt."""

        async with self._lifecycle_lock:
            should_close = self.generation == start_generation + 1 and not self._ready
        if should_close:
            await self.close()

    async def _connect_locked(
        self,
        username: str,
        password: str,
        expected_uid: str | None,
        allow_missing_uid: bool,
        request_timeout: float,
        password_is_hash: bool = False,
    ) -> None:
        async with self._lifecycle_lock:
            if self.connected:
                return

            reader, writer = await self._open_connection(self.host, self.port)
            self.generation += 1
            generation = self.generation
            self.reader = reader
            self.writer = writer
            self._closed = False
            self._ready = False
            self.session_id = None
            self.session_key = None
            self.peer_uid = None
            self.identity_confirmed = False
            self._keys = {ID_UNSET: DEFAULT_KEY}
            self._reader_task = asyncio.create_task(
                self._reader_loop(generation, reader, writer),
                name=f"orvibo-reader-{mask_identifier(self.host)}-{generation}",
            )

            hello = await self.request(
                self._hello_payload(),
                packet_type=PK_TYPE,
                key=DEFAULT_KEY,
                session_id=ID_UNSET,
                timeout=request_timeout,
            )
            session_id = self._response_session_id(hello)
            raw_key = hello.get("sessionKey") or hello.get("key")
            if session_id is None or raw_key is None:
                raise GatewayConnectionError(
                    "hello response omitted session credentials",
                    reason="hello_credentials_missing",
                )
            hello_uid = self._extract_uid(hello)
            if expected_uid is not None and hello_uid is not None and hello_uid != expected_uid:
                raise GatewayConnectionError(
                    "gateway hello did not confirm expected UID",
                    reason="hello_uid_mismatch",
                )
            self.session_key = self._decode_session_key(raw_key)
            self.session_id = session_id
            self._keys[session_id] = self.session_key

            login = await self.request(
                {
                    "cmd": CMD_LOGIN,
                    "serial": _serial(),
                    "userName": username,
                    "password": (
                        password
                        if password_is_hash
                        else hashlib.md5(password.encode()).hexdigest().upper()
                    ),
                    "clientType": 1,
                    "source": SOFTWARE_NAME,
                },
                timeout=request_timeout,
            )
            login_status = login.get("status")
            if login_status != 0:
                safe_status = (
                    login_status
                    if isinstance(login_status, int)
                    and not isinstance(login_status, bool)
                    else "invalid"
                )
                _LOGGER.debug(
                    "Gateway login rejected for %s (status=%s)",
                    mask_host(self.host),
                    safe_status,
                )
                raise GatewayLoginRejectedError(
                    "gateway login was rejected",
                    reason="login_rejected",
                )

            login_uid = self._extract_uid(login)
            if hello_uid and login_uid and hello_uid != login_uid:
                raise GatewayConnectionError(
                    "gateway handshake returned conflicting UIDs",
                    reason="handshake_uid_conflict",
                )
            self.peer_uid = login_uid or hello_uid
            self.identity_confirmed = bool(
                expected_uid is not None and self.peer_uid == expected_uid
            )
            if (
                expected_uid is not None
                and not self.identity_confirmed
                and not (allow_missing_uid and self.peer_uid is None)
            ):
                reason = (
                    "identity_missing"
                    if self.peer_uid is None
                    else "handshake_uid_mismatch"
                )
                raise GatewayConnectionError(
                    "gateway handshake did not confirm expected UID",
                    reason=reason,
                )
            if expected_uid is not None and self.peer_uid is None:
                _LOGGER.debug(
                    "Gateway %s did not return an identity; using the cloud-provided endpoint",
                    mask_host(self.host),
                )

            self._ready = True
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(generation),
                name=f"orvibo-heartbeat-{mask_identifier(self.host)}-{generation}",
            )
            _LOGGER.debug(
                "Gateway connection ready for %s (generation=%s, identity_confirmed=%s, pushes=%s)",
                mask_host(self.host),
                generation,
                self.identity_confirmed,
                self._push_callback is not None,
            )

    async def request(
        self,
        payload: Mapping[str, Any],
        *,
        timeout: float | None = None,
        packet_type: bytes = DK_TYPE,
        key: bytes | None = None,
        session_id: bytes | None = None,
    ) -> Payload:
        """Bound lock wait, write, and response wait by one deadline."""

        deadline = self.request_timeout if timeout is None else timeout
        if deadline <= 0:
            raise ValueError("timeout must be positive")
        reliable = self._correlation_keys(payload)
        try:
            return await asyncio.wait_for(
                self._request_with_guard(
                    payload,
                    reliable,
                    packet_type,
                    key,
                    session_id,
                ),
                timeout=deadline,
            )
        except asyncio.TimeoutError as error:
            raise GatewayRequestTimeoutError(
                f"gateway request timed out after {deadline:g}s",
                reason="request_timeout",
            ) from error

    async def _request_with_guard(
        self,
        payload: Mapping[str, Any],
        reliable: tuple[tuple[str, str], ...],
        packet_type: bytes,
        key: bytes | None,
        session_id: bytes | None,
    ) -> Payload:
        if reliable:
            return await self._request_once(
                payload,
                reliable,
                packet_type,
                key,
                session_id,
            )
        async with self._request_lock:
            return await self._request_once(
                payload,
                reliable,
                packet_type,
                key,
                session_id,
            )

    async def _request_once(
        self,
        payload: Mapping[str, Any],
        reliable: tuple[tuple[str, str], ...],
        packet_type: bytes,
        key: bytes | None,
        session_id: bytes | None,
    ) -> Payload:
        if not self._transport_active:
            raise GatewayDisconnectedError(
                "gateway is not connected",
                reason="transport_not_connected",
            )

        generation = self.generation
        request = dict(payload)
        request.setdefault("serial", _serial())
        request.setdefault("uniSerial", _serial())
        request.setdefault("serverRecord", False)
        request.setdefault("ver", SOFTWARE_VER)
        request.setdefault("debugInfo", DEBUG_INFO)
        routes = reliable or self._correlation_keys(request)
        future: asyncio.Future[Payload] = asyncio.get_running_loop().create_future()

        for route in routes:
            existing = self._pending.get(route)
            if existing is not None and not existing.done():
                raise GatewayConnectionError(
                    f"duplicate request correlation: {route!r}",
                    reason="correlation_collision",
                )
            self._pending[route] = future
        if not reliable:
            self._single_pending = future

        try:
            packet = build_packet(
                packet_type,
                key or self.session_key or DEFAULT_KEY,
                session_id or self.session_id or ID_UNSET,
                request,
            )
            await self._write(packet, generation)
            return await asyncio.shield(future)
        finally:
            for route in routes:
                if self._pending.get(route) is future:
                    self._pending.pop(route, None)
            if self._single_pending is future:
                self._single_pending = None
            if not future.done():
                future.cancel()

    async def send(
        self,
        payload: Mapping[str, Any],
        *,
        timeout: float | None = None,
    ) -> Payload:
        if not self.connected:
            raise GatewayDisconnectedError(
                "gateway login is not complete",
                reason="login_incomplete",
            )
        return await self.request(payload, timeout=timeout)

    async def close(self) -> None:
        """Idempotently detach, then stop tasks and close the transport."""

        async with self._lifecycle_lock:
            current = asyncio.current_task()
            tasks = (self._heartbeat_task, self._reader_task)
            writer = self.writer
            if self._closed and writer is None and all(task is None for task in tasks):
                return

            self._closed = True
            self._ready = False
            self.identity_confirmed = False
            self.generation += 1
            self._heartbeat_task = None
            self._reader_task = None
            self.writer = None
            self.reader = None
            self._fail_pending(
                GatewayDisconnectedError(
                    "gateway connection closed",
                    reason="connection_closed",
                )
            )

        for task in tasks:
            if task is not None and task is not current and not task.done():
                task.cancel()
        for task in tasks:
            if task is not None and task is not current:
                with suppress(asyncio.CancelledError, Exception):
                    await task
        if writer is not None:
            with suppress(Exception):
                writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    async def _write(self, packet: bytes, generation: int) -> None:
        failure: BaseException | None = None
        async with self._write_lock:
            writer = self.writer
            if writer is None or self._closed or generation != self.generation:
                raise GatewayDisconnectedError(
                    "gateway transport is unavailable",
                    reason="transport_unavailable",
                )
            try:
                writer.write(packet)
                await writer.drain()
            except (ConnectionError, OSError) as error:
                failure = error
                self._disconnect_generation(
                    generation,
                    writer,
                    GatewayDisconnectedError(
                        "gateway write failed",
                        reason="write_failed",
                    ),
                )
        if failure is not None:
            raise GatewayDisconnectedError(
                "gateway write failed",
                reason="write_failed",
            ) from failure

    async def _reader_loop(
        self,
        generation: int,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            while generation == self.generation and not self._closed:
                packet = await self._read_frame(reader)
                decoded = decode_packet(packet, self._keys)
                payload = dict(decoded.payload)
                payload["__session_id"] = decoded.session_id
                await self._route(payload)
        except asyncio.CancelledError:
            raise
        except (
            EOFError,
            OSError,
            asyncio.IncompleteReadError,
            ProtocolError,
        ) as error:
            _LOGGER.debug(
                "Gateway reader stopped for %s (%s)",
                mask_host(self.host),
                type(error).__name__,
            )
        finally:
            if generation == self.generation and not self._closed:
                self._disconnect_generation(
                    generation,
                    writer,
                    GatewayDisconnectedError(
                        "gateway reader stopped",
                        reason="reader_stopped",
                    ),
                )

    def _disconnect_generation(
        self,
        generation: int,
        writer: asyncio.StreamWriter,
        error: GatewayDisconnectedError,
    ) -> None:
        if generation != self.generation or self.writer is not writer or self._closed:
            return

        self._closed = True
        self._ready = False
        self.identity_confirmed = False
        self._fail_pending(error)
        self.writer = None
        self.reader = None
        with suppress(Exception):
            writer.close()

        heartbeat = self._heartbeat_task
        if heartbeat is not None and heartbeat is not asyncio.current_task():
            heartbeat.cancel()
        reader_task = self._reader_task
        if reader_task is not None and reader_task is not asyncio.current_task():
            reader_task.cancel()

    @staticmethod
    async def _read_frame(reader: asyncio.StreamReader) -> bytes:
        header = await reader.readexactly(4)
        if header[:2] != MAGIC:
            raise InvalidMagicError("invalid frame magic")
        length = int.from_bytes(header[2:4], "big")
        if length < HEADER_LENGTH + 16:
            raise InvalidLengthError(f"invalid frame length {length}")
        return header + await reader.readexactly(length - 4)

    async def _route(self, payload: Payload) -> None:
        command = self._command_value(payload.get("cmd"))
        response_routes = self._correlation_keys(payload)
        future = next(
            (self._pending[route] for route in response_routes if route in self._pending),
            None,
        )
        single_pending = self._single_pending if not response_routes else None

        # Some firmware uses a non-standard command number for uncorrelated
        # device updates. Those packets receive stricter gateway and field
        # validation in the coordinator before they can affect entity state.
        is_unsolicited_update = command == CMD_STATE_UPDATE or (
            future is None and single_pending is None and self._has_device_id(payload)
        )
        if is_unsolicited_update:
            _LOGGER.debug(
                "Gateway %s received device update (cmd=%r, fields=%s)",
                mask_host(self.host),
                command,
                tuple(sorted(str(key) for key in payload if not str(key).startswith("__"))),
            )
            if self._push_callback is not None:
                try:
                    result = self._push_callback(payload)
                    if inspect.isawaitable(result):
                        await result
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    _LOGGER.error(
                        "Gateway push callback failed for %s (%s)",
                        mask_host(self.host),
                        type(error).__name__,
                    )
            else:
                _LOGGER.debug(
                    "Gateway %s dropped device update because pushes are disabled",
                    mask_host(self.host),
                )
            return

        if future is None:
            future = single_pending
        if future is not None and not future.done():
            future.set_result(payload)

    @staticmethod
    def _command_value(value: Any) -> int | None:
        """Normalize integer command fields without accepting booleans."""

        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                return None
        return None

    @staticmethod
    def _has_device_id(payload: Mapping[str, Any]) -> bool:
        """Identify unsolicited device updates without logging the identifier."""

        for field in ("deviceId", "deviceID", "device_id"):
            value = payload.get(field)
            if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value):
                return True
        return False

    async def _heartbeat_loop(self, generation: int) -> None:
        try:
            while self.connected and generation == self.generation:
                await asyncio.sleep(self.heartbeat_interval)
                if generation != self.generation or not self.connected:
                    return
                await self.request(
                    {
                        "cmd": CMD_HEARTBEAT,
                        "serial": _serial(),
                        "clientType": 1,
                    },
                    timeout=self.heartbeat_timeout,
                )
        except asyncio.CancelledError:
            raise
        except GatewayConnectionError as error:
            _LOGGER.debug(
                "Gateway heartbeat failed for %s (reason=%s)",
                mask_host(self.host),
                error.reason,
            )
            if generation == self.generation:
                await self.close()

    def _fail_pending(self, error: BaseException) -> None:
        futures = set(self._pending.values())
        if self._single_pending is not None:
            futures.add(self._single_pending)
        self._pending.clear()
        self._single_pending = None
        for future in futures:
            if not future.done():
                future.set_exception(error)

    @staticmethod
    def _correlation_keys(
        payload: Mapping[str, Any],
    ) -> tuple[tuple[str, str], ...]:
        return tuple(
            (field, str(payload[field]))
            for field in ("serial", "uniSerial")
            if payload.get(field) is not None
        )

    @staticmethod
    def _extract_uid(payload: Mapping[str, Any]) -> str | None:
        for field in ("gatewayUid", "gatewayUID", "uid"):
            value = payload.get(field)
            if isinstance(value, str) and value:
                return value
        return None

    @staticmethod
    def _response_session_id(payload: Mapping[str, Any]) -> bytes | None:
        value = payload.get("__session_id")
        return value if isinstance(value, bytes) and len(value) == 32 else None

    @staticmethod
    def _decode_session_key(value: Any) -> bytes:
        if isinstance(value, bytes):
            key = value
        elif isinstance(value, str):
            try:
                key = bytes.fromhex(value)
            except ValueError:
                key = value.encode()
        else:
            raise GatewayConnectionError(
                "unsupported session key type",
                reason="session_key_type",
            )
        if len(key) not in (16, 24, 32):
            raise GatewayConnectionError(
                "invalid session key length",
                reason="session_key_length",
            )
        return key

    @staticmethod
    def _hello_payload() -> Payload:
        return {
            "source": SOFTWARE_NAME,
            "softwareVersion": SOFTWARE_VERSION,
            "sysVersion": SYS_VERSION,
            "hardwareVersion": HARDWARE_VERSION,
            "language": LANGUAGE,
            "identifier": hex(int(time.time()))[2:12],
            "phoneName": PHONE_NAME,
            "cmd": CMD_HELLO,
            "serial": _serial(),
            "clientType": 1,
        }
