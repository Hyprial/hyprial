"""Durable correlation state for the interactive Codex inbox carrier."""

from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from hyprial.daemon.api import HarnessDelivery


@dataclass(frozen=True, slots=True)
class StoredCarrierDelivery:
    actor: str
    session_ref: str
    delivery: HarnessDelivery
    intent: str
    state: str
    turn_id: str | None
    final_output: str | None
    settlement_attempts: int


class CodexCarrierStore:
    """SQLite-backed message-to-turn/final correlation.

    The durable inbox remains the authority for request bodies.  This store
    keeps only the metadata required to avoid starting a completed request a
    second time, plus the final that must survive until dual-true settlement.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        os.chmod(self.path, 0o600)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout = 5000")
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS codex_interactive_delivery (
                actor TEXT NOT NULL,
                message_id TEXT NOT NULL,
                session_ref TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                sender TEXT NOT NULL,
                message TEXT NOT NULL DEFAULT '',
                intent TEXT NOT NULL,
                state TEXT NOT NULL,
                turn_id TEXT,
                final_output TEXT,
                settlement_attempts INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (actor, message_id)
            )
            """
        )
        columns = {
            str(row[1])
            for row in self._db.execute(
                "PRAGMA table_info(codex_interactive_delivery)"
            ).fetchall()
        }
        if "message" not in columns:
            self._db.execute(
                """
                ALTER TABLE codex_interactive_delivery
                ADD COLUMN message TEXT NOT NULL DEFAULT ''
                """
            )
        self._db.commit()

    def load(self, actor: str, session_ref: str) -> tuple[StoredCarrierDelivery, ...]:
        """Load this thread's correlations plus finals safe to settle anywhere.

        A final is already detached from model execution and can be replied by
        the actor's current fenced session.  Keeping it recoverable across a
        changed thread/session lets stale or TTL-hidden inbox rows retain their
        only copy until the daemon confirms settlement.
        """

        with self._lock:
            rows = self._db.execute(
                """
                SELECT * FROM codex_interactive_delivery
                WHERE actor = ? AND (
                    session_ref = ? OR state IN ('FINAL_OBSERVED', 'REMOTE_SETTLED')
                )
                ORDER BY rowid
                """,
                (actor, session_ref),
            ).fetchall()
        return tuple(self._stored(row) for row in rows)

    def record_fetched(
        self,
        actor: str,
        session_ref: str,
        delivery: HarnessDelivery,
        intent: str,
    ) -> None:
        with self._lock, self._db:
            self._db.execute(
                """
                INSERT INTO codex_interactive_delivery (
                    actor, message_id, session_ref, conversation_id, sender,
                    message, intent, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'FETCHED')
                ON CONFLICT(actor, message_id) DO NOTHING
                """,
                (
                    actor,
                    delivery.delivery_id,
                    session_ref,
                    delivery.conversation_id,
                    delivery.sender,
                    delivery.message,
                    intent,
                ),
            )
            self._db.execute(
                """
                UPDATE codex_interactive_delivery
                SET conversation_id = ?, sender = ?, message = ?, intent = ?
                WHERE actor = ? AND message_id = ? AND state = 'FETCHED'
                """,
                (
                    delivery.conversation_id,
                    delivery.sender,
                    delivery.message,
                    intent,
                    actor,
                    delivery.delivery_id,
                ),
            )
            row = self._db.execute(
                """
                SELECT session_ref, state FROM codex_interactive_delivery
                WHERE actor = ? AND message_id = ?
                """,
                (actor, delivery.delivery_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("carrier delivery was not durably reserved")
            if row["session_ref"] != session_ref:
                raise RuntimeError(
                    "carrier delivery is owned by another persisted session"
                )
            if row["state"] != "FETCHED":
                raise RuntimeError(
                    "carrier delivery already has durable turn/final correlation"
                )

    def record_turn_started(self, actor: str, message_id: str, turn_id: str) -> None:
        with self._lock, self._db:
            row = self._db.execute(
                """
                SELECT state, turn_id FROM codex_interactive_delivery
                WHERE actor = ? AND message_id = ?
                """,
                (actor, message_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("carrier delivery disappeared before turn mapping")
            if row["turn_id"] not in {None, turn_id}:
                raise RuntimeError("carrier delivery already maps to another turn")
            self._db.execute(
                """
                UPDATE codex_interactive_delivery
                SET state = 'TURN_STARTED', turn_id = ?
                WHERE actor = ? AND message_id = ?
                """,
                (turn_id, actor, message_id),
            )

    def record_final(self, actor: str, message_id: str, output: str) -> None:
        with self._lock, self._db:
            row = self._db.execute(
                """
                SELECT final_output FROM codex_interactive_delivery
                WHERE actor = ? AND message_id = ?
                """,
                (actor, message_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("carrier delivery disappeared before final persistence")
            existing = row["final_output"]
            if existing is not None and existing != output:
                raise RuntimeError("carrier delivery final changed for the same message")
            self._db.execute(
                """
                UPDATE codex_interactive_delivery
                SET state = 'FINAL_OBSERVED', final_output = ?
                WHERE actor = ? AND message_id = ?
                """,
                (output, actor, message_id),
            )

    def record_settlement_attempts(
        self, actor: str, message_id: str, attempts: int
    ) -> None:
        with self._lock, self._db:
            self._db.execute(
                """
                UPDATE codex_interactive_delivery
                SET settlement_attempts = ?
                WHERE actor = ? AND message_id = ?
                """,
                (attempts, actor, message_id),
            )

    def record_remote_settled(self, actor: str, message_id: str) -> None:
        """Journal remote dual-true before local actor cleanup is requested."""

        with self._lock, self._db:
            row = self._db.execute(
                """
                SELECT state, final_output FROM codex_interactive_delivery
                WHERE actor = ? AND message_id = ?
                """,
                (actor, message_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("carrier delivery disappeared after remote settlement")
            if row["state"] not in {"FINAL_OBSERVED", "REMOTE_SETTLED"}:
                raise RuntimeError(
                    "remote settlement requires a durably observed final"
                )
            if row["final_output"] is None:
                raise RuntimeError("remote settlement had no durable final")
            self._db.execute(
                """
                UPDATE codex_interactive_delivery SET state = 'REMOTE_SETTLED'
                WHERE actor = ? AND message_id = ?
                """,
                (actor, message_id),
            )

    def settle_and_delete(self, actor: str, message_id: str) -> None:
        """Atomically record SETTLED and remove the now-redundant final."""

        with self._lock, self._db:
            self._db.execute(
                """
                UPDATE codex_interactive_delivery SET state = 'SETTLED'
                WHERE actor = ? AND message_id = ?
                """,
                (actor, message_id),
            )
            self._db.execute(
                """
                DELETE FROM codex_interactive_delivery
                WHERE actor = ? AND message_id = ? AND state = 'SETTLED'
                """,
                (actor, message_id),
            )

    def delete(self, actor: str, message_id: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                """
                DELETE FROM codex_interactive_delivery
                WHERE actor = ? AND message_id = ?
                """,
                (actor, message_id),
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @staticmethod
    def _stored(row: sqlite3.Row) -> StoredCarrierDelivery:
        return StoredCarrierDelivery(
            actor=str(row["actor"]),
            session_ref=str(row["session_ref"]),
            delivery=HarnessDelivery(
                delivery_id=str(row["message_id"]),
                conversation_id=str(row["conversation_id"]),
                sender=str(row["sender"]),
                recipient=str(row["actor"]),
                message=str(row["message"]),
            ),
            intent=str(row["intent"]),
            state=str(row["state"]),
            turn_id=(str(row["turn_id"]) if row["turn_id"] is not None else None),
            final_output=(
                str(row["final_output"])
                if row["final_output"] is not None
                else None
            ),
            settlement_attempts=int(row["settlement_attempts"]),
        )
