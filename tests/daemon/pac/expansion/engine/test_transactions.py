"""Caller-owned compilation and atomic binding tests."""

from __future__ import annotations

from dataclasses import replace
import sqlite3

import pytest

from hyprial.daemon.impl.pac.contracts.expansion.types import PreparedExpansion
from hyprial.daemon.impl.pac.graphs.reactor import PacReactor
from hyprial.daemon.impl.pac.workflows.graphs import (
    compile_workflow,
    compile_workflow_in_transaction,
    input_token,
    prepare_workflow_artifacts,
)
from hyprial.daemon.impl.pac.workflows.expansion.transactions import child_graph_id
from hyprial.identity import PacError

from .support import AGENT, USER, node, specification, store


def test_compiler_requires_and_preserves_caller_transaction(tmp_path):
    graph_store = store(tmp_path)
    spec = specification("child", node("work"))
    artifacts = prepare_workflow_artifacts(graph_store, spec)
    try:
        with pytest.raises(RuntimeError, match="caller transaction"):
            compile_workflow_in_transaction(
                graph_store,
                spec,
                sender=USER,
                machine="machine",
                local_owner="test",
                operation_key="child",
                at=100,
                prepared_artifacts=artifacts,
            )
        db = graph_store.write()
        graph_id = compile_workflow_in_transaction(
            graph_store,
            spec,
            sender=USER,
            machine="machine",
            local_owner="test",
            operation_key="child",
            at=100,
            prepared_artifacts=artifacts,
            graph_id="wf-fixed",
        )
        assert graph_id == "wf-fixed"
        assert db.in_transaction
        db.rollback()
        assert graph_store.graph("wf-fixed") is None
    finally:
        graph_store.close()


def test_child_compiler_persists_parent_identity_and_node_kind(tmp_path):
    graph_store = store(tmp_path)
    expansion, _ = node("slot", kind="expansion", owner=None)
    parent = specification("parent", (expansion, ()))
    parent_artifacts = prepare_workflow_artifacts(graph_store, parent)
    child = specification("child", node("work"))
    child_artifacts = prepare_workflow_artifacts(graph_store, child)
    try:
        with graph_store.write():
            compile_workflow_in_transaction(
                graph_store,
                parent,
                sender=USER,
                machine="machine",
                local_owner="test",
                operation_key="parent",
                at=100,
                prepared_artifacts=parent_artifacts,
                graph_id="parent",
            )
        with graph_store.write():
            compile_workflow_in_transaction(
                graph_store,
                child,
                sender=USER,
                machine="machine",
                local_owner="test",
                operation_key="expand:parent:slot:1",
                at=200,
                prepared_artifacts=child_artifacts,
                graph_id="child",
                parent_graph_id="parent",
                parent_node_id="slot",
                expansion_deadline_ms=1000,
            )
        parent_kind = graph_store._db.execute("SELECT node_kind FROM workflow_nodes WHERE graph_id='parent' AND node_id='slot'").fetchone()[0]
        child_row = graph_store._db.execute("SELECT parent_graph_id,parent_node_id,expansion_deadline_ms FROM workflow_graphs WHERE graph_id='child'").fetchone()
        assert parent_kind == "expansion"
        assert tuple(child_row) == ("parent", "slot", 1000)
    finally:
        graph_store.close()


def _prepared_parent_and_child(
    graph_store,
    *,
    sender=USER,
    local_owner="test",
    machine="machine",
    child_nodes=None,
):
    planner, _ = node("planner", role="plan")
    planner = replace(planner, expands="slot")
    placeholder, edges = node(
        "slot", kind="expansion", owner=None, role="plan", after=("planner",)
    )
    parent = specification("parent", (planner, ()), (placeholder, edges))
    parent_id = compile_workflow(
        graph_store,
        parent,
        sender=sender,
        machine=machine,
        local_owner=local_owner,
        operation_key="parent",
        at=100,
    )
    token = input_token(graph_store, parent_id, "planner")
    assert token is not None
    with graph_store.write() as db:
        db.execute(
            "UPDATE workflow_nodes SET state='requested',request_id='request-1',"
            "input_token=?,generation=1,deadline_ms=1000 "
            "WHERE graph_id=? AND node_id='planner'",
            (token, parent_id),
        )
    child = specification("child", *(child_nodes or (node("work"),)))
    context = PreparedExpansion(
        parent_graph_id=parent_id,
        planner_node_id="planner",
        planner_request_id="request-1",
        placeholder_node_id="slot",
        child_graph_id=child_graph_id(parent_id, "slot"),
        expansion_digest="d" * 64,
        captured_input_token=token,
        effective_limits={"maxNodes": 1},
        policy_snapshot={"version": 1},
        spec=child,
        prepared_artifacts=prepare_workflow_artifacts(graph_store, child),
        worktrees=(),
    )
    return parent_id, context


