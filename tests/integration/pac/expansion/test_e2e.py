"""Planner completion binds one immutable child through the production writer."""

import json
import time
from types import SimpleNamespace

from hyprial.daemon.impl.application.workflows import _WorkflowSurfaceMixin
from hyprial.daemon.impl.dispatch.remote.expansion import preview_origin_expansion
from hyprial.daemon.impl.pac.graphs.authority import PacGraphAuthority
from hyprial.daemon.impl.pac.graphs.authority.expansion import prepare_expansion
from hyprial.daemon.impl.pac.graphs.reactor import NullSender, PacReactor
from hyprial.daemon.impl.pac.storage.store import PacGraphStore
from hyprial.daemon.impl.pac.contracts.expansion import (
    load_expansion_policy,
    validate_parent,
)
from hyprial.daemon.impl.pac.workflows.graphs import read_specification
from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService

from .support import PLANNER, child_document, parent_graph
from .support import policy_home, repository


def test_workflow_start_persists_daemon_injected_policy_snapshot(tmp_path):
    repo, _base = repository(tmp_path)
    home, _work_root = policy_home(tmp_path, repo)
    state = tmp_path / "state"
    database = state / "pac-graph.sqlite3"
    authority = PacGraphAuthority(database, NullSender())
    service = GraphWorkflowService(
        state_dir=state,
        owner="test",
        machine="machine",
        sender=NullSender(),
        admit=lambda spec, _sender: validate_parent(
            spec, policy=load_expansion_policy(home)
        ),
        graph_authority=authority,
        start_thread=True,
    )
    try:
        result = service.start(
            yaml_text=f"""
version: 2
name: admitted-parent
nodes:
  - id: planner
    owner: {PLANNER}
    role: plan
    expands: slot
    task: plan
  - id: slot
    kind: expansion
    after: [planner]
    timeout: 5m
    limits:
      worktree: {{repo: {repo}, base: HEAD}}
""",
            sender="user:test",
        )
        read = PacGraphStore(database, read_only=True)
        try:
            row = read._db.execute(
                "SELECT * FROM workflow_graphs WHERE graph_id=?",
                (result["graphId"],),
            ).fetchone()
            document = read_specification(row)
            assert document["expansionPolicy"]["workRoot"] == str(
                home.parent / "managed"
            )
        finally:
            read.close()
    finally:
        assert service.close()
        assert authority.close()


def test_preflight_and_writer_publish_one_child_with_two_distinct_worktrees(tmp_path):
    store, graph_id, _token, home, work_root, base_oid = parent_graph(tmp_path)
    database = store.path
    store.close()
    raw = child_document()
    context = prepare_expansion(
        database,
        home=home,
        raw=raw,
        graph_id=graph_id,
        node_id="planner",
        request_id="request-1",
        actor=PLANNER,
        at=200,
        machine="machine",
        local_owner="test",
    )
    assert len(context.worktrees) == 2
    assert {plan["baseOid"] for plan in context.worktrees} == {base_oid}
    assert len({plan["path"] for plan in context.worktrees}) == 2
    assert all(str(work_root) in plan["path"] for plan in context.worktrees)

    authority = PacGraphAuthority(database, NullSender())
    try:
        result = authority.set_flag(
            graph_id,
            "planner",
            actor=PLANNER,
            reason_ref="evidence:i5",
            expected_request="request-1",
            output_text="planned",
            expansion=raw,
            expansion_context=context,
        )
        assert result["childGraphId"] == context.child_graph_id
        assert result["expansionDigest"] == context.expansion_digest
    finally:
        assert authority.close()

    read = PacGraphStore(database, read_only=True)
    try:
        child = read.graph(context.child_graph_id)
        assert child is not None
        meta = read._db.execute(
            "SELECT parent_graph_id,parent_node_id FROM workflow_graphs "
            "WHERE graph_id=?",
            (context.child_graph_id,),
        ).fetchone()
        assert tuple(meta) == (graph_id, "slot")
        assert read._db.execute(
            "SELECT COUNT(*) FROM workflow_expansions WHERE parent_graph_id=?",
            (graph_id,),
        ).fetchone()[0] == 1
    finally:
        read.close()


def _set_deadlines(store, graph_id, *, planner, placeholder=120_200):
    with store.write() as db:
        db.execute(
            "UPDATE workflow_nodes SET deadline_ms=? "
            "WHERE graph_id=? AND node_id='planner'",
            (planner, graph_id),
        )
        db.execute(
            "UPDATE workflow_nodes SET deadline_ms=? "
            "WHERE graph_id=? AND node_id='slot'",
            (placeholder, graph_id),
        )


def test_preflight_accepts_at_planner_deadline_boundary(tmp_path):
    store, graph_id, _token, home, _work_root, _base_oid = parent_graph(
        tmp_path, child_nodes=1
    )
    _set_deadlines(store, graph_id, planner=200)
    database = store.path
    store.close()

    context = prepare_expansion(
        database,
        home=home,
        raw=child_document(1),
        graph_id=graph_id,
        node_id="planner",
        request_id="request-1",
        actor=PLANNER,
        at=200,
        machine="machine",
        local_owner="test",
    )

    assert context.placeholder_node_id == "slot"


