"""Wire option B: the payload inner envelope codec (Allen, 2026-11-03).

The sender writes its thread, conversation cell and class as a versioned
object under the payload's top-level ``envelope`` key; frame ① stays as it is.
The receiver restores the sender's exact payload bytes, adopts every
well-formed value and falls back to local inference for anything absent or
malformed, without ever failing the delivery.
"""

from __future__ import annotations

import json

import pytest

from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage
from hyprial.daemon.impl.inbox.envelope import (
    PAYLOAD_ENVELOPE_KEY,
    PAYLOAD_ENVELOPE_VERSION,
    adopt_payload_envelope,
    derive_message_class,
    message_class_of,
    split_payload,
    stamp_payload,
)
from hyprial.daemon.impl.inbox.links.zenoh_link import (
    decode_delivery_frame,
    encode_delivery_frame,
)
from hyprial.kernel import MessageClass, mint_cell_id, mint_thread_id

THREAD = mint_thread_id(b"payload-codec-thread")
CELL = mint_cell_id(b"payload-codec-cell")
SEND_PAYLOAD = json.dumps(
    {"message": "hi", "topic": None, "origin": {"via": "session"}},
    separators=(",", ":"),
).encode()


def _message(**overrides: object) -> InboxMessage:
    fields: dict[str, object] = {
        "message_id": "m-codec-1",
        "conversation_id": "c-codec",
        "sender": "agent:op:node-1:driver",
        "recipient": "agent:op:node-2:reader",
        "payload": SEND_PAYLOAD,
        "intent": "request",
        "lifecycle": DeliveryLifecycle.DURABLE_SERVICE,
        "created_at_ms": 1_700_000_000_000,
        "idempotency_key": "k-codec-1",
        "summary": "S",
    }
    fields.update(overrides)
    return InboxMessage(**fields)  # type: ignore[arg-type]


def _wire(payload: bytes, envelope: object) -> bytes:
    """A payload carrying *envelope* exactly where a sender appends it."""

    encoded = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
    return payload[:-1] + b',"envelope":' + encoded + b"}"


def test_the_key_and_version_are_the_published_ones() -> None:
    assert PAYLOAD_ENVELOPE_KEY == "envelope"
    assert PAYLOAD_ENVELOPE_VERSION == 1


def test_the_sender_appends_one_canonical_versioned_object() -> None:
    message = _message().with_envelope(thread_id=THREAD, conversation_cell_id=CELL)
    stamped = stamp_payload(message)
    assert stamped.payload == (
        SEND_PAYLOAD[:-1]
        + b',"envelope":{"class":"interaction","conversationCellId":"'
        + CELL.encode()
        + b'","threadId":"'
        + THREAD.encode()
        + b'","v":1}}'
    )
    assert stamped == message.with_payload(stamped.payload)


def test_the_sender_writes_its_class_even_without_a_thread() -> None:
    notice = _message(
        sender="system:hyprial:node-1",
        intent="system",
        payload=b'{"message":"disk almost full"}',
    )
    body = json.loads(stamp_payload(notice).payload)
    assert body[PAYLOAD_ENVELOPE_KEY] == {"class": "system", "v": 1}


def test_an_explicit_class_stamp_wins_over_the_derived_one() -> None:
    message = _message().with_envelope(message_class=MessageClass.SYSTEM)
    assert json.loads(stamp_payload(message).payload)["envelope"]["class"] == "system"


@pytest.mark.parametrize(
    "payload",
    [
        b"plain text body",
        b'["message","hi"]',
        b'{"text":"no message key"}',
        b'{"message":42}',
        b'{"message":"hi","envelope":{"v":1}}',
        b' {"message":"hi"}',
        b'{"message":"hi"}\n',
        b"{\"message\":\"hi\"",
        b"\xff\xfe",
    ],
)
def test_a_payload_outside_the_inner_schema_is_never_touched(payload: bytes) -> None:
    message = _message(payload=payload).with_envelope(thread_id=THREAD)
    assert stamp_payload(message) is message


@pytest.mark.parametrize("intent", ["progress", "custom-intent"])
def test_only_request_reply_and_system_intents_are_stamped(intent: str) -> None:
    message = _message(intent=intent).with_envelope(thread_id=THREAD)
    assert stamp_payload(message) is message


