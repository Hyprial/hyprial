import pytest
import yaml
import time
import threading
from hyprial.daemon.impl.pac.graphs.authority import PacGraphAuthority
from hyprial.daemon.impl.pac.storage.store import default_database_path
from hyprial.daemon.impl.pac.storage.store import PacGraphStore
from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService, WorkflowServiceError

from ..support import (
    AGENT,
    Sender,
    USER,
    bound_message,
    complete,
    service as service,
    start,
)


def test_workflow_failure_mutation_runs_on_graph_writer(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    sender = Sender()
    authority = PacGraphAuthority(default_database_path(state_dir), sender)
    service = GraphWorkflowService(
        state_dir=state_dir, owner="test", machine="machine", sender=sender,
        start_thread=True, delivery_receipt=authority, graph_authority=authority,
    )
    threads: list[str] = []
    original = service._fail_direct

    def observed_fail(**kwargs):
        threads.append(threading.current_thread().name)
        return original(**kwargs)

    monkeypatch.setattr(service, "_fail_direct", observed_fail)
    try:
        graph = start(service, [{"id": "work", "owner": AGENT, "task": "do it"}])
        deadline = time.monotonic() + 3.0
        while True:
            status = service.status(run_id=graph)
            request_id = status["nodes"][0]["requestId"]
            if request_id:
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        failed = service.fail(
            graph_id=graph, node_id="work", actor=AGENT,
            request_id=request_id, reason_ref="evidence:failed",
        )
        assert failed["state"] == "failed"
        assert threads and all(name != threading.current_thread().name for name in threads)
    finally:
        assert service.close()
        assert authority.close()


def test_pruned_request_is_fenced_and_failed_on_graph_writer(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    sender = Sender()
    authority = PacGraphAuthority(default_database_path(state_dir), sender)
    service = GraphWorkflowService(
        state_dir=state_dir, owner="test", machine="machine", sender=sender,
        start_thread=True, delivery_receipt=authority, graph_authority=authority,
    )
    threads: list[str] = []
    original = service._record_request_pruned_direct

    def observed_prune(**kwargs):
        threads.append(threading.current_thread().name)
        return original(**kwargs)

    monkeypatch.setattr(service, "_record_request_pruned_direct", observed_prune)
    try:
        graph = start(service, [{"id": "work", "owner": AGENT, "task": "do it"}])
        deadline = time.monotonic() + 3.0
        while not sender.messages:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        message_id = None
        while True:
            store = PacGraphStore(service.database, read_only=True)
            try:
                binding = store._db.execute(
                    "SELECT message_id FROM workflow_deliveries WHERE graph_id=?",
                    (graph,),
                ).fetchone()
                if binding is not None:
                    message_id = str(binding[0])
            finally:
                store.close()
            if message_id is not None:
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert service.record_harness_outcome(
            message_id=message_id, recipient=AGENT, failed=False
        )
        assert service.record_request_pruned(message_id=message_id, recipient=AGENT)
        assert service.status(run_id=graph)["state"] == "failed"
        assert threads and threads[0] != threading.current_thread().name
    finally:
        assert service.close()
        assert authority.close()


def test_borrowed_root_is_dispatched_once_and_completes_only_by_flag(service):
    svc, sender, clock = service
    graph = start(svc, [{"id": "work", "owner": AGENT, "task": "actual task"}])
    svc._tick()
    assert len(sender.messages) == 1
    message = next(iter(sender.messages.values()))
    assert "actual task" in message["text"]
    assert "hyprial workflow complete" in message["text"]
    assert "--request-id workflow-request:" in message["text"]
    assert "--output-text <result>" in message["text"]
    assert svc.status(run_id=graph)["state"] == "running"
    svc._tick()
    assert len(sender.messages) == 1
    complete(svc, graph, "work")
    assert svc.status(run_id=graph)["state"] == "completed"
    assert "outputText" not in svc.status(run_id=graph)["nodes"][0]
    store = PacGraphStore(svc.database)
    try:
        assert not any(n.kind == "actor" for n in store.nodes(graph))
        assert store.graph(graph)["closed_at"] == clock[0]
        assert "actual task" not in str([n.to_json() for n in store.nodes(graph)])
    finally:
        store.close()


def test_list_projects_progress_and_topological_current_node(service):
    svc, _sender, clock = service
    graph = start(
        svc,
        [
            {"id": "second", "owner": AGENT, "task": "second", "after": ["first"]},
            {"id": "first", "owner": AGENT, "task": "first"},
        ],
    )
    svc._tick()

    listed = svc.list(viewer=USER)["runs"][0]
    assert listed["graphId"] == graph
    assert listed["lastProgressAtMs"] == 1000
    assert listed["currentNode"] == {"nodeId": "first", "state": "requested"}
    assert svc.list(viewer="agent:test:machine:stranger")["runs"] == []

    clock[0] = 2000
    complete(svc, graph, "first")
    listed = svc.list(viewer=AGENT)["runs"][0]
    assert listed["lastProgressAtMs"] == 2000
    assert listed["currentNode"] == {"nodeId": "second", "state": "requested"}

    clock[0] = 3000
    complete(svc, graph, "second")
    listed = svc.list(viewer=None)["runs"][0]
    assert listed["lastProgressAtMs"] == 3000
    assert listed["currentNode"] is None


def test_routine_last_dispatches_uses_latest_graph_creation(service):
    svc, _sender, clock = service
    document = yaml.safe_dump(
        {
            "version": 2,
            "name": "routine work",
            "nodes": [{"id": "work", "owner": AGENT, "task": "work"}],
        }
    )
    svc.start(
        yaml_text=document,
        sender=USER,
        operation_key="routine-first",
        routine_name="nightly",
        task_key="first",
    )
    clock[0] = 2500
    svc.start(
        yaml_text=document,
        sender=USER,
        operation_key="routine-second",
        routine_name="nightly",
        task_key="second",
    )

    assert svc.routine_last_dispatches() == {"nightly": 2500}


def test_replay_and_restart_use_same_graph_and_message(service):
    svc, sender, _ = service
    nodes = [{"id": "work", "owner": AGENT, "task": "do it"}]
    graph = start(svc, nodes)
    svc._tick()
    other = GraphWorkflowService(
        state_dir=svc.database.parent,
        owner="test",
        machine="machine",
        sender=sender,
        clock_ms=svc.clock,
        start_thread=False,
    )
    try:
        assert start(other, nodes) == graph
        other._tick()
        assert len(sender.messages) == 1
        with pytest.raises(WorkflowServiceError, match="different request"):
            start(other, [{"id": "work", "owner": AGENT, "task": "different"}])
    finally:
        other.close()


def test_implicit_operation_key_replays_same_graph_after_service_restart(service):
    svc, sender, _ = service
    document = yaml.safe_dump(
        {
            "version": 2,
            "name": "implicit replay",
            "nodes": [{"id": "work", "owner": AGENT, "task": "do it"}],
        }
    )
    graph = svc.start(yaml_text=document, sender=USER)["graphId"]
    svc._tick()

    reopened = GraphWorkflowService(
        state_dir=svc.database.parent,
        owner="test",
        machine="machine",
        sender=sender,
        clock_ms=svc.clock,
        start_thread=False,
    )
    try:
        replay = reopened.start(yaml_text=document, sender=USER)["graphId"]
        reopened._tick()

        assert replay == graph
        assert len(sender.messages) == 1
    finally:
        reopened.close()


def test_explicit_new_operation_key_starts_a_deliberate_second_graph(service):
    svc, _sender, _ = service
    document = yaml.safe_dump(
        {
            "version": 2,
            "name": "deliberate rerun",
            "nodes": [{"id": "work", "owner": AGENT, "task": "do it"}],
        }
    )

    first = svc.start(
        yaml_text=document, sender=USER, operation_key="deliberate-run-1"
    )["graphId"]
    second = svc.start(
        yaml_text=document, sender=USER, operation_key="deliberate-run-2"
    )["graphId"]

    assert second != first


@pytest.mark.parametrize(
    "policy,expected",
    [("terminate", "failed"), ("continue", "running"), ("hold", "held")],
)
def test_failure_policy_is_applied_and_alert_survives_close(service, policy, expected):
    svc, sender, _ = service
    graph = start(
        svc,
        [
            {"id": "a", "task": "a", "owner": AGENT},
            {"id": "b", "task": "b", "owner": AGENT},
            {"id": "c", "task": "c", "owner": AGENT, "after": ["a"]},
        ],
        policy=policy,
    )
    svc._tick()
    request = next(n for n in svc.status(run_id=graph)["nodes"] if n["nodeId"] == "a")[
        "requestId"
    ]
    svc.fail(
        graph_id=graph,
        node_id="a",
        actor=AGENT,
        request_id=request,
        reason_ref="failure:test",
    )
    assert svc.status(run_id=graph)["state"] == expected
    assert any(
        "failed" in m["text"] and m["recipient"] == USER
        for m in sender.messages.values()
    )
    if policy == "continue":
        complete(svc, graph, "b")
        assert svc.status(run_id=graph)["state"] == "failed"
        assert (
            next(n for n in svc.status(run_id=graph)["nodes"] if n["nodeId"] == "c")[
                "state"
            ]
            == "blocked"
        )


def test_delivery_binding_exists_before_transport_and_is_revoked_on_close(service):
    from hyprial.daemon.impl.pac.contracts.delivery import delivery_current

    svc, _, clock = service
    graph = start(svc, [{"id": "work", "task": "work", "owner": AGENT}])
    svc._tick()
    message = bound_message(svc)
    assert delivery_current(svc.database.parent, message, now_ms=clock[0])
    assert delivery_current(
        svc.database.parent, "workflow-ordinary-message", now_ms=clock[0]
    )
    svc.cancel(run_id=graph, actor=USER)
    assert not delivery_current(svc.database.parent, message, now_ms=clock[0])
    assert delivery_current(
        svc.database.parent, "workflow-ordinary-message", now_ms=clock[0]
    )


@pytest.mark.parametrize("key", ["", " ", [], False, 0, "bad\nkey"])
def test_invalid_operation_identity_cannot_allocate_a_graph(service, key):
    svc, sender, _ = service
    with pytest.raises(WorkflowServiceError) as error:
        start(svc, [{"id": "work", "task": "work", "owner": AGENT}], key=key)
    assert error.value.code == "INVALID_ARGUMENT"
    assert svc.list()["runs"] == []
    assert sender.messages == {}
    assert not (svc.database.parent / "workflow-artifacts").exists()
