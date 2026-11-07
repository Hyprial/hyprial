"""Schema-18 storage invariants for expansion custody."""

from __future__ import annotations

import json
import sqlite3

import pytest

import hyprial.daemon.impl.pac.storage.migrations as migrations
from hyprial.daemon.impl.pac.storage.migrations import SCHEMA_VERSION

from .support import store, strip_v18


def test_fresh_schema_keeps_expansion_tables_and_columns(tmp_path):
    graph_store = store(tmp_path)
    try:
        db = graph_store._db
        assert SCHEMA_VERSION == 19
        assert db.execute("PRAGMA user_version").fetchone()[0] == 19
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"workflow_expansions", "workflow_worktrees"} <= tables
        columns = {
            table: {
                row["name"]
                for row in db.execute(f"PRAGMA table_info({table})")
            }
            for table in (
                "workflow_graphs",
                "workflow_nodes",
                "workflow_outcome_receipts",
                "remote_workflow_outbox",
            )
        }
        assert {"parent_graph_id", "parent_node_id", "expansion_deadline_ms"} <= columns["workflow_graphs"]
        assert "node_kind" in columns["workflow_nodes"]
        assert {"graph_id", "node_id", "expansion_digest"} <= columns["workflow_outcome_receipts"]
        assert {"expansion_text", "expansion_digest", "attempt_no"} <= columns["remote_workflow_outbox"]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        graph_store.close()


def test_expansion_identity_and_policy_are_immutable(tmp_path):
    graph_store = store(tmp_path)
    try:
        db = graph_store._db
        db.execute("INSERT INTO graphs(graph_id,name,version,created_by,created_at,operation_key,activated_at,activated_by) VALUES ('p','p',1,'user:test',1,'p',1,'user:test')")
        db.execute("INSERT INTO nodes(graph_id,node_id,owner,brief_ref,kind) VALUES ('p','slot','agent:test:machine:pac-expansion','workflow:p#slot','task')")
        db.execute("INSERT INTO workflow_graphs(graph_id,specification_ref,specification_digest,on_failure,state) VALUES ('p','ref','digest','terminate','running')")
        db.execute("INSERT INTO workflow_nodes(graph_id,node_id,timeout_ms,node_kind) VALUES ('p','slot',1000,'expansion')")
        db.execute("INSERT INTO graphs(graph_id,name,version,created_by,created_at,operation_key,activated_at,activated_by) VALUES ('c','c',1,'user:test',1,'c',1,'user:test')")
        db.execute("INSERT INTO workflow_graphs(graph_id,specification_ref,specification_digest,on_failure,state,parent_graph_id,parent_node_id,expansion_deadline_ms) VALUES ('c','ref','digest','terminate','running','p','slot',2000)")
        db.execute(
            "INSERT INTO workflow_expansions(parent_graph_id,parent_node_id,child_graph_id,planner_node_id,planner_request_id,generation,expansion_digest,limits_json,accepted_at,deadline_ms) VALUES (?,?,?,?,?,1,?,?,?,?)",
            ("p", "slot", "c", "planner", "request", "a" * 64, json.dumps({"maxNodes": 2}), 1000, 2000),
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE workflow_expansions SET limits_json='{}' WHERE child_graph_id='c'")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE workflow_graphs SET parent_node_id='other' WHERE graph_id='c'")
    finally:
        graph_store.close()


def test_v18_migration_rolls_back_as_one_transaction(tmp_path, monkeypatch):
    path = tmp_path / "pac.sqlite3"
    graph_store = store(tmp_path)
    graph_store.close()
    db = sqlite3.connect(path)
    strip_v18(db)
    db.commit()
    db.close()
    real_upgrade = migrations._upgrade_v17_to_v18

    def crash(connection):
        real_upgrade(connection)
        raise RuntimeError("v18 crash")

    monkeypatch.setattr(migrations, "_upgrade_v17_to_v18", crash)
    with pytest.raises(RuntimeError, match="v18 crash"):
        store(tmp_path)
    db = sqlite3.connect(path)
    try:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 17
        assert db.execute("SELECT name FROM sqlite_master WHERE name='workflow_expansions'").fetchall() == []
    finally:
        db.close()
