"""Cross-process fencing for offline writers sharing daemon JSON stores."""

from __future__ import annotations

import fcntl
import os
import time
from contextlib import contextmanager


@contextmanager
def json_store_mutation(path, *, timeout=5.0):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.with_name("." + path.name + ".lock")
    descriptor = os.open(
        lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0), 0o600
    )
    deadline = time.monotonic() + max(0.0, timeout)
    try:
        os.fchmod(descriptor, 0o600)
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
