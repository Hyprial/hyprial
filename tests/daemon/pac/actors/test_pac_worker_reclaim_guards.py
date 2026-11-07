"""Safety edges of convergent PAC worker cleanup.

Review of fix/pac-worker-reclaim (2026-10-18) showed that two widenings of
``_own_incarnation`` survived the whole ``tests/daemon/pac`` directory: a
strict marker naming another graph or node, and an unmarked process whose
entity token matches a NULL receipt token.  These tests pin both edges on
the real graph authority and SQLite store; a launch that reports failure
leaves the receipt without a token, which is the shape that skips the
entity-token gate.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hyprial.daemon.impl.pac.actors.coordinator import RuntimeObservation
from tests.daemon.pac.actors.test_pac_worker_reclaim import (
    USER,
    _activation,
    _intent,
    _managed,
    _read,
    _state_is,
    _wait_for,
)


def _launch_failed(authority, runtime, graph_id, actor) -> None:
    runtime.start_error = RuntimeError("ROUTE_IO_FAILED: compensated")
    assert authority.reconcile_actor(graph_id, actor.node_id).value == "accepted"
    _wait_for(lambda: (
        _activation(authority.database, graph_id, actor.node_id) or {"op": None}
    )["op"] == "done")
    runtime.start_error = None


def _settled_attention(authority, runtime, graph_id, actor, reason: str) -> None:
    for _ in range(2):
        assert authority.reconcile_actor(graph_id, actor.node_id).value == "accepted"
        _wait_for(_state_is(authority.database, graph_id, "attention", reason))
    time.sleep(0.1)
    assert _intent(authority.database, graph_id)["state"] == "attention"
    assert runtime.stops == []
    receipt = _read(
        authority.database, lambda s: s.workflow_worker_receipts(graph_id)
    )[0]
    assert receipt["agent_entity_token"] is None
    assert receipt["state"] == "planned"


@pytest.mark.parametrize("foreign", ["graph", "node"])
def test_strict_marker_of_another_graph_or_node_fails_closed(
    tmp_path: Path, foreign: str
) -> None:
    authority, service, runtime, graph_id, actor = _managed(tmp_path)
    try:
        _launch_failed(authority, runtime, graph_id, actor)
        marker = (
            f"pac:wf-{'0' * 32}:{actor.node_id}:1:effect"
            if foreign == "graph"
            else f"pac:{graph_id}:_actor.someone-else:1:effect"
        )
        runtime.running[actor.actor_name] = RuntimeObservation(
            True, marker, "pi", None, agent_entity_token="entity-foreign",
        )
        service.cancel(run_id=graph_id, actor=USER)
        _settled_attention(authority, runtime, graph_id, actor, "marker_mismatch")
        assert runtime.running[actor.actor_name].identity_marker == marker
    finally:
        assert service.close()
        assert authority.close(3.0)


def test_unmarked_process_without_a_receipt_token_fails_closed(
    tmp_path: Path,
) -> None:
    authority, service, runtime, graph_id, actor = _managed(tmp_path)
    try:
        _launch_failed(authority, runtime, graph_id, actor)
        runtime.running[actor.actor_name] = RuntimeObservation(
            True, None, "pi", None, agent_entity_token=None,
        )
        service.cancel(run_id=graph_id, actor=USER)
        _settled_attention(authority, runtime, graph_id, actor, "unknown_identity")
        assert actor.actor_name in runtime.running
    finally:
        assert service.close()
        assert authority.close(3.0)


def test_a_marker_this_node_never_issued_lends_no_token(tmp_path: Path) -> None:
    authority, service, runtime, graph_id, actor = _managed(tmp_path)
    try:
        _launch_failed(authority, runtime, graph_id, actor)
        forged = f"pac:{graph_id}:{actor.node_id}:7:never-issued"
        runtime.running[actor.actor_name] = RuntimeObservation(
            True, forged, "pi", None, agent_entity_token="entity-forged",
        )
        service.cancel(run_id=graph_id, actor=USER)
        assert authority.reconcile_actor(graph_id, actor.node_id).value == "accepted"
        _wait_for(_state_is(authority.database, graph_id, "complete"))

        assert actor.actor_name not in runtime.running
        receipt = _read(
            authority.database, lambda s: s.workflow_worker_receipts(graph_id)
        )[0]
        assert receipt["agent_entity_token"] is None
    finally:
        assert service.close()
        assert authority.close(3.0)


def test_a_marker_with_an_unissued_effect_lends_no_token(tmp_path: Path) -> None:
    authority, service, runtime, graph_id, actor = _managed(tmp_path)
    try:
        _launch_failed(authority, runtime, graph_id, actor)
        issued = runtime.running[actor.actor_name].identity_marker
        head, _effect = issued.rsplit(":", 1)
        forged = f"{head}:not-the-issued-effect"
        runtime.running[actor.actor_name] = RuntimeObservation(
            True, forged, "pi", None, agent_entity_token="entity-forged",
        )
        service.cancel(run_id=graph_id, actor=USER)
        assert authority.reconcile_actor(graph_id, actor.node_id).value == "accepted"
        _wait_for(_state_is(authority.database, graph_id, "complete"))
        receipt = _read(
            authority.database, lambda s: s.workflow_worker_receipts(graph_id)
        )[0]
        assert receipt["agent_entity_token"] is None
    finally:
        assert service.close()
        assert authority.close(3.0)
