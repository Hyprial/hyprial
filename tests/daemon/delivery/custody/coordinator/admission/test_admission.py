from __future__ import annotations

import sqlite3
import inspect
import threading
from dataclasses import replace
from pathlib import Path
from hyprial.kernel.impl.contracts.ports import PortAdmission, PortCommandRejected
from hyprial.daemon.impl.adapters.lark.contracts.reply_bridge import LarkReplyBridgeTransport
from hyprial.daemon.impl.inbox.actor import DeliveryCustody, DeliveryCustodyCoordinator, DispatchIoCompleted
from hyprial.daemon.impl.inbox.authority import DeliveryCustodyFacade
from hyprial.daemon.impl.inbox.memory import MemoryDeliveryTransport
from hyprial.daemon.impl.inbox.contracts.ports import ReceiveCompleted, ReceiveMessageCommand, RetryDueCommand, RetryIoCompleted, SubmissionCompleted, SubmitMessageCommand
from hyprial.daemon.impl.inbox.tracking.progress import PROGRESS_INTENT, ProgressEvent, encode_progress_event
from hyprial.daemon.impl.inbox.links.pull import DEFAULT_HOLD_TTL_MS
from hyprial.daemon.impl.inbox.service import InboxService

from ..support import (
    _BlockingTransport,
    _Events,
    _OnlineTransport,
    _close,
    _message,
    _runtime,
    _wait_until,
)


def test_submit_returns_admission_and_correlated_typed_event(tmp_path: Path) -> None:
    events = _Events()
    transport = _OnlineTransport()
    coordinator = DeliveryCustodyCoordinator(
        tmp_path / "inbox.sqlite3", transport, events, runtime=_runtime()
    )

    assert coordinator.submit(SubmitMessageCommand("submit-1", _message())) is (
        PortAdmission.ACCEPTED
    )
    _wait_until(
        lambda: any(isinstance(item, SubmissionCompleted) for item in events.snapshot())
    )
    event = next(
        item for item in events.snapshot() if isinstance(item, SubmissionCompleted)
    )
    assert event.correlation_id == "submit-1"
    assert event.result.to_payload() == {
        "messageId": "m1",
        "accepted": True,
        "queued": False,
    }
    assert transport.deliveries == ["m1"]
    assert coordinator.read_counts().outbox_count == 0
    _close(coordinator, events)


def test_duplicate_receive_is_idempotent_and_projection_is_stable(tmp_path: Path) -> None:
    events = _Events()
    coordinator = DeliveryCustodyCoordinator(
        tmp_path / "inbox.sqlite3",
        MemoryDeliveryTransport(),
        events,
        runtime=_runtime(),
    )
    message = _message()
    assert coordinator.submit(ReceiveMessageCommand("receive-1", message)) is (
        PortAdmission.ACCEPTED
    )
    assert coordinator.submit(ReceiveMessageCommand("receive-2", message)) is (
        PortAdmission.ACCEPTED
    )
    _wait_until(
        lambda: len(
            [item for item in events.snapshot() if isinstance(item, ReceiveCompleted)]
        )
        == 2
    )
    received = [
        item for item in events.snapshot() if isinstance(item, ReceiveCompleted)
    ]
    assert received[0].result.duplicate is False
    assert received[1].result.duplicate is True
    assert coordinator.read_pending(message.recipient) == (message,)
    assert coordinator.has_pending_work(message.recipient)
    assert coordinator.read_counts().version == received[-1].version
    _close(coordinator, events)


def test_progress_storage_mutation_is_owned_by_state_actor(tmp_path: Path) -> None:
    database = tmp_path / "inbox.sqlite3"
    events = _Events()
    coordinator = DeliveryCustodyCoordinator(
        database,
        MemoryDeliveryTransport(),
        events,
        runtime=_runtime(),
    )
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
    message = replace(
        _message("progress-1"),
        intent=PROGRESS_INTENT,
        payload=encode_progress_event(progress),
    )
    assert coordinator.submit(ReceiveMessageCommand("progress", message)) is (
        PortAdmission.ACCEPTED
    )
    _wait_until(
        lambda: any(
            isinstance(item, ReceiveCompleted)
            and item.correlation_id == "progress"
            for item in events.snapshot()
        )
    )
    assert coordinator.read_pending(message.recipient) == ()
    _close(coordinator, events)
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT COUNT(*) FROM progress_events").fetchone()[0] == 1
    finally:
        connection.close()


