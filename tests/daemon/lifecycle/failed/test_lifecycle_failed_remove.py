"""104164aa: deterministic failed down through the production composition.

Only the process and network edges are injected. Domain events, receipts,
manager, desired store and application down handler are real.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest

from tests.support.runtime._daemon_diagnostics import write as write_diagnostic

from hyprial.daemon.impl.application import DaemonRequestError, _LocalPresence
from hyprial.daemon.impl.composition import CorrelatedDomainEvents, HarnessPortClient
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.actor.actor import HarnessRuntimeActor
from hyprial.daemon.impl.lifecycle_manager import LifecycleOperation
from hyprial.daemon.impl.lifecycle.lifecycle_receipts import (
    LifecycleMutationFailed,
    )
from hyprial.kernel import LifecycleMutationCompleted
from hyprial.kernel import LifecycleMutationRequest
from hyprial.daemon.impl.harnesses.runtime.ports import RemoveHarnessCommand
from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.kernel import (
    LIFECYCLE_OPERATION_DEADLINE_SECONDS,
    LIFECYCLE_WAIT_MARGIN_SECONDS,
)
from hyprial.kernel.impl.actor_runtime import AdmissionResult
from tests.daemon.application.composition.composition.test_final_composition_lifecycle import _Inbox, _LocalDelivery, _Transport, _isolated_application


#: How long mode E's remove step waits for its (deterministic) IO failure:
#: as long as the product lets any lifecycle operation run.
E_COMPLETION_WAIT_SECONDS = LIFECYCLE_OPERATION_DEADLINE_SECONDS


class HeldProcess:
    """Actual child, but SDK-style public pid unavailable during startup."""

    pid = None

    def __init__(self, mode: str, release: threading.Event) -> None:
        self.child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(900)"])
        self.mode = mode
        self.release = release
        self.entered = threading.Event()
        self.running = True
        self.stop_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1
        self.running = False
        self.entered.set()
        if self.mode == "E" and not self.release.is_set():
            raise RuntimeError("104164aa deterministic stop rejection")
        if self.mode == "H":
            self.release.wait()
        if self.child.poll() is None:
            self.child.terminate()
        self.child.wait(timeout=5)


class HeldLauncher:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.release = threading.Event()
        self.processes: list[HeldProcess] = []

    def start(self, _spec: HarnessLaunchSpec) -> HeldProcess:
        process = HeldProcess(self.mode, self.release)
        self.processes.append(process)
        return process

    def close(self) -> None:
        self.release.set()
        for process in self.processes:
            if process.child.poll() is None:
                process.child.terminate()
            process.child.wait(timeout=5)


class RemoveRig:
    def __init__(self, root: Path, mode: str, *, deadline: float = 0.3) -> None:
        self.root = root
        self.closed = False
        self.app = _isolated_application(
            state_dir=root / "state", socket_path=root / "state" / "daemon.sock",
            node_id="bi3-node", owner="bi3-owner",
            hyprial_home=root / "home",
            network_isolated=True,
        )
        self.launcher = HeldLauncher(mode)
        self.events = CorrelatedDomainEvents()
        self.observed: list[object] = []
        self.actor = HarnessRuntimeActor(
            self.launcher, desired_state=self.app.desired_state,
            event_sink=self.events, lifecycle_retry_base_seconds=0.01,
            restart_backoff_seconds=0.0,
        )
        self.actor.subscribe_events(self.observed.append)
        self.client = HarnessPortClient(self.actor, self.events)
        self.app._start_lifecycle_manager(
            transport=_Transport(), inbox=_Inbox(), local_delivery=_LocalDelivery(),
            harnesses=self.client,
        )
        self.manager = self.app._lifecycle_manager
        assert self.manager is not None
        self.operation_deadline = deadline
        self.app._inbox = _Inbox()
        self.app._harnesses = self.client
        self.app._presence = Mock(spec=_LocalPresence)
        self.app._restore_done.set()
        self.spec = HarnessLaunchSpec("pi", "bi3-worker", True)
        self.key = "pi:bi3-worker"

    @contextmanager
    def failure_budget(self) -> Iterator[None]:
        """Scope the narrow 0.3s failure budget to one operation under test.

        The budget is what makes a failed removal fail fast, so it belongs on
        the failed-removal path only.  It must never be active while a *real*
        lifecycle operation runs: this rig runs real creates after the failed
        removal, and the same create that takes 0.05s on an idle host takes
        0.3s-1.8s inside the CI unit job's 2-CPU cgroup -- the budget's own
        value.  While ``start()`` installed it permanently, the manager killed
        roughly a quarter of those creates (task 10041 / 10064: "lifecycle
        operation exceeded its 0.3s deadline with a step still unresolved").

        Entering it explicitly makes "only the fast-failure path is under the
        budget" a property of the call graph rather than of the order in which
        a test happens to drive the rig, so a real operation added later cannot
        inherit the budget by accident.
        """
        engine = self.manager._effects_engine
        previous = (
            engine._operation_deadline,
            engine._completion_timeout,
        )
        engine._operation_deadline = self.operation_deadline
        if self.operation_deadline < 1 and self.launcher.mode == "H":
            engine._completion_timeout = 0.02
        if self.launcher.mode == "E":
            # E's failure is deterministic (stop raises), so the step must
            # wait for that event rather than race it: with the manager's
            # default 2s completion wait, a stop reached later than 2s left
            # the step unresolved past the 0.3s deadline and the operation
            # was classified LIFECYCLE_OPERATION_TIMEOUT instead of
            # HARNESS_IO_FAILED (task 15736; reproduced by delaying E's stop
            # by 2.5s).  The deadline itself stays narrow.
            engine._completion_timeout = E_COMPLETION_WAIT_SECONDS
        try:
            yield
        finally:
            (
                engine._operation_deadline,
                engine._completion_timeout,
            ) = previous

    def start(self) -> None:
        # The real setup lifecycle runs under the manager's normal slow-step
        # budget.  The narrow failure budget is deliberately NOT installed
        # here: it is scoped to ``down()`` / explicit ``failure_budget()``
        # blocks, so no later real operation can inherit it. Do not override
        # the application terminal wait either: a 5s caller timeout expires
        # before the manager's 70s deadline, abandoning a healthy create under
        # load and preventing this fixture from reaching the removal test.
        self.app._run_lifecycle_operation(LifecycleOperation.create(
            "bi3-create", self.app._lifecycle_spec(self.spec)
        ))
        assert len(self.launcher.processes) == 1

    def down(self) -> tuple[str, float]:
        started = time.monotonic()
        try:
            with self.failure_budget():
                self.app.handle("down", {"target": self.spec.name})
        except DaemonRequestError as error:
            return error.code, time.monotonic() - started
        return "SUCCESS", time.monotonic() - started

    def snapshot(self) -> dict[str, object]:
        state = self.app.desired_state.load()
        with self.app.state_db.read() as db:
            cursor = db.execute(
                "SELECT * FROM lifecycle_operations WHERE operation_id LIKE 'lifecycle-down:%'"
            )
            operations = [dict(zip((column[0] for column in cursor.description), row)) for row in cursor]
            cursor = db.execute(
                "SELECT * FROM lifecycle_effects WHERE operation_id LIKE 'lifecycle-down:%'"
            )
            effects = [dict(zip((column[0] for column in cursor.description), row)) for row in cursor]
        return {
            "harnesses": [spec.to_json() for spec in state.harnesses],
            "receipts": [item.to_json() for item in state.lifecycle_receipts],
            "records": [item.harness_id for item in self.actor.read_harnesses()],
            "operations": operations, "effects": effects,
            "processes": [{"pid": p.child.pid, "alive": p.child.poll() is None,
                           "stopCalls": p.stop_calls, "entered": p.entered.is_set()}
                          for p in self.launcher.processes],
            "release": self.launcher.release.is_set(),
            "failures": [{"code": event.code, "attempt": event.attempt_token}
                         for event in self.observed if isinstance(event, LifecycleMutationFailed)],
        }

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.launcher.release.set()
        # Keep the original drain result and deadline. If CI fails here, name
        # the domain rather than losing that fact behind three registry FDs.
        drains: list[dict[str, object]] = []
        domains = self.app._agent_session_domains
        originals = []
        for name, owner in (("session", domains.session), ("agent", domains.agent)):
            original = owner.drain
            originals.append((owner, original))

            def observed(timeout=5.0, *, _name=name, _original=original):
                started = time.monotonic()
                report = _original(timeout)
                drains.append({
                    "domain": _name, "timeoutSeconds": timeout,
                    "elapsedSeconds": time.monotonic() - started,
                    "complete": report.complete,
                    "remainingCount": len(report.remaining),
                })
                return report

            owner.drain = observed
        try:
            self.app._close()
        except BaseException:
            try:
                write_diagnostic("failed-remove-domain-drain", {"drains": drains})
            except Exception:
                pass  # diagnostic failure cannot replace the original failure
            raise
        finally:
            for owner, original in originals:
                owner.drain = original
            self.launcher.close()


@pytest.fixture(params=["E", "H"])
def failed_remove(request: pytest.FixtureRequest, tmp_path: Path):
    rig = RemoveRig(tmp_path, request.param)
    try:
        rig.start()
        code, elapsed = rig.down()
        rig.down_result = (code, elapsed)
        print("immediate", request.param, code, elapsed, rig.snapshot(), flush=True)
        # The code check is a setup precondition: this fixture exists to hand a
        # test a *failed* removal, so it verifies it produced one.  How long
        # that took is not a precondition -- it is a claim about the system,
        # and it belongs to the test that makes it
        # (``test_failed_remove_returns_bounded_truthful_error`` bounds the
        # same stored ``elapsed`` against the budget the operation ran under).
        # Asserting it here bound the elapsed time of two tests that say
        # nothing about timing, and a fixture failure is reported as an ERROR:
        # "this operation was slower than 2s" arrived looking like "the
        # environment did not come up".
        assert code == ("HARNESS_IO_FAILED" if request.param == "E" else "LIFECYCLE_OPERATION_TIMEOUT")
        assert rig.launcher.processes[0].entered.is_set()
        assert rig.launcher.processes[0].child.poll() is None
        yield rig
    finally:
        rig.close()


def test_failed_remove_does_not_spawn_replacement(failed_remove: RemoveRig) -> None:
    """Assertion 1: independent from restart/restore assertion 2."""
    rig = failed_remove
    for _ in range(5):
        rig.client.reconcile()
        time.sleep(0.01)
    assert rig.actor.read_harness(rig.key) is None
    assert len(rig.launcher.processes) == 1


def test_failed_remove_is_absent_before_and_after_restart(failed_remove: RemoveRig) -> None:
    """Assertion 2: inspect BEFORE any shutdown/expiry could hide the bug."""
    rig = failed_remove
    assert not rig.app.desired_state.load().harnesses
    # Stop the old generation before building the new one on its database.
    rig.close()
    # A new application runs the actual previous-generation receipt expiry.
    rebuilt = RemoveRig(rig.root, "graceful")
    try:
        rebuilt.client.restore(rebuilt.app.desired_state.load().harnesses)
        assert not rebuilt.app.desired_state.load().harnesses
        assert not rebuilt.launcher.processes
    finally:
        rebuilt.close()


def test_failed_remove_returns_bounded_truthful_error(failed_remove: RemoveRig) -> None:
    """Assertion 3: error branches stay failures, never graceful success."""
    rig = failed_remove
    code, elapsed = rig.down_result
    expected = "HARNESS_IO_FAILED" if rig.launcher.mode == "E" else "LIFECYCLE_OPERATION_TIMEOUT"
    assert code == expected
    # Bound the observed failure against the budget the operation ran under
    # (``down()`` restores the manager's normal deadline on the way out).
    assert elapsed <= rig.operation_deadline + 10
    assert rig.snapshot()["operations"][0]["error_code"] == expected
    assert not rig.launcher.release.is_set()


def remove_request(rig: RemoveRig, operation: str = "direct-remove") -> LifecycleMutationRequest:
    correlation = f"lifecycle:{operation}:forward:0"
    return LifecycleMutationRequest(
        correlation, f"{operation}:forward:0", operation, None,
        RemoveHarnessCommand(correlation, rig.spec.harness, rig.spec.name),
    )


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition did not settle"
        time.sleep(0.005)


def test_remove_rig_installs_failure_budget_only_after_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This is the ordering guard: the real setup create must run under the
    # manager's normal slow-step budget, never under the narrow failure budget.
    # It is now stronger than when the budget was installed by ``start()``:
    # that lifecycle step does not install the budget at all.
    rig = RemoveRig(tmp_path, "H")
    engine = rig.manager._effects_engine
    setup_budget = (
        engine._operation_deadline,
        engine._completion_timeout,
    )
    observed: list[tuple[float, float]] = []
    run = rig.app._run_lifecycle_operation

    def observe_setup_budget(*args, **kwargs):
        observed.append(
            (
                engine._operation_deadline,
                engine._completion_timeout,
            )
        )
        return run(*args, **kwargs)

    wait = rig.manager.wait
    observed_waits = []

    def observe_terminal_wait(operation_id, timeout):
        observed_waits.append(timeout)
        return wait(operation_id, timeout)

    monkeypatch.setattr(rig.manager, "wait", observe_terminal_wait)
    monkeypatch.setattr(rig.app, "_run_lifecycle_operation", observe_setup_budget)
    try:
        assert setup_budget[0] >= 1
        assert setup_budget[1] >= 1
        rig.start()
        assert observed == [setup_budget]
        assert observed_waits == [setup_budget[0] + LIFECYCLE_WAIT_MARGIN_SECONDS]
        assert (
            engine._operation_deadline,
            engine._completion_timeout,
        ) == setup_budget
    finally:
        rig.close()


def test_failure_budget_covers_only_the_operation_under_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The narrow failure budget must not leak past ``down()``.

    Regression guard for the dev/PR #485 flake.  Three tests in this file run a
    *real* create after ``down()``; while ``start()`` installed the 0.3s budget
    permanently, that create ran under the failure deadline and the manager
    killed it inside CI's 2-CPU job (a healthy create there has p50 0.30s).
    """
    rig = RemoveRig(tmp_path, "H")
    engine = rig.manager._effects_engine
    normal = (
        engine._operation_deadline,
        engine._completion_timeout,
    )
    observed: list[tuple[float, float]] = []
    run = rig.app._run_lifecycle_operation

    wait = rig.manager.wait
    observed_waits = []

    def observe_terminal_wait(operation_id, timeout):
        observed_waits.append(timeout)
        return wait(operation_id, timeout)

    def observe_real_operation(*args, **kwargs):
        observed.append(
            (
                engine._operation_deadline,
                engine._completion_timeout,
            )
        )
        return run(*args, **kwargs)

    try:
        assert normal[0] >= 1
        assert normal[1] >= 1
        rig.start()
        assert (
            engine._operation_deadline,
            engine._completion_timeout,
        ) == normal
        with rig.failure_budget():
            assert (
                engine._operation_deadline,
                engine._completion_timeout,
            ) == (rig.operation_deadline, 0.02)
        assert (
            engine._operation_deadline,
            engine._completion_timeout,
        ) == normal
        code, _ = rig.down()
        assert code == "LIFECYCLE_OPERATION_TIMEOUT"
        assert (
            engine._operation_deadline,
            engine._completion_timeout,
        ) == normal
        monkeypatch.setattr(rig.manager, "wait", observe_terminal_wait)
        monkeypatch.setattr(rig.app, "_run_lifecycle_operation", observe_real_operation)
        rig.app._run_lifecycle_operation(LifecycleOperation.create(
            "after-failure", rig.app._lifecycle_spec(rig.spec)
        ))
        assert observed == [normal]
        assert observed_waits == [normal[0] + LIFECYCLE_WAIT_MARGIN_SECONDS]
    finally:
        rig.close()


@pytest.fixture
def hanging_remove(tmp_path):
    rig = RemoveRig(tmp_path, "H")
    try:
        rig.start()
        request = remove_request(rig)
        assert rig.actor.submit(request) is PortAdmission.ACCEPTED
        assert rig.launcher.processes[0].entered.wait(2)
        yield rig, request
    finally:
        rig.close()


def fail_now(rig, request):
    return rig.client.fail_lifecycle(request, "LIFECYCLE_OPERATION_TIMEOUT", "test deadline", 1)


def protected_process_token(rig: RemoveRig) -> str:
    process = rig.actor._records[rig.key].process
    token = getattr(process, "process_token", None)
    assert isinstance(token, str) and token
    assert process.running
    return token


def test_late_io_cannot_complete_new_same_name_record(hanging_remove):
    rig, request = hanging_remove
    old = rig.launcher.processes[0]
    old_token = protected_process_token(rig)
    result = fail_now(rig, request)
    assert isinstance(result, LifecycleMutationFailed)
    rig.app._run_lifecycle_operation(LifecycleOperation.create(
        "new-incarnation", rig.app._lifecycle_spec(rig.spec)
    ))
    new = rig.launcher.processes[1]
    replacement_token = protected_process_token(rig)
    assert replacement_token != old_token
    old.release.set()
    wait_until(lambda: old.child.poll() is not None)
    wait_until(lambda: rig.actor._io.in_flight == 0)
    rig.client.reconcile()
    assert protected_process_token(rig) == replacement_token
    assert new.child.poll() is None
    assert len(rig.launcher.processes) == 2
    assert rig.app.desired_state.load().harnesses == (rig.spec,)


def test_duplicate_terminal_keeps_original_error_and_new_token(hanging_remove):
    rig, request = hanging_remove
    first = fail_now(rig, request)
    rig.app._run_lifecycle_operation(LifecycleOperation.create(
        "replacement", rig.app._lifecycle_spec(rig.spec)
    ))
    replacement_token = protected_process_token(rig)
    state = rig.app.desired_state.load()
    second = rig.client.fail_lifecycle(request, "DIFFERENT_ERROR", "duplicate", 1)
    assert second == first
    assert rig.app.desired_state.load() == state
    assert protected_process_token(rig) == replacement_token
    assert rig.launcher.processes[1].child.poll() is None


def test_success_wins_before_deadline_control(tmp_path):
    rig = RemoveRig(tmp_path, "graceful")
    try:
        rig.start()
        request = remove_request(rig)
        assert rig.actor.submit(request) is PortAdmission.ACCEPTED
        success = rig.events.wait(request.correlation_id, LifecycleMutationCompleted, timeout=2)
        # A successful stop has a truthful completed receipt, not a failed one.
        result = fail_now(rig, request)
        assert isinstance(result, LifecycleMutationCompleted)
        assert result.provenance == success.provenance
        assert not rig.app.desired_state.load().harnesses
        receipt = rig.app.desired_state.harness_lifecycle_receipt(request.attempt_token)
        assert receipt is not None and receipt.completed
        rig.app._run_lifecycle_operation(LifecycleOperation.create(
            "new-after-success", rig.app._lifecycle_spec(rig.spec)
        ))
        replacement_token = protected_process_token(rig)
        assert isinstance(fail_now(rig, request), LifecycleMutationCompleted)
        assert protected_process_token(rig) == replacement_token
        assert rig.launcher.processes[1].child.poll() is None
    finally:
        rig.close()


def test_mailbox_overload_retries_control_without_waiting_for_stop(hanging_remove, monkeypatch):
    from hyprial.daemon.impl.lifecycle.lifecycle_receipts import FailHarnessLifecycleCommand

    rig, request = hanging_remove
    admit = rig.actor._admit
    attempts = []

    def overloaded(command):
        if isinstance(command, FailHarnessLifecycleCommand):
            attempts.append(command.correlation_id)
            if len(attempts) <= 3:
                return AdmissionResult.OVERLOADED
        return admit(command)

    monkeypatch.setattr(rig.actor, "_admit", overloaded)
    result = fail_now(rig, request)
    assert isinstance(result, LifecycleMutationFailed)
    assert len(set(attempts)) == 4
    assert not rig.launcher.release.is_set()
    assert rig.launcher.processes[0].child.poll() is None
    assert rig.actor.read_harness(rig.key) is None
    assert not rig.app.desired_state.load().harnesses


def test_manager_journals_success_that_wins_deadline_race(tmp_path, monkeypatch):
    # Only the harness-removal boundary is under test. A 0.3s budget over the
    # whole saga can expire at the preceding route step (CI task 22352), before
    # the success-vs-deadline control race has even been reached.
    rig = RemoveRig(tmp_path, "H", deadline=LIFECYCLE_OPERATION_DEADLINE_SECONDS)
    try:
        rig.start()
        engine = rig.manager._effects_engine
        port = engine._ports.harness
        control = port._failure_control
        perform = engine._perform
        boundaries = []

        def expire_at_remove(operation_id, ordinal, step, direction):
            if step.domain == "harness" and step.forward == "remove" and direction == "forward":
                with engine._started_lock:
                    engine._started[operation_id] = (
                        time.monotonic() - engine._operation_deadline - 1
                    )
            return perform(operation_id, ordinal, step, direction)

        def success_first(request, code, detail, timeout):
            assert code == "LIFECYCLE_OPERATION_TIMEOUT"
            rig.launcher.release.set()
            wait_until(lambda: (receipt := rig.app.desired_state.harness_lifecycle_receipt(
                request.attempt_token)) is not None and receipt.completed, timeout=timeout)
            boundaries.append("completed-receipt")
            result = control(request, code, detail, timeout)
            assert isinstance(result, LifecycleMutationCompleted)
            boundaries.append("deadline-control")
            # The injected expiry must not leak into unrelated tail steps.
            with engine._started_lock:
                engine._started[request.operation_id] = time.monotonic()
            return result

        monkeypatch.setattr(engine, "_perform", expire_at_remove)
        monkeypatch.setattr(port, "_failure_control", success_first)
        code, elapsed = rig.down()
        assert code == "SUCCESS"
        assert boundaries == ["completed-receipt", "deadline-control"]
        # The old line was ``assert elapsed < 2`` -- a wall-clock bet that the
        # whole down() beats two seconds on whatever machine runs it.  This
        # test is named ``wins_deadline_race``: what it must prove is the
        # OUTCOME (success is recorded before the deadline path), not the
        # speed.  So the literal is deleted and replaced with the semantic
        # result, in the two halves an ordering assertion needs:
        #   (1) both records are PRESENT -- an empty journal trivially
        #       satisfies "success before deadline", so absence must go red
        #       before order is even discussed;
        #   (2) their order is success first.
        records = rig.snapshot()["operations"]
        assert records, "no lifecycle operation was journaled at all"
        # Presence must be an exact count, not "at least one": a list that is
        # non-empty by construction makes ">= 1" always true, which carries no
        # information and reads as insurance while never working.  "== 1" can
        # fail on BOTH ends (0 records, or duplicated records), so it is a
        # real criterion.
        success_records = [r for r in records if r["state"] == "completed"]
        deadline_records = [
            r for r in records
            if r.get("error_code") in {"LIFECYCLE_OPERATION_TIMEOUT", "LIFECYCLE_DEADLINE_EXCEEDED", "DEADLINE"}
        ]
        assert len(success_records) == 1, (
            f"expected exactly one completed (success) operation, "
            f"got {len(success_records)}"
        )
        assert len(deadline_records) <= 1, (
            f"expected at most one deadline record, got {len(deadline_records)}"
        )
        if deadline_records:
            assert records.index(success_records[0]) < records.index(
                deadline_records[0]
            ), "the deadline path was journaled before the success that should win it"
        assert not rig.app.desired_state.load().harnesses
        assert not rig.app.desired_state.load().lifecycle_receipts
        assert rig.snapshot()["operations"][0]["state"] == "completed"
    finally:
        rig.close()


def test_permanent_mailbox_overload_is_bounded_and_does_not_fake_settlement(hanging_remove, monkeypatch):
    from hyprial.daemon.impl.lifecycle.lifecycle_receipts import FailHarnessLifecycleCommand

    rig, request = hanging_remove
    admit = rig.actor._admit
    monkeypatch.setattr(rig.actor, "_admit", lambda command: (
        AdmissionResult.OVERLOADED if isinstance(command, FailHarnessLifecycleCommand) else admit(command)
    ))
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="overloaded"):
        rig.client.fail_lifecycle(request, "LIFECYCLE_OPERATION_TIMEOUT", "deadline", 0.05)
    assert time.monotonic() - started < 0.5
    assert rig.actor.read_harness(rig.key) is not None
    assert rig.app.desired_state.harness_lifecycle_receipt(request.attempt_token) is not None


