# Deflake (2026-11-03, workflow wf-ff332cef #implDF): the missing-cwd
# lifecycle settlement and close_runtime budgets were hand-coded 2.0s
# wall-clock bets; under CI load (runs 45211/45455) the settle loop expired
# before the actor chain (pykka actor -> persistence write -> io pool ->
# event sink) could publish LifecycleMutationFailed, and close_runtime then
# raced the still-unwinding actor. The budgets now come from the CI-declared
# HYPRIAL_TEST_WAIT_SECONDS knob (see ../support.py WAIT_TIMEOUT_SECONDS);
# the loop still waits on the real condition (the failure event) and the
# content assertions are unchanged: code, detail path, no process launched,
# cwd never created.
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import inspect
import pytest
from hyprial.kernel.impl.actor_runtime import ActorState, AdmissionResult
from hyprial.kernel.impl.contracts.ports import PortAdmission, PortCommandRejected
from hyprial.kernel import HarnessLaunchSpec, LifecycleMutationRequest
from hyprial.daemon.impl.api import HarnessDelivery, HarnessResult
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.harnesses.actor import HarnessProjection, HarnessRuntimeActor, HarnessRuntimeClosed, HarnessRuntimeFacade, ProcessIdentity, ProcessIoPort
from hyprial.daemon.impl.harnesses.pi.loader import resolve_approved_pi_project
from hyprial.daemon.impl.harnesses.pi.rpc_process import PiRpcProcess
from hyprial.daemon.impl.harnesses.runtime.ports import EnsureHarnessCommand, HarnessLaunchProjection
from hyprial.daemon.impl.lifecycle.lifecycle_receipts import LifecycleMutationFailed
from hyprial.daemon.impl.processes.supervisor import ManagedHarnessRuntime

from ..support import (
    _Launcher,
    _Process,
    _dead_pid,
    _safety_contract_errors,
    _spec,
    _stopping_port,
    _wait_for,
    WAIT_TIMEOUT_SECONDS,
)


def test_missing_cwd_is_rejected_before_facade_submission(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = (tmp_path / "missing-worktree").resolve()
    actor = HarnessRuntimeActor(_Launcher())
    facade = HarnessRuntimeFacade(actor, reply_timeout=0.05)
    submitted: list[object] = []
    monkeypatch.setattr(actor, "submit", submitted.append)

    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError) as caught:
            facade.start(HarnessLaunchSpec("codex", "worker", True, cwd=str(missing)))
        assert time.monotonic() - started < 1.0
        assert getattr(caught.value, "code", None) == "HARNESS_CWD_MISSING"
        assert str(missing) in str(caught.value)
        assert submitted == []
        assert not missing.exists()
    finally:
        assert actor.close_runtime(2.0)


def test_missing_cwd_lifecycle_fails_fast_with_code_and_path(tmp_path) -> None:
    missing = (tmp_path / "missing-worktree").resolve()
    events: list[object] = []
    launcher = _Launcher()
    actor = HarnessRuntimeActor(
        launcher,
        desired_state=DesiredStateStore(tmp_path / "desired.json"),
        event_sink=events.append,
        failure_budget=1,
    )
    request = LifecycleMutationRequest(
        "cwd-fast-fail",
        "cwd-fast-fail:forward:0",
        "cwd-fast-fail",
        None,
        EnsureHarnessCommand(
            "cwd-fast-fail-payload",
            HarnessLaunchProjection("codex", "worker", True, cwd=str(missing)),
        ),
    )

    started = time.monotonic()
    try:
        assert actor.submit(request) is PortAdmission.ACCEPTED
        deadline = started + WAIT_TIMEOUT_SECONDS
        failure = None
        while failure is None:
            failure = next(
                (
                    event
                    for event in events
                    if isinstance(event, LifecycleMutationFailed)
                    and event.correlation_id == request.correlation_id
                ),
                None,
            )
            assert time.monotonic() < deadline, "missing cwd did not settle promptly"
            time.sleep(0.005)
        assert failure.code == "HARNESS_CWD_MISSING"
        assert str(missing) in failure.detail
        assert launcher.processes == []
        assert not missing.exists()
    finally:
        assert actor.close_runtime(WAIT_TIMEOUT_SECONDS)


