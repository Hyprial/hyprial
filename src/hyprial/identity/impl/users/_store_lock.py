"""Cross-process fencing for offline writers sharing identity JSON stores.

Pure, stateless helper sunk with the canonical ``UserProfileStore`` so identity
does not import the daemon's squire store lock.  The daemon keeps its own copy
for squire setup until the shared vocabulary is sunk by the integration owner.

The lock primitive is the kernel's platform file lock: ``flock`` on POSIX and a
byte-range lock on Windows.  This module is imported while the daemon starts, so
an unconditional ``fcntl`` import here would crash every Windows start.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager

from hyprial.kernel import lock_exclusive, unlock


@contextmanager
def json_store_mutation(path, *, timeout=5.0):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.with_name("." + path.name + ".lock")
    descriptor = os.open(
        lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0), 0o600
    )
    deadline = time.monotonic() + max(0.0, timeout)
    try:
        if hasattr(os, "fchmod"):  # Windows has no descriptor chmod; os.open's mode stands.
            os.fchmod(descriptor, 0o600)
        while True:
            try:
                lock_exclusive(descriptor, blocking=False)
                break
            except BlockingIOError as error:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "JSON store mutation is owned by another writer"
                    ) from error
                time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
        try:
            yield
        finally:
            unlock(descriptor)
    finally:
        os.close(descriptor)
