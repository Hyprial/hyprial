"""Durable conversation history over the existing inbox/outbox rows.

One conversation read spans both directions of this node's mail:

* ``inbox`` retains every delivery this node accepted, including rows the
  recipient has already consumed (``consumed = 1``).  A locally delivered
  send leaves one row here whose ``sender`` is the caller, so the caller's
  own outgoing copy is readable too.
* ``outbox`` holds sends that have not been delivered yet.  A row that has
  been delivered is deleted, so it is projected exactly once from the inbox.
* ``sender_copies`` retains one sender-owned copy of each durable outbound
  message.  A delivery to another node never leaves a local inbox row, and
  delivery deletes the outbox row; this table is what keeps the caller's own
  sent message readable after it.  It is only ever filtered by
  ``sender = participant``: a recipient reads its own inbox instead.

Only the two conversational intents are selected.  ``system_notices`` and
``progress_events`` live in separate tables and are never read here.  The
read is a plain ``SELECT`` on a caller-supplied connection; it never fetches,
ACKs or drains anything.

Decision note (plan slice S1, ruling R6): a conversation group also carries
the class of its newest row, so ``conversations.list`` can name it for a
display filter.  The class comes from ``message_class_of`` over the newest
row's own fields (no SQL copy of the rules); the probe it classifies has an
empty payload and a placeholder lifecycle, which no class rule reads.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from hyprial.daemon.impl.inbox.contracts.api import DeliveryLifecycle, InboxMessage
from hyprial.daemon.impl.inbox.envelope.columns import envelope_fields
from hyprial.daemon.impl.inbox.envelope.rules import message_class_of
from hyprial.kernel import is_thread_id_segment

HistoryHydrator = Callable[[sqlite3.Row, InboxMessage], InboxMessage]
HISTORY_PAGE_MAX = 200
#: Read one extra row so the caller can decide whether a next page exists
#: without a second query.
HISTORY_READ_LIMIT_MAX = HISTORY_PAGE_MAX + 1

#: ``source_rank`` orders the three stored facts about one message id:
#: a retained inbox copy (0) is the two-ended delivery fact; a pending outbox
#: row (1) exists only until delivery; the sender-owned copy (2) outlives the
#: outbox row and carries the caller's side of a remote delivery.  A row is
#: projected only from its lowest-ranked source, so every message id appears
#: at most once.
#: The keyset bounds can be applied from either end.  ``after`` walks
#: forward (v1 pagination and the v2 ``after`` mode); ``before`` walks
#: backward (v2 ``tail`` and ``older``).  A page is always returned in the
#: requested direction so the caller can reverse tail/older pages into
#: stable oldest-first order.  ``{direction}`` is replaced with a literal
#: ``ASC``/``DESC``, never with caller input.
_HISTORY_QUERY_TEMPLATE = """
WITH candidates AS (
    SELECT 0 AS source_rank,
           message_id, conversation_id, sender, recipient, payload, intent,
           lifecycle, idempotency_key, created_at_ms, message_expires_at_ms,
           origin_node, thread_id, conversation_cell_id, message_class,
           arrival_id AS body_position, body_dropped_at_ms,
           body_payload_sha256, body_ref
      FROM inbox
     WHERE conversation_id = ?
       AND intent IN ('request', 'reply')
       AND (sender = ? OR recipient = ?)
    UNION ALL
    SELECT 1 AS source_rank,
           message_id, conversation_id, sender, recipient, payload, intent,
           lifecycle, idempotency_key, created_at_ms, message_expires_at_ms,
           NULL AS origin_node, thread_id, conversation_cell_id, message_class,
           NULL AS body_position, NULL AS body_dropped_at_ms,
           NULL AS body_payload_sha256, NULL AS body_ref
      FROM outbox
     WHERE conversation_id = ?
       AND intent IN ('request', 'reply')
       AND (sender = ? OR recipient = ?)
    UNION ALL
    SELECT 2 AS source_rank,
           message_id, conversation_id, sender, recipient, payload, intent,
           lifecycle, idempotency_key, created_at_ms, message_expires_at_ms,
           NULL AS origin_node, thread_id, conversation_cell_id, message_class,
           rowid AS body_position, body_dropped_at_ms,
           body_payload_sha256, body_ref
      FROM sender_copies
     WHERE conversation_id = ?
       AND intent IN ('request', 'reply')
       AND sender = ?
)
SELECT * FROM candidates AS candidate
 WHERE (candidate.source_rank = 0
        OR (candidate.source_rank = 1
            AND NOT EXISTS (
                SELECT 1 FROM inbox AS retained
                 WHERE retained.message_id = candidate.message_id
            ))
        OR (candidate.source_rank = 2
            AND NOT EXISTS (
                SELECT 1 FROM inbox AS retained
                 WHERE retained.message_id = candidate.message_id
            )
            AND NOT EXISTS (
                SELECT 1 FROM outbox AS pending
                 WHERE pending.message_id = candidate.message_id
            )))
   AND (? IS NULL
        OR created_at_ms > ?
        OR (created_at_ms = ? AND message_id > ?))
   AND (? IS NULL
        OR created_at_ms < ?
        OR (created_at_ms = ? AND message_id < ?))
 ORDER BY created_at_ms {direction}, message_id {direction}
 LIMIT ?
