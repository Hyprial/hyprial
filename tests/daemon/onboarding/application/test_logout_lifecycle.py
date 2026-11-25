"""The serving port's daemon-owned logout transaction.

Every case runs on the real ``OperationService`` journal and the real
``secrets/login.json`` + ``settings.json`` shape, inside ``tmp_path``.  No daemon,
socket, credential worker or home outside the test directory is touched, and
nothing reads the credential bytes.

Scope note: the daemon is the single onboarding authority and the one journal
writer, so these cases assert the full daemon-side transaction — the durable
acceptance, the identity-owned removal, the truthful terminal receipt, the
replay, the concurrency guards, the crash reconciliation and the daemon's own
stop request.  The launcher keeps no custody coordinator.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from hyprial.daemon.impl.application.onboarding.account_lifecycle import (
    ServingOnboardingGateway,
)
from hyprial.daemon.impl.application.onboarding.errors import (
    OnboardingGatewayError,
)
from hyprial.daemon.impl.onboarding.operations import OperationService
from hyprial.daemon.impl.onboarding.operations import journal
from hyprial.daemon.impl.onboarding.operations.journal import (
    OPERATION_STORE_CORRUPT,
    HandoffRecord,
    OperationReceiptError,
)
from hyprial.daemon.impl.request_catalog.onboarding import (
    AUTH_OPERATION_CONFLICT,
    AUTH_OPERATION_NOT_FOUND,
    CallContext,
    Surface,
)
from hyprial.identity import (
    IdentitySnapshot,
    inspect_logout_artifacts,
    remove_logout_artifacts,
)
from hyprial.kernel import ipc_errors

ISSUER = "https://issuer.example.test"
OWNER = "alice@example.test"
LOGOUT_KEY = "logout-key-0000001"

_SETTINGS_FIELDS = {
    "identityIssuer": ISSUER,
    "identityMode": "casdoor",
    "owner": "alice",
    "userId": "subject-1",
    "username": "alice",
}

_NOW = 1_700_000_000.0


def _surface(generation: str = "serving-1") -> Surface:
    return Surface("home-binding", "host-control", "view", generation)


def _context(
    *, owner: str | None = OWNER, generation: str = "serving-1"
) -> CallContext:
    return CallContext(
        surface=_surface(generation), phase="serving", caller="local-gui", owner=owner
    )


def _seed_home(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(
        json.dumps({**_SETTINGS_FIELDS, "unrelated": {"keep": True}}), encoding="utf-8"
    )
    (home / "secrets").mkdir(exist_ok=True)
    (home / "secrets" / "login.json").write_text(
        json.dumps({"version": 1, "issuer": ISSUER, "refreshToken": "token-abc"}),
        encoding="utf-8",
    )


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _journal_states(root: Path) -> dict[str, str]:
    state = journal.read_journal(root)
    if state is None:
        return {}
    return {record.operation_id: record.state for record in state.snapshot.records}


class RecordingArtifacts:
    """The real identity primitives, recording every call."""

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def inspect(self, home: Path):
        self.calls.append("inspect")
        return inspect_logout_artifacts(home)

    def remove(self, home: Path):
        self.calls.append("remove")
        return remove_logout_artifacts(home)


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        owner: str | None = OWNER,
        seed: bool = True,
        request_stop=True,
    ) -> None:
        self.calls: list[str] = []
        self.stop_requests: list[str] = []
        self.home = tmp_path / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        if seed:
            _seed_home(self.home)
        self.root = tmp_path / "operations"
        self.service = OperationService.open(self.root)
        self.identity = IdentitySnapshot(owner=owner, mode="casdoor", issuer=ISSUER)
        self.artifacts = RecordingArtifacts(self.calls)
        self.gateway = ServingOnboardingGateway(
            operations=self.service,
            home=self.home,
            identity_reader=lambda _home: self.identity,
            device_reader=lambda _home: None,
            journal_reader=journal.read_journal,
            logout_artifacts=self.artifacts,
            clock=lambda: _NOW,
            request_stop=(
                (lambda: self.stop_requests.append("auth-logout"))
                if request_stop
                else None
            ),
        )

    def seed_accepted(
        self, *, key: str = LOGOUT_KEY, operation_id: str = "operation-seeded-1"
    ):
        """Seed one durable accepted receipt without running the transaction."""

        return self.service.begin_logout(
            _context(owner=self.identity.owner), key, operation_id, "receipt-seeded-1"
        )

    def logout(self, *, key: str = LOGOUT_KEY, confirm: bool = True, context=None,
               expected_epoch: str | None = None):
        return self.gateway.logout(
            context if context is not None else _context(owner=self.identity.owner),
            operation_key=key,
            confirm=confirm,
            expected_epoch=expected_epoch,
        )


def test_a_logout_removes_login_artifacts_and_completes(tmp_path: Path) -> None:
    harness = Harness(tmp_path)

    result = harness.logout()

    assert result["state"] == "completed"
    assert result["loggedOut"] is True
    assert result["removed"] == [
        "secrets/login.json",
        "settings.identityIssuer",
        "settings.identityMode",
        "settings.owner",
        "settings.userId",
        "settings.username",
    ]
    assert result["nextAction"] == "login"
    assert result["issuerRevocation"] == "not-attempted"
    assert json.loads((harness.home / "settings.json").read_text(encoding="utf-8")) == {
        "unrelated": {"keep": True}
    }
    assert not (harness.home / "secrets" / "login.json").exists()
    assert harness.calls == ["inspect", "remove"]
    assert _journal_states(harness.root) == {result["operationId"]: "completed"}
    assert harness.stop_requests == ["auth-logout"]


def test_replay_returns_the_receipt_without_a_second_side_effect(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)

    first = harness.logout()
    calls_after_first = list(harness.calls)
    second = harness.logout()
    third = harness.logout(key="another-logout-key-1")

    assert second["operationId"] == first["operationId"]
    assert third["operationId"] == first["operationId"]
    assert second["state"] == "completed"
    assert harness.calls == calls_after_first
    assert len(_journal_states(harness.root)) == 1
    assert len(harness.stop_requests) == 1


def test_empty_home_completes_without_any_side_effect(tmp_path: Path) -> None:
    harness = Harness(tmp_path, seed=False)

    result = harness.logout()

    assert result["state"] == "completed"
    assert result["loggedOut"] is True
    assert result["removed"] == []
    assert harness.calls == ["inspect"]
    assert _journal_states(harness.root) == {result["operationId"]: "completed"}
    assert harness.stop_requests == []


def test_unreadable_settings_refuse_before_any_receipt(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    (harness.home / "settings.json").write_text("{broken", encoding="utf-8")

    with pytest.raises(OnboardingGatewayError) as caught:
        harness.logout()

    assert caught.value.code == ipc_errors.IDENTITY_SOURCE_UNAVAILABLE
    assert _journal_states(harness.root) == {}
    assert (harness.home / "secrets" / "login.json").exists()


def test_logout_requires_the_explicit_confirm_literal(tmp_path: Path) -> None:
    harness = Harness(tmp_path)

    with pytest.raises(OnboardingGatewayError) as caught:
        harness.logout(confirm=False)

    assert caught.value.code == ipc_errors.INVALID_ARGUMENT
    assert _journal_states(harness.root) == {}
    assert harness.calls == []


def test_a_stale_expected_epoch_is_refused_before_any_identity_work(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    settings_before = _digest(harness.home / "settings.json")
    credential_before = _digest(harness.home / "secrets" / "login.json")

    with pytest.raises(OnboardingGatewayError) as caught:
        harness.logout(expected_epoch="another-generation")

    assert caught.value.code == ipc_errors.STALE_DAEMON_GENERATION
    assert _journal_states(harness.root) == {}
    assert harness.calls == []
    assert _digest(harness.home / "settings.json") == settings_before
    assert _digest(harness.home / "secrets" / "login.json") == credential_before
    assert harness.stop_requests == []


def test_the_matching_expected_epoch_completes(tmp_path: Path) -> None:
    harness = Harness(tmp_path)

    result = harness.logout(expected_epoch="serving-1")

    assert result["state"] == "completed"
    assert result["loggedOut"] is True
    assert not (harness.home / "secrets" / "login.json").exists()
    assert len(harness.stop_requests) == 1


def test_an_empty_expected_epoch_string_is_invalid(tmp_path: Path) -> None:
    harness = Harness(tmp_path)

    with pytest.raises(OnboardingGatewayError) as caught:
        harness.logout(expected_epoch="")

    assert caught.value.code == ipc_errors.INVALID_ARGUMENT
    assert _journal_states(harness.root) == {}
    assert harness.calls == []


def test_a_partial_removal_records_a_failed_receipt_with_real_labels(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    # The credential path becomes a directory after the settings rewrite, so
    # the removal really removes the settings fields before it fails.
    (harness.home / "secrets" / "login.json").unlink()
    (harness.home / "secrets" / "login.json").mkdir()

    result = harness.logout()

    assert result["state"] == "failed"
    assert result["loggedOut"] is False
    assert result["nextAction"] == "inspect"
    assert result["removed"] == [
        "settings.identityIssuer",
        "settings.identityMode",
        "settings.owner",
        "settings.userId",
        "settings.username",
    ]
    assert _journal_states(harness.root) == {result["operationId"]: "failed"}
    assert len(harness.stop_requests) == 1


def test_a_full_removal_failure_records_a_failed_receipt_with_no_labels(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    # The login-owned credential becomes a directory, and the home carries no
    # Casdoor identity, so the removal has no settings rewrite to report and
    # fails with nothing removed.
    settings = json.loads((harness.home / "settings.json").read_text(encoding="utf-8"))
    settings["identityMode"] = "local-usage"
    (harness.home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    (harness.home / "secrets" / "login.json").unlink()
    (harness.home / "secrets" / "login.json").mkdir()

    result = harness.logout()

    assert result["state"] == "failed"
    assert result["loggedOut"] is False
    assert result["removed"] == []
    assert result["nextAction"] == "inspect"
    assert _journal_states(harness.root) == {result["operationId"]: "failed"}
    assert harness.calls == ["inspect", "remove"]
    assert len(harness.stop_requests) == 1


def test_login_is_refused_while_a_logout_receipt_is_unfinished(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    harness.seed_accepted()

    with pytest.raises(OnboardingGatewayError) as caught:
        harness.gateway.relogin(_context(), operation_key="relogin-after-logout")

    assert caught.value.code == AUTH_OPERATION_CONFLICT


def test_logout_is_refused_while_an_auth_operation_is_pending(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    harness.service.begin_auth(
        _context(), "login", "pending-login-key", "operation-pending"
    )

    with pytest.raises(OnboardingGatewayError) as caught:
        harness.logout()

    assert caught.value.code == ipc_errors.OPERATION_IN_PROGRESS
    # Only the read-only preview ran; no receipt and no side effect.
    assert harness.calls == ["inspect"]
    assert _journal_states(harness.root) == {"operation-pending": "pending"}
    assert harness.stop_requests == []


def test_a_dead_writer_reconciles_to_unknown_and_is_not_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The acceptance outlives the daemon; a restart never re-runs anything."""

    from hyprial.daemon.impl.onboarding.operations.service import ControllerIdentity

    dead_writer = ControllerIdentity(pid=999_997, birth_identity="dead-birth")
    monkeypatch.setattr(
        ControllerIdentity, "current", classmethod(lambda cls: dead_writer)
    )
    harness = Harness(tmp_path)
    harness.seed_accepted()

    restart_writer = ControllerIdentity(pid=999_999, birth_identity="restart-birth")
    monkeypatch.setattr(
        ControllerIdentity, "current", classmethod(lambda cls: restart_writer)
    )
    restarted = OperationService.open(harness.root)
    calls: list[str] = []
    gateway = ServingOnboardingGateway(
        operations=restarted,
        home=harness.home,
        identity_reader=lambda _home: harness.identity,
        device_reader=lambda _home: None,
        journal_reader=journal.read_journal,
        logout_artifacts=RecordingArtifacts(calls),
        clock=lambda: _NOW,
    )

    replayed = gateway.logout(
        _context(), operation_key=LOGOUT_KEY, confirm=True
    )

    assert replayed["state"] == "unknown"
    assert replayed["loggedOut"] is False
    assert replayed["nextAction"] == "inspect"
    assert calls == []
    assert (harness.home / "secrets" / "login.json").exists()


