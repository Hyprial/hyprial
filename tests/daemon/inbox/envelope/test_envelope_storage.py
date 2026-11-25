"""S2: the local envelope persists in ``inbox.sqlite3`` and survives every read.

T1 pins the PRAGMA-guarded migration against a frozen copy of the
pre-envelope DDL (the code under test never builds the legacy file).  T2 pins
that a stamped message keeps its envelope through the inbox, outbox, custody
and sender-copy rows and through both the service reads and the read-only
authority projection.  Legacy rows read back as the old semantics (all None),
and a value the grammar refuses reads back as None instead of failing a read.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)
from hyprial.daemon.impl.inbox.authority.read import InboxReadProjection
from hyprial.daemon.impl.delivery.retention import BODY_COLUMNS, BODY_TABLES
from hyprial.kernel import MessageClass, mint_cell_id, mint_thread_id

THREAD = mint_thread_id(b"envelope-storage-thread")
CELL = mint_cell_id(b"envelope-storage-cell")
ENVELOPE_COLUMNS = ["thread_id", "conversation_cell_id", "message_class"]


def _body_columns(table: str) -> list[str]:
    """S5 appends the body release columns to inbox and sender copies only."""

    if table not in BODY_TABLES:
        return []
    names = [name for name, _kind in BODY_COLUMNS]
    return [*names, "body_settled_at_ms"] if table == "sender_copies" else names
RECIPIENT = "agent:op:node-1:reader"
SENDER = "agent:op:node-1:driver"

_PRE_ENVELOPE_DDL = """
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
    confirmation_reason TEXT,
    summary TEXT,
    link_hold TEXT
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
    origin_node TEXT,
    summary TEXT,
    arrival_ordinal INTEGER
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
    next_attempt_ms INTEGER NOT NULL,
    summary TEXT
);
CREATE TABLE sender_copies (
    message_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    sender TEXT NOT NULL,
    recipient TEXT NOT NULL,
    payload BLOB NOT NULL,
    intent TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    idempotency_key TEXT,
    created_at_ms INTEGER NOT NULL,
    message_expires_at_ms INTEGER
);
"""


def _message(message_id: str = "m-env", **envelope: object) -> InboxMessage:
    message = InboxMessage(
        message_id=message_id,
        conversation_id="c-env",
        sender=SENDER,
        recipient=RECIPIENT,
        payload=json.dumps({"message": "body"}).encode(),
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
        summary="S",
    )
    return message.with_envelope(**envelope) if envelope else message  # type: ignore[arg-type]


def _stamped(message_id: str = "m-env") -> InboxMessage:
    return _message(
        message_id,
        thread_id=THREAD,
        conversation_cell_id=CELL,
        message_class=MessageClass.SYSTEM,
    )


def _envelope(message: InboxMessage) -> tuple[object, object, object]:
    return (message.thread_id, message.conversation_cell_id, message.message_class)


STAMPED = (THREAD, CELL, MessageClass.SYSTEM)
LEGACY = (None, None, None)


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")]


class _ForwardingTransport(MemoryDeliveryTransport):
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


def test_the_four_tables_gain_nullable_envelope_columns(tmp_path: Path) -> None:
    database = tmp_path / "inbox.sqlite3"
    legacy = sqlite3.connect(database)
    legacy.executescript(_PRE_ENVELOPE_DDL)
    legacy.execute(
        """INSERT INTO inbox (message_id, conversation_id, sender, recipient,
               payload, intent, lifecycle, created_at_ms, received_at_ms)
           VALUES ('legacy', 'c-env', ?, ?, ?, 'request', 'durable_service', 1, 1)""",
        (SENDER, RECIPIENT, b'{"message":"legacy"}'),
    )
    legacy.commit()
    tables = ("outbox", "inbox", "custody", "sender_copies")
    before = {table: _columns(legacy, table) for table in tables}
    legacy.close()

    for _ in range(2):
        service = InboxService(database, MemoryDeliveryTransport())
        try:
            for table in tables:
                assert _columns(service._db, table) == [
                    *before[table], *ENVELOPE_COLUMNS, *_body_columns(table),
                ]
            for table in ("dlq", "system_notices"):
                assert not set(ENVELOPE_COLUMNS) & set(_columns(service._db, table))
            (row,) = service.pending_messages(RECIPIENT)
            assert row.message_id == "legacy"
            assert _envelope(row) == LEGACY
        finally:
            service.close()


def test_the_inbox_row_keeps_the_envelope(tmp_path: Path) -> None:
    database = tmp_path / "inbox.sqlite3"
    service = InboxService(database, MemoryDeliveryTransport())
    try:
        assert service.receive(_stamped()).acknowledged
        assert service.receive(_message("m-legacy")).acknowledged
        by_id = {row.message_id: row for row in service.pending_messages(RECIPIENT)}
        assert _envelope(by_id["m-env"]) == STAMPED
        assert _envelope(by_id["m-legacy"]) == LEGACY
        projected = {row.message_id: row for row in InboxReadProjection(database).pending_all()}
        assert _envelope(projected["m-env"]) == STAMPED
        assert _envelope(projected["m-legacy"]) == LEGACY
    finally:
        service.close()


def test_the_outbox_and_sender_copy_keep_the_envelope(tmp_path: Path) -> None:
    recipient = InboxService(tmp_path / "recipient.sqlite3", MemoryDeliveryTransport())
    transport = _ForwardingTransport(recipient)
    database = tmp_path / "sender.sqlite3"
    sender = InboxService(database, transport)
    try:
        assert sender.submit(_stamped()).accepted
        (held,) = sender.outbox_items()
        assert _envelope(held.message) == STAMPED
        (projected,) = InboxReadProjection(database).outbox_items()
        assert _envelope(projected.message) == STAMPED
        transport.forwarding = True
        sender.retry_due(now_ms=held.expires_at_ms - 1)
        assert sender.outbox_items() == ()
        (row,) = recipient.pending_messages(RECIPIENT)
        assert _envelope(row) == STAMPED
        (copy,) = InboxReadProjection(database).conversation_messages(
            participant=SENDER, conversation_id="c-env"
        )
        assert copy.message_id == "m-env"
        assert _envelope(copy) == STAMPED
        (local,) = InboxReadProjection(tmp_path / "recipient.sqlite3").conversation_messages(
            participant=RECIPIENT, conversation_id="c-env"
        )
        assert _envelope(local) == STAMPED
    finally:
        sender.close()
        recipient.close()


def test_the_custody_row_keeps_the_envelope(tmp_path: Path) -> None:
    recipient = InboxService(tmp_path / "recipient.sqlite3", MemoryDeliveryTransport())
    transport = _ForwardingTransport(recipient)
    mailbox = InboxService(tmp_path / "mailbox.sqlite3", transport)
    try:
        assert mailbox.accept_custody(_stamped(), mailbox_node="node-2").acknowledged
        row = mailbox._db.execute(
            "SELECT thread_id, conversation_cell_id, message_class FROM custody"
        ).fetchone()
        assert tuple(row) == (THREAD, CELL, "system")
        transport.forwarding = True
        mailbox.retry_custody_due()
        (delivered,) = recipient.pending_messages(RECIPIENT)
        assert _envelope(delivered) == STAMPED
    finally:
        mailbox.close()
        recipient.close()


def test_a_malformed_stored_value_reads_back_as_none(tmp_path: Path) -> None:
    database = tmp_path / "inbox.sqlite3"
    service = InboxService(database, MemoryDeliveryTransport())
    try:
        assert service.receive(_stamped()).acknowledged
        service._db.execute(
            "UPDATE inbox SET thread_id = 'x', conversation_cell_id = ?, "
            "message_class = 'loud'",
            (THREAD,),
        )
        service._db.commit()
        (row,) = service.pending_messages(RECIPIENT)
        assert _envelope(row) == LEGACY
        (projected,) = InboxReadProjection(database).pending_all()
        assert _envelope(projected) == LEGACY
    finally:
        service.close()
