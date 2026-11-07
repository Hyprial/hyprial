from __future__ import annotations

import json
from pathlib import Path
import pytest
from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage, InboxAuthorityTimeout, InboxAuthorityUnavailable, SubmissionResult

from ..support import (
    CANONICAL,
    DurableReplyInbox,
    FakeInbox,
    _close_application_domains as _close_application_domains,
    application,
    pending_message,
    register,
)


def test_reply_is_submitted_before_original_delivery_is_acknowledged(
    tmp_path: Path,
) -> None:
    # Canonical four-segment sender: a bare/two-segment sender is node-shaped
    # after normalization and the reply admission gate rejects it loudly.
    inbox = FakeInbox(
        InboxMessage(
            message_id="message-1",
            conversation_id="conversation-1",
            sender="agent:op:node-2:bob",
            recipient=CANONICAL,
            payload=json.dumps({"message": "durable body"}).encode(),
            intent="request",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=1,
        )
    )
    app = application(tmp_path, inbox)
    register(app)

    result = app.handle(
        "message.reply",
        {
            "actor": "alice",
            "sessionRef": "session-1",
            "messageId": "message-1",
            "message": "completed",
            "summary": "completed",
        },
    )

    assert result["ok"] is True
    assert result["replied"] is True
    assert result["acknowledged"] is True
    # The canonical sender URI passes through untouched: one recipient form
    # so dedup/custody/DLQ never split.
    assert inbox.submitted[0].recipient == "agent:op:node-2:bob"
    assert inbox.submitted[0].conversation_id == "conversation-1"
    assert json.loads(inbox.submitted[0].payload) == {
        "message": "completed",
        # Ruling 55: an agent reply carries its association so the receiver
        # can correlate the dispatch and close its outbound ledger row.
        "replyTo": "message-1",
        "origin": {"via": "session"},
    }
    assert inbox.acks == [(CANONICAL, "message-1")]


def test_failed_lark_reply_keeps_original_pending_without_ack(
    tmp_path: Path,
) -> None:
    class QueuedReplyInbox(FakeInbox):
        def submit(self, message: InboxMessage, **_link: object) -> SubmissionResult:
            self.submitted.append(message)
            return SubmissionResult(message.message_id, accepted=True, queued=True)

    inbox = QueuedReplyInbox(
        InboxMessage(
            message_id="message-1",
            conversation_id="conversation-1",
            sender="adapter:lark:e2e",
            recipient=CANONICAL,
            payload=json.dumps({"message": "durable body"}).encode(),
            intent="request",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=1,
        )
    )
    app = application(tmp_path, inbox)
    register(app)

    result = app.handle(
        "message.reply",
        {
            "actor": "alice",
            "sessionRef": "session-1",
            "messageId": "message-1",
            "message": "not delivered",
            "summary": "not delivered",
        },
    )

    assert result == {
        "ok": False,
        "messageId": "message-1",
        "replyMessageId": "c4ff4b95-7cef-54b3-a843-1643bc36c56d",
        "replied": False,
        "acknowledged": False,
        "queued": True,
        "code": "REPLY_SETTLEMENT_PENDING",
        "retryable": True,
        "hint": "the reply is queued; retrying returns its state and never sends different text",
    }
    assert len(inbox.submitted) == 1
    assert inbox.acks == []
    assert inbox.pending_messages(CANONICAL) != ()


def test_lark_reply_retry_returns_settled_receipt_after_background_ack(
    tmp_path: Path,
) -> None:
    inbox = DurableReplyInbox(
        InboxMessage(
            message_id="message-1",
            conversation_id="conversation-1",
            sender="adapter:lark:e2e",
            recipient=CANONICAL,
            payload=json.dumps({"message": "durable body"}).encode(),
            intent="request",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=1,
        )
    )
    app = application(tmp_path, inbox)
    register(app)
    request = {
        "actor": "alice",
        "sessionRef": "session-1",
        "messageId": "message-1",
        "message": "eventual reply",
        "summary": "eventual reply",
    }

    first = app.handle("message.reply", request)
    assert first["code"] == "REPLY_SETTLEMENT_PENDING"

    # The outbox advances the same durable receipt and acknowledges the inbound
    # before the caller's next retry reaches the daemon.
    inbox.settle(accepted=True)
    retried = app.handle("message.reply", request)

    assert retried == {
        "ok": True,
        "messageId": "message-1",
        "replyMessageId": "c4ff4b95-7cef-54b3-a843-1643bc36c56d",
        "replied": True,
        "acknowledged": True,
        "queued": False,
    }
    assert len(inbox.submitted) == 1
    assert inbox.acks == [(CANONICAL, "message-1")]


