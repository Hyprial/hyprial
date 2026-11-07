"""Acceptance tests for the durable mobile-access authority.

These are failing-first tests for the T1/T2/T3/T6 decisions: the pairing
lifecycle, revocation, the persistent replay window and explicit session
grants.  Every test runs the real SQLite authority on a temporary database;
only the request-key derivation is a deterministic stand-in for the external
sidecar seam.
"""

from __future__ import annotations

import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from hyprial.shell.impl.gui.adapter.mobile.store import (
    MobileAccessError,
    MobileAccessStore,
    pairing_fingerprint,
)
from tests.shell.gui.adapter.mobile.helpers import (
    NODE,
    OPERATOR,
    OTHER_OWNER,
    OWNER,
    PEER_A,
    PEER_B,
    make_store,
    pair_device,
)


def _code(exception: pytest.ExceptionInfo[MobileAccessError]) -> str:
    return exception.value.code


# -- T1: pairing is explicit, short-lived and one-time ----------------------


def test_pairing_code_is_one_time_and_never_stored_in_plaintext(
    tmp_path: Path,
) -> None:
    store, _clock = make_store(tmp_path)
    try:
        code, _expires_at = store.issue_pairing_code(operator=OPERATOR)
        request = store.redeem_pairing_code(
            code=code.lower(),
            device_id="dev-a",
            device_uid="uid-a",
            name="mate phone",
            owner=OWNER,
            client_public=PEER_A,
        )
        assert request["status"] == "pending"
        store.approve_pairing_request(
            request["requestId"],
            operator=OPERATOR,
            node_id=NODE,
            device_id="dev-a",
            client_public=PEER_A,
            fingerprint=request["fingerprint"],
        )
        assert code.encode("ascii") not in store._path.read_bytes()

        with pytest.raises(MobileAccessError) as first:
            store.redeem_pairing_code(
                code=code,
                device_id="dev-b",
                device_uid="uid-b",
                name="other phone",
                owner=OWNER,
                client_public=PEER_B,
            )
        assert _code(first) == "pairing-code-consumed"
    finally:
        store.close()


def test_wrong_expired_and_attempt_limited_codes_are_refused(
    tmp_path: Path,
) -> None:
    store, clock = make_store(tmp_path)
    try:
        code, _ = store.issue_pairing_code(operator=OPERATOR)
        for _ in range(5):
            with pytest.raises(MobileAccessError) as wrong:
                store.redeem_pairing_code(
                    code="ZZZZZZZZZZ",
                    device_id="dev-a",
                    device_uid="uid-a",
                    name="mate phone",
                    owner=OWNER,
                    client_public=PEER_A,
                )
            assert _code(wrong) == "pairing-code-invalid"
        with pytest.raises(MobileAccessError) as dead:
            store.redeem_pairing_code(
                code=code,
                device_id="dev-a",
                device_uid="uid-a",
                name="mate phone",
                owner=OWNER,
                client_public=PEER_A,
            )
        assert _code(dead) in {"pairing-code-invalid", "pairing-code-expired"}

        fresh, _ = store.issue_pairing_code(operator=OPERATOR)
        clock.advance(301)
        with pytest.raises(MobileAccessError) as expired:
            store.redeem_pairing_code(
                code=fresh,
                device_id="dev-b",
                device_uid="uid-b",
                name="other phone",
                owner=OWNER,
                client_public=PEER_B,
            )
        assert _code(expired) == "pairing-code-expired"

        store.issue_pairing_code(operator=OPERATOR)
        for _ in range(10):
            with pytest.raises(MobileAccessError):
                store.redeem_pairing_code(
                    code="YYYYYYYYYY",
                    device_id="dev-c",
                    device_uid="uid-c",
                    name="third phone",
                    owner=OWNER,
                    client_public=PEER_B,
                )
        with pytest.raises(MobileAccessError) as limited:
            store.redeem_pairing_code(
                code="YYYYYYYYYY",
                device_id="dev-c",
                device_uid="uid-c",
                name="third phone",
                owner=OWNER,
                client_public=PEER_B,
            )
        assert _code(limited) == "pairing-rate-limited"
    finally:
        store.close()


