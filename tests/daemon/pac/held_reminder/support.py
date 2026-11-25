"""Fixtures for the PAC held-reminder (#1803).

A held graph is produced by the real workflow service, not by poking rows, so
every test starts from the transition the product actually writes.
"""

from __future__ import annotations

import yaml

from hyprial.daemon.impl.pac.storage.store import PacGraphStore
from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService

USER = "user:test"
AGENT = "agent:test:machine:worker"
OPERATOR = "user:ops"

HOUR_MS = 3_600_000


class Sender:
    """The daemon's notification port, reduced to its observable effect."""

    def __init__(self) -> None:
        self.messages: dict[str, dict] = {}

    def send(self, **kwargs) -> str:
        key = kwargs["idempotency_key"]
        self.messages.setdefault(key, kwargs)
        return key

    def reminders(self) -> list[dict]:
        return [
            item for key, item in self.messages.items()
            if "held-reminder:" in key
        ]


class Clock:
    def __init__(self, now: int = 1_000) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now

    def advance(self, ms: int) -> None:
        self.now += ms


def service(tmp_path, sender: Sender, clock: Clock) -> GraphWorkflowService:
    return GraphWorkflowService(
        state_dir=tmp_path / "state",
        owner="test",
        machine="machine",
        sender=sender,
        clock_ms=clock,
        start_thread=False,
    )


def start_graph(service: GraphWorkflowService, nodes, *, key: str) -> str:
    return service.start(
        yaml_text=yaml.safe_dump(
            {"version": 2, "name": "held", "nodes": nodes, "on_failure": "hold"}
        ),
        sender=USER,
        operation_key=key,
    )["graphId"]


def fail_node(service: GraphWorkflowService, graph: str, node_id: str) -> None:
    snapshot = service.status(run_id=graph)
    row = next(item for item in snapshot["nodes"] if item["nodeId"] == node_id)
    service.fail(
        graph_id=graph,
        node_id=node_id,
        actor=row["owner"],
        request_id=row["requestId"],
        reason_ref=f"output:{node_id}",
    )


def held_graph(
    service: GraphWorkflowService,
    *,
    key: str = "held",
    node_id: str = "review",
) -> str:
    """One actor node, failed under ``on_failure: hold``."""

    graph = start_graph(
        service, [{"id": node_id, "task": "review it", "owner": AGENT}], key=key
    )
    service._tick()
    fail_node(service, graph, node_id)
    assert service.status(run_id=graph)["state"] == "held"
    return graph


def graph_state(service: GraphWorkflowService, graph: str) -> str:
    return str(service.status(run_id=graph)["state"])


def reminder_rows(service: GraphWorkflowService, graph: str | None = None) -> list:
    store = PacGraphStore(service.database, read_only=True)
    try:
        rows = store._db.execute(
            "SELECT * FROM notifications WHERE event_id LIKE 'held-reminder:%' "
            "ORDER BY event_id, edge"
        ).fetchall()
        if graph is None:
            return list(rows)
        return [
            row for row in rows
            if row["plan_json"] is not None and graph in row["event_id"]
        ]
    finally:
        store.close()
