"""One shared SQLite database for desired state and the lifecycle journal.

Step U0a-2 (architecture 丙): the desired-state tables and the lifecycle
journal tables share ``lifecycle-operations.sqlite3``.  A single
``StateDatabase`` instance owns the file, the connection conventions and the
write lock, and both stores receive that one instance, so in-process
contention between the two former per-store connections disappears by
construction instead of being absorbed by a busy timeout.  Cross-process
contention (offline management beside a running daemon) stays a
busy-timeout matter, as before.

Connections are opened per transaction (and per read) and closed on
commit/rollback: the suite enforces strict per-test fd hygiene
(tests/conftest.py ``_fd_hygiene``), so a long-lived descriptor would fail
every store-constructing test, and both stores write at human frequency.
One transaction is exactly one connection, so a transaction's atomicity
never spans two descriptors.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_BUSY_TIMEOUT_SECONDS = 2.0


class StateDatabase:
    """Owner of the shared state SQLite file and of its write serialization."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._write_lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    def exists(self) -> bool:
        """Whether the database file exists yet (reads must not create it)."""

        return self._path.exists()

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self._path.exists():
            self._path.touch(mode=0o600)
        os.chmod(self._path, 0o600)
        db = sqlite3.connect(self._path, timeout=_BUSY_TIMEOUT_SECONDS)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db

    @contextmanager
    def transaction(
        self, schema: str | None = None
    ) -> Iterator[sqlite3.Connection]:
        """One write transaction on one short-lived connection.

        ``schema`` (``CREATE ... IF NOT EXISTS`` statements) is applied on
        the same connection BEFORE the transaction begins --
        ``executescript`` would otherwise implicitly commit mid-transaction.
        """

        with self._write_lock:
            db = self._connect()
            try:
                if schema is not None:
                    db.executescript(schema)
                db.execute("BEGIN IMMEDIATE")
                try:
                    yield db
                except BaseException:
                    db.rollback()
                    raise
                else:
                    db.commit()
            finally:
                db.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """A short-lived read connection; the file must already exist."""

        db = self._connect()
        try:
            yield db
        finally:
            db.close()
