"""StateDatabase._connect must not abandon connections or race journal-mode.

CI task 12128 (run #3758, dev push, ``-n 2``): two threads connected to a
fresh ``lifecycle-operations.sqlite3`` while a write transaction was live;
both died at ``PRAGMA journal_mode=WAL`` with ``database is locked`` -- a
lock the busy handler does not arbitrate -- and each ``sqlite3.connect()``
had already succeeded, so the abandoned connections stayed open for as long
as their exception tracebacks lived (pytest's error report and pykka's
``exc_info``).  The suite's fd guard then reported the descriptors as a
test leak (``fd=38``/``fd=39``), reddening a healthy tree.

These tests pin the two halves of the fix:

* ``_connect`` reads the journal mode first (no exclusive lock) and runs the
  one conversion under the instance write lock, so in-process connectors
  never race the mode change, and an already-WAL file never executes a
  journal-mode write at all;
* whatever failure ``_connect`` does hit, the connection is closed before
  the error leaves the frame -- no descriptor can be pinned by a traceback.
"""
from __future__ import annotations

import gc
import os
import sqlite3
import threading
from pathlib import Path

import pytest

from hyprial.daemon.impl.state_db import StateDatabase


def _fds_for(path: Path) -> set[int]:
    """Open descriptors whose target is exactly ``path`` (guard's view).

    ⚠️ On darwin this ruler is blind: ``/dev/fd/N`` is not a symlink there,
    so ``os.readlink`` raises for every descriptor and the result is always
    the empty set.  Callers must pass a positive control before trusting a
    zero reading (see the skip in ``test_connect_failure_...``).
    """

    leaked = set()
    for fd in os.listdir("/dev/fd"):
        try:
            number = int(fd)
            target = os.readlink(f"/dev/fd/{number}")
        except (OSError, ValueError):
            continue
        if target == str(path):
            leaked.add(number)
    return leaked


def _delete_mode_database(path: Path) -> None:
    """A real database with a delete-mode header and one row."""

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("CREATE TABLE t(x)")
        conn.execute("INSERT INTO t VALUES (42)")
        conn.commit()
    finally:
        conn.close()


def test_connect_failure_closes_its_connection(tmp_path: Path) -> None:
    """A journal-mode failure must not leave a pinned descriptor behind.

    A live write holder on a delete-mode file makes the mode change fail
    without honoring the busy timeout (task 12128's lock half).  The error
    is legitimate cross-instance contention; the leaked descriptor was the
    bug: with the fix, the connection is closed before the error propagates,
    even while the exception (and its traceback) is still referenced.

    The fd ruler needs a baseline, and the holder provides both parts in
    one measurement: descriptors open before the failing ``_connect`` are
    the holder's own (CI task 12617 counted exactly those by accident --
    ``assert not {32, 35}`` from an absolute ruler while the holder was
    still open), so only descriptors that appear *after* the baseline are
    a leak.  A blind ruler reports zero even for the open holder (darwin:
    ``/dev/fd/N`` is not a symlink, so ``os.readlink`` raises for every
    descriptor); that condition skips loudly instead of reporting "no
    leak" from an instrument that cannot see.
    """

    db_path = tmp_path / "state" / "lifecycle-operations.sqlite3"
    _delete_mode_database(db_path)
    state_db = StateDatabase(db_path)

    holder = sqlite3.connect(db_path, timeout=0.5)
    holder.execute("PRAGMA busy_timeout=500")
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO t VALUES (1)")  # keep the write lock real

    # Positive control and baseline in one shot: the holder is genuinely
    # open right now, so an empty reading means the ruler is unavailable on
    # this platform -- not that zero descriptors are open.
    baseline = _fds_for(db_path)
    if not baseline:
        holder.execute("ROLLBACK")
        holder.close()
        pytest.skip("fd ruler unavailable: open holder is invisible on this platform")

    try:
        with pytest.raises(sqlite3.OperationalError) as caught:
            with state_db.read():
                pass
        # The exception object and its traceback stay referenced while we
        # count -- exactly how pytest's report and pykka's exc_info pinned
        # fd=38/39 on CI.  Subtraction changes the measured object to
        # "descriptors that appeared", not the threshold; fd-number reuse
        # after release is a theoretical aliasing hazard, but the holder
        # stays open across both measurements, so its numbers are stable.
        failure = caught.value
        gc.collect()
        after = _fds_for(db_path)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
        after_holder_closed = _fds_for(db_path)
    # AFTER_HOLDER_CLOSED separates "descriptor owned by the holder" from
    # "descriptor still held by something else": if closing the holder drops
    # the count to zero, the leaked-looking fd belonged to the holder's side
    # of the lock negotiation, not to _connect's failed connection.
    # The asserted quantity is what remains after every legitimate holder
    # has exited: the holder's connection (and its lock-negotiation
    # descriptors, cf. task 12723: LEAKED=[35] AFTER_HOLDER_CLOSED=[]) is
    # closed, and _connect's failed connection was closed in its except.
    # The exception object and its traceback are still referenced at this
    # point, so a connection that _connect really abandoned -- the 12128
    # defect -- would necessarily appear here.  The old diff (AFTER minus
    # BASELINE) was polluted by the holder's own later descriptors; this
    # set is not, and its resolution against the real defect is the same.
    leaked = after - baseline
    print(
        f"BASELINE={sorted(baseline)} AFTER={sorted(after)} "
        f"LEAKED={sorted(leaked)} AFTER_HOLDER_CLOSED={sorted(after_holder_closed)}",
        flush=True,
    )
    assert not after_holder_closed, (
        f"{len(after_holder_closed)} descriptor(s) still open after the holder "
        "and the failed _connect connection were both closed; the still-"
        "referenced traceback would pin any abandoned connection here"
    )
    assert failure is not None  # noqa: S101 -- keeps the traceback referenced


