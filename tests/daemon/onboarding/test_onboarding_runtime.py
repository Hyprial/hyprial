"""B0-B6 runtime composition: ownerless onboarding becomes the same daemon."""

import json
import os

import pytest

from hyprial.daemon.impl.configuration.ownership import (
    DaemonOwnershipBusy,
    DaemonStateOwnershipFence,
)
from hyprial.daemon.impl.onboarding.records import BootstrapRecord
from hyprial.daemon.impl.onboarding.runtime import (
    BOOTSTRAP_OPERATION_CONFLICT,
    BOOTSTRAP_PHASE_INVALID,
    BOOTSTRAP_PROMOTION_CONFLICT,
    BOOTSTRAP_SCOPE_MISMATCH,
    BootstrapError,
    BootstrapRuntime,
)
from hyprial.daemon.impl.request_catalog.onboarding import (
    CallContext,
    OnboardingStateError,
    Surface,
)
from hyprial.identity import (
    AuthProfile,
    IdentityBegin,
    IdentityPublicState,
    IdentitySnapshot,
    IdentityTransactionBusy,
    IdentityTransactionLock,
    read_identity_snapshot,
)
from hyprial.kernel import ipc_errors

ISSUER = "https://issuer.example.test"
OWNER = "alice@example.test"
PROFILE = AuthProfile(ISSUER, "fake-client-id")


def _surface() -> Surface:
    return Surface("home-binding", "host-control", "view", "generation-1")


def _committed(owner: str = OWNER) -> IdentityPublicState:
    return IdentityPublicState(
        "op-auth-0001",
        "committed",
        owner,
        ISSUER,
        {"credentialCommitted": True, "settingsCommitted": True},
    )


def _write_identity(home, owner: str = OWNER) -> None:
    (home / "settings.json").write_text(json.dumps({
        "owner": owner, "identityMode": "casdoor", "identityIssuer": ISSUER,
    }))


class FakeIdentity:
    def __init__(self, results=(), begin_errors=()) -> None:
        self.results = list(results)
        self.begin_errors = list(begin_errors)
        self.begins = []
        self.closed = False

    def begin(self, *, profile, home, permit, lock_fd=None, transport=None):
        del lock_fd
        if self.begin_errors:
            raise self.begin_errors.pop(0)
        self.begins.append((profile, home, permit, transport))
        return IdentityBegin("op-auth-0001", "https://verify.example.test", "CODE", 600, 5)

    def poll_public(self, operation_id):
        return self.results.pop(0)

    def cancel(self, operation_id):
        return IdentityPublicState(operation_id, "cancelled")

    def close(self) -> None:
        self.closed = True


class DispatchedFailure(RuntimeError):
    dispatched = True


@pytest.fixture(autouse=True)
def empty_override(monkeypatch):
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)


def _admit(home, fake):
    return BootstrapRuntime.admit(
        home=home,
        state_dir=home / "state",
        socket_path=home / "ipc.sock",
        identity_factory=lambda: fake,
    )


def _begin(runtime, surface=None):
    return runtime.begin_authentication(
        surface=surface or _surface(),
        intent="login",
        operation_key="login-key-0000001",
        operation_id="op-auth-0001",
        profile=PROFILE,
    )


def _initialize(runtime, surface=None):
    return runtime.accept_initialize(
        surface=surface or _surface(),
        operation_key="initialize-key-0001",
        operation_id="op-initialize",
        receipt_id="receipt-1",
    )


def _status_context(surface=None) -> CallContext:
    return CallContext(
        surface=surface or _surface(),
        phase="bootstrap",
        caller="local-bootstrap",
        owner=OWNER,
    )


