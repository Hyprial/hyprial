from __future__ import annotations

import sqlite3
from tests.support.pac.migrations import strip_tick_v19
from pathlib import Path
from typing import Any

import yaml

from hyprial.kernel import ipc_errors
from hyprial.daemon.impl.inbox.links.io import InboxIoDeferred, InboxIoError
from hyprial.daemon.impl.pac.graphs.edits import activate_graph, add_edge, add_node, create_graph
from hyprial.daemon.impl.pac.graphs.reactor import PacReactor
from hyprial.daemon.impl.pac.storage.store import PacGraphStore
from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService


CREATOR = "user:creator"
WORKER = "agent:creator:machine:worker"


class _SelectiveSender:
    def __init__(self, user_failures: list[Exception] | None = None) -> None:
        self.user_failures = list(user_failures or [])
        self.attempts: list[dict[str, Any]] = []

    def send(self, **kwargs: Any) -> str:
        self.attempts.append(kwargs)
        if kwargs["recipient"].startswith("user:") and self.user_failures:
            raise self.user_failures.pop(0)
        return kwargs["idempotency_key"]

    @property
    def user_attempts(self) -> list[dict[str, Any]]:
        return [
            attempt
            for attempt in self.attempts
            if attempt["recipient"].startswith("user:")
        ]


def _service(
    state_dir: Path,
    sender: _SelectiveSender,
    *,
    logs: list[tuple[str, str, str, dict[str, Any]]] | None = None,
) -> GraphWorkflowService:
    def logger(level: str, component: str, event: str, **fields: Any) -> None:
        if logs is not None:
            logs.append((level, component, event, fields))

    return GraphWorkflowService(
        state_dir=state_dir,
        owner="creator",
        machine="machine",
        sender=sender,
        logger=logger,
        start_thread=False,
    )


def _start(
    service: GraphWorkflowService,
    *,
    owner: str,
    operation_key: str,
) -> str:
    return service.start(
        yaml_text=yaml.safe_dump(
            {
                "version": 2,
                "name": operation_key,
                "on_failure": "terminate",
                "nodes": [{"id": "work", "owner": owner, "task": "do work"}],
            }
        ),
        sender=CREATOR,
        operation_key=operation_key,
    )["graphId"]


def _fail_requested_work(service: GraphWorkflowService, graph_id: str) -> None:
    service._tick()
    node = service.status(run_id=graph_id)["nodes"][0]
    service.fail(
        graph_id=graph_id,
        node_id="work",
        actor=WORKER,
        request_id=node["requestId"],
        reason_ref="evidence:failed",
    )


def test_permanent_actor_alert_failure_is_terminal_and_not_reselected(
    tmp_path: Path,
) -> None:
    failure = InboxIoError(
        "the live proxy rejected the notification sender",
        permanent=True,
        code=ipc_errors.SENDER_UNRESOLVED,
    )
    sender = _SelectiveSender([failure])
    logs: list[tuple[str, str, str, dict[str, Any]]] = []
    service = _service(tmp_path / "state", sender, logs=logs)
    try:
        graph_id = _start(service, owner=WORKER, operation_key="permanent-alert")
        _fail_requested_work(service, graph_id)
        projected: list[str] = []
        original_project = service._project

        def project(store: PacGraphStore, selected_graph: str, at: int) -> None:
            projected.append(selected_graph)
            original_project(store, selected_graph, at)

        service._project = project  # type: ignore[method-assign]
        service._tick()
        service._tick()
        service._tick()

        assert len(sender.user_attempts) == 1
        assert projected == []
        store = service._open()
        try:
            service._drain(store, graph_id)
        finally:
            store.close()
        assert len(sender.user_attempts) == 1
        connection = sqlite3.connect(service.database)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT failed_at,failure_code,failure_detail FROM notifications "
                "WHERE kind='actor_alert'"
            ).fetchone()
        finally:
            connection.close()
        assert row is not None
        assert row["failed_at"] is not None
        assert row["failure_code"] == ipc_errors.SENDER_UNRESOLVED
        assert "rejected" in row["failure_detail"]
        failures = [entry for entry in logs if entry[2] == "workflow.delivery_failed"]
        assert len(failures) == 1
        assert failures[0][3]["graphId"] == graph_id
        assert failures[0][3]["code"] == ipc_errors.SENDER_UNRESOLVED
    finally:
        assert service.close()


