"""The recorder's local state: feed cursors, the record index and segments.

Decision notes (phase A, S6; plan §3 "New databases"):

- ``conversation-records.sqlite3`` in the daemon state directory holds only
  bookkeeping, never a body: the feed cursor per source, one status row per
  recorded or skipped ``(source, position, payload sha256)``, and the
  current segment number per conversation record id.  It is in
  ``MIGRATION_DATABASES``; it stores no owner spelling (cell ids are
  digests, refs name space ids and paths).
- Losing this file is safe: the cursor restarts at 0, and the segment
  read-back finds entries already written by key, so nothing is appended
  twice and grants are re-issued idempotently.
- Status values: ``recorded`` (written and read back, grant pending),
  ``granted``, ``refused`` (the facade answered False), ``redacted``
  (written, never granted: a changed body or a bodiless placeholder) and
  ``skipped:<reason>`` (nothing written).
- ``pending_grants`` returns the oldest attempts first; ``defer`` moves a
  pending row behind every other row (``updated_at_ms`` past the current
  maximum), so the order stays fair even when the clock does not advance.
- One connection guarded by a lock; the recorder thread is the only writer.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

STATUS_RECORDED = "recorded"
STATUS_GRANTED = "granted"
STATUS_REFUSED = "refused"
STATUS_REDACTED = "redacted"
SKIPPED_PREFIX = "skipped:"


@dataclass(frozen=True, slots=True)
class PendingGrant:
    """A recorded row whose grant has not been accepted yet."""

    source: str
    position: int
    payload_sha256: str
    ref: str


class RecordStore:
    """SQLite bookkeeping of the conversation recorder."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self._path, check_same_thread=False, timeout=5.0)
        with self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS feed_cursor ("
                "source TEXT PRIMARY KEY, position INTEGER NOT NULL)"
            )
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS records (
                       source TEXT NOT NULL,
                       position INTEGER NOT NULL,
                       payload_sha256 TEXT NOT NULL,
                       status TEXT NOT NULL,
                       cell_id TEXT,
                       ref TEXT,
                       updated_at_ms INTEGER NOT NULL,
                       PRIMARY KEY (source, position, payload_sha256)
                   )"""
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS records_pending ON records(updated_at_ms) "
                f"WHERE status = '{STATUS_RECORDED}'"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS segments ("
                "cell_id TEXT PRIMARY KEY, seq INTEGER NOT NULL)"
            )

    @property
    def path(self) -> Path:
        return self._path

    def cursor(self, source: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT position FROM feed_cursor WHERE source = ?", (source,)
            ).fetchone()
        return 0 if row is None else int(row[0])

    def set_cursor(self, source: str, position: int) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO feed_cursor(source, position) VALUES (?, ?) "
                "ON CONFLICT(source) DO UPDATE SET position = excluded.position",
                (source, position),
            )

    def status(self, source: str, position: int, payload_sha256: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT status FROM records WHERE source = ? AND position = ? "
                "AND payload_sha256 = ?",
                (source, position, payload_sha256),
            ).fetchone()
        return None if row is None else str(row[0])

    def mark(
        self,
        source: str,
        position: int,
        payload_sha256: str,
        status: str,
        *,
        now_ms: int,
        cell_id: str | None = None,
        ref: str | None = None,
    ) -> None:
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO records(
                       source, position, payload_sha256, status, cell_id, ref, updated_at_ms
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source, position, payload_sha256) DO UPDATE SET
                       status = excluded.status,
                       cell_id = COALESCE(excluded.cell_id, records.cell_id),
                       ref = COALESCE(excluded.ref, records.ref),
                       updated_at_ms = excluded.updated_at_ms""",
                (source, position, payload_sha256, status, cell_id, ref, now_ms),
            )

    def defer(self, source: str, position: int, payload_sha256: str, *, now_ms: int) -> None:
        """Move one pending grant behind every other pending grant."""

        with self._lock, self._db:
            self._db.execute(
                "UPDATE records SET updated_at_ms = MAX(?, "
                "(SELECT COALESCE(MAX(updated_at_ms), 0) FROM records) + 1) "
                "WHERE source = ? AND position = ? AND payload_sha256 = ? AND status = ?",
                (now_ms, source, position, payload_sha256, STATUS_RECORDED),
            )

    def pending_grants(self, limit: int) -> tuple[PendingGrant, ...]:
        with self._lock:
            rows = self._db.execute(
                "SELECT source, position, payload_sha256, ref FROM records "
                "WHERE status = ? ORDER BY updated_at_ms, rowid LIMIT ?",
                (STATUS_RECORDED, limit),
            ).fetchall()
        return tuple(PendingGrant(str(r[0]), int(r[1]), str(r[2]), str(r[3])) for r in rows)

    def segment(self, cell_id: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT seq FROM segments WHERE cell_id = ?", (cell_id,)
            ).fetchone()
        return 0 if row is None else int(row[0])

    def set_segment(self, cell_id: str, seq: int) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO segments(cell_id, seq) VALUES (?, ?) "
                "ON CONFLICT(cell_id) DO UPDATE SET seq = excluded.seq",
                (cell_id, seq),
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()


__all__ = [
    "SKIPPED_PREFIX",
    "STATUS_GRANTED",
    "STATUS_RECORDED",
    "STATUS_REDACTED",
    "STATUS_REFUSED",
    "PendingGrant",
    "RecordStore",
]