def test_pending_request_confers_no_business_permission(tmp_path: Path) -> None:
    store, _clock = make_store(tmp_path)
    try:
        code, _ = store.issue_pairing_code(operator=OPERATOR)
        request = store.redeem_pairing_code(
            code=code,
            device_id="dev-a",
            device_uid="uid-a",
            name="mate phone",
            owner=OWNER,
            client_public=PEER_A,
        )
        assert store.device_row("dev-a") is None
        assert store.connection_allowed("dev-a") is False
        with pytest.raises(MobileAccessError) as unknown:
            store.device_status("dev-a")
        assert _code(unknown) == "device-unknown"
        with pytest.raises(MobileAccessError):
            store.check_session_access(
                "dev-a",
                1,
                operation="sessions.list",
                session_id=None,
                author=OWNER,
            )
        assert request["fingerprint"] == pairing_fingerprint(
            node_id=NODE,
            device_id="dev-a",
            device_uid="uid-a",
            name="mate phone",
            owner=OWNER,
            client_public=PEER_A,
            permissions=["sessions.read", "sessions.write", "sessions.events"],
        )
    finally:
        store.close()


def test_approval_requires_the_exact_binding_and_is_single_use(
    tmp_path: Path,
) -> None:
    store, _clock = make_store(tmp_path)
    try:
        code, _ = store.issue_pairing_code(operator=OPERATOR)
        request = store.redeem_pairing_code(
            code=code,
            device_id="dev-a",
            device_uid="uid-a",
            name="mate phone",
            owner=OWNER,
            client_public=PEER_A,
        )
        attempt = dict(
            operator=OPERATOR,
            node_id=NODE,
            device_id="dev-a",
            client_public=PEER_A,
            fingerprint=request["fingerprint"],
        )
        with pytest.raises(MobileAccessError) as wrong_host:
            store.approve_pairing_request(
                request["requestId"], **{**attempt, "node_id": "node:elsewhere"}
            )
        assert _code(wrong_host) == "pairing-host-mismatch"
        with pytest.raises(MobileAccessError) as wrong_device:
            store.approve_pairing_request(
                request["requestId"], **{**attempt, "device_id": "dev-b"}
            )
        assert _code(wrong_device) == "pairing-device-mismatch"
        with pytest.raises(MobileAccessError) as wrong_fingerprint:
            store.approve_pairing_request(
                request["requestId"], **{**attempt, "fingerprint": "0" * 64}
            )
        assert _code(wrong_fingerprint) == "pairing-fingerprint-mismatch"
        assert store.inspect_pairing_request(request["requestId"])["status"] == "pending"

        device = store.approve_pairing_request(request["requestId"], **attempt)
        assert device["status"] == "active"
        assert device["keyGeneration"] == 1
        assert device["clientPublic"] == PEER_A

        with pytest.raises(MobileAccessError) as replay:
            store.approve_pairing_request(request["requestId"], **attempt)
        assert _code(replay) == "pairing-request-not-pending"
    finally:
        store.close()


def test_concurrent_redemption_has_exactly_one_winner(tmp_path: Path) -> None:
    store, _clock = make_store(tmp_path)
    try:
        code, _ = store.issue_pairing_code(operator=OPERATOR)

        def redeem(index: int) -> str:
            try:
                store.redeem_pairing_code(
                    code=code,
                    device_id=f"dev-{index}",
                    device_uid=f"uid-{index}",
                    name=f"phone {index}",
                    owner=OWNER,
                    client_public=PEER_A,
                )
            except MobileAccessError as error:
                return error.code
            return "ok"

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(redeem, range(8)))
        assert outcomes.count("ok") == 1
        assert all(
            outcome in {"ok", "pairing-code-consumed", "pairing-code-invalid"}
            for outcome in outcomes
        )
    finally:
        store.close()


# -- T2: revocation is a committed boundary, not cached state ---------------


