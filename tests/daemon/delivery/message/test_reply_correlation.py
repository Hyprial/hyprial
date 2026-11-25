"""Read a current send association and its durable reply without consuming mail."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, replace

import pytest

from hyprial.kernel import reply_message_id

from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxMessage,
    InboxReadProjection,
    InboxService,
    MemoryDeliveryTransport,
)

SENDER = "user:owner@example.test"
AGENT = "agent:owner@example.test:node:guide"
CONVERSATION = "onboarding-conversation"
OPERATION = "frs-0123456789abcdef"
REQUEST = "current-request"
REPLY = reply_message_id(REQUEST)


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "inbox.sqlite3"
    service = InboxService(path, MemoryDeliveryTransport())
    yield service, path, InboxReadProjection(path)
    service.close()


def _request(**overrides):
    return replace(InboxMessage(
        message_id=REQUEST,
        conversation_id=CONVERSATION,
        sender=SENDER,
        recipient=AGENT,
        payload=b'{"message":"hello"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1000,
        idempotency_key=f"{OPERATION}:{AGENT}:0",
    ), **overrides)


def _reply(**overrides):
    return replace(InboxMessage(
        message_id=REPLY,
        conversation_id=CONVERSATION,
        sender=AGENT,
        recipient=SENDER,
        payload=json.dumps({"replyTo": REQUEST, "message": "answer"}).encode(),
        intent="reply",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1100,
        idempotency_key=f"reply:{REQUEST}",
    ), **overrides)


def _request_read(read, **overrides):
    return read.correlated_request(
        sender=SENDER, recipient=AGENT, operation_key=OPERATION, **overrides,
    )


def _reply_read(read, **overrides):
    selectors = dict(
        sender=AGENT, recipient=SENDER, conversation_id=CONVERSATION,
        request_message_id=REQUEST,
    )
    return read.correlated_reply(**(selectors | overrides))


def _dump(path):
    with closing(sqlite3.connect(path)) as connection:
        return tuple(connection.iterdump())


def test_send_binding_survives_delivery_removal_and_projection_restart(store):
    service, path, read = store
    assert service.submit(_request(), now_ms=1000, defer_direct=True).accepted
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DELETE FROM outbox")
    before = _dump(path)
    expected = dict(message_id=REQUEST, sender=SENDER, recipient=AGENT,
                    conversation_id=CONVERSATION, operation_key=OPERATION)
    assert asdict(_request_read(read)) == expected
    assert asdict(_request_read(InboxReadProjection(path))) == expected
    assert _dump(path) == before


def test_current_reply_remains_provable_after_ack_and_is_read_only(store):
    service, path, read = store
    assert service.receive(_reply(), now_ms=1200).accepted
    assert service.ack(SENDER, REPLY).acknowledged
    before = _dump(path)
    expected = dict(message_id=REPLY, request_message_id=REQUEST,
                    sender=AGENT, recipient=SENDER, conversation_id=CONVERSATION,
                    received_at_ms=1200)
    assert asdict(_reply_read(read)) == expected
    assert asdict(_reply_read(InboxReadProjection(path))) == expected
    assert _dump(path) == before


@pytest.mark.parametrize("overrides", [
    {"sender": "user:someone-else"}, {"recipient": "agent:owner@example.test:node:other"},
    {"operation_key": "frs-another-operation"},
])
def test_request_never_uses_another_sender_recipient_or_operation(store, overrides):
    service, _, read = store
    assert service.submit(_request(), now_ms=1000, defer_direct=True).accepted
    selectors = dict(sender=SENDER, recipient=AGENT, operation_key=OPERATION)
    assert read.correlated_request(**(selectors | overrides)) is None


@pytest.mark.parametrize("overrides", [
    {"sender": "agent:owner@example.test:node:other"},
    {"recipient": "user:someone-else"}, {"conversation_id": "other-conversation"},
    {"request_message_id": "older-request"},
])
def test_reply_requires_all_trusted_selectors(store, overrides):
    service, _, read = store
    assert service.receive(_reply(), now_ms=1200).accepted
    assert _reply_read(read, **overrides) is None


@pytest.mark.parametrize("payload", [
    b'{"replyTo":"older-request","message":"answer"}',
    b'{"message":"replyTo current-request"}', b"broken", b"[]",
    b'{"replyTo":"current-request","replyTo":"older-request"}',
])
def test_no_historical_or_malformed_body_can_prove_reply(store, payload):
    service, path, read = store
    assert service.receive(_reply(), now_ms=1200).accepted
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE inbox SET payload=? WHERE message_id=?", (payload, REPLY))
    assert _reply_read(read) is None


def test_dropped_body_and_ambiguous_replies_are_unverified(store):
    service, path, read = store
    assert service.receive(_reply(), now_ms=1200).accepted
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE inbox SET body_dropped_at_ms=1300 WHERE message_id=?", (REPLY,))
    assert _reply_read(read) is None
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE inbox SET body_dropped_at_ms=NULL WHERE message_id=?", (REPLY,))
    assert service.receive(_reply(message_id="another-reply", idempotency_key="other-reply"), now_ms=1300).accepted
    assert _reply_read(read) is None


def test_no_reply_is_inferred_from_send_acceptance_or_outbox(store):
    service, path, read = store
    assert service.submit(_request(), now_ms=1000, defer_direct=True).accepted
    assert _request_read(read) is not None
    assert _reply_read(read) is None
    assert service.submit(_reply(), now_ms=1200, defer_direct=True).accepted
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DELETE FROM sender_copies WHERE message_id=?", (REPLY,))
    assert _reply_read(read) is None


def test_local_agent_reply_is_proven_by_its_retained_sender_copy(store):
    service, path, read = store
    assert service.submit(_reply(), now_ms=1200, defer_direct=True).accepted
    before = _dump(path)
    reply = _reply_read(read)
    assert reply is not None and reply.message_id == REPLY
    assert reply.request_message_id == REQUEST
    assert _dump(path) == before
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DELETE FROM outbox")
    assert _reply_read(InboxReadProjection(path)) == reply


def test_same_reply_in_sender_copy_and_inbox_is_one_reply(store):
    service, _, read = store
    assert service.submit(_reply(), now_ms=1200, defer_direct=True).accepted
    assert service.receive(_reply(), now_ms=1300).accepted
    reply = _reply_read(read)
    assert reply is not None and reply.message_id == REPLY
    assert reply.received_at_ms == 1300


def test_retained_copy_does_not_restore_a_released_inbox_body(store):
    service, path, read = store
    assert service.submit(_reply(), now_ms=1200, defer_direct=True).accepted
    assert service.receive(_reply(), now_ms=1300).accepted
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE inbox SET body_dropped_at_ms=1400 WHERE message_id=?", (REPLY,))
    assert _reply_read(read) is None


def test_dropped_or_ambiguous_sender_reply_copies_are_unverified(store):
    service, path, read = store
    assert service.submit(_reply(), now_ms=1200, defer_direct=True).accepted
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE sender_copies SET body_dropped_at_ms=1300 WHERE message_id=?", (REPLY,))
    assert _reply_read(read) is None
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE sender_copies SET body_dropped_at_ms=NULL WHERE message_id=?", (REPLY,))
    assert service.receive(_reply(message_id="other-local-reply", idempotency_key="other-reply"), now_ms=1400).accepted
    assert _reply_read(read) is None


def test_many_newer_unrelated_replies_do_not_hide_the_correlated_reply(store):
    service, _, read = store
    assert service.receive(_reply(), now_ms=1200).accepted
    for index in range(205):
        assert service.receive(_reply(
            message_id=f"other-{index}",
            idempotency_key=f"reply:request-{index}",
            payload=json.dumps({"replyTo": f"request-{index}", "message": "other"}).encode(),
        ), now_ms=1300 + index).accepted
    assert _reply_read(read).message_id == REPLY


@pytest.mark.parametrize("table", ["inbox", "sender_copies"])
@pytest.mark.parametrize("field,value", [
    ("message_id", "not-the-current-reply"),
    ("idempotency_key", "reply:another-request"),
])
def test_reply_requires_daemon_reply_id_and_key(store, table, field, value):
    service, path, read = store
    if table == "inbox":
        assert service.receive(_reply(), now_ms=1200).accepted
    else:
        assert service.submit(_reply(), now_ms=1200, defer_direct=True).accepted
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(f"UPDATE {table} SET {field}=?", (value,))
    assert _reply_read(read) is None


@pytest.mark.parametrize("field,value", [
    ("payload", b'{"replyTo":"current-request","message":"different answer"}'),
    ("payload", b'{"replyTo":"older-request","message":"old answer"}'),
    ("payload", b"malformed"),
    ("sender", "agent:owner@example.test:node:other"),
    ("recipient", "user:other@example.test"),
    ("conversation_id", "other-conversation"),
    ("intent", "request"),
    ("body_dropped_at_ms", 1500),
])
def test_conflicting_sender_copy_and_inbox_are_unverified(store, field, value):
    service, path, read = store
    assert service.submit(_reply(), now_ms=1200, defer_direct=True).accepted
    assert service.receive(_reply(), now_ms=1300).accepted
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(f"UPDATE sender_copies SET {field}=?", (value,))
    assert _reply_read(read) is None
