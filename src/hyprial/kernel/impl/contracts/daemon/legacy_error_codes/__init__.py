"""Frozen exception list for historical lowercase OrgFS/daemon error codes.

Decision record (org-members batch 1 fix 2, 2026-11-02)
- MethodSpec and the H-03 HTTP projector accept two spellings: the existing
  uppercase protocol grammar, or an exact member of this frozen set.  The
  uppercase grammar is not relaxed, and a new lowercase code cannot enter by
  matching a pattern; it must be added here and to the contract snapshots with
  a reviewed reason.
- Membership was derived from producers rather than guessed.  It is every
  ``OrgFsError`` code in the published OrgFS registry (including the three
  lowercase values already named by ``ipc_errors``) plus every ``StoreError``
  code that the document, projection and lifecycle paths can re-raise as an
  ``OrgFsError``.  ``ambiguous-space`` is included because the org network
  raises it and the org.list/org.fetch declarations expose it.
- The values stay lowercase on the wire.  This list records compatibility; it
  does not rename or rewrite any shipped code.
- Organization membership registers ``already-exists`` from the OrgFS
  document register path, preserving its exact existing producer spelling.
"""

from __future__ import annotations

import re

from ..ipc_errors import (
    ORGFS_CONTENT_PENDING,
    ORGFS_CROSS_SPACE_URI,
    ORGFS_INVALID_URI,
)

_LEGACY_KEBAB_CODES = (
    "already-exists",
    "ambiguous-path",
    "ambiguous-space",
    "bad-cursor",
    "blob-unavailable",
    "checkout-io",
    "closed",
    ORGFS_CONTENT_PENDING,
    ORGFS_CROSS_SPACE_URI,
    "invalid-argument",
    ORGFS_INVALID_URI,
    "log-key-conflict",
    "no-holder-online",
    "not-a-member",
    "not-owner",
    "out-of-range",
    "pending-meta",
    "projection-behind",
    "protected-path",
    "purged",
    "resource-exhausted",
    "snapshot-barrier",
    "snapshot-unavailable",
    "space-purge-incomplete",
    "space-purge-refused",
    "stale-plan",
    "stale-write",
    "sync-peer-incompatible",
    "too-large",
    "unavailable",
    "unknown-blob",
    "unknown-doc",
    "unknown-space",
    "unsafe-space-path",
)

#: The complete historical lowercase-kebab exception set.  Frozen by test;
#: later additions need an explicit snapshot/contract change and review.
LEGACY_KEBAB_CODES: frozenset[str] = frozenset(_LEGACY_KEBAB_CODES)

_UPPER_CODE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def is_declared_error_code(code: str) -> bool:
    """Whether ``code`` is an uppercase protocol code or a frozen legacy code."""

    if not isinstance(code, str):
        return False
    return bool(_UPPER_CODE.fullmatch(code)) or code in LEGACY_KEBAB_CODES


__all__ = ["LEGACY_KEBAB_CODES", "is_declared_error_code"]