def test_revocation_commits_t0_and_denies_immediately(tmp_path: Path) -> None:
    store, clock = make_store(tmp_path)
    try:
        pair_device(store)
        store.consume_nonce("dev-a", 1, "before", 1_000_000)
        assert store.connection_allowed("dev-a") is True

        clock.advance(5)
        revoked = store.revoke_device(
            "dev-a", operator=OPERATOR, reason="operator revoked the phone"
        )
        assert revoked["t0"] == 1_000_005.0
        assert revoked["alreadyRevoked"] is False
        assert store.connection_allowed("dev-a") is False
        with pytest.raises(MobileAccessError) as status:
            store.consume_nonce("dev-a", 1, "after", 1_000_006)
        assert _code(status) == "device-revoked"
        row = store.device_row("dev-a")
        assert row is not None and row.status == "revoked"

        events = store.audit_events(device_id="dev-a")
        revocations = [event for event in events if event["event"] == "device-revoked"]
        assert revocations and revocations[0]["at"] == revoked["t0"]
        assert store.revoke_device(
            "dev-a", operator=OPERATOR, reason="again"
        )["alreadyRevoked"] is True
    finally:
        store.close()


def test_repair_requires_a_new_approval_and_old_generation_stays_dead(
    tmp_path: Path,
) -> None:
    store, clock = make_store(tmp_path)
    try:
        pair_device(store)
        store.consume_nonce("dev-a", 1, "gen-1", 1_000_000)
        store.revoke_device("dev-a", operator=OPERATOR, reason="lost phone")
        # A revoked device cannot be resurrected merely by presenting its old
        # request; a fresh pairing code and approval are required.
        with pytest.raises(MobileAccessError):
            store.consume_nonce("dev-a", 1, "gen-1-again", 1_000_001)

        clock.advance(10)
        repaired = pair_device(store, device_id="dev-a", name="replacement phone")
        assert repaired["keyGeneration"] == 2
        with pytest.raises(MobileAccessError) as stale:
            store.consume_nonce("dev-a", 1, "old-generation", 1_000_010)
        assert _code(stale) == "auth-device-generation-stale"
        store.consume_nonce("dev-a", 2, "new-generation", 1_000_010)
        with pytest.raises(MobileAccessError) as duplicate:
            store.consume_nonce("dev-a", 2, "new-generation", 1_000_010)
        assert _code(duplicate) == "auth-nonce-replayed"
    finally:
        store.close()


def test_revocation_survives_host_restart_and_repaired_credentials_are_new(
    tmp_path: Path,
) -> None:
    store, clock = make_store(tmp_path)
    pair_device(store)
    store.grant_session(
        "dev-a", "session-1", session_owner=OWNER, operator=OPERATOR
    )
    revoked = store.revoke_device("dev-a", operator=OPERATOR, reason="lost phone")
    store.close()

    reopened = MobileAccessStore(store._path, node_id=NODE, clock=clock)
    try:
        assert reopened.connection_allowed("dev-a") is False, (
            "the tombstone is authoritative after a host restart"
        )
        with pytest.raises(MobileAccessError) as refused_nonce:
            reopened.consume_nonce("dev-a", 1, "after-restart", 1_000_001)
        assert _code(refused_nonce) == "device-revoked"
        with pytest.raises(MobileAccessError) as refused_session:
            reopened.check_session_access(
                "dev-a",
                1,
                operation="sessions.history",
                session_id="session-1",
                author=OWNER,
            )
        assert _code(refused_session) == "device-revoked"
        row = reopened.device_status("dev-a")
        assert row["status"] == "revoked"
        assert row["revokedAt"] == revoked["t0"]
        assert reopened.list_grants("dev-a")[0]["revokedAt"] == revoked["t0"], (
            "cancelling the device also cancels its grants durably"
        )

        clock.advance(10)
        repaired = pair_device(
            reopened, device_id="dev-a", name="replacement phone"
        )
        assert repaired["keyGeneration"] == 2
        with pytest.raises(MobileAccessError) as stale:
            reopened.consume_nonce("dev-a", 1, "old-generation", 1_000_010)
        assert _code(stale) == "auth-device-generation-stale"
        reopened.consume_nonce("dev-a", 2, "new-generation", 1_000_010)

        events = reopened.audit_events(device_id="dev-a")
        assert any(event["event"] == "device-revoked" for event in events)
        approved = [
            event for event in events if event["event"] == "pairing-approved"
        ]
        assert approved and approved[0]["detail"]["keyGeneration"] == 2, (
            "the new approval is auditable and does not erase the old revocation"
        )
    finally:
        reopened.close()


