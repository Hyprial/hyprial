"""Serving account lifecycle: startup path, durable projection, same-owner relogin.

These cases use the real ``OperationService`` journal, a real device record
shape and fake identity/lock ports.  No credential file, daemon, socket or home
outside ``tmp_path`` is touched, and no test ever starts the credential worker
subprocess.
"""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.application.onboarding.account_lifecycle import (
    SERVING_PERMIT_SECONDS,
    ServingRelogin,
    classify_startup,
    observe_account,
    startup_decision,
)
from hyprial.daemon.impl.application.onboarding.errors import (
    OnboardingGatewayError,
)
from hyprial.daemon.impl.application.onboarding.service import (
    ServingOnboardingGateway,
)
from hyprial.daemon.impl.onboarding.operations import journal
from hyprial.daemon.impl.onboarding.operations import OperationService
from hyprial.daemon.impl.onboarding.operations.journal import (
    OPERATION_STORE_CORRUPT,
    OperationReceiptError,
)
from hyprial.daemon.impl.request_catalog.onboarding import (
    AUTH_OPERATION_CONFLICT,
    CallContext,
    Surface,
)
from hyprial.daemon.impl.request_catalog.onboarding.specs import (
    REGISTRATION_UNAVAILABLE,
)
from hyprial.identity import (
    AuthProfile,
    DeviceRecord,
    IdentityBegin,
    IdentityPublicState,
    IdentitySnapshot,
    IdentityTransactionBusy,
)
from hyprial.kernel import ipc_errors

ISSUER = "https://issuer.example.test"
OTHER_ISSUER = "https://other-issuer.example.test"
OWNER = "alice@example.test"
OTHER_OWNER = "mallory@example.test"
PROFILE = AuthProfile(ISSUER, "client-id")
RELOGIN_KEY = "relogin-key-000001"
INITIALIZE_KEY = "initialize-key-0001"


def _surface(generation: str = "serving-1") -> Surface:
    return Surface("home-binding", "host-control", "view", generation)


def _serving_context(*, owner: str | None = None) -> CallContext:
    return CallContext(
        surface=_surface(),
        phase="serving",
        caller="local-gui",
        owner=owner,
    )


def _bootstrap_context(
    *, owner: str | None = OWNER, generation: str = "bootstrap-1"
) -> CallContext:
    return CallContext(
        surface=Surface("home-binding", "host-control", "view", generation),
        phase="bootstrap",
        caller="local-bootstrap",
        owner=owner,
    )


def _device(owner: str = OWNER) -> DeviceRecord:
    return DeviceRecord(
        device_id="device-000001",
        owner=owner,
        server_public="nodekey:server-public",
        client_public="nodekey:client-public",
        key_generation=1,
    )


class Home:
    """A mutable public identity snapshot the gateway reads on every call."""

    def __init__(self, identity: IdentitySnapshot) -> None:
        self.identity = identity

    def read(self, _home) -> IdentitySnapshot:
        return self.identity


class FakeLock:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeIdentity:
    def __init__(self) -> None:
        self.begins: list[dict] = []
        self.poll_results: list[IdentityPublicState] = []
        self.cancel_result: IdentityPublicState | None = None
        self.cancel_error: BaseException | None = None
        self.closed = False

    def begin(self, *, profile, home, permit, lock_fd=None, transport=None):
        self.begins.append(
            {
                "profile": profile,
                "home": home,
                "permit": permit,
                "lock_fd": lock_fd,
                "transport": transport,
            }
        )
        return IdentityBegin(
            permit.operation_id,
            "https://verify.example.test",
            "ABCD-EFGH",
            600,
            5,
        )

    def poll_public(self, operation_id):
        return self.poll_results.pop(0)

    def cancel(self, operation_id):
        if self.cancel_error is not None:
            raise self.cancel_error
        assert self.cancel_result is not None
        return self.cancel_result

    def close(self) -> None:
        self.closed = True


