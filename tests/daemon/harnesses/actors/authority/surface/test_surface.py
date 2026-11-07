from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import get_args
from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.kernel.impl.contracts.ports import PortCommandRejected
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.api import HarnessDelivery, HarnessResult, HarnessResultStatus
from hyprial.daemon.impl.harnesses.actor import HarnessProjection, HarnessRuntimeActor, _Record
from hyprial.daemon.impl.harnesses.runtime.ports import DispatchHarnessDeliveryCommand, DrainHarnessProgressCommand, DrainHarnessResultsCommand, EnsureHarnessCommand, HarnessCommand, HarnessDeliveryAdmitted, HarnessLaunchProjection, HarnessProgressObserved, HarnessResultObserved, HarnessSessionRefsProjection, HarnessTimerCompleted, HarnessTimerElapsedCommand, RestoreEligibilityProjection, UpdateRestoreEligibilityCommand, harness_desired_generation
from hyprial.daemon.impl.processes.supervisor import ManagedHarnessRuntime

from ..support import (
    _Launcher,
    _Process,
    _actor_dispatch_source,
    _handled_frozen_commands,
    _spec,
    _wait_for,
)


def test_worker_mcp_session_projection_is_distinct_from_native_session() -> None:
    class _WorkerProcess(_Process):
        session_ref = "native-thread"
        worker_channel = SimpleNamespace(session_ref="worker-mcp-session")

    class _WorkerLauncher:
        def start(self, _spec: HarnessLaunchSpec) -> _WorkerProcess:
            return _WorkerProcess()

    actor = HarnessRuntimeActor(_WorkerLauncher())
    command = EnsureHarnessCommand(
        "worker-session",
        HarnessLaunchProjection("codex", "worker", True),
    )
    assert actor.submit(command) is PortAdmission.ACCEPTED
    _wait_for(
        lambda: (
            actor.read_harness("codex:worker") is not None
            and actor.read_harness("codex:worker").running  # type: ignore[union-attr]
        )
    )
    [native] = actor.read_session_refs().refs
    [worker] = actor.read_worker_session_refs().refs
    assert native.session_ref == "native-thread"
    assert worker.session_ref == "worker-mcp-session"
    assert actor.close_runtime(2.0)


def test_harness_reconcile_uses_restore_projection_without_callback_io() -> None:
    launcher = _Launcher()
    events: list[object] = []
    actor = HarnessRuntimeActor(
        launcher, restart_backoff_seconds=0, event_sink=events.append
    )
    spec = HarnessLaunchProjection("codex", "worker", True)
    assert actor.submit(EnsureHarnessCommand("start", spec)) is PortAdmission.ACCEPTED
    _wait_for(lambda: len(launcher.processes) == 1)
    suppressed = RestoreEligibilityProjection(
        "worker",
        "entity-1",
        harness_desired_generation(spec),
        True,
        1,
        1,
    )
    assert actor.submit(
        UpdateRestoreEligibilityCommand(
            "suppress",
            suppressed,
        )
    ) is PortAdmission.ACCEPTED
    _wait_for(lambda: actor._restore_eligibility.get("worker") == suppressed)
    launcher.processes[0].running = False
    assert actor.submit(
        HarnessTimerElapsedCommand("tick-1", actor.generation, 1, 1)
    ) is PortAdmission.ACCEPTED
    _wait_for(
        lambda: any(
            isinstance(event, HarnessTimerCompleted)
            and event.correlation_id == "tick-1"
            for event in events
        )
    )
    # A timer receipt precedes the asynchronous native-fact observation.
    # Join that observation and its effects before asserting no restart.
    _wait_for(lambda: not actor.read_harness("codex:worker").running)
    _wait_for(
        lambda: actor._io.in_flight == 0
        and (snapshot := actor._runtime.snapshot(actor._handle)).queued
        == snapshot.in_flight == 0
    )
    assert len(launcher.processes) == 1

    assert actor.submit(
        UpdateRestoreEligibilityCommand(
            "allow",
            RestoreEligibilityProjection(
                "worker",
                "entity-1",
                harness_desired_generation(spec),
                False,
                1,
                2,
            ),
        )
    ) is PortAdmission.ACCEPTED
    assert actor.submit(
        HarnessTimerElapsedCommand("tick-2", actor.generation, 2, 2)
    ) is PortAdmission.ACCEPTED
    _wait_for(lambda: len(launcher.processes) == 2)
    assert actor.close_runtime(2.0)


