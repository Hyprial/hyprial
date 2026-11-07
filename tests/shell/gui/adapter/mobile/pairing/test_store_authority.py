"""S2: the pairing-attempt authority over the real durable store.

These tests bind the S1 ``PairingAuthority`` protocol to the production
SQLite authority.  They exercise attempt uniqueness, code consumption,
idempotent retry/resume, expiry projection and status reads against real
transactions -- not the S1 in-memory fake.  Concurrency is produced with
threads and a start barrier, but the store serialises them through its own
RLock/file-lock/``BEGIN IMMEDIATE`` boundary; no multi-process race is
claimed here.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from hyprial.shell.impl.gui.adapter.mobile import MobileAccessStore
from hyprial.shell.impl.gui.adapter.mobile.models import MobileAccessError
from hyprial.shell.impl.gui.adapter.mobile.pairing import (
    AuthorityError,
    ClientPairingState,
    HostPairingState,
    b64url_encode,
    host_key_fingerprint,
)
from tests.shell.gui.adapter.mobile.helpers import (
    NODE,
    OPERATOR,
    OWNER,
    PEER_A,
    PERMISSIONS,
    make_store,
)

ATTEMPT_A = b64url_encode(bytes(range(32)))
ATTEMPT_B = b64url_encode(bytes(range(32, 64)))
ATTEMPT_C = b64url_encode(bytes(range(64, 96)))


def _attempt(index: int) -> str:
    return b64url_encode((index + 1000).to_bytes(32, "big"))


def _redeem(
    store: MobileAccessStore,
    *,
    code: str | None = None,
    attempt: str = ATTEMPT_A,
    mode: str = "start",
    now: float | None = None,
    **overrides: object,
) -> dict:
    fields: dict[str, object] = {
        "code": code,
        "device_id": "dev-a",
        "device_uid": "uid-a",
        "name": "mate phone",
        "owner": OWNER,
        "client_public": PEER_A,
        "permissions": list(PERMISSIONS),
        "pairing_attempt_id": attempt,
        "mode": mode,
        "now": now,
    }
    fields.update(overrides)
    return dict(store.redeem_pairing_code(**fields))


def _start(store: MobileAccessStore, code: str, **overrides: object) -> dict:
    return _redeem(store, code=code, **overrides)


def _resume(store: MobileAccessStore, **overrides: object) -> dict:
    return _redeem(store, mode="resume", **overrides)


def _status(
    store: MobileAccessStore,
    request_id: str,
    *,
    attempt: str = ATTEMPT_A,
    now: float | None = None,
) -> dict:
    return dict(
        store.pairing_status(
            request_id, pairing_attempt_id=attempt, now=now
        )
    )


def _request_count(store: MobileAccessStore) -> int:
    return int(
        store._connection()
        .execute("SELECT COUNT(*) FROM pairing_requests")
        .fetchone()[0]
    )


def _audit_count(store: MobileAccessStore, event: str) -> int:
    return sum(
        1 for row in store.audit_events(limit=500) if row["event"] == event
    )


def _approve(store: MobileAccessStore, request: dict) -> dict:
    return store.approve_pairing_request(
        request["requestId"],
        operator=OPERATOR,
        node_id=NODE,
        device_id="dev-a",
        client_public=PEER_A,
        fingerprint=request["fingerprint"],
    )


def test_start_retry_is_idempotent_for_one_attempt(tmp_path: Path) -> None:
    store, _clock = make_store(tmp_path)
    try:
        code, _expires = store.issue_pairing_code(operator=OPERATOR)
        first = _start(store, code)
        second = _start(store, code)

        assert first["requestId"] == second["requestId"]
        assert first["status"] == second["status"] == "pending"
        assert first["expiresAt"] == second["expiresAt"]
        assert _request_count(store) == 1
        assert _audit_count(store, "pairing-requested") == 1
        raw = store.inspect_pairing_request(first["requestId"])
        assert raw["status"] == "pending"
    finally:
        store.close()


def test_start_retry_with_other_fields_or_code_conflicts(
    tmp_path: Path,
) -> None:
    store, _clock = make_store(tmp_path)
    try:
        code, _expires = store.issue_pairing_code(operator=OPERATOR)
        first = _start(store, code)
        second_code, _expires = store.issue_pairing_code(operator=OPERATOR)

        with pytest.raises(AuthorityError) as different_code:
            _start(store, second_code)
        assert different_code.value.code == "pairing-attempt-conflict"

        with pytest.raises(AuthorityError) as different_name:
            _start(store, code, name="another phone")
        assert different_name.value.code == "pairing-attempt-conflict"
        assert _request_count(store) == 1

        # The conflicting start did not consume the newly issued code.
        other = _start(store, second_code, attempt=ATTEMPT_B, device_id="dev-b")
        assert other["requestId"] != first["requestId"]
        assert _request_count(store) == 2
    finally:
        store.close()


def test_resume_returns_the_bound_request_and_rejects_other_fields(
    tmp_path: Path,
) -> None:
    store, _clock = make_store(tmp_path)
    try:
        code, _expires = store.issue_pairing_code(operator=OPERATOR)
        first = _start(store, code)

        resumed = _resume(store)
        assert resumed["requestId"] == first["requestId"]
        assert resumed["status"] == "pending"
        assert _request_count(store) == 1

        with pytest.raises(AuthorityError) as conflict:
            _resume(store, owner="agent:other")
        assert conflict.value.code == "pairing-attempt-conflict"

        _approve(store, first)
        approved = _resume(store)
        assert approved["status"] == "approved"
        assert approved["device"]["deviceGeneration"] == 1
        assert type(approved["device"]["approvedAt"]) is int

        # A start retry after approval returns the current projection too.
        retried = _start(store, code)
        assert retried["status"] == "approved"
        assert retried["device"]["deviceId"] == "dev-a"

        store.revoke_device("dev-a", operator=OPERATOR, reason="test")
        revoked = _resume(store)
        assert revoked["status"] == "revoked"
        assert revoked["device"]["status"] == "revoked"
        assert type(revoked["device"]["revokedAt"]) is int
    finally:
        store.close()


def test_resume_without_an_attempt_hit_is_unknown_and_creates_nothing(
    tmp_path: Path,
) -> None:
    store, _clock = make_store(tmp_path)
    try:
        code, _expires = store.issue_pairing_code(operator=OPERATOR)
        with pytest.raises(AuthorityError) as unknown:
            _resume(store, attempt=ATTEMPT_B)
        assert unknown.value.code == "pairing-request-unknown"
        assert _request_count(store) == 0

        # The failed resume must not consume or invalidate the active code.
        request = _start(store, code)
        assert request["status"] == "pending"
        assert _request_count(store) == 1
    finally:
        store.close()


def test_status_forces_expiry_without_writing_and_approve_persists_it(
    tmp_path: Path,
) -> None:
    store, clock = make_store(tmp_path)
    try:
        code, _expires = store.issue_pairing_code(operator=OPERATOR)
        request = _start(store, code)
        request_id = request["requestId"]

        clock.advance(301)
        assert store.inspect_pairing_request(request_id)["status"] == "pending"
        sequence = store.state_sequence()
        before = store.path.read_bytes()

        expired = _status(store, request_id)
        assert expired["status"] == "expired"
        assert store.inspect_pairing_request(request_id)["status"] == "pending"
        assert store.state_sequence() == sequence
        assert store.path.read_bytes() == before

        with pytest.raises(MobileAccessError) as refused:
            _approve(store, request)
        assert refused.value.code == "pairing-request-expired"
        assert store.inspect_pairing_request(request_id)["status"] == "expired"
        assert _status(store, request_id)["status"] == "expired"
    finally:
        store.close()


def test_status_projects_rejection_and_is_unknown_for_unbound_attempts(
    tmp_path: Path,
) -> None:
    store, _clock = make_store(tmp_path)
    try:
        code, _expires = store.issue_pairing_code(operator=OPERATOR)
        request = _start(store, code)
        request_id = request["requestId"]

        with pytest.raises(AuthorityError) as wrong_attempt:
            _status(store, request_id, attempt=ATTEMPT_B)
        assert wrong_attempt.value.code == "pairing-request-unknown"

        with pytest.raises(AuthorityError) as missing:
            _status(store, "pair-missing")
        assert missing.value.code == "pairing-request-unknown"

        store.reject_pairing_request(
            request_id, operator=OPERATOR, node_id=NODE
        )
        rejected = _status(store, request_id)
        assert rejected["status"] == "rejected"
        assert type(rejected["rejectedAt"]) is int
        assert rejected["requestedAt"] == int(request["requestedAt"])
        assert rejected["expiresAt"] == int(request["expiresAt"])
    finally:
        store.close()


def test_request_ttl_is_bounded_by_the_code_expiry(tmp_path: Path) -> None:
    store, clock = make_store(tmp_path)
    try:
        code, code_expires = store.issue_pairing_code(operator=OPERATOR)
        clock.advance(100)
        request = _start(store, code)
        assert request["requestedAt"] == int(clock.now)
        assert request["expiresAt"] == int(code_expires)
    finally:
        store.close()


def test_concurrent_same_attempt_yields_one_request(tmp_path: Path) -> None:
    store, _clock = make_store(tmp_path)
    try:
        code, _expires = store.issue_pairing_code(operator=OPERATOR)
        barrier = threading.Barrier(8)

        def redeem(_index: int) -> str:
            barrier.wait()
            try:
                return _start(store, code)["requestId"]
            except AuthorityError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(redeem, range(8)))

        assert len(set(outcomes)) == 1
        assert outcomes[0].startswith("pair-")
        assert _request_count(store) == 1
        assert _audit_count(store, "pairing-requested") == 1
    finally:
        store.close()


def test_concurrent_attempts_on_one_code_have_one_winner(
    tmp_path: Path,
) -> None:
    store, _clock = make_store(tmp_path)
    try:
        code, _expires = store.issue_pairing_code(operator=OPERATOR)
        barrier = threading.Barrier(8)

        def redeem(index: int) -> tuple[str, str]:
            barrier.wait()
            try:
                return "ok", _start(
                    store, code, attempt=_attempt(index)
                )["requestId"]
            except AuthorityError as error:
                return "error", error.code

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(redeem, range(8)))

        winners = [value for kind, value in outcomes if kind == "ok"]
        losers = [value for kind, value in outcomes if kind == "error"]
        assert len(winners) == 1
        assert len(losers) == 7
        assert set(losers) <= {"pairing-code-consumed", "pairing-code-invalid"}
        assert _request_count(store) == 1
    finally:
        store.close()


def test_attempt_is_hashed_and_never_stored_in_plaintext(
    tmp_path: Path,
) -> None:
    store, _clock = make_store(tmp_path)
    try:
        code, _expires = store.issue_pairing_code(operator=OPERATOR)
        request = _start(store, code)
        database_path = store.path
        request_id = request["requestId"]
    finally:
        store.close()

    raw = database_path.read_bytes()
    assert ATTEMPT_A.encode("ascii") not in raw
    assert code.encode("ascii") not in raw

    connection = sqlite3.connect(database_path)
    try:
        row = connection.execute(
            "SELECT pairing_attempt_hash, code_hash FROM pairing_requests "
            "WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        assert row[0] == hashlib.sha256(ATTEMPT_A.encode("ascii")).hexdigest()
        assert row[1] and row[1] != hashlib.sha256(code.encode("ascii")).hexdigest()
        index = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' "
            "AND name = 'pairing_request_attempt_hash_unique'"
        ).fetchone()
        assert index is not None
        assert "UNIQUE" in index[0].upper()
        assert "WHERE pairing_attempt_hash IS NOT NULL" in index[0]
    finally:
        connection.close()


_LEGACY_DATABASE = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE pairing_requests (
  request_id TEXT PRIMARY KEY,
  device_id TEXT NOT NULL,
  device_uid TEXT NOT NULL,
  name TEXT NOT NULL,
  owner TEXT NOT NULL,
  client_public TEXT NOT NULL,
  permissions TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  node_id TEXT NOT NULL,
  requested_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  status TEXT NOT NULL,
  approved_at REAL,
  approved_by TEXT,
  rejected_at REAL,
  rejected_by TEXT
);
"""


