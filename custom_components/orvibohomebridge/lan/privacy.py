"""Privacy-safe formatting helpers for logs and diagnostics."""

from __future__ import annotations

import ipaddress


def mask_identifier(value: object, *, visible: int = 4) -> str:
    """Keep a small stable suffix without exposing a full identifier."""

    text = str(value).strip()
    if not text or len(text) <= visible:
        return "***"
    return f"***{text[-visible:]}"


def mask_host(value: object) -> str:
    """Mask a network address while retaining enough context for debugging."""

    text = str(value).strip()
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return mask_identifier(text)
    if address.version == 4:
        octets = text.split(".")
        return ".".join((*octets[:3], "*"))
    hextets = address.exploded.split(":")
    return f"{hextets[0]}:{hextets[1]}:*"
