from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
import pytest
from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.daemon.impl.correlation.correlation import CorrelationEventRouter
from hyprial.daemon.impl.lifecycle.lifecycle_receipts import MutationProvenance
from hyprial.kernel import DomainEffectClaim
from hyprial.daemon.impl.lifecycle_manager import _RECOVER_FAULT_EVENT_MIN_INTERVAL_S, LifecycleOperation, LifecyclePorts, LifecycleState
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.state_db import StateDatabase
from hyprial.daemon.impl.state_persistence import StatePersistenceAuthority
from hyprial.daemon.impl.lifecycle.lifecycle_coordinator import LifecycleCoordinator

from ..support import (
    TERMINAL_WAIT_SECONDS,
    _CompletionPorts,
    _CrashBeforeFirstCompletion,
    _close_test_managers as _close_test_managers,
    _manager,
    _settles,
    _spec,
)


@pytest.mark.parametrize("owner_kind", ("coordinator", "manager"))
def test_settled_receipts_precede_next_admission_and_rebuild(tmp_path, owner_kind):
    """Opposite controls: receipts settle before timeout, queueing, and restart."""
    path = tmp_path / "settled-order.sqlite3"
    router = CorrelationEventRouter()
    ports = _CompletionPorts(router)  # publish each receipt within submit()
    if owner_kind == "coordinator":
        owner = LifecycleCoordinator(
            path, LifecyclePorts(ports, ports, ports, ports), router,
            completion_timeout=0.03, operation_deadline=0.15,
        )
    else:
        owner = _manager(
            path, ports, router, capacity=1,
            completion_timeout=0.05, operation_deadline=0.3,
        )
    try:
        for operation_id in ("settled-first", "settled-second"):
            # The second producer arrives only after the first terminal receipt.
            assert owner.submit(
                LifecycleOperation.create(operation_id, _spec(operation_id))
            ) is PortAdmission.ACCEPTED
            result = owner.wait(operation_id, TERMINAL_WAIT_SECONDS)
            assert result.state is LifecycleState.COMPLETED
            assert result.compensated_effects == ()
            assert result.error_code is None
        assert ports.receipts == {}
        assert ports.retired_receipts == {}
        assert owner.drain(3.0)
    finally:
        owner.drain(3.0)

    # Fully retired terminal receipts precede restart: no backfill, replay, or
    # compensation may be needed, unlike the injected mid-effect crash cases.
    rebuilt_router = CorrelationEventRouter()
    rebuilt_ports = _CompletionPorts(rebuilt_router)
    rebuilt = _manager(path, rebuilt_ports, rebuilt_router)
    for operation_id in ("settled-first", "settled-second"):
        assert rebuilt.wait(operation_id, TERMINAL_WAIT_SECONDS).state is LifecycleState.COMPLETED
    assert rebuilt.drain(3.0)
    assert rebuilt_ports.calls == []


