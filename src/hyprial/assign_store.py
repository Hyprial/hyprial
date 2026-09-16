"""Shared SQLite ownership for the durable ``actor_assign`` ledger.

The table historically lives in ``workflows.sqlite3``.  Its location and row
shape stay unchanged so old homes remain readable, but neither the DDL nor the
reader/writer operations belong to the legacy workflow execution package.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Protocol

ASSIGN_SCHEMA = """
CREATE TABLE IF NOT EXISTS actor_assign (
    actor          TEXT NOT NULL,
    assign_kind    TEXT NOT NULL,
    assign_ref     TEXT NOT NULL,
    assigned_at_ms INTEGER NOT NULL,
    released_at_ms INTEGER,
    PRIMARY KEY (actor, assign_kind, assign_ref)
);
CREATE INDEX IF NOT EXISTS actor_assign_live
    ON actor_assign(actor) WHERE released_at_ms IS NULL;
CREATE INDEX IF NOT EXISTS actor_assign_ref
    ON actor_assign(assign_kind, assign_ref) WHERE released_at_ms IS NULL;
"""


class AssignLedger(Protocol):
    """The storage seam used by reconciliation, independent of run storage."""

    def live_assign_rows(self) -> tuple[tuple[str, str, str], ...]: ...

    def release_assigns(
        self, *, assign_kind: str, assign_ref: str, released_at_ms: int
    ) -> int: ...


class AssignStoreMixin:
    """Ledger operations for an owner of ``_db`` and ``_db_lock``.

    ``WorkflowStore`` inherits this while it remains the physical database
    owner.  ``AssignStore`` below opens the same historical database directly
    for shared readers and future non-workflow lifecycle owners.
    """

    _db: sqlite3.Connection
    _db_lock: threading.RLock

    def _install_assign_schema(self) -> None:
        with self._db_lock, self._db:
            self._db.executescript(ASSIGN_SCHEMA)

    def record_workflow_assign(
        self, *, actor: str, run_id: str, assigned_at_ms: int
    ) -> None:
        """Record that one legacy workflow run's work reached this actor."""

        with self._db_lock, self._db:
            self._db.execute(
                """INSERT INTO actor_assign
                       (actor, assign_kind, assign_ref, assigned_at_ms, released_at_ms)
                   VALUES (?, 'workflow', ?, ?, NULL)
                   ON CONFLICT(actor, assign_kind, assign_ref) DO UPDATE SET
                       released_at_ms = NULL""",
                (actor, run_id, assigned_at_ms),
            )

    def release_workflow_assigns(self, *, run_id: str, released_at_ms: int) -> None:
        """Mark every assign of one terminal legacy run released idempotently."""

        self.release_assigns(
            assign_kind="workflow",
            assign_ref=run_id,
            released_at_ms=released_at_ms,
        )

    def live_assigns(self, *, actor: str) -> tuple[tuple[str, str], ...]:
        """Return ``(assign_kind, assign_ref)`` for an actor's live edges."""

        with self._db_lock:
            rows = self._db.execute(
                """SELECT assign_kind, assign_ref FROM actor_assign
                    WHERE actor = ? AND released_at_ms IS NULL
                    ORDER BY assign_kind, assign_ref""",
                (actor,),
            ).fetchall()
        return tuple((str(row["assign_kind"]), str(row["assign_ref"])) for row in rows)

    def assign_rows(
        self, *, actor: str
    ) -> tuple[tuple[str, str, int | None], ...]:
        """Return every assign edge for an actor, released or live."""

        with self._db_lock:
            rows = self._db.execute(
                """SELECT assign_kind, assign_ref, released_at_ms FROM actor_assign
                    WHERE actor = ? ORDER BY assign_kind, assign_ref""",
                (actor,),
            ).fetchall()
        return tuple(
            (
                str(row["assign_kind"]),
                str(row["assign_ref"]),
                row["released_at_ms"],
            )
            for row in rows
        )

    def live_assign_rows(self) -> tuple[tuple[str, str, str], ...]:
        """Return every live ``(actor, assign_kind, assign_ref)`` edge."""

        with self._db_lock:
            rows = self._db.execute(
                """SELECT actor, assign_kind, assign_ref FROM actor_assign
                    WHERE released_at_ms IS NULL
                    ORDER BY actor, assign_kind, assign_ref"""
            ).fetchall()
        return tuple(
            (
                str(row["actor"]),
                str(row["assign_kind"]),
                str(row["assign_ref"]),
            )
            for row in rows
        )

    def release_assigns(
        self, *, assign_kind: str, assign_ref: str, released_at_ms: int
    ) -> int:
        """Stamp all live edges for one source, preserving earlier stamps."""

        with self._db_lock, self._db:
            return self._release_assigns_in_transaction(
                assign_kind=assign_kind,
                assign_ref=assign_ref,
                released_at_ms=released_at_ms,
            )

    def _release_assigns_in_transaction(
        self, *, assign_kind: str, assign_ref: str, released_at_ms: int
    ) -> int:
        """Release edges on the caller's current transaction.

        The caller must hold ``_db_lock`` and an active SQLite transaction.
        This keeps a terminal run transition and its assign release atomic.
        """

        cursor = self._db.execute(
            """UPDATE actor_assign
                  SET released_at_ms = ?
                WHERE assign_kind = ?
                  AND assign_ref = ?
                  AND released_at_ms IS NULL""",
            (released_at_ms, assign_kind, assign_ref),
        )
        return int(cursor.rowcount)


class AssignStore(AssignStoreMixin):
    """A direct shared reader/writer for an existing assign database."""

    def __init__(self, database: Path) -> None:
        database = Path(database)
        database.parent.mkdir(parents=True, exist_ok=True)
        self._db_lock = threading.RLock()
        self._db = sqlite3.connect(database, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._install_assign_schema()

    def close(self) -> None:
        with self._db_lock:
            self._db.close()


__all__ = [
    "ASSIGN_SCHEMA",
    "AssignLedger",
    "AssignStore",
    "AssignStoreMixin",
]
