"""T2/T10(A)/T11: the pending row the carrier reads.

A phase adds ``kind``/``summary``/``bodyBytes`` and keeps ``message``; the row
must never be derived from the body on the read side except in the A-phase
``message`` key.  System notices are not messages: their whole text is the
summary and they have no body to fetch.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hyprial.kernel import DaemonRequestError, ipc_errors
from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage

from ..support import (
    FakeInbox,
    FakeLinkGate,
    _close_application_domains as _close_application_domains,
    application,
    durable_application,
    pending_message,
    register,
    register_actor,
    system_notice,
)

CANONICAL = "agent:op:node-1:reader"

# The frozen pre-summary inbox DDL (branch baseline): a legacy row has no
# ``summary`` column until the WP3 migration adds one.
_LEGACY_INBOX_DDL = """
CREATE TABLE inbox (
    arrival_id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL UNIQUE,
    conversation_id TEXT NOT NULL,
    sender TEXT NOT NULL,
    recipient TEXT NOT NULL,
    payload BLOB NOT NULL,
    intent TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    idempotency_key TEXT,
    created_at_ms INTEGER NOT NULL,
    received_at_ms INTEGER NOT NULL,
    message_expires_at_ms INTEGER,
    expires_at_ms INTEGER,
    consumed INTEGER NOT NULL DEFAULT 0,
    acknowledged_at_ms INTEGER,
    fetched_at_ms INTEGER,
    origin_node TEXT
);
"""


def _seed_legacy_row(tmp_path: Path) -> None:
    database = sqlite3.connect(tmp_path / "inbox.sqlite3")
    database.executescript(_LEGACY_INBOX_DDL)
    database.execute(
        """INSERT INTO inbox (
               message_id, conversation_id, sender, recipient, payload, intent,
               lifecycle, created_at_ms, received_at_ms, consumed)
           VALUES ('legacy', 'c-legacy', 'user:op', ?, ?, 'request',
                   'durable_service', 1, 1, 0)""",
        (CANONICAL, b'{"message":"legacy"}'),
    )
    database.commit()
    database.close()


# The B-phase pending row: the §8.1 fields plus summary/bodyBytes, and no
# body.  The body is read on demand through ``message.read_body``.
_PENDING_ROW_KEYS = {
    "messageId",
    "deliveryId",
    "conversationId",
    "from",
    "to",
    "intent",
    "kind",
    "summary",
    "bodyBytes",
    "origin",
}


def _row(app, **params: object) -> dict[str, object]:  # type: ignore[no-untyped-def]
    listed = app.handle(
        "message.pending.list",
        {"actor": "reader", "sessionRef": "session-1", **params},
    )
    (row,) = listed["messages"]
    return row


def test_a_pre_summary_row_reads_as_null_summary(tmp_path: Path) -> None:
    _seed_legacy_row(tmp_path)
    app = durable_application(tmp_path, actors=("reader",))
    register_actor(app, "reader")

    row = _row(app)

    assert set(row) == _PENDING_ROW_KEYS
    assert row["kind"] == "message"
    assert row["summary"] is None
    assert row["bodyBytes"] == len("legacy".encode())

    body = app.handle(
        "message.read_body",
        {"actor": "reader", "sessionRef": "session-1", "messageId": "legacy"},
    )
    assert body["message"] == "legacy"
    assert body["summary"] is None
    assert body["pending"] is True


def _seed_live_row(app, *, message_id: str, summary: str, body: str, sender: str) -> None:  # type: ignore[no-untyped-def]
    assert app._inbox.receive(
        InboxMessage(
            message_id=message_id,
            conversation_id=f"c-{message_id}",
            sender=sender,
            recipient=CANONICAL,
            payload=json.dumps({"message": body}).encode(),
            intent="request",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=1,
            summary=summary,
        )
    ).accepted


def test_the_row_carries_the_summary_and_body_size_without_the_body(
    tmp_path: Path,
) -> None:
    app = durable_application(tmp_path, actors=("reader", "driver"))
    register_actor(app, "reader")
    app.link_admission.bind(FakeLinkGate())
    _seed_live_row(
        app,
        message_id="m-1",
        summary="SUM-SENTINEL",
        body="BODY-SENTINEL",
        sender="agent:op:node-1:driver",
    )

    row = _row(app)

    assert set(row) == _PENDING_ROW_KEYS
    assert row["kind"] == "message"
    assert row["summary"] == "SUM-SENTINEL"
    assert row["bodyBytes"] == len("BODY-SENTINEL".encode())
    # The body never leaks through the row (the key whitelist above excludes
    # ``message``/``payload``); a fetched read has the identical shape.
    fetched_row = _row(app, fetched=True)
    assert set(fetched_row) == _PENDING_ROW_KEYS
    assert fetched_row["summary"] == "SUM-SENTINEL"
    # A legal case the design allows: the summary may equal the body.
    _seed_live_row(
        app,
        message_id="m-2",
        summary="same text",
        body="same text",
        sender="agent:op:node-1:driver",
    )
    (_, equal) = app.handle(
        "message.pending.list",
        {"actor": "reader", "sessionRef": "session-1"},
    )["messages"]
    assert equal["summary"] == "same text"
    assert equal["bodyBytes"] == len("same text".encode())


def test_a_notice_row_carries_its_whole_text_and_no_body(tmp_path: Path) -> None:
    notice = system_notice()
    inbox = FakeInbox(pending_message(), (notice,))
    app = application(tmp_path, inbox)
    register(app)

    listed = app.handle(
        "message.pending.list", {"actor": "alice", "sessionRef": "session-1"}
    )

    rows = {row["messageId"]: row for row in listed["messages"]}
    notice_row = rows["notice-1"]
    assert notice_row["kind"] == "notice"
    assert notice_row["summary"] == json.loads(notice.payload)["message"]
    assert notice_row["bodyBytes"] == 0

    with pytest.raises(DaemonRequestError) as error:
        app.handle(
            "message.read_body",
            {"actor": "alice", "sessionRef": "session-1", "messageId": "notice-1"},
        )
    assert error.value.code == ipc_errors.MESSAGE_BODY_UNAVAILABLE
    assert error.value.data == {"messageId": "notice-1"}
