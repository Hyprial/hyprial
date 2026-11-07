"""G6 ground floor: real slice-1 schema upgrade, rollback, typed outbox."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from tests.support.pac.migrations import strip_tick_v19

import hyprial.daemon.impl.pac.storage.migrations as migrations
from hyprial.daemon.impl.pac.storage.journal import EVENT_TYPES, EventEnvelope, append_event
from hyprial.identity import PacError
from hyprial.daemon.impl.pac.graphs.projection import ProjectionState, fold_event
from hyprial.daemon.impl.pac.graphs.reactor import NullSender, PacReactor
from hyprial.daemon.impl.pac.storage.store import PacGraphStore


def _strip_v18(db: sqlite3.Connection) -> None:
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


@pytest.fixture()
def legacy(tmp_path: Path):
    path = tmp_path / "pac-graph.sqlite3"
    db = sqlite3.connect(path)
    schema = Path(__file__).resolve().parents[4] / 'tests/fixtures/pac/slice-1-schema.sql'
    db.executescript(schema.read_text())
    db.execute("INSERT INTO graphs VALUES ('g', 'legacy', 4, 'owner', 1)")
    db.execute("INSERT INTO nodes VALUES ('g', 'a', 'owner', 'ref#a', 'task', NULL, 1, 'owner', 3, 'result#a')")
    db.execute("INSERT INTO nodes VALUES ('g', 'b', 'owner', 'ref#b', 'clock', 5, 0, NULL, NULL, NULL)")
    db.execute("INSERT INTO edges VALUES ('g', 'a', 'b', 'forward')")
    db.execute("INSERT INTO flag_events VALUES (7, 'e', 'g', 4, 'a', 'set', 'owner', 3, 'result#a')")
    db.execute("INSERT INTO notifications VALUES ('e', 'a -> b', 'turn', 'owner', 1, 'original text', 'owner', NULL, 3, NULL)")
    db.execute("INSERT INTO notifications VALUES ('overdue:g:v4:b:5', 'clock:b', 'overdue', 'owner', NULL, 'original overdue', 'owner', NULL, 6, NULL)")
    db.commit()
    before = {table: db.execute(f"SELECT * FROM {table}").fetchall()
              for table in ("graphs", "nodes", "edges", "flag_events", "notifications")}
    assert db.execute("PRAGMA user_version").fetchone()[0] == 0
    db.close()
    return path, before


def test_real_slice1_upgrade_preserves_history_and_notification_keys(legacy):
    path, before = legacy
    store = PacGraphStore(path)
    try:
        assert store._db.execute("PRAGMA user_version").fetchone()[0] == 19
        for table, rows in before.items():
            actual = [tuple(row) for row in store._db.execute(f"SELECT * FROM {table}")]
            if table == "graphs":
                assert actual[0][5] is not None, "legacy graphs with facts are frozen on migration"
                assert actual[0][9] is None, "migration must not invent an operation key"
                actual = [row[:5] for row in actual]
            if table == "notifications":
                assert all(
                    row[-4:] == (None, None, None, None) for row in actual
                ), "do not invent historical inputs or delivery failures"
                actual = [row[:-4] for row in actual]
            if table == "nodes":
                assert all(row[10:] == (None, None, None, None) for row in actual), (
                    "migration must not infer actor identity, requirements, or clock guards"
                )
                actual = [row[:10] for row in actual]
            assert actual == rows
        baseline = store.journal_events("g")
        # B1 cursor neutrality: v7 -> v8 appends NOTHING -- the two baselines
        # are the pre-v8 slice-1 archive + resync marker, and no third event
        # moves cursorFloor or any consumer's cursor.  The era report lives in
        # the schema_era metadata table instead (see the rewrite tests).
        assert len(baseline) == 2
        assert baseline[1]["data"] == {"sourceSchemaVersion": 2, "resync": True}
        assert baseline[0]["type"] == "migration_baseline"
        assert baseline[0]["data"]["flag_events"][0]["seq"] == 7
        assert baseline[0]["data"]["nodes"][0]["flag_reason_ref"] == "result#a"
        assert len(baseline[0]["data"]["notifications"]) == 2
    finally:
        store.close()
    reopened = PacGraphStore(path)
    try:
        assert reopened.journal_events("g") == baseline, "migration is not repeated"
        outcome = PacReactor(reopened).reset_flag("g", "a", actor="owner")
        assert reopened.flag_events("g")[-1].seq == 8
        assert outcome.event["version"] == 4
    finally:
        reopened.close()


def test_agent_task_projection_tables_are_added_empty_on_upgrade(legacy):
    store = PacGraphStore(legacy[0])
    try:
        tables = {
            str(row[0])
            for row in store._db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "pac_agent_task_runs",
            "pac_agent_task_targets",
            "pac_agent_task_dispatches",
            "pac_agent_task_events",
            "pac_agent_task_results",
        } <= tables
        assert store._db.execute(
            "SELECT COUNT(*) FROM pac_agent_task_runs"
        ).fetchone()[0] == 0
        assert store._db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        store.close()


def test_v12_workflow_deadline_survives_and_new_graph_waits_for_request(tmp_path):
    from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService

    state_dir = tmp_path / "state"
    clock = [1000]
    service = GraphWorkflowService(
        state_dir=state_dir,
        owner="test",
        machine="machine",
        sender=NullSender(),
        clock_ms=lambda: clock[0],
        start_thread=False,
    )
    old_graph = service.start(
        yaml_text="""