"""
_HISTORY_QUERY_ASC = _HISTORY_QUERY_TEMPLATE.format(direction="ASC")
_HISTORY_QUERY_DESC = _HISTORY_QUERY_TEMPLATE.format(direction="DESC")

_GROUPS_QUERY = """
WITH candidates AS (
    SELECT 0 AS source_rank, message_id, conversation_id, sender, recipient,
           created_at_ms, thread_id, intent, idempotency_key, message_class
      FROM inbox
     WHERE intent IN ('request', 'reply') AND (sender = ? OR recipient = ?)
    UNION ALL
    SELECT 1 AS source_rank, message_id, conversation_id, sender, recipient,
           created_at_ms, thread_id, intent, idempotency_key, message_class
      FROM outbox
     WHERE intent IN ('request', 'reply') AND (sender = ? OR recipient = ?)
    UNION ALL
    SELECT 2 AS source_rank, message_id, conversation_id, sender, recipient,
           created_at_ms, thread_id, intent, idempotency_key, message_class
      FROM sender_copies
     WHERE intent IN ('request', 'reply') AND sender = ?
), visible AS (
    SELECT * FROM candidates AS candidate
     WHERE candidate.source_rank = 0
        OR (candidate.source_rank = 1
            AND NOT EXISTS (
                SELECT 1 FROM inbox AS retained
                 WHERE retained.message_id = candidate.message_id
            ))
        OR (candidate.source_rank = 2
            AND NOT EXISTS (
                SELECT 1 FROM inbox AS retained
                 WHERE retained.message_id = candidate.message_id
            )
            AND NOT EXISTS (
                SELECT 1 FROM outbox AS pending
                 WHERE pending.message_id = candidate.message_id
            ))
), ranked AS (
    SELECT conversation_id, sender, recipient, thread_id, created_at_ms,
           message_id, intent, idempotency_key, message_class,
           COUNT(*) OVER grouped AS messages,
           ROW_NUMBER() OVER (
               PARTITION BY conversation_id, sender, recipient, thread_id
               ORDER BY created_at_ms DESC, message_id DESC
           ) AS newest
      FROM visible
    WINDOW grouped AS (
        PARTITION BY conversation_id, sender, recipient, thread_id
    )
)
SELECT conversation_id, sender, recipient, created_at_ms, message_id, messages,
       thread_id, intent, idempotency_key, message_class
  FROM ranked
 WHERE newest = 1
 ORDER BY conversation_id, sender, recipient, thread_id