def test_blocked_transport_does_not_occupy_delivery_state_actor(
    tmp_path: Path,
) -> None:
    events = _Events()
    transport = _BlockingTransport()
    coordinator = DeliveryCustodyCoordinator(
        tmp_path / "inbox.sqlite3",
        transport,
        events,
        runtime=_runtime(),
        io_capacity=1,
    )
    outbound = _message("m1")
    inbound = _message("m2")
    assert coordinator.submit(SubmitMessageCommand("blocked-io", outbound)) is (
        PortAdmission.ACCEPTED
    )
    assert transport.entered.wait(1.0)
    assert coordinator.submit(ReceiveMessageCommand("state-command", inbound)) is (
        PortAdmission.ACCEPTED
    )
    _wait_until(
        lambda: any(
            isinstance(item, ReceiveCompleted)
            and item.correlation_id == "state-command"
            for item in events.snapshot()
        )
    )
    assert coordinator.read_pending(inbound.recipient) == (inbound,)
    assert coordinator.read_counts().outbox_count == 1
    assert not any(
        isinstance(item, SubmissionCompleted)
        and item.correlation_id == "blocked-io"
        for item in events.snapshot()
    )
    transport.release.set()
    _wait_until(
        lambda: any(
            isinstance(item, SubmissionCompleted)
            and item.correlation_id == "blocked-io"
            for item in events.snapshot()
        )
    )
    _close(coordinator, events)


def test_external_io_queue_is_bounded_independently_from_state_admission(
    tmp_path: Path,
) -> None:
    events = _Events()
    transport = _BlockingTransport()
    coordinator = DeliveryCustodyCoordinator(
        tmp_path / "inbox.sqlite3",
        transport,
        events,
        runtime=_runtime(),
        io_capacity=1,
    )
    assert coordinator.submit(SubmitMessageCommand("io-1", _message("m1"))) is (
        PortAdmission.ACCEPTED
    )
    assert transport.entered.wait(1.0)
    assert coordinator.submit(SubmitMessageCommand("io-2", _message("m2"))) is (
        PortAdmission.ACCEPTED
    )
    assert coordinator.submit(SubmitMessageCommand("io-3", _message("m3"))) is (
        PortAdmission.ACCEPTED
    )
    _wait_until(
        lambda: any(
            isinstance(item, SubmissionCompleted)
            and item.correlation_id == "io-3"
            and item.result.code == "DISPATCH_IO_FAILED"
            for item in events.snapshot()
        )
    )
    assert any(
        getattr(item, "code", None) == "DISPATCH_IO_OVERLOADED"
        for item in events.snapshot()
    )
    transport.release.set()
    _wait_until(
        lambda: len(
            [item for item in events.snapshot() if isinstance(item, SubmissionCompleted)]
        )
        == 3
    )
    _close(coordinator, events)


def test_state_handler_has_no_legacy_sync_dispatch_calls() -> None:
    source = inspect.getsource(DeliveryCustody)
    assert "_service.submit(" not in source
    assert "_service.retry_due(" not in source
    assert "_transport." not in source
    assert ".ask(" not in source
    assert ".get(" not in source


def test_queued_reply_receipt_advances_on_background_retry_without_duplicate_io(
    tmp_path: Path,
) -> None:
    """A transient offline projection remains joinable after one native retry."""

    events = _Events()

    class ReplyClient:
        def __init__(self) -> None:
            self.online = False
            self.deliveries: list[str] = []

        def reply_online(self, _adapter: str) -> bool:
            return self.online

        def deliver_reply(self, _adapter: str, delivery: object) -> bool:
            self.deliveries.append(str(getattr(delivery, "delivery_id")))
            return True

    client = ReplyClient()
    database = tmp_path / "inbox.sqlite3"
    coordinator = DeliveryCustodyCoordinator(
        database,
        LarkReplyBridgeTransport(MemoryDeliveryTransport(), client),
        events,
        runtime=_runtime(),
    )
    facade = DeliveryCustodyFacade(coordinator, database)
    actor = "agent:e2e:node:squire"
    inbound_recipient = "legacy-squire"
    original = replace(
        _message("om-inbound"),
        sender="adapter:lark:e2e",
        recipient=inbound_recipient,
        intent="request",
    )
    assert facade.receive(original, now_ms=9).acknowledged is True
    reply = replace(
        _message("lark-queued-reply"),
        sender=actor,
        recipient="adapter:lark:e2e",
        intent="reply",
        idempotency_key="reply:om-inbound",
    )

    first = facade.submit(reply, now_ms=10)
    assert first.accepted is True and first.queued is True
    assert client.deliveries == []
    assert [
        item.message_id
        for item in facade.pending_messages(inbound_recipient, now_ms=10)
    ] == [original.message_id]
    client.online = True
    retry_at = 11
    assert facade.wake_outbox_recipient(reply.recipient, now_ms=retry_at)
    assert coordinator.submit(
        RetryDueCommand(
            correlation_id="retry-queued-reply",
            generation=coordinator.generation,
            version=coordinator.version,
            now_ms=retry_at,
        )
    ) is PortAdmission.ACCEPTED
    _wait_until(
        lambda: any(
            isinstance(item, RetryIoCompleted)
            and item.correlation_id == "retry-queued-reply"
            for item in events.snapshot()
        )
    )
    assert client.deliveries == [reply.message_id]
    assert coordinator.read_counts().outbox_count == 0
    assert facade.pending_messages(inbound_recipient, now_ms=10_001) == ()

    joined = facade.submit(reply, now_ms=10_001)
    assert joined.accepted is True and joined.queued is False
    assert client.deliveries == [reply.message_id]
    _close(coordinator, events)