def test_deadline_control_can_overtake_unadmitted_remove(tmp_path):
    rig = RemoveRig(tmp_path, "H")
    try:
        rig.start()
        request = remove_request(rig)
        assert rig.app.desired_state.load().harnesses  # delete has NOT happened
        result = fail_now(rig, request)
        assert isinstance(result, LifecycleMutationFailed)
        assert not rig.app.desired_state.load().harnesses
        assert not rig.app.desired_state.load().lifecycle_receipts
        assert rig.actor.read_harness(rig.key) is None
        # The overtaken, genuine admission cannot start stop or resurrect intent.
        assert rig.actor.submit(request) is PortAdmission.ACCEPTED
        replay = rig.events.wait(request.correlation_id, LifecycleMutationFailed, timeout=1)
        assert replay == result
        rig.client.reconcile()
        assert rig.launcher.processes[0].stop_calls == 0
        assert len(rig.launcher.processes) == 1
    finally:
        rig.close()


def test_failure_settlement_explicitly_deletes_reappeared_same_token_row(hanging_remove):
    from dataclasses import replace

    rig, request = hanging_remove
    store = rig.app.desired_state
    projection = rig.app._state_persistence.projection()
    assert not projection.desired.harnesses
    # Real persisted shape, no invented completion: simulate a row rewritten
    # without a new token while the admitted process effect is blocked. Route
    # the exact replacement through the shared typed compare-and-save writer.
    rig.app._state_persistence.settled_desired.compare_and_save(
        replace(projection.desired, harnesses=(rig.spec,)),
        expected_version=projection.version,
    )
    assert store.load().harnesses
    fail_now(rig, request)
    assert not store.load().harnesses
    assert not store.load().lifecycle_receipts
    assert rig.actor.read_harness(rig.key) is None


