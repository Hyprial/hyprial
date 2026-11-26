# Decision notes (service grant S4-py, 2026-11-26, coordinator ruling on the
# design-mismatch evidence):
# - An admission set is derived, never stored on disk: the sidecar holds the
#   gate, and this object is what one derivation produced.
# - UnresolvedGrant is the operator's answer to "granted, but no key yet". It
#   names the grant, the recipient and the reason, and deliberately has no
#   field that could carry a key.
"""Value types of one port's derived admission."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

ALLOW = "allow"
KEYS = "keys"


@dataclass(frozen=True)
class AdmissionSet:
    """What the sidecar is told: admit every member, or exactly ``keys``."""

    admission: str
    keys: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.admission not in (ALLOW, KEYS):
            raise ValueError("admission must be 'allow' or 'keys'")
        if self.admission == ALLOW and self.keys:
            raise ValueError("'allow' carries no keys")
        if len(set(self.keys)) != len(self.keys):
            raise ValueError("keys must be unique")


@dataclass(frozen=True)
class UnresolvedGrant:
    """An effective grant whose recipient key could not be resolved.

    ``recipient`` is ``{"owner", "deviceId"}`` for a grant to one device and
    ``{"recipient": "@members"}`` for a grant to every member, which can be
    unresolved only when the org's roster cannot be read.
    """

    org: str
    grant_id: str
    recipient: Mapping[str, str]
    reason: str


@dataclass(frozen=True)
class AdmissionProjection:
    """One port: the set to push, and the grants that contributed nothing."""

    admission: AdmissionSet
    unresolved: tuple[UnresolvedGrant, ...]


@dataclass(frozen=True)
class EnforcementInputs:
    """What one derivation reads the grants and the roster's tailnet with.

    The daemon hands a fresh value per derivation, the way the service grant
    IPC handler builds its service per call, so a change of owner (a relogin)
    or of the OrgFS runtime is seen by the next tick and not held by a
    long-lived object.
    """

    store: Any
    fs: Any
    home: Any
    owner: str
    device_id: str


NOBODY = AdmissionSet(admission=KEYS, keys=())
EVERYONE = AdmissionSet(admission=ALLOW, keys=())

__all__ = [
    "AdmissionProjection",
    "AdmissionSet",
    "ALLOW",
    "EnforcementInputs",
    "EVERYONE",
    "KEYS",
    "NOBODY",
    "UnresolvedGrant",
]
