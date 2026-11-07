"""T6/T7: an over-budget or malformed summary is refused before any side effect.

Review blocking R1 fixes the over-budget ``data`` shape to exactly
``summaryBytes`` + ``maxBytes``.  A refused send must not enter the outbox, not
register a conversation and not reach the recipient.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.kernel import DaemonRequestError, ipc_errors
from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage

from ..support import (
    CANONICAL,
    FakeInbox,
    _close_application_domains as _close_application_domains,
    application,
    pending_message,
    register,
)

TARGET = "agent:op:node-1:bob"


def _send(app, **extra: object) -> object:  # type: ignore[no-untyped-def]
    return app.handle(
        "message.send",
        {
            "actor": "alice",
            "sessionRef": "session-1",
            "to": [TARGET],
            "message": "body",
            **extra,
        },
    )


def test_an_over_budget_summary_is_refused_with_exact_data(tmp_path: Path) -> None:
    inbox = FakeInbox(pending_message())
    app = application(tmp_path, inbox)
    register(app)
    before = tuple(app._dispatch_gate_conversations)

    with pytest.raises(DaemonRequestError) as error:
        _send(app, summary="a" * 513)

    assert error.value.code == ipc_errors.SUMMARY_REQUIRED
    assert error.value.data == {"summaryBytes": 513, "maxBytes": 512}
    assert inbox.submitted == []
    assert tuple(app._dispatch_gate_conversations) == before


def test_a_blank_summary_is_refused_as_empty(tmp_path: Path) -> None:
    inbox = FakeInbox(pending_message())
    app = application(tmp_path, inbox)
    register(app)

    with pytest.raises(DaemonRequestError) as error:
        _send(app, summary="  \n")

    assert error.value.code == ipc_errors.SUMMARY_REQUIRED
    assert error.value.data["reason"] == "empty"
    assert inbox.submitted == []


def test_a_non_string_summary_is_refused(tmp_path: Path) -> None:
    inbox = FakeInbox(pending_message())
    app = application(tmp_path, inbox)
    register(app)

    with pytest.raises(DaemonRequestError) as error:
        _send(app, summary=["not", "a", "string"])

    assert error.value.code == ipc_errors.SUMMARY_REQUIRED
    assert error.value.data["reason"] == "not_string"
    assert inbox.submitted == []


def test_a_legal_summary_reaches_the_outbox(tmp_path: Path) -> None:
    inbox = FakeInbox(pending_message())
    app = application(tmp_path, inbox)
    register(app)

    result = _send(app, summary="a summary")

    assert result["ok"] is True
    assert [message.summary for message in inbox.submitted] == ["a summary"]


def test_a_summary_equal_to_the_body_is_legal(tmp_path: Path) -> None:
    inbox = FakeInbox(pending_message())
    app = application(tmp_path, inbox)
    register(app)

    result = _send(app, message="same text", summary="same text")

    assert result["ok"] is True
    assert [message.summary for message in inbox.submitted] == ["same text"]


# --- Ruling 39: replyTo is a real association, validated before the payload --


@pytest.mark.parametrize("reply_to", ["", 123, ["message-1"]], ids=["empty", "int", "list"])
def test_a_reply_to_must_be_a_non_empty_string(
    tmp_path: Path, reply_to: object
) -> None:
    inbox = FakeInbox(pending_message())
    app = application(tmp_path, inbox)
    register(app)

    with pytest.raises(DaemonRequestError) as error:
        _send(app, summary="S", replyTo=reply_to)

    assert error.value.code == ipc_errors.INVALID_ARGUMENT
    assert inbox.submitted == []


def test_a_send_with_reply_to_saves_the_original_id_and_is_not_acked(
    tmp_path: Path,
) -> None:
    inbox = FakeInbox(pending_message())
    app = application(tmp_path, inbox)
    register(app)

    result = _send(app, summary="S", replyTo="message-1")

    assert result["ok"] is True
    (sent,) = inbox.submitted
    assert sent.intent == "reply"
    assert json.loads(sent.payload)["replyTo"] == "message-1"
    # A reply-shaped send is still delivered explicitly: the daemon does not
    # ack the original on the sender's behalf.
    assert inbox.acks == []
    assert [item.message_id for item in inbox.pending_messages(CANONICAL)] == [
        "message-1"
    ]


def test_a_refused_reply_send_does_not_ack_the_original(tmp_path: Path) -> None:
    inbox = FakeInbox(pending_message())
    app = application(tmp_path, inbox)
    register(app)

    with pytest.raises(DaemonRequestError) as error:
        _send(app, summary="a" * 513, replyTo="message-1")

    assert error.value.code == ipc_errors.SUMMARY_REQUIRED
    assert error.value.data == {"summaryBytes": 513, "maxBytes": 512}
    assert inbox.submitted == []
    assert inbox.acks == []
    assert [item.message_id for item in inbox.pending_messages(CANONICAL)] == [
        "message-1"
    ]


# --- R2: a plain send reusing a conversation is a new request ---------------


def test_a_plain_send_in_the_same_conversation_is_not_a_reply(
    tmp_path: Path,
) -> None:
    inbox = FakeInbox(pending_message())
    app = application(tmp_path, inbox)
    register(app)

    result = _send(app, summary="S", conversationId="conversation-1")

    assert result["ok"] is True
    (sent,) = inbox.submitted
    assert sent.conversation_id == "conversation-1"
    assert sent.intent == "request"
    assert "replyTo" not in json.loads(sent.payload)
    # The original request stays open: reusing its conversation is not a reply.
    assert [item.message_id for item in inbox.pending_messages(CANONICAL)] == [
        "message-1"
    ]


# --- T8/T9: stage B refuses a missing summary before any side effect -------


def test_a_send_without_a_summary_is_refused_before_any_side_effect(
    tmp_path: Path,
) -> None:
    inbox = FakeInbox(pending_message())
    app = application(tmp_path, inbox)
    register(app)
    before = tuple(app._dispatch_gate_conversations)

    with pytest.raises(DaemonRequestError) as error:
        _send(app)

    assert error.value.code == ipc_errors.SUMMARY_REQUIRED
    assert error.value.data["reason"] == "missing"
    assert inbox.submitted == []
    assert tuple(app._dispatch_gate_conversations) == before

    # An explicit reply association does not exempt the summary requirement.
    with pytest.raises(DaemonRequestError) as reply_to_error:
        _send(app, replyTo="message-1")

    assert reply_to_error.value.code == ipc_errors.SUMMARY_REQUIRED
    assert reply_to_error.value.data["reason"] == "missing"
    assert inbox.submitted == []


def test_authentication_is_refused_before_a_missing_summary(tmp_path: Path) -> None:
    """An unauthenticated declaration cannot probe for the summary rule."""

    inbox = FakeInbox(pending_message())
    app = application(tmp_path, inbox)
    register(app)
    app._sender_auth_enforced = True

    with pytest.raises(DaemonRequestError) as error:
        app.handle(
            "message.send",
            {"actor": "alice", "to": [TARGET], "message": "body"},
            _trusted_message_origin="ipc",
        )

    assert error.value.code == ipc_errors.SENDER_NOT_AUTHENTICATED
    assert inbox.submitted == []


def test_a_reply_without_a_summary_is_refused_and_leaves_the_request_pending(
    tmp_path: Path,
) -> None:
    inbox = FakeInbox(
        InboxMessage(
            message_id="message-1",
            conversation_id="conversation-1",
            sender="agent:op:node-1:driver",
            recipient=CANONICAL,
            payload=json.dumps({"message": "durable body"}).encode(),
            intent="request",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=1,
        )
    )
    app = application(tmp_path, inbox)
    register(app)

    with pytest.raises(DaemonRequestError) as error:
        app.handle(
            "message.reply",
            {
                "actor": "alice",
                "sessionRef": "session-1",
                "messageId": "message-1",
                "message": "the reply",
            },
        )

    assert error.value.code == ipc_errors.SUMMARY_REQUIRED
    assert error.value.data["reason"] == "missing"
    assert inbox.submitted == []
    assert inbox.acks == []
    assert [item.message_id for item in inbox.pending_messages(CANONICAL)] == [
        "message-1"
    ]
