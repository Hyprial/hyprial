from __future__ import annotations

import threading
import time
from pathlib import Path
import sqlite3

from tests.support.runtime.waiting_support import Budget, wait_until

import pytest

from hyprial.kernel.impl.actor_runtime.contracts import AdmissionResult
from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.daemon.impl.inbox.actor.coordinator import DeliveryCustodyCoordinator
from hyprial.daemon.impl.inbox.memory import MemoryDeliveryTransport
from hyprial.daemon.impl.inbox.contracts.api import DeliveryLifecycle, InboxMessage
from hyprial.daemon.impl.inbox.contracts.ports import ReceiveMessageCommand, SubmissionCompleted
from hyprial.daemon.impl.inbox.links.io import (
    CorrelatedInboxEventRouter,
    InboxDeliveryIoAdapter as WorkflowInboxIoAdapter,
    InboxIoDeferred as WorkflowIoDeferred,
)


SENDER = "agent:bobadmin:node-a:workflow"
TARGET = "agent:bobadmin:node-a:worker"
EFFECT = "wf-io-stable-effect"


def _coordinator(
    tmp_path: Path,
    transport: MemoryDeliveryTransport,
    events: object,
) -> DeliveryCustodyCoordinator:
    return DeliveryCustodyCoordinator(
        tmp_path / "inbox.sqlite3",
        transport,
        events,  # type: ignore[arg-type]
        mailbox_capacity=8,
        io_capacity=2,
    )


def _adapter(
    coordinator: object,
    router: CorrelatedInboxEventRouter,
    *,
    timeout: float = 1.0,
) -> WorkflowInboxIoAdapter:
    return WorkflowInboxIoAdapter(
        coordinator,  # type: ignore[arg-type]
        router,
        resolve_target=lambda target: target,
        clock_ms=lambda: 1_000,
        completion_timeout=timeout,
    )


def _deliver(adapter: WorkflowInboxIoAdapter) -> str:
    # These tests are about idempotent *message ids*; deliver() now also
    # reports the resolved recipient, so unwrap it here rather than changing
    # what every caller asserts.
    return adapter.deliver(
        effect_id=EFFECT,
        sender=SENDER,
        target=TARGET,
        conversation_id="wf-run-target",
        text="perform the check",
        summary="perform the check",
    ).message_id


def test_same_effect_id_survives_actor_and_facade_rebuild_without_transport_replay(
    tmp_path: Path,
) -> None:
    router = CorrelatedInboxEventRouter()
    transport = MemoryDeliveryTransport()
    message_id = WorkflowInboxIoAdapter.message_id(EFFECT)
    transport.set_online(TARGET, True)
    transport.confirm_direct(message_id)
    coordinator = _coordinator(tmp_path, transport, router)
    first = _adapter(coordinator, router)
    assert _deliver(first) == message_id

    generation = coordinator.generation
    assert coordinator._runtime.tell(coordinator.handle, object()) is AdmissionResult.ACCEPTED
    deadline = time.monotonic() + 2.0
    while coordinator.generation == generation:
        assert time.monotonic() < deadline
        time.sleep(0.01)

    rebuilt_facade = _adapter(coordinator, router)
    assert _deliver(rebuilt_facade) == message_id
    assert transport.delivered == [message_id]
    assert coordinator.read_counts().outbox_count == 0
    assert coordinator.drain(timeout=2.0).complete


def test_overload_retries_same_effect_without_duplicate_transport(tmp_path: Path) -> None:
    router = CorrelatedInboxEventRouter()
    transport = MemoryDeliveryTransport()
    message_id = WorkflowInboxIoAdapter.message_id(EFFECT)
    transport.set_online(TARGET, True)
    transport.confirm_direct(message_id)
    coordinator = _coordinator(tmp_path, transport, router)

    class OnceOverloaded:
        def __init__(self) -> None:
            self.calls = 0

        def submit(self, command: object) -> PortAdmission:
            self.calls += 1
            if self.calls == 1:
                return PortAdmission.OVERLOADED
            return coordinator.submit(command)  # type: ignore[arg-type]

    sink = OnceOverloaded()
    adapter = _adapter(sink, router)
    with pytest.raises(WorkflowIoDeferred):
        _deliver(adapter)
    assert _deliver(adapter) == message_id
    assert sink.calls == 2
    assert transport.delivered == [message_id]
    assert coordinator.drain(timeout=2.0).complete


def test_completion_timeout_keeps_claim_until_late_terminal_event(tmp_path: Path) -> None:
    router = CorrelatedInboxEventRouter()
    held: list[SubmissionCompleted] = []
    ready = threading.Event()

    class DelayedEvents:
        def publish(self, event: object) -> None:
            if isinstance(event, SubmissionCompleted):
                held.append(event)
                ready.set()
                return
            router.publish(event)

    transport = MemoryDeliveryTransport()
    message_id = WorkflowInboxIoAdapter.message_id(EFFECT)
    transport.set_online(TARGET, True)
    transport.confirm_direct(message_id)
    coordinator = _coordinator(tmp_path, transport, DelayedEvents())
    adapter = _adapter(coordinator, router, timeout=0.02)

    with pytest.raises(WorkflowIoDeferred):
        _deliver(adapter)
    assert ready.wait(1.0)
    assert transport.delivered == [message_id]
    router.publish(held[0])

    assert _deliver(_adapter(coordinator, router)) == message_id
    assert transport.delivered == [message_id]
    assert coordinator.drain(timeout=2.0).complete