version: 2
name: old
nodes:
  - id: work
    owner: agent:test:machine:worker
    task: work
    deadline_ms: 9000
""",
        sender="user:test",
        operation_key="old",
    )["graphId"]
    database = service.database
    assert service.close()

    db = sqlite3.connect(database)
    db.execute("PRAGMA foreign_keys = OFF")
    _strip_v18(db)
    db.execute(
        """
        CREATE TABLE workflow_nodes_v12 (
            graph_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            actor_node TEXT,
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK(state IN ('pending','requested','done','failed','blocked','cancelled')),
            request_id TEXT,
            input_token TEXT,
            generation INTEGER NOT NULL DEFAULT 0,
            deadline_ms INTEGER NOT NULL,
            reason_ref TEXT,
            PRIMARY KEY(graph_id,node_id),
            FOREIGN KEY(graph_id,node_id) REFERENCES nodes(graph_id,node_id)
        )
        """
    )
    db.execute(
        """
        INSERT INTO workflow_nodes_v12
            (graph_id,node_id,actor_node,state,request_id,input_token,generation,
             deadline_ms,reason_ref)
        SELECT graph_id,node_id,actor_node,state,request_id,input_token,generation,
               deadline_ms,reason_ref
        FROM workflow_nodes
        """
    )
    db.execute("DROP TABLE workflow_nodes")
    db.execute("ALTER TABLE workflow_nodes_v12 RENAME TO workflow_nodes")
    db.execute("CREATE INDEX workflow_nodes_request ON workflow_nodes(request_id)")
    # A real v12 database predates v14's output columns on these tables too.
    db.execute("ALTER TABLE workflow_outcome_receipts DROP COLUMN output_text")
    db.execute("ALTER TABLE remote_workflow_outbox DROP COLUMN output_text")
    db.execute("ALTER TABLE workflow_graphs DROP COLUMN roster_json")
    db.execute("ALTER TABLE workflow_graphs DROP COLUMN roster_digest")
    db.execute("DROP TABLE workflow_worker_cleanup_intents")
    db.execute("DROP TABLE workflow_worker_receipts")
    db.execute("DROP INDEX notifications_graph_retryable")
    for column in ("failure_detail", "failure_code", "failed_at"):
        db.execute(f"ALTER TABLE notifications DROP COLUMN {column}")
    db.execute("PRAGMA user_version = 12")
    db.commit()
    db.close()

    migrated = PacGraphStore(database)
    try:
        old = migrated._db.execute(
            "SELECT deadline_ms,timeout_ms FROM workflow_nodes "
            "WHERE graph_id=? AND node_id='work'",
            (old_graph,),
        ).fetchone()
        assert tuple(old) == (9000, None)
        columns = {
            row["name"]: row for row in migrated._db.execute("PRAGMA table_info(workflow_nodes)")
        }
        assert columns["deadline_ms"]["notnull"] == 0
        assert migrated._db.execute("PRAGMA user_version").fetchone()[0] == 19
    finally:
        migrated.close()

    clock[0] = 5000
    service = GraphWorkflowService(
        state_dir=state_dir,
        owner="test",
        machine="machine",
        sender=NullSender(),
        clock_ms=lambda: clock[0],
        start_thread=False,
    )
    try:
        new_graph = service.start(
            yaml_text="""
