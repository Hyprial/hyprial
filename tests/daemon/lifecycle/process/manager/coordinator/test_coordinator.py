from __future__ import annotations

import threading
import time
from pathlib import Path
import pytest
from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.daemon.impl.correlation.correlation import CorrelationEventRouter
from hyprial.kernel import LifecycleMutationRequest
from hyprial.daemon.impl.lifecycle_manager import LifecycleOperation, LifecycleOperationConflict, LifecyclePorts, LifecycleProcessManager, LifecycleState
from hyprial.daemon.impl.lifecycle.lifecycle_coordinator import LifecycleCoordinator
from hyprial.daemon.impl.route_registration import RouteRegistrationIo

from ..support import (
    EXPECTED_TIMEOUT_SECONDS,
    TERMINAL_WAIT_SECONDS,
    _CompletionGate,
    _CompletionPorts,
    _RouteTransport,
    _close_test_managers as _close_test_managers,
    _manager,
    _settles,
    _spec,
)


def test_coordinator_runs_independent_lifecycle_effects_while_one_waits(
    tmp_path: Path,
) -> None:
    router = CorrelationEventRouter()
    ports = _CompletionPorts(router, block_effect_token="blocked:forward:0")
    coordinator = LifecycleCoordinator(
        tmp_path / "coordinator.sqlite3", LifecyclePorts(ports, ports, ports, ports),
        router, completion_timeout=0.05, effect_workers=2,
    )
    try:
        assert coordinator.submit(LifecycleOperation.create("blocked", _spec("blocked"))) is PortAdmission.ACCEPTED
        assert _settles(lambda: len(ports.calls) >= 1)
        assert coordinator.submit(LifecycleOperation.create("independent", _spec("independent"))) is PortAdmission.ACCEPTED
        assert coordinator.wait("independent", TERMINAL_WAIT_SECONDS).state is LifecycleState.COMPLETED
        assert coordinator.result("blocked").state is LifecycleState.RUNNING
        with pytest.raises(TimeoutError):
            coordinator.wait("blocked", 0.01)
        ports.release.set()
        assert coordinator.wait("blocked", TERMINAL_WAIT_SECONDS).state is LifecycleState.COMPLETED
        assert coordinator.drain(3.0)
    finally:
        ports.release.set()
        coordinator.drain(3.0)


def test_coordinator_serializes_operations_for_same_resource(tmp_path: Path) -> None:
    router = CorrelationEventRouter()
    ports = _CompletionPorts(router, block_effect_token="first:forward:0")
    coordinator = LifecycleCoordinator(
        tmp_path / "serialized.sqlite3", LifecyclePorts(ports, ports, ports, ports),
        router, completion_timeout=0.05, effect_workers=2,
    )
    try:
        assert coordinator.submit(LifecycleOperation.create("first", _spec("same"))) is PortAdmission.ACCEPTED
        assert _settles(lambda: len(ports.calls) >= 1)
        assert coordinator.submit(LifecycleOperation.remove("second", _spec("same"))) is PortAdmission.ACCEPTED
        # Observe the conflicting reservation in the owner before checking silence.
        assert _settles(lambda: coordinator.stats()["pending"] == 1)
        assert all(request.operation_id == "first" for request in ports.requests)
        ports.release.set()
        assert coordinator.wait("first", TERMINAL_WAIT_SECONDS).state is LifecycleState.COMPLETED
        assert coordinator.wait("second", TERMINAL_WAIT_SECONDS).state is LifecycleState.COMPLETED
        # Opposite order: the next same-resource request arrives after settlement.
        assert coordinator.submit(LifecycleOperation.create("third", _spec("same"))) is PortAdmission.ACCEPTED
        assert coordinator.wait("third", TERMINAL_WAIT_SECONDS).state is LifecycleState.COMPLETED
        assert ports.requests[-1].operation_id == "third"
        assert coordinator.drain(3.0)
    finally:
        ports.release.set()
        coordinator.drain(3.0)