def test_transient_user_delivery_is_retried_on_the_next_cadence(
    tmp_path: Path,
) -> None:
    sender = _SelectiveSender([InboxIoDeferred("receipt is not settled")])
    service = _service(tmp_path / "state", sender)
    try:
        graph_id = _start(
            service, owner="user:reviewer", operation_key="transient-delivery"
        )
        service._tick()
        assert len(sender.user_attempts) == 1
        assert service.status(run_id=graph_id)["nodes"][0]["deliveryState"] == "planned"

        service._tick()
        assert len(sender.user_attempts) == 2
        assert service.status(run_id=graph_id)["nodes"][0]["deliveryState"] == "accepted"
    finally:
        assert service.close()


def test_workflow_inspect_projects_permanent_delivery_failure_code(
    tmp_path: Path,
) -> None:
    sender = _SelectiveSender(
        [
            InboxIoError(
                "sender is not registered",
                permanent=True,
                code=ipc_errors.SENDER_UNRESOLVED,
            )
        ]
    )
    service = _service(tmp_path / "state", sender)
    try:
        graph_id = _start(
            service, owner="user:reviewer", operation_key="failed-projection"
        )
        service._tick()
        service._tick()

        node = service.status(run_id=graph_id)["nodes"][0]
        assert len(sender.user_attempts) == 1
        assert node["deliveryState"] == "failed"
        assert node["deliveryFailureCode"] == ipc_errors.SENDER_UNRESOLVED
        assert node["messageId"] is None
    finally:
        assert service.close()


