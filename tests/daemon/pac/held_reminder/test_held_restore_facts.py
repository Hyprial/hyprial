"""#1803 item 5: a restart must not pre-spawn workers of an idle held graph.

The restore path reads PAC's committed facts (``pac_restore_facts``).  A held
graph with no current requested work reports ``held_idle``; the restore policy
then leaves that harness dormant and the coordinator starts it when a request
actually arrives.

The *transitions* (hold on a real failure, request by the coordinator) are
covered by the workflow and actor suites; these are read-model tests, so they
set the two rows the query reads.
"""

from __future__ import annotations

from hyprial.daemon.impl.pac.storage.store import PacGraphStore

from .support import Sender, Clock, service as service_fixture, start_graph


def facts(workflow) -> dict:
    store = PacGraphStore(workflow.database, read_only=True)
    try:
        return store.pac_restore_facts()
    finally:
        store.close()


def actor_for(workflow, graph: str, node_id: str) -> str:
    """The minted actor identity of one worker node's actor node."""

    store = PacGraphStore(workflow.database, read_only=True)
    try:
        row = store._db.execute(
            "SELECT n.actor_name FROM nodes n JOIN workflow_nodes w "
            "ON w.graph_id=n.graph_id AND w.actor_node=n.node_id "
            "WHERE w.graph_id=? AND w.node_id=?",
            (graph, node_id),
        ).fetchone()
        assert row is not None and row[0] is not None
        return str(row[0])
    finally:
        store.close()


def mutate(workflow, statements, params_list) -> None:
    store = PacGraphStore(workflow.database)
    try:
        with store.write():
            for statement, params in zip(statements, params_list, strict=True):
                store._db.execute(statement, params)
            store._db.commit()
    finally:
        store.close()


def hold(workflow, graph: str) -> None:
    mutate(
        workflow,
        ["UPDATE workflow_graphs SET state='held',held_at_ms=1000 WHERE graph_id=?"],
        [(graph,)],
    )


def test_held_graph_without_requested_work_reports_held_idle(tmp_path):
    clock = Clock()
    workflow = service_fixture(tmp_path, Sender(), clock)
    try:
        graph = start_graph(workflow, [{"id": "worker", "task": "do it"}], key="held-idle")
        workflow._tick()
        assert workflow.status(run_id=graph)["state"] == "running"
        hold(workflow, graph)

        worker = facts(workflow)[actor_for(workflow, graph, "worker")]
        assert worker.terminal is False
        assert worker.pending_work is False
        assert worker.held_idle is True
    finally:
        assert workflow.close()


def test_held_graph_with_an_active_request_keeps_pending_work(tmp_path):
    clock = Clock()
    workflow = service_fixture(tmp_path, Sender(), clock)
    try:
        graph = start_graph(
            workflow,
            [{"id": "worker", "task": "do it"}, {"id": "reviewer", "task": "check it"}],
            key="held-with-work",
        )
        workflow._tick()
        hold(workflow, graph)
        mutate(
            workflow,
            ["UPDATE workflow_nodes SET state='requested' "
             "WHERE graph_id=? AND node_id='reviewer'"],
            [(graph,)],
        )

        snapshot = facts(workflow)
        requested = snapshot[actor_for(workflow, graph, "reviewer")]
        assert requested.pending_work is True
        assert requested.held_idle is False
        # The other worker has no request of its own: it stays idle-held.
        assert snapshot[actor_for(workflow, graph, "worker")].held_idle is True
    finally:
        assert workflow.close()


def test_running_graph_is_never_held_idle(tmp_path):
    clock = Clock()
    workflow = service_fixture(tmp_path, Sender(), clock)
    try:
        graph = start_graph(workflow, [{"id": "worker", "task": "do it"}], key="running")
        workflow._tick()
        assert workflow.status(run_id=graph)["state"] == "running"
        assert (
            facts(workflow)[actor_for(workflow, graph, "worker")].held_idle is False
        )
    finally:
        assert workflow.close()
