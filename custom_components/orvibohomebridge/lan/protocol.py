"""Strict Orvibo packet codec compatible with the legacy packet builder."""

from __future__ import annotations

import binascii
import json
import struct
from collections.abc import Mapping
from typing import Any

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .exceptions import (
    EncryptionError,
    InvalidCrcError,
    InvalidLengthError,
    InvalidMagicError,
    InvalidPacketTypeError,
    InvalidPayloadError,
    InvalidSessionError,
)
from .models import DecodedPacket, immutable_value

MAGIC = b"hd"
PK_TYPE = b"pk"
DK_TYPE = b"dk"
DEFAULT_KEY = b"khggd54865SNJHGF"
HEADER_LENGTH = 42
SESSION_LENGTH = 32
MAX_PACKET_LENGTH = 0xFFFF
_PACKET_TYPES = frozenset((PK_TYPE, DK_TYPE))
_AES_KEY_LENGTHS = frozenset((16, 24, 32))


def _validate_packet_type(packet_type: bytes) -> None:
    if not isinstance(packet_type, bytes) or packet_type not in _PACKET_TYPES:
        raise InvalidPacketTypeError("packet_type must be b'pk' or b'dk'")


def _validate_key(key: bytes) -> None:
    if not isinstance(key, bytes) or len(key) not in _AES_KEY_LENGTHS:
        raise EncryptionError("AES key must contain16,24, or32 bytes")


def _validate_session_id(session_id: bytes) -> None:
    if not isinstance(session_id, bytes) or len(session_id) != SESSION_LENGTH:
        raise InvalidSessionError("session_id must contain exactly32 bytes")


def _crc32(data: bytes) -> bytes:
    return struct.pack(">I", binascii.crc32(data) & 0xFFFFFFFF)


def _encrypt(key: bytes, plaintext: bytes) -> bytes:
    """Encrypt without retaining key-dependent Cipher objects."""

    _validate_key(key)
    try:
        padder = padding.PKCS7(128).padder()
        padded = padder.update(plaintext) + padder.finalize()
        encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
        return encryptor.update(padded) + encryptor.finalize()
    except (TypeError, ValueError) as error:
        raise EncryptionError("failed to encrypt packet payload") from error


def _decrypt(key: bytes, ciphertext: bytes) -> bytes:
    _validate_key(key)
    if not ciphertext or len(ciphertext) % 16:
        raise InvalidLengthError("encrypted payload must be non-empty AES blocks")
    try:
        decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        return unpadder.update(padded) + unpadder.finalize()
    except (TypeError, ValueError) as error:
        raise EncryptionError("failed to decrypt packet payload") from error


def build_packet(
    packet_type: bytes,
    key: bytes,
    session_id: bytes,
    payload: dict[str, Any],
) -> bytes:
    """Build a wire-compatible PK or DK packet from a JSON object."""

    _validate_packet_type(packet_type)
    _validate_key(key)
    _validate_session_id(session_id)
    if not isinstance(payload, dict):
        raise InvalidPayloadError("payload must be a dict")
    try:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise InvalidPayloadError("payload must be JSON serializable") from error
    encrypted = _encrypt(key, encoded)
    packet_length = HEADER_LENGTH + len(encrypted)
    if packet_length > MAX_PACKET_LENGTH:
        raise InvalidLengthError("packet exceeds the protocol length limit")
    return b"".join(
        (
            MAGIC,
            struct.pack(">H", packet_length),
            packet_type,
            _crc32(encrypted),
            session_id,
            encrypted,
        )
    )


def decode_packet(
    data: bytes,
    keys: Mapping[bytes, bytes],
    *,
    default_key: bytes = DEFAULT_KEY,
) -> DecodedPacket:
    """Validate and decode a packet, retaining type and session metadata."""

    if not isinstance(data, bytes):
        raise InvalidLengthError("packet must be bytes")
    if len(data) < HEADER_LENGTH + 16:
        raise InvalidLengthError("packet is shorter than one encrypted payload block")
    if data[:2] != MAGIC:
        raise InvalidMagicError("invalid packet magic")
    declared_length = struct.unpack(">H", data[2:4])[0]
    if declared_length != len(data):
        raise InvalidLengthError(
            f"declared packet length {declared_length} does not match {len(data)}"
        )
    packet_type = data[4:6]
    _validate_packet_type(packet_type)
    session_id = data[10:HEADER_LENGTH]
    _validate_session_id(session_id)
    encrypted = data[HEADER_LENGTH:]
    if _crc32(encrypted) != data[6:10]:
        raise InvalidCrcError("encrypted payload CRC does not match")
    key: bytes | None
    if packet_type == PK_TYPE:
        key = default_key
    else:
        key = keys.get(session_id)
    if key is None:
        raise InvalidSessionError("DK packet session is unknown")
    plain = _decrypt(key, encrypted)
    try:
        payload = json.loads(plain.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidPayloadError("decrypted payload is not valid UTF-8 JSON") from error
    if not isinstance(payload, dict):
        raise InvalidPayloadError("decrypted JSON payload must be an object")
    return DecodedPacket(packet_type, session_id, immutable_value(payload))


def parse_packet(
    data: bytes,
    keys: Mapping[bytes, bytes],
    *,
    default_key: bytes = DEFAULT_KEY,
) -> dict[str, Any]:
    """Decode a packet and return a mutable dict for legacy call compatibility."""

    decoded = decode_packet(data, keys, default_key=default_key)
    return _mutable_copy(decoded.payload)


def _mutable_copy(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _mutable_copy(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_mutable_copy(item) for item in value]
    if isinstance(value, frozenset):
        return {_mutable_copy(item) for item in value}
    return value
