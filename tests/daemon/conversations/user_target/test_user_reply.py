"""UA2: a reply to the owner is delivered locally, recorded and consumed.

Before UA2 the reply row to ``user:op`` sat in the outbox with no consumer
until the hold TTL moved it to dead-letter with a fail-loud alarm.  Now the
row is delivered to this node's ``user:op`` actor, recorded, and the
recorder's observer acknowledges it through ``message.ack``.

UA2b: past the hold TTL the inbox builds an "expired unread" notice for the
never-fetched control row (ALPHA to BETA) and never for the acked reply
(item 10b).  A pass whose acknowledgement failed reports itself failed for
the recorder's backoff, so the retry test drives passes itself instead of
``settle`` (item 5).
"""

from __future__ import annotations

import json
import time

from hyprial.daemon.impl.inbox import DEFAULT_HOLD_TTL_MS, DeliveryLifecycle, InboxMessage
from tests.daemon.acceptance.acceptance_support import HOUR_MS, session
from tests.daemon.conversations.bypass.bypass_support import settle, stored
from tests.daemon.conversations.user_target.user_target_support import (
    boot_with_user_agent,
    operator_send,
    record_ends,
)
from tests.support.golden.app import ALPHA, OWNER_USER, golden_daemon as golden_daemon


def _reply(app, message_id: str, text: str = "answer for the owner") -> dict:
    return app.handle(
        "message.reply",
        {**session(ALPHA), "messageId": message_id, "message": text, "summary": "answer"},
    )


def settled_status(booted, message_id: str) -> dict[str, str]:
    """The recorder index status of each source row of one message."""

    from contextlib import closing
    import sqlite3

    from hyprial.daemon.impl.conversations import RECORDS_DATABASE

    with closing(sqlite3.connect(booted.app.state_dir / RECORDS_DATABASE)) as db:
        rows = db.execute("SELECT source, position, payload_sha256, status FROM records").fetchall()
    found = {}
    for source in ("inbox", "sender_copies"):
        for row in booted.app._inbox.interaction_rows_after(source, 0, limit=256).rows:
            if row.message.message_id == message_id:
                for stored_source, position, digest, status in rows:
                    if (stored_source, position, digest) == (source, row.position, row.payload_sha256):
                        found[source] = status
    return found


def _outbox_ids(app) -> set[str]:
    return {item.message.message_id for item in app._inbox.outbox_items()}


def test_reply_to_the_owner_is_recorded_acked_and_never_dead_lettered(
    golden_daemon, monkeypatch
) -> None:
    booted = boot_with_user_agent(golden_daemon, monkeypatch)
    app = booted.app
    question = operator_send(app, ALPHA, "question")["messageId"]

    replied = _reply(app, question)

    assert (replied["replied"], replied["acknowledged"]) == (True, True)
    reply_id = replied["replyMessageId"]
    assert reply_id not in _outbox_ids(app)
    assert [m.message_id for m in app._inbox.pending_messages(OWNER_USER)] == [reply_id]
    settled = settle(booted)
    assert settled.sources(reply_id) == ["inbox", "sender_copies"]
    assert record_ends(app, reply_id) == {
        ("inbox", ALPHA, OWNER_USER), ("sender_copies", ALPHA, OWNER_USER),
    }
    assert app._inbox.pending_messages(OWNER_USER) == ()

    noticed = _spy_expired_notices(monkeypatch)
    booted.daemon.clock.advance(DEFAULT_HOLD_TTL_MS + HOUR_MS)
    now = booted.daemon.clock.ms()
    app._inbox.retry_due(now_ms=now)
    pruned = app._inbox.prune_inbox(now_ms=now)
    assert reply_id not in {item.message_id for item in pruned}
    assert reply_id not in _outbox_ids(app)
    assert booted.control in noticed
    assert reply_id not in noticed


def _spy_expired_notices(monkeypatch) -> list[str]:
    """Message ids the inbox asked to build an "expired unread" notice for."""

    from hyprial.daemon.impl.inbox.service.delivery.notices import InboxServiceNoticesMixin

    noticed: list[str] = []
    real = InboxServiceNoticesMixin._expired_sender_notice

    def spy(self, row, now_ms):
        noticed.append(str(row["message_id"]))
        return real(self, row, now_ms)

    monkeypatch.setattr(InboxServiceNoticesMixin, "_expired_sender_notice", spy)
    return noticed


