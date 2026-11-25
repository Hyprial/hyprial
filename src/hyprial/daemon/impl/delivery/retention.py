"""Body release storage and the retention sweep (plan §2.0, §2.5 and §3).

Decision notes (phase A, S5):

- ``inbox`` and ``sender_copies`` gain ``body_class``, ``body_ref``,
  ``body_dropped_at_ms``, ``body_payload_sha256``, ``body_text_sha256`` and
  ``body_retention_ms``; ``sender_copies`` also gains ``body_settled_at_ms``.
  Each ALTER is guarded by ``PRAGMA table_info`` and nothing is backfilled,
  so a legacy row reads back unchanged and a downgrade stays safe (older
  builds INSERT with explicit column lists).  ``dlq`` and
  ``system_notices`` are untouched: DLQ bodies are never dropped (Q8).
- ``body_release(source, position, payload_sha256, ref, retention_ms,
  granted_at_ms)`` is the grant ledger, keyed by the row's physical position
  and not by message id.  A grant is applied only to an INTERACTION row whose
  current payload hashes to the granted digest; the row then carries the
  digest, ref and retention so the sweep can find it through a partial
  expression index instead of scanning every grant.
- An INTERACTION body is dropped only when a grant exists; without a grant
  it is never dropped.  SYSTEM bodies are not granted: ``system_residue``
  replaces them by the code default (slice T, Q18), walking the
  ``SYSTEM_RESIDUE_INDEXES`` created here with the other body indexes.
- "After ack" means ``inbox.acknowledged_at_ms``, which is only ever set
  together with ``consumed = 1``; the sweep selects by the due-time index and
  rechecks ``consumed`` in Python so the planner never walks every consumed
  row.  A sender copy has no ack
  of its own: it settles the first sweep after its outbox and custody rows
  are gone, which is never earlier than the delivery itself.
- Each sweep handles at most ``SWEEP_BATCH`` rows per step, rechecks the
  class and the payload digest in Python before it rewrites a payload, and
  writes no pending, outbox or custody state.
- Orphan grants are inert: ``arrival_id`` is AUTOINCREMENT and sender copies
  are never deleted, so a grant whose row is gone can never match again.

Decision notes (plan slice S1, rulings R4, R6; Q9):

- The recorder now records SYSTEM rows too, so a grant applies to a row of
  either class.  The grant stamps the row's own class in ``body_class``,
  and a SYSTEM grant always takes ``DEFAULT_BODY_RETENTION_MS``: Q9 (no
  per-agent SYSTEM retention) still holds, so a recorded SYSTEM body is
  dropped on the same clock as before, only now after its record exists.
- The sweep drops a granted row only while its class still equals the
  stamped ``body_class``.
- A granted SYSTEM row carries ``body_payload_sha256`` and so leaves the
  ``system_residue`` sweep, which stays the backstop for SYSTEM rows that
  are never granted (a peerless notice, a row the recorder skips).
"""

from __future__ import annotations

import sqlite3

from hyprial.daemon.impl.delivery.bodies import (
    payload_sha256,
    residue_payload,
    text_sha256,
)
from hyprial.daemon.impl.delivery.ports import (
    DEFAULT_BODY_RETENTION_MS,
    SOURCE_INBOX,
    SOURCE_SENDER_COPY,
    BodyGrant,
    ClassOf,
    RowDecoder,
)
from hyprial.kernel import MessageClass

SWEEP_BATCH = 64
BODY_TABLES = (SOURCE_INBOX, SOURCE_SENDER_COPY)
BODY_COLUMNS = (
    ("body_class", "TEXT"),
    ("body_ref", "TEXT"),
    ("body_dropped_at_ms", "INTEGER"),
    ("body_payload_sha256", "TEXT"),
    ("body_text_sha256", "TEXT"),
    ("body_retention_ms", "INTEGER"),
)
SETTLED_COLUMN = ("body_settled_at_ms", "INTEGER")
SYSTEM_RESIDUE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS inbox_body_unclassified ON inbox(acknowledged_at_ms) "
    "WHERE body_class IS NULL AND body_dropped_at_ms IS NULL "
    "AND acknowledged_at_ms IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS sender_copies_body_unclassified "
    "ON sender_copies(created_at_ms) "
    "WHERE body_class IS NULL AND body_dropped_at_ms IS NULL",
    "CREATE INDEX IF NOT EXISTS sender_copies_system_due "
    "ON sender_copies(body_settled_at_ms) "
    "WHERE body_class = 'system' AND body_dropped_at_ms IS NULL",
)
POSITION_COLUMN = {SOURCE_INBOX: "arrival_id", SOURCE_SENDER_COPY: "rowid"}
_SETTLED_AT = {
    SOURCE_INBOX: "acknowledged_at_ms",
    SOURCE_SENDER_COPY: "body_settled_at_ms",
}


def row_select(source: str) -> str:
    """``SELECT`` of every column of *source* plus its ``body_position``."""

    return f"SELECT {POSITION_COLUMN[source]} AS body_position, * FROM {source}"


