"""#1803 end to end: a real DaemonApplication, real PAC store, real inbox row.

Unit tests pin planning and idempotency against a recording sender.  This one
runs the same reminder through the production shape instead: the daemon's own
``DaemonPacNotificationSender`` over an ``InboxDeliveryIoAdapter`` whose
transport really commits an inbox row, so "the reminder was delivered" is a
committed durable fact rather than a spy's argument list.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.inbox import (
    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
    MemoryDeliveryTransport,
)
from hyprial.daemon.impl.inbox.links.io import (
    CorrelatedInboxEventRouter,
    InboxDeliveryIoAdapter,
)
from hyprial.daemon.impl.inbox.links.local_link import LocalFirstDeliveryTransport
from hyprial.daemon.impl.pac.actors.daemon import DaemonPacNotificationSender
from hyprial.daemon.impl.pac.held_reminder import HeldReminder, HeldReminderConfig
from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService
from hyprial.shell.impl.daemon_wiring import daemon_dependencies

from .support import HOUR_MS, Clock
from tests.support.actor_hosts import new_actor_host

CREATOR = "agent:test:machine:pm"
OPERATOR = "user:test"


def _app(tmp_path: Path) -> DaemonApplication:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    app = DaemonApplication(
        state_dir=state,
        socket_path=tmp_path / "daemon.sock",
        node_id="machine",
        owner="test",
        zenoh_listen=("tcp/127.0.0.1:0",),
        zenoh_connect=(),
        **daemon_dependencies(),
    )
    app.network_isolated = True
    return app


def _inbox_adapter(tmp_path: Path):
    router = CorrelatedInboxEventRouter()
    transport = LocalFirstDeliveryTransport(
        MemoryDeliveryTransport(),
        origin_node="machine",
        local_owner="test",
        local_node_id="machine",
    )
    coordinator = DeliveryCustodyCoordinator(
        tmp_path / "inbox.sqlite3",
        transport,
        router,
        mailbox_capacity=8,
        io_capacity=2, actor_host=new_actor_host(),
    )
    facade = DeliveryCustodyFacade(coordinator, tmp_path / "inbox.sqlite3")
    transport.bind_receiver(facade.receive)
    adapter = InboxDeliveryIoAdapter(
        coordinator,
        router,
        resolve_target=lambda target: target,
        clock_ms=lambda: 1_000,
        completion_timeout=2.0,
        deliver_user=lambda *_args: True,
    )
    return coordinator, facade, adapter


def test_held_graph_delivers_one_durable_reminder_to_a_real_daemon_inbox(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    coordinator, facade, adapter = _inbox_adapter(tmp_path)
    app._pac_notification_io = adapter
    app.agents.create("pm")
    clock = Clock()
    workflow = GraphWorkflowService(
        state_dir=app.state_dir,
        owner="test",
        machine="machine",
        sender=DaemonPacNotificationSender(app),
        clock_ms=clock,
        start_thread=False,
    )
    hour = HeldReminder(
        database=workflow.database,
        owner="test",
        config=HeldReminderConfig(threshold_ms=6 * HOUR_MS),
        creator_exists=app.agents.exists,
        operator_route=OPERATOR,
        clock_ms=clock,
        wake_delivery=lambda: workflow._tick(),
    )
    try:
        graph = workflow.start(
            yaml_text="\n".join(
                [
                    "version: 2",
                    "name: held-e2e",
                    "on_failure: hold",
                    "nodes:",
                    "  - id: review",
                    f"    owner: {CREATOR}",
                    "    task: review it",
                ]
            ),
            sender=CREATOR,
            operation_key="held-e2e",
        )["graphId"]
        workflow._tick()
        row = next(
            node for node in workflow.status(run_id=graph)["nodes"]
            if node["nodeId"] == "review"
        )
        workflow.fail(
            graph_id=graph,
            node_id="review",
            actor=CREATOR,
            request_id=row["requestId"],
            reason_ref="output:review",
        )
        held = workflow.status(run_id=graph)
        assert held["state"] == "held"
        clock.advance(7 * HOUR_MS)

        assert len(hour.run_once()) == 1
        workflow._tick()

        # The inbox already holds the earlier turn and failure alert; the
        # reminder is the row this feature owns.
        rows = [
            item for item in facade.pending_all()
            if item.recipient == CREATOR and b"PAC held-reminder:" in item.payload
        ]
        assert len(rows) == 1
        body = rows[0].payload.decode()
        assert graph in body
        assert "output:review" in body
        assert rows[0].sender == CREATOR

        # Re-running inside the window is a durable no-op, not a second send.
        clock.advance(24 * HOUR_MS - 1)
        assert hour.run_once() == []
        workflow._tick()
        assert len(
            [
                item for item in facade.pending_all()
                if item.recipient == CREATOR
                and b"PAC held-reminder:" in item.payload
            ]
        ) == 1
        clock.advance(1)
        assert len(hour.run_once()) == 1
        workflow._tick()
        rows = [
            item for item in facade.pending_all()
            if item.recipient == CREATOR and b"PAC held-reminder:" in item.payload
        ]
        assert len(rows) == 2
        assert len({item.message_id for item in rows}) == 2
        after = workflow.status(run_id=graph)
        assert after["state"] == held["state"]
        assert after["nodes"] == held["nodes"]
    finally:
        hour.close()
        workflow.close()
        coordinator.drain(timeout=2.0)
        app._close()
        app._close_home_lease()


@pytest.mark.parametrize("enabled", ["0", "1"])
def test_application_composition_owns_the_reminder_and_runs_it_after_restore(
    tmp_path: Path, monkeypatch, enabled: str,
) -> None:
    """The daemon wires the reminder; restore's own call runs it immediately."""

    monkeypatch.setenv("HYPRIAL_PAC_HELD_REMINDER", enabled)
    monkeypatch.setenv("HYPRIAL_PAC_HELD_REMINDER_THRESHOLD_SECONDS", "3600")
    app = _app(tmp_path)
    coordinator, _facade, adapter = _inbox_adapter(tmp_path)
    app._pac_notification_io = adapter
    app.agents.create("pm")
    clock = Clock()
    workflow = GraphWorkflowService(
        state_dir=app.state_dir,
        owner="test",
        machine="machine",
        sender=DaemonPacNotificationSender(app),
        clock_ms=clock,
        start_thread=False,
    )
    try:
        graph = workflow.start(
            yaml_text="\n".join(
                [
                    "version: 2",
                    "name: composed",
                    "on_failure: hold",
                    "nodes:",
                    "  - id: review",
                    f"    owner: {CREATOR}",
                    "    task: review it",
                ]
            ),
            sender=CREATOR,
            operation_key="composed",
        )["graphId"]
        workflow._tick()
        row = next(
            node for node in workflow.status(run_id=graph)["nodes"]
            if node["nodeId"] == "review"
        )
        workflow.fail(
            graph_id=graph,
            node_id="review",
            actor=CREATOR,
            request_id=row["requestId"],
            reason_ref="output:review",
        )
        clock.advance(2 * HOUR_MS)
        app._workflow_service = workflow

        app._compose_held_reminder()
        assert app._held_reminder is not None
        assert app._held_reminder.config.threshold_ms == HOUR_MS
        # Keep the composed reminder on the graph's test clock: at two hours
        # the environment threshold fires, but the default six hours would not.
        app._held_reminder._clock = clock
        app._start_held_reminder()
        workflow._tick()

        assert any(
            item.recipient == CREATOR and b"PAC held-reminder:" in item.payload
            for item in _facade.pending_all()
        ) is (enabled == "1")
        app._held_reminder.close()
        app._held_reminder = None
    finally:
        workflow.close()
        coordinator.drain(timeout=2.0)
        app._close()
        app._close_home_lease()
