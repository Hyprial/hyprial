"""Wire option B, trust pin: what an adopted peer thread reaches today.

The receiver adopts any well-formed ``threadId`` from a peer frame (Allen's
ruling: the sender's values are preferred).  These tests pin the current
reach of that choice, a known issue kept without a check:

* a peer X writing to Z can name the thread of another pair (Y, Z);
* the receiver's reply to that message (``inherit_envelope``, as composed by
  ``message.reply`` and the mechanism answer) inherits the adopted thread and
  carries it back on the wire;
* in the honest case the sender's value equals the pair derivation, because
  thread ids are deterministic per pair, which is what a pair check would
  compare against.

They pin shipped behaviour.  Per Allen 2026-11-07 no pair check is added
(no new security restriction); the trust boundary is recorded as a known
issue.  If a check is planned later, the forged-thread assertions flip.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage
from hyprial.daemon.impl.inbox.envelope import (
    adopt_payload_envelope,
    inherit_envelope,
    stamp_payload,
)
from hyprial.daemon.impl.inbox.links.zenoh_link import (
    decode_delivery_frame,
    encode_delivery_frame,
)
from hyprial.daemon.impl.conversations.keys import direct_key

PEER = "agent:op:node-x:mallory"
VICTIM = "agent:op:node-y:victim"
RECEIVER = "agent:op:node-z:reader"
FORGED = direct_key(VICTIM, RECEIVER).id  # type: ignore[union-attr]
HONEST = direct_key(PEER, RECEIVER).id  # type: ignore[union-attr]
SOURCE_ROOT = Path(__file__).resolve().parents[5] / "src" / "hyprial"
REPLY_SITES = {
    "daemon/impl/application/messaging/inbox_surface/reply/__init__.py",
    "daemon/impl/runtime/forwards.py",
}


def _request(**overrides: object) -> InboxMessage:
    fields: dict[str, object] = {
        "message_id": "m-trust-1",
        "conversation_id": "c-trust",
        "sender": PEER,
        "recipient": RECEIVER,
        "payload": b'{"message":"hello"}',
        "intent": "request",
        "lifecycle": DeliveryLifecycle.DURABLE_SERVICE,
        "created_at_ms": 1_700_000_000_000,
    }
    fields.update(overrides)
    return InboxMessage(**fields)  # type: ignore[arg-type]


def _received(sent: InboxMessage) -> InboxMessage:
    frame = encode_delivery_frame(stamp_payload(sent))
    return adopt_payload_envelope(decode_delivery_frame(frame))


def _reply_to(original: InboxMessage) -> InboxMessage:
    reply = InboxMessage(
        message_id="reply-trust-1",
        conversation_id=original.conversation_id,
        sender=original.recipient,
        recipient=original.sender,
        payload=b'{"message":"answer","replyTo":"m-trust-1"}',
        intent="reply",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        idempotency_key=f"reply:{original.message_id}",
        created_at_ms=1_700_000_000_001,
    )
    return inherit_envelope(reply, original)


def test_an_honest_sender_stamps_the_pair_derivation() -> None:
    assert FORGED is not None and HONEST is not None and FORGED != HONEST
    received = _received(_request().with_envelope(thread_id=HONEST))
    pair_key = direct_key(received.sender, received.recipient)
    assert pair_key is not None
    assert received.thread_id == HONEST == pair_key.id


def test_a_peer_can_name_another_pairs_thread_and_it_is_adopted() -> None:
    received = _received(_request().with_envelope(thread_id=FORGED))
    pair_key = direct_key(received.sender, received.recipient)
    assert pair_key is not None
    assert received.thread_id == FORGED
    assert received.thread_id != pair_key.id


def test_the_receivers_reply_inherits_and_resends_the_adopted_thread() -> None:
    received = _received(_request().with_envelope(thread_id=FORGED))
    reply = _reply_to(received)
    assert reply.thread_id == FORGED
    on_the_wire = json.loads(stamp_payload(reply).payload)["envelope"]
    assert on_the_wire["threadId"] == FORGED
    assert _received(reply).thread_id == FORGED


def test_reply_inheritance_is_composed_at_the_known_sites() -> None:
    sites = {
        str(path.relative_to(SOURCE_ROOT))
        for path in SOURCE_ROOT.rglob("*.py")
        if "inherit_envelope(reply, original)" in path.read_text(encoding="utf-8")
    }
    assert sites == REPLY_SITES


def test_an_unknown_envelope_version_is_stripped_ignored_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = b'{"message":"hello"}'
    future = body[:-1] + b',"envelope":{"class":"system","threadId":"' + FORGED.encode()
    future += b'","v":2}}'
    with caplog.at_level(logging.INFO, logger="hyprial.daemon.impl.inbox.envelope.payload"):
        received = adopt_payload_envelope(_request(payload=future))
    assert received.payload == body
    assert (received.thread_id, received.message_class) == (None, None)
    (record,) = [
        record
        for record in caplog.records
        if record.name == "hyprial.daemon.impl.inbox.envelope.payload"
    ]
    assert record.levelno == logging.INFO
    assert "m-trust-1" in record.getMessage()
    assert "2" in record.getMessage()
