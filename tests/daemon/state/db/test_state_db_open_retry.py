"""Real SQLITE_BUSY during connection setup must get the existing open budget."""

import sqlite3
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl import state_db as module
from hyprial.daemon.impl.state_db import StateDatabase


def contended_open(tmp_path, monkeypatch, *, mode, release_on_busy):
    path = tmp_path / "state.sqlite3"
    connect = sqlite3.connect
    holder = connect(path)
    holder.execute("CREATE TABLE t(x)")
    holder.execute("INSERT INTO t VALUES (42)")
    holder.commit()
    holder.execute(f"BEGIN {mode}")
    holder.execute("INSERT INTO t VALUES (99)")
    released = False
    errors = []
    connections = []
    timeouts = []

    def release():
        nonlocal released
        if not released:
            holder.rollback()
            holder.close()
            released = True

    class Cursor(sqlite3.Cursor):
        def execute(self, sql, *args):
            try:
                return super().execute(sql, *args)
            except sqlite3.OperationalError as error:
                errors.append((sql, error))
                if release_on_busy and error.sqlite_errorcode == sqlite3.SQLITE_BUSY:
                    # Release only AFTER SQLite rejected the contender. No
                    # wall-clock race decides whether contention really occurred.
                    release()
                raise

    class Connection(sqlite3.Connection):
        def cursor(self, *args, **kwargs):
            return super().cursor(*args, factory=Cursor, **kwargs)

    def open_connection(*args, **kwargs):
        timeouts.append(kwargs["timeout"])
        db = connect(*args, factory=Connection, **kwargs)
        # Model SQLite's immediate-BUSY paths, which skip the busy handler.
        # The failure itself is a real competing SQLite lock, not a fake error.
        db.execute("PRAGMA busy_timeout=0")
        connections.append(db)
        return db

    monkeypatch.setattr(module.sqlite3, "connect", open_connection)
    return StateDatabase(path), release, errors, connections, timeouts


@pytest.mark.parametrize(
    "mode,failed_sql",
    [("EXCLUSIVE", "PRAGMA journal_mode"), ("IMMEDIATE", "PRAGMA journal_mode=WAL")],
)
def test_transient_busy_during_open_is_retried_with_closed_connections(
    tmp_path, monkeypatch, mode, failed_sql
):
    store, release, errors, connections, timeouts = contended_open(
        tmp_path, monkeypatch, mode=mode, release_on_busy=True
    )
    try:
        with store.read() as db:
            assert db.execute("SELECT x FROM t").fetchall() == [(42,)]
            assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 2000
        assert errors and errors[0][0] == failed_sql
        assert errors[0][1].sqlite_errorcode == sqlite3.SQLITE_BUSY
        assert len(connections) == 2
        assert 0 <= timeouts[1] <= timeouts[0] <= module._BUSY_TIMEOUT_SECONDS
        # Keep the error/traceback referenced while proving every handle closed.
        for connection in connections:
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                connection.execute("SELECT 1")
    finally:
        release()


def test_persistent_open_lock_exhausts_one_budget_and_still_raises(
    tmp_path, monkeypatch
):
    now = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(
        module, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=sleep)
    )
    monkeypatch.setattr(module, "_BUSY_TIMEOUT_SECONDS", 0.1)
    store, release, errors, connections, timeouts = contended_open(
        tmp_path, monkeypatch, mode="EXCLUSIVE", release_on_busy=False
    )
    try:
        with pytest.raises(sqlite3.OperationalError) as caught:
            with store.read():
                pytest.fail("a held exclusive lock must not permit entry")
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        assert sleeps and sum(sleeps) == pytest.approx(0.1)
        assert timeouts == sorted(timeouts, reverse=True)
        for connection in connections:
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                connection.execute("SELECT 1")
        assert errors  # retains exception tracebacks through the close checks
    finally:
        release()


def test_corrupt_database_is_not_retried(tmp_path, monkeypatch):
    path = tmp_path / "corrupt.sqlite3"
    path.write_bytes(b"not a SQLite database" * 100)
    sleeps = []
    import time

    monkeypatch.setattr(
        module, "time", SimpleNamespace(monotonic=time.monotonic, sleep=sleeps.append)
    )
    with pytest.raises(sqlite3.DatabaseError, match="not a database"):
        with StateDatabase(path).read():
            pytest.fail("corrupt data must be rejected")
    assert sleeps == []


@pytest.mark.parametrize("code", [sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_IOERR])
def test_non_busy_operational_error_is_not_retried(tmp_path, monkeypatch, code):
    calls = []
    error = sqlite3.OperationalError("locked")
    error.sqlite_errorcode = code

    def fail_open(self, *, timeout):
        calls.append(timeout)
        raise error

    monkeypatch.setattr(StateDatabase, "_connect_once", fail_open)
    with pytest.raises(sqlite3.OperationalError) as caught:
        with StateDatabase(tmp_path / "state.sqlite3").read():
            pytest.fail("non-BUSY errors must escape")
    assert caught.value is error
    assert len(calls) == 1