def test_admit_publishes_private_record_and_holds_both_locks(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    runtime = _admit(home, FakeIdentity())
    try:
        assert runtime.phase == "awaiting-auth"
        record = BootstrapRecord.read(home / "state")
        assert record is not None
        assert record.pid == os.getpid()
        assert record.phase == "awaiting-auth"
        assert not (home / "state" / "daemon.json").exists()
        assert not (home / ".active_daemon").exists()
        with pytest.raises(IdentityTransactionBusy):
            IdentityTransactionLock.acquire(home, timeout=0)
        with pytest.raises(DaemonOwnershipBusy):
            DaemonStateOwnershipFence.acquire(home / "state", timeout=0)
    finally:
        runtime.close()
    assert BootstrapRecord.read(home / "state") is None
    with IdentityTransactionLock.acquire(home, timeout=0):
        pass


def test_happy_path_transfers_locks_into_the_same_process_run(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    fake = FakeIdentity([_committed()])
    runtime = _admit(home, fake)
    surface = _surface()
    try:
        begin = _begin(runtime, surface)
        assert begin.operation_id == "op-auth-0001"
        assert runtime.phase == "authenticating"
        profile, observed_home, permit, transport = fake.begins[0]
        assert profile == PROFILE
        assert observed_home == home
        assert permit.expected_identity == IdentitySnapshot()
        assert permit.bootstrap is True
        assert transport is None
        with pytest.raises(BootstrapError) as early:
            _initialize(runtime, surface)
        assert early.value.code == BOOTSTRAP_PHASE_INVALID

        _write_identity(home)
        state = runtime.poll_authentication(surface=surface)
        assert state.state == "committed"
        assert runtime.phase == "authenticated"
        assert fake.closed
        assert runtime.poll_authentication(surface=surface).state == "committed"

        quiesced = []
        runtime.register_quiesce(lambda: quiesced.append("closed-listener"))
        record = _initialize(runtime, surface)
        assert record.state == "promoting"
        assert runtime.phase == "promoting"
        assert quiesced == ["closed-listener"]
        assert BootstrapRecord.read(home / "state").phase == "promoting"

        calls = []

        class FakeDaemon:
            stop_reason = "shutdown"

            def owns_process_exit(self) -> None:
                calls.append("owns-process-exit")

            def run(self, *, ownership_stream, identity_transaction_stream) -> None:
                calls.append("run")
                assert ownership_stream is not None
                assert identity_transaction_stream is not None
                assert BootstrapRecord.read(home / "state") is None
                ownership_stream.close()
                identity_transaction_stream.close()

        def factory():
            calls.append("construct")
            return FakeDaemon()

        runtime.run_daemon(factory, owns_process_exit=True)
        assert calls == ["construct", "owns-process-exit", "run"]
        status = runtime.status(_status_context())
        states = {item["kind"]: item["state"] for item in status["operations"]}
        # R3/R6: returning from application.run() is not serving-ready.  The
        # claim stays promoting until daemon.ready + generation migration.
        assert states["initialize"] == "promoting"
        assert status["initialized"] is False
        runtime.complete_initialize()
        assert runtime.status(_status_context())["initialized"] is True
        with pytest.raises(BootstrapError) as second:
            runtime.run_daemon(factory)
        assert second.value.code == BOOTSTRAP_PROMOTION_CONFLICT
        runtime.close()
        with IdentityTransactionLock.acquire(home, timeout=0):
            pass
    finally:
        runtime.close()


def test_authenticated_bootstrap_logout_is_durable_and_requires_reopen(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    runtime = _admit(home, FakeIdentity([_committed()]))
    surface = _surface()
    context = CallContext(
        surface=surface, phase="bootstrap", caller="local-bootstrap"
    )
    try:
        _begin(runtime, surface)
        _write_identity(home)
        runtime.poll_authentication(surface=surface)
        assert runtime.phase == "authenticated"

        with pytest.raises(OnboardingStateError) as unconfirmed:
            runtime.logout(context, operation_key="logout-key-0000001", confirm=False)
        assert unconfirmed.value.code == ipc_errors.INVALID_ARGUMENT
        with pytest.raises(OnboardingStateError) as stale:
            runtime.logout(
                context,
                operation_key="logout-key-0000001",
                confirm=True,
                expected_epoch="another-generation",
            )
        assert stale.value.code == ipc_errors.STALE_DAEMON_GENERATION
        assert runtime.reopen_required is False

        record = runtime.logout(
            context, operation_key="logout-key-0000001", confirm=True
        )
        assert record.state == "completed"
        assert record.removed
        assert runtime.reopen_required is True
        assert read_identity_snapshot(home).owner is None

        replay = runtime.logout(
            context, operation_key="logout-key-0000002", confirm=True
        )
        assert replay.operation_id == record.operation_id
        assert runtime.get_operation(context, record.operation_id) == record

        with pytest.raises(BootstrapError) as blocked:
            _initialize(runtime, surface)
        assert blocked.value.code == BOOTSTRAP_PHASE_INVALID
    finally:
        runtime.close()


def test_poll_is_bound_to_the_first_control_surface(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    runtime = _admit(home, FakeIdentity([_committed()]))
    try:
        _begin(runtime)
        other = Surface("home-binding", "other-host", "view", "generation-1")
        with pytest.raises(BootstrapError) as refused:
            runtime.poll_authentication(surface=other)
        assert refused.value.code == BOOTSTRAP_SCOPE_MISMATCH
        assert runtime.phase == "authenticating"
    finally:
        runtime.close()


def test_committed_worker_without_matching_settings_never_authenticates(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    runtime = _admit(home, FakeIdentity([_committed("bob@example.test")]))
    try:
        _begin(runtime)
        _write_identity(home, owner=OWNER)
        with pytest.raises(RuntimeError, match="public identity"):
            runtime.poll_authentication(surface=_surface())
        assert runtime.phase == "authenticating"
        assert BootstrapRecord.read(home / "state").phase == "authenticating"
    finally:
        runtime.close()


def test_dispatched_begin_failure_is_not_replayed(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    fake = FakeIdentity(begin_errors=[DispatchedFailure("worker outcome unknown")])
    runtime = _admit(home, fake)
    try:
        with pytest.raises(DispatchedFailure):
            _begin(runtime)
        assert fake.closed
        assert runtime.phase == "awaiting-auth"
        with pytest.raises(BootstrapError) as replay:
            _begin(runtime)
        assert replay.value.code == BOOTSTRAP_OPERATION_CONFLICT
    finally:
        runtime.close()


def test_unconfirmed_begin_failure_remains_retryable(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    fake = FakeIdentity([_committed()], begin_errors=[ValueError("invalid profile")])
    runtime = _admit(home, fake)
    try:
        with pytest.raises(ValueError):
            _begin(runtime)
        assert runtime.phase == "awaiting-auth"
        begin = _begin(runtime)
        assert begin.operation_id == "op-auth-0001"
        assert runtime.phase == "authenticating"
    finally:
        runtime.close()


def test_daemon_construction_failure_marks_initialize_failed_and_keeps_locks(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    runtime = _admit(home, FakeIdentity([_committed()]))
    try:
        _begin(runtime)
        _write_identity(home)
        runtime.poll_authentication(surface=_surface())
        _initialize(runtime)

        def failing_factory():
            raise RuntimeError("daemon construction failed")

        with pytest.raises(RuntimeError, match="construction"):
            runtime.run_daemon(failing_factory)
        status = runtime.status(_status_context())
        states = {item["kind"]: item["state"] for item in status["operations"]}
        assert states["initialize"] == "failed"
        with pytest.raises(BootstrapError) as retry:
            runtime.run_daemon(lambda: None)
        assert retry.value.code == BOOTSTRAP_PROMOTION_CONFLICT
        with pytest.raises(IdentityTransactionBusy):
            IdentityTransactionLock.acquire(home, timeout=0)
    finally:
        runtime.close()
    with IdentityTransactionLock.acquire(home, timeout=0):
        pass


def test_quiesce_registration_is_refused_after_promotion(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    runtime = _admit(home, FakeIdentity([_committed()]))
    try:
        _begin(runtime)
        _write_identity(home)
        runtime.poll_authentication(surface=_surface())
        _initialize(runtime)
        with pytest.raises(BootstrapError) as refused:
            runtime.register_quiesce(lambda: None)
        assert refused.value.code == BOOTSTRAP_PHASE_INVALID
    finally:
        runtime.close()
