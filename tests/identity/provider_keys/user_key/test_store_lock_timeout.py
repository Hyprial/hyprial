"""A caller can bound how long the signing key store waits for its own file lock.

The default wait is unchanged (15 s).  A caller on a periodic maintenance path
passes a short one so a lock held elsewhere cannot stall that path.

The encryption key store reaches the same lock through its own ``ensure()``,
which takes no timeout yet; its case is left out until that changes.
"""

from __future__ import annotations

import time

import pytest

import hyprial.identity as identity
from hyprial.kernel import ProcessFileLock


def _signing_store(tmp_path):
    return identity.UserKey.Store(
        state_dir=tmp_path / "state",
        owner="alice@example",
        casdoor_sub="subject-alice",
        device_uid="device-a",
        fingerprint="machine-a",
    )


def test_the_default_lock_wait_is_unchanged_and_the_short_wait_gives_up_at_the_timeout(
    tmp_path,
) -> None:
    store = _signing_store(tmp_path)
    first = store.ensure()
    held = ProcessFileLock(store.root / ".lock")
    held.acquire(0.0)
    try:
        started = time.monotonic()
        with pytest.raises(identity.UserKey.Error) as refused:
            store.ensure(lock_timeout=0.3)
        elapsed = time.monotonic() - started
        with pytest.raises(identity.UserKey.Error) as refused_by_locked:
            with store.locked(timeout=0.3):
                raise AssertionError("the lock was taken")
        locked_elapsed = time.monotonic() - started - elapsed
    finally:
        held.release()

    assert refused.value.code == "USER_KEY_QUARANTINED"
    assert 0.3 <= elapsed < 3.0
    assert refused_by_locked.value.code == "USER_KEY_QUARANTINED"
    assert 0.3 <= locked_elapsed < 3.0
    assert store.ensure() == first
