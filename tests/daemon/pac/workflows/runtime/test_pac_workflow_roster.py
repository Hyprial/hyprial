"""Phase 1 workflow-worker receipts and the canonical workflow roster."""

from __future__ import annotations

import json
import sqlite3
from tests.support.pac.migrations import strip_tick_v19
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.dispatch.workflow_observation import observe_node
from hyprial.daemon.impl.pac.actors.coordinator import ActorCoordinator, FileLaunchResolver, RuntimeObservation
from hyprial.daemon.impl.pac.graphs.reactor import PacReactor
from hyprial.daemon.impl.pac.storage.store import PacGraphStore
from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService


USER = "user:test"
BORROWED = "agent:test:machine:wf-adversary"


class Sender:
    def __init__(self) -> None:
        self.messages: dict[str, dict] = {}

    def send(self, **kwargs):
        self.messages.setdefault(kwargs["idempotency_key"], kwargs)
        return kwargs["idempotency_key"]


class Runtime:
    def __init__(self) -> None:
        self.running: dict[str, RuntimeObservation] = {}
        self.starts: list[str] = []

    def observe(self, actor_name: str) -> RuntimeObservation:
        return self.running.get(actor_name, RuntimeObservation(False))

    def start(self, actor_name, launch, *, operation_id, identity_marker):
        self.starts.append(actor_name)
        observation = RuntimeObservation(
            True,
            identity_marker,
            launch.harness,
            operation_id,
            agent_entity_token=f"entity-{actor_name}",
        )
        self.running[actor_name] = observation
        return observation

    def stop(self, actor_name, launch, *, operation_id, identity_marker):
        self.running.pop(actor_name, None)
        return RuntimeObservation(False)


def _service(tmp_path):
    sender = Sender()
    service = GraphWorkflowService(
        state_dir=tmp_path / "state",
        owner="test",
        machine="machine",
        sender=sender,
        clock_ms=lambda: 1000,
        start_thread=False,
    )
    return service, sender


def _complete(service: GraphWorkflowService, graph_id: str, node_id: str) -> None:
    projected = next(
        node for node in service.status(run_id=graph_id)["nodes"]
        if node["nodeId"] == node_id
    )
    store = PacGraphStore(service.database)
    try:
        PacReactor(store, clock=service.clock).set_flag(
            graph_id,
            node_id,
            actor=projected["owner"],
            expected_request=projected["requestId"],
            reason_ref=f"result:{node_id}",
        )
    finally:
        store.close()
    service._tick()


def _roster_line(message: dict) -> str:
    return next(
        line.removeprefix("Workflow roster: ")
        for line in message["text"].splitlines()
        if line.startswith("Workflow roster: ")
    )


def test_compile_workflow_receipts_and_workflow_sender_share_one_roster(tmp_path):
    service, sender = _service(tmp_path)
    try:
        graph_id = service.start(
            yaml_text="""
version: 2
name: roster
nodes:
  - id: a
    task: first
  - id: b
    task: second
    after: [a]
  - id: borrowed
    owner: agent:test:machine:wf-adversary
    task: borrowed
    after: [b]
""",
            sender=USER,
            operation_key="roster",
        )["graphId"]

        status = service.status(run_id=graph_id)
        roster = status["roster"]
        assert roster["graphId"] == graph_id
        assert roster["digest"] == status["rosterDigest"]
        assert [node["nodeId"] for node in roster["nodes"]] == ["a", "b", "borrowed"]
        assert roster["nodes"][0] == {
            "nodeId": "a",
            "role": "execute",
            "owner": next(node for node in status["nodes"] if node["nodeId"] == "a")["owner"],
            "actorNode": "_actor.a",
            "actorUri": next(node for node in status["nodes"] if node["nodeId"] == "a")["owner"],
            "ownership": "workflow",
            "location": "local",
        }
        borrowed = roster["nodes"][2]
        assert borrowed == {
            "nodeId": "borrowed",
            "role": "execute",
            "owner": BORROWED,
            "ownership": "borrowed",
            "location": "local",
        }
        inspected = observe_node(
            service.database,
            status,
            next(node for node in status["nodes"] if node["nodeId"] == "a"),
            SimpleNamespace(list_progress_events=lambda *args, **kwargs: ()),
            recipient="agent:test:machine:observer",
            at=1000,
            epoch="test",
        )
        assert inspected["roster"] == roster
        assert inspected["rosterDigest"] == roster["digest"]

        store = PacGraphStore(service.database)
        runtime = Runtime()
        coordinator = ActorCoordinator(
            store,
            runtime,
            daemon_epoch="test",
            resolver=FileLaunchResolver(tmp_path),
            clock=service.clock,
        )
        try:
            receipts = store.workflow_worker_receipts(graph_id)
            assert [receipt["actor_node"] for receipt in receipts] == [
                "_actor.a",
                "_actor.b",
            ]
            assert not any(receipt["actor_name"] == "wf-adversary" for receipt in receipts)

            coordinator.reconcile_all()
            service._tick()
            first = next(iter(sender.messages.values()))
            first_roster = _roster_line(first)
            assert json.loads(first_roster) == roster

            receipt = store.workflow_worker_receipts(graph_id)[0]
            assert receipt["agent_entity_token"] == f"entity-{receipt['actor_name']}"

            _complete(service, graph_id, "a")
            coordinator.reconcile_all()
            service._tick()
            second = next(
                message for message in sender.messages.values()
                if "second" in message["text"]
            )
            assert _roster_line(second) == first_roster
        finally:
            store.close()
    finally:
        assert service.close()


