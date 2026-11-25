"""Real OS-lock continuity across bootstrap and runtime handoffs."""

import pytest

from hyprial.daemon.impl.configuration.ownership import (
    DaemonOwnershipBusy,
    DaemonStateOwnershipFence,
)
from hyprial.daemon.impl.onboarding.locks import BootstrapLocks
from hyprial.identity import IdentityTransactionBusy, IdentityTransactionLock


@pytest.fixture(autouse=True)
def bootstrap_environment(monkeypatch):
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)


def test_failed_second_lock_releases_identity_transaction(tmp_path):
    state = tmp_path / "state"
    with DaemonStateOwnershipFence.acquire(state):
        with pytest.raises(DaemonOwnershipBusy):
            BootstrapLocks.acquire(tmp_path, state)
        with IdentityTransactionLock.acquire(tmp_path, timeout=0):
            pass


def test_busy_identity_does_not_take_state_lock(tmp_path):
    state = tmp_path / "state"
    with IdentityTransactionLock.acquire(tmp_path, timeout=0):
        with pytest.raises(IdentityTransactionBusy):
            BootstrapLocks.acquire(tmp_path, state)
        with DaemonStateOwnershipFence.acquire(state):
            pass


def test_detached_streams_keep_both_locks_until_their_own_release(tmp_path):
    state = tmp_path / "state"
    locks = BootstrapLocks.acquire(tmp_path, state)
    ownership, identity = locks.detach()
    try:
        locks.close()
        with pytest.raises(RuntimeError):
            locks.detach()
        with pytest.raises(IdentityTransactionBusy):
            IdentityTransactionLock.acquire(tmp_path, timeout=0)
        with pytest.raises(DaemonOwnershipBusy):
            DaemonStateOwnershipFence.acquire(state)
        identity.close()
        with IdentityTransactionLock.acquire(tmp_path, timeout=0):
            pass
        with pytest.raises(DaemonOwnershipBusy):
            DaemonStateOwnershipFence.acquire(state)
    finally:
        identity.close()
        ownership.close()
    with DaemonStateOwnershipFence.acquire(state):
        pass
