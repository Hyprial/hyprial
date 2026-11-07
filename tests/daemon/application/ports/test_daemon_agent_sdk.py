from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.daemon.harnesses.agent_sdk.driver.support import (
    ClientFactory as StreamingClientFactory,
    FakeClient as StreamingFakeClient,
    process as streaming_process,
    wait_for_results as wait_for_streaming_results,
)

from hyprial.daemon.impl import (
    DaemonEventBridge,
    DesiredStateStore,
    HarnessDelivery,
    HarnessResult,
    HarnessResultStatus,
    ManagedHarnessRuntime,
)
from hyprial.daemon.impl.api import delivery_prompt
from hyprial.kernel import canonical_agent_uri
from hyprial.daemon.impl.inbox import (
    AckResult,
    DeliveryLifecycle,
    InboxMessage,
    SubmissionResult,
)
from hyprial.daemon.impl.inbox.batch import (
    BatchBounds,
    BatchRow,
    BatchSelection,
)

# The pump queries the inbox by the exact canonical URI the registrar
# advertises for this connector; the short name never reaches the wire.
WORKER_URI = canonical_agent_uri("op", "node-1", "claude-worker")
BATCH_TEXT = "[Harness Network inbox batch cursor=runtime]\nBYTE-EXACT-runtime-秘密"


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HYPRIAL_HOME", str(home / ".hyprial"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "harness-state"))
    monkeypatch.setenv("HARNESS_SOCKET_PATH", str(tmp_path / "isolated.sock"))
    monkeypatch.setenv("HYPRIAL_NETWORK_ISOLATED", "1")


@dataclass(frozen=True)
class _RenderedBatchDelivery:
    """Future batch envelope accepted by the real streaming process boundary."""

    delivery_id: str
    conversation_id: str
    sender: str
    recipient: str
    message: str
    batch_cursor: str
    message_ids: tuple[str, ...]
    origin: object | None = None
    hook_text: str | None = None
    hook_request: bool = False
    notice: bool = False


class FakeTransport:
    def close(self) -> None:
        pass


class _TurnObserver:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.completed: list[str] = []

    def turn_started(self, delivery, *, started_at_ms: int) -> None:  # type: ignore[no-untyped-def]
        del started_at_ms
        self.started.append(delivery.delivery_id)

    def __call__(self, delivery, result, **kwargs) -> None:  # type: ignore[no-untyped-def]
        del result, kwargs
        self.completed.append(delivery.delivery_id)


class StreamingProcess:
    def __init__(self) -> None:
        self.running = True
        self.pid: int | None = None
        self.deliveries: list[HarnessDelivery] = []
        self.results: list[HarnessResult] = []
        self.known: set[str] = set()

    def enqueue(self, delivery: HarnessDelivery) -> bool:
        if delivery.delivery_id in self.known:
            return False
        self.known.add(delivery.delivery_id)
        self.deliveries.append(delivery)
        return True

    def drain_results(self) -> tuple[HarnessResult, ...]:
        results = tuple(self.results)
        self.results.clear()
        self.known.difference_update(item.delivery_id for item in results)
        return results

    def interrupt(self, delivery_id: str, *, timeout: float = 1.0) -> bool:
        return delivery_id in self.known

    def stop(self) -> None:
        self.running = False


class Launcher:
    def __init__(self) -> None:
        self.process = StreamingProcess()

    def start(self, spec):  # type: ignore[no-untyped-def]
        return self.process


class FakeInbox:
    def __init__(self, *messages: InboxMessage) -> None:
        self.messages = {message.message_id: message for message in messages}
        self.submitted: list[InboxMessage] = []
        self.acks: list[tuple[str, str]] = []

    def batch_cursor_key(self) -> bytes:
        return b"runtime-origin-test-key"

    def batch_snapshot(self, recipient: str, **kwargs: object) -> BatchSelection:
        only = kwargs.get("only")
        allowed = set(only) if isinstance(only, tuple) else None
        members = tuple(
            item
            for item in self.messages.values()
            if item.recipient == recipient
            and (allowed is None or item.message_id in allowed)
        )
        rows = []
        for index, item in enumerate(members, start=1):
            body = json.loads(item.payload)
            text = body.get("message", "") if isinstance(body, dict) else ""
            rows.append(
                BatchRow(
                    source="inbox",
                    message=item,
                    ordinal=index,
                    body_text=text if isinstance(text, str) else "",
                    body_bytes=(len(text.encode()) if isinstance(text, str) else 0),
                )
            )
        return BatchSelection(
            actor=recipient,
            bounds=BatchBounds(
                inbox_high=len(rows),
                notice_high=0,
                capture_ordinal=len(rows),
            ),
            rows=tuple(rows),
        )

    def pending_messages(self, recipient: str) -> tuple[InboxMessage, ...]:
        return tuple(
            item for item in self.messages.values() if item.recipient == recipient
        )

    def prune_inbox(self):  # type: ignore[no-untyped-def]
        return []

    def submit(self, message: InboxMessage, *, now_ms=None) -> SubmissionResult:  # type: ignore[no-untyped-def]
        self.submitted.append(message)
        return SubmissionResult(message.message_id, accepted=True)

    def ack(self, recipient: str, message_id: str) -> AckResult:
        self.acks.append((recipient, message_id))
        self.messages.pop(message_id, None)
        return AckResult(message_id, acknowledged=True)

    def retry_due(self, *, now_ms=None):  # type: ignore[no-untyped-def]
        return []

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

    def close(self) -> None:
        pass


