"""Journal reads do not queue behind the single state writer.

Lifecycle root-fix plan S4: every lifecycle step re-reads the journal (load,
state, effect_done, effect_receipt, ...), and each read was a command on the
single writer's mailbox, behind whatever writes were queued.  A read only
needs committed state and opens its own SQLite connection, so it now runs on
the caller's thread.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.state_db import StateDatabase
from hyprial.daemon.impl.state_persistence import ReadJournalQuery, StatePersistenceAuthority

SLOW_WRITE = 0.5


def _authority(tmp_path: Path, actor_host) -> StatePersistenceAuthority:
    state_db = StateDatabase(tmp_path / "lifecycle-operations.sqlite3")
    return StatePersistenceAuthority(
        DesiredStateStore(tmp_path / "desired.json", state_db), actor_host=actor_host
    )


class _RecordingCell:
    def __init__(self, cell, told: list[type]) -> None:
        self._cell = cell
        self._told = told

    def tell(self, command):  # type: ignore[no-untyped-def]
        self._told.append(type(command))
        return self._cell.tell(command)

    def __getattr__(self, name):  # type: ignore[no-untyped-def]
        return getattr(self._cell, name)


def test_a_journal_read_does_not_wait_for_a_slow_write(tmp_path: Path, actor_host, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    authority = _authority(tmp_path, actor_host)
    writing = threading.Event()
    original = authority._journal_store.next_generation

    def slow_write():  # type: ignore[no-untyped-def]
        writing.set()
        time.sleep(SLOW_WRITE)
        return original()

    monkeypatch.setattr(authority._journal_store, "next_generation", slow_write)
    told: list[type] = []
    authority._cell = _RecordingCell(authority._cell, told)
    try:
        writer = threading.Thread(target=authority.journal.next_generation)
        writer.start()
        assert writing.wait(2)

        started = time.monotonic()
        assert authority.settled_journal.pending() == ()
        assert authority.journal.pending() == ()
        elapsed = time.monotonic() - started
        writer.join(5)

        assert elapsed < SLOW_WRITE / 2, elapsed
        assert ReadJournalQuery not in told, told
    finally:
        assert authority.close()