@pytest.mark.parametrize("with_marker", [False, True], ids=["empty", "existing"])
def test_precreated_cwd_starts_without_modification(tmp_path, with_marker: bool) -> None:
    cwd = tmp_path / "worktree"
    cwd.mkdir()
    marker = cwd / "owner.txt"
    if with_marker:
        marker.write_text("keep", encoding="utf-8")
    before = tuple(cwd.iterdir())
    supervisor = ManagedHarnessRuntime(_Launcher())

    try:
        assert supervisor.start(
            HarnessLaunchSpec("codex", "worker", True, cwd=str(cwd))
        )
        assert tuple(cwd.iterdir()) == before
        if with_marker:
            assert marker.read_text(encoding="utf-8") == "keep"
    finally:
        supervisor.stop()


def test_start_timeout_fences_and_stops_late_process() -> None:
    release = threading.Event()
    launched: list[_Process] = []

    class _LateLauncher:
        def start(self, _spec: HarnessLaunchSpec) -> _Process:
            assert release.wait(2.0)
            process = _Process()
            launched.append(process)
            return process

    supervisor = ManagedHarnessRuntime(
        _LateLauncher(),
        start_timeout_seconds=0.05,
        failure_budget=1,
    )
    with pytest.raises(TimeoutError, match="did not complete"):
        supervisor.start(_spec())
    assert supervisor.status()[0]["state"] == "failed"
    # Deprecated wire alias: ``quarantined`` still maps to the failed state.
    assert supervisor.status()[0]["quarantined"] is True

    release.set()
    _wait_for(lambda: bool(launched) and launched[0].stops == 1)
    assert supervisor.status()[0]["running"] is False
    supervisor.stop()


def test_explicit_start_revives_failed() -> None:
    """The only revive path: an explicit start clears the failure counter.

    Failed is terminal for the reconcile loop, not for the operator.  A
    successful explicit start returns the projection to ``state=None``, and
    a failure afterwards starts a fresh count rather than resuming the old
    one -- with budget 2, one new failure must land as ``retrying``, not
    straight back at ``failed``.
    """

    class _FlakyLauncher(_Launcher):
        def __init__(self) -> None:
            super().__init__()
            self.fail = True

        def start(self, spec: HarnessLaunchSpec) -> _Process:
            if self.fail:
                raise RuntimeError("fixture refusal")
            return super().start(spec)

    now = 100.0

    def clock() -> float:
        return now

    launcher = _FlakyLauncher()
    supervisor = ManagedHarnessRuntime(
        launcher,
        clock=clock,
        restart_backoff_seconds=30.0,
        failure_budget=2,
    )
    with pytest.raises(RuntimeError, match="fixture refusal"):
        supervisor.start(_spec())
    assert "codex:worker" not in supervisor._failed
    assert supervisor.status()[0]["state"] == "retrying"

    launcher.fail = False
    assert supervisor.start(_spec()) is True
    assert supervisor._actor._records["codex:worker"].failures == 0
    assert supervisor.status()[0].get("state") is None
    assert supervisor.status()[0]["running"] is True

    # Break it again: the post-revival failure must count from zero, so one
    # new failure is ``retrying`` -- not ``failed``, which is what a leftover
    # counter would produce at budget 2.
    launcher.fail = True
    launcher.processes[-1].running = False
    supervisor.reconcile()  # retires the dead process, arms the backoff
    now += 31.0
    supervisor.reconcile()  # backoff elapsed: one retry goes out and fails
    deadline = time.monotonic() + 3.0
    while supervisor._actor._records["codex:worker"].failures == 0:
        assert time.monotonic() < deadline, "post-revival failure never charged"
        time.sleep(0.02)
        now += 31.0
        supervisor.reconcile()
    _wait_for(lambda: supervisor.status()[0].get("state") == "retrying")
    assert "codex:worker" not in supervisor._failed
    assert supervisor.status()[0]["state"] == "retrying"
    supervisor.stop()