def test_coordinator_retains_unresolved_domain_until_late_receipt_then_compensates(
    tmp_path: Path,
) -> None:
    router = CorrelationEventRouter()
    ports = _CompletionPorts(router, block_effect_token="stuck:forward:0")
    coordinator = LifecycleCoordinator(
        tmp_path / "deadline.sqlite3", LifecyclePorts(ports, ports, ports, ports),
        router, completion_timeout=0.03, operation_deadline=0.15,
    )
    try:
        assert coordinator.submit(LifecycleOperation.create("stuck", _spec())) is PortAdmission.ACCEPTED
        assert _settles(lambda: coordinator.stats()["active"] == 1)
        # Elapsed caller time does not prove the effect worker saw the deadline.
        def deadline_observed() -> bool:
            with coordinator._effects_engine._started_lock:
                return "stuck" in coordinator._effects_engine._deadline_exceeded

        assert _settles(deadline_observed)
        assert coordinator.result("stuck").state is LifecycleState.RUNNING
        assert coordinator.stats()["custody"] == 1
        with pytest.raises(TimeoutError):
            coordinator.wait("stuck", 0.02)
        ports.release.set()
        result = coordinator.wait("stuck", 3.0)
        assert result.state is LifecycleState.COMPENSATED
        assert result.error_code == "LIFECYCLE_OPERATION_TIMEOUT"
        assert result.completed_effects == ("agent.create",)
        assert result.compensated_effects == ("agent.create",)
        assert not any(
            type(command).__name__ in {
                "EnsureRouteCommand", "BindAgentCommand", "EnsureHarnessCommand"
            }
            for command in ports.calls
        )
        assert _settles(lambda: coordinator.stats()["custody"] == 0)
        assert coordinator.drain(2.0)
    finally:
        ports.release.set()
        coordinator.drain(2.0)


def test_coordinator_total_custody_bound_includes_conflicting_pending_work(
    tmp_path: Path,
) -> None:
    router = CorrelationEventRouter()
    ports = _CompletionPorts(router, block_effect_token="first:forward:0")
    coordinator = LifecycleCoordinator(
        tmp_path / "bounded.sqlite3",
        LifecyclePorts(ports, ports, ports, ports),
        router,
        capacity=2,
        effect_workers=1,
        completion_timeout=0.03,
        operation_deadline=3.0,
    )
    try:
        assert coordinator.submit(
            LifecycleOperation.create("first", _spec("same"))
        ) is PortAdmission.ACCEPTED
        assert _settles(lambda: coordinator.stats()["active"] == 1)
        assert coordinator.submit(
            LifecycleOperation.remove("pending", _spec("same"))
        ) is PortAdmission.ACCEPTED
        assert _settles(lambda: coordinator.stats()["pending"] == 1)
        assert coordinator.stats()["custody"] == 2
        assert coordinator.submit(
            LifecycleOperation.create("overflow", _spec("other"))
        ) is PortAdmission.OVERLOADED
        assert coordinator.stats()["custody"] == 2
        ports.release.set()
        assert coordinator.wait("first", 3.0).state is LifecycleState.COMPLETED
        assert coordinator.wait("pending", 3.0).state is LifecycleState.COMPLETED
    finally:
        ports.release.set()
        coordinator.drain(3.0)


def test_coordinator_drain_waits_for_durable_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = CorrelationEventRouter()
    ports = _CompletionPorts(router)
    coordinator = LifecycleCoordinator(
        tmp_path / "reservation.sqlite3", LifecyclePorts(ports, ports, ports, ports),
        router,
    )
    reserving = threading.Event()
    release = threading.Event()
    original = coordinator._store.reserve

    def slow_reserve(operation: LifecycleOperation):
        reserving.set()
        assert release.wait(3.0)
        return original(operation)

    monkeypatch.setattr(coordinator._store, "reserve", slow_reserve)
    admitted: list[PortAdmission] = []
    submitter = threading.Thread(target=lambda: admitted.append(
        coordinator.submit(LifecycleOperation.create("reserved", _spec()))
    ))
    submitter.start()
    try:
        assert reserving.wait(1.0)
        assert coordinator.drain(0.02) is False
        release.set()
        submitter.join(3.0)
        assert admitted == [PortAdmission.ACCEPTED]
        assert coordinator.wait("reserved", TERMINAL_WAIT_SECONDS).state is LifecycleState.COMPLETED
        assert coordinator.drain(3.0)
    finally:
        release.set()
        submitter.join(3.0)
        coordinator.drain(3.0)


