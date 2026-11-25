"""SYSTEM body residue by the code default (plan §2.5, executor decision Q18).

Decision notes (phase A, slice T):

- A SYSTEM row that is never granted has its body replaced by the residue
  once the row has been acknowledged (``inbox.acknowledged_at_ms``) or
  settled (a sender copy whose outbox and custody rows are gone) for
  ``DEFAULT_BODY_RETENTION_MS``.
  ``delivery.bodyRetentionMs`` never applies (Q9: no shorter SYSTEM
  default), and ``system_notices`` keep their own 90 d / 10 k prune.
- The class is decided in Python by the injected ``message_class_of`` over
  rows past the cutoff, never by a SQL rule list: S2 stores NULL for every
  unstamped row.  ``body_class`` is the "already classified" marker: every
  candidate the sweep looks at is stamped with its class, so an INTERACTION
  row that is never granted is looked at once and a full batch of them
  cannot starve a SYSTEM row behind it.  Nothing reads ``body_class``
  outside this package, so the stamp changes no read.
- A SYSTEM residue keeps ``body_payload_sha256`` and ``body_ref`` NULL.
  Hydration needs that digest, so a bound resolver is never asked, history
  shows the residue and ``retained_body`` answers None, which makes
  ``message.read_body`` raise its existing ``MESSAGE_BODY_UNAVAILABLE``
  for a body that is no longer readable.  ``body_text_sha256`` is written
  as for a released INTERACTION body, so ``message.send.lookup`` answers
  the same after the residue.
- Plan slice S1 (R6): the recorder records and grants SYSTEM rows between
  two addressed actors.  A granted row carries ``body_payload_sha256``;
  this sweep never selects or rewrites such a row, so its body is dropped
  by the grant regime (``retention.sweep_body_retention``) and hydrates
  from its record.  This sweep stays the backstop for SYSTEM rows that are
  never granted, so no SYSTEM body is kept forever.  The one loss it
  allows: a recorder more than the code default behind an acknowledged
  row finds the body already residue and cannot record it.
- A sender copy has no ack: the first sweep that sees it settled stamps
  ``body_settled_at_ms`` on a SYSTEM copy, and the residue follows one
  code-default window later.  An unacknowledged inbox row is never a
  candidate, so pending reads never change.
- A row the injected decoder or classifier rejects (a lifecycle from a
  newer build, a corrupt column) is stamped ``undecodable`` and its body is
  kept: it leaves the candidate indexes, so one such row can neither stop
  the prune that follows nor fill every later batch.  Only ``ValueError``,
  ``TypeError`` and ``LookupError`` are set aside; a SQLite error still
  aborts the step.
- Each step handles at most ``SWEEP_BATCH`` rows and writes no pending,
  outbox or custody state.  The settled-copy scan walks
  ``sender_copies_body_unclassified`` by ``created_at_ms`` with two
  primary-key NOT EXISTS probes, so copies still in the outbox at the head
  of that index are revisited each step: the cost is bounded by the outbox
  size, which is accepted.
"""

from __future__ import annotations

import sqlite3

from hyprial.daemon.impl.delivery.bodies import residue_payload, text_sha256
from hyprial.daemon.impl.delivery.ports import (
    DEFAULT_BODY_RETENTION_MS,
    SOURCE_INBOX,
    SOURCE_SENDER_COPY,
    ClassOf,
    EnvelopeLike,
    RowDecoder,
)
from hyprial.daemon.impl.delivery.retention import (
    POSITION_COLUMN,
    SWEEP_BATCH,
    row_select,
)
from hyprial.kernel import MessageClass

SYSTEM_BODY_CLASS = MessageClass.SYSTEM.value
UNDECODABLE_BODY_CLASS = "undecodable"
_REJECTED = (ValueError, TypeError, LookupError)
_SETTLED_COPY = (
    "NOT EXISTS (SELECT 1 FROM outbox WHERE outbox.message_id = sender_copies.message_id)"
    " AND NOT EXISTS (SELECT 1 FROM custody"
    " WHERE custody.message_id = sender_copies.message_id)"
)


def _stamp(db: sqlite3.Connection, source: str, position: int, value: str) -> None:
    db.execute(
        f"""UPDATE {source} SET body_class = ?
             WHERE {POSITION_COLUMN[source]} = ? AND body_class IS NULL""",
        (value, position),
    )


def _classified(
    row: sqlite3.Row, decode: RowDecoder, class_of: ClassOf
) -> tuple[EnvelopeLike, MessageClass] | None:
    try:
        message = decode(row)
        return message, class_of(message)
    except _REJECTED:
        return None


