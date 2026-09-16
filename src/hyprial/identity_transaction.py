"""Home-scoped OS lock for identity commit and daemon generation handoff."""

from __future__ import annotations

import fcntl
import os
import time
from pathlib import Path
from typing import IO, MutableMapping

IDENTITY_TRANSACTION_LOCK = ".identity-transaction.lock"
IDENTITY_TRANSACTION_FD_ENV = "HYPRIAL_IDENTITY_TRANSACTION_FD"


class IdentityTransactionBusy(RuntimeError):
    code = "IDENTITY_TRANSACTION_BUSY"


class IdentityTransactionLock:
    """A held ``flock`` transferable to the daemon child by descriptor.

    The environment carries only the descriptor number.  Adoption verifies
    that the descriptor is the expected lock inode *and* that an independently
    opened descriptor cannot acquire the lock.  An environment string alone
    is therefore not an authorization bypass.
    """

    def __init__(self, path: Path, stream: IO[bytes]):
        self.path = path
        self._stream: IO[bytes] | None = stream

    @property
    def fileno(self) -> int:
        if self._stream is None:
            raise RuntimeError("identity transaction lock is closed")
        return self._stream.fileno()

    @classmethod
    def acquire(
        cls, home: Path, *, timeout: float = 15.0, poll_interval: float = 0.05
    ) -> IdentityTransactionLock:
        home = Path(home)
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = home / IDENTITY_TRANSACTION_LOCK
        stream = path.open("a+b")
        os.chmod(path, 0o600)
        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return cls(path, stream)
            except BlockingIOError as error:
                if time.monotonic() >= deadline:
                    stream.close()
                    raise IdentityTransactionBusy(
                        f"identity transaction already owns {path}"
                    ) from error
                time.sleep(max(0.001, poll_interval))

    @classmethod
    def acquire_or_adopt(
        cls,
        home: Path,
        environ: MutableMapping[str, str],
        *,
        timeout: float = 15.0,
    ) -> IdentityTransactionLock:
        raw = environ.get(IDENTITY_TRANSACTION_FD_ENV)
        if raw is None:
            return cls.acquire(home, timeout=timeout)
        try:
            descriptor = int(raw)
        except ValueError as error:
            raise RuntimeError("invalid inherited identity transaction descriptor") from error
        path = Path(home) / IDENTITY_TRANSACTION_LOCK
        try:
            expected = path.stat()
            actual = os.fstat(descriptor)
        except OSError as error:
            raise RuntimeError("inherited identity transaction descriptor is unreadable") from error
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise RuntimeError(
                "inherited identity transaction descriptor is not the home lock"
            )
        verifier = path.open("a+b")
        try:
            try:
                fcntl.flock(verifier.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                fcntl.flock(verifier.fileno(), fcntl.LOCK_UN)
                raise RuntimeError(
                    "inherited identity transaction descriptor does not hold the OS lock"
                )
        finally:
            verifier.close()
        # The verifier proves *someone* holds the lock; only re-locking this
        # very descriptor proves *this* open file description is the holder.
        # flock re-acquisition on the holding description succeeds, while a
        # description that merely names the same inode blocks — so without
        # this half, an unrelated inherited descriptor to the lock file
        # would adopt a transaction somebody else holds.  The residual race
        # is the true holder releasing between the two probes, which the
        # handoff contract forbids (the parent holds through post-launch
        # verification).
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(
                "inherited identity transaction descriptor is not the lock holder"
            ) from error
        stream = os.fdopen(descriptor, "a+b", closefd=True)
        # The variable's only job was this handoff.  Leaving it in the
        # daemon's environment leaks it to every descendant, and a
        # grandchild that starts its own daemon would try to adopt a
        # descriptor number that means nothing in its process.
        environ.pop(IDENTITY_TRANSACTION_FD_ENV, None)
        return cls(path, stream)

    def detach(self) -> IO[bytes]:
        stream = self._stream
        if stream is None:
            raise RuntimeError("identity transaction lock is already detached")
        self._stream = None
        return stream

    def close(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is not None:
            stream.close()

    def __enter__(self) -> IdentityTransactionLock:
        return self

    def __exit__(self, *_error: object) -> None:
        self.close()
