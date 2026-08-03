"""ORVIBO cloud-region definitions and deterministic endpoint selection."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .const import HTTPS_HOST, HTTPS_HOST_GLOBAL, SSL_HOST, SSL_HOST_GLOBAL


class CloudRegion(str, Enum):
    """Known ORVIBO account partitions."""

    CHINA = "china"
    GLOBAL = "global"


@dataclass(frozen=True, slots=True)
class CloudEndpoint:
    """REST and binary endpoints belonging to one account partition."""

    region: CloudRegion
    api_host: str
    ssl_host: str


CHINA_CLOUD = CloudEndpoint(CloudRegion.CHINA, HTTPS_HOST, SSL_HOST)
GLOBAL_CLOUD = CloudEndpoint(
    CloudRegion.GLOBAL,
    HTTPS_HOST_GLOBAL,
    SSL_HOST_GLOBAL,
)
CLOUD_ENDPOINTS = (CHINA_CLOUD, GLOBAL_CLOUD)


def cloud_for_region(value: object) -> CloudEndpoint:
    """Return a known endpoint, defaulting legacy/empty values to China."""
    normalized = str(value or "").strip().lower()
    for endpoint in CLOUD_ENDPOINTS:
        if normalized == endpoint.region.value:
            return endpoint
    return CHINA_CLOUD


def cloud_for_api_host(host: str) -> CloudEndpoint:
    """Resolve a supported API host without accepting arbitrary destinations."""
    normalized = str(host or "").strip().lower()
    for endpoint in CLOUD_ENDPOINTS:
        if normalized == endpoint.api_host.lower():
            return endpoint
    raise ValueError(f"unsupported ORVIBO cloud host: {host!r}")


def cloud_candidates(preferred: CloudEndpoint | None = None) -> tuple[CloudEndpoint, ...]:
    """Return the preferred partition first, followed by the other known one."""
    first = preferred or CHINA_CLOUD
    return (first,) + tuple(item for item in CLOUD_ENDPOINTS if item != first)