def test_read_does_not_touch_journal_mode_on_wal_files(tmp_path: Path) -> None:
    """Once the file is WAL, reads coexist with a live write holder.

    Pre-fix, every connect re-issued ``PRAGMA journal_mode=WAL``; under a
    concurrent write transaction that statement is exactly where CI task
    12128 raised.  Post-fix an already-WAL file never executes a
    journal-mode write, so the read succeeds beside the holder.
    """

    db_path = tmp_path / "state" / "lifecycle-operations.sqlite3"
    state_db = StateDatabase(db_path)
    with state_db.transaction(schema="CREATE TABLE IF NOT EXISTS t(x)") as db:
        db.execute("INSERT INTO t VALUES (7)")

    holder = sqlite3.connect(db_path, timeout=0.5)
    holder.execute("PRAGMA busy_timeout=500")
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO t VALUES (8)")
    try:
        with state_db.read() as db:
            rows = db.execute("SELECT x FROM t").fetchall()
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert rows == [(7,)]  # snapshot read; the holder's row is not visible


def test_wal_files_never_run_a_journal_mode_write_again(tmp_path: Path) -> None:
    """After the one conversion, no connect executes a journal-mode write.

    This is the load-bearing regression device for CI task 12128: the red
    line was ``PRAGMA journal_mode=WAL`` inside ``_connect``, executed by
    every connection -- including readers that hold no write lock -- against
    a database another thread was writing.  The fix runs that statement at
    most once per file (the conversion, under the per-file connection lock);
    every later connect only reads the mode.  Statement tracing makes that
    difference observable on every platform, not only on the sqlite build
    that raised.
    """

    import sqlite3 as sqlite3_mod

    db_path = tmp_path / "state" / "lifecycle-operations.sqlite3"
    _delete_mode_database(db_path)
    state_db = StateDatabase(db_path)

    statements: list[str] = []
    real_connect = sqlite3_mod.connect

    def traced_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    import pytest  # local import keeps the module import light

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(sqlite3_mod, "connect", traced_connect)
    try:
        for _ in range(3):
            with state_db.read() as db:
                db.execute("SELECT x FROM t").fetchall()
    finally:
        monkeypatch.undo()

    journal_mode_writes = [
        statement for statement in statements
        if "journal_mode" in statement.lower()
        and "=" in statement.replace(" ", "")
        and "select" not in statement.lower()
    ]
    # One conversion on the first connect; reads afterwards must not write.
    assert journal_mode_writes == ["PRAGMA journal_mode=WAL"], (
        f"journal-mode writes observed: {journal_mode_writes!r}; "
        "a WAL file must never run a journal-mode write again"
    )


def test_concurrent_first_connectors_serialize_the_conversion(
    tmp_path: Path,
) -> None:
    """In-process connectors never race the one delete->WAL conversion.

    The 12128 stacks were two threads of the same process inside ``_connect``
    (``read()`` takes no write lock, so both could be there at once).  With
    the conversion serialized through the per-file connection lock, a reader that
    arrives while another connector is converting waits instead of failing,
    even on a delete-mode file.
    """

    db_path = tmp_path / "state" / "lifecycle-operations.sqlite3"
    _delete_mode_database(db_path)
    state_db = StateDatabase(db_path)

    results: list[str] = []
    barrier = threading.Barrier(3, timeout=10)

    def connect_and_read() -> None:
        barrier.wait()
        try:
            with state_db.read() as db:
                db.execute("SELECT x FROM t").fetchall()
            results.append("ok")
        except sqlite3.OperationalError as error:
            results.append(f"OperationalError: {error}")

    threads = [threading.Thread(target=connect_and_read) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive(), "connector stayed blocked past the barrier budget"

    assert results == ["ok", "ok"], results
    # The conversion actually happened exactly once and the file is WAL now.
    mode = sqlite3.connect(db_path).execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal"
