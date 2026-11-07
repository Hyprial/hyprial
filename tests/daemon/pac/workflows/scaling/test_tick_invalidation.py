"""Input changes, deadlines, child completion and migration keep tick decisions live."""

import sqlite3
from contextlib import closing

import pytest

import hyprial.daemon.impl.pac.storage.migrations as migrations
from hyprial.daemon.impl.pac.graphs.reactor import PacReactor
from hyprial.daemon.impl.pac.storage.journal import append_event
from hyprial.daemon.impl.pac.storage.store import PacGraphStore
from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService
from tests.daemon.pac.expansion.engine.support import AGENT
from tests.daemon.pac.expansion.engine.test_transactions import _prepared_parent_and_child
from tests.support.pac.migrations import strip_tick_v19

from .support import USER, Sender, build_scale, measured_tick


def _facts(db):
    return {name: [tuple(row) for row in db.execute(f'SELECT * FROM {name} ORDER BY rowid')]
            for name in ('graphs', 'nodes', 'edges', 'workflow_graphs', 'workflow_nodes',
                         'notifications', 'flag_events', 'journal', 'workflow_deliveries',
                         'workflow_worker_receipts', 'workflow_expansions', 'workflow_worktrees')}


def _v18(path):
    service, _, _, graphs = build_scale(path, graph_count=1, notification_count=1)
    database = service.database
    assert service.close()
    with closing(sqlite3.connect(database)) as db, db:
        strip_tick_v19(db)
        facts = _facts(db)
    return database, graphs[0], facts


def test_v18_upgrade_preserves_facts_and_requires_initial_projection(tmp_path):
    database, graph, facts = _v18(tmp_path / 'old')
    store = PacGraphStore(database)
    try:
        assert store._db.execute('PRAGMA user_version').fetchone()[0] == 19
        assert _facts(store._db) == facts
        row = store._db.execute('SELECT * FROM workflow_tick_projection WHERE graph_id=?', (graph,)).fetchone()
        assert row['revision'] > row['projected_revision'] and row['next_due_ms'] is None
        assert store._db.execute('PRAGMA foreign_key_check').fetchall() == []
    finally:
        store.close()


def test_failed_tick_index_upgrade_rolls_back_schema_and_facts(tmp_path, monkeypatch):
    database, _, facts = _v18(tmp_path / 'rollback')
    original = migrations.upgrade_v18_to_v19

    def fail(db):
        original(db)
        raise RuntimeError('injected v19 migration interruption')

    with monkeypatch.context() as context:
        context.setattr(migrations, 'upgrade_v18_to_v19', fail)
        with pytest.raises(RuntimeError, match='injected v19'):
            PacGraphStore(database)
    with closing(sqlite3.connect(database)) as db, db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 18
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='workflow_tick_projection'").fetchone() is None
        assert _facts(db) == facts
    reopened = PacGraphStore(database)
    reopened.close()