def test_process_manager_restart_compensates_durably_completed_effects(
    tmp_path: Path,
) -> None:
    """U0c rewrite of ``..._skips_durably_completed_effects``.

    Why the old assertion was wrong: it asserted the second generation
    RESUMES the interrupted saga to COMPLETED, skipping the durably
    completed effects -- pinning the pre-U0c contract that journal effect
    status is a cross-generation forward cursor.  Allen's U0c ruling
    (spec-pr332-fixes 2026-09-03, lines 7-9) replaces it: a saga that did
    not finish in its generation is compensated on restart and
    desired-state is the only re-run authority, so the journal crosses
    generations as compensation input only.
    """

    path = tmp_path / "saga.sqlite3"
    receipts: dict[str, MutationProvenance] = {}
    current_tokens: dict[str, str] = {}
    router1 = CorrelationEventRouter()
    ports1 = _CompletionPorts(router1, receipts=receipts, current_tokens=current_tokens)
    bind_committed = threading.Event()

    def crash_after_bind(_operation_id: str, effect: str) -> None:
        if effect == "agent.bind":
            bind_committed.set()
            raise RuntimeError("simulated process-manager crash")

    first = _manager(path, ports1, router1, fault_after_effect=crash_after_bind)
    first.submit(LifecycleOperation.create("restart-1", _spec()))
    assert bind_committed.wait(10), "first generation never committed agent.bind"
    # The next generation must start after the injected crash, not merely
    # after an arbitrary 200 ms slice of the first generation's work.
    first._thread.join(5)
    assert not first._thread.is_alive(), "first generation did not exit after crash"
    with pytest.raises(TimeoutError):
        first.wait("restart-1", 0.0)
    assert [type(item).__name__ for item in ports1.calls] == [
        "CreateAgentCommand",
        "EnsureRouteCommand",
        "BindAgentCommand",
    ]

    router2 = CorrelationEventRouter()
    ports2 = _CompletionPorts(router2, receipts=receipts, current_tokens=current_tokens)
    second = _manager(path, ports2, router2)
    result = second.wait("restart-1", TERMINAL_WAIT_SECONDS)
    assert result.state is LifecycleState.COMPENSATED
    assert result.completed_effects == (
        "agent.create",
        "route.persona.ensure",
        "agent.bind",
    )
    # The reverse plan ran in the new generation, in reverse order.
    assert [type(item).__name__ for item in ports2.calls] == [
        "ReleaseAgentCommand",
        "DropRouteCommand",
        "DestroyAgentCommand",
    ]
    with sqlite3.connect(path) as db:
        retired = db.execute(
            "SELECT receipt_retired FROM lifecycle_effects "
            "WHERE operation_id = 'restart-1' AND effect_name = 'agent.bind' "
            "AND direction = 'forward'"
        ).fetchone()
    # The crash window (completed, never retired) is left exactly as the
    # crash left it: the restart compensates instead of finishing the
    # retirement handshake, and the daemon's receipt sweep -- not a
    # resumed saga -- is what clears the stale domain receipt.
    assert retired == (0,)
    assert "restart-1:forward:2" in receipts
    assert second.drain(1)


def test_crash_after_domain_retire_recovers_durable_retirement_phase(
    tmp_path: Path,
) -> None:
    """U0c rewrite: the retire-crash window no longer resumes forward.

    Why the old assertion was wrong: it asserted the second manager
    COMPLETES the interrupted saga (resuming past the durable-retirement
    journal phase without re-running the domain mutation) -- again the
    pre-U0c cross-generation resume contract.  U0c: the restart
    compensates; the stale retired-unconfirmed domain receipt must not
    block the reverse plan, and within one generation the retirement
    phases are unchanged (the same-generation paths in ``_execute`` still
    skip durably completed effects and finish their retirements).
    """

    path = tmp_path / "saga.sqlite3"
    receipts: dict[str, MutationProvenance] = {}
    current_tokens: dict[str, str] = {}
    retired_receipts: dict[str, str] = {}
    router1 = CorrelationEventRouter()
    first_ports = _CompletionPorts(
        router1,
        receipts=receipts,
        current_tokens=current_tokens,
        retired_receipts=retired_receipts,
    )

    def crash_after_retire(_operation_id: str, effect: str) -> None:
        if effect == "agent.create":
            raise RuntimeError("crash after domain retire before journal phase")

    first = _manager(
        path,
        first_ports,
        router1,
        fault_after_receipt_retire=crash_after_retire,
    )
    first.submit(LifecycleOperation.create("retire-crash", _spec()))
    assert _settles(lambda: "retire-crash:forward:0" in retired_receipts)
    with pytest.raises(TimeoutError):
        first.wait("retire-crash", 0.0)
    assert retired_receipts == {
        "retire-crash:forward:0": "resource:retire-crash:forward:0"
    }

    # The domain receipt is written before the injected crash and final journal read.
    assert _settles(lambda: first.crashed and not first.is_running)

    router2 = CorrelationEventRouter()
    second_ports = _CompletionPorts(
        router2,
        receipts=receipts,
        current_tokens=current_tokens,
        retired_receipts=retired_receipts,
    )
    second = _manager(path, second_ports, router2)
    result = second.wait("retire-crash", TERMINAL_WAIT_SECONDS)
    assert result.state is LifecycleState.COMPENSATED
    assert result.completed_effects == ("agent.create",)
    # The reverse plan destroyed the half-built agent in the new
    # generation despite the stale retired-unconfirmed receipt.
    assert [type(command).__name__ for command in second_ports.calls] == [
        "DestroyAgentCommand"
    ]
    # The stale ledger entry survives as dead-generation leftovers (the
    # daemon's receipt sweep deletes it); it must not be resurrected.
    assert retired_receipts == {
        "retire-crash:forward:0": "resource:retire-crash:forward:0"
    }
    with sqlite3.connect(path) as db:
        phase = db.execute(
            "SELECT receipt_retired FROM lifecycle_effects "
            "WHERE operation_id = 'retire-crash' "
            "AND effect_name = 'agent.create' AND direction = 'forward'"
        ).fetchone()
    # The journal retirement phase stays exactly as the crash left it too:
    # a compensating restart does not finish the dead generation's
    # retirement handshake (the receipt sweep deletes the stale domain
    # side); within a generation the phases are unchanged.
    assert phase == (0,)
    assert second.drain(1)