def test_lark_reply_retry_returns_same_pending_receipt(tmp_path: Path) -> None:
    inbox = DurableReplyInbox(
        InboxMessage(
            message_id="message-1",
            conversation_id="conversation-1",
            sender="adapter:lark:e2e",
            recipient=CANONICAL,
            payload=json.dumps({"message": "durable body"}).encode(),
            intent="request",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=1,
        )
    )
    app = application(tmp_path, inbox)
    register(app)
    request = {
        "actor": "alice",
        "sessionRef": "session-1",
        "messageId": "message-1",
        "message": "eventual reply",
        "summary": "eventual reply",
    }

    first = app.handle("message.reply", request)
    retried = app.handle("message.reply", request)

    assert retried == first
    assert retried["code"] == "REPLY_SETTLEMENT_PENDING"
    assert len(inbox.submitted) == 1


def test_lark_reply_retry_returns_terminal_receipt_code(tmp_path: Path) -> None:
    inbox = DurableReplyInbox(
        InboxMessage(
            message_id="message-1",
            conversation_id="conversation-1",
            sender="adapter:lark:e2e",
            recipient=CANONICAL,
            payload=json.dumps({"message": "durable body"}).encode(),
            intent="request",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=1,
        )
    )
    app = application(tmp_path, inbox)
    register(app)
    request = {
        "actor": "alice",
        "sessionRef": "session-1",
        "messageId": "message-1",
        "message": "eventual reply",
        "summary": "eventual reply",
    }

    app.handle("message.reply", request)
    inbox.settle(accepted=False, code="DELIVERY_RETRY_EXHAUSTED")
    retried = app.handle("message.reply", request)

    assert retried == {
        "ok": False,
        "messageId": "message-1",
        "replyMessageId": "c4ff4b95-7cef-54b3-a843-1643bc36c56d",
        "replied": False,
        "acknowledged": False,
        "queued": False,
        "code": "DELIVERY_RETRY_EXHAUSTED",
    }
    assert len(inbox.submitted) == 1
    assert inbox.acks == []


def test_reply_retry_with_different_text_after_settlement_is_rejected(
    tmp_path: Path,
) -> None:
    inbox = DurableReplyInbox(
        InboxMessage(
            message_id="message-1",
            conversation_id="conversation-1",
            sender="adapter:lark:e2e",
            recipient=CANONICAL,
            payload=json.dumps({"message": "durable body"}).encode(),
            intent="request",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=1,
        )
    )
    app = application(tmp_path, inbox)
    register(app)

    app.handle(
        "message.reply",
        {
            "actor": "alice",
            "sessionRef": "session-1",
            "messageId": "message-1",
            "message": "first answer",
            "summary": "answer",
        },
    )
    inbox.settle(accepted=True)
    retried = app.handle(
        "message.reply",
        {
            "actor": "alice",
            "sessionRef": "session-1",
            "messageId": "message-1",
            "message": "different answer",
            "summary": "answer",
        },
    )

    assert retried["code"] == "REPLY_ALREADY_SUBMITTED"
    assert retried["replyMessageId"] == "c4ff4b95-7cef-54b3-a843-1643bc36c56d"
    assert retried["retryable"] is False
    assert len(inbox.submitted) == 1
    assert inbox.acks == [(CANONICAL, "message-1")]


