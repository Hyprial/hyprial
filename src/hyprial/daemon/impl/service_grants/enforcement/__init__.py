"""Admission enforcement on the exposing device (service grant slice S4-py).

Decision notes (service grant S4-py, 2026-11-26, coordinator ruling on the
design-mismatch evidence):
- This package derives, for each user exposure, the admission set the sidecar
  is to enforce, and pushes it. The set is never stored; every push is a new
  derivation, so a restart or a later maintenance tick reaches the same set.
- The admission key of a granted device comes from the sidecar's own
  membership roster (membership-status), never from the directory device row.
- An org this device has left is covered by the roster check rather than a
  separate filter: once the device is no longer a ready member of that org's
  tailnet, the org's rows are unusable and its grants contribute nothing.
"""

from .derive import derive_admission
from .enforcer import (
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_MEMBERSHIP_TIMEOUT,
    DIRECTORY_UNAVAILABLE,
    KEY_UNAVAILABLE,
    LIMIT_EVENT,
    MAX_KEYS,
    UNEXPOSE_FAILED,
    ServiceAdmissionEnforcer,
)
from .models import (
    ALLOW,
    EVERYONE,
    KEYS,
    NOBODY,
    AdmissionProjection,
    AdmissionSet,
    EnforcementInputs,
    UnresolvedGrant,
)
from .reader import orgs, own_grants
from .roster import (
    KEY_MALFORMED,
    MEMBER_NOT_IN_ROSTER,
    MEMBER_WITHOUT_LOCAL_KEY,
    ROSTER_UNAVAILABLE,
    TAILNET_UNRESOLVED,
    admission_keys,
    member_key,
    tailnet_rows,
)

__all__ = [
    "ALLOW",
    "AdmissionProjection",
    "AdmissionSet",
    "DEFAULT_INTERVAL_SECONDS",
    "DEFAULT_MEMBERSHIP_TIMEOUT",
    "DIRECTORY_UNAVAILABLE",
    "EVERYONE",
    "EnforcementInputs",
    "KEYS",
    "KEY_MALFORMED",
    "KEY_UNAVAILABLE",
    "LIMIT_EVENT",
    "MAX_KEYS",
    "MEMBER_NOT_IN_ROSTER",
    "MEMBER_WITHOUT_LOCAL_KEY",
    "NOBODY",
    "ROSTER_UNAVAILABLE",
    "ServiceAdmissionEnforcer",
    "TAILNET_UNRESOLVED",
    "UNEXPOSE_FAILED",
    "UnresolvedGrant",
    "admission_keys",
    "derive_admission",
    "member_key",
    "orgs",
    "own_grants",
    "tailnet_rows",
]
