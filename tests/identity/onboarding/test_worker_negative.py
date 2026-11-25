"""Negative protocol boundaries for the in-process identity-credential service.

The issuer is the in-repo loopback fake.  These tests prove the service refuses
missing, wrong, expired, replayed, unlocked, cross-home, and post-cancel
authority before landing a credential.  They are not evidence of an external
real login.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any

import pytest

from hyprial.identity.impl.identity_transaction import IdentityTransactionLock
from hyprial.identity.impl.onboarding.credentials import flow as credential_flow
from hyprial.identity.impl.onboarding.credentials import permit_ledger
from hyprial.identity.impl.onboarding.identity_record import write_settings_identity
from hyprial.identity.impl.onboarding.ports import (
    AuthProfile,
    CommitPermit,
    IdentityError,
    IdentitySnapshot,
    IdentityWorkerError,
    IdentityWorkerProtocolError,
)
from hyprial.identity.impl.onboarding.service import IdentityCredentialService
from tests.fixtures.network.fake_oidc import FakeOidcConfig, fake_oidc

_REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _no_owner_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)


def _home(tmp_path: Path, name: str = "home") -> Path:
    home = tmp_path / name
    home.mkdir(mode=0o700)
    return home


def _permit(
    home: Path,
    profile: AuthProfile,
    *,
    operation_id: str = "op-negative",
    expected: IdentitySnapshot | None = None,
    expires_at: int | None = None,
) -> CommitPermit:
    return CommitPermit(
        operation_id=operation_id,
        nonce=f"nonce-{operation_id}",
        home=home,
        issuer=profile.issuer,
        client_id=profile.client_id,
        expected_identity=expected or IdentitySnapshot(),
        expires_at=expires_at if expires_at is not None else int(time.time()) + 600,
        bootstrap=True,
    )


def _assert_no_credential(home: Path) -> None:
    assert not (home / "secrets" / "login.json").exists()


def test_missing_permit_is_refused_before_spawn(tmp_path: Path) -> None:
    home = _home(tmp_path)
    service = IdentityCredentialService()
    try:
        with pytest.raises(IdentityError) as caught:
            service.begin(
                profile=AuthProfile(issuer="http://127.0.0.1:1", client_id="c"),
                home=home,
                permit=None,
                lock_fd=None,
            )
    finally:
        service.close()
    assert caught.value.code == "PERMIT_REQUIRED"
    assert service._op is None
    _assert_no_credential(home)


def test_unheld_transaction_lock_is_refused_before_network_and_commit(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    with fake_oidc(FakeOidcConfig(pending_polls=0)) as server:
        profile = AuthProfile(issuer=server.issuer, client_id="fake-client-id")
        unlocked = IdentityCredentialService()
        try:
            with pytest.raises(IdentityWorkerError) as begin_caught:
                unlocked.begin(
                    profile=profile,
                    home=home,
                    permit=_permit(home, profile, operation_id="op-unlocked-begin"),
                    lock_fd=None,
                )
        finally:
            unlocked.close()
        assert begin_caught.value.code == "IDENTITY_LOCK_REQUIRED"
        assert server.records == []
        _assert_no_credential(home)

        lock = IdentityTransactionLock.acquire(home)
        released_mid_flow = IdentityCredentialService()
        try:
            begin = released_mid_flow.begin(
                profile=profile,
                home=home,
                permit=_permit(home, profile, operation_id="op-released-before-poll"),
                lock_fd=None,
            )
            lock.close()
            with pytest.raises(IdentityWorkerError) as poll_caught:
                released_mid_flow.poll_public(begin.operation_id)
        finally:
            released_mid_flow.close()
            lock.close()
        assert poll_caught.value.code == "IDENTITY_LOCK_REQUIRED"
        _assert_no_credential(home)


def test_permit_for_another_home_is_refused(tmp_path: Path) -> None:
    home = _home(tmp_path)
    other = _home(tmp_path, "other-home")
    with fake_oidc() as server:
        profile = AuthProfile(issuer=server.issuer, client_id="fake-client-id")
        lock = IdentityTransactionLock.acquire(home)
        service = IdentityCredentialService()
        try:
            with pytest.raises(IdentityError) as caught:
                service.begin(
                    profile=profile,
                    home=home,
                    permit=_permit(other, profile),
                    lock_fd=None,
                )
        finally:
            service.close()
            lock.close()
        assert server.records == []
    assert caught.value.code == "PERMIT_HOME_MISMATCH"
    _assert_no_credential(home)
    _assert_no_credential(other)


def test_expired_permit_is_refused_before_network(tmp_path: Path) -> None:
    home = _home(tmp_path)
    with fake_oidc() as server:
        profile = AuthProfile(issuer=server.issuer, client_id="fake-client-id")
        lock = IdentityTransactionLock.acquire(home)
        service = IdentityCredentialService()
        try:
            with pytest.raises(IdentityWorkerError) as caught:
                service.begin(
                    profile=profile,
                    home=home,
                    permit=_permit(
                        home, profile, expires_at=int(time.time()) - 1
                    ),
                    lock_fd=None,
                )
        finally:
            service.close()
            lock.close()
        assert server.state.polls == 0
    assert caught.value.code == "PERMIT_EXPIRED"
    _assert_no_credential(home)


def test_permit_profile_mismatch_is_refused_before_discovery(tmp_path: Path) -> None:
    home = _home(tmp_path)
    with fake_oidc() as server:
        permit_profile = AuthProfile(
            issuer=server.issuer, client_id="fake-client-id"
        )
        trusted_profile = AuthProfile(
            issuer=server.issuer, client_id="another-client"
        )
        lock = IdentityTransactionLock.acquire(home)
        service = IdentityCredentialService()
        try:
            with pytest.raises(IdentityWorkerError) as caught:
                service.begin(
                    profile=trusted_profile,
                    home=home,
                    permit=_permit(home, permit_profile),
                    lock_fd=None,
                )
        finally:
            service.close()
            lock.close()
        assert server.state.polls == 0
        assert server.records_for("/.well-known/openid-configuration") == []
    assert caught.value.code == "PERMIT_PROFILE_MISMATCH"
    _assert_no_credential(home)


def test_other_owner_fails_without_writing_or_leaking_secrets(tmp_path: Path) -> None:
    home = _home(tmp_path)
    with fake_oidc(FakeOidcConfig(preferred_username="mallory", pending_polls=0)) as server:
        profile = AuthProfile(issuer=server.issuer, client_id="fake-client-id")
        write_settings_identity(
            "alicesmith",
            mode="casdoor",
            issuer=server.issuer,
            hyprial_home=home,
            environ={},
        )
        before = (home / "settings.json").read_bytes()
        permit = _permit(
            home,
            profile,
            expected=IdentitySnapshot(
                owner="alicesmith", mode="casdoor", issuer=server.issuer
            ),
        )
        lock = IdentityTransactionLock.acquire(home)
        service = IdentityCredentialService()
        try:
            begin = service.begin(
                profile=profile, home=home, permit=permit, lock_fd=None
            )
            state = service.poll_public(begin.operation_id)
            stderr = service.stderr_text()
        finally:
            service.close()
            lock.close()
    assert state.state == "failed"
    assert state.error_code == "OWNER_MISMATCH"
    assert state.receipt == {
        "currentOwner": "alicesmith",
        "loginOwner": "mallory",
    }
    observed = json.dumps({"error": state.error_message, "receipt": state.receipt})
    observed += stderr
    for secret in (server.config.access_token, server.config.refresh_token):
        assert secret not in observed
    assert "fake-device-code" not in observed
    _assert_no_credential(home)
    assert (home / "settings.json").read_bytes() == before


def test_cancel_refuses_late_poll_without_token_request_or_credential(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    with fake_oidc(FakeOidcConfig(pending_polls=5)) as server:
        profile = AuthProfile(issuer=server.issuer, client_id="fake-client-id")
        lock = IdentityTransactionLock.acquire(home)
        service = IdentityCredentialService()
        try:
            begin = service.begin(
                profile=profile,
                home=home,
                permit=_permit(home, profile),
                lock_fd=None,
            )
            pending = service.poll_public(begin.operation_id)
            assert pending.state == "pending"
            cancelled = service.cancel(begin.operation_id)
            assert cancelled.state == "cancelled"
            assert cancelled.receipt == {
                "credentialCommitted": False,
                "settingsCommitted": False,
            }
            polls_at_cancel = server.state.polls
            late = service.poll_public(begin.operation_id)
            assert late.state == "cancelled"
            assert server.state.polls == polls_at_cancel
        finally:
            service.close()
            lock.close()
    _assert_no_credential(home)


def test_cancel_before_commit_check_writes_no_identity_files(tmp_path: Path) -> None:
    home = _home(tmp_path)
    commit_check_started = threading.Event()
    release_commit_check = threading.Event()
    poll_result: list[Any] = []
    cancel_result: list[Any] = []

    class _BarrierService(IdentityCredentialService):
        def _before_commit_identity(self, operation: Any, epoch: int) -> None:
            del operation, epoch
            commit_check_started.set()
            release_commit_check.wait(timeout=5.0)

    with fake_oidc(FakeOidcConfig(pending_polls=0)) as server:
        profile = AuthProfile(issuer=server.issuer, client_id="fake-client-id")
        lock = IdentityTransactionLock.acquire(home)
        service = _BarrierService()
        try:
            begin = service.begin(
                profile=profile,
                home=home,
                permit=_permit(home, profile, operation_id="op-cancel-before-commit"),
                lock_fd=None,
            )
            poll_thread = threading.Thread(
                target=lambda: poll_result.append(service.poll_public(begin.operation_id)),
                daemon=True,
            )
            poll_thread.start()
            assert commit_check_started.wait(timeout=2.0)

            cancel_thread = threading.Thread(
                target=lambda: cancel_result.append(service.cancel(begin.operation_id)),
                daemon=True,
            )
            cancel_thread.start()
            cancel_thread.join(timeout=1.0)
            assert not cancel_thread.is_alive()
            assert cancel_result[0].state == "cancelled"

            release_commit_check.set()
            poll_thread.join(timeout=2.0)
            assert not poll_thread.is_alive()
            assert poll_result[0].state == "cancelled"
        finally:
            release_commit_check.set()
            service.close()
            lock.close()
    _assert_no_credential(home)
    assert not (home / "settings.json").exists()


def test_replayed_permit_after_first_commit_is_stale(tmp_path: Path) -> None:
    home = _home(tmp_path)
    with fake_oidc(FakeOidcConfig(pending_polls=0)) as server:
        profile = AuthProfile(issuer=server.issuer, client_id="fake-client-id")
        permit = _permit(home, profile, operation_id="op-replay")
        lock = IdentityTransactionLock.acquire(home)
        try:
            first = IdentityCredentialService()
            try:
                begin = first.begin(
                    profile=profile, home=home, permit=permit, lock_fd=None
                )
                committed = first.poll_public(begin.operation_id)
            finally:
                first.close()
            assert committed.state == "committed"
            credential = home / "secrets" / "login.json"
            first_bytes = credential.read_bytes()

            replay = IdentityCredentialService()
            try:
                with pytest.raises(IdentityWorkerError) as caught:
                    replay.begin(
                        profile=profile,
                        home=home,
                        permit=permit,
                        lock_fd=None,
                    )
            finally:
                replay.close()
        finally:
            lock.close()
    assert caught.value.code == "STALE_IDENTITY_SNAPSHOT"
    assert credential.read_bytes() == first_bytes


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("{not-json", "IDENTITY_SETTINGS_CORRUPT"),
        (
            '{"identityMode": "casdoor", "identityIssuer": "https://issuer.example"}',
            "IDENTITY_RESIDUE_PRESENT",
        ),
    ],
)
def test_worker_refuses_strict_settings_failure_before_network(
    tmp_path: Path, raw: str, code: str
) -> None:
    home = _home(tmp_path)
    settings = home / "settings.json"
    settings.write_text(raw, encoding="utf-8")
    before = settings.read_bytes()
    with fake_oidc() as server:
        profile = AuthProfile(issuer=server.issuer, client_id="fake-client-id")
        lock = IdentityTransactionLock.acquire(home)
        service = IdentityCredentialService()
        try:
            with pytest.raises(IdentityWorkerError) as caught:
                service.begin(
                    profile=profile,
                    home=home,
                    permit=_permit(home, profile),
                    lock_fd=None,
                )
        finally:
            service.close()
            lock.close()
        assert server.records == []
        assert server.state.polls == 0
    assert caught.value.code == code
    assert settings.read_bytes() == before
    _assert_no_credential(home)


def test_same_owner_permit_replayed_across_workers_is_refused(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    with fake_oidc(FakeOidcConfig(pending_polls=0)) as server:
        profile = AuthProfile(issuer=server.issuer, client_id="fake-client-id")
        expected = IdentitySnapshot(
            owner="alicesmith", mode="casdoor", issuer=server.issuer
        )
        write_settings_identity(
            "alicesmith",
            mode="casdoor",
            issuer=server.issuer,
            hyprial_home=home,
            environ={},
        )
        permit = _permit(
            home,
            profile,
            operation_id="op-same-owner-replay",
            expected=expected,
        )
        lock = IdentityTransactionLock.acquire(home)
        try:
            first = IdentityCredentialService()
            try:
                begin = first.begin(
                    profile=profile, home=home, permit=permit, lock_fd=None
                )
                committed = first.poll_public(begin.operation_id)
                first_stderr = first.stderr_text()
            finally:
                first.close()
            assert committed.state == "committed", first_stderr
            credential = home / "secrets" / "login.json"
            first_bytes = credential.read_bytes()
            first_obtained = json.loads(first_bytes)["obtainedAt"]
            settings_after_first = (home / "settings.json").read_bytes()

            replay = IdentityCredentialService()
            try:
                replay_begin = replay.begin(
                    profile=profile, home=home, permit=permit, lock_fd=None
                )
                replayed = replay.poll_public(replay_begin.operation_id)
                replay_stderr = replay.stderr_text()
            finally:
                replay.close()
            assert replayed.state == "failed", replay_stderr
            assert replayed.error_code == "PERMIT_REPLAYED", replayed.error_message
            assert credential.read_bytes() == first_bytes
            assert (home / "settings.json").read_bytes() == settings_after_first

            fresh = IdentityCredentialService()
            fresh_permit = _permit(
                home,
                profile,
                operation_id="op-same-owner-reauth",
                expected=expected,
            )
            try:
                fresh_begin = fresh.begin(
                    profile=profile,
                    home=home,
                    permit=fresh_permit,
                    lock_fd=None,
                )
                reauth = fresh.poll_public(fresh_begin.operation_id)
                fresh_stderr = fresh.stderr_text()
            finally:
                fresh.close()
        finally:
            lock.close()
    assert reauth.state == "committed", fresh_stderr
    second_bytes = credential.read_bytes()
    assert second_bytes != first_bytes
    assert json.loads(second_bytes)["obtainedAt"] != first_obtained
    assert json.loads(second_bytes)["refreshToken"] == server.config.refresh_token


def test_permit_expiring_after_begin_is_refused_before_commit(tmp_path: Path) -> None:
    home = _home(tmp_path)
    with fake_oidc(FakeOidcConfig(pending_polls=0)) as server:
        profile = AuthProfile(issuer=server.issuer, client_id="fake-client-id")
        expires_at = int(time.time()) + 3
        permit = _permit(
            home,
            profile,
            operation_id="op-expires-during-flow",
            expires_at=expires_at,
        )
        lock = IdentityTransactionLock.acquire(home)
        service = IdentityCredentialService()
        try:
            begin = service.begin(
                profile=profile, home=home, permit=permit, lock_fd=None
            )
            time.sleep(max(0.0, expires_at - time.time() + 0.2))
            state = service.poll_public(begin.operation_id)
            stderr = service.stderr_text()
        finally:
            service.close()
            lock.close()
    assert state.state == "failed", stderr
    assert state.error_code == "PERMIT_EXPIRED", state.error_message
    _assert_no_credential(home)
    assert not (home / "settings.json").exists()
    assert not (home / ".identity-permit-uses.json").exists()


@pytest.mark.parametrize("shape", ["directory", "corrupt", "symlink"])
def test_unverifiable_permit_ledger_fails_closed(
    tmp_path: Path, shape: str
) -> None:
    home = _home(tmp_path)
    ledger = home / ".identity-permit-uses.json"
    target = tmp_path / "outside-ledger.json"
    if shape == "directory":
        ledger.mkdir()
    elif shape == "corrupt":
        ledger.write_text("{not-json", encoding="utf-8")
    else:
        target.write_text('{"version": 1, "permits": []}\n', encoding="utf-8")
        ledger.symlink_to(target)
    target_before = target.read_bytes() if shape == "symlink" else None
    with fake_oidc(FakeOidcConfig(pending_polls=0)) as server:
        profile = AuthProfile(issuer=server.issuer, client_id="fake-client-id")
        lock = IdentityTransactionLock.acquire(home)
        service = IdentityCredentialService()
        try:
            begin = service.begin(
                profile=profile,
                home=home,
                permit=_permit(
                    home, profile, operation_id=f"op-ledger-{shape}"
                ),
                lock_fd=None,
            )
            state = service.poll_public(begin.operation_id)
            stderr = service.stderr_text()
        finally:
            service.close()
            lock.close()
    assert state.state == "failed", stderr
    assert state.error_code == "PERMIT_LEDGER_INVALID", state.error_message
    _assert_no_credential(home)
    assert not (home / "settings.json").exists()
    if target_before is not None:
        assert target.read_bytes() == target_before


def test_permit_ledger_stores_only_a_fingerprint(tmp_path: Path) -> None:
    home = _home(tmp_path)
    with fake_oidc(FakeOidcConfig(pending_polls=0)) as server:
        profile = AuthProfile(issuer=server.issuer, client_id="fake-client-id")
        permit = _permit(home, profile, operation_id="op-ledger-shape")
        lock = IdentityTransactionLock.acquire(home)
        service = IdentityCredentialService()
        try:
            begin = service.begin(
                profile=profile, home=home, permit=permit, lock_fd=None
            )
            committed = service.poll_public(begin.operation_id)
            stderr = service.stderr_text()
        finally:
            service.close()
            lock.close()
    assert committed.state == "committed", stderr
    ledger = home / ".identity-permit-uses.json"
    ledger_text = ledger.read_text(encoding="utf-8")
    record = json.loads(ledger_text)
    assert record["version"] == 1
    assert isinstance(record["permits"], list) and len(record["permits"]) == 1
    entry = record["permits"][0]
    assert set(entry) == {"fingerprint", "expiresAt"}
    assert entry["expiresAt"] == permit.expires_at
    assert len(entry["fingerprint"]) == 64
    int(entry["fingerprint"], 16)
    for secret in (
        server.config.device_code,
        server.config.access_token,
        server.config.refresh_token,
        server.config.id_token,
        permit.nonce,
    ):
        assert secret not in ledger_text
    assert ledger.stat().st_mode & 0o777 == 0o600
    credential = json.loads(
        (home / "secrets" / "login.json").read_text(encoding="utf-8")
    )
    assert set(credential) == {
        "version", "issuer", "refreshToken", "obtainedAt", "identityGeneration"
    }
    assert credential["identityGeneration"] == 1


def _commit_directly(
    home: Path, server: Any, profile: AuthProfile, permit: CommitPermit
) -> dict[str, Any]:
    return credential_flow.commit_identity(
        permit=permit,
        profile=profile,
        home=home,
        owner="alicesmith",
        subject="ou_0123456789abcdef0123456789abcdef",
        refresh_token=server.config.refresh_token,
        environ={},
        now_epoch=int(time.time()),
    )


def test_commit_leaves_a_held_binding_of_another_account_untouched(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    held = {
        "user": "someone-else",
        "larkUnionId": "on_someone_else",
        "proof": "held.proof.signature",
        "publishedAt": "2026-11-01T00:00:00+00:00",
    }
    with fake_oidc(FakeOidcConfig(pending_polls=0)) as server:
        profile = AuthProfile(issuer=server.issuer, client_id="fake-client-id")
        expected = IdentitySnapshot(
            owner="alicesmith", mode="casdoor", issuer=server.issuer
        )
        write_settings_identity(
            "alicesmith",
            mode="casdoor",
            issuer=server.issuer,
            hyprial_home=home,
            environ={},
        )
        settings = home / "settings.json"
        record = json.loads(settings.read_text(encoding="utf-8"))
        record["identityBinding"] = held
        settings.write_text(json.dumps(record), encoding="utf-8")
        _commit_directly(
            home,
            server,
            profile,
            _permit(
                home, profile, operation_id="op-held-binding", expected=expected
            ),
        )
    after = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert after["identityBinding"] == held


def test_permit_ledger_write_failure_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    with fake_oidc(FakeOidcConfig(pending_polls=0)) as server:
        profile = AuthProfile(issuer=server.issuer, client_id="fake-client-id")
        expected = IdentitySnapshot(
            owner="alicesmith", mode="casdoor", issuer=server.issuer
        )
        write_settings_identity(
            "alicesmith",
            mode="casdoor",
            issuer=server.issuer,
            hyprial_home=home,
            environ={},
        )
        settings_before = (home / "settings.json").read_bytes()

        def fail_write(_path: Path, _value: object) -> None:
            raise OSError("injected ledger write failure")

        monkeypatch.setattr(permit_ledger, "atomic_json_write", fail_write)
        with pytest.raises(IdentityError) as caught:
            _commit_directly(
                home,
                server,
                profile,
                _permit(
                    home,
                    profile,
                    operation_id="op-ledger-write-failure",
                    expected=expected,
                ),
            )
    assert caught.value.code == "PERMIT_LEDGER_UNAVAILABLE"
    _assert_no_credential(home)
    assert (home / "settings.json").read_bytes() == settings_before
    assert not (home / ".identity-permit-uses.json").exists()


def test_permit_stays_burned_when_the_credential_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    with fake_oidc(FakeOidcConfig(pending_polls=0)) as server:
        profile = AuthProfile(issuer=server.issuer, client_id="fake-client-id")
        expected = IdentitySnapshot(
            owner="alicesmith", mode="casdoor", issuer=server.issuer
        )
        write_settings_identity(
            "alicesmith",
            mode="casdoor",
            issuer=server.issuer,
            hyprial_home=home,
            environ={},
        )
        settings_before = (home / "settings.json").read_bytes()
        permit = _permit(
            home,
            profile,
            operation_id="op-burned-after-write-failure",
            expected=expected,
        )
        real_write = credential_flow.write_login_credential

        def fail_write(*_args: object, **_kwargs: object) -> None:
            raise OSError("injected credential write failure")

        monkeypatch.setattr(credential_flow, "write_login_credential", fail_write)
        with pytest.raises(OSError, match="injected credential write failure"):
            _commit_directly(home, server, profile, permit)
        assert (home / ".identity-permit-uses.json").exists()
        _assert_no_credential(home)
        assert (home / "settings.json").read_bytes() == settings_before

        monkeypatch.setattr(credential_flow, "write_login_credential", real_write)
        with pytest.raises(IdentityError) as caught:
            _commit_directly(home, server, profile, permit)
    assert caught.value.code == "PERMIT_REPLAYED"
    _assert_no_credential(home)
    assert (home / "settings.json").read_bytes() == settings_before


def test_permit_ledger_directory_sync_runs_after_the_entry_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    ledger = home / ".identity-permit-uses.json"
    with fake_oidc(FakeOidcConfig(pending_polls=0)) as server:
        profile = AuthProfile(issuer=server.issuer, client_id="fake-client-id")
        expected = IdentitySnapshot(
            owner="alicesmith", mode="casdoor", issuer=server.issuer
        )
        write_settings_identity(
            "alicesmith",
            mode="casdoor",
            issuer=server.issuer,
            hyprial_home=home,
            environ={},
        )
        permit = _permit(
            home,
            profile,
            operation_id="op-ledger-directory-sync-order",
            expected=expected,
        )
        real_sync = getattr(permit_ledger, "_fsync_directory", None)
        observed: list[tuple[Path, list[dict[str, object]]]] = []

        def record_sync(path: Path) -> None:
            entries = json.loads(ledger.read_text(encoding="utf-8"))["permits"]
            observed.append((path, entries))
            if real_sync is not None:
                real_sync(path)

        monkeypatch.setattr(
            permit_ledger, "_fsync_directory", record_sync, raising=False
        )
        receipt = _commit_directly(home, server, profile, permit)
    assert receipt == {"credentialCommitted": True, "settingsCommitted": True}
    assert len(observed) == 1
    assert observed[0][0] == home
    assert [entry["fingerprint"] for entry in observed[0][1]] == [
        permit_ledger.permit_fingerprint(permit)
    ]


def test_permit_ledger_directory_sync_failure_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    ledger = home / ".identity-permit-uses.json"
    with fake_oidc(FakeOidcConfig(pending_polls=0)) as server:
        profile = AuthProfile(issuer=server.issuer, client_id="fake-client-id")
        expected = IdentitySnapshot(
            owner="alicesmith", mode="casdoor", issuer=server.issuer
        )
        write_settings_identity(
            "alicesmith",
            mode="casdoor",
            issuer=server.issuer,
            hyprial_home=home,
            environ={},
        )
        settings_before = (home / "settings.json").read_bytes()
        permit = _permit(
            home,
            profile,
            operation_id="op-ledger-directory-sync-failure",
            expected=expected,
        )

        def fail_sync(_path: Path) -> None:
            raise OSError("injected ledger directory sync failure")

        monkeypatch.setattr(
            permit_ledger, "_fsync_directory", fail_sync, raising=False
        )
        with pytest.raises(IdentityError) as caught:
            _commit_directly(home, server, profile, permit)
        ledger_after_failure = ledger.read_bytes()
        with pytest.raises(IdentityError) as replay:
            _commit_directly(home, server, profile, permit)
    assert caught.value.code == "PERMIT_LEDGER_UNAVAILABLE"
    assert "injected ledger directory sync failure" in str(caught.value)
    assert ledger.stat().st_mode & 0o777 == 0o600
    assert json.loads(ledger_after_failure)["permits"] == [
        {
            "fingerprint": permit_ledger.permit_fingerprint(permit),
            "expiresAt": permit.expires_at,
        }
    ]
    assert replay.value.code == "PERMIT_REPLAYED"
    assert ledger.read_bytes() == ledger_after_failure
    _assert_no_credential(home)
    assert (home / "settings.json").read_bytes() == settings_before


def test_concurrent_begin_allows_one_operation_and_does_not_overwrite_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    with fake_oidc(FakeOidcConfig(pending_polls=1)) as server:
        profile = AuthProfile(issuer=server.issuer, client_id="fake-client-id")
        lock = IdentityTransactionLock.acquire(home)
        service = IdentityCredentialService()
        start_barrier = threading.Barrier(2)
        loser_done = threading.Event()
        probe_lock = threading.Lock()
        winner_tid: list[int | None] = [None]
        real_held = IdentityTransactionLock.is_held_in_process
        successes: list[str] = []
        failures: list[str] = []
        unexpected: list[BaseException] = []
        results_lock = threading.Lock()

        def gated_is_held(target_home: Path) -> bool:
            is_first = False
            with probe_lock:
                if winner_tid[0] is None:
                    winner_tid[0] = threading.get_ident()
                    is_first = True
            if is_first:
                assert loser_done.wait(timeout=5.0)
            return real_held(target_home)

        monkeypatch.setattr(
            IdentityTransactionLock, "is_held_in_process", staticmethod(gated_is_held)
        )

        def call_begin(operation_id: str) -> None:
            try:
                start_barrier.wait(timeout=5.0)
                begun = service.begin(
                    profile=profile,
                    home=home,
                    permit=_permit(home, profile, operation_id=operation_id),
                    lock_fd=None,
                )
                with results_lock:
                    successes.append(begun.operation_id)
            except IdentityWorkerError as error:
                with results_lock:
                    failures.append(error.code)
            except BaseException as error:
                with results_lock:
                    unexpected.append(error)
            finally:
                if threading.get_ident() != winner_tid[0]:
                    loser_done.set()

        t1 = threading.Thread(target=call_begin, args=("op-concurrent-1",), daemon=True)
        t2 = threading.Thread(target=call_begin, args=("op-concurrent-2",), daemon=True)
        try:
            t1.start()
            t2.start()
            t1.join(timeout=10.0)
            t2.join(timeout=10.0)
            assert not t1.is_alive() and not t2.is_alive()
            assert unexpected == []
            assert len(successes) == 1
            assert failures == ["IDENTITY_WORKER_BUSY"]
            assert service._op is not None
            assert service._op.operation_id == successes[0]
            assert len(server.records_for("/.well-known/openid-configuration")) == 1
            assert len(server.records_for("/api/device-auth")) == 1
        finally:
            loser_done.set()
            service.close()
            lock.close()


def test_public_proxy_imports_no_worker_or_credential_module() -> None:
    probe = (
        "import sys\n"
        "import hyprial.identity.impl.onboarding.service\n"
        "forbidden = [\n"
        "    'hyprial.identity.impl.onboarding.worker',\n"
        "    'hyprial.identity.impl.onboarding.credentials',\n"
        "    'hyprial.daemon',\n"
        "    'hyprial.shell',\n"
        "]\n"
        "loaded = [name for name in forbidden if name in sys.modules]\n"
        "assert loaded == [], loaded\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(_REPO_ROOT / "src"),
        },
    )
    assert result.returncode == 0, result.stderr
    assert isinstance(IdentityWorkerProtocolError("x", "y"), IdentityWorkerError)
