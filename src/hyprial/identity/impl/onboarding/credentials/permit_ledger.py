# Decision record (W2A-LOGIN-INPROC, 2026-11-21):
# - Directory durability after ledger atomic_json_write delegates to
#   hyprial.kernel.fsync_directory so Windows (where opening a directory
#   read-only raises PermissionError and NTFS journals the rename metadata)
#   does not abort permit consumption, while real POSIX I/O or missing-directory
#   errors continue to fail closed with PERMIT_LEDGER_UNAVAILABLE.
"""Durable one-time-use proof for identity commit permits.

Decision: a ``CommitPermit`` authorizes exactly one identity commit, so a
second credential service instance must not replay the same permit for a
same-owner reauth (where the settings snapshot still matches) and rewrite the
credential.  The commit stage therefore records a SHA-256
fingerprint of the canonical permit fields in a home-root ledger, under the
identity transaction lock the caller already holds, before the credential is
written.  The ledger stores only the fingerprint and the permit expiry; it
never stores a nonce, token, owner, or credential copy, and the credential and
settings schemas are unchanged.

Ordering and failure: validate the snapshot and owner, recheck expiry, consume
the permit, write the credential, then write settings.  A consumption failure
means no credential was written.  Once consumption succeeds the permit is
burned even if a later write fails, so a retry needs a new permit; this is the
intended fail-closed direction for a one-time proof.

Durability: the kernel writer fsyncs its temporary file and replaces the
ledger atomically, and consumption flushes the ledger's parent directory
through ``hyprial.kernel.fsync_directory`` before returning to the credential
write.  Any real directory-sync I/O failure fails closed with
``PERMIT_LEDGER_UNAVAILABLE`` before any credential write.
The already-replaced ledger is left in place in that case, so the permit stays
burned and an automatic retry is refused as a replay.  The ledger is pruned by
expiry on each use.  An unreadable, corrupt, symlinked, or non-regular ledger
fails closed instead of being treated as empty.  This closes the credential
service replay path; it is not a cryptographic defense against a
same-UID process that can already rewrite the home.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

from hyprial.kernel import atomic_json_write, fsync_directory

from ..ports import CommitPermit, IdentityError

PERMIT_LEDGER_FILENAME = ".identity-permit-uses.json"
PERMIT_LEDGER_VERSION = 1
_HEX_DIGITS = frozenset("0123456789abcdef")


def permit_ledger_path(home: Path) -> Path:
    """Return the single ledger location for one explicit home."""

    return Path(home) / PERMIT_LEDGER_FILENAME


def permit_fingerprint(permit: CommitPermit) -> str:
    """Return the canonical, secret-free fingerprint of one commit permit."""

    payload = {
        "operationId": permit.operation_id,
        "nonce": permit.nonce,
        "home": str(Path(permit.home).resolve()),
        "issuer": permit.issuer,
        "clientId": permit.client_id,
        "expectedOwner": permit.expected_identity.owner,
        "expectedMode": permit.expected_identity.mode,
        "expectedIssuer": permit.expected_identity.issuer,
        "expectedUserId": permit.expected_identity.user_id,
        "homeBinding": (
            permit.home_binding.as_dict() if permit.home_binding is not None else None
        ),
        "legacyBusinessData": permit.legacy_business_data,
        "expiresAt": permit.expires_at,
        "bootstrap": permit.bootstrap,
    }
    canonical = json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _invalid(message: str) -> IdentityError:
    return IdentityError("PERMIT_LEDGER_INVALID", message)


def _unavailable(message: str, error: OSError) -> IdentityError:
    return IdentityError("PERMIT_LEDGER_UNAVAILABLE", f"{message}: {error}")


def _fsync_directory(path: Path) -> None:
    """Flush directory entries where the OS supports directory descriptors."""

    fsync_directory(path)


def _valid_fingerprint(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in _HEX_DIGITS for char in value)
    )


def _read_entries(path: Path) -> list[dict[str, str | int]]:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return []
    except OSError as error:
        raise _unavailable("permit ledger cannot be inspected", error) from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise _invalid("permit ledger is not a regular file; nothing was written")
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise _unavailable("permit ledger cannot be read", error) from error
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _invalid("permit ledger is not valid JSON; nothing was written") from error
    if not isinstance(record, dict) or set(record) != {"version", "permits"}:
        raise _invalid("permit ledger has an unexpected shape; nothing was written")
    version = record["version"]
    if type(version) is not int or version != PERMIT_LEDGER_VERSION:
        raise _invalid("permit ledger version is unsupported; nothing was written")
    permits = record["permits"]
    if not isinstance(permits, list):
        raise _invalid("permit ledger entries are not a list; nothing was written")
    entries: list[dict[str, str | int]] = []
    for entry in permits:
        if not isinstance(entry, dict) or set(entry) != {"fingerprint", "expiresAt"}:
            raise _invalid(
                "permit ledger entry has an unexpected shape; nothing was written"
            )
        fingerprint = entry["fingerprint"]
        expires_at = entry["expiresAt"]
        if not _valid_fingerprint(fingerprint):
            raise _invalid(
                "permit ledger fingerprint is invalid; nothing was written"
            )
        if type(expires_at) is not int:
            raise _invalid("permit ledger expiry is invalid; nothing was written")
        entries.append({"fingerprint": fingerprint, "expiresAt": expires_at})
    return entries


def consume_permit(permit: CommitPermit, *, home: Path, now_epoch: int) -> None:
    """Record one permit use before the credential write, or fail closed.

    Callers must hold the identity transaction lock for ``home``; the worker
    holds it from begin through commit.  A matching unexpired fingerprint is a
    replay and is refused.  Expired entries are pruned.  The ledger is replaced
    atomically at mode 0600 and its directory is synced before this function
    returns; any inspection, parse, write, or durability-confirmation failure
    aborts the commit before any credential is written.  A failure after the
    replace keeps the landed ledger, so the permit remains burned.
    """

    path = permit_ledger_path(home)
    fingerprint = permit_fingerprint(permit)
    kept = [
        entry
        for entry in _read_entries(path)
        if int(entry["expiresAt"]) > now_epoch
    ]
    if any(entry["fingerprint"] == fingerprint for entry in kept):
        raise IdentityError(
            "PERMIT_REPLAYED",
            "commit permit was already consumed; nothing was written",
        )
    kept.append({"fingerprint": fingerprint, "expiresAt": permit.expires_at})
    try:
        atomic_json_write(
            path, {"version": PERMIT_LEDGER_VERSION, "permits": kept}
        )
    except OSError as error:
        raise _unavailable(
            "permit ledger could not be updated; nothing was written", error
        ) from error
    try:
        _fsync_directory(path.parent)
    except OSError as error:
        raise _unavailable(
            "permit ledger directory durability could not be confirmed; no "
            "credential was written",
            error,
        ) from error


__all__ = [
    "PERMIT_LEDGER_FILENAME",
    "PERMIT_LEDGER_VERSION",
    "consume_permit",
    "permit_fingerprint",
    "permit_ledger_path",
]