"""


@dataclass(frozen=True, slots=True)
class ConversationGroup:
    """One (legacy conversation, sender, recipient, stored thread) group.

    ``thread_id`` is the row's stored ``thread_id`` envelope column when it
    holds a ``t.`` id, else None (the reader then infers the thread from the
    two ends, as the recorder does).  ``message_class`` is the class value
    of the newest row.
    """

    conversation_id: str
    sender: str
    recipient: str
    last_created_at_ms: int
    last_message_id: str
    messages: int
    thread_id: str | None = None
    message_class: str | None = None


def _newest_class(row: Any) -> str:
    stamp = envelope_fields({"message_class": row[9]})["message_class"]
    probe = InboxMessage(
        message_id=str(row[4]),
        conversation_id=str(row[0]),
        sender=str(row[1]),
        recipient=str(row[2]),
        payload=b"",
        intent=str(row[7]),
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=int(row[3]),
        idempotency_key=row[8] if isinstance(row[8], str) else None,
        message_class=stamp,
    )
    return message_class_of(probe).value


def conversation_groups(
    connection: sqlite3.Connection, *, participant: str
) -> tuple[ConversationGroup, ...]:
    """Every conversational group *participant* is one end of.

    The rows are the ones ``conversation_messages`` would page for each
    legacy conversation (the same intents and source ranks), so a message
    stored both as an inbox row and as a sender copy on this node counts
    once.  Rows are grouped by their stored ``thread_id`` too, so a stamped
    thread is never folded into the inferred one.  Each group carries its
    row count, its newest row by ``(created_at_ms, message_id)`` and that
    row's class; no payload is read.
    """

    if not isinstance(participant, str) or not participant:
        raise ValueError("participant must be a non-empty string")
    rows = connection.execute(_GROUPS_QUERY, (participant,) * 5).fetchall()
    return tuple(
        ConversationGroup(
            conversation_id=str(row[0]),
            sender=str(row[1]),
            recipient=str(row[2]),
            last_created_at_ms=int(row[3]),
            last_message_id=str(row[4]),
            messages=int(row[5]),
            thread_id=(
                row[6]
                if isinstance(row[6], str) and is_thread_id_segment(row[6])
                else None
            ),
            message_class=_newest_class(row),
        )
        for row in rows
    )


def conversation_messages(
    connection: sqlite3.Connection,
    *,
    participant: str,
    conversation_id: str,
    after: tuple[int, str] | None = None,
    before: tuple[int, str] | None = None,
    limit: int = 50,
    newest_first: bool = False,
    hydrate: HistoryHydrator | None = None,
) -> tuple[InboxMessage, ...]:
    """Return one bounded, stable page of a participant's conversation.

    ``after`` and ``before`` are keyset positions
    ``(created_at_ms, message_id)``; they are mutually exclusive and both
    strict.  ``limit`` is the page size (the caller may ask for
    ``HISTORY_PAGE_MAX + 1`` to detect a following page).
    ``newest_first`` selects the direction of the walk: it returns the
    newest rows (descending) that satisfy the bounds, which the caller
    reverses when it needs oldest-first presentation.  A row is visible
    only when the conversation matches and the participant is one of its
    two ends.  ``hydrate``, when given, restores a released body from the
    row's ``body_*`` columns; without it every row is returned as stored.
    """

    if not isinstance(participant, str) or not participant:
        raise ValueError("participant must be a non-empty string")
    if not isinstance(conversation_id, str) or not conversation_id:
        raise ValueError("conversation_id must be a non-empty string")
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or limit < 1
        or limit > HISTORY_READ_LIMIT_MAX
    ):
        raise ValueError(f"limit must be an integer in 1..{HISTORY_READ_LIMIT_MAX}")
    if not isinstance(newest_first, bool):
        raise ValueError("newest_first must be a boolean")
    if after is not None and before is not None:
        raise ValueError("after and before are mutually exclusive")
    after_ms, after_id = _history_position(after, name="after")
    before_ms, before_id = _history_position(before, name="before")

    rows = connection.execute(
        _HISTORY_QUERY_DESC if newest_first else _HISTORY_QUERY_ASC,
        (
            conversation_id,
            participant,
            participant,
            conversation_id,
            participant,
            participant,
            conversation_id,
            participant,
            after_ms,
            after_ms,
            after_ms,
            after_id,
            before_ms,
            before_ms,
            before_ms,
            before_id,
            limit,
        ),
    ).fetchall()
    if hydrate is None:
        return tuple(history_message(row) for row in rows)
    return tuple(hydrate(row, history_message(row)) for row in rows)


def _history_position(
    value: tuple[int, str] | None, *, name: str
) -> tuple[int | None, str | None]:
    if value is None:
        return None, None
    if not isinstance(value, tuple) or len(value) != 2:
        raise ValueError(f"{name} must be a (created_at_ms, message_id) pair")
    position_ms, position_id = value
    if (
        isinstance(position_ms, bool)
        or not isinstance(position_ms, int)
        or position_ms < 0
    ):
        raise ValueError(
            f"{name} created_at_ms must be a non-negative integer"
        )
    if not isinstance(position_id, str) or not position_id:
        raise ValueError(f"{name} message_id must be a non-empty string")
    return position_ms, position_id


def history_message(row: sqlite3.Row) -> InboxMessage:
    """Decode one inbox/outbox/sender-copy row selected by the query."""

    message_expires_at_ms = row["message_expires_at_ms"]
    origin_node = row["origin_node"]
    return InboxMessage(
        message_id=str(row["message_id"]),
        conversation_id=str(row["conversation_id"]),
        sender=str(row["sender"]),
        recipient=str(row["recipient"]),
        payload=bytes(row["payload"]),
        intent=str(row["intent"]),
        lifecycle=DeliveryLifecycle(str(row["lifecycle"])),
        created_at_ms=int(row["created_at_ms"]),
        idempotency_key=row["idempotency_key"],
        origin_node=None if origin_node is None else str(origin_node),
        expires_at_ms=(
            None if message_expires_at_ms is None else int(message_expires_at_ms)
        ),
        **envelope_fields(row),
    )