def test_permanent_start_failure_keeps_code_and_disables_reconcile_retry() -> None:
    class _CredentialMissing(ValueError):
        code = "CLAUDE_CREDENTIAL_MISSING"
        permanent_start_failure = True

    class _RefusingLauncher:
        def __init__(self) -> None:
            self.attempts = 0

        def start(self, _spec: HarnessLaunchSpec) -> _Process:
            self.attempts += 1
            raise _CredentialMissing("fixture credential is missing")

    launcher = _RefusingLauncher()
    supervisor = ManagedHarnessRuntime(
        launcher,
        restart_backoff_seconds=0.0,
        failure_budget=5,
    )
    with pytest.raises(RuntimeError, match="fixture credential is missing") as raised:
        supervisor.start(_spec())

    assert getattr(raised.value, "code", None) == "CLAUDE_CREDENTIAL_MISSING"
    assert supervisor.status()[0]["state"] == "failed"
    for _ in range(3):
        supervisor.reconcile()
    assert launcher.attempts == 1
    supervisor.stop()


def test_async_pi_start_precondition_is_terminal_without_spending_budget(
    tmp_path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "--quiet", str(project)], check=True)
    spec = HarnessLaunchSpec(
        "pi",
        "worker",
        True,
        cwd=str(project),
        model_provider="openai",
        model="gpt-5",
    )

    class _AsyncPiLauncher:
        def __init__(self) -> None:
            self.attempts = 0
            self.processes: list[PiRpcProcess] = []

        def start(self, launch_spec: HarnessLaunchSpec) -> PiRpcProcess:
            self.attempts += 1

            def client_factory():
                resolve_approved_pi_project(
                    cwd=launch_spec.cwd,
                    runtime_args=launch_spec.args,
                    command_name=launch_spec.name,
                    command_headless=launch_spec.headless,
                    command_model_provider=launch_spec.model_provider,
                    command_model=launch_spec.model,
                    command_session_ref=launch_spec.session_ref,
                )
                raise AssertionError("missing approval unexpectedly passed")

            process = PiRpcProcess(
                launch_spec,
                client_factory=client_factory,
                reconnect_delay_seconds=0.001,
            )
            self.processes.append(process)
            return process

    launcher = _AsyncPiLauncher()
    supervisor = ManagedHarnessRuntime(
        launcher,
        restart_backoff_seconds=0.0,
        failure_budget=2,
    )
    try:
        assert supervisor.start(spec)
        _wait_for(lambda: not launcher.processes[0].running)
        for _ in range(10):
            supervisor.reconcile()
            time.sleep(0.01)

        record = supervisor._actor._records["pi:worker"]
        assert launcher.attempts == 1
        assert record.failures == 0
        assert supervisor.status()[0]["state"] == "failed"
        assert "hyprial agent start pi" in supervisor.last_errors["pi:worker"]
        assert supervisor.drain_failed_events() == ("pi:worker",)
        for _ in range(3):
            supervisor.reconcile()
        assert supervisor.drain_failed_events() == ()
    finally:
        supervisor.stop()


def test_start_preconditions_never_consume_the_restart_budget() -> None:
    class _Precondition(ValueError):
        code = "HARNESS_START_FAILED"
        permanent_start_failure = True
        start_precondition_failure = True

    class _RefusingLauncher:
        def __init__(self) -> None:
            self.attempts = 0

        def start(self, _spec: HarnessLaunchSpec) -> _Process:
            self.attempts += 1
            raise _Precondition("run: hyprial start pi --name worker -- --approve")

    launcher = _RefusingLauncher()
    supervisor = ManagedHarnessRuntime(
        launcher,
        restart_backoff_seconds=0.0,
        failure_budget=2,
    )
    try:
        for expected_attempts in range(1, 4):
            with pytest.raises(RuntimeError) as raised:
                supervisor.start(_spec())
            assert getattr(raised.value, "code", None) == "HARNESS_START_FAILED"
            record = supervisor._actor._records["codex:worker"]
            assert record.failures == 0
            assert supervisor.status()[0]["state"] == "failed"
            assert supervisor.drain_failed_events() == ("codex:worker",)
            for _ in range(3):
                supervisor.reconcile()
            assert launcher.attempts == expected_attempts
    finally:
        supervisor.stop()


