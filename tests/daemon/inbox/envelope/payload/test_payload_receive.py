"""Wire option B, receiver read site: the inbox adopts the sender's envelope.

Every ingress of a delivery frame (``receive`` and ``accept_custody``) strips
the sender form of the payload envelope before anything is stored, so the
stored payload, ``message.read_body`` and ``message.history`` are
byte-identical to an unstamped message, while the envelope columns carry the
sender's values.  A malformed envelope never fails a delivery.  A custody
holder hands the envelope on when it forwards.
"""

from __future__ import annotations

import json
from pathlib import Path

from hyprial.daemon.impl.application.messaging.inbox_surface.queries import (
    _MessageQueriesMixin,
)
from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)
from hyprial.daemon.impl.inbox.envelope import (
    EnvelopeStampingTransport,
    message_class_of,
    stamp_payload,
)
from hyprial.daemon.impl.inbox.history import conversation_messages
from hyprial.daemon.impl.inbox.links.zenoh_link import (
    decode_delivery_frame,
    encode_delivery_frame,
)
from hyprial.kernel import MessageClass, mint_cell_id, mint_thread_id

THREAD = mint_thread_id(b"payload-receive-thread")
CELL = mint_cell_id(b"payload-receive-cell")
SENDER = "agent:op:node-1:driver"
RECIPIENT = "agent:op:node-2:reader"
PAYLOAD = json.dumps(
    {"message": "status?", "topic": None, "origin": {"via": "session"}},
    separators=(",", ":"),
).encode()


def _message(**overrides: object) -> InboxMessage:
    fields: dict[str, object] = {
        "message_id": "m-receive-1",
        "conversation_id": "c-receive",
        "sender": SENDER,
        "recipient": RECIPIENT,
        "payload": PAYLOAD,
        "intent": "request",
        "lifecycle": DeliveryLifecycle.DURABLE_SERVICE,
        "created_at_ms": 1_700_000_000_000,
        "idempotency_key": "k-receive-1",
        "summary": "S",
    }
    fields.update(overrides)
    return InboxMessage(**fields)  # type: ignore[arg-type]


def _over_the_wire(message: InboxMessage) -> InboxMessage:
    return decode_delivery_frame(encode_delivery_frame(message))


class _Wire(MemoryDeliveryTransport):
    """A network transport: every delivery crosses frame ① to *peer*."""

    def __init__(self, peer: InboxService) -> None:
        super().__init__()
        self._peer = peer
        self.frames: list[bytes] = []

    def is_online(self, recipient: str) -> bool:
        return True

    def deliver(self, message: InboxMessage) -> bool:
        frame = encode_delivery_frame(message)
        self.frames.append(frame)
        return self._peer.receive(decode_delivery_frame(frame)).acknowledged


def _read_body(message: InboxMessage) -> dict[str, object]:
    return _MessageQueriesMixin._read_body_row(  # type: ignore[arg-type]
        None, message, pending=True, fetched=False
    )


def _received(tmp_path: Path, name: str, message: InboxMessage) -> InboxMessage:
    service = InboxService(tmp_path / f"{name}.sqlite3", MemoryDeliveryTransport())
    try:
        assert service.receive(message).acknowledged
        (stored,) = service.pending_messages(RECIPIENT)
        (history,) = conversation_messages(
            service._db, participant=RECIPIENT, conversation_id="c-receive"
        )
        assert history.payload == stored.payload
        return stored
    finally:
        service.close()


def test_a_stamped_frame_is_stored_with_the_senders_exact_payload(
    tmp_path: Path,
) -> None:
    sent = _message().with_envelope(thread_id=THREAD, conversation_cell_id=CELL)
    stored = _received(tmp_path, "new", _over_the_wire(stamp_payload(sent)))
    legacy = _received(tmp_path, "old", _over_the_wire(_message()))
    assert stored.payload == PAYLOAD == legacy.payload
    assert stored.thread_id == THREAD
    assert stored.conversation_cell_id == CELL
    assert stored.message_class is None
    assert message_class_of(stored) is MessageClass.INTERACTION
    assert _read_body(stored) == _read_body(legacy)


