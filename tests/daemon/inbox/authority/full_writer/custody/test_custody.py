"""Custody: one durable terminal outcome survives a failed notice generation.

The injected notice failure escapes ``_complete_dispatch`` on purpose:
``__call__`` marks that generation failed and re-raises, the guardian replaces
it, and the I/O worker keeps the unadmitted alarm completion in its unsettled
queue until the new generation is RUNNING.  The notice is therefore delivered
AFTER the restart, so a test that waits only for the new generation samples
before the effect -- the run28487 flake.  The wait below targets the effect
itself and is bounded: a genuinely lost notice must go red within
``WAIT_TIMEOUT_SECONDS``, never hang.  The generation/RUNNING check stays as
evidence that a restart happened.

Allen 2026-10-17: no inline comments in tests; decisions live here.
"""

from __future__ import annotations

from dataclasses import replace
import threading
import time
from pathlib import Path
import pytest
from hyprial.kernel.impl.actor_runtime import (
    STATE_AUTHORITY,
    ActorRuntime,
    ActorSpec,
    ActorState,
    AdmissionResult,
    SupervisionPolicy,
)
from hyprial.kernel.impl.contracts.ports import PortAdmission, PortCommandRejected
from hyprial.daemon import ALARM_THROTTLE_WINDOW_MS, Alarm, AlarmResult
from hyprial.daemon.impl.inbox import DeliveryCustodyCoordinator, DeliveryCustodyFacade, DeliveryLifecycle, InboxMessage, MemoryDeliveryTransport
from hyprial.daemon.impl.inbox.tracking.progress import ProgressEvent
from hyprial.daemon.impl.inbox.service import InboxService
from hyprial.daemon.impl.inbox.actor import (
    DispatchIoCompleted,
    DispatchIoFailed,
    DispatchIoKind,
    DispatchIoRequested,
    ReassociateIoCompletion,
)
from hyprial.daemon.impl.inbox.contracts.ports import AlarmCompleted, BoolMutationCompleted, EmitAlarmCommand, SubmitProgressCommand

from ..support import (
    _BlockingFinalEvents,
    _BlockingProgressTransport,
    _CompletionClosedRuntime,
    _CountingCompletionRuntime,
    _DoubleRestartReassociateRuntime,
    _Events,
    _actor_state,
    _message,
    _runtime,
    WAIT_TIMEOUT_SECONDS,
    _wait_until,
    authority as authority,
)


class _RestartObservedRuntime(ActorRuntime):
    def __init__(self) -> None:
        super().__init__(
            policies={
                STATE_AUTHORITY: SupervisionPolicy(
                    max_restarts=3,
                    restart_window=10.0,
                    base_backoff=0.0,
                    max_backoff=0.0,
                    jitter_ratio=0.0,
                )
            },
            random_source=lambda: 0.5,
        )
        self._factory_calls = 0
        self.restarted = threading.Event()

    def start(self, spec: ActorSpec):  # type: ignore[override]
        factory = spec.handler_factory

        def observed_factory():
            handler = factory()
            self._factory_calls += 1
            if self._factory_calls > 1:
                self.restarted.set()
            return handler

        return super().start(replace(spec, handler_factory=observed_factory))


def test_custody_retry_and_receipt_retirement_are_actor_owned(
    authority: tuple[DeliveryCustodyFacade, object, object, object],
) -> None:
    facade, _coordinator, raw_transport, _events = authority
    assert isinstance(raw_transport, MemoryDeliveryTransport)
    custody = _message("custody")
    assert facade.accept_custody(
        custody,
        mailbox_node="node-b",
        now_ms=0,
    ).acknowledged is True
    raw_transport.set_online(custody.recipient, True)
    raw_transport.confirm_direct(custody.message_id)
    outcomes = facade.retry_custody_due(now_ms=0)
    assert outcomes == [
        type(outcomes[0])("custody", True, queued=False)
    ]
    assert facade.has_custody("custody") is False

    held = _message("held")
    assert facade.submit(held, now_ms=0, defer_direct=True).queued is True
    assert facade.retire_outbox_receipt(held.sender, held.message_id, now_ms=5)
    assert facade.outbox_count() == 0