def test_earlier_planner_deadline_does_not_shorten_binding_cap(tmp_path):
    store, graph_id, _token, home, _work_root, _base_oid = parent_graph(
        tmp_path, child_nodes=1
    )
    _set_deadlines(store, graph_id, planner=1_000)
    database = store.path
    store.close()
    raw = child_document(1)
    context = prepare_expansion(
        database,
        home=home,
        raw=raw,
        graph_id=graph_id,
        node_id="planner",
        request_id="request-1",
        actor=PLANNER,
        at=200,
        machine="machine",
        local_owner="test",
    )

    write = PacGraphStore(database)
    try:
        PacReactor(
            write, clock=lambda: 200, expansion_context=context
        ).set_flag(
            graph_id,
            "planner",
            actor=PLANNER,
            reason_ref="evidence:planner-deadline-independent",
            expected_request="request-1",
            expansion=raw,
        )
        binding = write._db.execute(
            "SELECT deadline_ms FROM workflow_expansions "
            "WHERE parent_graph_id=? AND parent_node_id='slot'",
            (graph_id,),
        ).fetchone()
        assert binding["deadline_ms"] == 120_200
    finally:
        write.close()


class _PreviewApplication(_WorkflowSurfaceMixin):
    def __init__(self, database, home, context):
        self.state_dir = database.parent
        self.hyprial_home = home
        self.node_id = "machine"
        self.owner = "test"
        self._remote_workflow = None
        self._pac_graph_authority = SimpleNamespace(
            prepare_expansion=lambda **_facts: context
        )

    def _workflow_caller(self, _params):
        return PLANNER

    def _workflow_admit(self, spec, _source):
        return spec


def test_ipc_and_remote_previews_match_stored_binding_not_planner_deadline(
    tmp_path, monkeypatch
):
    store, graph_id, _token, home, _work_root, _base_oid = parent_graph(
        tmp_path, child_nodes=1
    )
    _set_deadlines(store, graph_id, planner=1_000_000)
    database = store.path
    store.close()
    raw = child_document(1)
    context = prepare_expansion(
        database,
        home=home,
        raw=raw,
        graph_id=graph_id,
        node_id="planner",
        request_id="request-1",
        actor=PLANNER,
        at=200,
        machine="machine",
        local_owner="test",
    )
    store = PacGraphStore(database)
    try:
        _set_deadlines(store, graph_id, planner=1_000)
    finally:
        store.close()
    app = _PreviewApplication(database, home, context)
    monkeypatch.setattr(time, "time_ns", lambda: 200_000_000)
    local_preview = app._ipc_workflow_expansion_plan(
        {"graphId": graph_id, "nodeId": "slot", "yaml": raw}
    )
    remote_preview = preview_origin_expansion(
        app,
        database,
        {
            "graphId": graph_id,
            "nodeId": "planner",
            "requestId": "request-1",
            "owner": PLANNER,
            "deadlineMs": 1_000,
        },
        {"nodeId": "slot", "yaml": raw},
    )

    write = PacGraphStore(database)
    try:
        PacReactor(
            write, clock=lambda: 200, expansion_context=context
        ).set_flag(
            graph_id,
            "planner",
            actor=PLANNER,
            reason_ref="evidence:deadline-boundary",
            expected_request="request-1",
            expansion=raw,
        )
        binding = write._db.execute(
            "SELECT deadline_ms FROM workflow_expansions "
            "WHERE parent_graph_id=? AND parent_node_id='slot'",
            (graph_id,),
        ).fetchone()
        assert local_preview["deadlineMs"] == 120_200
        assert remote_preview["deadlineMs"] == 120_200
        assert binding["deadline_ms"] == 120_200
    finally:
        write.close()


def test_explicit_cwd_execute_node_has_no_managed_worktree_plan(tmp_path):
    store, graph_id, _token, home, work_root, _base_oid = parent_graph(
        tmp_path, child_nodes=1
    )
    database = store.path
    store.close()
    explicit = work_root / "explicit"
    explicit.mkdir()
    raw = json.dumps(
        {
            "version": 2,
            "nodes": [
                {
                    "id": "work1",
                    "task": "use explicit cwd",
                    "role": "execute",
                    "launch": {"tier": "fast", "cwd": str(explicit)},
                    "timeout": "2m",
                }
            ],
        }
    )

    context = prepare_expansion(
        database,
        home=home,
        raw=raw,
        graph_id=graph_id,
        node_id="planner",
        request_id="request-1",
        actor=PLANNER,
        at=200,
        machine="machine",
        local_owner="test",
    )

    assert context.worktrees == ()
    assert context.spec.nodes[0].launch["cwd"] == str(explicit)
