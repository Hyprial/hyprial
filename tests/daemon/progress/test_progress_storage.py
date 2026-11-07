"""Storage-layer tests for progress events (route C, T4).

The progress_events table is independent from system_notices by design: the
notice dispatch path cannot read it, and it carries the bounds the notice
table lacks -- TTL, per-delivery cap, per-recipient cap.
"""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage, InboxService, MemoryDeliveryTransport
from hyprial.daemon.impl.inbox.tracking.progress import encode_progress_event, ProgressEvent
from hyprial.daemon.impl.inbox.service.delivery.outbox import (
    PROGRESS_EVENT_PER_DELIVERY_LIMIT,
    PROGRESS_EVENT_PER_RECIPIENT_LIMIT,
    PROGRESS_EVENT_TTL_MS,
)

RECIPIENT = "agent:op:node-1:requester"
ACTOR = "agent:op:node-1:worker"


def _message(
    delivery_id: str,
    seq: int,
    *,
    intent: str = "progress",
    created_at_ms: int = 1_000,
    summary: str | None = None,
) -> InboxMessage:
    event = ProgressEvent(
        delivery_id=delivery_id,
        conversation_id="conversation-1",
        actor=ACTOR,
        harness="pi",
        phase="tool-call",
        seq=seq,
        emitted_at_ms=created_at_ms,
        summary=summary or f"step {seq} of {delivery_id}",
    )
    return InboxMessage(
        message_id=f"progress:{delivery_id}:{seq}",
        conversation_id="conversation-1",
        sender=ACTOR,
        recipient=RECIPIENT,
        payload=encode_progress_event(event),
        intent=intent,
        lifecycle=DeliveryLifecycle.ONLINE_ONLY,
        created_at_ms=created_at_ms,
    )


@pytest.fixture
def service(tmp_path):
    inbox = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())
    yield inbox
    inbox.close()


def test_receive_and_list_round_trip_is_non_destructive(service) -> None:
    assert service.receive_progress_event(_message("d-1", 0))
    assert service.receive_progress_event(_message("d-1", 1))

    first = service.list_progress_events(RECIPIENT)
    second = service.list_progress_events(RECIPIENT)

    assert [item.message_id for item in first] == [
        "progress:d-1:0",
        "progress:d-1:1",
    ]
    # Non-destructive: a read never trims; cleanup is TTL/cap driven.
    assert [item.message_id for item in second] == [
        "progress:d-1:0",
        "progress:d-1:1",
    ]
    # And the inbox FIFO is untouched: progress is not a pending message.
    assert service.pending_messages(RECIPIENT) == ()


def test_list_filters_by_delivery(service) -> None:
    service.receive_progress_event(_message("d-1", 0))
    service.receive_progress_event(_message("d-2", 0))

    assert [item.message_id for item in service.list_progress_events(RECIPIENT, delivery_id="d-2")] == [
        "progress:d-2:0"
    ]


def test_receive_guards_intent_and_payload(service) -> None:
    # A notice or a normal message must never land in this table.
    assert not service.receive_progress_event(_message("d-1", 0, intent="system"))
    assert not service.receive_progress_event(_message("d-1", 0, intent="request"))
    # A payload that fails contract validation is not ours either.
    bad = _message("d-1", 1)
    bad = bad.with_payload(b'{"message":"impostor"}')
    assert not service.receive_progress_event(bad)

    assert service.list_progress_events(RECIPIENT) == ()


def test_redelivery_of_the_same_event_is_idempotent(service) -> None:
    assert service.receive_progress_event(_message("d-1", 0))
    assert service.receive_progress_event(_message("d-1", 0))

    assert len(service.list_progress_events(RECIPIENT)) == 1


def test_per_delivery_cap_keeps_the_newest(service) -> None:
    for seq in range(PROGRESS_EVENT_PER_DELIVERY_LIMIT + 10):
        assert service.receive_progress_event(_message("d-1", seq))

    retained = service.list_progress_events(RECIPIENT)
    assert len(retained) == PROGRESS_EVENT_PER_DELIVERY_LIMIT
    # Newest survive; the oldest 10 were trimmed.
    assert retained[0].message_id == "progress:d-1:10"
    assert retained[-1].message_id == f"progress:d-1:{PROGRESS_EVENT_PER_DELIVERY_LIMIT + 9}"


def test_per_recipient_cap_bounds_the_whole_table(service) -> None:
    deliveries = PROGRESS_EVENT_PER_RECIPIENT_LIMIT // PROGRESS_EVENT_PER_DELIVERY_LIMIT + 5
    for index in range(deliveries):
        for seq in range(PROGRESS_EVENT_PER_DELIVERY_LIMIT):
            service.receive_progress_event(_message(f"d-{index}", seq))

    retained = service.list_progress_events(RECIPIENT)
    assert len(retained) == PROGRESS_EVENT_PER_RECIPIENT_LIMIT
    # The oldest deliveries are gone entirely; the newest survive complete.
    assert any(item.message_id.startswith(f"progress:d-{deliveries - 1}:") for item in retained)
    assert not any(item.message_id.startswith("progress:d-0:") for item in retained)


def test_flood_converges_to_the_caps(service) -> None:
    """The §5.3 acceptance shape: 12k offered events leave a bounded table."""

    offered = 0
    for index in range(30):
        for seq in range(400):
            service.receive_progress_event(_message(f"flood-{index}", seq))
            offered += 1
    assert offered == 12_000

    assert len(service.list_progress_events(RECIPIENT)) == min(
        30 * PROGRESS_EVENT_PER_DELIVERY_LIMIT,
        PROGRESS_EVENT_PER_RECIPIENT_LIMIT,
    )


def test_expired_events_are_swept_on_receive(service, monkeypatch) -> None:
    clock = {"now": 1_000_000}
    monkeypatch.setattr(
        type(service), "_now_ms", staticmethod(lambda: clock["now"])
    )
    service.receive_progress_event(_message("d-1", 0))
    assert len(service.list_progress_events(RECIPIENT)) == 1

    clock["now"] += PROGRESS_EVENT_TTL_MS + 1
    # The sweep rides the next receive; reads alone do not resurrect them
    # into correctness, so sweep-then-read is the asserted sequence.
    service.receive_progress_event(_message("d-1", 1))

    retained = service.list_progress_events(RECIPIENT)
    assert [item.message_id for item in retained] == ["progress:d-1:1"]
