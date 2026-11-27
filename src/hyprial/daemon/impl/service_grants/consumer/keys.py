# Decision notes (service grant S5, 2026-11-26):
# - The opened Key becomes one user-provider entry, under a name derived from
#   the grant id alone. A user entry that already owns that name is never
#   overwritten.
# - S5 fix (2026-11-27, critic 1): the first write goes through identity's
#   create-only entry, which checks and writes under the record's own write
#   lock. The lstat beforehand only saves opening a credential for nothing;
#   an entry that appears after it is refused by identity ("entry-exists"),
#   never merged over.
# - S5 fix (2026-11-27, critic 5): each ledger row also records a digest of
#   the credential ciphertext (never of the plaintext). When the grantor
#   replaces the ciphertext under the same (org, grant id), the credential is
#   opened again and, while the entry still holds the Key this slice wrote,
#   its KEY field is replaced through identity's ordinary writer (one atomic
#   file replace under the record's write lock; a configured entry keeps its
#   routes). An entry whose Key the user replaced is released and left alone,
#   as before. A replacement that cannot be opened keeps the entry and the
#   row as they are, reports the fixed reason, and is tried again next tick,
#   as a first write that cannot be opened already is. The ownership check
#   runs right before the write and outside identity's lock; the window
#   between them is what remains of the race for a replaced credential.
#   A ledger row without the credential digest is not read (no migration:
#   nothing of this slice has shipped), which fails towards keeping files.
# - Ownership is proved by a private ledger, not by the name: one row per
#   (org, grant id) naming the entry id and the digest of the Key value this
#   slice wrote into it. The proof is about the Key, not about the file: the
#   recipient is expected to rewrite the entry (binding it to a model
#   connection adds route metadata beside the value), and a granted Key must
#   still be taken back after that. An entry is deleted only when the ledger
#   claims it for the withdrawn grant AND its KEY field still holds the value
#   this slice wrote. An entry whose Key the user replaced, or one this slice
#   never wrote, is left alone.
# - The ledger is the only thing this slice persists, and it holds no secret:
#   a digest and identifiers. Losing it means orphaned entries are no longer
#   claimed, which fails towards keeping a user's file.
# - A steady tick only stats the entries (one lstat each): nothing is read,
#   rewritten or opened again while size and mtime are the recorded ones. A
#   changed stat is settled by reading the Key value once and comparing
#   digests. A missing entry is restored; one holding another Key stops being
#   managed.
# - The credential is opened only when can_open_grant_credential says so,
#   with the grantor from the document path, and only after two probes that
#   create nothing: this device's key record is there, and the key store's
#   lock is free (the periodic path never waits on the store's own 15 s lock).
#   Without a key record the store is never entered, so refusing a grant
#   leaves no directory and no lock file behind. No key is ever created here.
# - Plaintext never leaves these functions in a message: every failure carries
#   one of two fixed reasons, never the text of the underlying error.
#   "key-unavailable" means this device cannot open anything (no committed
#   account, no key, unusable or busy key store). "credential-unopenable"
#   means this device is fine but the credential is not sealed to it as this
#   exact grant: the store rebuilds the header from the grant fields and
#   refuses an envelope whose own header differs, which is what a copied or
#   mis-addressed ciphertext always is.
"""Write, keep and delete the user-provider entry of a granted Key."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hyprial.identity import (
    EncryptionKey,
    IdentityRecordError,
    SecretResolutionError,
    SecretResolver,
    create_user_provider,
    delete_user_provider,
    machine_fingerprint,
    read_onboarding_subject,
    read_user_provider_value,
)
from hyprial.kernel import DaemonRequestError, FileLockBusy, ProcessFileLock, write_private_file_atomic

from .derive import KEY_FIELD, DerivedGrant, Rejection

LEDGER_RELPATH = "state/service-grants/consumer-ledger.json"
CREDENTIAL_UNOPENABLE = "credential-unopenable"
KEY_UNAVAILABLE = "key-unavailable"
ENTRY_EXISTS = "entry-exists"
ENTRY_MODIFIED = "entry-modified"
PROVIDERS_DIRNAME = Path("secrets") / "providers"
_LOCK_PROBE_SECONDS = 0.25
_LOCK_POLL_SECONDS = 0.02


def entry_id_for_grant(grant_id: str) -> str:
    """The one entry name a grant id determines: ``grant.<id without its prefix>``."""

    return "grant." + grant_id.removeprefix("sg.")


def entry_path(home: Path, entry_id: str) -> Path:
    return home / PROVIDERS_DIRNAME / f"{entry_id}.json"


@dataclass(frozen=True, slots=True)
class KeyRow:
    """The ledger's proof that one entry holds the Key this slice wrote for one grant.

    ``digest`` is the SHA-256 of the Key value as written and
    ``credential_digest`` the SHA-256 of the ciphertext it was opened from;
    ``size`` and ``mtime_ns`` describe the entry file right after the write,
    so a steady tick can tell an untouched entry by one lstat.
    """

    org: str
    grant_id: str
    grantor: str
    entry_id: str
    digest: str
    credential_digest: str
    size: int
    mtime_ns: int

    @property
    def key(self) -> tuple[str, str]:
        return (self.org, self.grant_id)

    def record(self) -> dict[str, Any]:
        return {
            "org": self.org,
            "grantId": self.grant_id,
            "grantor": self.grantor,
            "entryId": self.entry_id,
            "digest": self.digest,
            "credentialDigest": self.credential_digest,
            "size": self.size,
            "mtimeNs": self.mtime_ns,
        }


_ROW_TEXT = ("org", "grantId", "grantor", "entryId", "digest", "credentialDigest")
_ROW_INTS = ("size", "mtimeNs")


def _row_from_record(value: Any) -> KeyRow | None:
    if not isinstance(value, dict) or set(value) != set(_ROW_TEXT + _ROW_INTS):
        return None
    if not all(isinstance(value[name], str) for name in _ROW_TEXT):
        return None
    if not all(type(value[name]) is int for name in _ROW_INTS):
        return None
    return KeyRow(*(value[name] for name in _ROW_TEXT + _ROW_INTS))


class Ledger:
    """What this slice wrote, and which derived connections are alive right now."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.rows: dict[tuple[str, str], KeyRow] = {}
        self.connections: set[str] = set()
        self._saved: str | None = None
        self._load()

    def _load(self) -> None:
        try:
            value = json.loads(self.path.read_bytes().decode("utf-8"))
            if value.get("schemaVersion") != 1 or not isinstance(value.get("entries"), list):
                return
            if not isinstance(value.get("connections"), list):
                return
        except (OSError, ValueError, TypeError, AttributeError, RecursionError):
            return
        for item in value["entries"]:
            row = _row_from_record(item)
            if row is not None:
                self.rows[row.key] = row
        self.connections = {name for name in value["connections"] if isinstance(name, str)}
        self._saved = self._serialize()

    def _serialize(self) -> str:
        rows = sorted(self.rows.values(), key=lambda row: row.key)
        return json.dumps(
            {
                "schemaVersion": 1,
                "entries": [row.record() for row in rows],
                "connections": sorted(self.connections),
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def save(self) -> bool:
        """Write the ledger when it changed; ``False`` when the write failed."""

        text = self._serialize()
        if text == self._saved:
            return True
        try:
            write_private_file_atomic(self.path, (text + "\n").encode("utf-8"))
        except Exception:
            return False
        self._saved = text
        return True

    def claim(self, row: KeyRow) -> None:
        self.rows[row.key] = row

    def row_of(self, grant: DerivedGrant) -> KeyRow | None:
        return self.rows.get((grant.org, grant.grant_id))

    def release(self, key: tuple[str, str]) -> KeyRow | None:
        return self.rows.pop(key, None)


class KeyUnavailable(Exception):
    """The credential could not be opened; ``reason`` is fixed and carries no material."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _key_missing() -> KeyUnavailable:
    return KeyUnavailable(KEY_UNAVAILABLE)


def default_opener(
    stored: Any, *, home: Path, state_dir: Path, owner: str, device_id: str
) -> bytes:
    """Open one granted credential with this device's key; never create a key store."""

    try:
        subject = read_onboarding_subject(home)
    except IdentityRecordError:
        raise _key_missing() from None
    except (OSError, ValueError, TypeError, AttributeError):
        raise _key_missing() from None
    if subject is None or not subject.device_uid or not subject.user_id:
        raise _key_missing()
    try:
        store = EncryptionKey.Store(
            state_dir=state_dir,
            kind="user",
            casdoor_sub=subject.user_id,
            device_uid=subject.device_uid,
            fingerprint=machine_fingerprint(),
        )
        if not _key_record_present(store.root / "key.json"):
            raise _key_missing()
        if not _lock_is_free(store.root / ".lock"):
            raise _key_missing()
        service = stored.grant.service
        assert service is not None
        opened = store.open_service_grant(
            dict(stored.grant.credential),
            org=stored.org,
            grant_id=stored.grant.grant_id,
            grantor=stored.grantor,
            service_device=service.device_id,
            port=service.port,
            recipient_owner=owner,
            recipient_device=device_id,
        )
    except DaemonRequestError as error:
        raise KeyUnavailable(
            KEY_UNAVAILABLE if str(error.code).startswith("USER_KEY_") else CREDENTIAL_UNOPENABLE
        ) from None
    except KeyUnavailable:
        raise
    except Exception:
        raise _key_missing() from None
    return opened


def _key_record_present(path: Path) -> bool:
    """Whether a key record is there to open with; ``True`` when that cannot be told.

    Entering the store's own lock creates its directory and lock file, so a
    device that has no key must be answered before that. Any doubt (an
    unreadable path, a directory) goes on to the store, which owns the verdict.
    """

    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _lock_is_free(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode):
        return True
    probe = ProcessFileLock(path)
    try:
        probe.acquire(timeout=_LOCK_PROBE_SECONDS, poll_interval=_LOCK_POLL_SECONDS)
    except (FileLockBusy, OSError):
        return False
    probe.release()
    return True


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def credential_digest(grant: DerivedGrant) -> str:
    """The SHA-256 of the grant's ciphertext as canonical JSON; it never sees a plaintext."""

    try:
        text = json.dumps(grant.stored.grant.credential, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):
        return ""
    return _digest(text)


def _key_still_ours(row: KeyRow, home: Path) -> bool:
    """Whether the entry still holds the Key this slice wrote, read through identity's rules."""

    try:
        return _digest(read_user_provider_value(home, row.entry_id, KEY_FIELD)) == row.digest
    except Exception:
        return False


@dataclass(frozen=True, slots=True)
class EntryState:
    """What the file at one entry path is, as far as a single lstat can tell."""

    info: os.stat_result | None
    occupied: bool

    @property
    def missing(self) -> bool:
        return self.info is None and not self.occupied


def _inspect(path: Path) -> EntryState:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return EntryState(None, False)
    except OSError:
        return EntryState(None, True)
    return EntryState(info if stat.S_ISREG(info.st_mode) else None, True)


@dataclass(frozen=True, slots=True)
class KeyOutcome:
    """What one credential-bearing grant did to its entry this tick.

    ``stored`` says the entry is on disk and claimed by the ledger;
    ``written`` says this tick is the one that put it there.
    """

    entry_id: str
    stored: bool
    written: bool = False
    rejection: Rejection | None = None

    @classmethod
    def refused(cls, grant: DerivedGrant, reason: str) -> KeyOutcome:
        return cls(
            entry_id_for_grant(grant.grant_id), False, False,
            Rejection(grant.org, grant.grant_id, grant.grantor, reason),
        )


def sync_entry(
    grant: DerivedGrant,
    ledger: Ledger,
    opener: Callable[[Any], bytes],
    home: Path,
) -> KeyOutcome:
    """Bring the entry of one effective, credential-bearing grant in line with the grant."""

    entry_id = entry_id_for_grant(grant.grant_id)
    path = entry_path(home, entry_id)
    row = ledger.row_of(grant)
    state = _inspect(path)
    if row is not None:
        if state.info is not None:
            if not _unchanged(row, state.info) and not _key_still_ours(row, home):
                ledger.release(row.key)
                return KeyOutcome.refused(grant, ENTRY_MODIFIED)
            if not _unchanged(row, state.info):
                row = dataclasses.replace(row, size=state.info.st_size, mtime_ns=state.info.st_mtime_ns)
                ledger.claim(row)
            if row.credential_digest != credential_digest(grant):
                return _rotate(grant, ledger, opener, home, row)
            return KeyOutcome(entry_id, True)
        ledger.release(row.key)
    elif not state.missing:
        return KeyOutcome.refused(grant, ENTRY_EXISTS)
    return _restore(grant, ledger, opener, home, path, entry_id)


def _unchanged(row: KeyRow, info: os.stat_result) -> bool:
    return info.st_size == row.size and info.st_mtime_ns == row.mtime_ns


def _open(grant: DerivedGrant, opener: Callable[[Any], bytes]) -> str:
    """The plaintext Key; failures raise :class:`KeyUnavailable` with a fixed reason only."""

    try:
        return opener(grant.stored).decode("utf-8")
    except KeyUnavailable:
        raise
    except Exception:
        raise KeyUnavailable(CREDENTIAL_UNOPENABLE) from None


def _claim(ledger: Ledger, grant: DerivedGrant, entry_id: str, text: str, info: os.stat_result) -> None:
    ledger.claim(
        KeyRow(
            grant.org, grant.grant_id, grant.grantor, entry_id, _digest(text),
            credential_digest(grant), info.st_size, info.st_mtime_ns,
        )
    )


def _rotate(
    grant: DerivedGrant,
    ledger: Ledger,
    opener: Callable[[Any], bytes],
    home: Path,
    row: KeyRow,
) -> KeyOutcome:
    """The ciphertext changed: open it again and replace the Key of an entry that is still ours."""

    try:
        text = _open(grant, opener)
    except KeyUnavailable as error:
        return KeyOutcome(row.entry_id, True, False, Rejection(grant.org, grant.grant_id, grant.grantor, error.reason))
    if not _key_still_ours(row, home):
        ledger.release(row.key)
        return KeyOutcome.refused(grant, ENTRY_MODIFIED)
    try:
        SecretResolver(home, None).write_user_provider(row.entry_id, {KEY_FIELD: text})  # type: ignore[arg-type]
        info = entry_path(home, row.entry_id).lstat()
    except Exception:
        return KeyOutcome(row.entry_id, True, False, Rejection(grant.org, grant.grant_id, grant.grantor, KEY_UNAVAILABLE))
    _claim(ledger, grant, row.entry_id, text, info)
    return KeyOutcome(row.entry_id, True, True)


def _restore(
    grant: DerivedGrant,
    ledger: Ledger,
    opener: Callable[[Any], bytes],
    home: Path,
    path: Path,
    entry_id: str,
) -> KeyOutcome:
    try:
        text = _open(grant, opener)
    except KeyUnavailable as error:
        return KeyOutcome.refused(grant, error.reason)
    try:
        create_user_provider(home, entry_id, {KEY_FIELD: text})
        info = path.lstat()
    except SecretResolutionError as error:
        return KeyOutcome.refused(grant, ENTRY_EXISTS if error.category == "exists" else KEY_UNAVAILABLE)
    except Exception:
        return KeyOutcome.refused(grant, KEY_UNAVAILABLE)
    _claim(ledger, grant, entry_id, text, info)
    return KeyOutcome(entry_id, True, True)


def withdraw_entry(row: KeyRow, home: Path) -> str:
    """Delete the entry a withdrawn grant caused; ``kept`` unless it still holds our Key."""

    if not _key_still_ours(row, home):
        return "kept"
    try:
        delete_user_provider(home, row.entry_id)
    except Exception:
        return "kept"
    return "deleted"


__all__ = [
    "CREDENTIAL_UNOPENABLE",
    "ENTRY_EXISTS",
    "ENTRY_MODIFIED",
    "KEY_UNAVAILABLE",
    "LEDGER_RELPATH",
    "credential_digest",
    "EntryState",
    "KeyOutcome",
    "KeyRow",
    "KeyUnavailable",
    "Ledger",
    "default_opener",
    "entry_id_for_grant",
    "entry_path",
    "sync_entry",
    "withdraw_entry",
]