def test_unadmitted_stale_control_has_no_delete_provenance(tmp_path):
    from dataclasses import replace

    rig = RemoveRig(tmp_path, "H")
    try:
        rig.start()
        store = rig.app.desired_state
        token = next(resource.resource_token for resource in store.load().lifecycle_resources
                     if resource.domain == "harness")
        replacement = replace(rig.spec, args=("--replacement",))
        rig.app._state_persistence.settled_desired.upsert_harness(replacement)
        before = store.load()
        request = replace(remove_request(rig), expected_resource_token=token)
        assert isinstance(fail_now(rig, request), LifecycleMutationFailed)
        assert store.load().harnesses == (replacement,)
        assert store.load().lifecycle_resources == before.lifecycle_resources
        assert not store.load().lifecycle_receipts
        assert rig.actor.read_harness(rig.key) is not None
        assert rig.launcher.processes[0].stop_calls == 0
    finally:
        rig.close()


def test_close_settles_failed_removal_on_actor_not_calling_thread(hanging_remove, monkeypatch):
    rig, request = hanging_remove
    caller = threading.get_ident()
    seen = []
    original = rig.app.desired_state.fail_harness_removal

    def observed(attempt, token):
        seen.append(threading.get_ident())
        return original(attempt, token)

    monkeypatch.setattr(rig.app.desired_state, "fail_harness_removal", observed)
    assert not rig.actor.close_runtime(0.2)
    assert seen and all(thread != caller for thread in seen)
    assert not rig.app.desired_state.load().harnesses
    assert rig.actor.read_harness(rig.key) is None
    rig.launcher.release.set()
    wait_until(lambda: rig.actor._io.in_flight == 0)
    assert rig.actor.close_runtime(5.0)
    rig.app._harnesses = None
    rig.close()