def test_restore_projection_rejects_late_entity_and_desired_generation() -> None:
    actor = HarnessRuntimeActor(_Launcher())
    spec = HarnessLaunchProjection("codex", "worker", True)
    current = RestoreEligibilityProjection(
        "worker", "entity-new", harness_desired_generation(spec), False, 2, 1
    )
    assert actor.submit(
        UpdateRestoreEligibilityCommand("current", current)
    ) is PortAdmission.ACCEPTED
    _wait_for(lambda: actor._restore_eligibility.get("worker") == current)

    assert actor.submit(
        UpdateRestoreEligibilityCommand(
            "late-old",
            RestoreEligibilityProjection(
                "worker",
                "entity-old",
                harness_desired_generation(spec),
                True,
                1,
                999,
            ),
        )
    ) is PortAdmission.ACCEPTED
    _wait_for(
        lambda: (snapshot := actor._runtime.snapshot(actor._handle)).queued
        == snapshot.in_flight == 0
    )
    assert actor._restore_eligibility["worker"] == current

    mismatched = RestoreEligibilityProjection(
        "worker", "entity-new", "different-desired-generation", True, 2, 2
    )
    assert actor.submit(
        UpdateRestoreEligibilityCommand("mismatch", mismatched)
    ) is PortAdmission.ACCEPTED
    _wait_for(lambda: actor._restore_eligibility.get("worker") == mismatched)
    assert mismatched.desired_generation != harness_desired_generation(spec)
    assert actor.close_runtime(2.0)


def test_slow_start_does_not_block_projection_reads() -> None:
    entered = threading.Event()
    release = threading.Event()

    class _BlockingLauncher(_Launcher):
        def start(self, spec: HarnessLaunchSpec) -> _Process:
            entered.set()
            assert release.wait(2.0)
            return super().start(spec)

    launcher = _BlockingLauncher()
    supervisor = ManagedHarnessRuntime(launcher, start_timeout_seconds=2.0)
    outcome: list[bool] = []
    start_thread = threading.Thread(
        target=lambda: outcome.append(supervisor.start(_spec())), daemon=True
    )
    start_thread.start()
    assert entered.wait(1.0)

    started_at = time.monotonic()
    rows = supervisor.status()
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.1
    assert rows[0]["starting"] is True
    assert rows[0]["running"] is False
    release.set()
    start_thread.join(2.0)
    assert outcome == [True]
    supervisor.stop()


def test_projection_reports_retrying_and_failed_states() -> None:
    """The wire shape: ``state`` marks failure accounting; ``quarantined`` maps.

    ``state`` appears exactly on rows inside failure accounting (absent means
    None), and ``quarantined`` -- the deprecated alias kept for existing
    ``ps`` ``connectors[]`` consumers -- appears exactly where
    ``state == "failed"`` and nowhere else.
    """

    projection = HarnessProjection()
    records = {
        "codex:ok": _Record(spec=_spec("ok")),
        "codex:retrying": _Record(spec=_spec("retrying"), failures=1),
        "codex:failed": _Record(spec=_spec("failed"), failures=3),
    }
    projection.publish(records, 1, failure_budget=3)
    rows = {row["id"]: row for row in projection.status()}
    # Absent key == None: failure accounting only appears on rows inside it.
    assert rows["codex:ok"].get("state") is None
    assert rows["codex:retrying"]["state"] == "retrying"
    assert rows["codex:failed"]["state"] == "failed"
    assert "quarantined" not in rows["codex:ok"]
    assert "quarantined" not in rows["codex:retrying"]
    assert rows["codex:failed"]["quarantined"] is True
    assert projection.failed() == frozenset({"codex:failed"})

    # Budget 0 disables termination: the same counter is retrying forever.
    projection.publish(records, 2, failure_budget=0)
    rows = {row["id"]: row for row in projection.status()}
    assert rows["codex:failed"]["state"] == "retrying"
    assert "quarantined" not in rows["codex:failed"]
    assert projection.failed() == frozenset()


def test_mailbox_overload_is_explicit_while_projection_publish_is_blocked() -> None:
    supervisor = ManagedHarnessRuntime(_Launcher(), actor_mailbox_capacity=1)
    projection = supervisor._actor.projection
    projection._lock.acquire()
    try:
        first = HarnessTimerElapsedCommand("first", 1, 1, 1)
        second = HarnessTimerElapsedCommand("second", 1, 2, 1)
        third = HarnessTimerElapsedCommand("third", 1, 3, 1)
        assert supervisor._actor.submit(first) is PortAdmission.ACCEPTED
        _wait_for(
            lambda: supervisor._actor._runtime.snapshot(
                supervisor._actor._handle
            ).in_flight
            == 1
        )
        assert supervisor._actor.submit(second) is PortAdmission.ACCEPTED
        assert (
            supervisor._actor.submit(third)
            is PortAdmission.OVERLOADED
        )
    finally:
        projection._lock.release()
    _wait_for(lambda: supervisor._actor._runtime.snapshot(supervisor._actor._handle).in_flight == 0)
    supervisor.stop()


