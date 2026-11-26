"""A caller can bound how long a key store waits for its own file lock.

The default wait is unchanged (15 s).  A caller on a periodic maintenance path
passes a short one so a lock held elsewhere cannot stall that path.
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


def _encryption_store(tmp_path):
    return identity.EncryptionKey.Store(
        state_dir=tmp_path / "state",
        kind="user",
        casdoor_sub="subject-alice",
        device_uid="device-a",
        fingerprint="machine-a",
    )


@pytest.mark.parametrize("make_store", [_signing_store, _encryption_store], ids=["signing", "encryption"])
def test_the_default_lock_wait_is_unchanged_and_the_short_wait_gives_up_at_the_timeout(
    tmp_path, make_store
) -> None:
    store = make_store(tmp_path)
    first = store.ensure()
    held = ProcessFileLock(store.root / ".lock")
    held.acquire(0.0)
    try:
        started = time.monotonic()
        with pytest.raises(identity.UserKey.Error) as refused:
            store.ensure(lock_timeout=0.3)
        elapsed = time.monotonic() - started
    finally:
        held.release()

    assert refused.value.code == "USER_KEY_QUARANTINED"
    assert 0.3 <= elapsed < 3.0
    assert store.ensure() == first