def test_reply_to_another_machines_user_agent_lands_with_the_local_owner(
    golden_daemon, monkeypatch
) -> None:
    """The injected row stands for a message received from the owner's other
    instance; no link service (an unbound slot accepts ingress), since a bound
    gate refuses an unlinked agent sender as LINK_UNKNOWN."""

    app = golden_daemon(user_agent=True, links=False).app
    instance = "agent:op:node-9:hyprial-user"
    app._inbox.receive(InboxMessage(
        message_id="from-other-instance",
        conversation_id="conv-instance",
        sender=instance,
        recipient=ALPHA,
        payload=json.dumps({"message": "hello from my laptop"}).encode(),
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        idempotency_key="instance-1",
        created_at_ms=time.time_ns() // 1_000_000,
        summary="hello",
    ))

    replied = _reply(app, "from-other-instance")

    rows = stored(app, replied["replyMessageId"])
    assert rows["inbox"]["recipient"] == OWNER_USER
    assert rows["inbox"]["conversation_id"] == "conv-instance"


def test_a_placeholder_record_is_not_acknowledged(golden_daemon, monkeypatch) -> None:
    """S2: a row recorded without its body keeps its body, so it stays queued."""

    booted = boot_with_user_agent(golden_daemon, monkeypatch)
    app = booted.app
    question = operator_send(app, ALPHA, "redacted-question")["messageId"]
    reply_id = _reply(app, question)["replyMessageId"]
    monkeypatch.setattr(
        booted.recorder, "_redact",
        lambda draft: None if draft.message.message_id == reply_id else draft,
    )
    settle(booted)
    assert settled_status(booted, reply_id) == {"inbox": "redacted", "sender_copies": "redacted"}

    assert [m.message_id for m in app._inbox.pending_messages(OWNER_USER)] == [reply_id]
    booted.daemon.clock.advance(DEFAULT_HOLD_TTL_MS + HOUR_MS)
    pruned = app._inbox.prune_inbox(now_ms=booted.daemon.clock.ms())
    assert {(item.message_id, item.reason) for item in pruned if item.recipient == OWNER_USER} == {
        (reply_id, "TTL_EXPIRED")
    }


def test_a_recorded_row_left_unacknowledged_is_acknowledged_at_the_next_start(
    golden_daemon, monkeypatch
) -> None:
    """The observer is replayed once per recorder: a stop between record and ack heals."""

    booted = boot_with_user_agent(golden_daemon, monkeypatch)
    app = booted.app
    observer = booted.recorder._observer
    monkeypatch.setattr(booted.recorder, "_observer", None)
    question = operator_send(app, ALPHA, "crash-question")["messageId"]
    reply_id = _reply(app, question)["replyMessageId"]
    settle(booted)
    assert [m.message_id for m in app._inbox.pending_messages(OWNER_USER)] == [reply_id]

    monkeypatch.setattr(booted.recorder, "_observer", observer)
    monkeypatch.setattr(booted.recorder, "_replayed", False)
    booted.recorder.run_once()

    assert app._inbox.pending_messages(OWNER_USER) == ()


def test_a_failed_ack_is_retried_on_the_next_pass(golden_daemon, monkeypatch) -> None:
    booted = boot_with_user_agent(golden_daemon, monkeypatch)
    app = booted.app
    question = operator_send(app, ALPHA, "flaky-ack-question")["messageId"]
    reply_id = _reply(app, question)["replyMessageId"]
    real_handle = app.handle
    refused: list[str] = []

    def refuse_first_ack(method, params, **kwargs):
        if method == "message.ack" and not refused:
            refused.append(params["messageId"])
            return {"ok": False, "messageId": params["messageId"], "acknowledged": False}
        return real_handle(method, params, **kwargs)

    monkeypatch.setattr(app, "handle", refuse_first_ack)
    for _ in range(16):
        booted.recorder.run_once()
    assert refused == [reply_id]

    assert app._inbox.pending_messages(OWNER_USER) == ()


def test_a_message_from_the_owner_to_the_owner_is_recorded_and_acked(
    golden_daemon, monkeypatch
) -> None:
    booted = boot_with_user_agent(golden_daemon, monkeypatch)
    app = booted.app

    answer = operator_send(app, OWNER_USER, "self-note")
    settle(booted)

    assert record_ends(app, answer["messageId"]) == {
        ("inbox", OWNER_USER, OWNER_USER), ("sender_copies", OWNER_USER, OWNER_USER),
    }
    assert app._inbox.pending_messages(OWNER_USER) == ()
