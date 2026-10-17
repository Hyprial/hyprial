"""Durable correlation state for the interactive Codex inbox carrier."""

from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from hyprial.daemon.impl.api import HarnessDelivery
from hyprial.daemon.impl.harnesses.carrier.contract import INJECTION_FORMAT



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
    injection_format: str | None = None


@dataclass(frozen=True, slots=True)
class StoredTurnReport:
    report_id: str
    phase: str
    status: str | None
    delivery_id: str


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
        if "injection_format" not in columns:
            # NULL marks a row written before summary-only injection: its
            # ``message`` is an old body and must never reach a model.
            self._db.execute(
                "ALTER TABLE codex_interactive_delivery ADD COLUMN injection_format TEXT"
            )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS codex_turn_report (actor TEXT NOT NULL, "
            "session_ref TEXT NOT NULL, report_id TEXT NOT NULL, phase TEXT NOT NULL, "
            "status TEXT, delivery_id TEXT NOT NULL DEFAULT '', "
            "created_ms INTEGER NOT NULL, PRIMARY KEY (actor, report_id, phase))"
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
                    session_ref = ? OR state IN ('FINAL_OBSERVED', 'REMOTE_SETTLED', 'KEPT')
                )
                ORDER BY rowid
                """,
                (actor, session_ref),
            ).fetchall()
        return tuple(self._stored(row) for row in rows)

    def persisted_turn_id(self, actor: str, message_id: str) -> str | None:
        """The turn id written synchronously at the native turn/start boundary.

        The carrier projection can lag the pump thread under load; this durable
        mapping is what lets the removal path still journal the failed turn's
        ended report before it drops the delivery.
        """

        with self._lock:
            row = self._db.execute(
                """
                SELECT turn_id FROM codex_interactive_delivery
                WHERE actor = ? AND message_id = ?
                """,
                (actor, message_id),
            ).fetchone()
        if row is None or row["turn_id"] is None:
            return None
        return str(row["turn_id"])

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
                    message, intent, state, injection_format
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'FETCHED', ?)
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
                    INJECTION_FORMAT,
                ),
            )
            self._db.execute(
                """
                UPDATE codex_interactive_delivery
                SET conversation_id = ?, sender = ?, message = ?, intent = ?,
                    injection_format = ?
                WHERE actor = ? AND message_id = ? AND state = 'FETCHED'
                """,
                (
                    delivery.conversation_id,
                    delivery.sender,
                    delivery.message,
                    intent,
                    INJECTION_FORMAT,
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
                "UPDATE codex_interactive_delivery SET state = 'REMOTE_SETTLED' "
                "WHERE actor = ? AND message_id = ?",
                (actor, message_id),
            )

    def record_kept(self, actor: str, message_id: str) -> None:
        """A ``keep`` hint: remember the id, drop the final, never refetch it."""

        with self._lock, self._db:
            self._db.execute(
                "UPDATE codex_interactive_delivery SET state = 'KEPT', "
                "final_output = NULL, settlement_attempts = 0 "
                "WHERE actor = ? AND message_id = ?",
                (actor, message_id),
            )

    def kept_ids(self, actor: str) -> frozenset[str]:
        """KEPT ids of this actor, across sessions (dedup key is the id)."""

        return self._ids(actor, "state = 'KEPT'")

    def legacy_fetched_ids(self, actor: str) -> frozenset[str]:
        """FETCHED rows from before summary-only injection (body in ``message``)."""

        return self._ids(actor, "state = 'FETCHED' AND injection_format IS NULL")

    def _ids(self, actor: str, condition: str) -> frozenset[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT message_id FROM codex_interactive_delivery "
                f"WHERE actor = ? AND {condition}",
                (actor,),
            ).fetchall()
        return frozenset(str(r[0]) for r in rows)

    def delete_in_state(self, actor: str, message_id: str, state: str) -> None:
        """Delete a row only while it is still in ``state`` (FETCHED or KEPT)."""

        with self._lock, self._db:
            self._db.execute(
                "DELETE FROM codex_interactive_delivery "
                "WHERE actor = ? AND message_id = ? AND state = ?",
                (actor, message_id, state),
            )

    def add_turn_report(
        self,
        actor: str,
        session_ref: str,
        report_id: str,
        phase: str,
        status: str | None,
        delivery_id: str,
        created_ms: int,
    ) -> None:
        """Journal one turn report; a repeat keeps the first ``created_ms``."""

        with self._lock, self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO codex_turn_report VALUES (?, ?, ?, ?, ?, ?, ?)",
                (actor, session_ref, report_id, phase, status, delivery_id, created_ms),
            )

    def pending_turn_reports(
        self, actor: str, session_ref: str
    ) -> tuple[StoredTurnReport, ...]:
        """Unconfirmed reports of the current session: oldest first, started first."""

        with self._lock:
            rows = self._db.execute(
                "SELECT report_id, phase, status, delivery_id FROM codex_turn_report "
                "WHERE actor = ? AND session_ref = ? ORDER BY created_ms, "
                "CASE phase WHEN 'started' THEN 0 ELSE 1 END, rowid",
                (actor, session_ref),
            ).fetchall()
        return tuple(
            StoredTurnReport(
                str(r["report_id"]),
                str(r["phase"]),
                (str(r["status"]) if r["status"] is not None else None),
                str(r["delivery_id"]),
            )
            for r in rows
        )

    def delete_turn_report(self, actor: str, report_id: str, phase: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "DELETE FROM codex_turn_report "
                "WHERE actor = ? AND report_id = ? AND phase = ?",
                (actor, report_id, phase),
            )

    def drop_other_session_reports(self, actor: str, session_ref: str) -> None:
        """Reports of an older session are fenced out forever; drop them."""

        with self._lock, self._db:
            self._db.execute(
                "DELETE FROM codex_turn_report WHERE actor = ? AND session_ref != ?",
                (actor, session_ref),
            )

    def settle_and_delete(self, actor: str, message_id: str) -> None:
        """Atomically record SETTLED and remove the now-redundant final."""

        with self._lock, self._db:
            self._db.execute(
                "UPDATE codex_interactive_delivery SET state = 'SETTLED' "
                "WHERE actor = ? AND message_id = ?",
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
            injection_format=(
                str(row["injection_format"])
                if row["injection_format"] is not None
                else None
            ),
        )