def test_admission_and_revocation_are_serialized_at_t0(tmp_path: Path) -> None:
    store, _clock = make_store(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    revoked = threading.Event()
    try:
        pair_device(store)
        store.grant_session(
            "dev-a", "session-1", session_owner=OWNER, operator=OPERATOR
        )

        def admitted() -> str:
            with store.admission(
                "dev-a",
                1,
                operation="sessions.send",
                session_id="session-1",
                author=OWNER,
            ) as context:
                assert context["sharing"] == "isolated"
                entered.set()
                assert release.wait(5)
            return "admitted"

        def revoker() -> str:
            assert entered.wait(5)
            store.revoke_device("dev-a", operator=OPERATOR, reason="t0")
            revoked.set()
            return "revoked"

        with ThreadPoolExecutor(max_workers=2) as pool:
            admission_future = pool.submit(admitted)
            revoke_future = pool.submit(revoker)
            assert entered.wait(5)
            time.sleep(0.1)
            assert not revoked.is_set(), (
                "revocation must not commit between admission's check and its "
                "side effect"
            )
            release.set()
            assert admission_future.result(timeout=5) == "admitted"
            assert revoke_future.result(timeout=5) == "revoked"

        with pytest.raises(MobileAccessError) as refused:
            with store.admission(
                "dev-a",
                1,
                operation="sessions.send",
                session_id="session-1",
                author=OWNER,
            ):
                pytest.fail("a revoked device must never enter admission")
        assert _code(refused) == "device-revoked"
    finally:
        store.close()


def test_storage_faults_and_clock_rollback_fail_closed(tmp_path: Path) -> None:
    store, clock = make_store(tmp_path)
    try:
        pair_device(store)
        clock.advance(-10)
        with pytest.raises(MobileAccessError) as anomaly:
            store.issue_pairing_code(operator=OPERATOR)
        assert _code(anomaly) == "auth-clock-anomaly"
    finally:
        store.close()

    backup = tmp_path / "mobile-access.sqlite3.backup"
    shutil.copy2(store._path, backup)
    reopened = MobileAccessStore(store._path, node_id=NODE)
    try:
        reopened.revoke_device("dev-a", operator=OPERATOR, reason="newer state")
    finally:
        reopened.close()
    shutil.copy2(backup, store._path)
    with pytest.raises(MobileAccessError) as rolled_back:
        MobileAccessStore(store._path, node_id=NODE)
    assert _code(rolled_back) == "access-state-rolled-back"

    closed, _ = make_store(tmp_path / "closed")
    closed.close()
    with pytest.raises(MobileAccessError) as unavailable:
        closed.device_row("dev-a")
    assert _code(unavailable) == "access-store-unavailable"
    assert closed.connection_allowed("dev-a") is False


# -- T3: the replay window survives restart --------------------------------


def test_nonce_replay_is_refused_across_restart(tmp_path: Path) -> None:
    store, clock = make_store(tmp_path)
    pair_device(store)
    store.consume_nonce("dev-a", 1, "persistent", 1_000_000)
    store.close()

    reopened = MobileAccessStore(store._path, node_id=NODE, clock=clock)
    try:
        with pytest.raises(MobileAccessError) as replay:
            reopened.consume_nonce("dev-a", 1, "persistent", 1_000_000)
        assert _code(replay) == "auth-nonce-replayed"
        reopened.consume_nonce("dev-a", 1, "fresh", 1_000_001)
        assert reopened.device_row("dev-a") is not None
    finally:
        reopened.close()


def test_concurrent_nonce_consumption_has_one_winner(tmp_path: Path) -> None:
    store, _clock = make_store(tmp_path)
    try:
        pair_device(store)

        def consume(_index: int) -> str:
            try:
                store.consume_nonce("dev-a", 1, "same-nonce", 1_000_000)
            except MobileAccessError as error:
                return error.code
            return "ok"

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(consume, range(8)))
        assert outcomes.count("ok") == 1
        assert outcomes.count("auth-nonce-replayed") == 7
    finally:
        store.close()


# -- T6: explicit grants, isolation, sharing and source attribution ---------


