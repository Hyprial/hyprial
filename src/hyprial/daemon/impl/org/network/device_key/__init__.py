# Decision record (service grant S1, 2026-11-26):
# - This subpackage is the one place the daemon asks for this device's
#   encryption key.  It is a subpackage because ``org/network`` already holds
#   the seven direct files the structure gate allows.
# - ``encryption_key_reader`` is for a caller that publishes more than once:
#   it is bound to the home and state directory once and read on every call.
#   ``ensure_encryption_key`` is the same read as one call, for serving start.
#   Both answer ``{keyId, publicKey}`` or ``None``.
# - "No key" is an answer, not a failure.  Every missing prerequisite and
#   every refusal of the key store turns into ``None``, so the row is
#   published without the pair and the publish itself never fails.  The
#   daemon must not become unable to register a device because a credential
#   it does not need yet is unavailable.
# - The identity is the same one the login user-key step uses
#   (``read_onboarding_subject``): settings owner, issuer and ``userId``, the
#   device record's ``deviceUid``, and a device that belongs to the settings
#   owner.  A wider rule here would give a device an encryption key where it
#   has no signing key, and the two would disagree about who the device is.
#   It is read on every call, so a rebind of the principal in the running
#   daemon is picked up by the next publish and not by a cached reader.
# - Nothing under ``state_dir`` is touched while the account is incomplete.
#   The reader returns before it builds a store, and the store itself refuses
#   an empty subject, so a home without an account never grows key
#   directories on every refresh of the org directory.
# - ``ensure`` is called on every publish (the directory refresh runs every
#   ten seconds).  It is idempotent and takes the store's own lock, so
#   repeated starts keep one key.  Measured on Linux with the key already in
#   place, one call of the reader took a median of 0.6 ms (p99 0.7 ms, 300
#   calls): ``ensure`` re-reads and re-checks the record under the fence, which
#   is more than one lock and one read.  Because it takes that lock, a refresh
#   waits for any other process holding it, up to the store's own lock timeout.
"""This device's published encryption key: the public half of the local store."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from hyprial.identity import (
    EncryptionKey,
    IdentityRecordError,
    UserKey,
    machine_fingerprint,
    read_device_record,
    read_onboarding_subject,
)
from .pair import published_pair as published_pair

__all__ = ["encryption_key_reader", "ensure_encryption_key", "published_pair"]


def ensure_encryption_key(home: Path, state_dir: Path) -> dict[str, str] | None:
    """Read or create this device's key once and answer its public half.

    This is the serving-start step and takes its two paths positionally, like
    the signing-key step beside it.  ``None`` covers a missing prerequisite
    and a key the store refuses alike; neither is an error for the caller.
    """

    return _read(home, state_dir)


def encryption_key_reader(
    *, home: Path, state_dir: Path
) -> Callable[[], dict[str, str] | None]:
    """Return a callable giving ``{keyId, publicKey}`` for this device, or ``None``.

    The callable is for a caller that publishes more than once, such as the
    directory refresh.  The store is read again on every call, so a key that
    becomes usable later is published then, without a restart.
    """

    return lambda: _read(home, state_dir)


def _read(home: Path, state_dir: Path) -> dict[str, str] | None:
    try:
        subject = read_onboarding_subject(Path(home))
        device = read_device_record(Path(home))
        if subject is None or device is None or device.device_uid is None:
            return None
        store = EncryptionKey.Store(
            state_dir=Path(state_dir),
            kind="user",
            casdoor_sub=subject.user_id,
            device_uid=device.device_uid,
            fingerprint=machine_fingerprint(),
        )
        record = store.ensure()
    except UserKey.Error:
        return None
    except (IdentityRecordError, OSError, ValueError, TypeError, AttributeError):
        return None
    key_id, public_key = record.get("keyId"), record.get("publicKey")
    if not isinstance(key_id, str) or not isinstance(public_key, str):
        return None
    return {"keyId": key_id, "publicKey": public_key}
