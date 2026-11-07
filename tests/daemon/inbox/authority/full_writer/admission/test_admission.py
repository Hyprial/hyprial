from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
import pytest
from hyprial.kernel.impl.actor_runtime import ActorState
from hyprial.daemon import Alarm, AlarmResult
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.inbox import DEFAULT_HOLD_TTL_MS, ActorAlarmEmitter, DeliveryCustodyCoordinator, DeliveryCustodyFacade, DeliveryLifecycle, InboxMessage, MemoryDeliveryTransport
from hyprial.daemon.impl.inbox.tracking.progress import ProgressEvent
from hyprial.daemon.impl.inbox.service import InboxService
from hyprial.daemon.impl.inbox.actor import DispatchIoRequested
from hyprial.daemon.impl.dispatch.alarm import DispatchAlarm

from ..support import (
    _Events,
    _actor_state,
    _message,
    _runtime,
    _wait_until,
    authority as authority,
)


def test_fetch_refresh_ack_and_fail_mutate_only_via_actor(
    authority: tuple[DeliveryCustodyFacade, object, object, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    facade = authority[0]
    caller_thread = threading.current_thread().name
    mutation_threads: list[str] = []
    original = InboxService.refresh_hold

    def observed(
        service: InboxService, message_id: str, *, now_ms: int | None = None
    ) -> bool:
        mutation_threads.append(threading.current_thread().name)
        return original(service, message_id, now_ms=now_ms)

    monkeypatch.setattr(InboxService, "refresh_hold", observed)
    first = _message("m1")
    second = _message("m2")
    assert facade.receive(first, now_ms=10).accepted is True
    assert facade.receive(second, now_ms=11).accepted is True
    assert facade.refresh_hold("m1", now_ms=20) is True
    assert facade.fetch_pending(first.recipient, now_ms=21) == (first, second)
    assert facade.has_fetched("m1") is True
    assert facade.ack(first.recipient, "m1").acknowledged is True
    failure = facade.fail(second.recipient, "m2", "controlled-failure")
    assert failure.code == "DELIVERY_FAILED_AFTER_ACCEPTANCE"
    assert facade.dlq_count() == 1
    assert mutation_threads and mutation_threads[0] != caller_thread


def test_runtime_mutation_ports_share_only_the_inbox_owner_thread(
    authority: tuple[DeliveryCustodyFacade, object, object, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    facade = authority[0]
    caller = threading.get_ident()
    observed: dict[str, list[int]] = {}

    def observe(method: str) -> None:
        original = getattr(InboxService, method)

        def mutation(service, *args, **kwargs):
            observed.setdefault(method, []).append(threading.get_ident())
            return original(service, *args, **kwargs)

        monkeypatch.setattr(InboxService, method, mutation)

    for method in ("receive", "ack", "refresh_hold", "dismiss_system_notice",
                   "receive_progress_event"):
        observe(method)

    def forbidden_legacy_progress(*_args, **_kwargs):
        raise AssertionError("progress must use the typed owner path")

    monkeypatch.setattr(InboxService, "submit_progress_event", forbidden_legacy_progress)
    message = _message("runtime-ports")
    assert facade.receive(message, now_ms=10).accepted
    assert facade.refresh_hold(message.message_id, now_ms=20)
    assert facade.ack(message.recipient, message.message_id).acknowledged
    notice = InboxMessage(
        message_id="runtime-notice",
        conversation_id="c1",
        sender="system:hyprial:node-b",
        recipient=message.recipient,
        payload=b'{"message":"notice"}',
        intent="system",
        lifecycle=DeliveryLifecycle.ONLINE_ONLY,
        created_at_ms=10,
    )
    assert facade.receive_system_notice(notice)
    assert facade.dismiss_system_notice(notice.message_id)
    progress = ProgressEvent(
        delivery_id="runtime-progress", conversation_id="c1",
        actor=message.recipient, harness="codex", phase="running", seq=1,
        emitted_at_ms=10, summary="working",
    )
    assert facade.submit_progress_event(progress, recipient=message.recipient)
    owner_threads = set(observed["receive"])
    assert len(owner_threads) == 1 and caller not in owner_threads
    for method in ("ack", "refresh_hold", "dismiss_system_notice", "receive_progress_event"):
        assert set(observed[method]) == owner_threads


def test_system_notice_progress_and_prune_paths_use_authority(
    authority: tuple[DeliveryCustodyFacade, object, object, object],
) -> None:
    facade = authority[0]
    notice = InboxMessage(
        message_id="notice-1",
        conversation_id="c1",
        sender="system:hyprial:node-b",
        recipient="agent:owner:node-b:bob",
        payload=b'{"message":"notice"}',
        intent="system",
        lifecycle=DeliveryLifecycle.ONLINE_ONLY,
        created_at_ms=10,
    )
    assert facade.receive_system_notice(notice) is True
    assert facade.drain_system_notices(notice.recipient) == (notice,)
    assert facade.receive_system_notice(notice) is True
    assert facade.system_notices(notice.recipient) == ()
    assert facade.ack(notice.recipient, notice.message_id).code == "MESSAGE_ALREADY_SETTLED"
    # Only the reminder producer opts into a fresh presentation of the same id.
    assert facade.receive_system_notice(notice, allow_replay=True) is True
    assert facade.dismiss_system_notice(notice.message_id) is True

    progress = ProgressEvent(
        delivery_id="delivery-1",
        conversation_id="c1",
        actor="agent:owner:node-b:bob",
        harness="codex",
        phase="running",
        seq=1,
        emitted_at_ms=10,
        summary="working",
    )
    assert facade.submit_progress_event(progress, recipient=notice.recipient) is True
    stored = facade.list_progress_events(notice.recipient)
    assert len(stored) == 1 and stored[0].intent == "progress"

    held = _message("prunable")
    facade.submit(held, now_ms=0, defer_direct=True)
    pruned = facade.prune_outbox(
        undeliverable=lambda recipient: recipient == held.recipient,
        now_ms=1,
    )
    assert [item.message_id for item in pruned] == ["prunable"]


def test_expired_sender_notice_uses_actor_io_without_retry(
    authority: tuple[DeliveryCustodyFacade, object, object, object],
    tmp_path: Path,
) -> None:
    facade, coordinator, transport, _events = authority
    assert isinstance(coordinator, DeliveryCustodyCoordinator)
    assert isinstance(transport, MemoryDeliveryTransport)
    message = _message("expired-authority")
    assert facade.receive(message, now_ms=0).accepted

    pruned = facade.prune_inbox(now_ms=DEFAULT_HOLD_TTL_MS)

    assert [item.message_id for item in pruned] == [message.message_id]
    _wait_until(lambda: len(transport.notices) == 1)
    [(target_node, notice)] = transport.notices
    assert target_node == "node-a"
    assert notice.recipient == message.sender
    assert json.loads(notice.payload)["reason"] == "TTL_EXPIRED"
    assert message.payload not in notice.payload
    log_path = tmp_path / "logs" / "daemon.jsonl"
    _wait_until(
        lambda: log_path.exists()
        and "inbox.expired_sender_notice" in log_path.read_text(encoding="utf-8")
    )
    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    attempts = [
        record
        for record in records
        if record["event"] == "inbox.expired_sender_notice"
    ]
    assert len(attempts) == 1
    assert attempts[0]["messageId"] == message.message_id
    assert attempts[0]["delivered"] is True
    assert _actor_state(coordinator) is ActorState.RUNNING


def test_terminal_retry_stages_local_notice_without_blocking_terminal_commit(
    authority: tuple[DeliveryCustodyFacade, object, object, object],
) -> None:
    facade, _coordinator, _transport, events = authority
    assert isinstance(events, _Events)
    message = InboxMessage(
        message_id="terminal-local",
        conversation_id="terminal-conversation",
        sender="agent:owner:node-b:alice",
        recipient="agent:owner:node-b:missing",
        payload=b'{"message":"expire"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=10,
        idempotency_key="terminal-local-idem",
    )
    assert facade.submit(message, now_ms=10).queued

    [result] = facade.retry_due(now_ms=10**15)

    assert result.accepted is False
    assert result.code == "TTL_EXPIRED"
    _wait_until(lambda: bool(facade.system_notices(message.sender)))
    [notice] = facade.system_notices(message.sender)
    assert json.loads(notice.payload)["originalMessageId"] == message.message_id
    requests = [
        event
        for event in events.snapshot()
        if isinstance(event, DispatchIoRequested)
    ]
    assert len(requests) == 2  # initial submit + best-effort TTL alarm


def test_shared_dispatch_alarm_uses_actor_alarm_emitter(authority, tmp_path: Path) -> None:
    facade, _coordinator, _transport, _events = authority
    alarm = DispatchAlarm(facade.alarm_emitter)
    alarm.escalate(to="agent:owner:node-b:bob", text="workflow timeout")
    notices = facade.system_notices("agent:owner:node-b:bob")
    assert len(notices) == 1
    assert json.loads(notices[0].payload)["reason"] == "WORKFLOW_TARGET_TIMEOUT"


def test_harness_failed_alarm_survives_root_cutover_and_missing_emitter_mutation(
    authority: tuple[DeliveryCustodyFacade, object, object, object],
) -> None:
    facade = authority[0]
    application = object.__new__(DaemonApplication)
    application._inbox = facade
    application.node_id = "node-b"
    application.owner = "owner"

    emitter = facade.alarm_emitter
    facade.alarm_emitter = None  # type: ignore[assignment]
    application._raise_harness_failed_alarm("codex:mutated")
    assert facade.system_notices("node-b") == ()

    facade.alarm_emitter = emitter
    application._raise_harness_failed_alarm("codex:worker")
    notices = facade.system_notices("node-b")
    assert len(notices) == 1
    assert json.loads(notices[0].payload)["reason"] == (
        "HARNESS_FAILED:codex:worker"
    )


def test_blocked_human_alarm_delivery_does_not_occupy_state_actor(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def deliver_human(_alarm: Alarm, _text: str) -> bool:
        entered.set()
        assert release.wait(1.0)
        return True

    events = _Events()
    coordinator = DeliveryCustodyCoordinator(
        tmp_path / "inbox.sqlite3",
        MemoryDeliveryTransport(),
        events,
        runtime=_runtime(),
        node_id="node-b",
        service_options={"alarm_human_delivery": deliver_human},
    )
    facade = DeliveryCustodyFacade(coordinator, tmp_path / "inbox.sqlite3")
    results: list[AlarmResult] = []
    alarm = Alarm(
        correlation_id="human-alarm",
        message_id="human-alarm",
        conversation_id="human-conversation",
        sender="channel:lark:test-app",
        recipient="operator",
        reason="HUMAN_ALARM",
        audience="human",
    )
    thread = threading.Thread(
        target=lambda: results.append(
            facade.alarm_emitter.emit(alarm, terminal=False)
        )
    )
    thread.start()
    assert entered.wait(1.0)
    assert facade.receive(_message("while-alarm-blocks"), now_ms=10).accepted
    release.set()
    thread.join(1.0)
    assert results == [AlarmResult("delivered", "human")]
    assert coordinator.drain(1.0).complete


def test_alarm_delivery_failure_is_non_recursive(tmp_path: Path) -> None:
    attempts = 0

    def fail_delivery(_alarm: Alarm, _text: str) -> bool:
        nonlocal attempts
        attempts += 1
        raise OSError("controlled alarm transport failure")

    coordinator = DeliveryCustodyCoordinator(
        tmp_path / "inbox.sqlite3",
        MemoryDeliveryTransport(),
        _Events(),
        runtime=_runtime(),
        service_options={"alarm_human_delivery": fail_delivery},
    )
    facade = DeliveryCustodyFacade(coordinator, tmp_path / "inbox.sqlite3")
    result = facade.alarm_emitter.emit(
        Alarm(
            correlation_id="failed-alarm",
            message_id="failed-alarm",
            conversation_id="failed-conversation",
            sender="channel:lark:test-app",
            recipient="operator",
            reason="CONTROLLED_FAILURE",
            audience="human",
        ),
        terminal=False,
    )
    assert result == AlarmResult("failed", "human")
    assert result.claimed is True
    assert attempts == 1
    assert coordinator.drain(1.0).complete


def test_alarm_throttle_false_does_not_consume_throttle_window(
    tmp_path: Path,
) -> None:
    deliveries = 0

    def deliver_human(_alarm: Alarm, _text: str) -> bool:
        nonlocal deliveries
        deliveries += 1
        return True

    database = tmp_path / "inbox.sqlite3"
    coordinator = DeliveryCustodyCoordinator(
        database,
        MemoryDeliveryTransport(),
        _Events(),
        runtime=_runtime(),
        service_options={"alarm_human_delivery": deliver_human},
    )
    facade = DeliveryCustodyFacade(coordinator, database)
    alarm = Alarm(
        correlation_id="throttle-semantics",
        message_id="throttle-semantics",
        conversation_id="throttle-semantics",
        sender="channel:lark:test-app",
        recipient="operator",
        reason="THROTTLE_SEMANTICS",
        audience="human",
    )
    assert facade.alarm_emitter.emit(
        alarm,
        terminal=False,
        throttle=False,
    ) == AlarmResult("delivered", "human")
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT COUNT(*) FROM alarm_throttle").fetchone()[0] == 0
    finally:
        connection.close()
    assert facade.alarm_emitter.emit(
        alarm,
        terminal=False,
        throttle=True,
    ) == AlarmResult("delivered", "human")
    assert facade.alarm_emitter.emit(
        alarm,
        terminal=False,
        throttle=True,
    ) == AlarmResult("throttled", "human")
    assert deliveries == 2
    assert coordinator.drain(1.0).complete is True


@pytest.mark.parametrize("error_type", (NameError, ImportError))
def test_actor_alarm_emitter_rethrows_program_errors(
    error_type: type[Exception],
) -> None:
    class BrokenAuthority:
        @staticmethod
        def emit_alarm(
            alarm: Alarm, *, terminal: bool, throttle: bool
        ) -> AlarmResult:
            del alarm, terminal, throttle
            raise error_type("controlled program error")

    emitter = ActorAlarmEmitter(BrokenAuthority())  # type: ignore[arg-type]
    with pytest.raises(error_type, match="controlled program error"):
        emitter.emit(
            Alarm(
                correlation_id="program-error",
                message_id="program-error",
                conversation_id="program-error",
                sender="node-b",
                recipient="owner",
                reason="PROGRAM_ERROR",
                audience="operator",
            ),
            terminal=False,
        )


def test_actor_alarm_emitter_maps_runtime_failure_to_failed() -> None:
    class BrokenAuthority:
        @staticmethod
        def emit_alarm(
            alarm: Alarm, *, terminal: bool, throttle: bool
        ) -> AlarmResult:
            del alarm, terminal, throttle
            raise OSError("controlled runtime failure")

    alarm = Alarm(
        correlation_id="runtime-error",
        message_id="runtime-error",
        conversation_id="runtime-error",
        sender="node-b",
        recipient="owner",
        reason="RUNTIME_ERROR",
        audience="operator",
    )
    emitter = ActorAlarmEmitter(BrokenAuthority())  # type: ignore[arg-type]
    result = emitter.emit(alarm, terminal=False)
    assert result == AlarmResult("failed", "operator")
    assert result.claimed is False