@pytest.mark.parametrize("durable_original", (False, True))
def test_reply_without_this_actors_durable_receipt_stays_unavailable(
    tmp_path: Path,
    durable_original: bool,
) -> None:
    inbox = DurableReplyInbox(pending_message())
    inbox.visible = False
    if not durable_original:
        inbox.pending = InboxMessage(
            message_id="some-other-message",
            conversation_id="conversation-1",
            sender="agent:op:node-2:bob",
            recipient=CANONICAL,
            payload=b"{}",
            intent="request",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=1,
        )
    app = application(tmp_path, inbox)
    register(app)

    with pytest.raises(DaemonRequestError) as failure:
        app.handle(
            "message.reply",
            {
                "actor": "alice",
                "sessionRef": "session-1",
                "messageId": "message-1",
                "message": "late answer",
                "summary": "late answer",
            },
        )

    assert failure.value.code == "MESSAGE_REPLY_UNAVAILABLE"
    assert inbox.submitted == []


def test_ambiguous_lark_reply_is_retryable_and_acks_only_after_settlement(
    tmp_path: Path,
) -> None:
    class PendingThenSettledInbox(FakeInbox):
        def __init__(self, pending: InboxMessage) -> None:
            super().__init__(pending)
            self.attempts = 0

        def submit(self, message: InboxMessage, **_link: object) -> SubmissionResult:
            self.attempts += 1
            self.submitted.append(message)
            if self.attempts == 1:
                raise InboxAuthorityTimeout("still settling")
            return SubmissionResult(message.message_id, accepted=True, queued=False)

    inbox = PendingThenSettledInbox(
        InboxMessage(
            message_id="message-1",
            conversation_id="conversation-1",
            sender="adapter:lark:e2e",
            recipient=CANONICAL,
            payload=json.dumps({"message": "durable body"}).encode(),
            intent="request",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=1,
        )
    )
    app = application(tmp_path, inbox)
    register(app)
    request = {
        "actor": "alice",
        "sessionRef": "session-1",
        "messageId": "message-1",
        "message": "eventual reply",
        "summary": "eventual reply",
    }

    first = app.handle("message.reply", request)
    assert first["code"] == "REPLY_SETTLEMENT_PENDING"
    assert first["retryable"] is True
    assert first["replied"] is False and first["acknowledged"] is False
    assert inbox.acks == []

    second = app.handle("message.reply", request)
    assert second["replied"] is True and second["acknowledged"] is True
    assert inbox.acks == [(CANONICAL, "message-1")]
    assert inbox.attempts == 2


def test_different_text_retry_acks_inbound_when_existing_reply_is_settled(
    tmp_path: Path,
) -> None:
    class SettledThenConflictInbox(FakeInbox):
        def submit(self, message: InboxMessage, **_link: object) -> SubmissionResult:
            if self.submitted:
                raise InboxAuthorityUnavailable(
                    "SUBMISSION_RECEIPT_CONFLICT: correlation_id is already "
                    "settled for a different inbox command"
                )
            self.submitted.append(message)
            return SubmissionResult(message.message_id, accepted=True, queued=True)

        def reply_submission_result(self, message_id: str) -> SubmissionResult:
            return SubmissionResult(message_id, accepted=True, queued=False)

    inbox = SettledThenConflictInbox(
        InboxMessage(
            message_id="message-1",
            conversation_id="conversation-1",
            sender="adapter:lark:e2e",
            recipient=CANONICAL,
            payload=json.dumps({"message": "durable body"}).encode(),
            intent="request",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=1,
        )
    )
    app = application(tmp_path, inbox)
    register(app)

    app.handle(
        "message.reply",
        {
            "actor": "alice",
            "sessionRef": "session-1",
            "messageId": "message-1",
            "message": "first answer",
            "summary": "answer",
        },
    )
    result = app.handle(
        "message.reply",
        {
            "actor": "alice",
            "sessionRef": "session-1",
            "messageId": "message-1",
            "message": "different answer",
            "summary": "answer",
        },
    )

    assert result == {
        "ok": False,
        "messageId": "message-1",
        "replyMessageId": "c4ff4b95-7cef-54b3-a843-1643bc36c56d",
        "replied": True,
        "acknowledged": True,
        "queued": False,
        "code": "REPLY_ALREADY_SUBMITTED",
        "retryable": False,
        "settled": True,
    }
    assert len(inbox.submitted) == 1
    assert inbox.acks == [(CANONICAL, "message-1")]
