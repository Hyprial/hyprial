"""T6 (reply half) and T18: reply summaries, and receipt-bound retries.

A retried reply joins its durable receipt only when the command -- summary
included -- is byte-identical.  A different summary must not silently send a
second reply, and a refused reply must leave the original request pending.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.kernel import DaemonRequestError, ipc_errors
from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage

from ..support import (
    CANONICAL,
    DurableReplyInbox,
    FakeInbox,
    _close_application_domains as _close_application_domains,
    application,
    register,
)


def _pending() -> InboxMessage:
    """A pending request from a canonical agent, so a reply can route to it."""

    return InboxMessage(
        message_id="message-1",
        conversation_id="conversation-1",
        sender="agent:op:node-1:driver",
        recipient=CANONICAL,
        payload=json.dumps({"message": "durable body"}).encode(),
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )


def _reply(app, **extra: object) -> object:  # type: ignore[no-untyped-def]
    return app.handle(
        "message.reply",
        {
            "actor": "alice",
            "sessionRef": "session-1",
            "messageId": "message-1",
            "message": "the reply",
            **extra,
        },
    )


def test_an_over_budget_reply_summary_is_refused_and_leaves_the_request_pending(
    tmp_path: Path,
) -> None:
    inbox = FakeInbox(_pending())
    app = application(tmp_path, inbox)
    register(app)

    with pytest.raises(DaemonRequestError) as error:
        _reply(app, summary="a" * 513)

    assert error.value.code == ipc_errors.SUMMARY_REQUIRED
    assert error.value.data == {"summaryBytes": 513, "maxBytes": 512}
    assert inbox.submitted == []
    assert inbox.acks == []


def test_identical_reply_retries_join_one_receipt(tmp_path: Path) -> None:
    inbox = DurableReplyInbox(_pending())
    app = application(tmp_path, inbox)
    register(app)

    first = _reply(app, summary="S1")
    retried = _reply(app, summary="S1")

    assert first["ok"] is True
    assert first["replied"] is True
    assert retried["ok"] is True
    assert len(inbox.submitted) == 1


def test_a_reply_retry_with_a_different_summary_conflicts(tmp_path: Path) -> None:
    inbox = DurableReplyInbox(_pending())
    app = application(tmp_path, inbox)
    register(app)
    assert _reply(app, summary="S1")["replied"] is True

    conflicted = _reply(app, summary="S2")

    assert conflicted["ok"] is False
    assert conflicted["code"] == "REPLY_ALREADY_SUBMITTED"
    assert conflicted["retryable"] is False
    assert len(inbox.submitted) == 1
    assert inbox.submitted[0].summary == "S1"


def test_a_reply_retry_with_different_text_conflicts(tmp_path: Path) -> None:
    inbox = DurableReplyInbox(_pending())
    app = application(tmp_path, inbox)
    register(app)
    assert _reply(app, summary="S1", message="first")["replied"] is True

    conflicted = _reply(app, summary="S1", message="second")

    assert conflicted["ok"] is False
    assert conflicted["code"] == "REPLY_ALREADY_SUBMITTED"
    assert conflicted["retryable"] is False
    assert len(inbox.submitted) == 1


def test_a_reply_saves_the_original_id_in_its_payload(tmp_path: Path) -> None:
    """R2: the receiver must be able to close the exact original request."""

    inbox = DurableReplyInbox(_pending())
    app = application(tmp_path, inbox)
    register(app)
    assert _reply(app, summary="S")["replied"] is True

    (reply,) = inbox.submitted

    assert reply.intent == "reply"
    assert json.loads(reply.payload)["replyTo"] == "message-1"
    assert reply.summary == "S"