def test_public_domain_error_code_survives_compensation_and_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "saga.sqlite3"
    router = CorrelationEventRouter()
    ports = _CompletionPorts(
        router, fail_at=2, failure_code="AGENT_ALREADY_RUNNING"
    )
    manager = _manager(path, ports, router)
    manager.submit(LifecycleOperation.create("public-code", _spec()))
    result = manager.wait("public-code", TERMINAL_WAIT_SECONDS)
    assert result.state is LifecycleState.COMPENSATED
    assert result.error_code == "AGENT_ALREADY_RUNNING"
    assert "AGENT_ALREADY_RUNNING" in (result.error or "")
    assert manager.drain(1)

    rebuilt_router = CorrelationEventRouter()
    rebuilt = _manager(path, _CompletionPorts(rebuilt_router), rebuilt_router)
    persisted = rebuilt.result("public-code")
    assert persisted.error_code == "AGENT_ALREADY_RUNNING"
    assert persisted.error == result.error
    assert rebuilt.drain(1)


def test_compensating_restart_retains_original_public_error_code(
    tmp_path: Path,
) -> None:
    path = tmp_path / "saga.sqlite3"
    receipts: dict[str, MutationProvenance] = {}
    current_tokens: dict[str, str] = {}
    retired_receipts: dict[str, str] = {}
    router1 = CorrelationEventRouter()
    ports1 = _CompletionPorts(
        router1,
        fail_at=2,
        failure_code="SERVICE_BINDING_NOT_FOUND",
        receipts=receipts,
        current_tokens=current_tokens,
        retired_receipts=retired_receipts,
    )
    completions = 0

    def crash_during_compensation(_operation: str, _step: str) -> None:
        nonlocal completions
        completions += 1
        if completions == 3:
            raise RuntimeError("crash during compensation")

    first = _manager(
        path,
        ports1,
        router1,
        fault_after_effect=crash_during_compensation,
    )
    first.submit(LifecycleOperation.create("comp-restart-code", _spec()))
    assert _settles(
        lambda: first.result("comp-restart-code").error_code
        == "SERVICE_BINDING_NOT_FOUND"
    )
    with pytest.raises(TimeoutError):
        first.wait("comp-restart-code", 0.0)
    assert first.result("comp-restart-code").error_code == "SERVICE_BINDING_NOT_FOUND"

    # Persisting the error is earlier than the compensation fault/consumer exit.
    assert _settles(lambda: first.crashed and not first.is_running)

    router2 = CorrelationEventRouter()
    ports2 = _CompletionPorts(
        router2,
        receipts=receipts,
        current_tokens=current_tokens,
        retired_receipts=retired_receipts,
    )
    second = _manager(path, ports2, router2)
    result = second.wait("comp-restart-code", TERMINAL_WAIT_SECONDS)
    assert result.state is LifecycleState.COMPENSATED
    assert result.error_code == "SERVICE_BINDING_NOT_FOUND"
    assert second.drain(1)


