import json
import sqlite3

import pytest

from hyprial.daemon.impl.pac.storage.legacy import LegacyWorkflowHistory, cutover
from hyprial.identity import PacError


@pytest.fixture
def legacy(tmp_path):
    path = tmp_path / "workflows.sqlite3"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE runs(run_id TEXT PRIMARY KEY,name TEXT,state TEXT,sender TEXT,
                          created_at_ms INTEGER,finished_at_ms INTEGER,yaml_text TEXT);
        CREATE TABLE targets(run_id TEXT,target TEXT,state TEXT,reply_excerpt TEXT);
        CREATE TABLE actor_assign(actor TEXT,assign_kind TEXT,assign_ref TEXT,released_at_ms INTEGER);
        INSERT INTO runs VALUES ('active','active','running','user:test',1,NULL,'original task body');
        INSERT INTO runs VALUES ('done','done','completed','user:test',1,2,'finished body');
        INSERT INTO targets VALUES ('active','worker','awaiting','partial result');
        INSERT INTO targets VALUES ('done','worker','done','verified result');
        INSERT INTO actor_assign VALUES ('worker','workflow','active',NULL);
        INSERT INTO actor_assign VALUES ('unrelated','routine','routine-x',NULL);
    """)
    db.close()
    return tmp_path


def test_fresh_home_installs_a_sealed_barrier_without_inventing_history(tmp_path):
    result = cutover(tmp_path, at=100)
    assert result == {
        "present": False,
        "terminated": 0,
        "sealed": True,
        "at": 100,
        "cancelled": [],
    }
    assert LegacyWorkflowHistory(tmp_path).list() == {"runs": []}
    with sqlite3.connect(tmp_path / "workflows.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        for table in (
            "runs",
            "targets",
            "agent_task_runs",
            "agent_task_targets",
            "workflow_effects",
            "actor_assign",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="WORKFLOW_RETIRED"):
                db.execute(f"INSERT INTO {table} DEFAULT VALUES")
            db.rollback()
    assert cutover(tmp_path, at=200) == result


def test_cutover_terminates_preserves_history_and_seals_old_writers(legacy):
    assert cutover(legacy, at=100)["terminated"] == 1
    history = LegacyWorkflowHistory(legacy)
    active = history.status("active")
    assert active["state"] == "cancelled"
    assert active["priorState"] == "running"
    assert active["yaml_text"] == "original task body"
    assert active["targets"][0]["reply_excerpt"] == "partial result"
    assert active["readOnly"] is True
    assert history.status("done")["state"] == "completed"
    db = sqlite3.connect(legacy / "workflows.sqlite3")
    try:
        previous = json.loads(
            db.execute(
                "SELECT prior_json FROM pac_prior_states WHERE table_name='runs'"
            ).fetchone()[0]
        )
        assert previous["state"] == "running" and previous["finished_at_ms"] is None
        assert (
            db.execute(
                "SELECT released_at_ms FROM actor_assign WHERE actor='worker'"
            ).fetchone()[0]
            == 100
        )
        assert (
            db.execute(
                "SELECT released_at_ms FROM actor_assign WHERE actor='unrelated'"
            ).fetchone()[0]
            is None
        )
        for statement in (
            "INSERT INTO runs(run_id) VALUES ('new')",
            "UPDATE runs SET state='running'",
            "DELETE FROM targets",
            "DELETE FROM pac_prior_states",
            "UPDATE pac_cutover SET terminated=0",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="WORKFLOW_RETIRED"):
                db.execute(statement)
            db.rollback()
    finally:
        db.close()
    assert cutover(legacy, at=200)["at"] == 100
    assert history.status("active")["priorState"] == "running"


def test_archive_is_filtered_by_original_owner(legacy):
    cutover(legacy)
    history = LegacyWorkflowHistory(legacy)
    assert history.list(viewer="user:other")["runs"] == []
    with pytest.raises(PacError):
        history.status("active", viewer="user:other")
    assert history.status("active", viewer="user:test")["runId"] == "active"


def test_cutover_failure_rolls_back_termination_and_can_be_retried(legacy):
    db = sqlite3.connect(legacy / "workflows.sqlite3")
    db.execute(
        "CREATE TRIGGER fail_cutover BEFORE UPDATE ON targets BEGIN SELECT RAISE(ABORT,'injected'); END"
    )
    db.close()
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        cutover(legacy)
    db = sqlite3.connect(legacy / "workflows.sqlite3")
    try:
        assert (
            db.execute("SELECT state FROM runs WHERE run_id='active'").fetchone()[0]
            == "running"
        )
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name='pac_cutover'"
        ).fetchall()
        db.execute("DROP TRIGGER fail_cutover")
    finally:
        db.close()
    assert cutover(legacy)["terminated"] == 1


def test_account_switch_does_not_rewrite_a_sealed_legacy_archive(legacy):
    from hyprial.daemon.impl.owner_migration import MigrationPlan, plan_database, apply_plan

    cutover(legacy, at=100)
    plan = MigrationPlan()
    plan_database(legacy / "workflows.sqlite3", "test", "next-owner", plan)
    assert plan.writes == []
    assert plan.unclassified == []
    assert plan.skipped_archival_columns
    assert apply_plan(plan) == 0
    assert LegacyWorkflowHistory(legacy).status("active")["sender"] == "user:test"
