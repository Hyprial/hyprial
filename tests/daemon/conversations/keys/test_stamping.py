"""S4: message.send / message.reply stamp the local thread id (plan §2.0).

The stamp is a local ``InboxMessage`` field only (Q1): the IPC responses
gain no key and the frame is unchanged.
"""

from __future__ import annotations

from tests.daemon.delivery.message import test_message_history as history
from tests.daemon.delivery.message.test_message_history import (
    READER_SESSION,
    SESSION,
    _deliver,
    _HistoryHarness,
)

from hyprial.daemon.impl.conversations.keys import direct_key
from hyprial.kernel import mint_thread_id

history_harness = history.harness
_ENVELOPE_KEYS = {"threadId", "thread_id", "conversationCellId", "messageClass"}


def _send(harness: _HistoryHarness, key: str, **extra: object) -> dict:
    result = harness.app.handle(
        "message.send",
        {
            "from": harness.actor,
            "sessionRef": SESSION,
            "to": [harness.reader],
            "message": f"probe {key}",
            "summary": f"probe {key}",
            "idempotencyKey": key,
            **extra,
        },
        _trusted_message_origin="ipc",
    )
    assert result["ok"] is True, result
    return result


def _pending(harness: _HistoryHarness, actor: str, message_id: str):
    (row,) = [
        row
        for row in harness.inbox.pending_messages(actor)
        if row.message_id == message_id
    ]
    return row


def test_agent_send_stamps_the_pair_thread(history_harness: _HistoryHarness) -> None:
    sent = _send(history_harness, "stamp-1", conversationId="conv-stamp")
    (delivery,) = sent["deliveries"]
    assert not _ENVELOPE_KEYS & set(sent) and not _ENVELOPE_KEYS & set(delivery)
    row = _pending(history_harness, history_harness.reader, delivery["messageId"])
    assert row.thread_id == direct_key(history_harness.actor, history_harness.reader).id
    assert row.conversation_id == "conv-stamp"


def test_reply_to_an_unstamped_request_derives_the_thread(
    history_harness: _HistoryHarness,
) -> None:
    _deliver(history_harness, "m-unstamped")
    replied = history_harness.app.handle(
        "message.reply",
        {
            "from": history_harness.actor,
            "sessionRef": SESSION,
            "messageId": "m-unstamped",
            "message": "answer",
            "summary": "answer",
        },
    )
    assert not _ENVELOPE_KEYS & set(replied)
    reply = _pending(history_harness, history_harness.reader, replied["replyMessageId"])
    assert reply.thread_id == direct_key(history_harness.actor, history_harness.reader).id


def test_reply_keeps_an_inherited_thread(history_harness: _HistoryHarness) -> None:
    inherited = mint_thread_id(b"inherited-thread")
    message = _deliver(history_harness, "m-carrier", sender=history_harness.actor, recipient=history_harness.reader)
    assert message.thread_id is None
    stamped = message.with_envelope(thread_id=inherited)
    history_harness.inbox.receive(
        stamped.__class__(**{**_fields(stamped), "message_id": "m-stamped"})
    )
    replied = history_harness.app.handle(
        "message.reply",
        {
            "from": history_harness.reader,
            "sessionRef": READER_SESSION,
            "messageId": "m-stamped",
            "message": "answer",
            "summary": "answer",
        },
    )
    reply = _pending(history_harness, history_harness.actor, replied["replyMessageId"])
    assert reply.thread_id == inherited


def _fields(message: object) -> dict:
    return {
        name: getattr(message, name)
        for name in message.__dataclass_fields__  # type: ignore[attr-defined]
    }
