# Decision record (W2A-LOGIN-INPROC, 2026-11-21):
# - Removed POSIX-only cross-process descriptor handoff (acquire_transferable,
#   adopt_inherited, fileno) and <=0.5.5.3.3 upgrade descriptor adoption
#   (adopt_legacy, _cached_descriptor_launcher) per Owner 2026-11-21 ruling.
# - IdentityTransactionLock is backed solely by kernel ProcessFileLock and
#   harden_private_path; is_held_in_process() exposes in-process ownership
#   verification via ProcessFileLock.probe() == "self-owned" on all platforms.
"""Home-scoped OS lock for identity commit and daemon identity read.

The lock is never handed to another process.  A CLI commits identity under it
and releases; the daemon it starts acquires the lock itself before reading
identity and releases it once ready.  The CLI passes the committed identity
generation (``EXPECTED_IDENTITY_GENERATION_ENV``) so a daemon that loses the
gap to a later login exits as stale instead of serving the wrong owner.  The
env value is a check, never an authorization.

Bootstrap and serving reauthentication commit credentials in-process while
holding this lock continuously and verify ownership through
:meth:`IdentityTransactionLock.is_held_in_process`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from hyprial.kernel import FileLockBusy, ProcessFileLock, harden_private_path

IDENTITY_TRANSACTION_LOCK = ".identity-transaction.lock"
EXPECTED_IDENTITY_GENERATION_ENV = "HYPRIAL_EXPECTED_IDENTITY_GENERATION"


class IdentityTransactionBusy(RuntimeError):
    code = "IDENTITY_TRANSACTION_BUSY"


class _HeldLock(Protocol):
    def release(self) -> None: ...

    def close(self) -> None: ...


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
            harden_private_path(path, mode=0o600)
        except BaseException:
            lock.release()
            raise
        return cls(path, lock)

    @classmethod
    def is_held_in_process(cls, home: Path) -> bool:
        """Whether this process currently holds ``home``'s identity transaction lock."""

        return (
            ProcessFileLock(Path(home) / IDENTITY_TRANSACTION_LOCK).probe()
            == "self-owned"
        )

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
