# Decision record (service grant S1, 2026-11-26):
# - This subpackage is the one place the daemon asks for this device's
#   encryption key.  It is a subpackage because ``org/network`` already holds
#   the seven direct files the structure gate allows.
# - ``ensure_encryption_key`` is the serving-start and relogin entry; it calls
#   ``store.ensure()`` directly without the periodic cache or short pre-probe,
#   and returns ``{keyId, publicKey}`` or ``None``.
# - ``encryption_key_reader`` is for directory publication (``ipc._build_service``
#   and ``publish_self_for_app``).  It distinguishes three outcomes:
#   * ``{keyId, publicKey}`` — a usable key for the current committed subject;
#   * ``None`` — definitely no key (missing prerequisites, invalid state path,
#     unsupported platform, or revoked/unbound key);
#   * ``KEY_UNAVAILABLE`` — temporarily unreadable (busy store lock, transient
#     I/O, or store quarantine refusal that shares ``USER_KEY_QUARANTINED`` with
#     lock-busy and I/O errors in ``user_key/store.py``).
# - The identity is read once via ``read_onboarding_subject(home)``, using
#   ``subject.user_id`` and ``subject.device_uid`` directly without a second
#   read of ``device.json``.  ``read_onboarding_subject`` already verifies that
#   ``device.owner == settings.owner`` in one consistent read, preventing a
#   chimeric key across a concurrent reprovision.
# - Nothing under ``state_dir`` is touched while the account is incomplete.
# - Steady-state periodic refreshes serve ``{keyId, publicKey}`` from an
#   in-memory ``EncryptionKeyCache`` keyed by ``(user_id, device_uid)``,
#   invalidated on subject change, so ``store.ensure()`` runs only at serving
#   start, relogin, or on a cache miss.  On a cache miss in the periodic path,
#   an existing regular ``.lock`` file is probed with a short timeout before
#   calling ``store.ensure()``; timeout returns ``KEY_UNAVAILABLE`` instead of
#   blocking the daemon-runtime lane for the store's 15 s lock timeout.
"""This device's published encryption key: the public half of the local store."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path

from hyprial.identity import (
    EncryptionKey,
    IdentityRecordError,
    UserKey,
    machine_fingerprint,
    read_onboarding_subject,
)
from hyprial.kernel import FileLockBusy, ProcessFileLock, ipc_errors

from .cache import EncryptionKeyCache as EncryptionKeyCache
from .pair import KEY_UNAVAILABLE as KEY_UNAVAILABLE
from .pair import published_pair as published_pair

__all__ = [
    "EncryptionKeyCache",
    "KEY_UNAVAILABLE",
    "encryption_key_reader",
    "ensure_encryption_key",
    "published_pair",
]

_PERIODIC_LOCK_TIMEOUT_SECONDS = 0.25
_LOCK_POLL_INTERVAL_SECONDS = 0.02
_DEFINITELY_NO_KEY_CODES = frozenset(
    {
        ipc_errors.PAYLOAD_INVALID,
        ipc_errors.USER_KEY_ABSENT,
        ipc_errors.USER_KEY_UNBOUND,
    }
)


def ensure_encryption_key(home: Path, state_dir: Path) -> dict[str, str] | None:
    """Read or create this device's key once and answer its public half.

    This is the serving-start and relogin step and takes its two paths
    positionally, like the signing-key step beside it.  ``None`` covers a
    missing prerequisite or store refusal alike; neither fails recovery.
    """

    result = _read_uncached(Path(home), Path(state_dir), lock_timeout=None)
    return result if isinstance(result, dict) else None


def encryption_key_reader(
    *,
    home: Path,
    state_dir: Path,
    cache: EncryptionKeyCache | None = None,
    lock_timeout: float | None = _PERIODIC_LOCK_TIMEOUT_SECONDS,
) -> Callable[[], dict[str, str] | object | None]:
    """Return a cached, bounded reader giving ``{keyId, publicKey}``, ``None``, or ``KEY_UNAVAILABLE``.

    A cache hit returns the public pair without entering ``store.ensure()`` or
    the store's file lock.  A change in ``(user_id, device_uid)`` invalidates
    the cache; a busy store lock on a cache miss returns ``KEY_UNAVAILABLE``
    within ``lock_timeout`` seconds instead of blocking on the 15 s store lock.
    """

    home_path = Path(home)
    state_path = Path(state_dir)
    active_cache = cache if cache is not None else EncryptionKeyCache()

    def _resolve() -> dict[str, str] | object | None:
        return active_cache.read_in_pass(
            lambda: _read_cached(
                home_path,
                state_path,
                active_cache,
                lock_timeout=lock_timeout,
            )
        )

    return _resolve


def _read_subject(home: Path) -> tuple[str, str] | object | None:
    try:
        subject = read_onboarding_subject(home)
    except IdentityRecordError as error:
        if error.code == "IDENTITY_SETTINGS_UNREADABLE":
            return KEY_UNAVAILABLE
        return None
    except OSError:
        return KEY_UNAVAILABLE
    except (ValueError, TypeError, AttributeError):
        return None
    if subject is None or not subject.device_uid:
        return None
    return (subject.user_id, subject.device_uid)


def _check_store_lock(lock_path: Path, *, timeout: float) -> bool:
    try:
        info = os.lstat(lock_path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode):
        return True
    probe = ProcessFileLock(lock_path)
    try:
        probe.acquire(
            timeout=timeout,
            poll_interval=min(_LOCK_POLL_INTERVAL_SECONDS, max(0.001, timeout)),
        )
    except (FileLockBusy, OSError):
        return False
    probe.release()
    return True


def _ensure_for_subject(
    subject_key: tuple[str, str],
    state_dir: Path,
    *,
    lock_timeout: float | None,
) -> dict[str, str] | object | None:
    if not state_dir.is_absolute() or not UserKey.Store.supported():
        return None
    user_id, device_uid = subject_key
    try:
        fingerprint = machine_fingerprint()
    except OSError:
        return KEY_UNAVAILABLE
    except (ValueError, TypeError, AttributeError):
        return None
    try:
        store = EncryptionKey.Store(
            state_dir=state_dir,
            kind="user",
            casdoor_sub=user_id,
            device_uid=device_uid,
            fingerprint=fingerprint,
        )
        if lock_timeout is not None and not _check_store_lock(
            store.root / ".lock", timeout=lock_timeout
        ):
            return KEY_UNAVAILABLE
        record = store.ensure()
    except UserKey.Error as error:
        if error.code in _DEFINITELY_NO_KEY_CODES:
            return None
        return KEY_UNAVAILABLE
    except OSError:
        return KEY_UNAVAILABLE
    except (ValueError, TypeError, AttributeError):
        return None
    key_id, public_key = record.get("keyId"), record.get("publicKey")
    if not isinstance(key_id, str) or not isinstance(public_key, str):
        return None
    return {"keyId": key_id, "publicKey": public_key}


def _read_uncached(
    home: Path, state_dir: Path, *, lock_timeout: float | None
) -> dict[str, str] | object | None:
    subject_key = _read_subject(home)
    if not isinstance(subject_key, tuple):
        return subject_key
    return _ensure_for_subject(subject_key, state_dir, lock_timeout=lock_timeout)


def _read_cached(
    home: Path,
    state_dir: Path,
    cache: EncryptionKeyCache,
    *,
    lock_timeout: float | None,
) -> dict[str, str] | object | None:
    subject_key = _read_subject(home)
    if subject_key is KEY_UNAVAILABLE:
        return KEY_UNAVAILABLE
    if not isinstance(subject_key, tuple):
        cache.invalidate()
        return None
    cached = cache.get(subject_key)
    if cached is not None:
        return cached
    result = _ensure_for_subject(subject_key, state_dir, lock_timeout=lock_timeout)
    if isinstance(result, dict):
        cache.put(subject_key, result)
    return result
