"""The single state writer reports how long commands queue, not only their CPU.

Lifecycle root-fix plan S1 (docs/design/lifecycle-root-fix-plan-2026-10-05.md):
on production 0.5.0, 12 of 15 stack dumps had the caller waiting on one
journal read while the writer itself looked idle, and ``ps`` only showed the
writer's CPU.  ``statePersistenceLatency`` adds the mailbox depth, execution
wall time and the caller's round trip; round trip minus execution is time
spent queued behind other commands.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.state_db import StateDatabase
from hyprial.daemon.impl.state_persistence import StatePersistenceAuthority

SLOW = 0.2


def _authority(tmp_path: Path) -> StatePersistenceAuthority:
    state_db = StateDatabase(tmp_path / "lifecycle-operations.sqlite3")
    return StatePersistenceAuthority(DesiredStateStore(tmp_path / "desired.json", state_db))


def test_a_command_queued_behind_a_slow_one_shows_the_wait(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    authority = _authority(tmp_path)
    original = authority._journal_store.next_generation

    def slow_next_generation():  # type: ignore[no-untyped-def]
        time.sleep(SLOW)
        return original()

    monkeypatch.setattr(authority._journal_store, "next_generation", slow_next_generation)
    try:
        callers = [
            threading.Thread(target=authority.journal.next_generation) for _ in range(2)
        ]
        for caller in callers:
            caller.start()
        for caller in callers:
            caller.join(5)

        status = authority.latency_status()
        (key,) = [name for name, row in status["execution"].items() if row["calls"] == 2]
        execution = status["execution"][key]
        round_trip = status["roundTrip"][key]

        assert execution["maxWallMs"] >= SLOW * 1000 * 0.9, execution
        # The second caller waited for the first: its round trip spans both.
        assert round_trip["maxWallMs"] >= 2 * SLOW * 1000 * 0.9, round_trip
        assert round_trip["wallMs"] > execution["wallMs"], (round_trip, execution)
        assert status["mailbox"]["capacity"] == 128
        assert status["mailbox"]["queued"] == 0
        assert status["mailbox"]["pendingCompletions"] == 0
    finally:
        assert authority.close()
