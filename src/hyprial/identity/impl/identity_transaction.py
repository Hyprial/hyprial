"""Home-scoped OS lock for identity commit and daemon identity read.

The lock is never handed to another process.  A CLI commits identity under it
and releases; the daemon it starts acquires the lock itself before reading
identity and releases it once ready.  The CLI passes the committed identity
generation (``EXPECTED_IDENTITY_GENERATION_ENV``) so a daemon that loses the
gap to a later login exits as stale instead of serving the wrong owner.  The
env value is a check, never an authorization.

One legacy exception, kept for exactly one release: a ``<=0.5.5.3.3``
interpreter finishing ``hyprial upgrade`` (or the autoupdate child) still
launches the new ``daemon run`` holding the lock and passing its descriptor in
``LEGACY_TRANSACTION_FD_ENV``.  :meth:`IdentityTransactionLock.adopt_legacy`
takes that descriptor over instead of contending with it -- a second open file
description can never acquire a lock its own inherited descriptor holds, so
without the adoption the upgraded daemon would wait out its lock budget and
exit busy.  Remove in 0.5.5.4.
"""

from __future__ import annotations

import os
from pathlib import Path

from typing import MutableMapping, Protocol

from hyprial.kernel import FileLockBusy, ProcessFileLock, lock_exclusive

IDENTITY_TRANSACTION_LOCK = ".identity-transaction.lock"
EXPECTED_IDENTITY_GENERATION_ENV = "HYPRIAL_EXPECTED_IDENTITY_GENERATION"
# Set only by a <=0.5.5.3.3 launcher (upgrade tail); read by adopt_legacy.
LEGACY_TRANSACTION_FD_ENV = "HYPRIAL_IDENTITY_TRANSACTION_FD"


class IdentityTransactionBusy(RuntimeError):
    code = "IDENTITY_TRANSACTION_BUSY"


class _HeldLock(Protocol):
    def release(self) -> None: ...

    def close(self) -> None: ...


class _InheritedLegacyLock:
    """The descriptor a legacy launcher handed over; closing it releases."""

    def __init__(self, descriptor: int) -> None:
        self._descriptor: int | None = descriptor

    @property
    def held(self) -> bool:
        return self._descriptor is not None

    def release(self) -> None:
        descriptor = self._descriptor
        self._descriptor = None
        if descriptor is not None:
            os.close(descriptor)

    close = release


class IdentityTransactionLock:
    """One held identity-transaction lock, owned by this process only."""

    def __init__(self, path: Path, lock: _HeldLock):
        self.path = path
        self._lock: _HeldLock | None = lock

    @classmethod
    def acquire(
        cls, home: Path, *, timeout: float = 15.0, poll_interval: float = 0.05
    ) -> IdentityTransactionLock:
        home = Path(home)
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = home / IDENTITY_TRANSACTION_LOCK
        lock = ProcessFileLock(path)
        try:
            lock.acquire(timeout=timeout, poll_interval=poll_interval)
        except FileLockBusy as error:
            raise IdentityTransactionBusy(
                f"identity transaction already owns {path}"
            ) from error
        try:
            os.chmod(path, 0o600)
        except BaseException:
            lock.release()
            raise
        return cls(path, lock)

    @classmethod
    def adopt_legacy(
        cls, home: Path, environ: MutableMapping[str, str]
    ) -> IdentityTransactionLock | None:
        """Adopt a ``<=0.5.5.3.3`` launcher's inherited lock, else ``None``.

        The variable is popped first so no descendant inherits it.  Adoption
        requires the descriptor to name the home lock file (``fstat`` inode)
        and a non-blocking ``lock_exclusive`` on that very descriptor to
        succeed -- which it does only for the holding open file description
        (or a free lock, which this process then holds).  Anything else is
        ignored and the caller acquires normally.  Legacy launchers never
        passed a descriptor on Windows (``pass_fds`` is POSIX-only there).
        """

        raw = environ.pop(LEGACY_TRANSACTION_FD_ENV, None)
        if raw is None or os.name == "nt":
            return None
        try:
            descriptor = int(raw)
        except ValueError:
            return None
        path = Path(home) / IDENTITY_TRANSACTION_LOCK
        try:
            expected = path.stat()
            actual = os.fstat(descriptor)
        except OSError:
            return None
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            return None
        try:
            lock_exclusive(descriptor, blocking=False)
        except OSError:
            return None
        return cls(path, _InheritedLegacyLock(descriptor))

    def detach(self) -> _HeldLock:
        """Move the held lock (``close()`` releases it) to an in-process owner."""
        lock = self._lock
        if lock is None:
            raise RuntimeError("identity transaction lock is already detached")
        self._lock = None
        return lock

    def close(self) -> None:
        lock = self._lock
        self._lock = None
        if lock is not None:
            lock.release()

    def __enter__(self) -> IdentityTransactionLock:
        return self

    def __exit__(self, *_error: object) -> None:
        self.close()
