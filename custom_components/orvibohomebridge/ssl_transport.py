"""Mutual-TLS stream transport for the ORVIBO binary protocol."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import ssl
from typing import Any


@dataclass(frozen=True, slots=True)
class TlsFiles:
    """Certificate paths required by the ORVIBO mutual-TLS endpoint."""

    certificate: Path
    private_key: Path
    server_ca: Path


class SSLTransport:
    """Own the TLS context and stream lifecycle, without protocol knowledge."""

    def __init__(
        self,
        hass: Any,
        host: str,
        port: int,
        tls_files: TlsFiles,
        *,
        connect_timeout: float = 10.0,
        close_timeout: float = 2.0,
    ) -> None:
        self.hass = hass
        self.host = host
        self.port = port
        self.tls_files = tls_files
        self.connect_timeout = connect_timeout
        self.close_timeout = close_timeout
        self.ssl_context: ssl.SSLContext | None = None
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.connected = False

    def _build_ssl_context(self) -> ssl.SSLContext:
        for path in (
            self.tls_files.certificate,
            self.tls_files.private_key,
            self.tls_files.server_ca,
        ):
            if not path.is_file():
                raise FileNotFoundError(f"找不到证书文件: {path}")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_cert_chain(
            certfile=self.tls_files.certificate,
            keyfile=self.tls_files.private_key,
        )
        context.load_verify_locations(cafile=self.tls_files.server_ca)
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        return context

    async def create_ssl_context(self) -> ssl.SSLContext:
        """Load certificate material outside Home Assistant's event loop."""
        return await self.hass.async_add_executor_job(self._build_ssl_context)

    async def connect(self) -> bool:
        if self.connected:
            return True
        if self.ssl_context is None:
            self.ssl_context = await self.create_ssl_context()
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(
                host=self.host,
                port=self.port,
                ssl=self.ssl_context,
                server_hostname=self.host,
            ),
            timeout=self.connect_timeout,
        )
        self.connected = True
        return True

    async def close(self) -> None:
        writer = self.writer
        try:
            if writer is not None and not writer.is_closing():
                writer.close()
                try:
                    await asyncio.wait_for(
                        writer.wait_closed(),
                        timeout=self.close_timeout,
                    )
                except (asyncio.TimeoutError, OSError):
                    pass
        finally:
            self.reader = None
            self.writer = None
            self.connected = False

    async def write(self, data: bytes) -> None:
        writer = self.writer
        if writer is None or writer.is_closing():
            raise ConnectionError("SSL stream is not connected")
        writer.write(data)
        await writer.drain()

    async def readexactly(self, size: int) -> bytes:
        reader = self.reader
        if reader is None:
            raise ConnectionError("SSL stream is not connected")
        return await reader.readexactly(size)