def write_desired_state(path: Path) -> None:
    Path(f"{path}.v1").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "asMailbox": False,
                "providers": [
                    {
                        "provider": "claude",
                        "name": "claude-worker",
                        "headless": True,
                        "args": [],
                        "ownership": "managed",
                    }
                ],
                "interactiveSessions": [],
            }
        ),
        encoding="utf-8",
    )


def message(
    *, message_id: str = "delivery-1", intent: str = "request"
) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="conversation-1",
        sender="requester",
        recipient=WORKER_URI,
        payload=b'{"message":"do the work"}',
        intent=intent,
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )


def runtime(tmp_path: Path, inbox: FakeInbox, launcher: Launcher) -> DaemonEventBridge:
    desired = tmp_path / "desired-state.json"
    write_desired_state(desired)
    desired_state = DesiredStateStore(desired)
    harnesses = ManagedHarnessRuntime(launcher)
    harnesses.restore(desired_state.load().harnesses)
    value = DaemonEventBridge(
        state_dir=tmp_path,
        node_id="node-1",
        desired_state=desired_state,
        transport=FakeTransport(),
        inbox=inbox,
        harnesses=harnesses,
        harness_actor_uri=lambda name: canonical_agent_uri("op", "node-1", name),
    )
    value.start()
    return value


def test_batch_delivery_and_result_reject_a_non_member_identity() -> None:
    with pytest.raises(ValueError, match="member message ids"):
        HarnessDelivery(
            delivery_id="synthetic-wrapper",
            conversation_id="conversation-batch",
            sender="requester",
            recipient=WORKER_URI,
            message=BATCH_TEXT,
            batch_cursor="cursor-runtime",
            message_ids=("delivery-1", "delivery-2"),
        )
    with pytest.raises(ValueError, match="member message ids"):
        HarnessResult(
            delivery_id="synthetic-wrapper",
            recipient=WORKER_URI,
            status=HarnessResultStatus.COMPLETED,
            message_ids=("delivery-1", "delivery-2"),
        )


def test_real_streaming_client_query_gets_batch_text_without_scalar_wrapping() -> None:
    client = StreamingFakeClient(["batch complete"])
    observer = _TurnObserver()
    process = streaming_process(
        StreamingClientFactory([client]), on_turn_completed=observer
    )
    batch = _RenderedBatchDelivery(
        delivery_id="delivery-1",
        conversation_id="conversation-batch",
        sender="requester",
        recipient=WORKER_URI,
        message=BATCH_TEXT,
        batch_cursor="cursor-runtime",
        message_ids=("delivery-1", "delivery-2"),
    )
    try:
        assert process.enqueue(batch)  # type: ignore[arg-type]
        [result] = wait_for_streaming_results(process, 1)

        assert result.status is HarnessResultStatus.COMPLETED
        assert result.delivery_id == "delivery-1"
        assert result.message_ids == ("delivery-1", "delivery-2")
        assert observer.started == ["delivery-1", "delivery-2"]
        assert observer.completed == ["delivery-1", "delivery-2"]
        assert client.queries == [BATCH_TEXT]
        assert client.queries[0].startswith("[Harness Network inbox batch")
        assert "message from requester" not in client.queries[0]
    finally:
        process.stop()


def test_real_streaming_client_query_preserves_hook_text_protocol() -> None:
    client = StreamingFakeClient(["hook complete"])
    process = streaming_process(StreamingClientFactory([client]))
    hook = HarnessDelivery(
        delivery_id="hook-1",
        conversation_id="conversation-hook",
        sender="mechanism",
        recipient=WORKER_URI,
        message='{"request":"raw hook protocol"}',
        hook_text="HOOK-TEXT-SENTINEL",
        hook_request=True,
    )
    expected = (
        f"[Harness Network message from mechanism to {WORKER_URI}]\n"
        '{"request":"raw hook protocol"}\n\n'
        "<<<HYPRIAL-HOOK>>>\n"
        "HOOK-TEXT-SENTINEL\n"
        "<<<END-HYPRIAL-HOOK>>>"
    )
    try:
        assert process.enqueue(hook)
        [result] = wait_for_streaming_results(process, 1)

        assert result.status is HarnessResultStatus.COMPLETED
        assert delivery_prompt(hook) == expected
        assert client.queries == [expected]
    finally:
        process.stop()