def test_reply_timeout_joins_late_durable_receipt_without_resubmit(
    tmp_path: Path, monkeypatch,
) -> None:
    from hyprial.daemon.impl.inbox.actor.internal import projection as actor
    from hyprial.daemon.impl.inbox.authority import facade as authority

    events = _Events()
    entered = threading.Event()
    release = threading.Event()
    receipt_committed = threading.Event()
    publish_projection = threading.Event()
    timed_out: list[str] = []
    missing_reads: list[str] = []

    class ReplyClient:
        def __init__(self) -> None:
            self.deliveries: list[str] = []

        def reply_online(self, _adapter: str) -> bool:
            return True

        def deliver_reply(self, _adapter: str, delivery: object) -> bool:
            entered.set()
            assert release.wait(10.0), "test did not release the native receipt"
            self.deliveries.append(str(getattr(delivery, "delivery_id")))
            return True

    # Exercise a real expired reply wait only after I/O has started. Millisecond
    # sleeps also time out SQLite setup under CI load, before the intended seam.
    wait = actor._SystemReply.wait

    def expire_reply(reply, timeout):
        if reply.correlation_id == "inbox:reply-submit:late-receipt":
            assert entered.wait(5.0), "native reply never started"
            timed_out.append(reply.correlation_id)
            return wait(reply, 0.0)
        return wait(reply, timeout)

    monkeypatch.setattr(actor._SystemReply, "wait", expire_reply)
    # This test proves timeout recovery/no resubmit, not a wall-clock SLA.
    monkeypatch.setattr(authority, "_REPLY_COMPLETION_HANDOFF_GRACE_SECONDS", 5.0)
    # Hold the actor after its durable transaction, before the read snapshot
    # and SubmissionCompleted event. Timeout recovery is allowed to return
    # from that committed receipt without waiting for projection publication.
    apply_outcome = DeliveryCustody._apply_outcome

    def hold_projection(self, item, *args, **kwargs):
        result = apply_outcome(self, item, *args, **kwargs)
        if item.message.message_id == "late-receipt":
            receipt_committed.set()
            assert publish_projection.wait(10.0), "test did not release the projection"
        return result

    monkeypatch.setattr(DeliveryCustody, "_apply_outcome", hold_projection)
    client = ReplyClient()
    database = tmp_path / "inbox.sqlite3"
    coordinator = DeliveryCustodyCoordinator(
        database,
        LarkReplyBridgeTransport(MemoryDeliveryTransport(), client),
        events,
    )
    try:
        facade = DeliveryCustodyFacade(coordinator, database, timeout=5.0)
        read_result = facade._reads.submission_result

        def release_after_missing_read(correlation, message_id):
            result = read_result(correlation, message_id)
            if not release.is_set():
                assert result is None, "receipt settled before the timeout handoff"
                missing_reads.append(correlation)
                release.set()
            return result

        monkeypatch.setattr(facade._reads, "submission_result", release_after_missing_read)
        reply = replace(
            _message("late-receipt"),
            sender="agent:e2e:node:squire",
            recipient="adapter:lark:e2e",
            intent="reply",
            idempotency_key="reply:om-inbound",
        )
        result = facade.submit(reply, now_ms=10)

        assert timed_out == ["inbox:reply-submit:late-receipt"]
        assert missing_reads == timed_out
        assert result.accepted is True and result.queued is False
        assert client.deliveries == [reply.message_id]
        assert receipt_committed.wait(5.0), "durable receipt did not settle"
        assert facade._reads.outbox_count() == 0
        assert not any(
            isinstance(item, SubmissionCompleted)
            and item.correlation_id == timed_out[0]
            for item in events.snapshot()
        )
        publish_projection.set()
        # Preserve #552's projection wait: the facade may return the durable
        # receipt before the actor publishes its read snapshot. The controlled
        # hold above proves that boundary without relying on a timing race.
        # NONMONOTONIC-WAIT: outbox_count can rise again as new messages are submitted -- waiting for a count to reach zero is not monotone
        _wait_until(lambda: coordinator.read_counts().outbox_count == 0, timeout=5.0)
        assert coordinator.read_counts().outbox_count == 0
    finally:
        publish_projection.set()
        release.set()
        assert coordinator.drain(5.0).complete is True


