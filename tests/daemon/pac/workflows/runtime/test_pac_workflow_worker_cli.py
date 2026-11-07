"""R3 worker-control contract for managed workflow graphs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from hyprial.daemon.impl.pac.graphs.reactor import PacReactor
from hyprial.daemon.impl.pac.storage.store import PacGraphStore
from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService, WorkflowServiceError
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


OWNER = "user:test"
BORROWED = "agent:other:node:borrowed"


class Sender:
    def __init__(self) -> None:
        self.messages: dict[str, dict[str, Any]] = {}

    def send(self, **kwargs: Any) -> str:
        key = kwargs["idempotency_key"]
        self.messages.setdefault(key, kwargs)
        return key


@pytest.fixture
def service(tmp_path: Path):
    sender = Sender()
    svc = GraphWorkflowService(
        state_dir=tmp_path / "state",
        owner="test",
        machine="node",
        sender=sender,
        start_thread=False,
    )
    yield svc, sender
    assert svc.close()


def _start(svc: GraphWorkflowService, policy: str = "terminate") -> str:
    raw = yaml.safe_dump(
        {
            "version": 2,
            "name": "worker-control",
            "on_failure": policy,
            "workers": {
                "owned": {"tier": "fast"},
                "other": {"tier": "fast"},
            },
            "nodes": [
                {"id": "first", "task": "first", "worker": "owned"},
                {"id": "second", "task": "second", "worker": "owned"},
                {"id": "other", "task": "other", "worker": "other"},
            ],
        }
    )
    return svc.start(yaml_text=raw, sender=OWNER, operation_key=f"worker-{policy}")["graphId"]


@pytest.mark.parametrize(
    ("policy", "expected_graph_state"),
    [("terminate", "failed"), ("continue", "running"), ("hold", "held")],
)
def test_worker_stop_fails_every_owned_unfinished_node_and_applies_policy(
    service, policy: str, expected_graph_state: str
) -> None:
    svc, _sender = service
    graph = _start(svc, policy)
    svc._tick()
    actor = next(
        node["actorNode"]
        for node in svc.status(run_id=graph)["nodes"]
        if node["nodeId"] == "first"
    )

    result = svc.stop_worker(graph_id=graph, actor_name=actor, actor=OWNER)

    assert result["state"] == expected_graph_state
    status = svc.status(run_id=graph)
    assert {
        node["state"] for node in status["nodes"] if node["nodeId"] in {"first", "second"}
    } == {"failed"}
    assert all(
        node["reasonRef"] == "worker stopped by user:test"
        for node in status["nodes"]
        if node["nodeId"] in {"first", "second"}
    )


@pytest.mark.parametrize("policy", ["terminate", "hold"])
def test_worker_stop_on_closed_graph_releases_only_without_applying_policy(
    service, policy: str
) -> None:
    svc, _sender = service
    graph = _start(svc, policy)
    svc._tick()
    actor = next(
        node["actorNode"]
        for node in svc.status(run_id=graph)["nodes"]
        if node["nodeId"] == "first"
    )
    assert svc.cancel(run_id=graph, actor=OWNER)["state"] == "cancelled"
    before = svc.status(run_id=graph)

    result = svc.stop_worker(graph_id=graph, actor_name=actor, actor=OWNER)

    assert result == before
    assert svc.status(run_id=graph) == before


def test_worker_restart_keeps_work_and_redelivers_in_progress_once(service) -> None:
    svc, sender = service
    raw = yaml.safe_dump(
        {
            "version": 2,
            "name": "restart-control",
            "workers": {"owned": {"tier": "fast"}},
            "nodes": [{"id": "first", "task": "first", "worker": "owned"}],
        }
    )
    graph = svc.start(yaml_text=raw, sender=OWNER, operation_key="worker-restart")["graphId"]
    store = PacGraphStore(svc.database)
    try:
        actor_node = next(
            node["actorNode"] for node in svc.status(run_id=graph)["nodes"] if node["nodeId"] == "first"
        )
        store._db.execute(
            "UPDATE nodes SET flag=1 WHERE graph_id=? AND node_id=?",
            (graph, actor_node),
        )
        store._db.commit()
    finally:
        store.close()
    svc._tick()
    first = svc.status(run_id=graph)["nodes"]
    actor = next(node["actorNode"] for node in first if node["nodeId"] == "first")
    initial_messages = len(sender.messages)

    svc.restart_worker(graph_id=graph, actor_name=actor, actor=OWNER)
    svc._tick()

    assert len(sender.messages) == initial_messages + 1
    status = svc.status(run_id=graph)
    first_node = next(node for node in status["nodes"] if node["nodeId"] == "first")
    assert first_node["state"] == "requested"
    store = PacGraphStore(svc.database)
    try:
        PacReactor(store, clock=svc.clock).set_flag(
            graph,
            "first",
            actor=first_node["owner"],
            expected_request=first_node["requestId"],
            reason_ref="restart-result",
        )
    finally:
        store.close()
    svc._tick()
    assert svc.status(run_id=graph)["state"] == "completed"


def test_worker_stop_and_restart_refuse_borrowed_actor(service) -> None:
    svc, _sender = service
    raw = yaml.safe_dump(
        {
            "version": 2,
            "name": "borrowed-control",
            "nodes": [{"id": "borrowed", "task": "borrowed", "owner": BORROWED}],
        }
    )
    graph = svc.start(yaml_text=raw, sender=OWNER, operation_key="borrowed")["graphId"]
    for operation in (svc.stop_worker, svc.restart_worker):
        with pytest.raises(WorkflowServiceError, match="borrowed|remote") as raised:
            operation(graph_id=graph, actor_name=BORROWED, actor=OWNER)
        assert raised.value.code == "WORKFLOW_BORROWED_ACTOR"


def test_worker_stop_by_non_owner_is_refused(service) -> None:
    svc, _sender = service
    graph = _start(svc)
    actor = next(
        node["actorNode"]
        for node in svc.status(run_id=graph)["nodes"]
        if node["nodeId"] == "first"
    )
    with pytest.raises(WorkflowServiceError) as raised:
        svc.stop_worker(graph_id=graph, actor_name=actor, actor="user:intruder")
    assert raised.value.code == "WORKFLOW_NOT_OWNER"


def test_worker_commands_are_exposed_under_workflow_and_preserve_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def request(method: str, params: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        calls.append((method, params))
        return {"ok": True}

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    runner = CliRunner()
    for command in (
        ["workflow", "worker", "stop", "graph", "actor", "--json"],
        ["workflow", "worker", "restart", "graph", "actor", "--json"],
    ):
        result = runner.invoke(_cli_root_app, command)
        assert result.exit_code == 0, result.output
    assert [method for method, _params in calls] == [
        "workflow.worker.stop",
        "workflow.worker.restart",
    ]
    assert all(params["actor"] == "user:hyprial-unit-owner" for _method, params in calls)