def test_old_schema_is_migrated_forward_without_losing_rows(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "mobile-access.sqlite3"
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(_LEGACY_DATABASE)
        connection.execute(
            "INSERT INTO meta(key, value) VALUES('node_id', ?)", (NODE,)
        )
        connection.execute(
            "INSERT INTO meta(key, value) VALUES('state_seq', '0')"
        )
        connection.execute(
            "INSERT INTO pairing_requests(request_id, device_id, device_uid, "
            "name, owner, client_public, permissions, fingerprint, node_id, "
            "requested_at, expires_at, status) VALUES(?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, 'pending')",
            (
                "pair-legacy",
                "dev-old",
                "uid-old",
                "old phone",
                OWNER,
                PEER_A,
                '["sessions.read"]',
                "0" * 64,
                NODE,
                1000.0,
                1300.0,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    Path(f"{database_path}.epoch").write_text("0", encoding="ascii")

    store = MobileAccessStore(database_path, node_id=NODE)
    try:
        legacy = store.inspect_pairing_request("pair-legacy")
        assert legacy["status"] == "pending"
        assert legacy["requestedAt"] == 1000.0
        with pytest.raises(AuthorityError) as unknown:
            _status(store, "pair-legacy")
        assert unknown.value.code == "pairing-request-unknown"

        # The legacy redemption path still works after the additive migration.
        legacy_code, _expires = store.issue_pairing_code(operator=OPERATOR)
        legacy_request = store.redeem_pairing_code(
            code=legacy_code,
            device_id="dev-legacy",
            device_uid="uid-legacy",
            name="legacy phone",
            owner=OWNER,
            client_public=PEER_A,
        )
        assert legacy_request["status"] == "pending"

        # And the attempt-aware path is available on the same migrated file.
        code, _expires = store.issue_pairing_code(operator=OPERATOR)
        request = _start(store, code, device_id="dev-new", device_uid="uid-new")
        assert request["status"] == "pending"
        assert _status(store, request["requestId"])["status"] == "pending"
    finally:
        store.close()

    connection = sqlite3.connect(database_path)
    try:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(pairing_requests)"
            ).fetchall()
        }
        assert {"pairing_attempt_hash", "code_hash"} <= columns
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND tbl_name = 'pairing_requests'"
            ).fetchall()
        }
        assert "pairing_request_attempt_hash_unique" in indexes
    finally:
        connection.close()