def test_restart_backfills_domain_attested_effect_and_compensates_it(
    tmp_path: Path,
) -> None:
    """U0c rewrite of ``..._preserves_pre_dispatch_ownership_when_replay_is_noop``.

    Why the old assertion was wrong: it asserted the fresh manager REPLAYS
    the pre-dispatch create as changed=False (preserving ownership) and
    then RESUMES forward to the failing step -- receipt-replay resume
    across generations.  U0c removes both halves: the domain's durable
    receipt is turned into a journal backfill (``DomainEffectClaim``) and
    the restart compensates the attested effect, so the half-built agent
    is destroyed instead of adopted by a resumed saga.
    """

    path = tmp_path / "saga.sqlite3"
    domain_receipts: dict[str, MutationProvenance] = {}
    router1 = CorrelationEventRouter()
    crashing = _CrashBeforeFirstCompletion(router1, receipts=domain_receipts)
    first = _manager(path, crashing, router1)
    first.submit(LifecycleOperation.create("crash-window", _spec()))
    # Wait for the crash itself (the domain receipt it leaves), not 0.1s of
    # wall clock: under slow state-db I/O the manager had not reached the
    # create yet and the claims below came out empty.
    assert _settles(
        lambda: "crash-window:forward:0" in domain_receipts,
        timeout=TERMINAL_WAIT_SECONDS,
    )
    with pytest.raises(TimeoutError):
        first.wait("crash-window", 0.1)

    # Backfill/recovery must not overlap the old consumer's final journal access.
    assert _settles(lambda: first.crashed and not first.is_running)

    # The daemon's startup sweep: every surviving domain receipt becomes a
    # claim; the journal records what the dead generation's domain
    # attested (it committed the mutation together with the receipt).
    claims = [
        DomainEffectClaim(
            "crash-window",
            attempt,
            provenance.changed,
            provenance.created_by_operation,
            provenance.resource_token,
        )
        for attempt, provenance in domain_receipts.items()
    ]
    state_db = StateDatabase(path)
    state = StatePersistenceAuthority(
        DesiredStateStore(tmp_path / "desired.json", state_db)
    )
    try:
        assert state.journal.backfill_domain_attested_effects(tuple(claims)) == (
            "crash-window:forward:0",
        )
    finally:
        assert state.close()

    router2 = CorrelationEventRouter()
    recovery = _CompletionPorts(
        router2,
        receipts=domain_receipts,
    )
    second = _manager(path, recovery, router2)
    result = second.wait("crash-window", TERMINAL_WAIT_SECONDS)
    assert result.state is LifecycleState.COMPENSATED
    assert result.completed_effects == ("agent.create",)
    assert result.compensated_effects == ("agent.create",)
    assert type(recovery.calls[-1]).__name__ == "DestroyAgentCommand"
    with sqlite3.connect(path) as db:
        receipt = db.execute(
            "SELECT created_by_operation, resource_token FROM lifecycle_effects "
            "WHERE operation_id = 'crash-window' AND effect_name = 'agent.create' "
            "AND direction = 'forward'"
        ).fetchone()
    assert receipt == (1, "resource:crash-window:forward:0")
    assert second.drain(1)


def test_unresolvable_forward_step_retains_custody_until_late_completion(
    tmp_path: Path,
) -> None:
    """Card 104164aa (c): a forward step the domain admits but never completes
    must not release its resource hold or start the next forward step.
    """

    router = CorrelationEventRouter()
    # block_at=0 admits the first forward step but never publishes its
    # completion (release is never set), so every re-drive times out unresolved.
    ports = _CompletionPorts(router, block_effect_token=":forward:0")
    # Rig values, not production values (those are 70 s / 2 s in
    # contracts/lifecycle_budgets.py and LifecycleProcessManager's default).
    # The deadline is the quantity under test.  The per-step wait only has to
    # leave room for re-drives inside it: a re-drive can only happen once a
    # whole wait has elapsed before the deadline.  At 0.2 s against 0.3 s
    # that room was ~60 ms on an idle host, so a CPU-throttled CI job could
    # spend attempt 1 entirely and the manager -- correctly -- failed at the
    # deadline with one attempt.  0.05 s leaves room for several re-drives
    # even when ~150 ms of attempt 1 is lost to scheduling.
    manager = _manager(
        tmp_path / "saga.sqlite3",
        ports,
        router,
        completion_timeout=0.05,
        operation_deadline=0.3,
    )
    assert (
        manager.submit(LifecycleOperation.create("down-1", _spec()))
        is PortAdmission.ACCEPTED
    )

    try:
        assert _settles(lambda: ports.attempts > 1)
        assert _settles(lambda: "down-1" in manager._deadline_exceeded)
        with pytest.raises(TimeoutError):
            manager.wait("down-1", 0.02)
        assert manager.result("down-1").state is LifecycleState.RUNNING
        ports.release.set()
        result = manager.wait("down-1", TERMINAL_WAIT_SECONDS)
        assert result.state is LifecycleState.COMPENSATED
        assert result.error_code == "LIFECYCLE_OPERATION_TIMEOUT"
        assert result.completed_effects == ("agent.create",)
        assert result.compensated_effects == ("agent.create",)
        assert manager.drain(1)
    finally:
        # Release every parked _publish_after_release thread so the test leaves
        # no blocked thread behind: a leaked thread is the C-family teardown
        # shape even when the fd guard cannot see it (card 2389ace2).
        ports.release.set()


