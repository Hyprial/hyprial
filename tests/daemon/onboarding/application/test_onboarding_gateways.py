"""Bootstrap and serving onboarding request ports over the real runtime/service.

The bootstrap cases use a real ``BootstrapRuntime`` with its continuous OS
locks, a real journal under a temporary home and a fake credential worker that
echoes only the permit's public operation id.  The serving cases use the real
``OperationService``; no second journal is opened and no credential is touched.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
import pytest

from hyprial.daemon.impl.application.onboarding.errors import (
    OnboardingGatewayError,
)
from hyprial.daemon.impl.application.onboarding.provisioning import (
    DeviceProvisioningError,
)
from hyprial.daemon.impl.application.onboarding.service import (
    BootstrapOnboardingGateway,
    PromotionFinalizer,
    ServingOnboardingGateway,
    TrustedScope,
)
from hyprial.daemon.impl.onboarding.first_run import FirstRunStore
from hyprial.daemon.impl.onboarding.operations import OperationService
from hyprial.daemon.impl.onboarding.operations.service import (
    ControllerIdentity,
    RecoveryAuthority,
)
from hyprial.daemon.impl.onboarding.runtime import (
    BOOTSTRAP_OPERATION_CONFLICT,
    BootstrapError,
    BootstrapRuntime,
)
from hyprial.daemon.impl.request_catalog.onboarding import (
    AUTH_OPERATION_CONFLICT,
    CallContext,
    Surface,
)
from hyprial.daemon.impl.request_catalog.onboarding.specs import (
    DEVICE_ENSURE_FAILED,
    REGISTRATION_UNAVAILABLE,
)
from hyprial.identity import (
    AuthProfile,
    IdentityBegin,
    IdentityPublicState,
    IdentitySnapshot,
    OnboardingSubject,
    read_onboarding_subject,
)
from hyprial.kernel import ipc_errors

ISSUER = "https://issuer.example.test"
OWNER = "alice@example.test"
PROFILE = AuthProfile(ISSUER, "fake-client-id")
LOGIN_KEY = "login-key-0000001"
DEVICE_KEY = "device-key-0000001"
INITIALIZE_KEY = "initialize-key-0001"


def _surface(generation: str = "generation-1") -> Surface:
    return Surface("home-binding", "host-control", "view", generation)


def _context(
    phase: str = "bootstrap",
    caller: str = "local-bootstrap",
    *,
    owner: str | None = None,
    generation: str = "generation-1",
) -> CallContext:
    return CallContext(
        surface=_surface(generation),
        phase=phase,
        caller=caller,
        owner=owner,
    )


class FakeIdentity:
    """A credential worker that returns the permit's own public operation id."""

    def __init__(self) -> None:
        self.begin_calls: list[object] = []
        self.cancel_calls: list[str] = []
        self.closed = False
        self.cancel_error: BaseException | None = None
        self.poll_calls: list[str] = []
        self.poll_results: list[IdentityPublicState] = []

    def begin(self, *, profile, home, permit, lock_fd=None, transport=None):
        del lock_fd
        self.begin_calls.append((profile, home, permit, transport))
        return IdentityBegin(
            permit.operation_id,
            "https://verify.example.test",
            "ABCD-EFGH",
            600,
            5,
        )

    def poll_public(self, operation_id):
        self.poll_calls.append(operation_id)
        if self.poll_results:
            return self.poll_results.pop(0)
        return IdentityPublicState(
            operation_id,
            "committed",
            OWNER,
            ISSUER,
            {"credentialCommitted": True, "settingsCommitted": True},
        )

    def cancel(self, operation_id):
        self.cancel_calls.append(operation_id)
        if self.cancel_error is not None:
            raise self.cancel_error
        return IdentityPublicState(operation_id, "cancelled")

    def close(self) -> None:
        self.closed = True


@dataclass(frozen=True)
class FakeDevice:
    device_id: str


class FakeProvisioner:
    def __init__(self, *, result=None, error=None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[object, str]] = []

    def ensure(self, *, home, owner):
        self.calls.append((home, owner))
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture(autouse=True)
def empty_override(monkeypatch):
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)


def _admit(home, fake: FakeIdentity) -> BootstrapRuntime:
    return BootstrapRuntime.admit(
        home=home,
        state_dir=home / "state",
        socket_path=home / "ipc.sock",
        identity_factory=lambda: fake,
    )


