"""Immutable domain models shared by cloud and binary transports."""

from __future__ import annotations

from dataclasses import dataclass

from .protocol import normalize_password_hash


@dataclass(frozen=True, slots=True)
class AccountCredentials:
    """Replayable ORVIBO account credentials without a plaintext password."""

    username: str
    password_hash: str
    family_id: str = ""

    def __post_init__(self) -> None:
        username = self.username.strip()
        if not username:
            raise ValueError("username must not be empty")
        object.__setattr__(self, "username", username)
        object.__setattr__(
            self,
            "password_hash",
            normalize_password_hash(self.password_hash),
        )
        object.__setattr__(self, "family_id", self.family_id.strip())

