"""Abstract consumer seams only. No storage, directory, network or inbox owner."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from .models import OwnerKey, Recipient


@dataclass(frozen=True)
class KeyLookup:
    """Preserve U2's three states; unavailable is never evidence of absence."""
    state: Literal["verified", "absent", "unavailable"]
    key: OwnerKey | None = None


class OwnerKeyPort(Protocol):
    def lookup(self, owner: str, key_id: str) -> KeyLookup:
        """Use a verified, fresh U2 anchor; never trust self-reported public keys."""
        ...


class StoragePort(Protocol):
    def read_envelope(self, owner: str, key_id: str) -> dict | None:
        """Return a candidate document, not proof of authenticity or freshness."""
        ...

    def read_grant(self, agent: str, provider: str) -> dict | None:
        """Return a candidate signed grant; no revision authority is implied."""
        ...


class RecipientPort(Protocol):
    def resolve(self, owner: str) -> tuple[Recipient, ...]:
        """Resolve trusted X1 owner-device keys; host signer delivery is separate."""
        ...
