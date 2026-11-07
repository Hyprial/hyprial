"""WP3-A: the three durable tables gain ``summary`` without losing a byte.

T1 pins the migration against the frozen pre-summary DDL (the table text the
branch baseline wrote) and T3 pins that a sender's summary survives the inbox,
outbox-retry and custody paths into the recipient's inbox row.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)
from hyprial.daemon.impl.inbox.links.zenoh_link import (
    decode_delivery_frame,
    encode_delivery_frame,
)

# The exact pre-summary DDL (branch baseline ``2120854e3``), deliberately a
# frozen copy: the migration test must not build its legacy database from the
# code under test.
_LEGACY_DDL = """
CREATE TABLE outbox (
    message_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    sender TEXT NOT NULL,
    recipient TEXT NOT NULL,
    payload BLOB NOT NULL,
    intent TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    idempotency_key TEXT,
    created_at_ms INTEGER NOT NULL,
    message_expires_at_ms INTEGER,
    expires_at_ms INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_ms INTEGER NOT NULL,
    last_error TEXT,
    retry_started_at_ms INTEGER,
    confirmation_started_at_ms INTEGER,
    confirmation_attempts INTEGER NOT NULL DEFAULT 0,
    confirmation_reason TEXT
);
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
CREATE TABLE custody (
    message_id TEXT PRIMARY KEY,
    mailbox_node TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    sender TEXT NOT NULL,
    recipient TEXT NOT NULL,
    payload BLOB NOT NULL,
    intent TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    idempotency_key TEXT,
    created_at_ms INTEGER NOT NULL,
    accepted_at_ms INTEGER NOT NULL,
    message_expires_at_ms INTEGER,
    expires_at_ms INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_ms INTEGER NOT NULL
);
"""


def _legacy_database(path: Path) -> sqlite3.Connection:
    database = sqlite3.connect(path)
    database.row_factory = sqlite3.Row
    database.executescript(_LEGACY_DDL)
    database.execute(
        """INSERT INTO outbox (
               message_id, conversation_id, sender, recipient, payload, intent,
               lifecycle, created_at_ms, expires_at_ms, next_attempt_ms)
           VALUES ('legacy-out', 'c-1', 'agent:a', 'agent:b', ?, 'request',
                   'durable_service', 1, 99999999999999, 1)""",
        (b'{"message":"legacy"}',),
    )
    database.execute(
        """INSERT INTO inbox (
               message_id, conversation_id, sender, recipient, payload, intent,
               lifecycle, created_at_ms, received_at_ms, consumed)
           VALUES ('legacy', 'c-1', 'user:op', 'agent:op:node-1:reader', ?,
                   'request', 'durable_service', 1, 1, 0)""",
        (b'{"message":"legacy"}',),
    )
    database.execute(
        """INSERT INTO custody (
               message_id, mailbox_node, conversation_id, sender, recipient,
               payload, intent, lifecycle, created_at_ms, accepted_at_ms,
               expires_at_ms, next_attempt_ms)
           VALUES ('legacy-custody', 'node-2', 'c-1', 'agent:a', 'agent:b', ?,
                   'request', 'durable_service', 1, 1, 99999999999999, 1)""",
        (b'{"message":"legacy"}',),
    )
    database.commit()
    return database


def _columns(service: InboxService, table: str) -> list[str]:
    return [
        str(row["name"])
        for row in service._db.execute(f"PRAGMA table_info({table})").fetchall()
    ]


def _message(summary: str | None, message_id: str = "m-summary") -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="c-1",
        sender="agent:op:node-1:driver",
        recipient="agent:op:node-1:reader",
        payload=json.dumps({"message": "body"}).encode(),
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
        summary=summary,
    )


class _ForwardingTransport(MemoryDeliveryTransport):
    """Deliver into a recipient inbox once ``forwarding`` is on.

    ``is_online`` stays true so the sender keeps the message in its outbox
    with an ordinary retry schedule, instead of parking it at its TTL.
    """

    def __init__(self, recipient: InboxService) -> None:
        super().__init__()
        self.recipient = recipient
        self.forwarding = False

    def is_online(self, recipient: str) -> bool:
        return True

    def deliver(self, message: InboxMessage) -> bool:
        if not self.forwarding:
            return False
        return self.recipient.receive(message).acknowledged


def test_the_three_tables_gain_summary_and_keep_legacy_rows(tmp_path: Path) -> None:
    database = tmp_path / "inbox.sqlite3"
    connection = _legacy_database(database)
    before = {
        table: _columns_from_connection(connection, table)
        for table in ("outbox", "inbox", "custody")
    }
    connection.close()

    service = InboxService(database, MemoryDeliveryTransport())
    try:
        for table in ("outbox", "inbox", "custody"):
            # Legacy columns keep their order; the migrations append their
            # columns (outbox also gains B1's ``link_hold``).
            added = {
                "summary",
                # Inbox batches need one durable arrival ordinal; outbox also
                # gains B1's link_hold.
                *(("link_hold",) if table == "outbox" else ()),
                *(("arrival_ordinal",) if table == "inbox" else ()),
            }
            columns = _columns(service, table)
            assert columns[: len(before[table])] == before[table]
            assert set(columns[len(before[table]) :]) == added
        # The legacy inbox row is still readable and its summary is NULL.
        (legacy,) = service.pending_messages("agent:op:node-1:reader")
        assert legacy.message_id == "legacy"
        assert legacy.summary is None
        assert service.outbox_items()[0].message.summary is None
        # Reopening must not repeat the ALTER (a second ALTER would raise).
        service.close()
        reopened = InboxService(database, MemoryDeliveryTransport())
        try:
            assert _columns(reopened, "inbox") == [
                *before["inbox"],
                "arrival_ordinal",
                "summary",
            ]
            assert reopened.pending_messages("agent:op:node-1:reader")[0].summary is None
        finally:
            reopened.close()
    finally:
        service.close()


def _columns_from_connection(connection: sqlite3.Connection, table: str) -> list[str]:
    return [
        str(row["name"])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    ]


def test_summary_survives_the_inbox_direct_path(tmp_path: Path) -> None:
    service = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())
    try:
        assert service.receive(_message("S")).acknowledged
        (row,) = service.pending_messages("agent:op:node-1:reader")
        assert row.summary == "S"
    finally:
        service.close()


def test_summary_survives_the_outbox_retry_path(tmp_path: Path) -> None:
    recipient = InboxService(tmp_path / "recipient.sqlite3", MemoryDeliveryTransport())
    transport = _ForwardingTransport(recipient)
    sender = InboxService(tmp_path / "sender.sqlite3", transport)
    try:
        assert sender.submit(_message("S")).accepted
        (held,) = sender.outbox_items()
        assert held.message.summary == "S"
        transport.forwarding = True
        sender.retry_due(now_ms=held.expires_at_ms - 1)
        (row,) = recipient.pending_messages("agent:op:node-1:reader")
        assert row.summary == "S"
    finally:
        sender.close()
        recipient.close()


def test_summary_survives_the_custody_path(tmp_path: Path) -> None:
    recipient = InboxService(tmp_path / "recipient.sqlite3", MemoryDeliveryTransport())
    transport = _ForwardingTransport(recipient)
    mailbox = InboxService(tmp_path / "mailbox.sqlite3", transport)
    try:
        mailbox.accept_custody(_message("S"), mailbox_node="node-2")
        transport.forwarding = True
        mailbox.retry_custody_due()
        (row,) = recipient.pending_messages("agent:op:node-1:reader")
        assert row.summary == "S"
    finally:
        mailbox.close()
        recipient.close()


# --- R2: the explicit request id survives outbox, custody, wire and inbox ---


def _reply_to_message(message_id: str, intent: str) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="c-reply",
        sender="agent:op:node-1:driver",
        recipient="agent:op:node-1:reader",
        payload=json.dumps(
            {"message": "reply body", "replyTo": "original-1"}
        ).encode(),
        intent=intent,
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
        summary="S-reply",
    )


@pytest.mark.parametrize("intent", ["reply", "request"], ids=["harness-reply", "send-reply-to"])
def test_an_explicit_reply_to_survives_every_durable_path(
    tmp_path: Path, intent: str
) -> None:
    message = _reply_to_message("reply-1", intent)

    decoded = decode_delivery_frame(encode_delivery_frame(message))
    assert json.loads(decoded.payload)["replyTo"] == "original-1"
    assert decoded.summary == "S-reply"

    direct = InboxService(tmp_path / "direct.sqlite3", MemoryDeliveryTransport())
    try:
        assert direct.receive(message).accepted
        (row,) = direct.pending_messages("agent:op:node-1:reader")
        assert json.loads(row.payload)["replyTo"] == "original-1"
        assert row.summary == "S-reply"
    finally:
        direct.close()

    retry_recipient = InboxService(
        tmp_path / "retry-recipient.sqlite3", MemoryDeliveryTransport()
    )
    transport = _ForwardingTransport(retry_recipient)
    sender = InboxService(tmp_path / "retry-sender.sqlite3", transport)
    try:
        assert sender.submit(message).accepted
        (held,) = sender.outbox_items()
        assert json.loads(held.message.payload)["replyTo"] == "original-1"
        transport.forwarding = True
        sender.retry_due(now_ms=held.expires_at_ms - 1)
        (row,) = retry_recipient.pending_messages("agent:op:node-1:reader")
        assert json.loads(row.payload)["replyTo"] == "original-1"
        assert row.summary == "S-reply"
    finally:
        sender.close()
        retry_recipient.close()

    custody_recipient = InboxService(
        tmp_path / "custody-recipient.sqlite3", MemoryDeliveryTransport()
    )
    custody_transport = _ForwardingTransport(custody_recipient)
    mailbox = InboxService(tmp_path / "mailbox.sqlite3", custody_transport)
    try:
        mailbox.accept_custody(message, mailbox_node="node-2")
        custody_transport.forwarding = True
        mailbox.retry_custody_due()
        (row,) = custody_recipient.pending_messages("agent:op:node-1:reader")
        assert json.loads(row.payload)["replyTo"] == "original-1"
        assert row.summary == "S-reply"
    finally:
        mailbox.close()
        custody_recipient.close()