def test_agent_created_parent_expands_with_managed_worker_and_expansion(tmp_path):
    graph_store = store(tmp_path)
    try:
        parent_id, context = _prepared_parent_and_child(
            graph_store,
            sender="agent:test:origin:planner",
            local_owner="test",
            machine="origin",
            child_nodes=(
                node("work", worker="builder"),
                node("child-slot", kind="expansion", owner=None, role="plan"),
            ),
        )
        reactor = PacReactor(
            graph_store, clock=lambda: 200, expansion_context=context
        )
        reactor.set_flag(
            parent_id,
            "planner",
            actor="agent:test:actor-machine:planner",
            expected_request="request-1",
            reason_ref="evidence:plan",
            expansion="version: 2",
        )
        child_owner = graph_store._db.execute(
            "SELECT owner FROM nodes WHERE graph_id=? AND node_id='child-slot'",
            (context.child_graph_id,),
        ).fetchone()[0]
        assert child_owner == "agent:test:origin:pac-expansion"
    finally:
        graph_store.close()


def test_user_email_created_parent_expansion_behavior_is_unchanged(tmp_path):
    graph_store = store(tmp_path)
    try:
        parent_id, context = _prepared_parent_and_child(
            graph_store,
            sender="user:alice@example.com",
            local_owner="alice@example.com",
            machine="parent-machine",
            child_nodes=(
                node("work", worker="builder"),
                node("child-slot", kind="expansion", owner=None, role="plan"),
            ),
        )
        parent_owner = graph_store._db.execute(
            "SELECT owner FROM nodes WHERE graph_id=? AND node_id='slot'",
            (parent_id,),
        ).fetchone()[0]
        assert parent_owner == "agent:alice@example.com:parent-machine:pac-expansion"
        reactor = PacReactor(
            graph_store, clock=lambda: 200, expansion_context=context
        )
        reactor.set_flag(
            parent_id,
            "planner",
            actor="agent:alice@example.com:worker-machine:planner",
            expected_request="request-1",
            reason_ref="evidence:plan",
            expansion="version: 2",
        )
        child_owner = graph_store._db.execute(
            "SELECT owner FROM nodes WHERE graph_id=? AND node_id='child-slot'",
            (context.child_graph_id,),
        ).fetchone()[0]
        assert child_owner == "agent:alice@example.com:worker-machine:pac-expansion"
    finally:
        graph_store.close()


@pytest.mark.parametrize(
    "created_by",
    (
        "agent:test:machine",
        "agent:test:machine:planner:extra",
        "agent:test::planner",
        "agent::machine:planner",
        "user:",
    ),
)
def test_malformed_created_by_still_fails_closed(tmp_path, created_by):
    graph_store = store(tmp_path)
    try:
        parent_id, context = _prepared_parent_and_child(
            graph_store,
            child_nodes=(node("work", worker="builder"),),
        )
        with graph_store.write() as db:
            db.execute(
                "UPDATE graphs SET created_by=? WHERE graph_id=?",
                (created_by, parent_id),
            )
        reactor = PacReactor(
            graph_store, clock=lambda: 200, expansion_context=context
        )
        with pytest.raises((ValueError, PacError)):
            reactor.set_flag(
                parent_id,
                "planner",
                actor=AGENT,
                expected_request="request-1",
                reason_ref="evidence:plan",
                expansion="version: 2",
            )
        assert graph_store.graph(context.child_graph_id) is None
        assert graph_store.node(parent_id, "planner").flag is False
        assert graph_store._db.execute(
            "SELECT COUNT(*) FROM workflow_expansions"
        ).fetchone()[0] == 0
    finally:
        graph_store.close()