def test_unresolvable_harness_start_compensates_completed_binding(
    tmp_path: Path,
) -> None:
    """A stuck start must not leave the already-committed binding live.

    ``harness.ensure`` is the fourth create step.  The fake domain accepts it
    but never publishes a receipt, reproducing a lost terminal start failure
    without launching a real process.  Once the operation deadline expires,
    the saga must reverse the three completed effects so the same actor can be
    started again instead of being refused as already running.
    """

    router = CorrelationEventRouter()
    ports = _CompletionPorts(
        router,
        block_effect_token=":forward:3",
        harness_failure_settlement="failed",
        refuse_live_bind=True,
    )
    manager = _manager(
        tmp_path / "saga.sqlite3",
        ports,
        router,
        completion_timeout=0.05,
        operation_deadline=0.3,
    )
    assert (
        manager.submit(LifecycleOperation.create("failed-start", _spec()))
        is PortAdmission.ACCEPTED
    )

    try:
        result = manager.wait("failed-start", TERMINAL_WAIT_SECONDS)
        assert result.state is LifecycleState.COMPENSATED
        assert result.error_code == "LIFECYCLE_OPERATION_TIMEOUT"
        assert result.completed_effects == (
            "agent.create",
            "route.persona.ensure",
            "agent.bind",
        )
        assert result.compensated_effects == result.completed_effects
        assert [type(command).__name__ for command in ports.calls[-3:]] == [
            "ReleaseAgentCommand",
            "DropRouteCommand",
            "DestroyAgentCommand",
        ]
        assert len(ports.failure_controls) == 1

        # The failed start's binding was actually released: the same actor can
        # immediately run a fresh create saga without the fake domain's
        # AGENT_ALREADY_RUNNING guard rejecting its bind.
        ports.block_effect_token = None
        assert (
            manager.submit(LifecycleOperation.create("retry-start", _spec()))
            is PortAdmission.ACCEPTED
        )
        retry = manager.wait("retry-start", TERMINAL_WAIT_SECONDS)
        assert retry.state is LifecycleState.COMPLETED
        assert retry.error_code is None
    finally:
        ports.release.set()


def test_late_harness_start_completion_is_journalled_then_compensated(
    tmp_path: Path,
) -> None:
    router = CorrelationEventRouter()
    ports = _CompletionPorts(
        router,
        block_effect_token=":forward:3",
        harness_failure_settlement="completed",
    )
    manager = _manager(
        tmp_path / "saga.sqlite3",
        ports,
        router,
        completion_timeout=0.05,
        operation_deadline=0.3,
    )
    assert (
        manager.submit(LifecycleOperation.create("late-ready", _spec()))
        is PortAdmission.ACCEPTED
    )

    try:
        result = manager.wait("late-ready", TERMINAL_WAIT_SECONDS)
        assert result.state is LifecycleState.COMPENSATED
        assert result.error_code == "LIFECYCLE_OPERATION_TIMEOUT"
        assert result.compensated_effects == result.completed_effects
        assert "agent.bind" in result.completed_effects
        assert "harness.ensure" in result.completed_effects
        assert len(ports.failure_controls) == 1
    finally:
        ports.release.set()


def test_missing_harness_failure_ack_keeps_custody_without_compensation(
    tmp_path: Path,
) -> None:
    router = CorrelationEventRouter()
    ports = _CompletionPorts(
        router,
        block_effect_token=":forward:3",
        harness_failure_settlement="timeout",
    )
    manager = _manager(
        tmp_path / "saga.sqlite3",
        ports,
        router,
        completion_timeout=0.05,
        operation_deadline=0.3,
    )
    assert (
        manager.submit(LifecycleOperation.create("missing-ack", _spec()))
        is PortAdmission.ACCEPTED
    )

    try:
        assert _settles(lambda: bool(ports.failure_controls))
        unsettled = manager.result("missing-ack")
        assert unsettled.state is LifecycleState.RUNNING
        assert unsettled.compensated_effects == ()
        assert "agent.bind" in unsettled.completed_effects

        # Allow the managed thread to settle so teardown proves the retained
        # custody can make progress once the actor finally acknowledges.
        ports.harness_failure_settlement = "failed"
        settled = manager.wait("missing-ack", TERMINAL_WAIT_SECONDS)
        assert settled.state is LifecycleState.COMPENSATED
        assert "agent.bind" in settled.compensated_effects
    finally:
        ports.release.set()


