"""Bootstrap must reject ambient identity overrides before touching home."""

import pytest

from hyprial.daemon.impl.configuration.ownership import (
    DaemonOwnershipBusy,
    DaemonStateOwnershipFence,
)
from hyprial.daemon.impl.onboarding.locks import BootstrapLocks
from hyprial.identity import IdentityTransactionBusy, IdentityTransactionLock


@pytest.mark.parametrize("owner", ["same@example.test", "other@example.test", " "])
def test_override_refuses_empty_home_without_creating_locks(tmp_path, monkeypatch, owner):
    home = tmp_path / "empty-home"
    monkeypatch.setenv("HYPRIAL_OWNER", owner)
    with pytest.raises(ValueError, match="HYPRIAL_OWNER"):
        with BootstrapLocks.acquire(home, home / "state"):
            pass
    assert not home.exists()


def test_override_added_before_promotion_retains_locks(tmp_path, monkeypatch):
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    state = tmp_path / "state"
    with BootstrapLocks.acquire(tmp_path, state) as locks:
        monkeypatch.setenv("HYPRIAL_OWNER", "other@example.test")
        with pytest.raises(ValueError, match="HYPRIAL_OWNER"):
            locks.detach()
        with pytest.raises(IdentityTransactionBusy):
            IdentityTransactionLock.acquire(tmp_path, timeout=0)
        with pytest.raises(DaemonOwnershipBusy):
            DaemonStateOwnershipFence.acquire(state, timeout=0)
    with IdentityTransactionLock.acquire(tmp_path, timeout=0):
        pass
    with DaemonStateOwnershipFence.acquire(state, timeout=0):
        pass


def test_empty_override_allows_lock_handoff(tmp_path, monkeypatch):
    monkeypatch.setenv("HYPRIAL_OWNER", "")
    with BootstrapLocks.acquire(tmp_path, tmp_path / "state") as locks:
        ownership, identity = locks.detach()
        identity.close()
        ownership.close()


@pytest.mark.parametrize("owner", ["same@example.test", "other@example.test"])
def test_override_refused_even_when_matching_settings(tmp_path, monkeypatch, owner):
    settings = tmp_path / "settings.json"
    original = '{"owner":"same@example.test"}'
    settings.write_text(original)
    monkeypatch.setenv("HYPRIAL_OWNER", owner)
    with pytest.raises(ValueError, match="HYPRIAL_OWNER"):
        with BootstrapLocks.acquire(tmp_path, tmp_path / "state"):
            pass
    assert settings.read_text() == original
    assert list(tmp_path.iterdir()) == [settings]