def test_frozen_harness_command_union_has_exactly_one_actor_handler() -> None:
    counts = _handled_frozen_commands(_actor_dispatch_source())
    assert set(counts) == set(get_args(HarnessCommand))
    assert all(count == 1 for count in counts.values())


def test_frozen_dispatch_progress_result_and_session_ref_projections() -> None:
    class _Streaming(_Process):
        session_ref = "session-1"

        def enqueue(self, delivery: HarnessDelivery) -> bool:
            self.delivery = delivery
            return True

        def drain_results(self) -> tuple[HarnessResult, ...]:
            return (
                HarnessResult(
                    "delivery-1",
                    "agent:o:n:worker",
                    HarnessResultStatus.COMPLETED,
                    output="done",
                ),
            )

        def drain_progress(self) -> tuple[object, ...]:
            return ("working",)

        def interrupt(self, delivery_id: str, *, timeout: float = 1.0) -> bool:
            del delivery_id, timeout
            return True

    class _StreamingLauncher:
        def __init__(self) -> None:
            self.process = _Streaming()

        def start(self, _spec: HarnessLaunchSpec) -> _Streaming:
            return self.process

    events: list[object] = []
    launcher = _StreamingLauncher()
    actor = HarnessRuntimeActor(launcher, event_sink=events.append)
    assert actor.submit(
        EnsureHarnessCommand(
            "ensure", HarnessLaunchProjection("codex", "worker", True)
        )
    ) is PortAdmission.ACCEPTED
    _wait_for(lambda: any(getattr(event, "correlation_id", "") == "ensure" for event in events))

    delivery = HarnessDelivery(
        "delivery-1", "conversation-1", "sender", "agent:o:n:worker", "hello"
    )
    assert actor.submit(
        DispatchHarnessDeliveryCommand("dispatch", "worker", delivery)
    ) is PortAdmission.ACCEPTED
    assert actor.submit(DrainHarnessProgressCommand("progress")) is PortAdmission.ACCEPTED
    assert actor.submit(DrainHarnessResultsCommand("results")) is PortAdmission.ACCEPTED
    _wait_for(
        lambda: {
            getattr(event, "correlation_id", "") for event in events
        } >= {"dispatch", "progress", "results"}
    )
    assert any(
        isinstance(event, HarnessDeliveryAdmitted) and event.accepted
        for event in events
    )
    assert any(
        isinstance(event, HarnessProgressObserved)
        and event.progress == ("working",)
        for event in events
    )
    assert any(
        isinstance(event, HarnessResultObserved)
        and event.results[0].output == "done"
        for event in events
    )
    refs = actor.read_session_refs()
    assert isinstance(refs, HarnessSessionRefsProjection)
    assert refs.refs[0].session_ref == "session-1"
    assert actor.read_streaming().actors == ("worker",)
    snapshot_version = refs.version
    # The authority now holds an opaque I/O owner; change the fake native driver.
    launcher.process.session_ref = "session-2"
    unchanged = actor.read_session_refs()
    assert unchanged.version == snapshot_version
    assert unchanged.refs[0].session_ref == "session-1"
    assert actor.submit(DrainHarnessProgressCommand("refresh-ref")) is PortAdmission.ACCEPTED
    _wait_for(
        lambda: any(
            getattr(event, "correlation_id", "") == "refresh-ref"
            for event in events
        )
    )
    refreshed = actor.read_session_refs()
    assert refreshed.version > snapshot_version
    assert refreshed.refs[0].session_ref == "session-2"
    actor.close_runtime(1.0)


def test_stale_frozen_timer_is_rejected_with_typed_event() -> None:
    events: list[object] = []
    actor = HarnessRuntimeActor(_Launcher(), event_sink=events.append)
    assert actor.submit(
        HarnessTimerElapsedCommand("stale", actor.generation + 1, 1, 1)
    ) is PortAdmission.ACCEPTED
    _wait_for(lambda: bool(events))
    rejection = events[-1]
    assert isinstance(rejection, PortCommandRejected)
    assert rejection.code == "STALE_HARNESS_TIMER"
    assert rejection.correlation_id == "stale"
    actor.close_runtime(1.0)