def test_recover_store_fault_does_not_kill_the_consumer_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-09-14 production death: recover()'s store read hit
    ``sqlite3.OperationalError: database is locked`` at an unguarded call
    site, the consumer thread exited, and ``submit`` kept returning ACCEPTED
    into a queue nobody drained (every down/start then sat in `running`).

    A retryable store fault must be journaled and retried; work submitted
    around the fault is EXECUTED, never stranded.
    """

    router = CorrelationEventRouter()
    ports = _CompletionPorts(router)
    events: list[tuple[str, dict[str, object]]] = []
    manager = _manager(
        tmp_path / "saga.sqlite3",
        ports,
        router,
        event_sink=lambda event, **fields: events.append((event, fields)),
    )
    original_pending = manager._store.pending
    faults = 2

    def flaky_pending():  # type: ignore[no-untyped-def]
        nonlocal faults
        if faults > 0:
            faults -= 1
            raise sqlite3.OperationalError("database is locked")
        return original_pending()

    monkeypatch.setattr(manager._store, "pending", flaky_pending)
    try:
        assert (
            manager.submit(LifecycleOperation.deactivate("op-1", _spec()))
            is PortAdmission.ACCEPTED
        )
        result = manager.wait("op-1", 5)
        assert result.state is LifecycleState.COMPLETED
        # The thread ate both injected faults and kept consuming.
        assert _settles(lambda: faults == 0)
        assert manager.is_running
        assert not manager.crashed
        assert manager.last_error is not None
        assert "database is locked" in manager.last_error
        thread_errors = [
            fields for event, fields in events if event == "thread_error"
        ]
        # Throttled: the first fault is emitted, the second is counted into
        # the episode (one event per _RECOVER_FAULT_EVENT_EVERY faults).
        assert len(thread_errors) == 1
        assert "database is locked" in str(thread_errors[0]["detail"])
        assert thread_errors[0]["consecutiveFaults"] == 1
        # The recovered line closes the episode with the total fault count.
        assert _settles(
            lambda: any(event == "thread_recovered" for event, _f in events)
        )
        recovered = [fields for event, fields in events if event == "thread_recovered"]
        assert recovered[0]["consecutiveFaults"] == 2
        # A retryable fault is not a thread death.
        assert not [event for event, _fields in events if event == "thread_exited"]
        assert manager.drain(2)
    finally:
        manager.drain(1.0)


def test_fast_recover_faults_carry_a_clock_floor_on_events(
    tmp_path: Path,
) -> None:
    """The count throttle alone mis-prices immediately-returning faults.

    A locked-database fault waits out the busy timeout before returning, so
    every 50th fault is minutes apart; but a fast OSError turns a loop
    every ~0.07 s (queue poll 0.05 s + backoff <= 0.02 s), and the count
    trigger would then emit one error line every ~3.5 s -- roughly 1000 per
    hour.  Count-triggered events closer together than the floor are held
    back; the first fault still emits, the running count and the recovered
    line still carry the exact total.
    """

    router = CorrelationEventRouter()
    ports = _CompletionPorts(router)
    events: list[tuple[str, dict[str, object]]] = []
    manager = _manager(
        tmp_path / "saga.sqlite3",
        ports,
        router,
        admission_backoff=(0.0,),
        event_sink=lambda event, **fields: events.append((event, fields)),
    )
    # The test drives the fault counter directly, so it must be its only
    # writer: in production that is the consumer thread, and a live thread's
    # clean recover pass resets the counter mid-storm (CI unit 10776 saw the
    # second event carry consecutiveFaults=1).  Stop the thread first.
    assert manager.drain(1.0)
    try:
        # Fault 1: always emitted, and it starts the clock.
        manager._note_recover_fault(OSError("immediate store fault"))
        thread_errors = [
            fields for event, fields in events if event == "thread_error"
        ]
        assert len(thread_errors) == 1
        assert thread_errors[0]["consecutiveFaults"] == 1

        # Faults 2..50 land milliseconds later (backoff zeroed): the 50th
        # trips the count trigger but is held back by the clock floor.
        for _ in range(49):
            manager._note_recover_fault(OSError("immediate store fault"))
        thread_errors = [
            fields for event, fields in events if event == "thread_error"
        ]
        assert len(thread_errors) == 1, "count trigger fired inside the clock floor"

        # The same storm past the floor: the next count trigger emits again,
        # with the running total so far (nothing is lost, only deferred).
        manager._last_recover_event_at -= _RECOVER_FAULT_EVENT_MIN_INTERVAL_S
        for _ in range(50):
            manager._note_recover_fault(OSError("immediate store fault"))
        thread_errors = [
            fields for event, fields in events if event == "thread_error"
        ]
        assert len(thread_errors) == 2
        assert thread_errors[1]["consecutiveFaults"] == 100

        # Faulting fast is not a crash; lastError stays fresh on every fault
        # regardless of the event throttle.  (Thread survival under a fault
        # storm is covered by the thread-driven tests above.)
        assert not manager.crashed
        assert manager.last_error is not None
        assert "immediate store fault" in manager.last_error
    finally:
        manager.drain(1.0)


def test_non_retryable_recover_fault_exits_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop guard swallows ONLY the retryable family.  A non-retryable
    fault from the recovery scan takes the same loud exit as an unexpected
    ``_execute`` fault: ``_crashed`` set, thread_exited emitted, and every
    later submission refused instead of accepted into a dead queue.
    """

    router = CorrelationEventRouter()
    ports = _CompletionPorts(router)
    events: list[tuple[str, dict[str, object]]] = []
    manager = _manager(
        tmp_path / "saga.sqlite3",
        ports,
        router,
        event_sink=lambda event, **fields: events.append((event, fields)),
    )

    def corrupt_pending():  # type: ignore[no-untyped-def]
        raise ValueError("corrupt lifecycle row")

    monkeypatch.setattr(manager._store, "pending", corrupt_pending)
    try:
        assert _settles(lambda: manager.crashed)
        assert _settles(lambda: not manager.is_running)
        assert manager.last_error is not None
        assert "corrupt lifecycle row" in manager.last_error
        exited = [fields for event, fields in events if event == "thread_exited"]
        assert exited, "non-retryable recover fault did not exit loudly"
        assert "ValueError" in str(exited[0]["detail"])
        assert (
            manager.submit(LifecycleOperation.deactivate("op-1", _spec()))
            is PortAdmission.CLOSING
        )
    finally:
        # drain() reads pending() on the caller thread; restore the real store
        # first so teardown is not exercising the injected fault.
        monkeypatch.undo()
        manager.drain(1.0)


