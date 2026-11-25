"""The v20 hold-age migration preserves dev's v19 tick index and is atomic."""

from contextlib import closing
import sqlite3

import pytest

from hyprial.daemon.impl.pac.storage import migrations
from hyprial.daemon.impl.pac.storage.journal import append_event
from hyprial.daemon.impl.pac.storage.store import PacGraphStore
from tests.support.pac.migrations import strip_held_v20

from .support import AGENT, Clock, Sender, held_graph, service, start_graph


def _previous_database(tmp_path):
    workflow = service(tmp_path, Sender(), Clock())
    try:
        held = held_graph(workflow)
        unknown = start_graph(workflow, [{"id": "work", "task": "work", "owner": AGENT}], key="unknown")
        running = start_graph(workflow, [{"id": "work", "task": "work", "owner": AGENT}], key="running")
    finally:
        assert workflow.close()
    with closing(sqlite3.connect(workflow.database)) as db, db:
        strip_held_v20(db)
        # A legacy held graph can lack a transition journal fact. Do not guess.
        db.execute("UPDATE workflow_graphs SET state='held' WHERE graph_id=?", (unknown,))
        # Ensure backfill chooses the latest failure, not the original at 1000.
        append_event(db, graph_id=held, version=1, type="workflow_changed", at=2000,
                     data={"state": "failed", "nodeId": "review"})
    return workflow.database, held, unknown, running


def test_v19_backfill_preserves_tick_index_and_tracks_new_column(tmp_path):
    database, held, unknown, running = _previous_database(tmp_path)
    store = PacGraphStore(database)
    try:
        db = store._db
        assert db.execute("PRAGMA user_version").fetchone()[0] == 20
        ages = dict(db.execute("SELECT graph_id,held_at_ms FROM workflow_graphs"))
        assert ages == {held: 2000, unknown: None, running: None}
        before = db.execute("SELECT revision FROM workflow_tick_projection WHERE graph_id=?", (held,)).fetchone()[0]
        with store.write():
            db.execute("UPDATE workflow_graphs SET held_at_ms=3000 WHERE graph_id=?", (held,))
        after = db.execute("SELECT revision FROM workflow_tick_projection WHERE graph_id=?", (held,)).fetchone()[0]
        assert after > before
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        store.close()


def test_v20_migration_failure_rolls_back_column_backfill_and_triggers(tmp_path, monkeypatch):
    database, held, _, _ = _previous_database(tmp_path)
    with closing(sqlite3.connect(database)) as db:
        before_schema = db.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall()
        before_facts = db.execute("SELECT * FROM workflow_graphs ORDER BY graph_id").fetchall()
    upgrade = migrations._upgrade_v19_to_v20

    def fail(db):
        upgrade(db)
        raise RuntimeError("injected v20 migration interruption")

    with monkeypatch.context() as context:
        context.setattr(migrations, "_upgrade_v19_to_v20", fail)
        with pytest.raises(RuntimeError, match="injected v20"):
            PacGraphStore(database)
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 19
        assert db.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall() == before_schema
        assert db.execute("SELECT * FROM workflow_graphs ORDER BY graph_id").fetchall() == before_facts
    store = PacGraphStore(database)
    try:
        assert store._db.execute("SELECT held_at_ms FROM workflow_graphs WHERE graph_id=?", (held,)).fetchone()[0] == 2000
    finally:
        store.close()