class Harness:
    def __init__(
        self,
        tmp_path,
        *,
        identity: IdentitySnapshot,
        device: DeviceRecord | None = None,
        claim: str | None = None,
        complete=None,
        journal_reader=journal.read_journal,
        lock_error: BaseException | None = None,
    ) -> None:
        self.home_dir = tmp_path / "home"
        self.home_dir.mkdir(parents=True, exist_ok=True)
        self.home = Home(identity)
        self.device = device
        self.service = OperationService.open(tmp_path / "operations")
        self.identity = FakeIdentity()
        self.lock = FakeLock()
        self.lock_error = lock_error
        self.gateway = ServingOnboardingGateway(
            operations=self.service,
            home=self.home_dir,
            complete_initialize=complete,
            promotion_operation_id=claim,
            identity_reader=self.home.read,
            device_reader=lambda _home: self.device,
            journal_reader=journal_reader,
            profile_resolver=lambda _home: PROFILE,
            identity_factory=lambda: self.identity,
            transaction_factory=self._transaction,
            permit_seconds=SERVING_PERMIT_SECONDS,
            clock=lambda: 1_700_000_000.0,
        )

    def _transaction(self, _home):
        if self.lock_error is not None:
            raise self.lock_error
        return self.lock

    def project(self):
        return observe_account(
            home=self.home_dir,
            operations_root=self.service.root,
            identity_reader=self.home.read,
            device_reader=lambda _home: self.device,
            journal_reader=journal.read_journal,
            credential="unverified",
        )


def _completed_initialize(
    service: OperationService,
    *,
    owner: str = OWNER,
    generation: str = "bootstrap-1",
):
    context = _bootstrap_context(owner=owner, generation=generation)
    record = service.accept_initialize(
        context,
        INITIALIZE_KEY,
        "op-initialize-1",
        "receipt-initialize-1",
        promotion=lambda _record: None,
    )
    return service.mark_initialize(context, record.operation_id, "completed")


def test_startup_path_follows_the_committed_identity_only(tmp_path):
    committed = IdentitySnapshot(OWNER, "casdoor", ISSUER)
    decision = classify_startup(committed)
    assert decision.mode == "serving"
    assert decision.owner == OWNER
    assert decision.bootstrap_required is False

    ownerless = classify_startup(IdentitySnapshot())
    assert ownerless.mode == "bootstrap"
    assert ownerless.owner is None
    assert ownerless.bootstrap_required is True

    home = tmp_path / "crash"
    home.mkdir()

    def read(_home):
        return committed

    crashed = startup_decision(home=home, identity_reader=read)
    assert crashed.mode == "serving"
    assert crashed.bootstrap_required is False


def test_startup_classification_refuses_a_non_snapshot():
    with pytest.raises(OnboardingGatewayError) as refused:
        classify_startup({"owner": OWNER})
    assert refused.value.code == ipc_errors.INVALID_ARGUMENT


def test_projection_is_ready_only_with_identity_device_and_initialize(tmp_path):
    harness = Harness(tmp_path, identity=IdentitySnapshot(OWNER, "casdoor", ISSUER))
    _completed_initialize(harness.service)

    projected = harness.project()
    assert projected.authenticated is True
    assert projected.owner == OWNER
    assert projected.device_ready is False
    assert projected.initialized is False
    assert projected.ready is False
    assert projected.next_actions == ("device.ensure",)

    harness.device = _device()
    projected = harness.project()
    assert projected.device_ready is True
    assert projected.device_id == "device-000001"
    assert projected.initialized is True
    assert projected.ready is True
    assert projected.next_actions == ()


def test_projection_reads_the_initialize_receipt_across_generations(tmp_path):
    harness = Harness(
        tmp_path, identity=IdentitySnapshot(OWNER, "casdoor", ISSUER), device=_device()
    )
    record = _completed_initialize(harness.service, generation="bootstrap-1")
    assert record.surface.transport_generation == "bootstrap-1"

    status = harness.gateway.status(_serving_context())
    assert status["authenticated"] is True
    assert status["owner"] == OWNER
    assert status["deviceReady"] is True
    assert status["initialized"] is True
    assert status["nextActions"] == []
    assert status["operations"] == []