def test_rolled_back_input_change_does_not_force_projection(tmp_path, monkeypatch):
    service, _, _, graphs = build_scale(tmp_path / 'rollback-input', graph_count=1, notification_count=1)
    try:
        with closing(sqlite3.connect(service.database)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE workflow_nodes SET state='pending',request_id=NULL WHERE graph_id=?", (graphs[0],))
            db.rollback()
        assert measured_tick(service, monkeypatch, 2100)['projections'] == []
    finally:
        assert service.close()


def test_updated_step_deadline_after_idle_keeps_strict_boundary(tmp_path):
    service, _, _, graphs = build_scale(tmp_path / 'cap', graph_count=1, notification_count=1)
    try:
        with closing(sqlite3.connect(service.database)) as db, db:
            db.execute('UPDATE workflow_nodes SET deadline_ms=2100 WHERE graph_id=?', (graphs[0],))
            db.execute("UPDATE nodes SET deadline_ms=2100 WHERE graph_id=? AND kind='clock'", (graphs[0],))
        service._tick(2100)
        assert service.status(run_id=graphs[0])['state'] == 'running'
        service._tick(2101)
        assert service.status(run_id=graphs[0])['state'] == 'failed'
    finally:
        assert service.close()


def test_commit_after_projection_is_not_cleared_by_its_bookmark(tmp_path, monkeypatch):
    service, _, _, graphs = build_scale(tmp_path / 'racing-input', graph_count=1, notification_count=1)
    graph = graphs[0]
    original = service._project
    injected = False

    def project(store, graph_id, at, dirty=None):
        nonlocal injected
        original(store, graph_id, at, dirty)
        if not injected:
            injected = True
            with closing(sqlite3.connect(service.database)) as db, db:
                db.execute("UPDATE workflow_nodes SET state='pending',request_id=NULL,input_token=NULL WHERE graph_id=?", (graph,))

    try:
        with closing(sqlite3.connect(service.database)) as db, db:
            db.execute("UPDATE workflow_nodes SET reason_ref='new-input' WHERE graph_id=?", (graph,))
        monkeypatch.setattr(service, '_project', project)
        service._tick(2100)
        assert injected
        assert service.status(run_id=graph)['nodes'][0]['state'] == 'pending'
        service._tick(2101)
        node = service.status(run_id=graph)['nodes'][0]
        assert node['state'] == 'requested' and node['requestId'] is not None
    finally:
        assert service.close()


def test_child_close_reawakens_its_previously_stable_parent(tmp_path):
    state = tmp_path / 'expansion'
    state.mkdir()
    store = PacGraphStore(state / 'pac-graph.sqlite3')
    try:
        parent, context = _prepared_parent_and_child(store)
        PacReactor(store, clock=lambda: 200, expansion_context=context).set_flag(
            parent, 'planner', actor=AGENT, expected_request='request-1',
            reason_ref='evidence:plan', output_text='planned', expansion='version: 2',
        )
        child = context.child_graph_id
    finally:
        store.close()
    service = GraphWorkflowService(state_dir=state, owner='test', machine='machine',
                                   sender=Sender(), start_thread=False)
    try:
        for at in (300, 301, 302):
            service._tick(at)
        request = service.status(run_id=child)['nodes'][0]['requestId']
        store = PacGraphStore(service.database)
        try:
            PacReactor(store, clock=lambda: 400).set_flag(
                child, 'work', actor=AGENT, expected_request=request,
                reason_ref='evidence:child-complete', output_text='child result',
            )
        finally:
            store.close()
        service._tick(410)
        assert service.status(run_id=child)['state'] == 'completed'
        service._tick(420)
        assert service.status(run_id=parent)['state'] == 'completed'
    finally:
        assert service.close()


def test_delivery_audit_alone_is_not_a_projection_input(tmp_path, monkeypatch):
    service, _, _, graphs = build_scale(tmp_path / 'audit', graph_count=1, notification_count=1)
    try:
        store = PacGraphStore(service.database)
        try:
            with store.write() as db:
                append_event(db, graph_id=graphs[0], version=store.graph(graphs[0])['version'],
                             type='delivery_changed', at=2100, data={'state': 'accepted'})
        finally:
            store.close()
        assert measured_tick(service, monkeypatch, 2101)['projections'] == []
    finally:
        assert service.close()


@pytest.mark.parametrize('kind', ['actor_lost', 'actor_unowned', 'launch_failed'])
def test_actor_lost_journal_reawakens_an_idle_workflow(tmp_path, kind):
    service = GraphWorkflowService(state_dir=tmp_path / 'lost', owner='tick-test', machine='machine',
                                   sender=Sender(), clock_ms=lambda: 1000, start_thread=False)
    try:
        graph = service.start(yaml_text='version: 2\nname: lost\nnodes:\n  - id: work\n    task: work\n',
                              sender=USER, operation_key='lost')['graphId']
        for at in (1000, 1001, 1002):
            service._tick(at)
        actor_node = service.status(run_id=graph)['nodes'][0]['actorNode']
        assert actor_node is not None
        store = PacGraphStore(service.database)
        try:
            with store.write() as db:
                append_event(db, graph_id=graph, version=store.graph(graph)['version'],
                             type=kind, at=1100, data={'nodeId': actor_node})
        finally:
            store.close()
        service._tick(1101)
        state = service.status(run_id=graph)
        assert state['state'] == 'failed'
        assert state['nodes'][0]['reasonRef'] == f'pac:{kind}'
    finally:
        assert service.close()