def test_coordinator_preserves_conflicting_reservation_error(tmp_path: Path) -> None:
    router = CorrelationEventRouter()
    ports = _CompletionPorts(router)
    coordinator = LifecycleCoordinator(
        tmp_path / "conflict.sqlite3", LifecyclePorts(ports, ports, ports, ports),
        router,
    )
    try:
        assert coordinator.submit(LifecycleOperation.create("same-id", _spec("one"))) is PortAdmission.ACCEPTED
        with pytest.raises(LifecycleOperationConflict):
            coordinator.submit(LifecycleOperation.create("same-id", _spec("other")))
        assert coordinator.wait("same-id", TERMINAL_WAIT_SECONDS).state is LifecycleState.COMPLETED
        assert coordinator.drain(3.0)
    finally:
        coordinator.drain(3.0)


def test_coordinator_refuses_new_work_after_nonretryable_recovery_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = CorrelationEventRouter()
    ports = _CompletionPorts(router)
    coordinator = LifecycleCoordinator(
        tmp_path / "recover-fault.sqlite3", LifecyclePorts(ports, ports, ports, ports),
        router,
    )
    def fail_pending() -> tuple[str, ...]:
        raise ValueError("malformed lifecycle journal")

    monkeypatch.setattr(coordinator._store, "pending", fail_pending)
    try:
        assert _settles(lambda: coordinator.crashed, timeout=2.0)
        assert coordinator.submit(LifecycleOperation.create("refused", _spec())) is PortAdmission.CLOSING
        assert coordinator.last_error is not None
        assert "malformed lifecycle journal" in coordinator.last_error
        assert coordinator.drain(2.0)
    finally:
        coordinator.drain(2.0)


def test_bounded_drain_reports_incomplete_while_real_completion_is_blocked(
    tmp_path: Path,
) -> None:
    router = CorrelationEventRouter()
    ports = _CompletionPorts(router, block_at=0)
    manager = _manager(tmp_path / "saga.sqlite3", ports, router)
    manager.submit(LifecycleOperation.create("blocked", _spec()))
    assert not manager.drain(0.02)
    ports.release.set()
    assert manager.wait("blocked", TERMINAL_WAIT_SECONDS).state is LifecycleState.COMPLETED
    assert manager.drain(1)


def test_slow_reservation_does_not_hold_manager_condition_or_escape_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = CorrelationEventRouter()
    manager = _manager(tmp_path / "saga.sqlite3", _CompletionPorts(router), router)
    reserving = threading.Event()
    release = threading.Event()
    original = manager._store.reserve

    def slow_reserve(operation: LifecycleOperation):
        reserving.set()
        assert release.wait(3.0), "test did not release reservation"
        return original(operation)

    monkeypatch.setattr(manager._store, "reserve", slow_reserve)
    admitted: list[PortAdmission] = []
    submitter = threading.Thread(
        target=lambda: admitted.append(
            manager.submit(LifecycleOperation.create("slow-reserve", _spec()))
        )
    )
    submitter.start()
    try:
        assert reserving.wait(1.0)
        started = time.monotonic()
        assert manager.crashed is False
        assert time.monotonic() - started < 0.1
        assert manager.drain(0.02) is False
    finally:
        release.set()
        submitter.join(3.0)
    assert admitted == [PortAdmission.ACCEPTED]
    assert manager.wait("slow-reserve", TERMINAL_WAIT_SECONDS).state is LifecycleState.COMPLETED
    assert manager.drain(1.0)


