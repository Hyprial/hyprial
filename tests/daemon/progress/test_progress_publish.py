"""Daemon orchestration for route-C progress events (T7)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from hyprial.daemon.impl.api import HarnessResult, HarnessResultStatus
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.runtime import DaemonEventBridge, _coalesce_progress_events
from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage, InboxService, MemoryDeliveryTransport
from hyprial.daemon.impl.inbox.tracking.progress import decode_progress_event, ProgressEvent

OWNER = "op"
NODE = "node-1"
WORKER = f"agent:{OWNER}:{NODE}:worker"
SENDER = f"agent:{OWNER}:{NODE}:requester"


def _event(
    seq: int,
    phase: str,
    *,
    delivery_id: str = "delivery-1",
    dropped: int = 0,
) -> ProgressEvent:
    return ProgressEvent(
        delivery_id=delivery_id,
        conversation_id="conversation-1",
        actor=WORKER,
        harness="pi",
        phase=phase,
        seq=seq,
        emitted_at_ms=1_000 + seq,
        summary=f"{phase} {seq}",
        dropped_since_seq=dropped,
    )


def _request(delivery_id: str = "delivery-1") -> InboxMessage:
    return InboxMessage(
        message_id=delivery_id,
        conversation_id="conversation-1",
        sender=SENDER,
        recipient=WORKER,
        payload=json.dumps({"message": "do the work"}).encode(),
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )


def test_submit_progress_event_stores_a_local_sender_without_touching_fifo(
    tmp_path: Path,
) -> None:
    service = InboxService(
        tmp_path / "inbox.sqlite3", MemoryDeliveryTransport(), node_id=NODE
    )
    try:
        assert service.submit_progress_event(_event(0, "tool-call"), recipient=SENDER)

        (stored,) = service.list_progress_events(SENDER)
        assert stored.intent == "progress"
        assert stored.sender == WORKER
        assert stored.recipient == SENDER
        decoded = decode_progress_event(stored.payload)
        assert decoded is not None
        assert decoded.summary == "tool-call 0"
        assert service.pending_messages(SENDER) == ()
        assert service.system_notices(SENDER) == ()
    finally:
        service.close()


def test_submit_progress_event_uses_the_remote_sender_node(tmp_path: Path) -> None:
    transport = MemoryDeliveryTransport()
    service = InboxService(
        tmp_path / "inbox.sqlite3", transport, node_id=NODE
    )
    remote_sender = f"agent:{OWNER}:node-2:requester"
    try:
        assert service.submit_progress_event(_event(0, "thinking"), recipient=remote_sender)

        [(node, message)] = transport.progress_events
        assert node == "node-2"
        assert message.recipient == remote_sender
        assert message.intent == "progress"
        assert service.list_progress_events(remote_sender) == ()
    finally:
        service.close()


def test_coalesce_keeps_latest_tool_call_tool_result_and_other() -> None:
    coalesced = _coalesce_progress_events(
        (
            _event(0, "tool-call"),
            _event(1, "thinking"),
            _event(2, "tool-result"),
            _event(3, "thinking"),
            _event(4, "tool-call"),
        )
    )

    assert [(event.phase, event.seq) for event in coalesced] == [
        ("tool-result", 2),
        ("thinking", 3),
        ("tool-call", 4),
    ]
    # The first retained event carries the two merged events' accounting.
    assert [event.dropped_since_seq for event in coalesced] == [2, 0, 0]


def test_coalesce_carries_forward_drop_accounting_from_merged_events() -> None:
    coalesced = _coalesce_progress_events(
        (
            _event(0, "thinking", dropped=3),
            _event(1, "thinking"),
        )
    )

    assert [(event.seq, event.dropped_since_seq) for event in coalesced] == [(1, 4)]


class _Transport:
    def close(self) -> None:
        return None


class _Harnesses:
    def __init__(self, events: tuple[ProgressEvent, ...]) -> None:
        self.events = events

    def restore(self, _specs: tuple[object, ...]):  # type: ignore[no-untyped-def]
        return SimpleNamespace(attempted=0, restored=0, failed=0)

    def reconcile(self) -> int:
        return 0

    def streaming_actors(self) -> tuple[str, ...]:
        return ()

    def drain_progress(self) -> tuple[ProgressEvent, ...]:
        events, self.events = self.events, ()
        return events

    def drain_results(self) -> tuple[HarnessResult, ...]:
        return ()

    def reconcile_session_refs(self) -> dict[str, str]:
        return {}

    def stop(self) -> None:
        return None


class _Inbox:
    def __init__(self, original: InboxMessage | None) -> None:
        self.original = original
        self.progress: list[tuple[ProgressEvent, str]] = []

    def pending_messages(self, actor: str) -> tuple[InboxMessage, ...]:
        if self.original is not None and actor == self.original.recipient:
            return (self.original,)
        return ()

    def submit_progress_event(self, event: ProgressEvent, *, recipient: str) -> bool:
        self.progress.append((event, recipient))
        return True

    def fetch_pending(
        self,
        _recipient: str,
        *,
        only=None,  # type: ignore[no-untyped-def]
        completed_turn: bool = False,
    ) -> tuple[InboxMessage, ...]:
        # WP5-A settle path: stamping/clearing lives in the durable service;
        # this double only answers the read.
        del only, completed_turn
        return ()

    def ack(self, _recipient: str, message_id: str):  # type: ignore[no-untyped-def]
        return SimpleNamespace(message_id=message_id, acknowledged=True)

    def retry_due(self) -> list[object]:
        return []

    def prune_inbox(self) -> tuple[object, ...]:
        return ()

    def close(self) -> None:
        return None


def _runtime(tmp_path: Path, inbox: _Inbox, events: tuple[ProgressEvent, ...]) -> DaemonEventBridge:
    return DaemonEventBridge(
        state_dir=tmp_path / "state",
        node_id=NODE,
        desired_state=DesiredStateStore(tmp_path / "state" / "desired-state.json"),
        transport=_Transport(),  # type: ignore[arg-type]
        inbox=inbox,  # type: ignore[arg-type]
        harnesses=_Harnesses(events),  # type: ignore[arg-type]
    )


def test_runtime_publishes_coalesced_progress_to_the_original_sender(
    tmp_path: Path,
) -> None:
    inbox = _Inbox(_request())
    runtime = _runtime(
        tmp_path,
        inbox,
        (
            _event(0, "tool-call"),
            _event(1, "thinking"),
            _event(2, "tool-result"),
            _event(3, "thinking"),
        ),
    )
    runtime.start()
    try:
        summary = runtime.on_timer()

        assert summary.harness_progress == 3
        assert [(event.phase, event.seq) for event, _ in inbox.progress] == [
            ("tool-call", 0),
            ("tool-result", 2),
            ("thinking", 3),
        ]
        assert [recipient for _, recipient in inbox.progress] == [SENDER] * 3
        # The merged earlier thinking event is charged to the first survivor.
        assert sum(event.dropped_since_seq for event, _ in inbox.progress) == 1
    finally:
        runtime.stop()


def test_runtime_drops_progress_whose_original_request_is_gone(tmp_path: Path) -> None:
    inbox = _Inbox(None)
    runtime = _runtime(tmp_path, inbox, (_event(0, "thinking"),))
    runtime.start()
    try:
        summary = runtime.on_timer()

        assert summary.harness_progress == 0
        assert inbox.progress == []
    finally:
        runtime.stop()


def test_terminal_result_still_uses_the_authoritative_reply_path(
    tmp_path: Path,
) -> None:
    """Progress publication must not consume or replace the request's reply."""

    class ResultHarness(_Harnesses):
        def drain_results(self) -> tuple[HarnessResult, ...]:
            return (
                HarnessResult(
                    "delivery-1",
                    WORKER,
                    HarnessResultStatus.COMPLETED,
                    output="done",
                ),
            )

    class ResultInbox(_Inbox):
        def __init__(self) -> None:
            super().__init__(_request())
            self.submitted: list[InboxMessage] = []
            self.acked: list[tuple[str, str]] = []

        def submit(self, message: InboxMessage):  # type: ignore[no-untyped-def]
            self.submitted.append(message)
            return SimpleNamespace(message_id=message.message_id, accepted=True)

        def ack(self, recipient: str, message_id: str):  # type: ignore[no-untyped-def]
            self.acked.append((recipient, message_id))
            return SimpleNamespace(message_id=message_id, acknowledged=True)

    inbox = ResultInbox()
    runtime = DaemonEventBridge(
        state_dir=tmp_path / "state",
        node_id=NODE,
        desired_state=DesiredStateStore(tmp_path / "state" / "desired-state.json"),
        transport=_Transport(),  # type: ignore[arg-type]
        inbox=inbox,  # type: ignore[arg-type]
        harnesses=ResultHarness((_event(0, "turn-end"),)),  # type: ignore[arg-type]
    )
    runtime.start()
    try:
        summary = runtime.on_timer()

        assert summary.harness_progress == 1
        assert summary.harness_results == 1
        assert [event.phase for event, _ in inbox.progress] == ["turn-end"]
        # WP5-A: the terminal result settles per hint (keep, no gate) and
        # never becomes a reply; progress publication must not consume or
        # replace the request either.
        assert inbox.submitted == []
        assert inbox.acked == []
    finally:
        runtime.stop()