def test_direct_reactor_terminalizes_permanent_failure_and_resend_skips_it(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    store = PacGraphStore(state_dir / "pac-graph.sqlite3")
    sender = _SelectiveSender(
        [
            InboxIoError(
                "receiver rejected sender",
                permanent=True,
                code=ipc_errors.SENDER_UNRESOLVED,
            )
        ]
    )
    logs: list[tuple[str, str, str, dict[str, Any]]] = []

    def logger(level: str, component: str, event: str, **fields: Any) -> None:
        logs.append((level, component, event, fields))

    try:
        graph_id = create_graph(
            store, name="reactor-permanent", created_by=CREATOR
        )["graphId"]
        version = add_node(
            store,
            state_dir,
            graph_id=graph_id,
            node_id="source",
            owner=CREATOR,
            brief_ref="ref#source",
            expect_version=1,
        )["version"]
        version = add_node(
            store,
            state_dir,
            graph_id=graph_id,
            node_id="target",
            owner="user:reviewer",
            brief_ref="ref#target",
            expect_version=version,
        )["version"]
        add_edge(
            store,
            graph_id=graph_id,
            from_node="source",
            to_node="target",
            kind="forward",
            expect_version=version,
        )
        activate_graph(store, graph_id, actor=CREATOR)
        reactor = PacReactor(store, sender=sender, logger=logger)

        outcome = reactor.set_flag(graph_id, "source", actor=CREATOR)
        assert len(outcome.undelivered) == 1
        assert len(sender.user_attempts) == 1
        row = store.notifications(graph_id)[0]
        assert row.failed_at is not None
        assert row.failure_code == ipc_errors.SENDER_UNRESOLVED
        assert reactor.resend_undelivered(graph_id) == {
            "delivered": 0,
            "undelivered": 0,
            "error": None,
        }
        assert len(sender.user_attempts) == 1
        assert len([entry for entry in logs if entry[2] == "workflow.delivery_failed"]) == 1
    finally:
        store.close()


def _downgrade_notifications_to_v16(database: Path) -> None:
    connection = sqlite3.connect(database)
    try:
        strip_tick_v19(connection)
        for trigger in (
            "workflow_graph_expansion_insert_guard",
            "workflow_graph_expansion_identity_immutable",
            "workflow_node_kind_insert_guard",
            "workflow_node_kind_immutable",
            "workflow_expansion_identity_immutable",
            "workflow_expansion_binding_guard",
            "workflow_worktree_plan_immutable",
            "workflow_outcome_expansion_insert_guard",
            "workflow_outcome_expansion_identity_immutable",
            "remote_workflow_expansion_insert_guard",
            "remote_workflow_expansion_update_guard",
        ):
            connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        connection.execute("DROP INDEX IF EXISTS workflow_graphs_parent")
        connection.execute("DROP INDEX IF EXISTS workflow_graphs_one_child")
        connection.execute("DROP TABLE IF EXISTS workflow_worktrees")
        connection.execute("DROP TABLE IF EXISTS workflow_expansions")
        connection.execute("ALTER TABLE workflow_nodes DROP COLUMN node_kind")
        for table, columns in (
            ("workflow_outcome_receipts", ("expansion_digest", "node_id", "graph_id")),
            ("remote_workflow_outbox", ("attempt_no", "expansion_digest", "expansion_text")),
            ("workflow_graphs", ("expansion_deadline_ms", "parent_node_id", "parent_graph_id")),
        ):
            for column in columns:
                connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        connection.execute("ALTER TABLE notifications RENAME TO notifications_next")
        connection.execute(
            """
            CREATE TABLE notifications (
                event_id TEXT NOT NULL,
                edge TEXT NOT NULL,
                kind TEXT NOT NULL CHECK (
                    kind IN ('turn','withdraw','overdue','launch_failed','actor_alert')
                ),
                recipient TEXT NOT NULL,
                round_no INTEGER,
                text TEXT NOT NULL,
                sender TEXT NOT NULL,
                message_id TEXT,
                at INTEGER NOT NULL,
                delivered_at INTEGER,
                plan_json TEXT,
                PRIMARY KEY(event_id, edge)
            )
            """
        )
        connection.execute(
            "INSERT INTO notifications "
            "(event_id,edge,kind,recipient,round_no,text,sender,message_id,at,delivered_at,plan_json) "
            "SELECT event_id,edge,kind,recipient,round_no,text,sender,message_id,at,delivered_at,plan_json "
            "FROM notifications_next"
        )
        connection.execute("DROP TABLE notifications_next")
        connection.execute(
            "CREATE INDEX notifications_recipient ON notifications(recipient, at)"
        )
        connection.execute(
            "CREATE INDEX notifications_graph_pending "
            "ON notifications(json_extract(plan_json,'$.graphId'),message_id)"
        )
        connection.execute("PRAGMA user_version = 16")
        connection.commit()
    finally:
        connection.close()


def test_v16_pending_alert_migrates_and_remains_eligible(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    first_sender = _SelectiveSender([InboxIoDeferred("receiver is restarting")])
    first = _service(state_dir, first_sender)
    graph_id = _start(first, owner=WORKER, operation_key="migration-pending")
    _fail_requested_work(first, graph_id)
    assert len(first_sender.user_attempts) == 1
    assert first.close()

    _downgrade_notifications_to_v16(first.database)
    second_sender = _SelectiveSender()
    second = _service(state_dir, second_sender)
    try:
        store = second._open(read_only=True)
        try:
            assert store._db.execute("PRAGMA user_version").fetchone()[0] == 19
        finally:
            store.close()
        second._tick()
        assert len(second_sender.user_attempts) == 1
        connection = sqlite3.connect(second.database)
        try:
            row = connection.execute(
                "SELECT message_id,failed_at,failure_code FROM notifications "
                "WHERE kind='actor_alert'"
            ).fetchone()
        finally:
            connection.close()
        assert row is not None
        assert row[0] is not None
        assert row[1:] == (None, None)
    finally:
        assert second.close()


def test_actorized_drain_routes_permanent_failure_through_the_graph_writer(
    tmp_path: Path,
) -> None:
    """Production wiring: application.py passes the graph authority as the
    workflow runtime's delivery_receipt, and from then on every writable open
    belongs to that authority.  A permanent failure must be terminalized once,
    THROUGH the authority's writer (its logger reports it), never by the
    runtime's own direct reactor write, and never be re-sent.
    """

    from hyprial.daemon.impl.pac.graphs.authority import PacGraphAuthority
    from hyprial.daemon.impl.pac.storage.store import default_database_path

    failure = InboxIoError(
        "the live proxy rejected the notification sender",
        permanent=True,
        code=ipc_errors.SENDER_UNRESOLVED,
    )
    sender = _SelectiveSender([failure])
    service_logs: list[tuple[str, str, str, dict[str, Any]]] = []
    writer_logs: list[tuple[str, str, str, dict[str, Any]]] = []

    def service_logger(level: str, component: str, event: str, **fields: Any) -> None:
        service_logs.append((level, component, event, fields))

    def writer_logger(level: str, component: str, event: str, **fields: Any) -> None:
        writer_logs.append((level, component, event, fields))

    state_dir = tmp_path / "state"
    authority = PacGraphAuthority(
        default_database_path(state_dir), sender, logger=writer_logger
    )
    service = GraphWorkflowService(
        state_dir=state_dir,
        owner="creator",
        machine="machine",
        sender=sender,
        logger=service_logger,
        start_thread=False,
        delivery_receipt=authority,
    )
    try:
        graph_id = _start(service, owner=WORKER, operation_key="actorized-alert")
        _fail_requested_work(service, graph_id)
        for _ in range(3):
            store = service._open(read_only=True)
            try:
                service._drain(store, graph_id)
            finally:
                store.close()
        assert len(sender.user_attempts) == 1
        connection = sqlite3.connect(service.database)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT failed_at,failure_code FROM notifications "
                "WHERE kind='actor_alert'"
            ).fetchone()
        finally:
            connection.close()
        assert row is not None
        assert row["failed_at"] is not None
        assert row["failure_code"] == ipc_errors.SENDER_UNRESOLVED
        written = [e for e in writer_logs if e[2] == "workflow.delivery_failed"]
        assert len(written) == 1
        assert written[0][3]["graphId"] == graph_id
        # Not written behind the authority's back by the runtime's reactor.
        assert not [e for e in service_logs if e[2] == "workflow.delivery_failed"]
    finally:
        assert service.close()
        assert authority.close()


def test_tick_selection_keeps_undelivered_alerts_without_a_correlated_scan(
    tmp_path: Path,
) -> None:
    """The tick's graph selection: open graphs, unfinished workflows, and closed
    graphs still holding an undelivered actor alert.  It must evaluate the
    alert list once per tick, not re-scan every notification per graph (a
    correlated subquery): that cost ~0.65 s CPU per tick in production 0.5.0
    (hq notes/diag-050-daemon-cpu-2026-10-05.md)."""

    from hyprial.daemon.impl.pac.workflows.runtime.operations import TICK_GRAPH_SELECT

    sender = _SelectiveSender(
        [InboxIoDeferred("receipt is not settled")] * 50
    )
    service = _service(tmp_path / "state", sender)
    try:
        # Closed by on_failure=terminate, its user alert still undelivered.
        pending = _start(service, owner=WORKER, operation_key="alert-pending")
        _fail_requested_work(service, pending)
        service._tick()
        # Still open: no completion or failure.
        open_graph = _start(service, owner=WORKER, operation_key="still-open")
        service._tick()
        store = service._open()
        try:
            selected = [row[0] for row in store._db.execute(TICK_GRAPH_SELECT)]
            plan = [
                row[-1]
                for row in store._db.execute("EXPLAIN QUERY PLAN " + TICK_GRAPH_SELECT)
            ]
            closed = store.graph(pending)["closed_at"]
        finally:
            store.close()
        assert closed is not None
        assert pending in selected
        assert open_graph in selected
        assert not [step for step in plan if "CORRELATED" in step], plan
    finally:
        assert service.close()
