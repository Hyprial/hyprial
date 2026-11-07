"""Real workflow/SQLite fixtures for production-scale tick observations."""

from __future__ import annotations

import json
from time import thread_time_ns

import yaml

from hyprial.daemon.impl.pac.storage.store import PacGraphStore
from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService

USER = "user:tick-test"
WORKER = "agent:tick-test:machine:worker"


class Sender:
    def __init__(self):
        self.sent = []

    def send(self, **kwargs):
        self.sent.append(kwargs)
        return kwargs["idempotency_key"]


def build_scale(path, graph_count=700, notification_count=2000, steps=1):
    sender, clock = Sender(), [1000]
    service = GraphWorkflowService(
        state_dir=path, owner="tick-test", machine="machine", sender=sender,
        clock_ms=lambda: clock[0], start_thread=False,
    )
    text = yaml.safe_dump({
        "version": 2, "name": "tick-scale", "on_failure": "terminate",
        "nodes": [
            {"id": f"work-{index}", "owner": WORKER, "task": "await explicit completion",
             "timeout": "1h", **({"after": [f"work-{index - 1}"]} if index else {})}
            for index in range(steps)
        ],
    })
    graphs = []
    for index in range(graph_count):
        clock[0] = 1000 + index
        graphs.append(service.start(yaml_text=text, sender=USER, operation_key=f"scale-{index}")["graphId"])
    clock[0] = 2000
    service._tick()
    with_store = PacGraphStore(service.database)
    try:
        existing = with_store._db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
        with with_store.write():
            for index in range(notification_count - existing):
                with_store._db.execute(
                    "INSERT INTO notifications(event_id,edge,kind,recipient,text,sender,"
                    "message_id,at,delivered_at,plan_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (f"history-{index}", f"edge-{index}", "actor_alert", USER,
                     "historical delivered alert", USER, f"delivered-{index}", 1, 2,
                     json.dumps({"graphId": graphs[index % graph_count]})),
                )
        assert with_store._db.execute("SELECT COUNT(*) FROM graphs").fetchone()[0] == graph_count
        assert with_store._db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == notification_count
    finally:
        with_store.close()
    service._tick(2001)
    service._tick(2002)
    return service, sender, clock, graphs


def measured_tick(service, monkeypatch, at):
    facts = {"vmSteps": 0, "statements": 0, "materializedNodeRows": 0, "projections": []}
    open_store, project = service._open, service._project
    read_nodes = PacGraphStore.nodes

    def progress():
        facts["vmSteps"] += 100
        return 0

    def statement(_sql):
        facts["statements"] += 1

    def observed_open(*args, **kwargs):
        store = open_store(*args, **kwargs)
        store._db.set_progress_handler(progress, 100)
        store._db.set_trace_callback(statement)
        return store

    def observed_project(store, graph_id, when, dirty=None):
        facts["projections"].append(graph_id)
        return project(store, graph_id, when, dirty)

    def observed_nodes(store, graph_id):
        rows = read_nodes(store, graph_id)
        facts['materializedNodeRows'] += len(rows)
        return rows

    with monkeypatch.context() as context:
        context.setattr(service, "_open", observed_open)
        context.setattr(service, "_project", observed_project)
        context.setattr(PacGraphStore, 'nodes', observed_nodes)
        start = thread_time_ns()
        service._tick(at)
        facts["threadCpuMs"] = (thread_time_ns() - start) / 1_000_000
    return facts