@pytest.mark.parametrize(
    "payload",
    [
        SEND_PAYLOAD,
        json.dumps({"message": "héllo ✓", "origin": None}, ensure_ascii=False).encode(),
        json.dumps({"message": "spaced", "topic": "t"}).encode(),
        b'{"message":"inner \\"envelope\\": text","replyTo":"m-0"}',
    ],
)
def test_the_receiver_restores_the_senders_exact_bytes(payload: bytes) -> None:
    message = _message(payload=payload).with_envelope(thread_id=THREAD)
    body, envelope = split_payload(stamp_payload(message).payload)
    assert body == payload
    assert envelope == {"class": "interaction", "threadId": THREAD, "v": 1}


def test_a_legacy_payload_splits_to_itself_without_an_envelope() -> None:
    assert split_payload(SEND_PAYLOAD) == (SEND_PAYLOAD, None)


def test_the_receiver_adopts_the_senders_thread_and_cell() -> None:
    sent = _message().with_envelope(thread_id=THREAD, conversation_cell_id=CELL)
    received = adopt_payload_envelope(stamp_payload(sent))
    assert received.payload == SEND_PAYLOAD
    assert received.thread_id == THREAD
    assert received.conversation_cell_id == CELL


def test_a_class_equal_to_local_inference_stays_unstamped() -> None:
    received = adopt_payload_envelope(stamp_payload(_message()))
    assert received.message_class is None
    assert message_class_of(received) is MessageClass.INTERACTION
    assert received == _message()


def test_a_class_the_receiver_cannot_infer_is_adopted_as_a_stamp() -> None:
    sent = _message().with_envelope(message_class=MessageClass.SYSTEM)
    received = adopt_payload_envelope(stamp_payload(sent))
    assert derive_message_class(received) is MessageClass.INTERACTION
    assert received.message_class is MessageClass.SYSTEM
    assert message_class_of(received) is MessageClass.SYSTEM


def test_an_old_sender_payload_falls_back_to_local_inference() -> None:
    legacy = _message()
    received = adopt_payload_envelope(legacy)
    assert received is legacy
    assert received.thread_id is None
    assert received.conversation_cell_id is None
    assert message_class_of(received) is derive_message_class(legacy)


@pytest.mark.parametrize(
    ("envelope", "expected"),
    [
        ({"v": 1, "threadId": "t.not-a-thread!", "class": "system"}, (None, None, "system")),
        ({"v": 1, "threadId": THREAD, "conversationCellId": "c-legacy"}, (THREAD, None, None)),
        ({"v": 1, "conversationCellId": CELL, "class": "urgent"}, (None, CELL, None)),
        ({"v": 1, "threadId": 7, "conversationCellId": None}, (None, None, None)),
        ({"v": 2, "threadId": THREAD, "class": "system"}, (None, None, None)),
        ({"v": True, "threadId": THREAD}, (None, None, None)),
        ({"threadId": THREAD}, (None, None, None)),
    ],
)
def test_malformed_values_are_stripped_and_fall_back_one_by_one(
    envelope: dict[str, object], expected: tuple[object, object, object]
) -> None:
    received = adopt_payload_envelope(_message(payload=_wire(SEND_PAYLOAD, envelope)))
    thread, cell, klass = expected
    assert received.payload == SEND_PAYLOAD
    assert received.thread_id == thread
    assert received.conversation_cell_id == cell
    assert received.message_class == (None if klass is None else MessageClass(klass))


@pytest.mark.parametrize(
    "payload",
    [
        b'{"envelope":{"v":1,"threadId":"' + THREAD.encode() + b'"},"message":"hi"}',
        b'{"message":"hi","envelope":"t.not-an-object"}',
        b'{"message":"hi","envelope":{"v": 1}}',
        b'{"message":"hi","envelope":{"v":1},"envelope":{"v":1}}',
    ],
)
def test_an_envelope_key_outside_the_sender_form_is_left_in_the_body(
    payload: bytes,
) -> None:
    message = _message(payload=payload)
    received = adopt_payload_envelope(message)
    assert received is message


def test_a_stamped_frame_round_trips_to_the_senders_message() -> None:
    sent = _message().with_envelope(thread_id=THREAD, conversation_cell_id=CELL)
    frame = encode_delivery_frame(stamp_payload(sent))
    assert set(json.loads(frame)) == set(json.loads(encode_delivery_frame(sent)))
    assert adopt_payload_envelope(decode_delivery_frame(frame)) == sent


def test_restamping_a_received_message_is_byte_stable() -> None:
    sent = stamp_payload(_message().with_envelope(thread_id=THREAD))
    relayed = stamp_payload(adopt_payload_envelope(sent))
    assert relayed.payload == sent.payload
