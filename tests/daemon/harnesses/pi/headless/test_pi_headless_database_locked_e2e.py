"""Deterministic reproduction of the CI face "lifecycle.start rejected: database is locked".

CI task 15631 (``test_pi_headless_worker_fetch_path_is_not_rejected``) failed
with the daemon answering

    daemon rejected lifecycle.start: {'code': 'DAEMON_ERROR',
                                      'message': 'database is locked'}

The trigger is a second connection holding the shared state database's write
lock longer than the 2.0s busy timeout.  This file makes that holder
deterministic: it opens the daemon's own ``lifecycle-operations.sqlite3`` from
the test process (a real second connection), takes ``BEGIN IMMEDIATE``, and
holds it until the test explicitly releases it -- never for a fixed wall-clock
duration, because a release timer makes the outcome depend on which clock
fires first (the holder's release or the daemon's give-up), which is exactly
the flake class CI task 16836 lost.

Before ``StateDatabase`` retried a transient lock, the daemon answered
``DAEMON_ERROR: database is locked`` after ~2.1s (measured). The lifecycle
reservation now has its own bounded completion contract: the caller receives
``SUBMIT_OUTCOME_UNKNOWN`` with the stable operation ID while the accepted
reservation keeps its original custody and completes after the lock clears.
"""

from __future__ import annotations

from tests.support.runtime._tmp import hyprial_test_tmp_root

import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.daemon.channels.mcp.lifecycle.test_mcp_channel_owner_migration_e2e import IsolatedHarness

OWNER = "owner-a"
WORKER = "db-lock-worker"


def _open_write_lock_holder(db_path: Path) -> sqlite3.Connection:
    """Acquire the test writer lock, waiting out transient setup writers."""

    holder = sqlite3.connect(db_path, timeout=10.0, check_same_thread=False)
    try:
        holder.execute("PRAGMA busy_timeout=10000")
        holder.execute("BEGIN IMMEDIATE")
    except BaseException:
        holder.close()
        raise
    return holder


@pytest.fixture()
def harness() -> Iterator[IsolatedHarness]:
    root = Path(tempfile.mkdtemp(dir=hyprial_test_tmp_root(), prefix="hyprial-dblock-"))
    value = IsolatedHarness(root)
    value.write_owner(OWNER)
    value.start_daemon()
    try:
        yield value
    finally:
        try:
            value.ipc("down", {"target": WORKER, "provider": "pi"})
        except Exception:  # noqa: BLE001 - teardown must not mask the failure
            pass
        value.stop_daemon()
        shutil.rmtree(root, ignore_errors=True)


def test_lifecycle_start_survives_a_writer_past_the_busy_timeout(
    harness: IsolatedHarness,
) -> None:
    harness.ipc("agent.create", {"name": "sender"})
    db_path = harness.state / "lifecycle-operations.sqlite3"
    assert db_path.exists()

    holder = _open_write_lock_holder(db_path)
    try:
        release = threading.Event()

        def hold() -> None:
            release.wait(timeout=30)
            holder.execute("ROLLBACK")

        holder_thread = threading.Thread(target=hold, name="write-lock-holder")
        holder_thread.start()

        observed: dict[str, object] = {}
        finished = threading.Event()

        def start() -> None:
            try:
                observed["value"] = harness.ipc(
                    "lifecycle.start",
                    {
                        "provider": "pi",
                        "name": WORKER,
                        "headless": True,
                        "command": [sys.executable, "-c", "import time; time.sleep(10)"],
                        "cwd": str(harness.root),
                        "args": [],
                    },
                )
            except BaseException as error:  # noqa: BLE001 - surfaced below
                observed["error"] = error
            finally:
                finished.set()

        caller = threading.Thread(target=start, name="lifecycle-start")
        try:
            caller.start()
            assert finished.wait(8.0), "bounded lifecycle reservation did not return"
            assert "SUBMIT_OUTCOME_UNKNOWN" in str(observed.get("error")), observed
            assert "operationId" in str(observed["error"]), observed
        finally:
            release.set()
            caller.join(timeout=30)
            holder_thread.join(timeout=10)
    finally:
        holder.close()

    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        connectors = harness.ipc("ps", {}).get("connectors", [])
        if any(
            isinstance(item, dict) and item.get("name") == WORKER
            for item in connectors
        ):
            break
        time.sleep(0.1)
    else:
        raise AssertionError(
            f"accepted lifecycle operation did not recover after unlock: {observed!r}"
        )


def test_holder_acquisition_waits_for_a_competing_writer(tmp_path: Path) -> None:
    db_path = tmp_path / "lifecycle-operations.sqlite3"
    setup = sqlite3.connect(db_path)
    try:
        setup.execute("CREATE TABLE marker (value INTEGER)")
        setup.commit()
    finally:
        setup.close()

    competing_ready = threading.Event()
    competing_errors: list[BaseException] = []

    def compete() -> None:
        connection = sqlite3.connect(db_path, timeout=10.0, check_same_thread=False)
        try:
            connection.execute("PRAGMA busy_timeout=10000")
            connection.execute("BEGIN IMMEDIATE")
            competing_ready.set()
            time.sleep(0.3)
            connection.execute("ROLLBACK")
        except BaseException as error:  # noqa: BLE001 - surfaced below
            competing_errors.append(error)
            competing_ready.set()
        finally:
            connection.close()

    competing = threading.Thread(target=compete, name="competing-writer")
    competing.start()
    assert competing_ready.wait(timeout=5)

    holder: sqlite3.Connection | None = None
    try:
        holder = _open_write_lock_holder(db_path)
    finally:
        if holder is not None:
            holder.execute("ROLLBACK")
            holder.close()
        competing.join(timeout=10)

    assert not competing.is_alive()
    assert competing_errors == []
