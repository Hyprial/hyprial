"""Exact durable results for bounded harness restore operations."""

from __future__ import annotations

import sqlite3

from hyprial.kernel import DesiredStateError

HARNESS_SETTLEMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS harness_restore_settlements (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT NOT NULL UNIQUE,
    attempted INTEGER NOT NULL,
    restored INTEGER NOT NULL,
    failed INTEGER NOT NULL,
    deferred INTEGER NOT NULL
);
"""

_CAPACITY = 256


class _HarnessSettlementStoreMixin:
    """Store exact restore results without changing the desired document."""

    def record_harness_restore_settlement(
        self,
        operation_id: str,
        attempted: int,
        restored: int,
        failed: int,
        deferred: int,
    ) -> tuple[int, int, int, int]:
        result = (attempted, restored, failed, deferred)
        with self.state_db.transaction(HARNESS_SETTLEMENT_SCHEMA) as db:
            db.execute(
                "INSERT OR IGNORE INTO harness_restore_settlements("
                "operation_id,attempted,restored,failed,deferred) "
                "VALUES(?,?,?,?,?)",
                (operation_id, *result),
            )
            row = db.execute(
                "SELECT attempted,restored,failed,deferred "
                "FROM harness_restore_settlements WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if row is None or tuple(int(value) for value in row) != result:
                raise DesiredStateError(
                    f"restore operation {operation_id} changed its result"
                )
            db.execute(
                "DELETE FROM harness_restore_settlements WHERE sequence NOT IN ("
                "SELECT sequence FROM harness_restore_settlements "
                "ORDER BY sequence DESC LIMIT ?)",
                (_CAPACITY,),
            )
        return result

    def harness_restore_settlement(
        self, operation_id: str
    ) -> tuple[int, int, int, int] | None:
        if not self.state_db.exists():
            return None
        try:
            with self.state_db.read() as db:
                row = db.execute(
                    "SELECT attempted,restored,failed,deferred "
                    "FROM harness_restore_settlements WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
        except sqlite3.OperationalError as error:
            if "no such table" in str(error).lower():
                return None
            raise
        if row is None:
            return None
        return tuple(int(value) for value in row)  # type: ignore[return-value]