def test_process_rebuild_replays_durable_receipt_after_delivery_before_workflow_completion(
    tmp_path: Path,
) -> None:
    first_router = CorrelatedInboxEventRouter()
    held: list[SubmissionCompleted] = []
    ready = threading.Event()

    class DropWorkflowCompletion:
        def publish(self, event: object) -> None:
            if isinstance(event, SubmissionCompleted):
                held.append(event)
                ready.set()
                return
            first_router.publish(event)

    transport = MemoryDeliveryTransport()
    message_id = WorkflowInboxIoAdapter.message_id(EFFECT)
    transport.set_online(TARGET, True)
    transport.confirm_direct(message_id)
    first = _coordinator(tmp_path, transport, DropWorkflowCompletion())
    with pytest.raises(WorkflowIoDeferred):
        _deliver(_adapter(first, first_router, timeout=0.02))
    assert ready.wait(1.0)
    assert len(held) == 1
    assert transport.delivered == [message_id]
    assert first.drain(timeout=2.0).complete
    connection = sqlite3.connect(tmp_path / "inbox.sqlite3")
    try:
        receipt = connection.execute(
            "SELECT correlation_id, message_id, accepted, queued "
            "FROM actor_submission_receipts WHERE correlation_id = ?",
            (EFFECT,),
        ).fetchone()
    finally:
        connection.close()
    assert receipt == (EFFECT, message_id, 1, 0)

    # New process objects: no in-memory event cache or actor generation is
    # shared.  The same inbox database is the only continuity boundary.
    second_router = CorrelatedInboxEventRouter()
    second = _coordinator(tmp_path, transport, second_router)
    assert _deliver(_adapter(second, second_router)) == message_id
    replay = second_router.wait(EFFECT, 0.0)
    assert isinstance(replay, SubmissionCompleted)
    assert replay.generation == second.generation
    assert replay.result.message_id == message_id
    assert transport.delivered == [message_id]
    assert second.read_counts().outbox_count == 0
    assert second.drain(timeout=2.0).complete


def test_process_rebuild_reverse_mutation_without_d22_receipt_replays_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_router = CorrelatedInboxEventRouter()
    transport = MemoryDeliveryTransport()
    message_id = WorkflowInboxIoAdapter.message_id(EFFECT)
    transport.set_online(TARGET, True)
    transport.confirm_direct(message_id)
    first = _coordinator(tmp_path, transport, first_router)
    assert _deliver(_adapter(first, first_router)) == message_id
    assert first.drain(timeout=2.0).complete

    # Reverse mutation: deleting the receipt read recreates the original
    # process-rebuild bug and proves this test is sensitive to the D22 seam.
    from hyprial.daemon.impl.inbox.actor.custody import DeliveryCustody

    monkeypatch.setattr(
        DeliveryCustody,
        "_read_submission_receipt",
        lambda _self, _command: None,
    )
    second_router = CorrelatedInboxEventRouter()
    second = _coordinator(tmp_path, transport, second_router)
    assert _deliver(_adapter(second, second_router)) == message_id
    assert transport.delivered == [message_id, message_id]
    assert second.drain(timeout=2.0).complete


def test_acknowledgement_uses_correlated_typed_inbox_command(tmp_path: Path) -> None:
    router = CorrelatedInboxEventRouter()
    coordinator = _coordinator(tmp_path, MemoryDeliveryTransport(), router)
    message = InboxMessage(
        message_id="worker-reply-1",
        conversation_id="wf-run-target",
        sender=TARGET,
        recipient=SENDER,
        payload=b'{"message":"done"}',
        intent="reply",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1_000,
    )
    assert coordinator.submit(ReceiveMessageCommand("seed-reply", message)) is PortAdmission.ACCEPTED
    # The old shape hand-copied the product's settle bound as a bare 2.0 and
    # polled sleep(0.01) against it, so the verdict landed on the budget edge
    # (CI task 17377: 2.0s overshot by 21.1ms). The bound is the facade's own
    # synchronous call timeout -- the product constructs it with this default
    # and never overrides it -- so derive it from the named constant instead.
    wait_until(
        lambda: bool(coordinator.read_pending(SENDER)),
        budget=Budget.from_product(
            "hyprial.daemon.impl.inbox.authority.facade.DELIVERY_CUSTODY_CALL_TIMEOUT_SECONDS"
        ),
        proves=(
            "an accepted ReceiveMessageCommand lands in the "
            "recipient's pending inbox"
        ),
    )

    adapter = _adapter(coordinator, router)
    assert adapter.acknowledge(
        effect_id="wf-ack-effect-1",
        recipient=SENDER,
        message_id=message.message_id,
    )
    assert coordinator.read_pending(SENDER) == ()
    assert coordinator.drain(timeout=2.0).complete
