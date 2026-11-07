"""``StateDatabase`` must ride out a transient write holder, not reject the caller.

CI task 15631: ``test_pi_headless_worker_fetch_path_is_not_rejected`` failed
because the isolated daemon answered

    daemon rejected lifecycle.start: {'code': 'DAEMON_ERROR',
                                      'message': 'database is locked'}

The deterministic mechanism is a second connection holding ``BEGIN IMMEDIATE``
longer than the handler waits.  ``lifecycle.start`` reaches it through
``LifecycleProcessManager.submit`` -> ``_LifecycleStore.reserve`` ->
``StateDatabase.transaction``; the raw ``sqlite3.OperationalError`` then
escapes to the daemon's generic error boundary as ``DAEMON_ERROR``.

The fix gives one lock acquisition a bounded budget of retries on top of the
per-attempt busy handler.  These tests pin both halves:

* a holder that outlasts the 2.0s busy timeout is waited out (the body runs
  exactly once -- a failed ``BEGIN`` starts no transaction, so nothing is
  replayed);
* a holder that outlasts the whole budget still fails loudly with the
  original ``OperationalError`` -- the retry must not become a hang;
* two ``StateDatabase`` objects for the same file serialize on one shared
  per-path lock (review 14570: the lock was per instance, so the fallback
  constructors -- ``DesiredStateStore`` built from a path,
  ``LifecycleProcessManager`` likewise, ``DesiredStateSqliteShadow``
  likewise -- could put two instances on one ``lifecycle-operations.sqlite3``
  in a single process and collide);
* the retry family is classified by ``sqlite_errorcode`` (``SQLITE_BUSY_*``),
  not by the word "locked", so ``SQLITE_LOCKED`` is not retried.

⚠️ Every holder here is **test-driven**: it holds ``BEGIN IMMEDIATE`` until
``release()`` is called, never for a fixed wall-clock duration.  A timer-based
holder makes the outcome depend on which clock fires first -- the holder's
release or the contender's give-up (budget + busy timeout), both wall-clock --
which is what CI task 16836 lost under CPU throttling
(``DID NOT RAISE`` because the holder had already released).  Likewise, no
assertion here is an ``elapsed >= busy_timeout`` floor: a stall can only delay
a contender, so the checks are one-directional ("did it return while the lock
was still held?") and cannot false-red.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from hyprial.daemon.impl.state_db import StateDatabase


class _WriteLockHolder:
    """A second connection holding ``BEGIN IMMEDIATE`` until the test releases it.

    The lock is taken synchronously in ``__init__`` (so it is held before this
    constructor returns), and only ``release()`` gives it back.  Compare the
    old helper, which slept a fixed ``hold_seconds`` on a timer thread: under
    throttling that release could beat the contender's give-up, turning
    ``pytest.raises`` into ``DID NOT RAISE`` (CI task 16836).
    """

    def __init__(self, path: Path, *, row: int = -1) -> None:
        self._connection = sqlite3.connect(path, timeout=0.0, check_same_thread=False)
        self._connection.execute("PRAGMA busy_timeout=0")
        self._connection.execute("BEGIN IMMEDIATE")
        # Keep the write lock real (an empty transaction still holds it, but a
        # written row makes it unambiguous), and discard it on ROLLBACK.
        self._connection.execute("INSERT INTO t VALUES (?)", (row,))
        self._release = threading.Event()
        self._thread = threading.Thread(target=self._run, name="write-lock-holder")
        self._thread.start()

    def _run(self) -> None:
        self._release.wait(timeout=30)
        self._connection.execute("ROLLBACK")

    def release(self) -> None:
        self._release.set()
        self._thread.join(timeout=10)
        self._connection.close()


def _seeded_database(tmp_path: Path) -> tuple[StateDatabase, Path]:
    path = tmp_path / "state" / "lifecycle-operations.sqlite3"
    state_db = StateDatabase(path)
    with state_db.transaction(schema="CREATE TABLE IF NOT EXISTS t(x)") as db:
        db.execute("INSERT INTO t VALUES (0)")
    return state_db, path


def test_transaction_waits_out_a_transient_external_writer(tmp_path: Path) -> None:
    """A holder past the 2.0s busy timeout must not reject the writer.

    Before the fix this raised ``sqlite3.OperationalError: database is locked``
    after ~2.1s -- the exact error the daemon turned into ``DAEMON_ERROR``.
    The "it really contended" evidence is a state check, not an elapsed floor:
    while the holder is held the transaction may not return (a fix that raised
    on the first busy cycle would set ``finished`` well before 2.5s), and it
    must not carry an error.
    """

    state_db, path = _seeded_database(tmp_path)
    holder = _WriteLockHolder(path)
    body_runs: list[int] = []
    errors: list[BaseException] = []
    finished = threading.Event()

    def contend() -> None:
        try:
            with state_db.transaction() as db:
                body_runs.append(1)
                db.execute("INSERT INTO t VALUES (1)")
        except BaseException as error:  # noqa: BLE001 - surfaced below
            errors.append(error)
        finally:
            finished.set()

    contender = threading.Thread(target=contend)
    try:
        contender.start()
        # One-directional: the holder cannot release before this line, so a
        # return here means the acquisition never waited.  A throttled start
        # only makes this harder to falsify, never falsely red.
        assert not finished.wait(2.5), (
            f"the writer returned while the write lock was still held: {errors!r}"
        )
    finally:
        holder.release()
        contender.join(timeout=10)

    assert errors == []
    assert body_runs == [1], "a failed BEGIN must not replay the transaction body"
    assert not contender.is_alive()
    with state_db.read() as db:
        rows = [int(row[0]) for row in db.execute("SELECT x FROM t ORDER BY x")]
    assert rows == [0, 1]


def test_two_instances_on_one_file_share_the_write_lock(tmp_path: Path) -> None:
    """Two ``StateDatabase`` objects for one file serialize, not collide.

    Review 14570: ``_write_lock`` was per instance.  The fallback
    constructors (``DesiredStateStore`` built from a path,
    ``LifecycleProcessManager`` likewise, ``DesiredStateSqliteShadow``
    likewise) create a second instance whenever a caller passes a path
    instead of ``DaemonApplication.state_db``; with per-instance locks the
    second instance's ``BEGIN IMMEDIATE`` busy-waits the 2.0s handler and then
    raises -- the CI face, inside one process.  With the lock keyed by
    resolved path the second instance waits on the shared lock instead.

    The contender carries ``budget=0.0`` so the retry cannot mask a
    per-instance lock: only the shared lock can carry it through the hold.
    """

    state_db, path = _seeded_database(tmp_path)
    second = StateDatabase(path, lock_acquire_budget_seconds=0.0)
    holding = threading.Event()
    release = threading.Event()
    holder_errors: list[BaseException] = []

    def hold() -> None:
        try:
            with state_db.transaction() as db:
                db.execute("INSERT INTO t VALUES (1)")
                holding.set()
                release.wait(timeout=30)
        except BaseException as error:  # noqa: BLE001 - surfaced below
            holder_errors.append(error)
            holding.set()

    holder = threading.Thread(target=hold, name="first-instance")
    holder.start()
    assert holding.wait(timeout=10)

    contender_errors: list[BaseException] = []
    finished = threading.Event()

    def contend() -> None:
        try:
            with second.transaction() as db:
                db.execute("INSERT INTO t VALUES (2)")
        except BaseException as error:  # noqa: BLE001 - surfaced below
            contender_errors.append(error)
        finally:
            finished.set()

    contender = threading.Thread(target=contend, name="second-instance")
    try:
        contender.start()
        # Per-instance lock: the contender fails on SQLite's lock at ~2.0s and
        # sets ``finished``; shared lock: it waits on the RLock.  One-
        # directional, so throttling cannot false-red it.
        assert not finished.wait(2.5), (
            f"the second instance returned while the lock was held: {contender_errors!r}"
        )
    finally:
        release.set()
        contender.join(timeout=10)
        holder.join(timeout=10)

    assert holder_errors == []
    assert contender_errors == []
    assert not holder.is_alive() and not contender.is_alive()
    with state_db.read() as db:
        rows = [int(row[0]) for row in db.execute("SELECT x FROM t ORDER BY x")]
    assert rows == [0, 1, 2]


def test_transaction_still_fails_loudly_when_the_holder_outlasts_the_budget(
    tmp_path: Path,
) -> None:
    """The retry budget is bounded: an outlasting holder still raises.

    The holder is held across the whole ``pytest.raises`` block and released
    only afterwards, so the contention is a fact for the entire acquisition --
    not a race between a release timer and the 0.5s budget + 2.0s busy handler
    (CI task 16836: under throttling the give-up crossed the timer, the lock
    was free, the INSERT succeeded and the assertion failed ``DID NOT RAISE``).
    """

    state_db, path = _seeded_database(tmp_path)
    state_db = StateDatabase(path, lock_acquire_budget_seconds=0.5)
    holder = _WriteLockHolder(path)
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            with state_db.transaction() as db:
                db.execute("INSERT INTO t VALUES (2)")
    finally:
        holder.release()


def test_lock_classifier_retries_sqlite_busy_only(tmp_path: Path) -> None:
    """The retry family is classified by code, not by the word "locked".

    A real SQLITE_BUSY (contended ``BEGIN IMMEDIATE``) is retryable; a real
    non-lock OperationalError (missing table) is not.  Classification by
    ``sqlite_errorcode`` is what keeps ``SQLITE_LOCKED`` (``database table is
    locked``, a same-connection statement conflict) out of the retry family
    even though its text contains "locked".  No timing is involved: the
    holder's ``busy_timeout=0`` makes the contention fail immediately.
    """

    path = tmp_path / "state" / "lifecycle-operations.sqlite3"
    with StateDatabase(path).transaction(
        schema="CREATE TABLE IF NOT EXISTS t(x)"
    ) as db:
        db.execute("INSERT INTO t VALUES (1)")

    holder = sqlite3.connect(path, timeout=0.0)
    holder.execute("PRAGMA busy_timeout=0")
    holder.execute("BEGIN IMMEDIATE")
    try:
        contender = sqlite3.connect(path, timeout=0.0)
        contender.execute("PRAGMA busy_timeout=0")
        try:
            with pytest.raises(sqlite3.OperationalError) as busy:
                contender.execute("BEGIN IMMEDIATE")
        finally:
            contender.close()
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert StateDatabase._is_transient_lock(busy.value) is True

    class _CodedTableLocked(sqlite3.OperationalError):
        """A real class distinction the text alone cannot see: SQLITE_LOCKED."""

        sqlite_errorcode = sqlite3.SQLITE_LOCKED
        sqlite_errorname = "SQLITE_LOCKED"

    assert StateDatabase._is_transient_lock(
        _CodedTableLocked("database table is locked")
    ) is False, "SQLITE_LOCKED must not be retried just because its text says locked"

    reader = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.OperationalError) as other:
            reader.execute("SELECT * FROM no_such_table")
    finally:
        reader.close()
    assert StateDatabase._is_transient_lock(other.value) is False