def test_authenticated_home_without_initialize_reports_recovery_not_ready(tmp_path):
    harness = Harness(
        tmp_path, identity=IdentitySnapshot(OWNER, "casdoor", ISSUER), device=_device()
    )

    status = harness.gateway.status(_serving_context(owner=OTHER_OWNER))
    assert status["owner"] == OWNER
    assert status["authenticated"] is True
    assert status["deviceReady"] is True
    assert status["initialized"] is False
    assert status["nextActions"] == ["initialize"]

    projected = harness.gateway.account_projection(_serving_context())
    assert projected.ready is False
    assert projected.recovery_action == "initialize"


def test_an_unfinished_initialize_claim_reports_recovery_not_ready(tmp_path):
    harness = Harness(
        tmp_path,
        identity=IdentitySnapshot(OWNER, "casdoor", ISSUER),
        device=_device(),
    )
    harness.service.accept_initialize(
        _bootstrap_context(generation="bootstrap-1"),
        INITIALIZE_KEY,
        "op-initialize-2",
        "receipt-initialize-2",
        promotion=lambda _record: None,
    )

    projected = harness.project()
    assert projected.initialize_completed is False
    assert projected.initialized is False
    assert projected.ready is False
    assert projected.next_actions == ("initialize",)

    status = harness.gateway.status(_serving_context())
    assert status["initialized"] is False
    assert status["nextActions"] == ["initialize"]
    assert [item["state"] for item in status["operations"]] == []


def test_ownerless_serving_projection_offers_only_login(tmp_path):
    harness = Harness(tmp_path, identity=IdentitySnapshot())

    status = harness.gateway.status(_serving_context())
    assert status["authenticated"] is False
    assert status["owner"] is None
    assert status["nextActions"] == ["login"]
    assert status["operations"] == []


def test_device_record_for_another_owner_is_not_ready(tmp_path):
    harness = Harness(
        tmp_path,
        identity=IdentitySnapshot(OWNER, "casdoor", ISSUER),
        device=_device(OTHER_OWNER),
    )
    _completed_initialize(harness.service)

    projected = harness.project()
    assert projected.device_ready is False
    assert projected.initialized is False
    assert projected.next_actions == ("device.ensure",)


def test_unreadable_device_and_journal_fail_closed(tmp_path):
    harness = Harness(tmp_path, identity=IdentitySnapshot(OWNER, "casdoor", ISSUER))

    def broken_device(_home):
        raise ValueError("device record is corrupt")

    with pytest.raises(OnboardingGatewayError) as device_refused:
        observe_account(
            home=harness.home_dir,
            operations_root=harness.service.root,
            identity_reader=harness.home.read,
            device_reader=broken_device,
        )
    assert device_refused.value.code == ipc_errors.DAEMON_ERROR

    def broken_journal(_root):
        raise OperationReceiptError(OPERATION_STORE_CORRUPT, "journal is corrupt")

    with pytest.raises(OnboardingGatewayError) as journal_refused:
        observe_account(
            home=harness.home_dir,
            operations_root=harness.service.root,
            identity_reader=harness.home.read,
            device_reader=lambda _home: None,
            journal_reader=broken_journal,
        )
    assert journal_refused.value.code == ipc_errors.DAEMON_ERROR


def test_normal_startup_needs_no_promotion_claim_and_migrates_nothing(tmp_path):
    harness = Harness(
        tmp_path, identity=IdentitySnapshot(OWNER, "casdoor", ISSUER), device=_device()
    )
    _completed_initialize(harness.service)
    before = journal.read_journal(harness.service.root).snapshot
    assert (
        harness.gateway.migrate_generation(_serving_context(), authority=None) is None
    )
    harness.gateway.on_daemon_ready()
    assert harness.gateway._recovery.join(30)
    assert journal.read_journal(harness.service.root).snapshot == before
    assert harness.service.visible(_serving_context(owner=OWNER)) == ()