def test_unexpected_thread_fault_exits_loudly_and_submit_refuses(
    tmp_path: Path,
) -> None:
    """The other half: a REAL thread death is loud -- state, event, refusal --
    instead of the silent shape where submit kept accepting work nobody drove.
    """

    router = CorrelationEventRouter()
    ports = _CompletionPorts(router)
    events: list[tuple[str, dict[str, object]]] = []
    release_event_sink = threading.Event()

    def event_sink(event: str, **fields: object) -> None:
        if event == "thread_exited":
            # _mark_crashed publishes state before observability and teardown.
            # Hold that real window open, independent of scheduler timing.
            release_event_sink.wait(5.0)
        events.append((event, fields))

    def boom(_operation_id: str, _step_name: str) -> None:
        raise RuntimeError("effect applied, process manager crashed before completion")

    manager = _manager(
        tmp_path / "saga.sqlite3",
        ports,
        router,
        fault_after_effect=boom,
        event_sink=event_sink,
    )
    try:
        assert (
            manager.submit(LifecycleOperation.deactivate("op-1", _spec()))
            is PortAdmission.ACCEPTED
        )
        assert _settles(lambda: manager.crashed)
        release_event_sink.set()
        assert _settles(lambda: not manager.is_running)
        assert manager.last_error is not None
        assert "injected manager crash" in manager.last_error
        exited = [fields for event, fields in events if event == "thread_exited"]
        assert exited, "thread death was not emitted as an event"
        assert "injected manager crash" in str(exited[0]["detail"])
        # A stranded RUNNING operation fails its waiter immediately instead of
        # waiting out the full timeout for a settlement that cannot arrive.
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            manager.wait("op-1", 30)
        assert time.monotonic() - started < 5
        # New work is refused at admission, never ACCEPTED into a dead queue.
        assert (
            manager.submit(LifecycleOperation.deactivate("op-2", _spec()))
            is PortAdmission.CLOSING
        )
    finally:
        release_event_sink.set()
        manager.drain(1.0)