def _gateway(runtime: BootstrapRuntime, **kwargs) -> BootstrapOnboardingGateway:
    kwargs.setdefault("profile_resolver", lambda _home: PROFILE)
    return BootstrapOnboardingGateway(runtime=runtime, **kwargs)


def _write_identity(
    home,
    owner: str = OWNER,
    *,
    user_id: str | None = None,
) -> None:
    record = {
        "owner": owner,
        "identityMode": "casdoor",
        "identityIssuer": ISSUER,
    }
    if user_id is not None:
        record["userId"] = user_id
    (home / "settings.json").write_text(json.dumps(record))


def _write_device(home) -> None:
    from hyprial.identity import DeviceRecord, write_device_record

    write_device_record(
        home,
        DeviceRecord(
            device_id="device-1",
            owner=OWNER,
            server_public="nodekey:server",
            client_public="nodekey:client",
            key_generation=1,
            device_uid="12345678123442348234123456789abc",
            name="laptop",
        ),
    )


def _new_runtime(tmp_path) -> tuple[BootstrapRuntime, FakeIdentity, BootstrapOnboardingGateway]:
    home = tmp_path / "home"
    home.mkdir()
    fake = FakeIdentity()
    runtime = _admit(home, fake)
    return runtime, fake, _gateway(runtime)


def _begin(
    gateway: BootstrapOnboardingGateway, surface: Surface
) -> dict:
    return gateway.begin_auth(
        _context(generation=surface.transport_generation),
        intent="login",
        operation_key=LOGIN_KEY,
    )


def _authenticate(
    runtime: BootstrapRuntime,
    fake: FakeIdentity,
    surface: Surface,
    *,
    user_id: str | None = None,
) -> None:
    _write_identity(runtime.home, user_id=user_id)
    state = runtime.poll_authentication(surface=surface)
    assert state.state == "committed"
    assert fake.closed


class FakeDaemon:
    stop_reason = "shutdown"

    def __init__(self) -> None:
        self.events: list[str] = []

    def owns_process_exit(self) -> None:
        self.events.append("owns-process-exit")

    def run(self, *, ownership_stream, identity_transaction_stream) -> None:
        self.events.append("run")
        ownership_stream.close()
        identity_transaction_stream.close()


def test_bootstrap_status_and_begin_projections(tmp_path):
    runtime, fake, gateway = _new_runtime(tmp_path)
    surface = _surface()
    try:
        status = gateway.status(_context())
        assert status["phase"] == "bootstrap"
        assert status["authenticated"] is False
        assert status["owner"] is None
        # The public status projection currently offers login+register; the
        # frozen four-state contract drops register once it is unavailable.
        # Assert the stable first action without pinning the shared line here.
        assert status["nextActions"][0] == "login"

        first = _begin(gateway, surface)
        assert first["ok"] is True
        assert first["state"] == "pending"
        assert first["owner"] is None
        assert first["intent"] == "login"
        assert first["verification"]["userCode"] == "ABCD-EFGH"
        assert first["verification"]["verificationUri"].startswith("https://")
        assert len(fake.begin_calls) == 1

        again = _begin(gateway, surface)
        assert again == first
        assert len(fake.begin_calls) == 1

        with pytest.raises(OnboardingGatewayError) as other_key:
            gateway.begin_auth(
                _context(), intent="login", operation_key="login-key-0000002"
            )
        assert other_key.value.code == ipc_errors.OPERATION_IN_PROGRESS

        with pytest.raises(OnboardingGatewayError) as wrong_caller:
            gateway.status(_context("serving", "local-gui"))
        assert wrong_caller.value.code == ipc_errors.CALLER_NOT_AUTHORIZED
    finally:
        runtime.close()