def test_pid_reuse_fence_refuses_to_stop_different_identity() -> None:
    # A live PID whose birth marker changed is the only PID_REUSED case.  The
    # pid must actually exist: a missing PID is "the recorded process is dead"
    # (see test_stop_after_dead_pid_is_not_pid_reuse), not reuse.
    live_pid = os.getpid()
    marker = {live_pid: "identity-a"}

    class _PidLauncher:
        def __init__(self) -> None:
            self.process = _Process(pid=live_pid)

        def start(self, _spec: HarnessLaunchSpec) -> _Process:
            return self.process

    launcher = _PidLauncher()
    supervisor = ManagedHarnessRuntime(
        launcher,
        restart_backoff_seconds=30.0,
        process_identity_reader=marker.get,
    )
    supervisor.start(_spec())
    launcher.process.running = False
    marker[live_pid] = "identity-b"

    supervisor.reconcile()
    _wait_for(lambda: "PID_REUSED" in supervisor.last_errors.get("codex:worker", ""))

    assert launcher.process.stops == 0
    assert supervisor.status()[0]["pid"] is None
    supervisor.stop()


def test_stop_after_dead_pid_is_not_pid_reuse() -> None:
    """A missing recorded PID is dead, not reused: stop must proceed."""

    process = _Process(running=True, pid=None)
    port = _stopping_port(lambda _pid: None)

    stopped, detail = port._stop_checked(
        process,
        ProcessIdentity(_dead_pid(), "old-marker"),
        harness_id="dsh:worker",
    )

    assert stopped is True and detail is None
    assert process.stops == 1


def test_stop_uses_current_generation_identity_after_respawn() -> None:
    """A respawn's live child is stopped with its OWN identity, not refused."""

    live = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)"]
    )
    try:
        process = _Process(running=True, pid=live.pid)
        port = _stopping_port(
            lambda pid: "new-marker" if pid == live.pid else None
        )

        stopped, detail = port._stop_checked(
            process,
            ProcessIdentity(_dead_pid(), "old-marker"),
            harness_id="dsh:worker",
        )

        assert stopped is True and detail is None
        assert process.stops == 1
    finally:
        live.kill()
        live.wait()


def test_stop_still_refuses_live_pid_with_changed_marker() -> None:
    """Negative control: the fence is still there for a genuinely reused PID."""

    live_pid = os.getpid()
    process = _Process(running=True, pid=live_pid)
    port = _stopping_port(lambda _pid: "different-marker")

    stopped, detail = port._stop_checked(
        process,
        ProcessIdentity(live_pid, "expected-marker"),
        harness_id="dsh:worker",
    )

    assert stopped is False
    assert detail is not None and detail.startswith("PID_REUSED")
    assert process.stops == 0


def test_guardian_restart_reassociates_inflight_io_completion() -> None:
    wait_entered = threading.Event()
    release_wait = threading.Event()

    class _ReadyProcess(_Process):
        def wait_ready(self, *, timeout: float | None = None) -> bool:
            del timeout
            wait_entered.set()
            return release_wait.wait(2.0)

    class _ReadyLauncher:
        def start(self, _spec: HarnessLaunchSpec) -> _ReadyProcess:
            return _ReadyProcess()

    supervisor = ManagedHarnessRuntime(_ReadyLauncher())
    supervisor.start(_spec())
    errors: list[BaseException] = []
    outcomes: list[bool] = []

    def wait_ready() -> None:
        try:
            outcomes.append(supervisor.wait_ready("codex", "worker", 2.0))
        except BaseException as error:
            errors.append(error)

    waiter = threading.Thread(target=wait_ready, daemon=True)
    waiter.start()
    assert wait_entered.wait(1.0)
    actor = supervisor._actor
    first_handler_generation = actor._handler_generation
    first_handler_token = actor._handler_token

    crash_once = True

    def fail_once(_event: object) -> None:
        nonlocal crash_once
        if crash_once:
            crash_once = False
            raise RuntimeError("boom")

    actor.subscribe_events(fail_once)
    assert actor._admit(object()) is AdmissionResult.ACCEPTED
    _wait_for(lambda: actor._handler_generation > first_handler_generation)
    assert actor._handler_token != first_handler_token
    _wait_for(
        lambda: actor._runtime.snapshot(actor._handle).state
        is ActorState.RUNNING
    )
    assert actor._runtime.snapshot(actor._handle).state is ActorState.RUNNING
    assert waiter.is_alive()
    release_wait.set()
    waiter.join(1.0)
    assert errors == []
    assert outcomes == [True]
    assert supervisor.status()[0]["running"] is True
    supervisor.stop()


