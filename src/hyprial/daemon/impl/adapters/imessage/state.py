"""Durable ROWID checkpoints for the local Apple Messages source."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path


class IMessageCursorLedger:
    def __init__(self, path: str | Path) -> None:
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.RLock()
        with self._connection:
            self._connection.execute(
                """CREATE TABLE IF NOT EXISTS imessage_cursors (
                       instance_id TEXT NOT NULL,
                       generation INTEGER NOT NULL,
                       rowid INTEGER NOT NULL,
                       PRIMARY KEY(instance_id,generation)
                   )"""
            )

    def load_sync(self, instance_id: str, generation: int) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT rowid FROM imessage_cursors WHERE instance_id=? AND generation=?",
                (instance_id, generation),
            ).fetchone()
        return int(row[0]) if row else 0

    async def load(self, instance_id: str, generation: int) -> int:
        return self.load_sync(instance_id, generation)

    async def commit(self, instance_id: str, generation: int, rowid: int) -> None:
        with self._lock, self._connection:
            current = self.load_sync(instance_id, generation)
            if rowid < current:
                raise ValueError("iMessage cursor cannot move backwards")
            self._connection.execute(
                """INSERT INTO imessage_cursors(instance_id,generation,rowid) VALUES(?,?,?)
                   ON CONFLICT(instance_id,generation) DO UPDATE SET rowid=excluded.rowid""",
                (instance_id, generation, rowid),
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()


__all__ = ["IMessageCursorLedger"]
