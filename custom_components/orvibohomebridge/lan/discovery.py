"""Untrusted UDP candidate discovery for Orvibo gateways."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass

from .exceptions import ProtocolError
from .packet import ID_UNSET, SOFTWARE_VER, UDP_BROADCAST, UDP_PORT
from .protocol import DEFAULT_KEY, PK_TYPE, build_packet, parse_packet
from .serial import next_serial


@dataclass(frozen=True, slots=True)
class DiscoveryCandidate:
    """A filtered but not yet trusted gateway address."""

    uid: str
    host: str


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, known_uids: frozenset[str]) -> None:
        self.known_uids = known_uids
        self.candidates: dict[str, DiscoveryCandidate] = {}

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        host = addr[0]
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return
        if (
            address.version != 4
            or not address.is_private
            or address.is_loopback
            or address.is_unspecified
            or address.is_multicast
        ):
            return
        try:
            payload = parse_packet(data, {ID_UNSET: DEFAULT_KEY})
        except ProtocolError:
            return
        uid = payload.get("uid")
        if not isinstance(uid, str) or uid not in self.known_uids:
            return
        self.candidates[uid] = DiscoveryCandidate(uid, host)


class GatewayDiscovery:
    """Broadcast discovery requests and return untrusted candidates only."""

    def __init__(
        self,
        *,
        port: int = UDP_PORT,
        broadcast: str = UDP_BROADCAST,
        timeout: float = 2.0,
    ) -> None:
        self.port = port
        self.broadcast = broadcast
        self.timeout = timeout

    async def discover(
        self, known_uids: set[str] | frozenset[str]
    ) -> dict[str, DiscoveryCandidate]:
        if not known_uids:
            return {}
        loop = asyncio.get_running_loop()
        protocol = _DiscoveryProtocol(frozenset(known_uids))
        transport, _ = await loop.create_datagram_endpoint(
            lambda: protocol,
            family=socket.AF_INET,
            local_addr=("0.0.0.0", 0),
            allow_broadcast=True,
        )
        try:
            transport.sendto(self._request_packet(), (self.broadcast, self.port))
            await asyncio.sleep(self.timeout)
            return dict(protocol.candidates)
        finally:
            transport.close()

    @staticmethod
    def _request_packet() -> bytes:
        serial = next_serial()
        return build_packet(
            PK_TYPE,
            DEFAULT_KEY,
            ID_UNSET,
            {
                "cmd": 86,
                "serial": serial,
                "uniSerial": serial,
                "clientType": 1,
                "serverRecord": False,
                "ver": SOFTWARE_VER,
            },
        )
