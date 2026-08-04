"""Domain exceptions for the Orvibo LAN integration."""


class OrviboLanError(Exception):
    """Base exception for integration-owned failures."""


class ProtocolError(OrviboLanError, ValueError):
    """Base exception for malformed or unsupported protocol packets."""


class InvalidPacketTypeError(ProtocolError):
    """Raised when a packet is not PK or DK."""


class InvalidMagicError(ProtocolError):
    """Raised when the packet magic does not match the protocol."""


class InvalidLengthError(ProtocolError):
    """Raised when packet or protocol field lengths are invalid."""


class InvalidCrcError(ProtocolError):
    """Raised when the encrypted payload checksum is invalid."""


class InvalidSessionError(ProtocolError):
    """Raised when a DK packet has no matching session key."""


class InvalidPayloadError(ProtocolError):
    """Raised when the decrypted payload is not a JSON object."""


class EncryptionError(ProtocolError):
    """Raised when AES encryption or decryption fails."""


class StateStoreError(OrviboLanError, ValueError):
    """Base exception for invalid state-store operations."""


class UnknownDeviceError(StateStoreError, KeyError):
    """Raised when an update targets a device outside the whitelist."""


class StaleGenerationError(StateStoreError):
    """Raised when an update uses an older source generation."""
