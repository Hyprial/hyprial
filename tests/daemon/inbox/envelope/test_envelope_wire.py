"""Executor decision Q1: the envelope never reaches ``hyprial-delivery-frame/v1``.

Frame bytes for a message with the local envelope set must equal the bytes for
the same message without it, and the decoder must not pick envelope keys up
from a frame (a peer cannot stamp a local field).  PIN: these pass on the
first run against an unchanged codec.  Each guard also runs against a seeded
red case, a synthetic codec that emits ``thread_id`` or a decoder that picks
it up, and must reject it.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable

import pytest

from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage
from hyprial.daemon.impl.inbox.links.zenoh_link import (
    decode_delivery_frame,
    encode_delivery_frame,
)
from hyprial.kernel import MessageClass, mint_cell_id, mint_thread_id

THREAD = mint_thread_id(b"envelope-wire-thread")
CELL = mint_cell_id(b"envelope-wire-cell")

_PINNED_FRAME = (
    b'{"conversation_id":"c-wire","created_at_ms":1700000000000,'
    b'"expires_at_ms":1700000100000,"idempotency_key":"k-1","intent":"request",'
    b'"lifecycle":"durable_service","message_id":"m-wire-1","origin_node":"node-1",'
    b'"payload":"eyJtZXNzYWdlIjoiaGkifQ==","recipient":"agent:op:node-2:reader",'
    b'"schema":"hyprial-delivery-frame/v1","sender":"agent:op:node-1:driver",'
    b'"summary":"S"}'
)
_PINNED_FRAME_SHA256 = "dd125ab51d7a8bcc578b7f713cb814280923846fcf84f7673c208c4c56c780f5"


def _legacy() -> InboxMessage:
    return InboxMessage(
        message_id="m-wire-1",
        conversation_id="c-wire",
        sender="agent:op:node-1:driver",
        recipient="agent:op:node-2:reader",
        payload=b'{"message":"hi"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1_700_000_000_000,
        idempotency_key="k-1",
        origin_node="node-1",
        expires_at_ms=1_700_000_100_000,
        summary="S",
    )


def _stamped() -> InboxMessage:
    return _legacy().with_envelope(
        thread_id=THREAD, conversation_cell_id=CELL, message_class=MessageClass.SYSTEM
    )


Encode = Callable[[InboxMessage], bytes]
Decode = Callable[[bytes], InboxMessage]


def _assert_frame_ignores_the_envelope(encode: Encode) -> None:
    assert encode(_stamped()) == encode(_legacy())


def _assert_decode_ignores_wire_envelope_keys(decode: Decode) -> None:
    value = json.loads(encode_delivery_frame(_legacy()))
    value.update(
        {
            "thread_id": THREAD,
            "conversation_cell_id": CELL,
            "message_class": "system",
            "threadId": THREAD,
            "class": "system",
        }
    )
    decoded = decode(json.dumps(value).encode())
    assert decoded.thread_id is None
    assert decoded.conversation_cell_id is None
    assert decoded.message_class is None
    assert decoded == decode(encode_delivery_frame(_legacy()))


def _leaky_encode(message: InboxMessage) -> bytes:
    value = json.loads(encode_delivery_frame(message))
    if message.thread_id is not None:
        value["thread_id"] = message.thread_id
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _leaky_decode(frame: bytes) -> InboxMessage:
    thread_id = json.loads(frame).get("thread_id")
    return decode_delivery_frame(frame).with_envelope(thread_id=thread_id)


def test_the_envelope_does_not_change_frame_bytes() -> None:
    _assert_frame_ignores_the_envelope(encode_delivery_frame)


def test_the_frame_guard_rejects_a_codec_that_emits_thread_id() -> None:
    with pytest.raises(AssertionError):
        _assert_frame_ignores_the_envelope(_leaky_encode)


def test_the_legacy_frame_bytes_are_pinned() -> None:
    frame = encode_delivery_frame(_legacy())
    assert set(json.loads(frame)) == {
        "schema",
        "message_id",
        "conversation_id",
        "sender",
        "recipient",
        "intent",
        "lifecycle",
        "idempotency_key",
        "created_at_ms",
        "payload",
        "expires_at_ms",
        "origin_node",
        "summary",
    }
    assert frame == _PINNED_FRAME
    assert hashlib.sha256(frame).hexdigest() == _PINNED_FRAME_SHA256


def test_decode_ignores_envelope_keys_a_peer_puts_on_the_wire() -> None:
    _assert_decode_ignores_wire_envelope_keys(decode_delivery_frame)


def test_the_decode_guard_rejects_a_decoder_that_reads_thread_id() -> None:
    with pytest.raises(AssertionError):
        _assert_decode_ignores_wire_envelope_keys(_leaky_decode)


def test_a_stamped_message_round_trips_the_wire_without_its_envelope() -> None:
    decoded = decode_delivery_frame(encode_delivery_frame(_stamped()))
    assert decoded == _legacy()
    assert base64.b64decode(json.loads(encode_delivery_frame(_stamped()))["payload"]) == (
        _legacy().payload
    )