def test_daemon_managed_model_edge_dispatches_one_canonical_batch(
    tmp_path: Path,
) -> None:
    inbox = FakeInbox(
        message(message_id="delivery-1"),
        message(message_id="delivery-2"),
    )
    launcher = Launcher()
    daemon = runtime(tmp_path, inbox, launcher)
    try:
        outcome = daemon.on_timer()

        assert outcome.harness_deliveries == 1
        assert len(launcher.process.deliveries) == 1
        delivery = launcher.process.deliveries[0]
        assert delivery.message.startswith("[Harness Network inbox batch")
        assert delivery.message.count("do the work") == 2
        assert delivery.batch_cursor
        assert delivery.delivery_id == "delivery-1"
        assert delivery.message_ids == ("delivery-1", "delivery-2")
        assert delivery.delivery_id in delivery.message_ids
    finally:
        daemon.stop()


def test_daemon_managed_model_edge_preserves_verified_owner_origin(
    tmp_path: Path,
) -> None:
    verified = InboxMessage(
        message_id="verified-delivery",
        conversation_id="conversation-verified",
        sender="adapter:lark:test",
        recipient=WORKER_URI,
        payload=json.dumps(
            {
                "message": "do the verified work",
                "origin": {
                    "sender": {
                        "displayName": "Verified Owner",
                        "standing": "verified",
                        "owner": "user:owner",
                    }
                },
            }
        ).encode(),
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )
    inbox = FakeInbox(verified)
    launcher = Launcher()
    daemon = runtime(tmp_path, inbox, launcher)
    try:
        outcome = daemon.on_timer()

        assert outcome.harness_deliveries == 1
        [delivery] = launcher.process.deliveries
        assert delivery.delivery_id == "verified-delivery"
        assert delivery.message_ids == ("verified-delivery",)
        assert "origin: sender=Verified Owner (owner user:owner, verified)" in (
            delivery.message
        )
        assert "origin: (unrecorded; NOT verified)" not in delivery.message
    finally:
        daemon.stop()


def test_completed_batch_keeps_each_unsettled_member_without_reply_loop(
    tmp_path: Path,
) -> None:
    inbox = FakeInbox(
        message(message_id="delivery-1"),
        message(message_id="delivery-2"),
    )
    launcher = Launcher()
    daemon = runtime(tmp_path, inbox, launcher)
    try:
        daemon.on_timer()
        [delivery] = launcher.process.deliveries
        launcher.process.results.append(
            HarnessResult(
                delivery.delivery_id,
                WORKER_URI,
                HarnessResultStatus.COMPLETED,
                output="work completed",
                message_ids=delivery.message_ids,
            )
        )
        second = daemon.on_timer()

        # A model turn never treats final text as a reply. With no settlement
        # gate bound, every real member remains independently pending.
        assert second.harness_results == 2
        assert inbox.acks == []
        assert inbox.submitted == []
        # The real loop guard is the daemon's one-way fetched stamp (proven in
        # tests/daemon/application/transport/channel/summary/batch); this
        # double answers every read, so it is not asserted here.
        assert {item.message_id for item in inbox.pending_messages(WORKER_URI)} == {
            "delivery-1",
            "delivery-2",
        }
    finally:
        daemon.stop()


def test_interrupted_result_does_not_reply_or_ack_and_is_redelivered(
    tmp_path: Path,
) -> None:
    inbox = FakeInbox(message())
    launcher = Launcher()
    daemon = runtime(tmp_path, inbox, launcher)
    try:
        daemon.on_timer()
        launcher.process.results.append(
            HarnessResult(
                "delivery-1",
                WORKER_URI,
                HarnessResultStatus.INTERRUPTED,
                error="interrupted",
            )
        )

        outcome = daemon.on_timer()

        assert outcome.harness_results == 0
        assert outcome.harness_deliveries == 1
        assert inbox.submitted == []
        assert inbox.acks == []
        delivery_ids = [item.delivery_id for item in launcher.process.deliveries]
        assert delivery_ids == ["delivery-1", "delivery-1"]
    finally:
        daemon.stop()


def test_completed_reply_delivery_is_consumed_without_reply_loop(
    tmp_path: Path,
) -> None:
    inbox = FakeInbox(message(intent="reply"))
    launcher = Launcher()
    daemon = runtime(tmp_path, inbox, launcher)
    try:
        daemon.on_timer()
        launcher.process.results.append(
            HarnessResult(
                "delivery-1",
                WORKER_URI,
                HarnessResultStatus.COMPLETED,
                output="not sent back",
            )
        )

        outcome = daemon.on_timer()

        assert outcome.harness_results == 1
        assert inbox.submitted == []
        assert inbox.acks == [(WORKER_URI, "delivery-1")]
    finally:
        daemon.stop()
