"""Historical assignment rows remain readable; PAC lifecycle uses graph markers."""

import sqlite3

from hyprial.daemon.impl.pac.actors.assignments import (
    ASSIGN_KINDS,
    ASSIGN_PRODUCERS,
    kinds_without_producer,
    reclamation_blocked_reason,
)
from hyprial.daemon.impl.pac.storage.assignments import AssignStore


def test_shared_store_preserves_existing_assignment_rows(tmp_path):
    path = tmp_path / "assign.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE actor_assign(actor TEXT,assign_kind TEXT,assign_ref TEXT,assigned_at_ms INTEGER,released_at_ms INTEGER,PRIMARY KEY(actor,assign_kind,assign_ref))"
        )
        db.execute(
            "INSERT INTO actor_assign VALUES ('worker','workflow','old-run',7,NULL)"
        )
        db.execute(
            "INSERT INTO actor_assign VALUES ('worker','routine','old-routine',8,9)"
        )
    store = AssignStore(path)
    try:
        assert store.assign_rows(actor="worker") == (
            ("routine", "old-routine", 9),
            ("workflow", "old-run", None),
        )
        assert store.live_assigns(actor="worker") == (("workflow", "old-run"),)
        store.release_workflow_assigns(run_id="old-run", released_at_ms=10)
        store.release_workflow_assigns(run_id="old-run", released_at_ms=20)
        assert store.assign_rows(actor="worker")[-1] == ("workflow", "old-run", 10)
    finally:
        store.close()


def test_empty_store_does_not_invent_actor_ownership(tmp_path):
    store = AssignStore(tmp_path / "assign.sqlite3")
    try:
        assert store.live_assign_rows() == ()
        assert store.assign_rows(actor="standing-by") == ()
    finally:
        store.close()


def test_retired_producers_cannot_authorize_reclamation():
    assert set(ASSIGN_KINDS) == set(ASSIGN_PRODUCERS)
    assert set(kinds_without_producer()) == {"workflow", "routine"}
    assert reclamation_blocked_reason() is not None