version: 2
name: new
nodes:
  - id: upstream
    owner: agent:test:machine:worker
    task: upstream
  - id: work
    owner: agent:test:machine:worker
    task: work
    after: [upstream]
    timeout: 1s
""",
            sender="user:test",
            operation_key="new",
        )["graphId"]
        pending = next(
            node
            for node in service.status(run_id=new_graph)["nodes"]
            if node["nodeId"] == "work"
        )
        assert pending["deadlineMs"] is None
        assert pending["timeoutMs"] == 1000
    finally:
        assert service.close()


def test_v13_adds_optional_workflow_output_columns(tmp_path):
    path = tmp_path / "pac-graph.sqlite3"
    store = PacGraphStore(path)
    store.close()
    db = sqlite3.connect(path)
    _strip_v18(db)
    for table in (
        "workflow_nodes",
        "workflow_outcome_receipts",
        "remote_workflow_outbox",
    ):
        db.execute(f"ALTER TABLE {table} DROP COLUMN output_text")
    db.execute("ALTER TABLE workflow_graphs DROP COLUMN roster_json")
    db.execute("ALTER TABLE workflow_graphs DROP COLUMN roster_digest")
    db.execute("DROP TABLE workflow_worker_cleanup_intents")
    db.execute("DROP TABLE workflow_worker_receipts")
    db.execute("DROP INDEX notifications_graph_retryable")
    for column in ("failure_detail", "failure_code", "failed_at"):
        db.execute(f"ALTER TABLE notifications DROP COLUMN {column}")
    db.execute("PRAGMA user_version = 13")
    db.commit()
    db.close()

    migrated = PacGraphStore(path)
    try:
        assert migrated._db.execute("PRAGMA user_version").fetchone()[0] == 19
        for table in (
            "workflow_nodes",
            "workflow_outcome_receipts",
            "remote_workflow_outbox",
        ):
            columns = {
                row["name"]
                for row in migrated._db.execute(f"PRAGMA table_info({table})")
            }
            assert "output_text" in columns
    finally:
        migrated.close()


def test_upgrade_failure_rolls_back_schema_data_and_version(legacy, monkeypatch):
    path, before = legacy
    upgrade = migrations._upgrade_v1_to_v2

    def crash(db):
        upgrade(db)
        raise RuntimeError("migration crash")

    with monkeypatch.context() as patch:
        patch.setattr(migrations, "_upgrade_v1_to_v2", crash)
        with pytest.raises(RuntimeError, match="migration crash"):
            PacGraphStore(path)
    db = sqlite3.connect(path)
    try:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='journal'").fetchall()
        for table, rows in before.items():
            assert db.execute(f"SELECT * FROM {table}").fetchall() == rows
    finally:
        db.close()
    recovered = PacGraphStore(path)
    assert len(recovered.journal_events("g")) == 2
    recovered.close()


@pytest.mark.parametrize("type", EVENT_TYPES)
def test_typed_envelope_round_trip_does_not_implicitly_mutate_flags(legacy, type):
    path, _ = legacy
    store = PacGraphStore(path)
    try:
        before = [node.to_json() for node in store.nodes("g")]
        data = {"nodeId": "a", "reference": "ref#result"}
        with store.write() as db:
            seq = append_event(db, graph_id="g", version=4, type=type, at=10, data=data)
        event = store.journal_events("g", after_seq=seq - 1)[0]
        assert event["schemaVersion"] == 1
        assert event["graphId"] == "g"
        assert event["type"] == type
        assert event["data"] == data
        assert json.loads(json.dumps(event)) == event
        assert EventEnvelope.from_json(json.loads(json.dumps(event))).to_json() == event
        identity = store._db.execute("SELECT journal_id FROM journal_meta").fetchone()[0]
        public = {**event, "journalId": identity}
        assert EventEnvelope.from_json(json.loads(json.dumps(public))).to_json() == public
        assert [node.to_json() for node in store.nodes("g")] == before
    finally:
        store.close()


def test_journal_requires_transaction_rejects_unknown_and_is_append_only(legacy):
    store = PacGraphStore(legacy[0])
    try:
        kwargs = dict(graph_id="g", version=4, type="actor_lost", at=10, data={})
        with pytest.raises(RuntimeError, match="business transaction"):
            append_event(store._db, **kwargs)
        with store.write() as db:
            with pytest.raises(PacError, match="unknown PAC event type") as raised:
                append_event(db, **{**kwargs, "type": "surprise"})
            assert raised.value.code == "PAC_EVENT_TYPE_UNKNOWN"
            with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
                db.execute("INSERT INTO journal(event_id,graph_id,version,type,at,data_json) VALUES ('bad','g',4,'surprise',10,'{}')")
            for statement in ("UPDATE journal SET at=11", "DELETE FROM journal"):
                with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                    db.execute(statement)
        assert len(store.journal_events("g")) == 2
    finally:
        store.close()


def test_legacy_pending_delivery_appends_updates_without_inventing_old_plans(legacy):
    store = PacGraphStore(legacy[0])
    try:
        cursor = store.journal_events("g")[-1]["seq"]
        sender = NullSender()
        reactor = PacReactor(store, sender=sender, clock=lambda: 20)
        assert reactor.resend_undelivered("g")["delivered"] == 2
        changes = store.journal_events("g", cursor)
        assert [e["type"] for e in changes] == ["delivery_changed", "delivery_changed"]
        assert [e["version"] for e in changes] == [4, 4]
        assert [message["text"] for message in sender.sent] == ["original text", "original overdue"]
        assert reactor.resend_undelivered("g")["delivered"] == 0
        assert store.journal_events("g", cursor) == changes
    finally:
        store.close()


@pytest.mark.parametrize("event", [
    {"nodeId": "a", "action": "launch_failed"},
    {"nodeId": "a", "type": "actor_lost"},
    {"nodeId": "a", "type": "actor_down", "action": "reset"},
])
def test_non_flag_event_cannot_fall_through_to_reset(event):
    state = ProjectionState(flags={"a": True})
    with pytest.raises(ValueError, match="explicit set/reset"):
        fold_event(state, {"nodes": {"a": {}}, "edges": []}, event)
    assert state.flags == {"a": True}
    assert state.events == []


def test_v2_to_public_schema_interruption_is_atomic(legacy, monkeypatch):
    path, _ = legacy
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("BEGIN IMMEDIATE")
    migrations._upgrade_v1_to_v2(db)  # exact checkpoint-1 schema/implementation
    db.execute("PRAGMA user_version = 2")
    db.commit()
    old_journal = [dict(row) for row in db.execute("SELECT * FROM journal")]
    db.close()
    upgrade = migrations._upgrade_v2_to_v3

    def crash(db):
        upgrade(db)
        raise RuntimeError("public migration crash")

    with monkeypatch.context() as patch:
        patch.setattr(migrations, "_upgrade_v2_to_v3", crash)
        with pytest.raises(RuntimeError, match="public migration crash"):
            PacGraphStore(path)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    assert db.execute("PRAGMA user_version").fetchone()[0] == 2
    assert [dict(row) for row in db.execute("SELECT * FROM journal")] == old_journal
    assert not db.execute("SELECT name FROM sqlite_master WHERE name='journal_meta'").fetchall()
    assert "activated_at" not in {row["name"] for row in db.execute("PRAGMA table_info(graphs)")}
    db.close()
    store = PacGraphStore(path)
    identity = store._db.execute("SELECT journal_id FROM journal_meta").fetchone()[0]
    assert store.graph("g")["activated_by"] == "migration"
    store.close()
    store = PacGraphStore(path)
    assert store._db.execute("SELECT journal_id FROM journal_meta").fetchone()[0] == identity
    store.close()


def test_database_produced_by_real_slice1_code_upgrades_without_rebuilding_facts(tmp_path):
    import hashlib

    from hyprial.daemon.impl.pac.views.context import node_context
    from hyprial.daemon.impl.pac.graphs.edits import close_graph
    from hyprial.daemon.impl.pac.graphs.subscription import events_since, snapshot

    fixture = Path(__file__).resolve().parents[4] / 'tests/fixtures/pac'
    manifest = json.loads((fixture / "slice-1-real.json").read_text())
    sql = (fixture / "slice-1-real.sql").read_bytes()
    assert hashlib.sha256(sql).hexdigest() == manifest["dumpSha256"]
    assert manifest["sourceCommit"] == "17de00f65b73468a985a78e3ce66b8e16319a406"
    path = tmp_path / "original.sqlite3"
    db = sqlite3.connect(path)
    db.executescript(sql.decode())  # exact dump made by the ORIGINAL Graph/PacReactor APIs
    assert db.execute("PRAGMA user_version").fetchone()[0] == 0
    tables = ("graphs", "nodes", "edges", "flag_events", "notifications")
    columns = {table: [row[1] for row in db.execute(f"PRAGMA table_info({table})")] for table in tables}
    rows = {table: db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() for table in tables}
    db.close()
    store = PacGraphStore(path)
    gid = manifest["graphId"]
    try:
        for table in tables:
            assert [tuple(row) for row in store._db.execute(
                f"SELECT {','.join(columns[table])} FROM {table} ORDER BY rowid"
            )] == rows[table]
        assert store._db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert store._db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        state = snapshot(path, gid)
        assert state["active"]["by"] == "migration" and state["cursorFloor"] > 0
        assert all(item["plan"] is None for item in state["notifications"])
        with pytest.raises(PacError) as error:
            events_since(path, gid, 0)
        assert error.value.code == "PAC_EVENTS_RESYNC"
        context = node_context(path, gid, "b")
        assert context["completedCount"] == 1 and context["currentActivation"] is None
        assert context["predecessors"][0]["reasonRef"] == "artifact#result-a-v2"
        assert context["predecessors"][0]["setBy"] == "fixture-owner"
        assert context["predecessors"][0]["setAt"] == 100
        assert "actor" not in context and "deadlineMs" not in context
        assert node_context(path, gid, "timer")["deadlineMs"] == 50
        count = len(store.flag_events(gid))
        close_graph(store, gid, actor="fixture-owner")
        assert len(store.flag_events(gid)) == count
        assert store.node(gid, "a").flag is True and store.node(gid, "b").flag is True
        assert store.journal_events(gid)[-1]["type"] == "graph_closed"
        assert node_context(path, gid, "b")["closed"]["by"] == "fixture-owner"
    finally:
        store.close()
    reopened = PacGraphStore(path)
    try:
        assert [e["type"] for e in reopened.journal_events(gid)].count("graph_closed") == 1
        assert len(reopened.flag_events(gid)) == count
    finally:
        reopened.close()


def test_future_schema_is_rejected_without_downgrade(tmp_path):
    path = tmp_path / "future.sqlite3"
    db = sqlite3.connect(path)
    db.execute("PRAGMA user_version = 99")
    db.close()
    with pytest.raises(RuntimeError, match="unsupported PAC schema version 99"):
        PacGraphStore(path)
    db = sqlite3.connect(path)
    assert db.execute("PRAGMA user_version").fetchone()[0] == 99
    db.close()