def test_failed_removal_store_error_does_not_claim_settlement(hanging_remove, monkeypatch):
    rig, request = hanging_remove
    store = rig.app.desired_state
    before = store.load()
    receipt = store.harness_lifecycle_receipt(request.attempt_token)
    assert receipt is not None

    def unavailable(_state):
        raise OSError("injected durable store write failure")

    with monkeypatch.context() as patch:
        patch.setattr(rig.app._state_persistence._desired_store, "save", unavailable)
        with pytest.raises(OSError, match="durable store"):
            rig.app._state_persistence.settled_desired.fail_harness_removal(
                request.attempt_token, receipt.provenance.resource_token
            )
    assert store.load() == before
    assert rig.actor.read_harness(rig.key) is not None
    assert request.attempt_token not in rig.actor._lifecycle_failures


def test_stale_removal_failure_preserves_external_replacement_token(hanging_remove):
    from dataclasses import replace

    rig, request = hanging_remove
    store = rig.app.desired_state
    replacement = replace(rig.spec, args=("--new-owner",))
    rig.app._state_persistence.settled_desired.upsert_harness(replacement)
    resources = store.load().lifecycle_resources
    fail_now(rig, request)
    assert store.load().harnesses == (replacement,)
    assert store.load().lifecycle_resources == resources
    assert not store.load().lifecycle_receipts