def test_a_claim_is_required_to_complete_initialize(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    service = OperationService.open(tmp_path / "operations")
    with pytest.raises(ValueError, match="promotion claim"):
        ServingOnboardingGateway(
            operations=service,
            home=home,
            complete_initialize=lambda: None,
            identity_reader=lambda _home: IdentitySnapshot(OWNER, "casdoor", ISSUER),
        )
    with pytest.raises(ValueError):
        ServingOnboardingGateway(
            operations=service,
            home=home,
            promotion_operation_id="",
            identity_reader=lambda _home: IdentitySnapshot(OWNER, "casdoor", ISSUER),
        )
    with pytest.raises(ValueError, match="given together"):
        ServingOnboardingGateway(
            operations=service,
            home=home,
            promotion_operation_id="op-initialize-1",
            identity_reader=lambda _home: IdentitySnapshot(OWNER, "casdoor", ISSUER),
        )
    with pytest.raises(ValueError, match="given together"):
        ServingOnboardingGateway(
            operations=service,
            home=home,
            complete_initialize=lambda: None,
            identity_reader=lambda _home: IdentitySnapshot(OWNER, "casdoor", ISSUER),
        )


def test_relogin_uses_one_narrow_permit_under_the_transaction_lock(tmp_path):
    harness = Harness(tmp_path, identity=IdentitySnapshot(OWNER, "casdoor", ISSUER))
    begin = harness.gateway.relogin(_serving_context(), operation_key=RELOGIN_KEY)
    assert begin["intent"] == "login"
    assert begin["state"] == "pending"
    assert begin["owner"] is None
    assert begin["verification"]["userCode"] == "ABCD-EFGH"

    assert len(harness.identity.begins) == 1
    called = harness.identity.begins[0]
    permit = called["permit"]
    assert permit.bootstrap is False
    assert permit.expected_identity == IdentitySnapshot(OWNER, "casdoor", ISSUER)
    assert permit.issuer == ISSUER
    assert permit.expires_at == 1_700_000_000 + SERVING_PERMIT_SECONDS
    assert called["lock_fd"] is None
    assert harness.lock.closed is False
    assert (
        harness.gateway.account_projection(_serving_context()).credential == "pending"
    )
    harness.gateway.close()
    assert harness.lock.closed is True


def test_a_pending_poll_keeps_the_live_relogin_and_its_verification(tmp_path):
    harness = Harness(tmp_path, identity=IdentitySnapshot(OWNER, "casdoor", ISSUER))
    begin = harness.gateway.relogin(_serving_context(), operation_key=RELOGIN_KEY)
    operation_id = harness.identity.begins[0]["permit"].operation_id
    harness.identity.poll_results.append(
        IdentityPublicState(operation_id, "pending", retry_after_seconds=5.0)
    )

    polled = harness.gateway.get(_serving_context(), operation_id=operation_id)
    assert polled["operation"]["state"] == "pending"
    assert polled["operation"]["verification"] == begin["verification"]
    assert harness.identity.closed is False
    assert harness.lock.closed is False
    assert harness.gateway.account_projection(_serving_context()).credential == "pending"
    assert harness.home.identity.owner == OWNER

    harness.identity.poll_results.append(
        IdentityPublicState(operation_id, "committed", OWNER, ISSUER)
    )
    confirmed = harness.gateway.get(_serving_context(), operation_id=operation_id)
    assert confirmed["operation"]["state"] == "authenticated"
    assert confirmed["operation"]["owner"] == OWNER
    assert confirmed["operation"]["verification"] is None
    assert harness.identity.closed is True
    assert harness.lock.closed is True
    assert harness.gateway.account_projection(_serving_context()).credential == "committed"


def test_a_second_pending_poll_still_leaves_the_worker_alive(tmp_path):
    harness = Harness(tmp_path, identity=IdentitySnapshot(OWNER, "casdoor", ISSUER))
    harness.gateway.relogin(_serving_context(), operation_key=RELOGIN_KEY)
    operation_id = harness.identity.begins[0]["permit"].operation_id
    for _ in range(3):
        harness.identity.poll_results.append(
            IdentityPublicState(operation_id, "pending")
        )
        polled = harness.gateway.get(_serving_context(), operation_id=operation_id)
        assert polled["operation"]["state"] == "pending"
    assert harness.identity.closed is False
    assert len(harness.identity.begins) == 1


def test_same_owner_relogin_confirms_only_the_observed_owner(tmp_path):
    harness = Harness(
        tmp_path,
        identity=IdentitySnapshot(OWNER, "casdoor", ISSUER),
        device=_device(),
    )
    _completed_initialize(harness.service)
    harness.gateway.relogin(_serving_context(), operation_key=RELOGIN_KEY)
    harness.identity.poll_results.append(
        IdentityPublicState(
            harness.identity.begins[0]["permit"].operation_id,
            "committed",
            OWNER,
            ISSUER,
            {"credentialCommitted": True, "settingsCommitted": True},
        )
    )

    operation = harness.gateway.get(
        _serving_context(),
        operation_id=harness.identity.begins[0]["permit"].operation_id,
    )
    assert operation["operation"]["state"] == "authenticated"
    assert operation["operation"]["owner"] == OWNER
    assert harness.identity.closed is True
    projected = harness.gateway.account_projection(_serving_context())
    assert projected.credential == "committed"
    assert projected.ready is True


def test_relogin_refuses_a_mode_conflict_before_any_write(tmp_path):
    harness = Harness(tmp_path, identity=IdentitySnapshot(OWNER, "local", ISSUER))
    with pytest.raises(OnboardingGatewayError) as refused:
        harness.gateway.relogin(_serving_context(), operation_key=RELOGIN_KEY)
    assert refused.value.code == ipc_errors.IDENTITY_CONFLICT
    assert harness.identity.begins == []
    assert journal.journal_path(harness.service.root).exists() is False


def test_relogin_refuses_an_issuer_conflict_before_any_write(tmp_path):
    harness = Harness(
        tmp_path, identity=IdentitySnapshot(OWNER, "casdoor", OTHER_ISSUER)
    )
    with pytest.raises(OnboardingGatewayError) as refused:
        harness.gateway.relogin(_serving_context(), operation_key=RELOGIN_KEY)
    assert refused.value.code == ipc_errors.IDENTITY_CONFLICT
    assert harness.identity.begins == []
    assert journal.journal_path(harness.service.root).exists() is False


def test_a_commit_for_another_owner_is_a_conflict_and_leaves_identity(tmp_path):
    harness = Harness(tmp_path, identity=IdentitySnapshot(OWNER, "casdoor", ISSUER))
    harness.gateway.relogin(_serving_context(), operation_key=RELOGIN_KEY)
    operation_id = harness.identity.begins[0]["permit"].operation_id
    harness.identity.poll_results.append(
        IdentityPublicState(operation_id, "committed", OTHER_OWNER, ISSUER)
    )

    with pytest.raises(OnboardingGatewayError) as refused:
        harness.gateway.get(_serving_context(), operation_id=operation_id)
    assert refused.value.code == ipc_errors.IDENTITY_CONFLICT
    assert refused.value.data["committedOwner"] == OTHER_OWNER
    assert harness.home.identity.owner == OWNER
    assert harness.gateway.account_projection(_serving_context()).credential == "failed"
    assert harness.gateway.status(_serving_context())["authenticated"] is True


def test_a_failed_relogin_keeps_the_local_identity_and_reports_unknown(tmp_path):
    harness = Harness(tmp_path, identity=IdentitySnapshot(OWNER, "casdoor", ISSUER))
    harness.gateway.relogin(_serving_context(), operation_key=RELOGIN_KEY)
    operation_id = harness.identity.begins[0]["permit"].operation_id
    harness.identity.poll_results.append(
        IdentityPublicState(
            operation_id,
            "failed",
            None,
            None,
            error_code="OWNER_MISMATCH",
        )
    )

    operation = harness.gateway.get(_serving_context(), operation_id=operation_id)
    assert operation["operation"]["state"] == "unknown"
    assert operation["operation"]["owner"] == OWNER
    assert harness.home.identity.owner == OWNER
    projected = harness.gateway.account_projection(_serving_context())
    assert projected.authenticated is True
    assert projected.credential == "failed"
    assert projected.recovery_action == "relogin"

    status = harness.gateway.status(_serving_context())
    assert status["authenticated"] is True
    assert status["owner"] == OWNER
    assert [item["kind"] for item in status["operations"]] == ["login"]


def test_cancel_of_a_live_relogin_reports_a_truthful_outcome(tmp_path):
    harness = Harness(tmp_path, identity=IdentitySnapshot(OWNER, "casdoor", ISSUER))
    harness.gateway.relogin(_serving_context(), operation_key=RELOGIN_KEY)
    operation_id = harness.identity.begins[0]["permit"].operation_id
    harness.identity.cancel_result = IdentityPublicState(operation_id, "cancelled")

    outcome = harness.gateway.cancel(_serving_context(), operation_id=operation_id)
    assert outcome["state"] == "cancelled"
    assert outcome["cancelled"] is True
    assert outcome["reason"] == "cancelled"
    assert harness.identity.closed is True
    assert harness.home.identity.owner == OWNER
    assert (
        harness.gateway.account_projection(_serving_context()).credential
        == "unverified"
    )


def test_cancel_with_an_unconfirmed_worker_marks_unknown(tmp_path):
    harness = Harness(tmp_path, identity=IdentitySnapshot(OWNER, "casdoor", ISSUER))
    harness.gateway.relogin(_serving_context(), operation_key=RELOGIN_KEY)
    operation_id = harness.identity.begins[0]["permit"].operation_id
    harness.identity.cancel_error = RuntimeError("worker died")

    outcome = harness.gateway.cancel(_serving_context(), operation_id=operation_id)
    assert outcome["state"] == "unknown"
    assert outcome["cancelled"] is False
    assert outcome["reason"] == "outcome-unknown"
    assert harness.home.identity.owner == OWNER


def test_a_busy_identity_transaction_is_reported_as_a_conflict(tmp_path):
    harness = Harness(
        tmp_path,
        identity=IdentitySnapshot(OWNER, "casdoor", ISSUER),
        lock_error=IdentityTransactionBusy("home lock is held"),
    )
    with pytest.raises(OnboardingGatewayError) as refused:
        harness.gateway.relogin(_serving_context(), operation_key=RELOGIN_KEY)
    assert refused.value.code == AUTH_OPERATION_CONFLICT
    assert harness.identity.begins == []
    assert journal.journal_path(harness.service.root).exists() is True


def test_relogin_replays_the_same_key_and_refuses_a_second_pending(tmp_path):
    harness = Harness(tmp_path, identity=IdentitySnapshot(OWNER, "casdoor", ISSUER))
    first = harness.gateway.relogin(_serving_context(), operation_key=RELOGIN_KEY)
    replay = harness.gateway.relogin(_serving_context(), operation_key=RELOGIN_KEY)
    assert replay["operationId"] == first["operationId"]
    assert len(harness.identity.begins) == 1

    with pytest.raises(OnboardingGatewayError) as busy:
        harness.gateway.relogin(_serving_context(), operation_key="relogin-key-000002")
    assert busy.value.code == ipc_errors.OPERATION_IN_PROGRESS


def test_relogin_never_writes_a_logout_receipt(tmp_path):
    harness = Harness(tmp_path, identity=IdentitySnapshot(OWNER, "casdoor", ISSUER))
    _completed_initialize(harness.service)
    harness.gateway.relogin(_serving_context(), operation_key=RELOGIN_KEY)
    kinds = {
        item["kind"]
        for item in harness.gateway.status(_serving_context())["operations"]
    }
    assert "logout" not in kinds


def test_registration_is_refused_in_serving(tmp_path):
    harness = Harness(tmp_path, identity=IdentitySnapshot(OWNER, "casdoor", ISSUER))
    with pytest.raises(OnboardingGatewayError) as refused:
        harness.gateway.begin_auth(
            _serving_context(),
            intent="register",
            operation_key="register-key-00001",
        )
    assert refused.value.code == REGISTRATION_UNAVAILABLE


def test_relogin_refuses_a_bootstrap_operator_surface(tmp_path):
    harness = Harness(tmp_path, identity=IdentitySnapshot(OWNER, "casdoor", ISSUER))
    with pytest.raises(OnboardingGatewayError) as refused:
        harness.gateway.relogin(_bootstrap_context(), operation_key=RELOGIN_KEY)
    assert refused.value.code == ipc_errors.CALLER_NOT_AUTHORIZED


def test_relogin_coordinator_requires_a_committed_owner(tmp_path):
    coordinator = ServingRelogin(
        home=tmp_path / "home",
        profile_resolver=lambda _home: PROFILE,
        identity_reader=lambda _home: IdentitySnapshot(),
        identity_factory=FakeIdentity,
        lock_factory=lambda _home: FakeLock(),
    )
    with pytest.raises(OnboardingGatewayError) as refused:
        coordinator.precheck(IdentitySnapshot())
    assert refused.value.code == ipc_errors.IDENTITY_UNBOUND
    assert coordinator.live is False
    assert coordinator.live_operation_id is None


def test_relogin_coordinator_holds_lock_across_committed_poll_until_close(tmp_path):
    expected = IdentitySnapshot(OWNER, "casdoor", ISSUER)
    identity = FakeIdentity()
    lock = FakeLock()
    home = tmp_path / "home"
    home.mkdir()
    coordinator = ServingRelogin(
        home=home,
        profile_resolver=lambda _home: PROFILE,
        identity_reader=lambda _home: expected,
        identity_factory=lambda: identity,
        lock_factory=lambda _home: lock,
    )

    begin = coordinator.begin(operation_id="op-relogin-commit", expected=expected)
    assert lock.closed is False

    identity.poll_results.append(
        IdentityPublicState(begin.operation_id, "pending", retry_after_seconds=5.0)
    )
    pending = coordinator.poll(begin.operation_id)
    assert pending.state == "pending"
    assert lock.closed is False

    identity.poll_results.append(
        IdentityPublicState(begin.operation_id, "committed", OWNER, ISSUER)
    )
    committed = coordinator.poll(begin.operation_id)
    assert committed.state == "committed"
    assert lock.closed is False

    coordinator.verify_commit(committed, expected=expected)
    assert lock.closed is False

    coordinator.close()
    assert lock.closed is True
    assert identity.closed is True


def test_relogin_coordinator_releases_lock_on_non_committed_poll_and_verify_error(
    tmp_path,
):
    expected = IdentitySnapshot(OWNER, "casdoor", ISSUER)
    home = tmp_path / "home"
    home.mkdir()

    for terminal_state in ("failed", "cancelled"):
        identity = FakeIdentity()
        lock = FakeLock()
        coordinator = ServingRelogin(
            home=home,
            profile_resolver=lambda _home: PROFILE,
            identity_reader=lambda _home: expected,
            identity_factory=lambda: identity,
            lock_factory=lambda _home: lock,
        )
        begin = coordinator.begin(
            operation_id=f"op-{terminal_state}", expected=expected
        )
        identity.poll_results.append(
            IdentityPublicState(begin.operation_id, terminal_state)
        )
        polled = coordinator.poll(begin.operation_id)
        assert polled.state == terminal_state
        assert lock.closed is True
        coordinator.close()

    failing_identity = FakeIdentity()
    failing_lock = FakeLock()
    failing_coordinator = ServingRelogin(
        home=home,
        profile_resolver=lambda _home: PROFILE,
        identity_reader=lambda _home: expected,
        identity_factory=lambda: failing_identity,
        lock_factory=lambda _home: failing_lock,
    )
    begin_err = failing_coordinator.begin(operation_id="op-poll-err", expected=expected)
    with pytest.raises(OnboardingGatewayError):
        failing_coordinator.poll(begin_err.operation_id)
    assert failing_lock.closed is True
    failing_coordinator.close()

    conflict_identity = FakeIdentity()
    conflict_lock = FakeLock()
    conflict_coordinator = ServingRelogin(
        home=home,
        profile_resolver=lambda _home: PROFILE,
        identity_reader=lambda _home: expected,
        identity_factory=lambda: conflict_identity,
        lock_factory=lambda _home: conflict_lock,
    )
    begin_conflict = conflict_coordinator.begin(
        operation_id="op-verify-err", expected=expected
    )
    conflict_identity.poll_results.append(
        IdentityPublicState(begin_conflict.operation_id, "committed", OTHER_OWNER, ISSUER)
    )
    committed_conflict = conflict_coordinator.poll(begin_conflict.operation_id)
    assert conflict_lock.closed is False
    with pytest.raises(OnboardingGatewayError) as refused:
        conflict_coordinator.verify_commit(committed_conflict, expected=expected)
    assert refused.value.code == ipc_errors.IDENTITY_CONFLICT
    assert conflict_lock.closed is True
    conflict_coordinator.close()


def test_gateway_holds_lock_through_mark_authenticated_and_closes_on_error(
    tmp_path, monkeypatch
):
    success_harness = Harness(
        tmp_path / "success", identity=IdentitySnapshot(OWNER, "casdoor", ISSUER)
    )
    lock_closed_during_mark: list[bool] = []
    real_mark = success_harness.service.mark_authenticated

    def spy_mark_authenticated(context, operation_id, *, owner):
        lock_closed_during_mark.append(success_harness.lock.closed)
        return real_mark(context, operation_id, owner=owner)

    monkeypatch.setattr(
        success_harness.service, "mark_authenticated", spy_mark_authenticated
    )
    success_harness.gateway.relogin(_serving_context(), operation_key=RELOGIN_KEY)
    operation_id = success_harness.identity.begins[0]["permit"].operation_id
    success_harness.identity.poll_results.append(
        IdentityPublicState(operation_id, "committed", OWNER, ISSUER)
    )
    confirmed = success_harness.gateway.get(
        _serving_context(), operation_id=operation_id
    )
    assert confirmed["operation"]["state"] == "authenticated"
    assert lock_closed_during_mark == [False]
    assert success_harness.lock.closed is True
    assert success_harness.identity.closed is True

    error_harness = Harness(
        tmp_path / "mark_error", identity=IdentitySnapshot(OWNER, "casdoor", ISSUER)
    )
    lock_closed_before_error: list[bool] = []

    def failing_mark_authenticated(context, operation_id, *, owner):
        del context, operation_id, owner
        lock_closed_before_error.append(error_harness.lock.closed)
        raise OperationReceiptError(OPERATION_STORE_CORRUPT, "journal write failed")

    monkeypatch.setattr(
        error_harness.service, "mark_authenticated", failing_mark_authenticated
    )
    error_harness.gateway.relogin(_serving_context(), operation_key=RELOGIN_KEY)
    failing_op_id = error_harness.identity.begins[0]["permit"].operation_id
    error_harness.identity.poll_results.append(
        IdentityPublicState(failing_op_id, "committed", OWNER, ISSUER)
    )
    with pytest.raises(OnboardingGatewayError) as mark_failed:
        error_harness.gateway.get(_serving_context(), operation_id=failing_op_id)
    assert mark_failed.value.code == ipc_errors.DAEMON_ERROR
    assert lock_closed_before_error == [False]
    assert error_harness.lock.closed is True
    assert error_harness.identity.closed is True