def test_overloaded_completion_handoff_retries_without_repeating_io() -> None:
    class _ReadyProcess(_Process):
        def __init__(self) -> None:
            super().__init__()
            self.waits = 0

        def wait_ready(self, *, timeout: float | None = None) -> bool:
            del timeout
            self.waits += 1
            return True

    class _ReadyLauncher:
        def __init__(self) -> None:
            self.process = _ReadyProcess()

        def start(self, _spec: HarnessLaunchSpec) -> _ReadyProcess:
            return self.process

    launcher = _ReadyLauncher()
    supervisor = ManagedHarnessRuntime(launcher)
    supervisor.start(_spec())
    io = supervisor._actor._io
    assert io is not None
    real_emit = io._emit
    attempts = 0

    def overloaded_twice(completion: object) -> tuple[AdmissionResult, int]:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            return AdmissionResult.OVERLOADED, supervisor._actor.generation
        return real_emit(completion)

    io._emit = overloaded_twice

    assert supervisor.wait_ready("codex", "worker", 0.1) is True
    assert attempts == 3
    assert launcher.process.waits == 1
    assert len(supervisor._actor._calls) == 0
    supervisor.stop()


def test_transient_closed_completion_handoff_reassociates_without_repeating_io() -> None:
    class _ReadyProcess(_Process):
        def wait_ready(self, *, timeout: float | None = None) -> bool:
            del timeout
            return True

    class _ReadyLauncher:
        def start(self, _spec: HarnessLaunchSpec) -> _ReadyProcess:
            return _ReadyProcess()

    supervisor = ManagedHarnessRuntime(_ReadyLauncher())
    supervisor.start(_spec())
    io = supervisor._actor._io
    assert io is not None
    real_emit = io._emit
    attempts = 0

    def closed_twice(completion: object) -> tuple[AdmissionResult, int]:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            return AdmissionResult.CLOSED, supervisor._actor.generation
        return real_emit(completion)

    io._emit = closed_twice

    assert supervisor.wait_ready("codex", "worker", 0.1) is True
    assert attempts == 3
    assert len(supervisor._actor._calls) == 0
    io._emit = real_emit
    supervisor.stop(timeout=0.01)


def test_stop_has_bounded_drain_when_process_stop_hangs() -> None:
    release = threading.Event()

    class _HungStop(_Process):
        def stop(self) -> None:
            release.wait(2.0)
            super().stop()

    class _HungStopLauncher:
        def start(self, _spec: HarnessLaunchSpec) -> _HungStop:
            return _HungStop()

    supervisor = ManagedHarnessRuntime(_HungStopLauncher())
    supervisor.start(_spec())
    started_at = time.monotonic()
    supervisor.stop(timeout=0.05)
    elapsed = time.monotonic() - started_at
    release.set()

    assert elapsed < 0.5


def test_close_retains_handle_and_dependents_until_actor_stop_confirms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = HarnessRuntimeActor(_Launcher())
    handle = actor._handle
    actor._register_call(
        "pending-close-call", actor.generation, operation="test-close"
    )
    real_stop = actor._runtime.stop
    attempts = 0

    def stop_once_unsettled(current, timeout):  # type: ignore[no-untyped-def]
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return False
        return real_stop(current, timeout)

    monkeypatch.setattr(actor._runtime, "stop", stop_once_unsettled)
    assert not actor.close_runtime(0.1)
    assert actor._handle is handle
    assert len(actor._calls) == 1

    assert actor.close_runtime(1.0)
    assert actor._handle is None
    assert len(actor._calls) == 0