def test_blocked_human_alarm_delivery_runs_off_the_state_actor(
    tmp_path: Path,
) -> None:
    events = _Events()
    alarm_entered = threading.Event()
    alarm_release = threading.Event()

    def deliver_human(_alarm: object, _text: str) -> bool:
        alarm_entered.set()
        assert alarm_release.wait(1.0)
        return True

    coordinator = DeliveryCustodyCoordinator(
        tmp_path / "inbox.sqlite3",
        MemoryDeliveryTransport(),
        events,
        runtime=_runtime(),
        service_options={"alarm_human_delivery": deliver_human},
    )
    outbound = replace(_message("expired"), sender="channel:lark:test-app")
    assert coordinator.submit(
        SubmitMessageCommand(
            "stage-expired",
            outbound,
            now_ms=0,
            defer_direct=True,
        )
    ) is PortAdmission.ACCEPTED
    _wait_until(
        lambda: any(
            isinstance(item, SubmissionCompleted)
            and item.correlation_id == "stage-expired"
            for item in events.snapshot()
        )
    )
    assert coordinator.submit(
        RetryDueCommand(
            correlation_id="expire",
            generation=coordinator.generation,
            version=coordinator.version,
            now_ms=DEFAULT_HOLD_TTL_MS,
        )
    ) is PortAdmission.ACCEPTED
    assert alarm_entered.wait(1.0)

    inbound = _message("while-alarm-blocks")
    assert coordinator.submit(ReceiveMessageCommand("state-still-free", inbound)) is (
        PortAdmission.ACCEPTED
    )
    _wait_until(
        lambda: any(
            isinstance(item, ReceiveCompleted)
            and item.correlation_id == "state-still-free"
            for item in events.snapshot()
        )
    )
    alarm_release.set()
    _wait_until(
        lambda: any(
            isinstance(item, DispatchIoCompleted)
            and item.correlation_id.startswith("alarm:expired:TTL_EXPIRED:")
            for item in events.snapshot()
        )
    )
    _close(coordinator, events)


def test_stale_retry_fence_is_correlated_and_does_not_mutate(tmp_path: Path) -> None:
    events = _Events()
    coordinator = DeliveryCustodyCoordinator(
        tmp_path / "inbox.sqlite3",
        MemoryDeliveryTransport(),
        events,
        runtime=_runtime(),
    )
    before = coordinator.read_counts()
    assert coordinator.submit(
        RetryDueCommand(
            correlation_id="stale-retry",
            generation=coordinator.generation + 1,
            version=coordinator.version,
            now_ms=10,
        )
    ) is PortAdmission.ACCEPTED
    _wait_until(
        lambda: any(
            isinstance(item, PortCommandRejected)
            and item.correlation_id == "stale-retry"
            for item in events.snapshot()
        )
    )
    rejection = next(
        item
        for item in events.snapshot()
        if isinstance(item, PortCommandRejected)
        and item.correlation_id == "stale-retry"
    )
    assert rejection.code == "STALE_COMMAND"
    assert coordinator.read_counts() == before
    _close(coordinator, events)


def test_actor_path_preserves_legacy_sqlite_schema(tmp_path: Path) -> None:
    legacy_path = tmp_path / "legacy.sqlite3"
    actor_path = tmp_path / "actor.sqlite3"
    legacy = InboxService(legacy_path, MemoryDeliveryTransport())
    legacy.close()
    events = _Events()
    coordinator = DeliveryCustodyCoordinator(
        actor_path, MemoryDeliveryTransport(), events, runtime=_runtime()
    )
    _close(coordinator, events)

    def schema(path: Path) -> tuple[tuple[str, str, str], ...]:
        connection = sqlite3.connect(path)
        try:
            return tuple(
                connection.execute(
                    "SELECT type, name, sql FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
                ).fetchall()
            )
        finally:
            connection.close()

    assert schema(actor_path) == schema(legacy_path)