def test_queue_overload_keeps_durable_operations_for_recovery_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = CorrelationEventRouter()
    ports = _CompletionPorts(router, block_at=0)
    first_admitted = threading.Event()
    submit = ports.submit

    def hold_first_admission(command: object) -> PortAdmission:
        result = submit(command)
        if isinstance(command, LifecycleMutationRequest) and command.operation_id == "queued-0":
            first_admitted.set()
            assert ports.release.wait(3.0), "test did not release first admission"
        return result

    monkeypatch.setattr(ports, "submit", hold_first_admission)
    manager = _manager(tmp_path / "saga.sqlite3", ports, router, capacity=1)
    manager.submit(LifecycleOperation.create("queued-0", _spec("q0")))
    try:
        assert first_admitted.wait(1.0)
        for index in range(1, 4):
            assert (
                manager.submit(
                    LifecycleOperation.create(f"queued-{index}", _spec(f"q{index}"))
                )
                is PortAdmission.ACCEPTED
            )
        # Holding submit, not only its receipt, prevents the consumer from
        # timing out and processing queued work before saturation is checked.
        assert manager._queue.qsize() == 1
    finally:
        ports.release.set()
    for index in range(4):
        assert manager.wait(f"queued-{index}", TERMINAL_WAIT_SECONDS).state is LifecycleState.COMPLETED
    assert manager.drain(1)


def test_route_completion_deadline_keeps_operation_and_drain_unresolved(
    tmp_path: Path,
) -> None:
    router = CorrelationEventRouter()
    domains = _CompletionPorts(router)
    transport = _RouteTransport()
    gate = _CompletionGate(router)
    route = RouteRegistrationIo(
        transport,  # type: ignore[arg-type]
        lambda _route: lambda _key: b"ok",
        gate,
        completion_deadline=0.02,
        completion_backoff=(0.001,),
    )
    manager = LifecycleProcessManager(
        tmp_path / "saga.sqlite3",
        LifecyclePorts(domains, domains, domains, route),
        router,
        completion_timeout=0.03,
        admission_deadline=0.03,
        admission_backoff=(0.001,),
    )
    try:
        manager.submit(LifecycleOperation.create("route-custody", _spec()))
        deadline = time.monotonic() + 1
        while not route.registered() and time.monotonic() < deadline:
            time.sleep(0.005)
        with pytest.raises(TimeoutError):
            manager.wait("route-custody", EXPECTED_TIMEOUT_SECONDS)
        assert manager.result("route-custody").state is LifecycleState.RUNNING
        assert route.registered()
        assert not manager.drain(0.03)

        gate.allow = True
        assert manager.wait("route-custody", TERMINAL_WAIT_SECONDS).state is LifecycleState.COMPLETED
        assert manager.drain(1)
        assert route.drain(1)
    finally:
        # Same direct construction as the route-integration test above.  Open
        # the gate first: if an assertion failed while custody was still held,
        # the operation cannot finish, and a manager that has not finished
        # never closes its store.
        gate.allow = True
        manager.drain(5.0)
        route.drain(5.0)


def test_lifecycle_operation_timeouts_are_ordered_by_derivation() -> None:
    """Card 104164aa (c): the three waits are derived from one deadline, in a
    strict order, never chosen independently.

    The daemon-side wait must outlast the operation deadline so it receives the
    terminal FAILED the manager writes; the CLI's IPC wait must outlast the
    daemon-side wait so that coded failure reaches the caller instead of an
    IPC_TIMEOUT. If any margin were dropped the down would time out at the wrong
    layer and report the wrong thing.
    """

    import inspect

    from hyprial.daemon.impl.application import DaemonApplication
    from hyprial.kernel import (
        LIFECYCLE_IPC_MARGIN_SECONDS,
        LIFECYCLE_OPERATION_DEADLINE_SECONDS,
        LIFECYCLE_WAIT_MARGIN_SECONDS,
    )

    daemon_wait = LIFECYCLE_OPERATION_DEADLINE_SECONDS + LIFECYCLE_WAIT_MARGIN_SECONDS
    cli_ipc = daemon_wait + LIFECYCLE_IPC_MARGIN_SECONDS
    assert LIFECYCLE_OPERATION_DEADLINE_SECONDS < daemon_wait < cli_ipc

    # The daemon-side wait site uses exactly the derived value.
    default = (
        inspect.signature(DaemonApplication._run_lifecycle_operation)
        .parameters["timeout"]
        .default
    )
    assert default == daemon_wait

    # The CLI's `start` IPC wait site uses exactly the same composed value as
    # `down` (cli.py sizes both from these three constants); the behavioural
    # pin is tests/test_harness_command_pinning.py, which observes the actual
    # timeout kwarg the command sends.
    from hyprial.shell.impl.cli.commands.agent.runtime.start import (
        LIFECYCLE_IPC_MARGIN_SECONDS as _CLI_IPC_MARGIN,
        LIFECYCLE_OPERATION_DEADLINE_SECONDS as _CLI_DEADLINE,
        LIFECYCLE_WAIT_MARGIN_SECONDS as _CLI_WAIT_MARGIN,
    )
    assert (_CLI_DEADLINE, _CLI_WAIT_MARGIN, _CLI_IPC_MARGIN) == (
        LIFECYCLE_OPERATION_DEADLINE_SECONDS,
        LIFECYCLE_WAIT_MARGIN_SECONDS,
        LIFECYCLE_IPC_MARGIN_SECONDS,
    )