def test_status_shows_the_completed_logout(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    result = harness.logout()

    status = harness.gateway.status(_context())

    operations = {item["operationId"]: item for item in status["operations"]}
    assert operations[result["operationId"]]["kind"] == "logout"
    assert operations[result["operationId"]]["state"] == "completed"


# -- fenced lifecycle handoff (journal section) -----------------------------


def _iso(offset_seconds: float) -> str:
    from datetime import datetime, timezone
    import time

    return datetime.fromtimestamp(
        time.time() + offset_seconds, tz=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _recorded_writer(harness: Harness, operation_id: str) -> tuple[int, str]:
    state = journal.read_journal(harness.root)
    assert state is not None
    for item_id, pid, birth in state.writer_identities:
        if item_id == operation_id:
            return pid, birth
    raise AssertionError("the receipt has no recorded writer")


def _handoff(harness: Harness, operation_id: str, receipt_id: str) -> HandoffRecord:
    old_pid, old_birth = _recorded_writer(harness, operation_id)
    return HandoffRecord(
        operation_id=operation_id,
        receipt_id=receipt_id,
        surface=_surface(),
        old_pid=old_pid,
        old_birth=old_birth,
        old_epoch="serving-1",
        custodian_pid=5252,
        custodian_birth="birth-2",
        proof=journal.HANDOFF_PROOF_PID_BIRTH_EXIT,
        taken_at=_iso(0),
        expires_at=_iso(600),
    )


def test_handoff_round_trips_and_release_keeps_the_receipt(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    seeded = harness.seed_accepted()

    handoff = harness.service.take_handoff(
        _handoff(harness, seeded.operation_id, seeded.receipt_id)
    )

    state = journal.read_journal(harness.root)
    assert state is not None
    assert state.handoff == handoff
    assert [record.state for record in state.snapshot.records] == ["accepted"]

    harness.service.release_handoff()

    state = journal.read_journal(harness.root)
    assert state is not None
    assert state.handoff is None
    assert [record.state for record in state.snapshot.records] == ["accepted"]


def test_handoff_must_name_one_existing_logout_receipt(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    seeded = harness.seed_accepted()

    wrong_receipt = _handoff(harness, seeded.operation_id, "some-other-receipt")
    with pytest.raises(Exception) as wrong:
        harness.service.take_handoff(wrong_receipt)
    assert "logout receipt" in str(wrong.value)

    # A handoff for an operation that does not exist is refused too.
    missing = HandoffRecord(
        operation_id="no-such-operation",
        receipt_id=seeded.receipt_id,
        surface=_surface(),
        old_pid=1,
        old_birth="birth-x",
        old_epoch="serving-1",
        custodian_pid=2,
        custodian_birth="birth-y",
        proof=journal.HANDOFF_PROOF_PID_BIRTH_EXIT,
        taken_at=_iso(0),
        expires_at=_iso(600),
    )
    with pytest.raises(Exception):
        harness.service.take_handoff(missing)


@pytest.mark.parametrize(
    "mutation",
    [
        {"custodianPid": "old-writer"},  # custodian is the old writer
        {"proof": "trust-me"},  # not an exit proof
        {"expiresAt": "2000-01-01T00:00:00Z"},  # expiry before start
        {"takenAt": "yesterday"},  # not ISO-8601 UTC
        {"oldBirth": ""},  # no old birth identity
        {"extra": 1},  # unknown field
    ],
    ids=[
        "same-pid",
        "fake-proof",
        "expiry-before-start",
        "bad-timestamp",
        "empty-birth",
        "unknown-field",
    ],
)
def test_malformed_handoff_section_fails_closed(
    tmp_path: Path, mutation: dict[str, object]
) -> None:
    harness = Harness(tmp_path)
    seeded = harness.seed_accepted()
    handoff = _handoff(harness, seeded.operation_id, seeded.receipt_id)
    harness.service.take_handoff(handoff)

    path = journal.journal_path(harness.root)
    document = json.loads(path.read_text(encoding="utf-8"))
    resolved = {
        key: (document["handoff"]["oldPid"] if value == "old-writer" else value)
        for key, value in mutation.items()
    }
    document["handoff"].update(resolved)
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(OperationReceiptError) as caught:
        journal.read_journal(harness.root)

    assert caught.value.code == OPERATION_STORE_CORRUPT


# -- polling a logout receipt (AUTH_OPERATION variant) ----------------------


def _get(harness: Harness, operation_id: str) -> dict:
    return harness.gateway.get(
        _context(owner=harness.identity.owner), operation_id=operation_id
    )


def test_get_projects_an_accepted_logout(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    seeded = harness.seed_accepted()

    result = _get(harness, seeded.operation_id)

    operation = result["operation"]
    assert result["ok"] is True
    assert operation == {
        "operationId": seeded.operation_id,
        "kind": "logout",
        "state": "accepted",
        "owner": OWNER,
        "receiptId": seeded.receipt_id,
        "loggedOut": False,
        "removed": [],
        "failure": None,
    }


def test_get_projects_a_completed_logout_with_its_labels(tmp_path: Path) -> None:
    harness = Harness(tmp_path)

    # A receipt that really removed something reports those labels.
    seeded = harness.seed_accepted(operation_id="operation-completed-1")
    harness.service.mark_logout(
        _context(owner=OWNER),
        seeded.operation_id,
        "completed",
        ("secrets/login.json", "settings.owner"),
    )
    projected = _get(harness, seeded.operation_id)["operation"]
    assert projected["removed"] == ["secrets/login.json", "settings.owner"]
    assert projected["loggedOut"] is True


def test_get_projects_a_failed_logout_with_the_real_labels(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    seeded = harness.seed_accepted()
    harness.service.mark_logout(
        _context(owner=OWNER),
        seeded.operation_id,
        "failed",
        ("settings.owner",),
    )

    operation = _get(harness, seeded.operation_id)["operation"]

    assert operation["state"] == "failed"
    assert operation["loggedOut"] is False
    assert operation["removed"] == ["settings.owner"]


def test_get_projects_an_unknown_logout(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    seeded = harness.seed_accepted()
    harness.service.mark_logout(
        _context(owner=OWNER), seeded.operation_id, "unknown"
    )

    operation = _get(harness, seeded.operation_id)["operation"]

    assert operation["state"] == "unknown"
    assert operation["loggedOut"] is False
    assert operation["removed"] == []


def test_get_is_read_only_for_a_logout(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    seeded = harness.seed_accepted()
    before = journal.journal_path(harness.root).read_bytes()

    _get(harness, seeded.operation_id)
    _get(harness, seeded.operation_id)

    assert journal.journal_path(harness.root).read_bytes() == before


def test_get_still_refuses_an_unknown_operation(tmp_path: Path) -> None:
    harness = Harness(tmp_path)

    with pytest.raises(OnboardingGatewayError) as caught:
        _get(harness, "no-such-operation")

    assert caught.value.code == AUTH_OPERATION_NOT_FOUND


def test_every_logout_projection_matches_the_declared_schema(tmp_path: Path) -> None:
    from jsonschema import Draft202012Validator

    from hyprial.daemon.impl.request_catalog.onboarding.schemas import (
        AUTH_GET_RESULT,
    )

    validator = Draft202012Validator(AUTH_GET_RESULT)
    # One receipt per terminal state: the state machine is monotonic, so the
    # projections are validated on separate journals.
    for state, removed in (
        ("accepted", ()),
        ("failed", ("settings.owner",)),
        ("unknown", ()),
        ("completed", ("secrets/login.json",)),
    ):
        harness = Harness(tmp_path / state)
        seeded = harness.seed_accepted(operation_id=f"operation-{state}-1")
        if state != "accepted":
            harness.service.mark_logout(
                _context(owner=OWNER), seeded.operation_id, state, removed
            )
        result = _get(harness, seeded.operation_id)
        validator.validate(result)
        assert result["operation"]["state"] == state
        assert result["operation"]["kind"] == "logout"
        assert result["operation"]["loggedOut"] is (state == "completed")
        assert result["operation"]["removed"] == list(removed)


def test_a_login_receipt_keeps_the_login_variant(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.service.begin_auth(
        _context(owner=OWNER), "login", "login-key-0000001", "login-operation"
    )

    operation = _get(harness, "login-operation")["operation"]

    assert operation["kind"] == "login"
    assert "verification" in operation
    assert "loggedOut" not in operation
