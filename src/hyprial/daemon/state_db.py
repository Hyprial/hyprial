"""One shared SQLite database for desired state and the lifecycle journal.

Step U0a-2 (architecture 丙): the desired-state tables and the lifecycle
journal tables share ``lifecycle-operations.sqlite3``.  One
``StateDatabase`` owns the file's connection conventions and the write lock,
and the daemon wires one instance into both stores, so in-process contention
between the two former per-store connections disappears by construction.
The lock is keyed by the database FILE rather than by the instance (review
14570), so the path-taking fallback constructors cannot reintroduce
in-process contention either.  Cross-process contention (offline management
beside a running daemon) is absorbed by a bounded retry on top of the busy
handler.

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
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_BUSY_TIMEOUT_SECONDS = 2.0

# The busy handler only covers ``_BUSY_TIMEOUT_SECONDS`` per lock attempt.
# Cross-process contention (CI task 15631: a second connection held
# ``BEGIN IMMEDIATE`` past the 2.0s busy timeout and the daemon surfaced the
# raw ``sqlite3.OperationalError`` as ``DAEMON_ERROR: database is locked`` on
# ``lifecycle.start``) is transient by nature, so one lock acquisition gets a
# bounded budget of retries on top of the busy handler instead of failing on
# the first expiry.  The budget bounds the failure too: an outlasting holder
# still raises, only later and after retrying.
#
# ⚠️ The budget is a RETRY deadline, not a return deadline: it is checked
# after a failed ``BEGIN IMMEDIATE``, so the last attempt can still consume a
# full ``_BUSY_TIMEOUT_SECONDS``.  Worst-case return is therefore
# ``_LOCK_ACQUIRE_BUDGET_SECONDS + _BUSY_TIMEOUT_SECONDS`` (~10s).  Keep that
# sum below the caller's IPC patience (the CLI's
# ``_DAEMON_IPC_ROUNDTRIP_SECONDS = 15.0``; the lark worker ``_ipc`` = 15s;
# the lifecycle operation deadline = 70s).  Raising the budget past that
# turns a visible ``DAEMON_ERROR`` into a client timeout -- same cause,
# different symptom, and the hardest kind of "fixed" to diagnose later.
_LOCK_ACQUIRE_BUDGET_SECONDS = 8.0
_LOCK_RETRY_DELAY_SECONDS = 0.05

#: One write lock per database FILE, not per StateDatabase instance.  The
#: instance-local lock (2026-09-17, review 14570) could not serialize two
#: instances constructed on the same ``lifecycle-operations.sqlite3`` -- the
#: fallback constructors below -- ``DesiredStateStore`` built from a path,
#: ``LifecycleProcessManager`` likewise, ``DesiredStateSqliteShadow``
#: likewise -- do exactly that whenever a caller passes a path instead of the
#: canonical ``DaemonApplication.state_db``.  Two such instances in one
#: process would then contend on SQLite's own lock and surface the same
#: 'database is locked' the retry exists to absorb.  Keying the lock by
#: resolved path removes that class by construction, independent of which
#: construction site a future caller reaches for.
_LOCKS: dict[str, threading.RLock] = {}
_CONNECTION_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _write_lock_for(path: Path) -> threading.RLock:
    """The single re-entrant write lock shared by every instance of ``path``."""

    return _lock_for(path, _LOCKS)


def _lock_for(path: Path, locks: dict[str, threading.RLock]) -> threading.RLock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        lock = locks.get(key)
        if lock is None:
            lock = threading.RLock()
            locks[key] = lock
        return lock


class StateDatabase:
    """Owner of the shared state SQLite file and of its write serialization."""

    def __init__(
        self,
        path: Path,
        *,
        lock_acquire_budget_seconds: float = _LOCK_ACQUIRE_BUDGET_SECONDS,
    ) -> None:
        self._path = Path(path)
        self._lock_acquire_budget_seconds = lock_acquire_budget_seconds
        # Shared per FILE (see ``_write_lock_for``), so two instances on the
        # same database serialize against each other, not only against
        # themselves.
        self._write_lock = _write_lock_for(self._path)
        # SQLite's last close takes an exclusive lock to checkpoint/clean WAL.
        # A concurrent open can exhaust its busy timeout waiting for that close
        # on a slow disk. Serialize only connection setup/close, NOT SQL bodies.
        # This lock never acquires _write_lock: writers take write -> connection;
        # reversing that order in a reader's first WAL conversion would deadlock.
        self._connection_lock = _lock_for(self._path, _CONNECTION_LOCKS)

    @property
    def path(self) -> Path:
        return self._path

    def exists(self) -> bool:
        """Whether the database file exists yet (reads must not create it)."""

        return self._path.exists()

    def _connect(self) -> sqlite3.Connection:
        with self._connection_lock:
            return self._open()

    def _open(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self._path.exists():
            self._path.touch(mode=0o600)
        os.chmod(self._path, 0o600)
        # SQLite can return BUSY from connection PRAGMAs without invoking its
        # busy handler (including the journal-mode READ, CI tasks 22208/22209).
        # Honor the existing 2s contention budget across fresh connections,
        # rather than giving every retry another 2s or replaying business SQL.
        deadline = time.monotonic() + _BUSY_TIMEOUT_SECONDS
        while True:
            try:
                return self._connect_once(timeout=max(0.0, deadline - time.monotonic()))
            except sqlite3.OperationalError as error:
                if not self._is_transient_lock(error):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(_LOCK_RETRY_DELAY_SECONDS, remaining))

    def _connect_once(self, *, timeout: float) -> sqlite3.Connection:
        db = sqlite3.connect(self._path, timeout=timeout)
        try:
            # Setting journal_mode takes a lock the busy handler does not
            # arbitrate: on a database raced by concurrent connectors, the
            # `PRAGMA journal_mode=WAL` statement can fail immediately with
            # SQLITE_BUSY instead of honoring ``timeout`` (CI task 12128:
            # two concurrent readers against a live write holder), and a
            # connection abandoned mid-_connect stays open for as long as its
            # exception traceback lives -- the suite's fd guard then reports
            # it as a leak. So: read the mode first (no mode-change write), set
            # it only when needed. The per-file connection lock serializes
            # the one conversion and excludes closing peers. Already-WAL files
            # -- every connect after the first -- never run a journal-mode
            # write at all.
            cursor = db.cursor()
            try:
                mode = str(
                    cursor.execute("PRAGMA journal_mode").fetchone()[0]
                ).lower()
                if mode != "wal":
                    cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=FULL")
                # The shrinking timeout belongs only to initialization. Keep
                # the caller's subsequent SQL/BEGIN busy-handler budget intact.
                cursor.execute(f"PRAGMA busy_timeout={int(_BUSY_TIMEOUT_SECONDS * 1000)}")
            finally:
                # Finalize the in-flight statement even when one of these
                # PRAGMAs raises.  sqlite3_close_v2() leaves the connection
                # a zombie -- file descriptor still open -- while any
                # prepared statement is unfinalized, so a bare db.close()
                # after a raised PRAGMA does not release the descriptor
                # (CI task 12683 measured it: BASELINE=[32] AFTER=[32,34]
                # LEAKED=[34]).  Closing the cursor finalizes the statement
                # first; the close below then releases the file.
                cursor.close()
        except BaseException:
            # Never abandon an open connection: the caller still sees the
            # error, but the descriptor does not outlive this frame pinned
            # by the exception's traceback.
            db.close()
            raise
        return db

    def _close(self, db: sqlite3.Connection) -> None:
        with self._connection_lock:
            db.close()

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
                self._begin_write(db)
                try:
                    yield db
                except BaseException:
                    db.rollback()
                    raise
                else:
                    db.commit()
            finally:
                self._close(db)

    @staticmethod
    def _is_transient_lock(error: sqlite3.OperationalError) -> bool:
        """Whether the error is the retryable SQLITE_BUSY family.

        Classified by ``sqlite_errorcode`` when sqlite supplied one: the
        extended codes share the primary byte, so ``& 0xFF == SQLITE_BUSY``
        keeps ``SQLITE_BUSY_*`` while excluding ``SQLITE_LOCKED`` -- a
        table-level conflict between statements on ONE connection (a bug, not
        contention; retrying it only delays the failure).  Errors sqlite did
        not label (e.g. constructed in tests) fall back to the message.
        """

        code = getattr(error, "sqlite_errorcode", None)
        if isinstance(code, int) and code >= 0:
            return code & 0xFF == sqlite3.SQLITE_BUSY
        message = str(error).lower()
        return "locked" in message or "busy" in message

    def _begin_write(self, db: sqlite3.Connection) -> None:
        """``BEGIN IMMEDIATE`` with a bounded retry past the busy timeout.

        A failed ``BEGIN`` starts no transaction, so retrying on the same
        connection cannot replay work: the body runs exactly once, after the
        write lock is actually held.  The deadline is what keeps this from
        turning a permanently locked database into a hang -- expiry raises
        the original ``OperationalError``.
        """

        deadline = time.monotonic() + max(0.0, self._lock_acquire_budget_seconds)
        while True:
            try:
                db.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as error:
                if not self._is_transient_lock(error):
                    raise
                if time.monotonic() >= deadline:
                    raise
                time.sleep(_LOCK_RETRY_DELAY_SECONDS)

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """A short-lived read connection; the file must already exist."""

        db = self._connect()
        try:
            yield db
        finally:
            self._close(db)