def test_expansion_publication_and_accepted_replay_are_atomic(tmp_path):
    graph_store = store(tmp_path)
    try:
        parent_id, context = _prepared_parent_and_child(graph_store)
        reactor = PacReactor(
            graph_store, clock=lambda: 200, expansion_context=context
        )
        outcome = reactor.set_flag(
            parent_id,
            "planner",
            actor=AGENT,
            expected_request="request-1",
            reason_ref="evidence:plan",
            output_text="planned",
            expansion="version: 2",
        )
        assert graph_store.node(parent_id, "planner").flag is True
        assert graph_store.graph(context.child_graph_id) is not None
        binding = graph_store._db.execute(
            "SELECT * FROM workflow_expansions WHERE parent_graph_id=?",
            (parent_id,),
        ).fetchone()
        assert binding["child_graph_id"] == context.child_graph_id
        placeholder = graph_store._db.execute(
            "SELECT state,generation,deadline_ms FROM workflow_nodes "
            "WHERE graph_id=? AND node_id='slot'",
            (parent_id,),
        ).fetchone()
        assert tuple(placeholder) == ("requested", 1, 60_200)
        graph_store._db.execute(
            "UPDATE graphs SET closed_at=300,closed_by='test' WHERE graph_id=?",
            (parent_id,),
        )
        replay = reactor.set_flag(
            parent_id,
            "planner",
            actor=AGENT,
            expected_request="request-1",
            reason_ref="evidence:plan",
            output_text="planned",
            expansion="version: 2",
        )
        assert replay.event == outcome.event
        with pytest.raises(PacError, match="different accepted outcome"):
            reactor.set_flag(
                parent_id,
                "planner",
                actor=AGENT,
                expected_request="request-1",
                reason_ref="evidence:changed",
                output_text="planned",
                expansion="version: 2",
            )
    finally:
        graph_store.close()


@pytest.mark.parametrize(
    "stage",
    (
        "compile",
        "binding",
        "worktrees",
        "flag",
        "placeholder",
        "notification",
        "receipt",
    ),
)
def test_fault_at_each_publication_stage_rolls_back_every_fact(
    tmp_path, monkeypatch, stage
):
    graph_store = store(tmp_path)
    try:
        parent_id, context = _prepared_parent_and_child(graph_store)
        import hyprial.daemon.impl.pac.workflows.expansion.transactions as tx

        if stage in {"compile", "binding", "worktrees", "flag"}:
            def crash(*args, **kwargs):
                raise RuntimeError(f"{stage} crash")

            monkeypatch.setattr(
                tx,
                {
                    "compile": "compile_workflow_in_transaction",
                    "binding": "insert_binding",
                    "worktrees": "insert_worktree_plans",
                    "flag": "write_flag_in_transaction",
                }[stage],
                crash,
            )
        else:
            table, when = {
                "placeholder": (
                    "workflow_nodes",
                    "NEW.node_id='slot' AND NEW.state='requested'",
                ),
                "notification": ("notifications", "NEW.kind='actor_alert'"),
                "receipt": ("workflow_outcome_receipts", "1"),
            }[stage]
            operation = "UPDATE" if stage == "placeholder" else "INSERT"
            graph_store._db.execute(
                f"CREATE TEMP TRIGGER fail_{stage} BEFORE {operation} ON {table} "
                f"WHEN {when} BEGIN SELECT RAISE(ABORT, '{stage} crash'); END"
            )
        reactor = PacReactor(
            graph_store, clock=lambda: 200, expansion_context=context
        )
        with pytest.raises(
            (RuntimeError, sqlite3.IntegrityError), match=f"{stage} crash"
        ):
            reactor.set_flag(
                parent_id,
                "planner",
                actor=AGENT,
                expected_request="request-1",
                reason_ref="evidence:plan",
                expansion="version: 2",
            )
        assert graph_store.graph(context.child_graph_id) is None
        assert graph_store.node(parent_id, "planner").flag is False
        assert graph_store._db.execute(
            "SELECT COUNT(*) FROM workflow_expansions"
        ).fetchone()[0] == 0
        assert graph_store._db.execute(
            "SELECT COUNT(*) FROM workflow_outcome_receipts"
        ).fetchone()[0] == 0
    finally:
        graph_store.close()