def test_coordinator_terminal_result_precedes_owner_custody_release(tmp_path, monkeypatch):
    from hyprial.daemon.impl.lifecycle.lifecycle_coordinator import DomainCompleted

    router = CorrelationEventRouter()
    ports = _CompletionPorts(router)
    coordinator = LifecycleCoordinator(tmp_path / "receipt.sqlite3", LifecyclePorts(ports, ports, ports, ports), router)
    entered, release = threading.Event(), threading.Event()
    deliver = coordinator._deliver

    def hold_terminal(command):
        if isinstance(command, DomainCompleted) and command.result is not None and command.result.state is LifecycleState.COMPLETED:
            entered.set()
            assert release.wait(3)
        deliver(command)

    monkeypatch.setattr(coordinator, "_deliver", hold_terminal)
    try:
        assert coordinator.submit(LifecycleOperation.create("terminal", _spec())) is PortAdmission.ACCEPTED
        assert entered.wait(2)
        assert coordinator.result("terminal").state is LifecycleState.COMPLETED
        assert coordinator.stats()["custody"] == 1
        assert coordinator.wait("terminal", 0.02).state is LifecycleState.COMPLETED
        assert coordinator.stats()["custody"] == 1
        release.set()
        assert coordinator.wait("terminal", 2).state is LifecycleState.COMPLETED
        assert _settles(lambda: coordinator.stats()["custody"] == 0)
    finally:
        release.set()
        assert coordinator.drain(3)


def test_coordinator_stale_recovery_does_not_requeue_active_operation(tmp_path, monkeypatch):
    from hyprial.daemon.impl.lifecycle.lifecycle_coordinator import Recovered, _Mailbox

    router = CorrelationEventRouter()
    ports = _CompletionPorts(router, block_effect_token="active:forward:0")
    coordinator = LifecycleCoordinator(tmp_path / "active.sqlite3", LifecyclePorts(ports, ports, ports, ports), router, completion_timeout=0.03)
    operation = LifecycleOperation.create("active", _spec())
    processed = threading.Event()
    receive = _Mailbox.__call__

    def observe(owner, command):
        receive(owner, command)
        if isinstance(command, Recovered):
            processed.set()

    monkeypatch.setattr(_Mailbox, "__call__", observe)
    try:
        assert coordinator.submit(operation) is PortAdmission.ACCEPTED
        assert _settles(lambda: coordinator.stats()["active"] == 1)
        coordinator._deliver(Recovered((operation,)))
        assert processed.wait(2)
        assert coordinator.stats()["pending"] == 0
        assert coordinator.stats()["custody"] == 1
    finally:
        ports.release.set()
        assert coordinator.drain(3)