def test_stop_deadline_fails_pending_caller_and_reports_incomplete_drain() -> None:
    enqueue_entered = threading.Event()
    release_enqueue = threading.Event()

    class _BlockedStreaming(_Process):
        def enqueue(self, _delivery: HarnessDelivery) -> bool:
            enqueue_entered.set()
            assert release_enqueue.wait(2.0)
            return True

        def drain_results(self) -> tuple[HarnessResult, ...]:
            return ()

        def interrupt(self, delivery_id: str, *, timeout: float = 1.0) -> bool:
            del delivery_id, timeout
            return True

    class _BlockedLauncher:
        def start(self, _spec: HarnessLaunchSpec) -> _BlockedStreaming:
            return _BlockedStreaming()

    supervisor = ManagedHarnessRuntime(_BlockedLauncher(), start_max_workers=1)
    supervisor.start(_spec())
    actor = supervisor._actor
    observed: list[object] = []
    actor.subscribe_events(observed.append)
    delivery = HarnessDelivery("d-stop", "c", "a", "worker", "hello")
    caller_errors: list[BaseException] = []

    def dispatch() -> None:
        try:
            supervisor.dispatch("worker", delivery)
        except BaseException as error:
            caller_errors.append(error)

    caller = threading.Thread(target=dispatch, daemon=True)
    caller.start()
    assert enqueue_entered.wait(1.0)
    supervisor.stop(timeout=0.05)
    caller.join(1.0)

    assert not caller.is_alive()
    assert len(caller_errors) == 1
    assert isinstance(caller_errors[0], HarnessRuntimeClosed)
    assert supervisor.drain_complete is False
    assert len(supervisor._facade._pending) == 0
    assert len(actor._calls) == 0
    assert any(
        isinstance(event, PortCommandRejected)
        and event.code == "HARNESS_RUNTIME_STOPPED"
        for event in observed
    )
    assert actor._handle is not None
    release_enqueue.set()
    _wait_for(lambda: actor._io.in_flight == 0)
    assert actor.close_runtime(2.0)
    assert actor._handle is None
    assert not any(
        isinstance(event, PortCommandRejected)
        and event.code == "HARNESS_COMPLETION_HANDOFF_FAILED"
        for event in observed
    )


def test_failed_process_stop_is_retained_for_later_full_drain() -> None:
    class RetryStopProcess(_Process):
        def stop(self) -> None:
            self.stops += 1
            if self.stops == 1:
                raise RuntimeError("observer custody remains")
            self.running = False

    class RetryStopLauncher:
        def __init__(self) -> None:
            self.process = RetryStopProcess()

        def start(self, _spec: HarnessLaunchSpec) -> RetryStopProcess:
            return self.process

    launcher = RetryStopLauncher()
    logs: list[tuple[str, str, dict[str, object]]] = []
    supervisor = ManagedHarnessRuntime(
        launcher,
        orphan_logger=lambda level, component, event, **fields: logs.append(
            (level, event, fields)
        ),
    )
    supervisor.start(_spec())

    supervisor.stop(timeout=1.0)
    assert supervisor.drain_complete is False
    assert launcher.process.stops == 1
    assert supervisor._actor._handle is not None
    assert supervisor._actor._failed_events == []
    assert [entry for entry in logs if entry[1] == "harness.stop_failed"] == [
        (
            "error",
            "harness.stop_failed",
            {"detail": "observer custody remains"},
        )
    ]

    supervisor.stop(timeout=1.0)
    assert supervisor.drain_complete is True
    assert launcher.process.stops == 2
    assert supervisor._actor._handle is None


def test_reverse_mutations_cover_receipt_stop_and_immutable_session_ref() -> None:
    handoff = inspect.getsource(ProcessIoPort._handoff)
    stop = inspect.getsource(HarnessRuntimeActor._on_stop_all)
    session_refs = inspect.getsource(HarnessProjection.read_session_refs)
    assert _safety_contract_errors(handoff, stop, session_refs) == ()
    assert "completion receipt missing" in _safety_contract_errors(
        handoff.replace("receipt.wait", "receipt_seen"), stop, session_refs
    )
    assert "same-generation claim missing" in _safety_contract_errors(
        handoff.replace("receipt.claimed_by", "receipt_unclaimed", 1),
        stop,
        session_refs,
    )
    assert "successful completion has a false deadline" in _safety_contract_errors(
        handoff + '\nraise RuntimeError("handoff remained overloaded")',
        stop,
        session_refs,
    )
    assert "stop deadline missing" in _safety_contract_errors(
        handoff, stop.replace("command.deadline_ms", "0", 1), session_refs
    )
    assert "pending caller termination missing" in _safety_contract_errors(
        handoff, stop.replace("_terminate_pending", "terminate_removed", 1), session_refs
    )
    assert "session ref dynamically reads process" in _safety_contract_errors(
        handoff, stop, session_refs + "\nprocess.session_ref"
    )
