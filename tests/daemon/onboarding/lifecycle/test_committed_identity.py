"""Worker success is checked against independent public state before promotion."""

from dataclasses import replace
import json
import secrets
import time

import pytest

from hyprial.identity import IdentityPublicState
from hyprial.daemon.impl.onboarding.lifecycle.admission import BootstrapAdmission
from hyprial.daemon.impl.onboarding.lifecycle.identity import verify_worker_commit, verify_promotion_identity


@pytest.fixture
def admitted(tmp_path, monkeypatch):
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    with BootstrapAdmission.acquire(tmp_path, tmp_path / "state", tmp_path / "ipc.sock") as value:
        yield value


def write_identity(home, owner="alice@example.test", issuer="https://issuer.example.test"):
    (home / "settings.json").write_text(json.dumps({
        "owner": owner, "identityMode": "casdoor", "identityIssuer": issuer,
    }))


def result():
    return IdentityPublicState("op-1", "committed", "alice@example.test",
                               "https://issuer.example.test",
                               {"credentialCommitted": True, "settingsCommitted": True})


def verified(admitted, value=None):
    return verify_worker_commit(admitted, value or result(), operation_id="op-1",
                                issuer="https://issuer.example.test")


def test_verified_commit_and_promotion_read_only_public_identity(admitted):
    write_identity(admitted.home)
    secrets = admitted.home / "secrets"
    secrets.mkdir()
    (secrets / "login.json").mkdir()
    proof = verified(admitted)
    assert proof.snapshot.owner == "alice@example.test"
    assert verify_promotion_identity(admitted, proof) == proof.snapshot
    assert (secrets / "login.json").is_dir()


@pytest.mark.parametrize("changes", [
    {"operation_id": "other"}, {"state": "pending"}, {"state": "failed"},
    {"owner": None}, {"owner": "bob@example.test"}, {"issuer": "other"},
    {"receipt": {"credentialCommitted": True, "settingsCommitted": False}},
    {"receipt": {"credentialCommitted": 1, "settingsCommitted": True}},
])
def test_wrong_or_partial_worker_receipt_never_promotes(admitted, changes):
    write_identity(admitted.home)
    before = (admitted.home / "settings.json").read_bytes()
    with pytest.raises(RuntimeError):
        verified(admitted, replace(result(), **changes))
    assert (admitted.home / "settings.json").read_bytes() == before


def test_reported_commit_without_settings_is_not_success(admitted):
    with pytest.raises(RuntimeError):
        verified(admitted)


@pytest.mark.parametrize("change", ["owner", "issuer", "override", "closed-lock"])
def test_promotion_rechecks_identity_environment_and_lock_lifetime(admitted, monkeypatch, change):
    write_identity(admitted.home)
    proof = verified(admitted)
    if change == "owner":
        write_identity(admitted.home, owner="bob@example.test")
    elif change == "issuer":
        write_identity(admitted.home, issuer="https://other.example.test")
    elif change == "override":
        monkeypatch.setenv("HYPRIAL_OWNER", "alice@example.test")
    else:
        admitted.close()
    with pytest.raises((RuntimeError, ValueError)):
        verify_promotion_identity(admitted, proof)


def test_verification_cannot_cross_admitted_homes(admitted, tmp_path):
    write_identity(admitted.home)
    proof = verified(admitted)
    other = tmp_path / "other"
    with BootstrapAdmission.acquire(other, other / "state", other / "ipc.sock") as second:
        write_identity(other)
        with pytest.raises(RuntimeError):
            verify_promotion_identity(second, proof)


def test_real_isolated_worker_commit_reconciles_under_continuous_locks(admitted):
    from hyprial.identity import (
        AuthProfile, CommitPermit, IdentityCredentialService,
        IdentityTransactionBusy, IdentityTransactionLock,
    )
    from hyprial.daemon.impl.configuration.ownership import DaemonOwnershipBusy, DaemonStateOwnershipFence
    from tests.fixtures.network.fake_oidc import FakeOidcConfig, fake_oidc

    with fake_oidc(FakeOidcConfig(pending_polls=0)) as server:
        profile = AuthProfile(server.issuer, "fake-client-id")
        permit = CommitPermit("op-real", secrets.token_hex(32), admitted.home,
                              profile.issuer, profile.client_id,
                              admitted.identity.snapshot, int(time.time()) + 60)
        service = IdentityCredentialService()
        try:
            service.begin(profile=profile, home=admitted.home, permit=permit, lock_fd=None)
            committed = service.poll_public("op-real")
        finally:
            service.close()
        assert committed.state == "committed"
        with pytest.raises(IdentityTransactionBusy):
            IdentityTransactionLock.acquire(admitted.home, timeout=0)
        with pytest.raises(DaemonOwnershipBusy):
            DaemonStateOwnershipFence.acquire(admitted.state_dir, timeout=0)
        proof = verify_worker_commit(admitted, committed, operation_id="op-real", issuer=server.issuer)
        assert verify_promotion_identity(admitted, proof).owner == "alicesmith"
        assert not (admitted.home / ".active_daemon").exists()
        assert not (admitted.state_dir / "daemon.json").exists()
