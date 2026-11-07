"""Deterministic expansion result and roster projections."""

from __future__ import annotations

from dataclasses import replace

import pytest

from hyprial.daemon.impl.pac.contracts.expansion.types import PreparedExpansion
from hyprial.daemon.impl.pac.contracts.roster import build_roster
from hyprial.daemon.impl.pac.graphs.reactor import NullSender, PacReactor
from hyprial.daemon.impl.pac.storage.store import PacGraphStore
from hyprial.daemon.impl.pac.workflows.expansion.transactions import child_graph_id
from hyprial.daemon.impl.pac.workflows.expansion.results import child_result_summary
from hyprial.daemon.impl.pac.workflows.graphs import (
    compile_workflow,
    input_token,
    prepare_workflow_artifacts,
)
from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService

from .support import AGENT, USER, node, specification


def test_system_placeholder_roster_has_no_actor_binding():
    placeholder, _ = node("slot", kind="expansion", owner=None, role="plan")
    roster, _, _ = build_roster(
        "parent",
        [placeholder],
        owned_bindings={},
        local_owner="test",
        local_machine="machine",
    )
    assert roster["nodes"] == [
        {
            "nodeId": "slot",
            "role": "plan",
            "owner": "agent:test:machine:pac-expansion",
            "ownership": "system",
        }
    ]


def test_child_result_summary_is_sorted_bounded_and_digest_bound():
    first = child_result_summary(
        [
            {"nodeId": "z", "state": "done", "outputText": "z" * 100},
            {"nodeId": "a", "state": "failed", "reasonRef": "evidence:a"},
        ],
        worktrees=[{"nodeId": "z", "branch": "pac/c/z", "path": "/managed/z", "effectiveCwd": "/managed/z"}],
        maximum_bytes=240,
    )
    second = child_result_summary(
        [
            {"nodeId": "a", "state": "failed", "reasonRef": "evidence:a"},
            {"nodeId": "z", "state": "done", "outputText": "z" * 100},
        ],
        worktrees=[{"nodeId": "z", "branch": "pac/c/z", "path": "/managed/z", "effectiveCwd": "/managed/z"}],
        maximum_bytes=240,
    )
    assert first == second
    assert first["nodes"][0]["nodeId"] == "a"
    assert len(first["digest"]) == 64
    assert len(first["text"].encode("utf-8")) <= 240


@pytest.mark.parametrize(
    (
        "case",
        "tick_at",
        "reason",
        "expected_child_state",
        "expected_child_reason",
        "expected_closed_at",
    ),
    [
        (
            "deadline",
            60_201,
            "pac:deadline-expired",
            "cancelled",
            "pac:deadline-expired",
            60_201,
        ),
        (
            "ancestor",
            300,
            "pac:ancestor-invalidated",
            "cancelled",
            "pac:ancestor-invalidated",
            300,
        ),
        (
            "ancestor-completed",
            300,
            "pac:ancestor-invalidated",
            "completed",
            "evidence:child-completed",
            250,
        ),
    ],
)
def test_child_invalidation_closes_cancelled_in_same_tick(
    tmp_path,
    case,
    tick_at,
    reason,
    expected_child_state,
    expected_child_reason,
    expected_closed_at,
):
    state = tmp_path / "state"
    state.mkdir()
    graph_store = PacGraphStore(state / "pac-graph.sqlite3")
    planner, _ = node("planner", role="plan")
    planner = replace(planner, expands="slot")
    placeholder, edges = node(
        "slot", kind="expansion", owner=None, role="plan", after=("planner",)
    )
    keep_open, _ = node("keep-open")
    parent = specification(
        "parent", (planner, ()), (placeholder, edges), (keep_open, ())
    )
    parent_id = compile_workflow(
        graph_store,
        parent,
        sender=USER,
        machine="machine",
        local_owner="test",
        operation_key="parent",
        at=100,
    )
    token = input_token(graph_store, parent_id, "planner")
    assert token is not None
    with graph_store.write() as db:
        db.execute(
            "UPDATE workflow_nodes SET state='requested',request_id='request-1',"
            "input_token=?,generation=1,deadline_ms=1000000 "
            "WHERE graph_id=? AND node_id='planner'",
            (token, parent_id),
        )
    child = specification("child", node("work"))
    child_id = child_graph_id(parent_id, "slot")
    context = PreparedExpansion(
        parent_graph_id=parent_id,
        planner_node_id="planner",
        planner_request_id="request-1",
        placeholder_node_id="slot",
        child_graph_id=child_id,
        expansion_digest="d" * 64,
        captured_input_token=token,
        effective_limits={"maxNodes": 1},
        policy_snapshot={"version": 1},
        spec=child,
        prepared_artifacts=prepare_workflow_artifacts(graph_store, child),
        worktrees=(),
    )
    PacReactor(graph_store, clock=lambda: 200, expansion_context=context).set_flag(
        parent_id,
        "planner",
        actor=AGENT,
        expected_request="request-1",
        reason_ref="evidence:child-invalidation",
        expansion="version: 2",
    )
    if case.startswith("ancestor"):
        with graph_store.write() as db:
            if case == "ancestor-completed":
                db.execute(
                    "UPDATE workflow_graphs SET state='completed',"
                    "reason_ref='evidence:child-completed' WHERE graph_id=?",
                    (child_id,),
                )
                db.execute(
                    "UPDATE graphs SET closed_at=250,closed_by='worker' "
                    "WHERE graph_id=?",
                    (child_id,),
                )
                db.execute(
                    "UPDATE workflow_nodes SET state='done' WHERE graph_id=?",
                    (child_id,),
                )
            db.execute(
                "UPDATE workflow_nodes SET input_token='stale' "
                "WHERE graph_id=? AND node_id='planner'",
                (parent_id,),
            )
    graph_store.close()

    service = GraphWorkflowService(
        state_dir=state,
        owner="test",
        machine="machine",
        sender=NullSender(),
        start_thread=False,
    )
    try:
        service._tick(tick_at)
        read = PacGraphStore(service.database, read_only=True)
        try:
            graph = read.graph(child_id)
            meta = read._db.execute(
                "SELECT state,reason_ref FROM workflow_graphs WHERE graph_id=?",
                (child_id,),
            ).fetchone()
            closed = [
                event
                for event in read.journal_events(child_id)
                if event["type"] == "graph_closed"
            ]
            assert graph["closed_at"] == expected_closed_at
            assert tuple(meta) == (expected_child_state, expected_child_reason)
            if expected_child_state == "cancelled":
                assert closed[-1]["data"]["state"] == "cancelled"
            else:
                assert not closed
            placeholder = read._db.execute(
                "SELECT state,reason_ref FROM workflow_nodes "
                "WHERE graph_id=? AND node_id='slot'",
                (parent_id,),
            ).fetchone()
            assert tuple(placeholder) == ("failed", reason)
        finally:
            read.close()
    finally:
        assert service.close()