def test_bootstrap_relogin_without_an_issuer_profile_is_the_typed_failure(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    runtime = _admit(home, FakeIdentity())

    def unavailable(_home):
        raise RuntimeError("no issuer profile is available")

    gateway = _gateway(runtime, profile_resolver=unavailable)
    try:
        with pytest.raises(OnboardingGatewayError) as refused:
            gateway.relogin(_context(), operation_key="relogin-key-0123456789")
        assert refused.value.code == ipc_errors.IDENTITY_SOURCE_UNAVAILABLE
    finally:
        runtime.close()


def test_bootstrap_runtime_refuses_the_same_key_with_another_intent(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    fake = FakeIdentity()
    runtime = _admit(home, fake)
    try:
        first = runtime.begin_authentication(
            surface=_surface(),
            intent="login",
            operation_key=LOGIN_KEY,
            operation_id="op-login-0001",
            profile=PROFILE,
        )
        cached = runtime.begin_authentication(
            surface=_surface(),
            intent="login",
            operation_key=LOGIN_KEY,
            operation_id="op-login-0002",
            profile=PROFILE,
        )
        assert cached is first
        assert len(fake.begin_calls) == 1
        with pytest.raises(BootstrapError) as refused:
            runtime.begin_authentication(
                surface=_surface(),
                intent="register",
                operation_key=LOGIN_KEY,
                operation_id="op-register-0001",
                profile=PROFILE,
            )
        assert refused.value.code == BOOTSTRAP_OPERATION_CONFLICT
    finally:
        runtime.close()


def test_register_is_never_faked(tmp_path):
    runtime, _fake, gateway = _new_runtime(tmp_path)
    try:
        with pytest.raises(OnboardingGatewayError) as refused:
            gateway.begin_auth(
                _context(), intent="register", operation_key="register-key-0001"
            )
        assert refused.value.code == "REGISTRATION_UNAVAILABLE"
    finally:
        runtime.close()


def test_authenticated_projection_and_committed_cancel(tmp_path):
    runtime, fake, gateway = _new_runtime(tmp_path)
    surface = _surface()
    try:
        begin = _begin(gateway, surface)
        operation_id = begin["operationId"]

        fake.poll_results.append(IdentityPublicState(operation_id, "pending"))
        pending = gateway.get(_context(), operation_id=operation_id)
        assert pending["operation"]["state"] == "pending"
        assert pending["operation"]["verification"]["userCode"] == "ABCD-EFGH"

        _authenticate(runtime, fake, surface)
        status = gateway.status(_context())
        assert status["authenticated"] is True
        assert status["owner"] == OWNER
        assert status["nextActions"] == ["device.ensure"]

        committed = gateway.get(_context(), operation_id=operation_id)
        assert committed["operation"]["state"] == "authenticated"
        assert committed["operation"]["owner"] == OWNER
        assert committed["operation"]["verification"] is None

        replay = _begin(gateway, surface)
        assert replay["operationId"] == operation_id
        assert replay["state"] == "authenticated"
        assert replay["verification"] is None

        with pytest.raises(OnboardingGatewayError) as new_key:
            gateway.begin_auth(
                _context(), intent="login", operation_key="login-key-0000002"
            )
        assert new_key.value.code == ipc_errors.IDENTITY_CONFLICT

        outcome = gateway.cancel(_context(), operation_id=operation_id)
        assert outcome["state"] == "authenticated"
        assert outcome["cancelled"] is False
        assert outcome["reason"] == "identity-committed"
    finally:
        runtime.close()


def test_get_reconciles_the_in_flight_login_exactly_once(tmp_path):
    runtime, fake, gateway = _new_runtime(tmp_path)
    surface = _surface()
    try:
        begin = _begin(gateway, surface)
        operation_id = begin["operationId"]
        _write_identity(runtime.home)

        reconciled = gateway.get(_context(), operation_id=operation_id)
        assert reconciled["operation"]["state"] == "authenticated"
        assert reconciled["operation"]["owner"] == OWNER
        assert reconciled["operation"]["verification"] is None
        assert runtime.phase == "authenticated"
        assert fake.closed
        assert fake.poll_calls == [operation_id]

        # A second get is a journal read: the terminal record is not polled.
        again = gateway.get(_context(), operation_id=operation_id)
        assert again == reconciled
        assert fake.poll_calls == [operation_id]

        status = gateway.status(_context())
        assert status["authenticated"] is True
        assert status["nextActions"] == ["device.ensure"]
    finally:
        runtime.close()


def test_cancelled_pending_operation_is_reported_and_new_key_can_start(tmp_path):
    runtime, fake, gateway = _new_runtime(tmp_path)
    surface = _surface()
    try:
        begin = _begin(gateway, surface)
        operation_id = begin["operationId"]

        outcome = gateway.cancel(_context(), operation_id=operation_id)
        assert outcome["cancelled"] is True
        assert outcome["state"] == "cancelled"
        assert outcome["reason"] == "cancelled"
        assert fake.cancel_calls == [operation_id]
        assert runtime.phase == "awaiting-auth"

        cancelled = gateway.get(_context(), operation_id=operation_id)
        assert cancelled["operation"]["state"] == "cancelled"
        assert cancelled["operation"]["verification"] is None

        with pytest.raises(OnboardingGatewayError) as replay:
            _begin(gateway, surface)
        assert replay.value.code == AUTH_OPERATION_CONFLICT

        second = gateway.begin_auth(
            _context(), intent="login", operation_key="login-key-0000002"
        )
        assert second["state"] == "pending"
        assert len(fake.begin_calls) == 2
    finally:
        runtime.close()


def test_cancel_without_a_live_worker_is_unknown_not_cancelled(tmp_path):
    runtime, fake, gateway = _new_runtime(tmp_path)
    surface = _surface()
    try:
        begin = _begin(gateway, surface)
        operation_id = begin["operationId"]
        # Simulate the process-local worker having been lost: the credential
        # side may still have run, so the durable outcome must not be rewritten.
        runtime._close_identity()

        outcome = gateway.cancel(_context(), operation_id=operation_id)

        assert outcome["state"] == "unknown"
        assert outcome["cancelled"] is False
        assert outcome["reason"] == "outcome-unknown"
        assert fake.cancel_calls == []
        record = gateway.get(_context(), operation_id=operation_id)
        assert record["operation"]["state"] == "unknown"
    finally:
        runtime.close()


def test_cancel_worker_failure_is_unknown_not_cancelled(tmp_path):
    runtime, fake, gateway = _new_runtime(tmp_path)
    surface = _surface()
    try:
        begin = _begin(gateway, surface)
        fake.cancel_error = RuntimeError("worker did not answer")
        outcome = gateway.cancel(
            _context(), operation_id=begin["operationId"]
        )
        assert outcome["state"] == "unknown"
        assert outcome["cancelled"] is False
        assert outcome["reason"] == "outcome-unknown"
        assert runtime.phase == "awaiting-auth"
    finally:
        runtime.close()


def _authenticated(tmp_path, **gateway_kwargs):
    home = tmp_path / "home"
    home.mkdir()
    fake = FakeIdentity()
    runtime = _admit(home, fake)
    gateway = _gateway(runtime, **gateway_kwargs)
    surface = _surface()
    begin = _begin(gateway, surface)
    _authenticate(runtime, fake, surface)
    return runtime, fake, gateway, surface, begin


def test_device_ensure_success_then_initialize_defers_quiesce(tmp_path):
    provisioner = FakeProvisioner(result=FakeDevice("device-1"))
    runtime, _fake, gateway, surface, _begin_result = _authenticated(
        tmp_path, provisioner=provisioner
    )
    try:
        device = gateway.ensure_device(
            _context(), operation_key=DEVICE_KEY
        )
        assert device["deviceReady"] is True
        assert device["identityCommitted"] is True
        assert device["deviceId"] == "device-1"
        assert device["retryable"] is False
        assert provisioner.calls == [(runtime.home, OWNER)]

        status = gateway.status(_context())
        assert status["deviceReady"] is True
        assert status["nextActions"] == ["initialize"]

        quiesced: list[str] = []
        runtime.register_quiesce(lambda: quiesced.append("closed-listener"))

        result = gateway.initialize(
            _context(), operation_key=INITIALIZE_KEY
        )
        assert result["state"] == "promoting"
        assert result["initialized"] is False
        assert result["cancelable"] is False
        assert quiesced == [], "the reply frame has not been written yet"

        action = gateway.take_post_reply()
        assert callable(action)
        assert quiesced == []
        action()
        assert quiesced == ["closed-listener"]
        assert gateway.take_post_reply() is None
        action()
        assert quiesced == ["closed-listener"]

        daemon = FakeDaemon()
        runtime.run_daemon(lambda: daemon, owns_process_exit=True)
        assert daemon.events == ["owns-process-exit", "run"]
        status = gateway.status(_context())
        assert status["initialized"] is False
        states = {item["kind"]: item["state"] for item in status["operations"]}
        assert states["initialize"] == "promoting"
        # The serving-ready finalizer calls this only after daemon.ready and
        # the transport-generation migration; the runtime seam is idempotent.
        runtime.complete_initialize()
        assert gateway.status(_context())["initialized"] is True
        completed = gateway.initialize(_context(), operation_key="another-initialize-key")
        assert completed["operationId"] == result["operationId"]
        assert completed["state"] == "completed"
        assert len(provisioner.calls) == 1
    finally:
        runtime.close()


def test_run_daemon_quiesces_a_promotion_whose_reply_was_never_consumed(tmp_path):
    provisioner = FakeProvisioner(result=FakeDevice("device-1"))
    runtime, _fake, gateway, _surface_value, _result = _authenticated(
        tmp_path, provisioner=provisioner
    )
    try:
        gateway.ensure_device(_context(), operation_key=DEVICE_KEY)
        quiesced: list[str] = []
        runtime.register_quiesce(lambda: quiesced.append("closed-listener"))
        gateway.initialize(_context(), operation_key=INITIALIZE_KEY)
        assert quiesced == []

        daemon = FakeDaemon()

        def factory():
            assert quiesced == ["closed-listener"]
            return daemon

        runtime.run_daemon(factory)
        assert quiesced == ["closed-listener"]
        status = gateway.status(_context())
        assert status["initialized"] is False
        states = {item["kind"]: item["state"] for item in status["operations"]}
        assert states["initialize"] == "promoting"
    finally:
        runtime.close()


def test_initialize_requires_a_verified_owner(tmp_path):
    runtime, _fake, gateway = _new_runtime(tmp_path)
    try:
        with pytest.raises(OnboardingGatewayError) as refused:
            gateway.initialize(_context(), operation_key=INITIALIZE_KEY)
        assert refused.value.code == ipc_errors.IDENTITY_UNBOUND
    finally:
        runtime.close()


def test_initialize_prepares_device_once_and_reuses_the_promotion_receipt(tmp_path):
    provisioner = FakeProvisioner(result=FakeDevice("device-1"))
    runtime, _, gateway, _, _ = _authenticated(tmp_path, provisioner=provisioner)
    try:
        first = gateway.initialize(_context(), operation_key=INITIALIZE_KEY)
        assert first["state"] == "promoting"
        assert gateway.status(_context())["deviceReady"] is True
        assert provisioner.calls == [(runtime.home, OWNER)]
        assert gateway.initialize(_context(), operation_key=INITIALIZE_KEY) == first
        assert len(provisioner.calls) == 1
    finally:
        runtime.close()


@pytest.mark.parametrize("state", ["pending", "unknown"])
def test_initialize_refuses_unresolved_device_receipts_without_provisioning(tmp_path, state):
    provisioner = FakeProvisioner(result=FakeDevice("device-1"))
    runtime, _, gateway, surface, _ = _authenticated(tmp_path, provisioner=provisioner)
    try:
        record = runtime.begin_device(surface=surface, operation_key=DEVICE_KEY,
                                      operation_id="device-pending", receipt_id="device-receipt")
        if state == "unknown":
            runtime.mark_device(surface=surface, operation_id=record.operation_id, state=state)
        for key in (INITIALIZE_KEY, "initialize-key-another"):
            with pytest.raises(OnboardingGatewayError) as refused:
                gateway.initialize(_context(), operation_key=key)
            assert refused.value.code == ipc_errors.SUBMIT_OUTCOME_UNKNOWN
            assert refused.value.data["retryable"] is False
            assert refused.value.data["operationId"] == record.operation_id
        assert provisioner.calls == []
        assert not any(row["kind"] == "initialize" for row in gateway.status(_context())["operations"])
    finally:
        runtime.close()


@pytest.mark.parametrize("outcome_unknown", [False, True])
def test_initialize_preserves_device_failure_and_does_not_promote(tmp_path, outcome_unknown):
    provisioner = FakeProvisioner(error=DeviceProvisioningError("device failed", outcome_unknown=outcome_unknown))
    runtime, _, gateway, _, _ = _authenticated(tmp_path, provisioner=provisioner)
    try:
        for _ in range(2):
            with pytest.raises(OnboardingGatewayError) as refused:
                gateway.initialize(_context(), operation_key=INITIALIZE_KEY)
            assert refused.value.code == (ipc_errors.SUBMIT_OUTCOME_UNKNOWN if outcome_unknown else DEVICE_ENSURE_FAILED)
        assert len(provisioner.calls) == 1
        assert runtime.phase == "authenticated"
        assert gateway.take_post_reply() is None
    finally:
        runtime.close()


def test_device_definite_failure_is_terminal_for_the_same_key(tmp_path):
    provisioner = FakeProvisioner(
        error=DeviceProvisioningError("sidecar keygen failed")
    )
    runtime, _fake, gateway, _surface_value, _result = _authenticated(
        tmp_path, provisioner=provisioner
    )
    try:
        with pytest.raises(OnboardingGatewayError) as refused:
            gateway.ensure_device(_context(), operation_key=DEVICE_KEY)
        assert refused.value.code == DEVICE_ENSURE_FAILED
        assert refused.value.data["identityCommitted"] is True
        assert refused.value.data["deviceReady"] is False
        assert refused.value.data["retryable"] is True
        assert refused.value.data["reason"] == "device-unavailable"

        with pytest.raises(OnboardingGatewayError) as terminal:
            gateway.ensure_device(_context(), operation_key=DEVICE_KEY)
        assert terminal.value.code == DEVICE_ENSURE_FAILED
        assert terminal.value.data["retryable"] is False
    finally:
        runtime.close()


def test_device_outcome_unknown_is_not_retryable_blindly(tmp_path):
    provisioner = FakeProvisioner(
        error=DeviceProvisioningError(
            "sidecar dispatch outcome is unknown", outcome_unknown=True
        )
    )
    runtime, fake, gateway, _surface_value, _result = _authenticated(
        tmp_path, provisioner=provisioner
    )
    try:
        with pytest.raises(OnboardingGatewayError) as refused:
            gateway.ensure_device(_context(), operation_key=DEVICE_KEY)
        assert refused.value.code == ipc_errors.SUBMIT_OUTCOME_UNKNOWN
        assert refused.value.data["identityCommitted"] is True
        assert refused.value.data["deviceReady"] is False
        assert refused.value.data["retryable"] is False
        assert refused.value.data["reason"] == "outcome-unknown"
        assert refused.value.data["receiptId"] is not None
        assert len(provisioner.calls) == 1
        assert len(fake.begin_calls) == 1
    finally:
        runtime.close()


def test_promotion_finalizer_needs_both_ready_and_migration():
    first: list[str] = []
    finalizer = PromotionFinalizer(lambda: first.append("completed"))
    finalizer.mark_daemon_ready()
    assert first == []
    finalizer.mark_generation_migrated()
    assert first == ["completed"]
    finalizer.mark_daemon_ready()
    finalizer.mark_generation_migrated()
    assert first == ["completed"]

    second: list[str] = []
    reverse = PromotionFinalizer(lambda: second.append("completed"))
    reverse.mark_generation_migrated()
    assert second == []
    reverse.mark_daemon_ready()
    assert second == ["completed"]


def test_trusted_scope_rejects_mixed_pairs():
    scope = TrustedScope(_surface(), "bootstrap", "local-bootstrap")
    assert scope.phase == "bootstrap"
    with pytest.raises(ValueError, match="trusted scope"):
        TrustedScope(_surface(), "bootstrap", "local-gui")


def _serving(tmp_path, *, owner=OWNER, complete=None, promotion_operation_id=None):
    home = tmp_path / "home"
    home.mkdir(parents=True)
    service = OperationService.open(tmp_path / "operations")
    gateway = ServingOnboardingGateway(
        operations=service,
        home=home,
        promotion_operation_id=promotion_operation_id,
        complete_initialize=complete,
        identity_reader=lambda _home: IdentitySnapshot(owner=owner),
    )
    return service, gateway


def test_serving_status_uses_the_public_identity_not_the_context(tmp_path):
    _service, gateway = _serving(tmp_path)
    status = gateway.status(_context("serving", "local-gui", owner="spoofed"))
    assert status["phase"] == "serving"
    assert status["authenticated"] is True
    assert status["owner"] == OWNER

    with pytest.raises(OnboardingGatewayError) as wrong_pair:
        gateway.status(_context())
    assert wrong_pair.value.code == ipc_errors.CALLER_NOT_AUTHORIZED


def test_serving_begin_never_fakes_an_identity_or_a_registration(tmp_path):
    service, gateway = _serving(tmp_path / "second", owner=None)
    del service
    with pytest.raises(OnboardingGatewayError) as unavailable:
        gateway.begin_auth(
            _context("serving", "local-gui"),
            intent="login",
            operation_key="serving-key-0002",
        )
    assert unavailable.value.code == ipc_errors.IDENTITY_SOURCE_UNAVAILABLE

    service, gateway = _serving(tmp_path / "third")
    del service
    with pytest.raises(OnboardingGatewayError) as register:
        gateway.begin_auth(
            _context("serving", "local-gui"),
            intent="register",
            operation_key="serving-key-0003",
        )
    assert register.value.code == REGISTRATION_UNAVAILABLE

    with pytest.raises(OnboardingGatewayError) as bootstrap_frame:
        gateway.status(_context("serving", "local-bootstrap"))
    assert bootstrap_frame.value.code == ipc_errors.CALLER_NOT_AUTHORIZED


def test_serving_begin_replays_only_the_same_visible_key(tmp_path):
    service, gateway = _serving(tmp_path)
    record = service.begin_auth(
        _context(owner=OWNER), "login", "serving-key-0001", "op-auth-1"
    )

    replay = gateway.begin_auth(
        _context("serving", "local-gui"),
        intent="login",
        operation_key="serving-key-0001",
    )
    assert replay["operationId"] == record.operation_id
    assert replay["state"] == "pending"
    assert replay["verification"] is None

    with pytest.raises(OnboardingGatewayError) as busy:
        gateway.begin_auth(
            _context("serving", "local-gui"),
            intent="login",
            operation_key="serving-key-0002",
        )
    assert busy.value.code == ipc_errors.OPERATION_IN_PROGRESS


def test_serving_cancel_reconciles_pending_and_committed_records(tmp_path):
    service, gateway = _serving(tmp_path / "unbound", owner=None)
    pending = service.begin_auth(
        _context(), "login", "serving-key-0003", "op-auth-3"
    )
    outcome = gateway.cancel(
        _context("serving", "local-gui"),
        operation_id=pending.operation_id,
    )
    assert outcome["state"] == "unknown"
    assert outcome["cancelled"] is False
    assert outcome["reason"] == "outcome-unknown"

    service, gateway = _serving(tmp_path / "committed")
    record = service.begin_auth(
        _context(), "login", "serving-key-0004", "op-auth-4"
    )
    service.mark_authenticated(_context(), record.operation_id, OWNER)
    outcome = gateway.cancel(
        _context("serving", "local-gui"),
        operation_id=record.operation_id,
    )
    assert outcome["state"] == "authenticated"
    assert outcome["cancelled"] is False
    assert outcome["reason"] == "identity-committed"


def test_serving_refuses_bootstrap_only_methods(tmp_path):
    _service, gateway = _serving(tmp_path)
    serving = _context("serving", "local-gui")
    with pytest.raises(OnboardingGatewayError) as device:
        gateway.ensure_device(serving, operation_key=DEVICE_KEY)
    assert device.value.code == ipc_errors.CALLER_NOT_AUTHORIZED
    with pytest.raises(OnboardingGatewayError) as initialize:
        gateway.initialize(serving, operation_key=INITIALIZE_KEY)
    assert initialize.value.code == ipc_errors.CALLER_NOT_AUTHORIZED


def test_serving_migrate_generation_releases_the_finalizer_once(tmp_path):
    completed: list[str] = []
    service, gateway = _serving(
        tmp_path,
        complete=lambda: completed.append("completed"),
        promotion_operation_id="op-initialize-1",
    )
    bootstrap = _context(owner=OWNER)
    record = service.accept_initialize(
        bootstrap,
        INITIALIZE_KEY,
        "op-initialize-1",
        "receipt-initialize-1",
        promotion=lambda _record: None,
    )
    assert record.state == "promoting"
    controller = ControllerIdentity.current()
    authority = RecoveryAuthority(
        home_binding=record.surface.home_binding,
        host_control_session_id=record.surface.host_control_session_id,
        view_session_id=record.surface.view_session_id,
        owner=record.account_owner,
        controller_pid=controller.pid,
        controller_birth=controller.birth_identity,
    )

    gateway.on_daemon_ready()
    assert completed == []

    serving = _context("serving", "local-gui", generation="generation-2")
    migrated = gateway.migrate_generation(serving, authority=authority)
    assert migrated.surface.transport_generation == "generation-2"
    assert completed == ["completed"]

    gateway.migrate_generation(serving, authority=authority)
    gateway.on_daemon_ready()
    assert completed == ["completed"]

    status = gateway.status(serving)
    operations = {item["operationId"]: item for item in status["operations"]}
    assert operations[record.operation_id]["state"] == "promoting"


@pytest.mark.parametrize('code,expected', [
    ('DEVICE_CODE_EXPIRED', 'expired'),
    ('ACCESS_DENIED', 'denied'),
    ('IDENTITY_WORKER_FAILURE', 'unknown'),
])
def test_worker_terminal_auth_outcome_is_preserved(tmp_path, code, expected):
    runtime, fake, gateway = _new_runtime(tmp_path)
    try:
        first = _begin(gateway, _surface())
        fake.poll_results.append(IdentityPublicState(
            first['operationId'], 'failed', error_code=code,
        ))
        receipt = gateway.get(_context(), operation_id=first['operationId'])
        assert receipt['operation']['state'] == expected
        assert gateway.status(_context())['authenticated'] is False
        assert fake.closed
        if expected != 'unknown':
            second = gateway.begin_auth(
                _context(), intent='login', operation_key='login-key-second-0001',
            )
            assert second['operationId'] != first['operationId']
            assert len(fake.begin_calls) == 2
    finally:
        runtime.close()


@pytest.mark.parametrize('identity_changed,receipt', [
    (True, {}), (False, {'credentialCommitted': True}),
])
def test_expiry_cannot_hide_a_possible_identity_commit(tmp_path, identity_changed, receipt):
    runtime, fake, gateway = _new_runtime(tmp_path)
    try:
        first = _begin(gateway, _surface())
        if identity_changed:
            _write_identity(runtime.home)
        fake.poll_results.append(IdentityPublicState(
            first['operationId'], 'failed', receipt=receipt, error_code='DEVICE_CODE_EXPIRED',
        ))
        result = gateway.get(_context(), operation_id=first['operationId'])
        assert result['operation']['state'] == 'unknown'
    finally:
        runtime.close()


def test_bootstrap_status_reads_the_subject_first_run_receipt_after_login(tmp_path):
    runtime, fake, gateway = _new_runtime(tmp_path)
    try:
        surface = _surface()
        subject = OnboardingSubject(
            issuer=ISSUER,
            user_id="u-1",
            device_uid="12345678123442348234123456789abc",
        )
        FirstRunStore(runtime.operation_service.root).confirm(
            subject,
            step="configuration",
            expected_revision=0,
            operation_key="opkey-0123456789abcdef",
            now="2026-11-18T00:00:00Z",
        )
        assert gateway.status(_context())["firstRun"]["state"] == "none"
        _begin(gateway, surface)
        _authenticate(runtime, fake, surface, user_id="u-1")
        _write_device(runtime.home)
        assert read_onboarding_subject(runtime.home) == subject
        status = gateway.status(_context())
        assert status["firstRun"] == {
            "state": "recorded",
            "revision": 1,
            "confirmed": ["configuration"],
            "updatedAt": "2026-11-18T00:00:00Z",
            "skipped": False,
        }
    finally:
        runtime.close()


def test_serving_status_reads_the_same_first_run_projection(tmp_path):
    service, gateway = _serving(tmp_path)
    home = tmp_path / "home"
    _write_identity(home, user_id="u-1")
    _write_device(home)
    subject = read_onboarding_subject(home)
    assert subject is not None
    FirstRunStore(service.root).confirm(
        subject,
        step="configuration",
        expected_revision=0,
        operation_key="opkey-0123456789abcdef",
        now="2026-11-18T00:00:00Z",
    )
    status = gateway.status(_context("serving", "local-gui"))
    assert status["authenticated"] is True
    assert status["firstRun"] == {
        "state": "recorded",
        "revision": 1,
        "confirmed": ["configuration"],
        "updatedAt": "2026-11-18T00:00:00Z",
        "skipped": False,
    }


def test_status_keeps_a_corrupt_first_run_record_as_corrupt(tmp_path):
    service, gateway = _serving(tmp_path)
    home = tmp_path / "home"
    _write_identity(home, user_id="u-1")
    _write_device(home)
    path = service.root / "first-run.json"
    path.write_bytes(b'{"version": 1, "records": [')
    path.chmod(0o600)
    status = gateway.status(_context("serving", "local-gui"))
    assert status["firstRun"]["state"] == "corrupt"
    assert path.read_bytes() == b'{"version": 1, "records": ['