def test_coordinator_stale_recovery_cannot_resurrect_terminal_custody(tmp_path, monkeypatch):
    from hyprial.daemon.impl.lifecycle.lifecycle_coordinator import Recovered, _Mailbox

    router = CorrelationEventRouter()
    ports = _CompletionPorts(router)
    coordinator = LifecycleCoordinator(tmp_path / "terminal.sqlite3", LifecyclePorts(ports, ports, ports, ports), router)
    operation = LifecycleOperation.create("done", _spec())
    processed, release = threading.Event(), threading.Event()
    receive = _Mailbox.__call__
    advance = coordinator._effects_engine.advance

    def observe(owner, command):
        receive(owner, command)
        if isinstance(command, Recovered):
            processed.set()

    def hold_duplicate(operation_id):
        assert release.wait(3)
        return advance(operation_id)

    try:
        assert coordinator.submit(operation) is PortAdmission.ACCEPTED
        assert coordinator.wait("done", 2).state is LifecycleState.COMPLETED
        assert _settles(lambda: coordinator.stats()["custody"] == 0)
        monkeypatch.setattr(_Mailbox, "__call__", observe)
        monkeypatch.setattr(coordinator._effects_engine, "advance", hold_duplicate)
        coordinator._deliver(Recovered((operation,)))
        assert processed.wait(2)
        assert coordinator.stats()["custody"] == 0
        assert coordinator.stats()["active"] == coordinator.stats()["pending"] == 0
    finally:
        release.set()
        assert coordinator.drain(3)


def test_coordinator_late_reservation_ack_does_not_requeue_recovered_work(tmp_path, monkeypatch):
    from hyprial.daemon.impl.lifecycle.lifecycle_coordinator import ReservationCompleted

    router = CorrelationEventRouter()
    ports = _CompletionPorts(router, block_effect_token="reserve:forward:0")
    coordinator = LifecycleCoordinator(tmp_path / "reserve.sqlite3", LifecyclePorts(ports, ports, ports, ports), router, completion_timeout=0.03)
    entered, release = threading.Event(), threading.Event()
    deliver = coordinator._deliver
    answers = []

    def hold_reservation(command):
        if isinstance(command, ReservationCompleted):
            entered.set()
            assert release.wait(3)
        deliver(command)

    monkeypatch.setattr(coordinator, "_deliver", hold_reservation)
    submitter = threading.Thread(target=lambda: answers.append(coordinator.submit(LifecycleOperation.create("reserve", _spec()))))
    try:
        submitter.start()
        assert entered.wait(2)
        coordinator.recover()
        assert _settles(lambda: coordinator.stats()["active"] == 1)
        release.set()
        submitter.join(2)
        assert answers == [PortAdmission.ACCEPTED]
        assert coordinator.stats()["pending"] == 0
        assert coordinator.stats()["custody"] == 1
    finally:
        release.set()
        ports.release.set()
        submitter.join(2)
        assert coordinator.drain(3)


def test_close_requested_and_drained_honours_in_flight_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pure invariant: closed + _reserving > 0 is not drained, regardless of phase."""

    router = CorrelationEventRouter()
    manager = _manager(tmp_path / "saga.sqlite3", _CompletionPorts(router), router)
    monkeypatch.setattr(manager._store, "pending", lambda: ())
    try:
        with manager._condition:
            manager._closed = True
            manager._reserving = 1
        assert manager._close_requested_and_drained() is False
    finally:
        with manager._condition:
            manager._reserving = 0
        manager.drain(1.0)


def test_drain_racing_a_slow_reservation_keeps_consumer_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """drain() must not let the idle consumer exit while _reserving > 0."""

    router = CorrelationEventRouter()
    manager = _manager(tmp_path / "saga.sqlite3", _CompletionPorts(router), router)
    reserving = threading.Event()
    release = threading.Event()
    original = manager._store.reserve

    def slow_reserve(operation: LifecycleOperation):
        reserving.set()
        assert release.wait(3.0), "test did not release reservation"
        return original(operation)

    monkeypatch.setattr(manager._store, "reserve", slow_reserve)
    admitted: list[PortAdmission] = []
    submitter = threading.Thread(
        target=lambda: admitted.append(
            manager.submit(LifecycleOperation.create("slow-race", _spec()))
        )
    )
    submitter.start()
    try:
        assert reserving.wait(1.0)
        assert manager.drain(0.02) is False
        # Give the consumer at least three 50ms empty-branch cycles after
        # _closed was set, without depending on the phase of its first wait.
        time.sleep(0.15)
        assert manager._thread.is_alive(), "consumer exited during an in-flight reservation"
    finally:
        release.set()
        submitter.join(3.0)
    assert admitted == [PortAdmission.ACCEPTED]
    assert manager.wait("slow-race", 2.0).state is LifecycleState.COMPLETED
    assert manager.drain(1.0)
