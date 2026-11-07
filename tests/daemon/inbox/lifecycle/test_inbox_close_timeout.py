"""An unanswered inbox close must not abandon the custody database.

CI tasks 25544 and 27556: ``first.close()`` got no ``InboxClosed`` within the
facade timeout while the xdist worker was stalled.  ``shutdown`` raised before
it ever drained, so the stopped-too-late actor kept its SQLite connection open
and the fd hygiene fixture reported ``inbox.sqlite3``/``-wal``/``-shm`` as
leaked.  The timeout is still the caller's to see; the descriptors are not.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from hyprial.daemon.impl.inbox import (
    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
    MemoryDeliveryTransport,
)
from hyprial.daemon.impl.inbox.actor import DeliveryCustody, InboxAuthorityTimeout
from hyprial.daemon.impl.inbox.contracts.ports import CloseInboxCommand


class _Events:
    def publish(self, _event: object) -> None:
        pass


def test_a_close_answered_after_its_deadline_still_releases_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()
    original_handle = DeliveryCustody._handle

    def stalled_on_close(self: DeliveryCustody, command: object) -> None:
        if isinstance(command, CloseInboxCommand):
            release.wait(10.0)
        original_handle(self, command)

    monkeypatch.setattr(DeliveryCustody, "_handle", stalled_on_close)
    database = tmp_path / "inbox.sqlite3"
    coordinator = DeliveryCustodyCoordinator(
        database, MemoryDeliveryTransport(), _Events(), node_id="close-timeout"
    )
    facade = DeliveryCustodyFacade(coordinator, database, timeout=0.2)
    handler = coordinator._active_handler
    assert handler is not None

    original_drain = coordinator.drain

    def drain_after_the_stall_ends(timeout: float = 5.0):
        # The stall ends only after the reply deadline has passed.
        release.set()
        return original_drain(timeout)

    monkeypatch.setattr(coordinator, "drain", drain_after_the_stall_ends)

    with pytest.raises(InboxAuthorityTimeout) as timed_out:
        facade.close()

    # The timeout names what the actor was doing: stuck in the close itself.
    assert "queued=0 in_flight=1" in str(timed_out.value)
    assert "waited " in str(timed_out.value)

    assert release.is_set(), "shutdown gave up without draining"
    with pytest.raises(sqlite3.ProgrammingError):
        handler._service._db.execute("SELECT 1")