def ensure_body_retention_schema(db: sqlite3.Connection) -> None:
    """Add the body columns, the grant ledger and the sweep indexes."""

    for table in BODY_TABLES:
        present = {
            str(row[1]) for row in db.execute(f"PRAGMA table_info({table})").fetchall()
        }
        columns = BODY_COLUMNS + ((SETTLED_COLUMN,) if table == SOURCE_SENDER_COPY else ())
        for name, kind in columns:
            if name not in present:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
        db.execute(
            f"CREATE INDEX IF NOT EXISTS {table}_body_due ON {table}("
            f"{_SETTLED_AT[table]} + body_retention_ms) "
            "WHERE body_dropped_at_ms IS NULL AND body_payload_sha256 IS NOT NULL"
        )
    db.execute(
        "CREATE INDEX IF NOT EXISTS sender_copies_body_unsettled "
        "ON sender_copies(body_settled_at_ms) "
        "WHERE body_payload_sha256 IS NOT NULL AND body_settled_at_ms IS NULL"
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS body_release (
               source TEXT NOT NULL,
               position INTEGER NOT NULL,
               payload_sha256 TEXT NOT NULL,
               ref TEXT NOT NULL,
               retention_ms INTEGER NOT NULL,
               granted_at_ms INTEGER NOT NULL,
               PRIMARY KEY (source, position)
           )"""
    )
    for statement in SYSTEM_RESIDUE_INDEXES:
        db.execute(statement)


def apply_body_grant(
    db: sqlite3.Connection,
    grant: BodyGrant,
    *,
    retention_ms: int,
    now_ms: int,
    decode: RowDecoder,
    class_of: ClassOf,
) -> bool:
    """Record *grant* if it names a live row with that payload.

    A grant for a row that is already dropped is answered idempotently by
    its digest and changes nothing.
    """

    source = grant.source
    row = db.execute(
        f"{row_select(source)} WHERE {POSITION_COLUMN[source]} = ?",
        (grant.position,),
    ).fetchone()
    if row is None:
        return False
    message = decode(row)
    message_class = class_of(message)
    if message_class is MessageClass.SYSTEM:
        retention_ms = DEFAULT_BODY_RETENTION_MS
    if row["body_dropped_at_ms"] is not None:
        return row["body_payload_sha256"] == grant.payload_sha256
    if payload_sha256(message.payload) != grant.payload_sha256:
        return False
    with db:
        db.execute(
            """INSERT INTO body_release(
                   source, position, payload_sha256, ref, retention_ms, granted_at_ms
               ) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(source, position) DO UPDATE SET
                   payload_sha256 = excluded.payload_sha256,
                   ref = excluded.ref,
                   retention_ms = excluded.retention_ms,
                   granted_at_ms = excluded.granted_at_ms""",
            (source, grant.position, grant.payload_sha256, grant.ref,
             retention_ms, now_ms),
        )
        db.execute(
            f"""UPDATE {source}
                   SET body_class = ?, body_ref = ?, body_payload_sha256 = ?,
                       body_retention_ms = ?
                 WHERE {POSITION_COLUMN[source]} = ?
                   AND body_dropped_at_ms IS NULL""",
            (message_class.value, grant.ref, grant.payload_sha256,
             retention_ms, grant.position),
        )
    return True


def _settle_sender_copies(db: sqlite3.Connection, now_ms: int, batch: int) -> None:
    with db:
        db.execute(
            """UPDATE sender_copies SET body_settled_at_ms = ?
                WHERE rowid IN (
                    SELECT copy.rowid FROM sender_copies AS copy
                     WHERE copy.body_payload_sha256 IS NOT NULL
                       AND copy.body_settled_at_ms IS NULL
                       AND NOT EXISTS (
                           SELECT 1 FROM outbox WHERE outbox.message_id = copy.message_id
                       )
                       AND NOT EXISTS (
                           SELECT 1 FROM custody
                            WHERE custody.message_id = copy.message_id
                       )
                     LIMIT ?)""",
            (now_ms, batch),
        )


def _due_rows(
    db: sqlite3.Connection, source: str, now_ms: int, batch: int
) -> list[sqlite3.Row]:
    due = f"{_SETTLED_AT[source]} + body_retention_ms"
    return db.execute(
        f"""{row_select(source)}
             WHERE body_dropped_at_ms IS NULL
               AND body_payload_sha256 IS NOT NULL
               AND {due} <= ?
             ORDER BY {due}
             LIMIT ?""",
        (now_ms, batch),
    ).fetchall()


def _granted_digest(db: sqlite3.Connection, source: str, position: int) -> str | None:
    row = db.execute(
        "SELECT payload_sha256 FROM body_release WHERE source = ? AND position = ?",
        (source, position),
    ).fetchone()
    return None if row is None else str(row[0])


def sweep_body_retention(
    db: sqlite3.Connection,
    *,
    now_ms: int,
    decode: RowDecoder,
    class_of: ClassOf,
    batch: int = SWEEP_BATCH,
) -> int:
    """Replace granted, settled bodies past retention by their residue."""

    _settle_sender_copies(db, now_ms, batch)
    dropped = 0
    for source in BODY_TABLES:
        with db:
            for row in _due_rows(db, source, now_ms, batch):
                position = int(row["body_position"])
                message = decode(row)
                digest = payload_sha256(message.payload)
                if (
                    (source == SOURCE_INBOX and row["consumed"] != 1)
                    or class_of(message).value != row["body_class"]
                    or digest != row["body_payload_sha256"]
                    or digest != _granted_digest(db, source, position)
                ):
                    continue
                dropped += db.execute(
                    f"""UPDATE {source}
                           SET payload = ?, body_dropped_at_ms = ?,
                               body_text_sha256 = ?
                         WHERE {POSITION_COLUMN[source]} = ?
                           AND body_dropped_at_ms IS NULL""",
                    (residue_payload(message.payload), now_ms,
                     text_sha256(message.payload), position),
                ).rowcount
    return dropped


__all__ = [
    "BODY_COLUMNS",
    "BODY_TABLES",
    "POSITION_COLUMN",
    "SWEEP_BATCH",
    "SYSTEM_RESIDUE_INDEXES",
    "apply_body_grant",
    "ensure_body_retention_schema",
    "row_select",
    "sweep_body_retention",
]
