from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from hyprial.kernel.impl.actor_runtime import AdmissionResult, ActorState
from hyprial.kernel.impl.contracts.ports import PortAdmission, PortCommandRejected
from hyprial.daemon.impl.inbox.actor import DeliveryCustodyCoordinator
from hyprial.daemon.impl.inbox.memory import MemoryDeliveryTransport
from hyprial.daemon.impl.inbox.contracts.ports import CloseInboxCommand, InboxClosed, ReceiveMessageCommand

from ..support import (
    _Events,
    _message,
    _runtime,
    _wait_until,
)


def test_close_drains_admitted_work_and_rejects_new_commands(tmp_path: Path) -> None:
    events = _Events()
    coordinator = DeliveryCustodyCoordinator(
        tmp_path / "inbox.sqlite3",
        MemoryDeliveryTransport(),
        events,
        runtime=_runtime(),
    )
    assert coordinator.submit(ReceiveMessageCommand("receive", _message())) is (
        PortAdmission.ACCEPTED
    )
    assert coordinator.submit(CloseInboxCommand("close")) is PortAdmission.ACCEPTED
    assert coordinator.submit(ReceiveMessageCommand("late", _message("m2"))) is (
        PortAdmission.CLOSING
    )
    _wait_until(lambda: any(isinstance(item, InboxClosed) for item in events.snapshot()))
    assert coordinator.drain(1.0).complete is True
    assert any(
        isinstance(item, PortCommandRejected)
        and item.correlation_id == "late"
        and item.code == "PORT_CLOSING"
        for item in events.snapshot()
    )


def test_terminal_actor_state_still_closes_the_port(tmp_path: Path) -> None:
    """A quarantined or stopped actor is terminal: the port stays closed."""

    events = _Events()
    coordinator = DeliveryCustodyCoordinator(
        tmp_path / "inbox.sqlite3",
        MemoryDeliveryTransport(),
        events,
        runtime=_runtime(),
    )
    coordinator._runtime.tell = (  # type: ignore[assignment]
        lambda handle, command: AdmissionResult.CLOSED
    )
    live_snapshot = coordinator._runtime.snapshot
    coordinator._runtime.snapshot = (  # type: ignore[assignment]
        lambda handle: replace(live_snapshot(handle), state=ActorState.QUARANTINED)
    )

    assert coordinator.submit(ReceiveMessageCommand("first", _message())) is (
        PortAdmission.CLOSING
    )
    assert coordinator._closing is True
    assert coordinator.drain(1.0).complete is True


def test_direct_drain_finishes_admitted_work_and_releases_storage(
    tmp_path: Path,
) -> None:
    events = _Events()
    coordinator = DeliveryCustodyCoordinator(
        tmp_path / "inbox.sqlite3",
        MemoryDeliveryTransport(),
        events,
        runtime=_runtime(),
    )
    message = _message()
    assert coordinator.submit(ReceiveMessageCommand("receive", message)) is (
        PortAdmission.ACCEPTED
    )
    assert coordinator.drain(1.0).complete is True
    assert coordinator.read_pending(message.recipient) == (message,)
    assert coordinator.submit(ReceiveMessageCommand("late", _message("m2"))) is (
        PortAdmission.CLOSING
    )