def _set_aside(db: sqlite3.Connection, source: str, position: int) -> None:
    db.execute(
        f"""UPDATE {source} SET body_class = ?
             WHERE {POSITION_COLUMN[source]} = ? AND body_dropped_at_ms IS NULL""",
        (UNDECODABLE_BODY_CLASS, position),
    )


def _residue(
    db: sqlite3.Connection, source: str, position: int, payload: bytes, now_ms: int
) -> int:
    return db.execute(
        f"""UPDATE {source}
               SET payload = ?, body_class = ?, body_dropped_at_ms = ?,
                   body_text_sha256 = ?
             WHERE {POSITION_COLUMN[source]} = ?
               AND body_dropped_at_ms IS NULL
               AND body_payload_sha256 IS NULL""",
        (residue_payload(payload), SYSTEM_BODY_CLASS, now_ms,
         text_sha256(payload), position),
    ).rowcount


def _sweep_inbox(
    db: sqlite3.Connection, cutoff_ms: int, now_ms: int,
    decode: RowDecoder, class_of: ClassOf, batch: int,
) -> int:
    dropped = 0
    with db:
        rows = db.execute(
            f"""{row_select(SOURCE_INBOX)}
                 WHERE body_class IS NULL AND body_dropped_at_ms IS NULL
                   AND acknowledged_at_ms IS NOT NULL
                   AND acknowledged_at_ms <= ? AND consumed = 1
                 ORDER BY acknowledged_at_ms
                 LIMIT ?""",
            (cutoff_ms, batch),
        ).fetchall()
        for row in rows:
            position = int(row["body_position"])
            classified = _classified(row, decode, class_of)
            if classified is None:
                _set_aside(db, SOURCE_INBOX, position)
                continue
            message, found = classified
            if found is MessageClass.SYSTEM:
                dropped += _residue(db, SOURCE_INBOX, position, message.payload, now_ms)
            else:
                _stamp(db, SOURCE_INBOX, position, found.value)
    return dropped


def _sweep_sender_copies(
    db: sqlite3.Connection, cutoff_ms: int, now_ms: int,
    decode: RowDecoder, class_of: ClassOf, batch: int,
) -> int:
    dropped = 0
    with db:
        due = db.execute(
            f"""{row_select(SOURCE_SENDER_COPY)}
                 WHERE body_class = ? AND body_dropped_at_ms IS NULL
                   AND body_payload_sha256 IS NULL
                   AND body_settled_at_ms IS NOT NULL
                   AND body_settled_at_ms <= ?
                 ORDER BY body_settled_at_ms
                 LIMIT ?""",
            (SYSTEM_BODY_CLASS, cutoff_ms, batch),
        ).fetchall()
        for row in due:
            position = int(row["body_position"])
            classified = _classified(row, decode, class_of)
            if classified is None:
                _set_aside(db, SOURCE_SENDER_COPY, position)
            elif classified[1] is MessageClass.SYSTEM:
                dropped += _residue(
                    db, SOURCE_SENDER_COPY, position, classified[0].payload, now_ms,
                )
        settled = db.execute(
            f"""{row_select(SOURCE_SENDER_COPY)}
                 WHERE body_class IS NULL AND body_dropped_at_ms IS NULL
                   AND {_SETTLED_COPY}
                 ORDER BY created_at_ms
                 LIMIT ?""",
            (batch,),
        ).fetchall()
        for row in settled:
            position = int(row["body_position"])
            classified = _classified(row, decode, class_of)
            if classified is None:
                _set_aside(db, SOURCE_SENDER_COPY, position)
                continue
            found = classified[1]
            if found is MessageClass.SYSTEM:
                db.execute(
                    """UPDATE sender_copies
                          SET body_class = ?, body_settled_at_ms = ?
                        WHERE rowid = ? AND body_class IS NULL""",
                    (SYSTEM_BODY_CLASS, now_ms, position),
                )
            else:
                _stamp(db, SOURCE_SENDER_COPY, position, found.value)
    return dropped


def sweep_system_residue(
    db: sqlite3.Connection,
    *,
    now_ms: int,
    decode: RowDecoder,
    class_of: ClassOf,
    retention_ms: int = DEFAULT_BODY_RETENTION_MS,
    batch: int = SWEEP_BATCH,
) -> int:
    """Replace SYSTEM bodies acked or settled ``retention_ms`` ago by residue."""

    cutoff_ms = now_ms - retention_ms
    return _sweep_inbox(db, cutoff_ms, now_ms, decode, class_of, batch) + (
        _sweep_sender_copies(db, cutoff_ms, now_ms, decode, class_of, batch)
    )


__all__ = [
    "SYSTEM_BODY_CLASS",
    "UNDECODABLE_BODY_CLASS",
    "sweep_system_residue",
]