def test_attempt_hit_precedes_the_code_guess_lockout(tmp_path: Path) -> None:
    store, _clock = make_store(tmp_path)
    try:
        code, _expires = store.issue_pairing_code(operator=OPERATOR)
        request = _start(store, code)

        for index in range(10):
            with pytest.raises(AuthorityError):
                _start(store, "ZZZZZZZZZZ", attempt=_attempt(index))

        with pytest.raises(AuthorityError) as limited:
            _start(store, "ZZZZZZZZZZ", attempt=_attempt(99))
        assert limited.value.code == "pairing-rate-limited"

        resumed = _resume(store)
        assert resumed["requestId"] == request["requestId"]
        assert resumed["status"] == "pending"
    finally:
        store.close()


def test_s1_state_machine_runs_against_the_real_store(tmp_path: Path) -> None:
    store, clock = make_store(tmp_path)
    try:
        signing_key = ec.generate_private_key(ec.SECP256R1())
        derive_key = ec.generate_private_key(ec.SECP256R1())
        client_key = ec.generate_private_key(ec.SECP256R1())
        pin = host_key_fingerprint(b"unit-test-host-spki")
        host = HostPairingState(
            node_id=NODE,
            host_key_fingerprint=pin,
            host_signing_key=signing_key,
            host_derive_key=derive_key,
            authority=store,
            clock=clock,
        )
        client = ClientPairingState(
            node_id=NODE,
            host_key_fingerprint=pin,
            host_public_key=signing_key.public_key(),
            client_private_key=client_key,
            clock=clock,
        )
        code, _expires = store.issue_pairing_code(operator=OPERATOR)

        hello = client.hello()
        challenge = host.challenge(hello)
        client.accept_challenge(challenge)
        redeem = client.redeem_start(
            code=code,
            device_id="dev-a",
            device_uid="uid-a",
            name="mate phone",
            owner=OWNER,
            permissions=list(PERMISSIONS),
        )
        pending = client.accept_status(host.redeem(hello, challenge, redeem))
        assert pending["status"] == "pending"
        assert pending["requestId"].startswith("pair-")
        request_id = pending["requestId"]

        hello_2 = client.hello()
        challenge_2 = host.challenge(hello_2)
        client.accept_challenge(challenge_2)
        polled = client.accept_status(
            host.status(challenge_2, client.status_query(request_id))
        )
        assert polled["status"] == "pending"
        assert polled["pollAfterMs"] == 2000

        store.approve_pairing_request(
            request_id,
            operator=OPERATOR,
            node_id=NODE,
            device_id="dev-a",
            client_public=hello["clientPublic"],
            fingerprint=pending["fingerprint"],
        )

        hello_3 = client.hello()
        challenge_3 = host.challenge(hello_3)
        client.accept_challenge(challenge_3)
        approved = client.accept_status(
            host.status(challenge_3, client.status_query(request_id))
        )
        assert approved["status"] == "approved"
        assert approved["device"]["deviceId"] == "dev-a"
        assert approved["device"]["deviceGeneration"] == 1
        assert approved["device"]["targetNodeId"] == NODE
        assert approved["device"]["permissions"] == sorted(PERMISSIONS)

        store.revoke_device("dev-a", operator=OPERATOR, reason="unit-test")

        hello_4 = client.hello()
        challenge_4 = host.challenge(hello_4)
        client.accept_challenge(challenge_4)
        revoked = client.accept_status(
            host.status(challenge_4, client.status_query(request_id))
        )
        assert revoked["status"] == "revoked"
        assert revoked["device"]["status"] == "revoked"
        assert type(revoked["device"]["revokedAt"]) is int
    finally:
        store.close()