def test_receipt_launch_digest_mismatch_fails_loud_before_start(tmp_path):
    service, _ = _service(tmp_path)
    try:
        graph_id = service.start(
            yaml_text="""
version: 2
name: mismatch
nodes:
  - id: work
    task: work
""",
            sender=USER,
            operation_key="mismatch",
        )["graphId"]
        store = PacGraphStore(service.database)
        runtime = Runtime()
        try:
            db = store.write()
            with pytest.raises(sqlite3.IntegrityError, match="identity is immutable"):
                db.execute(
                    "UPDATE workflow_worker_receipts SET launch_digest=? WHERE graph_id=?",
                    ("0" * 64, graph_id),
                )
            db.rollback()
            with store.write() as db:
                db.execute("DROP TRIGGER workflow_worker_receipt_identity_immutable")
                db.execute(
                    "UPDATE workflow_worker_receipts SET launch_digest=? WHERE graph_id=?",
                    ("0" * 64, graph_id),
                )
            ActorCoordinator(
                store,
                runtime,
                daemon_epoch="test",
                resolver=FileLaunchResolver(tmp_path),
                clock=service.clock,
            ).reconcile_all()
            assert runtime.starts == []
            failure = next(
                event for event in store.journal_events(graph_id)
                if event["type"] == "launch_failed"
            )
            assert "receipt launch digest mismatch" in failure["data"]["error"]
        finally:
            store.close()
    finally:
        assert service.close()


def test_pac_store_v14_migration_preserves_graph_and_backfills_derivable_receipts(
    tmp_path,
):
    service, _ = _service(tmp_path)
    graph_id = service.start(
        yaml_text="""
version: 2
name: migration
nodes:
  - id: owned
    task: owned
  - id: borrowed
    owner: agent:test:machine:wf-migration-adversary
    task: borrowed
""",
        sender=USER,
        operation_key="migration",
    )["graphId"]
    before = service.status(run_id=graph_id)["nodes"]
    database = service.database
    assert service.close()

    db = sqlite3.connect(database)
    strip_tick_v19(db)
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
        db.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    db.execute("DROP INDEX IF EXISTS workflow_graphs_parent")
    db.execute("DROP INDEX IF EXISTS workflow_graphs_one_child")
    db.execute("DROP TABLE IF EXISTS workflow_worktrees")
    db.execute("DROP TABLE IF EXISTS workflow_expansions")
    db.execute("ALTER TABLE workflow_nodes DROP COLUMN node_kind")
    for table, columns in (
        ("workflow_outcome_receipts", ("expansion_digest", "node_id", "graph_id")),
        ("remote_workflow_outbox", ("attempt_no", "expansion_digest", "expansion_text")),
        ("workflow_graphs", ("expansion_deadline_ms", "parent_node_id", "parent_graph_id")),
    ):
        for column in columns:
            db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    columns = {
        row[1] for row in db.execute("PRAGMA table_info(workflow_graphs)")
    }
    if "roster_json" in columns:
        db.execute("ALTER TABLE workflow_graphs DROP COLUMN roster_json")
    if "roster_digest" in columns:
        db.execute("ALTER TABLE workflow_graphs DROP COLUMN roster_digest")
    db.execute("DROP TABLE IF EXISTS workflow_worker_cleanup_intents")
    db.execute("DROP TABLE IF EXISTS workflow_worker_receipts")
    db.execute("DROP INDEX notifications_graph_retryable")
    for column in ("failure_detail", "failure_code", "failed_at"):
        db.execute(f"ALTER TABLE notifications DROP COLUMN {column}")
    db.execute("PRAGMA user_version = 14")
    db.commit()
    db.close()

    migrated = PacGraphStore(database)
    try:
        assert migrated._db.execute("PRAGMA user_version").fetchone()[0] == 19
        assert [node.to_json() for node in migrated.nodes(graph_id)]
        receipts = migrated.workflow_worker_receipts(graph_id)
        assert len(receipts) == 1
        assert receipts[0]["actor_node"] == "_actor.owned"
        assert receipts[0]["agent_entity_token"] is None
    finally:
        migrated.close()

    reopened = GraphWorkflowService(
        state_dir=database.parent,
        owner="test",
        machine="machine",
        sender=Sender(),
        clock_ms=lambda: 1000,
        start_thread=False,
    )
    try:
        after = reopened.status(run_id=graph_id)
        assert after["nodes"] == before
        assert after["roster"]["graphId"] == graph_id
    finally:
        assert reopened.close()


def test_terminal_close_persists_worker_cleanup_intent_and_projects_pending(tmp_path):
    service, _ = _service(tmp_path)
    try:
        graph_id = service.start(
            yaml_text="""
version: 2
name: terminal-cleanup
nodes:
  - id: work
    task: work
""",
            sender=USER,
            operation_key="terminal-cleanup",
        )["graphId"]
        store = PacGraphStore(service.database)
        runtime = Runtime()
        try:
            ActorCoordinator(
                store,
                runtime,
                daemon_epoch="test",
                resolver=FileLaunchResolver(tmp_path),
                clock=service.clock,
            ).reconcile_all()
            status = service.cancel(run_id=graph_id, actor=USER)

            assert status["workerCleanup"] == "pending"
            inspected = observe_node(
                service.database,
                status,
                status["nodes"][0],
                SimpleNamespace(list_progress_events=lambda *args, **kwargs: ()),
                recipient="agent:test:machine:observer",
                at=1000,
                epoch="test",
            )
            assert inspected["workerCleanup"] == "pending"
            intents = store.workflow_worker_cleanup_intents(graph_id)
            assert len(intents) == 1
            assert intents[0]["actor_node"] == "_actor.work"
            assert intents[0]["state"] == "pending"
        finally:
            store.close()
    finally:
        assert service.close()