def test_approved_devices_start_with_only_explicitly_granted_sessions(
    tmp_path: Path,
) -> None:
    store, _clock = make_store(tmp_path)
    try:
        pair_device(store, device_id="dev-a", client_public=PEER_A)
        pair_device(
            store,
            device_id="dev-b",
            device_uid="uid-b",
            client_public=PEER_B,
            name="tablet",
        )
        store.grant_session(
            "dev-a", "session-1", session_owner=OWNER, operator=OPERATOR
        )

        assert store.check_session_access(
            "dev-a",
            1,
            operation="sessions.history",
            session_id="session-1",
            author=OWNER,
        ) == "isolated"
        with pytest.raises(MobileAccessError) as isolated:
            store.check_session_access(
                "dev-b",
                1,
                operation="sessions.history",
                session_id="session-1",
                author=OWNER,
            )
        assert _code(isolated) == "auth-session-not-granted"
        with pytest.raises(MobileAccessError) as other_session:
            store.check_session_access(
                "dev-a",
                1,
                operation="sessions.events",
                session_id="session-2",
                author=OWNER,
            )
        assert _code(other_session) == "auth-session-not-granted"
        with pytest.raises(MobileAccessError) as wrong_owner:
            store.grant_session(
                "dev-a", "session-2", session_owner=OTHER_OWNER, operator=OPERATOR
            )
        assert _code(wrong_owner) == "owner-mismatch"
        with pytest.raises(MobileAccessError) as wrong_author:
            store.check_session_access(
                "dev-a",
                1,
                operation="sessions.history",
                session_id="session-1",
                author=OTHER_OWNER,
            )
        assert _code(wrong_author) == "auth-not-authorized"
    finally:
        store.close()


def test_explicit_sharing_preserves_source_attribution_and_isolation(
    tmp_path: Path,
) -> None:
    store, _clock = make_store(tmp_path)
    try:
        pair_device(store, device_id="dev-a", client_public=PEER_A)
        pair_device(
            store,
            device_id="dev-b",
            device_uid="uid-b",
            client_public=PEER_B,
            name="tablet",
        )
        store.grant_session(
            "dev-a", "session-1", session_owner=OWNER, operator=OPERATOR
        )
        store.grant_session(
            "dev-a", "session-2", session_owner=OWNER, operator=OPERATOR
        )
        store.grant_session(
            "dev-b",
            "session-1",
            session_owner=OWNER,
            operator=OPERATOR,
            source_device_id="dev-a",
        )

        with pytest.raises(MobileAccessError):
            # Sharing is explicit per session: session-2 was never shared.
            store.check_session_access(
                "dev-b",
                1,
                operation="sessions.history",
                session_id="session-2",
                author=OWNER,
            )

        with store.admission(
            "dev-b",
            1,
            operation="sessions.history",
            session_id="session-1",
            author=OWNER,
        ) as context:
            assert context["sharing"] == "shared"
            assert context["sourceDeviceId"] == "dev-a"

        events = store.audit_events(device_id="dev-b", session_id="session-1")
        admitted = [event for event in events if event["event"] == "request-admitted"]
        assert admitted[0]["sharing"] == "shared"
        assert admitted[0]["sourceDeviceId"] == "dev-a"
        assert admitted[0]["status"] == "completed"

        with pytest.raises(MobileAccessError):
            store.grant_session(
                "dev-b",
                "session-1",
                session_owner=OWNER,
                operator=OPERATOR,
                source_device_id="dev-b",
            )
    finally:
        store.close()


def test_device_revocation_keeps_other_devices_history_and_grants(
    tmp_path: Path,
) -> None:
    store, _clock = make_store(tmp_path)
    try:
        pair_device(store, device_id="dev-a", client_public=PEER_A)
        pair_device(
            store,
            device_id="dev-b",
            device_uid="uid-b",
            client_public=PEER_B,
            name="tablet",
        )
        for device_id in ("dev-a", "dev-b"):
            store.grant_session(
                device_id, "session-1", session_owner=OWNER, operator=OPERATOR
            )

        store.revoke_device("dev-a", operator=OPERATOR, reason="lost phone")

        assert store.connection_allowed("dev-b") is True
        assert store.check_session_access(
            "dev-b",
            1,
            operation="sessions.history",
            session_id="session-1",
            author=OWNER,
        ) == "isolated"
        assert store.list_grants("dev-b")[0]["revokedAt"] is None
        assert store.list_grants("dev-a")[0]["revokedAt"] is not None
        device_a_events = store.audit_events(device_id="dev-a")
        assert any(event["event"] == "device-revoked" for event in device_a_events)
        assert any(event["event"] == "pairing-approved" for event in device_a_events)
    finally:
        store.close()