def _wait_for_terminal_notice(facade: DeliveryCustodyFacade, sender: str) -> None:
    deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if len(facade.system_notices(sender)) == 1:
            return
        time.sleep(0.005)
    raise AssertionError(
        "the terminal notice was not persisted within "
        f"{WAIT_TIMEOUT_SECONDS:.1f}s; notices={len(facade.system_notices(sender))}"
    )


def test_terminal_notice_failure_does_not_roll_back_ttl_outcome(
    authority: tuple[DeliveryCustodyFacade, object, object, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    facade, coordinator, _transport, _events = authority
    assert isinstance(coordinator, DeliveryCustodyCoordinator)
    message = InboxMessage(
        message_id="terminal-restart",
        conversation_id="terminal-restart-conversation",
        sender="agent:owner:node-b:alice",
        recipient="agent:owner:node-b:missing",
        payload=b'{"message":"expire"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=10,
        idempotency_key="terminal-restart-idem",
    )
    assert facade.submit(message, now_ms=10, defer_direct=True).queued
    original = InboxService._persist_system_notice_locked
    failed = False

    def fail_once(service: InboxService, notice: InboxMessage):  # type: ignore[no-untyped-def]
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("injected post-DLQ notice failure")
        return original(service, notice)

    monkeypatch.setattr(InboxService, "_persist_system_notice_locked", fail_once)
    generation = coordinator.generation
    with pytest.raises(Exception):
        facade.retry_due(now_ms=10**15)
    _wait_for_terminal_notice(facade, message.sender)
    assert coordinator.generation > generation
    assert _actor_state(coordinator) is ActorState.RUNNING
    assert facade.outbox_count() == 0
    assert facade.dlq_count() == 1
    [record] = facade.delivery_status_records(
        message.sender, message_id=message.message_id
    )
    assert record.reason == "TTL_EXPIRED"


def test_async_notice_fault_restarts_actor_without_rolling_back_ttl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = _Events()
    transport = MemoryDeliveryTransport()
    runtime = _RestartObservedRuntime()
    database = tmp_path / "inbox.sqlite3"
    coordinator = DeliveryCustodyCoordinator(
        database,
        transport,
        events,
        runtime=runtime,
        node_id="node-b",
    )
    facade = DeliveryCustodyFacade(coordinator, database, timeout=WAIT_TIMEOUT_SECONDS)
    try:
        message = InboxMessage(
            message_id="async-terminal-restart",
            conversation_id="async-terminal-conversation",
            sender="agent:owner:node-b:alice",
            recipient="agent:owner:node-b:missing",
            payload=b'{"message":"expire"}',
            intent="request",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=10,
            idempotency_key="async-terminal-restart-idem",
        )
        assert facade.submit(message, now_ms=10, defer_direct=True).queued
        original = InboxService._persist_system_notice_locked
        fault_seen = threading.Event()
        recovered = threading.Event()
        calls = 0

        def fail_once(service: InboxService, notice: InboxMessage):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            if calls == 1:
                fault_seen.set()
                raise RuntimeError("injected async notice persistence failure")
            recovered.set()
            return original(service, notice)

        monkeypatch.setattr(InboxService, "_persist_system_notice_locked", fail_once)
        completion_seen = threading.Event()
        release_completion = threading.Event()
        completion_sink = coordinator._io_worker._completion_sink

        # Hold the ALARM completion after retry's durable result so the
        # injected notice fault is proven actor-owned, not caller timing.
        def controlled_completion(request, completion):  # type: ignore[no-untyped-def]
            if request.kind is DispatchIoKind.ALARM:
                completion_seen.set()
                assert release_completion.wait(WAIT_TIMEOUT_SECONDS)
            return completion_sink(request, completion)

        coordinator._io_worker._completion_sink = controlled_completion
        [result] = facade.retry_due(now_ms=10**15)
        assert result.accepted is False
        assert result.code == "TTL_EXPIRED"
        assert completion_seen.wait(WAIT_TIMEOUT_SECONDS)
        assert fault_seen.is_set() is False
        assert facade.outbox_count() == 0
        assert facade.dlq_count() == 1
        [record] = facade.delivery_status_records(
            message.sender, message_id=message.message_id
        )
        assert record.reason == "TTL_EXPIRED"
        release_completion.set()
        assert fault_seen.wait(WAIT_TIMEOUT_SECONDS)
        assert runtime.restarted.wait(WAIT_TIMEOUT_SECONDS)
        assert recovered.wait(WAIT_TIMEOUT_SECONDS)
        # The injected event precedes the SQL mutation and its projection
        # publication. FULL WAL synchronization makes that window observable;
        # wait for the authority's published fact, not callback entry.
        deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
        while not facade.system_notices(message.sender):
            assert time.monotonic() < deadline, "recovered notice was never published"
            time.sleep(0.005)
        assert len(facade.system_notices(message.sender)) == 1
        assert _actor_state(coordinator) is ActorState.RUNNING
    finally:
        release_completion.set()
        assert coordinator.drain(WAIT_TIMEOUT_SECONDS).complete


def test_alarm_throttle_and_completion_are_correlated(
    authority: tuple[DeliveryCustodyFacade, object, object, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Throttling uses aligned windows, so adjacent wall-clock calls can land
    # in different minutes. Control this clock independently of actor timers.
    now_ms = ALARM_THROTTLE_WINDOW_MS + 100
    monkeypatch.setattr(InboxService, "_now_ms", staticmethod(lambda: now_ms))
    facade, _coordinator, _transport, raw_events = authority
    assert isinstance(raw_events, _Events)
    alarm = Alarm(
        correlation_id="alarm-original",
        message_id="alarm-original",
        conversation_id="alarm-conversation",
        sender="node-b",
        recipient="owner",
        reason="CONTROLLED_ALARM",
        audience="operator",
    )
    first = facade.alarm_emitter.emit(alarm, terminal=False)
    second = facade.alarm_emitter.emit(alarm, terminal=False)
    assert first == AlarmResult("delivered", "operator")
    assert second == AlarmResult("throttled", "operator")
    assert first.claimed is True
    assert second.claimed is False
    requested = [
        event for event in raw_events.items if isinstance(event, DispatchIoRequested)
    ]
    completed = [
        event for event in raw_events.items if isinstance(event, DispatchIoCompleted)
    ]
    terminal = [event for event in raw_events.items if isinstance(event, AlarmCompleted)]
    assert len(requested) == len(completed) == 1
    assert len(terminal) == 2
    assert requested[0].correlation_id == completed[0].correlation_id
    assert terminal[0].result.status == "delivered"
    assert terminal[1].result.status == "throttled"

    # Advancing into the next window must admit a fresh, correlated delivery.
    now_ms += ALARM_THROTTLE_WINDOW_MS
    assert facade.alarm_emitter.emit(alarm, terminal=False) == AlarmResult(
        "delivered", "operator"
    )
    requested = [
        event for event in raw_events.items if isinstance(event, DispatchIoRequested)
    ]
    completed = [
        event for event in raw_events.items if isinstance(event, DispatchIoCompleted)
    ]
    assert len(requested) == len(completed) == 2
    assert requested[1].correlation_id == completed[1].correlation_id
    assert requested[1].correlation_id != requested[0].correlation_id


def test_progress_closed_completion_retains_custody_and_blocks_drain(
    tmp_path: Path,
) -> None:
    runtime = _CompletionClosedRuntime()
    events = _Events()
    transport = MemoryDeliveryTransport()
    coordinator = DeliveryCustodyCoordinator(
        tmp_path / "inbox.sqlite3",
        transport,
        events,
        runtime=runtime,
        node_id="node-b",
        completion_deadline=0.01,
        completion_backoff=(0.001,),
    )
    progress = ProgressEvent(
        delivery_id="progress-closed",
        conversation_id="c1",
        actor="agent:owner:node-b:bob",
        harness="codex",
        phase="running",
        seq=1,
        emitted_at_ms=10,
        summary="working",
    )
    assert coordinator.submit(
        SubmitProgressCommand(
            "progress-closed",
            progress,
            "agent:owner:node-c:alice",
        )
    ) is PortAdmission.ACCEPTED
    _wait_until(lambda: len(transport.progress_events) == 1)
    time.sleep(0.03)
    assert not any(
        isinstance(event, DispatchIoFailed)
        and event.code == "COMPLETION_DURABLY_HANDED_OFF"
        for event in events.snapshot()
    )
    facade = DeliveryCustodyFacade(coordinator, tmp_path / "inbox.sqlite3")
    assert facade.outbox_count() == facade.custody_count() == 0
    assert coordinator.drain(0.03).complete is False
    assert not any(
        isinstance(event, BoolMutationCompleted)
        and event.correlation_id == "progress-closed"
        for event in events.snapshot()
    )

    runtime.allow_completions()
    _wait_until(
        lambda: any(
            isinstance(event, BoolMutationCompleted)
            and event.correlation_id == "progress-closed"
            for event in events.snapshot()
        )
    )
    assert coordinator.drain(WAIT_TIMEOUT_SECONDS).complete is True


def test_alarm_closed_completion_retains_custody_and_blocks_drain(
    tmp_path: Path,
) -> None:
    runtime = _CompletionClosedRuntime()
    events = _Events()
    delivered = 0

    def deliver_human(_alarm: Alarm, _text: str) -> bool:
        nonlocal delivered
        delivered += 1
        return True

    coordinator = DeliveryCustodyCoordinator(
        tmp_path / "inbox.sqlite3",
        MemoryDeliveryTransport(),
        events,
        runtime=runtime,
        completion_deadline=0.01,
        completion_backoff=(0.001,),
        service_options={"alarm_human_delivery": deliver_human},
    )
    alarm = Alarm(
        correlation_id="alarm-closed",
        message_id="alarm-closed",
        conversation_id="alarm-closed",
        sender="channel:lark:test-app",
        recipient="operator",
        reason="ALARM_CLOSED",
        audience="human",
    )
    assert coordinator.submit(
        EmitAlarmCommand("alarm-closed", alarm, terminal=False)
    ) is PortAdmission.ACCEPTED
    _wait_until(lambda: delivered == 1)
    time.sleep(0.03)
    assert not any(
        isinstance(event, DispatchIoFailed)
        and event.code == "COMPLETION_DURABLY_HANDED_OFF"
        for event in events.snapshot()
    )
    facade = DeliveryCustodyFacade(coordinator, tmp_path / "inbox.sqlite3")
    assert facade.outbox_count() == facade.custody_count() == 0
    assert coordinator.drain(0.03).complete is False
    assert not any(
        isinstance(event, AlarmCompleted) and event.correlation_id == "alarm-closed"
        for event in events.snapshot()
    )

    runtime.allow_completions()
    _wait_until(
        lambda: any(
            isinstance(event, AlarmCompleted) and event.correlation_id == "alarm-closed"
            for event in events.snapshot()
        )
    )
    assert delivered == 1
    assert coordinator.drain(WAIT_TIMEOUT_SECONDS).complete is True


def test_progress_completion_reassociates_after_actor_restart_without_io_replay(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    events = _Events()
    transport = _BlockingProgressTransport()
    coordinator = DeliveryCustodyCoordinator(
        tmp_path / "inbox.sqlite3",
        transport,
        events,
        runtime=runtime,
        node_id="node-b",
    )
    progress = ProgressEvent(
        delivery_id="progress-restart",
        conversation_id="c1",
        actor="agent:owner:node-b:bob",
        harness="codex",
        phase="running",
        seq=1,
        emitted_at_ms=10,
        summary="working",
    )
    assert coordinator.submit(
        SubmitProgressCommand(
            "progress-restart",
            progress,
            "agent:owner:node-c:alice",
        )
    ) is PortAdmission.ACCEPTED
    assert transport.entered.wait(1.0)
    assert runtime.tell(coordinator.handle, object()) is AdmissionResult.ACCEPTED
    _wait_until(
        lambda: (
            runtime.snapshot(coordinator.handle).generation == 2
            and runtime.snapshot(coordinator.handle).state is ActorState.RUNNING
        )
    )
    transport.release.set()
    _wait_until(
        lambda: any(
            isinstance(event, BoolMutationCompleted)
            and event.correlation_id == "progress-restart"
            and event.result is True
            for event in events.snapshot()
        )
    )
    assert transport.calls == 1
    assert len(
        [
            event
            for event in events.snapshot()
            if isinstance(event, BoolMutationCompleted)
            and event.correlation_id == "progress-restart"
        ]
    ) == 1
    assert coordinator.drain(WAIT_TIMEOUT_SECONDS).complete is True


def test_completion_receipt_survives_double_restart_takeover_toctou(
    tmp_path: Path,
) -> None:
    runtime = _DoubleRestartReassociateRuntime()
    events = _Events()
    transport = _BlockingProgressTransport()
    coordinator = DeliveryCustodyCoordinator(
        tmp_path / "inbox.sqlite3",
        transport,
        events,
        runtime=runtime,
        node_id="node-b",
        completion_receipt_timeout=0.2,
    )
    progress = ProgressEvent(
        delivery_id="progress-double-restart",
        conversation_id="c1",
        actor="agent:owner:node-b:bob",
        harness="codex",
        phase="running",
        seq=1,
        emitted_at_ms=10,
        summary="working",
    )
    try:
        assert coordinator.submit(
            SubmitProgressCommand(
                "progress-double-restart",
                progress,
                "agent:owner:node-c:alice",
            )
        ) is PortAdmission.ACCEPTED
        assert transport.entered.wait(1.0)
        assert runtime.tell(coordinator.handle, object()) is AdmissionResult.ACCEPTED
        _wait_until(
            lambda: (
                runtime.snapshot(coordinator.handle).generation == 2
                and runtime.snapshot(coordinator.handle).state is ActorState.RUNNING
            )
        )
        transport.release.set()
        _wait_until(
            lambda: any(
                isinstance(event, BoolMutationCompleted)
                and event.correlation_id == "progress-double-restart"
                for event in events.snapshot()
            ),
            timeout=2.0,
        )
        assert runtime.snapshot(coordinator.handle).generation == 3
        assert transport.calls == 1
        assert len(
            [
                event
                for event in events.snapshot()
                if isinstance(event, BoolMutationCompleted)
                and event.correlation_id == "progress-double-restart"
            ]
        ) == 1
        assert any(
            isinstance(event, PortCommandRejected)
            and event.correlation_id == "progress-double-restart"
            and event.code == "STALE_COMMAND"
            for event in events.snapshot()
        )
    finally:
        transport.release.set()
        assert coordinator.drain(WAIT_TIMEOUT_SECONDS).complete is True


def test_pending_receipt_waiter_does_not_enqueue_duplicate_completion(
    tmp_path: Path,
) -> None:
    correlation_id = "progress-held-processing"
    runtime = _CountingCompletionRuntime()
    events = _BlockingFinalEvents(correlation_id)
    transport = MemoryDeliveryTransport()
    coordinator = DeliveryCustodyCoordinator(
        tmp_path / "inbox.sqlite3",
        transport,
        events,
        runtime=runtime,
        node_id="node-b",
        completion_deadline=0.2,
        completion_backoff=(0.001,),
        completion_receipt_timeout=0.01,
    )
    progress = ProgressEvent(
        delivery_id=correlation_id,
        conversation_id="c1",
        actor="agent:owner:node-b:bob",
        harness="codex",
        phase="running",
        seq=1,
        emitted_at_ms=10,
        summary="working",
    )
    assert coordinator.submit(
        SubmitProgressCommand(
            correlation_id,
            progress,
            "agent:owner:node-c:alice",
        )
    ) is PortAdmission.ACCEPTED
    assert events.entered.wait(1.0)
    time.sleep(0.05)
    assert runtime.completion_admissions == 1
    events.release.set()
    _wait_until(
        lambda: any(
            isinstance(event, BoolMutationCompleted)
            and event.correlation_id == correlation_id
            for event in events.snapshot()
        )
    )
    assert len(transport.progress_events) == 1
    assert runtime.completion_admissions == 1
    assert len(
        [
            event
            for event in events.snapshot()
            if isinstance(event, BoolMutationCompleted)
            and event.correlation_id == correlation_id
        ]
    ) == 1

    request = next(
        event for event in events.snapshot() if isinstance(event, DispatchIoRequested)
    )
    completion = next(
        event for event in events.snapshot() if isinstance(event, DispatchIoCompleted)
    )
    assert runtime.tell(
        coordinator.handle,
        ReassociateIoCompletion(
            correlation_id=correlation_id,
            generation=coordinator.generation,
            version=coordinator.version,
            request=request,
            completion=completion,
        ),
    ) is AdmissionResult.ACCEPTED
    time.sleep(0.03)
    assert len(
        [
            event
            for event in events.snapshot()
            if isinstance(event, BoolMutationCompleted)
            and event.correlation_id == correlation_id
        ]
    ) == 1
    assert coordinator.drain(WAIT_TIMEOUT_SECONDS).complete is True