def test_an_old_sender_frame_falls_back_to_local_inference(tmp_path: Path) -> None:
    stored = _received(tmp_path, "old", _over_the_wire(_message()))
    assert stored.payload == PAYLOAD
    assert (stored.thread_id, stored.conversation_cell_id, stored.message_class) == (
        None,
        None,
        None,
    )
    workflow = _received(
        tmp_path,
        "workflow",
        _over_the_wire(_message(message_id="workflow-00000000-0000-4000-8000-000000000001")),
    )
    assert message_class_of(workflow) is MessageClass.SYSTEM


def test_a_sender_class_is_preferred_over_local_inference(tmp_path: Path) -> None:
    sent = _message().with_envelope(message_class=MessageClass.SYSTEM)
    stored = _received(tmp_path, "system", _over_the_wire(stamp_payload(sent)))
    assert stored.payload == PAYLOAD
    assert message_class_of(stored) is MessageClass.SYSTEM


def test_a_malformed_envelope_never_fails_the_delivery(tmp_path: Path) -> None:
    bad = PAYLOAD[:-1] + b',"envelope":{"class":"loud","threadId":"t.?","v":1}}'
    stored = _received(tmp_path, "bad", _over_the_wire(_message(payload=bad)))
    assert stored.payload == PAYLOAD
    assert (stored.thread_id, stored.message_class) == (None, None)
    assert message_class_of(stored) is MessageClass.INTERACTION


def test_a_custody_holder_strips_stores_and_hands_the_envelope_on(
    tmp_path: Path,
) -> None:
    recipient = InboxService(tmp_path / "recipient.sqlite3", MemoryDeliveryTransport())
    wire = _Wire(recipient)
    mailbox = InboxService(tmp_path / "mailbox.sqlite3", EnvelopeStampingTransport(wire))
    try:
        sent = _message().with_envelope(thread_id=THREAD, conversation_cell_id=CELL)
        frame = _over_the_wire(stamp_payload(sent))
        assert mailbox.accept_custody(frame, mailbox_node="node-3").acknowledged
        row = mailbox._db.execute(
            "SELECT payload, thread_id, conversation_cell_id, message_class FROM custody"
        ).fetchone()
        assert tuple(row) == (PAYLOAD, THREAD, CELL, None)
        mailbox.retry_custody_due()
        (forwarded,) = wire.frames
        assert decode_delivery_frame(forwarded).payload == stamp_payload(sent).payload
        (delivered,) = recipient.pending_messages(RECIPIENT)
        assert delivered.payload == PAYLOAD
        assert (delivered.thread_id, delivered.conversation_cell_id) == (THREAD, CELL)
    finally:
        mailbox.close()
        recipient.close()


def test_a_sender_outbox_row_never_carries_the_envelope(tmp_path: Path) -> None:
    recipient = InboxService(tmp_path / "recipient.sqlite3", MemoryDeliveryTransport())
    wire = _Wire(recipient)
    sender = InboxService(tmp_path / "sender.sqlite3", EnvelopeStampingTransport(wire))
    try:
        sent = _message().with_envelope(thread_id=THREAD)
        assert sender.submit(sent).accepted
        (frame,) = wire.frames
        assert b'"envelope":' in decode_delivery_frame(frame).payload
        rows = sender._db.execute(
            "SELECT payload FROM sender_copies UNION ALL SELECT payload FROM outbox"
        ).fetchall()
        assert rows and all(bytes(row[0]) == PAYLOAD for row in rows)
        (delivered,) = recipient.pending_messages(RECIPIENT)
        assert delivered.payload == PAYLOAD
        assert delivered.thread_id == THREAD
    finally:
        sender.close()
        recipient.close()
