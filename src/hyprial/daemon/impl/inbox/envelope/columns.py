"""Envelope columns of ``inbox.sqlite3`` and the one stored-row decoder.

Decisions (cell model phase A, slice S2; merged plan §3):

* ``outbox``, ``inbox``, ``custody`` and ``sender_copies`` gain nullable
  ``thread_id``, ``conversation_cell_id`` and ``message_class`` TEXT columns.
  Each ALTER is guarded by ``PRAGMA table_info``, so a reopen is a no-op.
  There is no backfill: a legacy row keeps NULL, which reads back as the old
  semantics.  ``dlq``, ``system_notices`` and ``progress_events`` are
  unchanged.  Downgrade stays safe because older builds INSERT with explicit
  column lists.
* The stored value is the message's own envelope, never a derived class: a
  NULL ``message_class`` means "derive", exactly like ``None`` in memory, so a
  write and read round trip returns an equal message.
* Reads are lenient: a value the kernel grammar or ``MessageClass`` refuses
  reads back as None rather than failing the whole read.
* ``stored_message`` is the single decoder for rows of the durable message
  tables.  The service and the read-only authority projection used to carry
  identical copies; one decoder keeps every read path in step when a column
  is added.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from hyprial.daemon.impl.inbox.contracts.api import DeliveryLifecycle, InboxMessage
from hyprial.kernel import MessageClass, is_cell_id_segment, is_thread_id_segment

ENVELOPE_TABLES = ("outbox", "inbox", "custody", "sender_copies")
ENVELOPE_COLUMNS = ("thread_id", "conversation_cell_id", "message_class")
ENVELOPE_COLUMN_LIST = ", ".join(ENVELOPE_COLUMNS)
ENVELOPE_PLACEHOLDERS = ", ".join("?" for _ in ENVELOPE_COLUMNS)


def ensure_envelope_columns(db: sqlite3.Connection) -> None:
    """Add the envelope columns to every envelope table that lacks them."""

    for table in ENVELOPE_TABLES:
        present = {
            str(row[1]) for row in db.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for column in ENVELOPE_COLUMNS:
            if column not in present:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")


def envelope_values(message: InboxMessage) -> tuple[str | None, str | None, str | None]:
    """The column values for *message*, in ``ENVELOPE_COLUMNS`` order."""

    return (
        message.thread_id,
        message.conversation_cell_id,
        None if message.message_class is None else message.message_class.value,
    )


def _column(row: sqlite3.Row, name: str) -> Any:
    return row[name] if name in row.keys() else None


def _message_class(value: Any) -> MessageClass | None:
    if not isinstance(value, str):
        return None
    try:
        return MessageClass(value)
    except ValueError:
        return None


def envelope_fields(row: sqlite3.Row) -> dict[str, Any]:
    """``InboxMessage`` keyword arguments for the envelope of one stored row."""

    thread_id = _column(row, "thread_id")
    cell_id = _column(row, "conversation_cell_id")
    return {
        "thread_id": (
            thread_id
            if isinstance(thread_id, str) and is_thread_id_segment(thread_id)
            else None
        ),
        "conversation_cell_id": (
            cell_id if isinstance(cell_id, str) and is_cell_id_segment(cell_id) else None
        ),
        "message_class": _message_class(_column(row, "message_class")),
    }


def stored_message(row: sqlite3.Row) -> InboxMessage:
    """Decode one row of any durable message table into an ``InboxMessage``.

    Columns a table does not carry decode to None: ``origin_node`` lives only
    on ``inbox``, ``summary`` is absent from ``progress_events``, and the
    envelope columns exist only on the envelope tables.
    """

    payload = row["payload"]
    expires_at_ms = _column(row, "message_expires_at_ms")
    origin_node = _column(row, "origin_node")
    summary = _column(row, "summary")
    return InboxMessage(
        message_id=str(row["message_id"]),
        conversation_id=str(row["conversation_id"]),
        sender=str(row["sender"]),
        recipient=str(row["recipient"]),
        payload=payload.encode("utf-8") if isinstance(payload, str) else bytes(payload),
        intent=str(row["intent"]),
        lifecycle=DeliveryLifecycle(str(row["lifecycle"])),
        idempotency_key=row["idempotency_key"],
        created_at_ms=int(row["created_at_ms"]),
        expires_at_ms=None if expires_at_ms is None else int(expires_at_ms),
        origin_node=None if origin_node is None else str(origin_node),
        summary=None if summary is None else str(summary),
        **envelope_fields(row),
    )


__all__ = [
    "ENVELOPE_COLUMNS",
    "ENVELOPE_COLUMN_LIST",
    "ENVELOPE_PLACEHOLDERS",
    "ENVELOPE_TABLES",
    "ensure_envelope_columns",
    "envelope_fields",
    "envelope_values",
    "stored_message",
]
