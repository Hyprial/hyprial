"""Connections must not open while the same process closes the last WAL handle."""

import sqlite3
import threading

import pytest

from hyprial.daemon.impl.state_db import StateDatabase


@pytest.mark.parametrize("closing_kind", ["read", "transaction"])
def test_open_waits_for_in_process_close_without_replaying_sql(
    tmp_path, monkeypatch, closing_kind
):
    path = tmp_path / "state.sqlite3"
    first = StateDatabase(path)
    second = StateDatabase(path)
    with first.transaction(schema="CREATE TABLE t(x)") as db:
        db.execute("INSERT INTO t VALUES (42)")
    closing = threading.Event()
    release_close = threading.Event()
    contender_at_boundary = threading.Event()
    raced = []
    errors = []
    rows = []
    connect = sqlite3.connect

    # This probe signals BEFORE attempting the real lock. It doesn't serialize
    # anything that the implementation hasn't already chosen to serialize.
    class ObservedLock:
        def __init__(self, underlying):
            self.underlying = underlying

        def __enter__(self):
            if threading.current_thread().name == "contender":
                contender_at_boundary.set()
            self.underlying.acquire()

        def __exit__(self, *args):
            self.underlying.release()

    lock = getattr(first, "_connection_lock", threading.RLock())
    assert getattr(second, "_connection_lock", lock) is lock
    observed = ObservedLock(lock)
    first._connection_lock = second._connection_lock = observed

    class Connection(sqlite3.Connection):
        def close(self):
            if threading.current_thread().name == "closer":
                closing.set()
                if not release_close.wait(5):
                    raise AssertionError("test did not release close")
            return super().close()

    def open_connection(*args, **kwargs):
        if threading.current_thread().name == "contender":
            if closing.is_set() and not release_close.is_set():
                raced.append(True)
                contender_at_boundary.set()
                # Model last-close's exclusive cleanup window, without relying
                # on slow fsync or sleep scheduling to manufacture the race.
                error = sqlite3.OperationalError("database is locked")
                error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                raise error
        return connect(*args, factory=Connection, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", open_connection)

    def worker(store, kind):
        try:
            with getattr(store, kind)() as db:
                rows.append(db.execute("SELECT x FROM t").fetchall())
        except BaseException as error:
            errors.append(error)

    closer = threading.Thread(target=worker, args=(first, closing_kind), name="closer")
    contender = threading.Thread(target=worker, args=(second, "read"), name="contender")
    closer.start()
    try:
        assert closing.wait(5)
        contender.start()
        assert contender_at_boundary.wait(5), (
            "contender did not attempt the open boundary"
        )
    finally:
        release_close.set()
        closer.join(5)
        if contender.ident is not None:
            contender.join(5)
    assert not closer.is_alive() and not contender.is_alive()
    assert not errors
    assert raced == [], "a fresh open overlapped the last connection's close"
    assert rows == [[(42,)], [(42,)]]


def test_read_body_remains_concurrent_with_in_process_writer(tmp_path):
    store = StateDatabase(tmp_path / "state.sqlite3")
    with store.transaction(schema="CREATE TABLE t(x)") as db:
        db.execute("INSERT INTO t VALUES (42)")
    errors = []
    rows = []

    def read():
        try:
            with store.read() as db:
                rows.extend(db.execute("SELECT x FROM t").fetchall())
        except BaseException as error:
            errors.append(error)

    with store.transaction() as db:
        db.execute("INSERT INTO t VALUES (99)")
        reader = threading.Thread(target=read)
        reader.start()
        reader.join(5)
        finished_during_write = not reader.is_alive()
    reader.join(5)
    assert finished_during_write, (
        "connection guard accidentally serialized transaction bodies"
    )
    assert errors == []
    assert rows == [(42,)]
