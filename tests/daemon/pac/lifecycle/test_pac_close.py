"""First-wave close is owner-only, monotonic task/clock metadata, not actors."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from typer.main import get_command

import hyprial.daemon.impl.pac.graphs.edits as graph_module
from hyprial.daemon.impl.pac.views.context import node_context
from hyprial.identity import PacError
from hyprial.daemon.impl.pac.graphs.edits import activate_graph, add_edge, add_node, close_graph, create_graph
from hyprial.daemon.impl.pac.graphs.reactor import NullSender, PacReactor
from hyprial.daemon.impl.pac.storage.store import PacGraphStore, default_database_path
from hyprial.daemon.impl.pac.graphs.subscription import snapshot
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

OWNER = "user:hyprial-unit-owner"


@pytest.fixture()
def run_graph():
    state = Path(os.environ["HARNESS_STATE_DIR"])
    store = PacGraphStore(default_database_path(state))
    gid = create_graph(store, name="close", created_by=OWNER)["graphId"]
    version = 1
    for node in ("root", "left", "right", "clock"):
        version = add_node(store, state, graph_id=gid, node_id=node, owner=OWNER, brief_ref=f"ref#{node}",
                           kind="clock" if node == "clock" else "task", deadline_ms=1 if node == "clock" else None,
                           expect_version=version)["version"]
    for node in ("left", "right"):
        version = add_edge(store, graph_id=gid, from_node="root", to_node=node, expect_version=version)["version"]
    activate_graph(store, gid, actor=OWNER)
    yield store, gid
    store.close()


def test_draft_can_close_without_inventing_activation(run_graph):
    store, _gid = run_graph
    gid = create_graph(store, name="draft-close", created_by=OWNER)["graphId"]
    close_graph(store, gid, actor=OWNER)
    state = snapshot(store.path, gid)
    assert state["active"] is None and state["closed"]["by"] == OWNER
    assert state["assignments"] == [] and state["flags"] == {}
    assert [event["type"] for event in store.journal_events(gid)] == ["structure_changed", "graph_closed"]
    with pytest.raises(PacError) as error:
        activate_graph(store, gid, actor=OWNER)
    assert error.value.code == "PAC_GRAPH_CLOSED"


def test_close_is_idempotent_keeps_flags_and_invalidates_assignments(run_graph):
    store, gid = run_graph
    PacReactor(store).set_flag(gid, "root", actor=OWNER, reason_ref="ref#result")
    before = snapshot(store.path, gid)
    assert len(before["assignments"]) == 2
    result = close_graph(store, gid, actor=OWNER)
    assert close_graph(store, gid, actor=OWNER) == result
    after = snapshot(store.path, gid)
    assert after["closed"]["by"] == OWNER and after["closed"]["at"] is not None
    assert after["assignments"] == [] and after["flags"] == before["flags"]
    assert after["notifications"] == before["notifications"]
    assert after["version"] == before["version"]
    assert [e["type"] for e in store.journal_events(gid)].count("graph_closed") == 1
    context = node_context(store.path, gid, "left")
    assert context["completedCount"] == 0 and context["currentActivation"] is None
    assert context["closed"] == after["closed"] and "actor" not in context


def test_close_blocks_flags_reactivation_edits_and_outbox_resend(run_graph):
    store, gid = run_graph
    sender = NullSender()
    reactor = PacReactor(store, sender=sender)
    close_graph(store, gid, actor=OWNER)
    for operation in (
        lambda: reactor.set_flag(gid, "root", actor=OWNER),
        lambda: reactor.reset_flag(gid, "root", actor=OWNER),
        lambda: activate_graph(store, gid, actor=OWNER),
        lambda: reactor.resend_undelivered(gid),
    ):
        with pytest.raises(PacError) as error:
            operation()
        assert error.value.code == "PAC_GRAPH_CLOSED"
    with pytest.raises(PacError) as error:
        add_edge(store, graph_id=gid, from_node="left", to_node="right", expect_version=store.graph(gid)["version"])
    assert error.value.code == "PAC_GRAPH_FROZEN"
    assert reactor.tick_clocks(gid) == [] and sender.sent == []


def test_close_requires_owner_even_when_already_closed(run_graph):
    store, gid = run_graph
    before = snapshot(store.path, gid)
    with pytest.raises(PacError) as error:
        close_graph(store, gid, actor="user:not-owner")
    assert error.value.code == "PAC_GRAPH_NOT_OWNER" and snapshot(store.path, gid) == before
    close_graph(store, gid, actor=OWNER)
    with pytest.raises(PacError) as error:
        close_graph(store, gid, actor="user:not-owner")
    assert error.value.code == "PAC_GRAPH_NOT_OWNER"


def test_close_journal_failure_rolls_back_terminal_metadata(run_graph, monkeypatch):
    store, gid = run_graph
    before = snapshot(store.path, gid)
    original = graph_module.append_event

    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("close journal crash")

    monkeypatch.setattr(graph_module, "append_event", crash)
    with pytest.raises(RuntimeError, match="close journal crash"):
        close_graph(store, gid, actor=OWNER)
    assert snapshot(store.path, gid) == before


def test_two_real_connections_close_once(run_graph):
    store, gid = run_graph
    barrier = Barrier(2)

    def close():
        other = PacGraphStore(store.path)
        try:
            barrier.wait(timeout=5)
            return close_graph(other, gid, actor=OWNER)
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(close) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert results[0] == results[1]
    assert [e["type"] for e in store.journal_events(gid)].count("graph_closed") == 1


@pytest.mark.parametrize("mode", ["initial", "resend"])
def test_inflight_receipt_is_recorded_but_close_stops_remaining_sends(run_graph, mode):
    store, gid = run_graph

    class ClosingSender(NullSender):
        def send(self, **kwargs):
            receipt = super().send(**kwargs)
            close_graph(store, gid, actor=OWNER)
            return receipt

    sender = ClosingSender()
    reactor = PacReactor(store, sender=sender)
    if mode == "initial":
        outcome = reactor.set_flag(gid, "root", actor=OWNER)
        assert len(outcome.delivered) == 1 and len(outcome.undelivered) == 1
    else:
        PacReactor(store).set_flag(gid, "root", actor=OWNER)
        outcome = reactor.resend_undelivered(gid)
        assert outcome["delivered"] == 1 and outcome["undelivered"] == 1
    assert len(sender.sent) == 1
    state = snapshot(store.path, gid)
    assert state["closed"] is not None and state["assignments"] == []
    assert sum(n["messageId"] is not None for n in state["notifications"]) == 1
    assert state["flags"]["left"]["flag"] is False and state["flags"]["right"]["flag"] is False
    events = store.journal_events(gid)
    assert events[-2]["type"] == "graph_closed" and events[-1]["type"] == "delivery_changed"


def test_cliclean_public_names_and_close_cli(run_graph):
    _store, _gid = run_graph
    commands = get_command(_cli_root_app).commands
    assert "pac" not in commands
    workflow = commands["workflow"].commands
    assert {"events", "inspect", "reset", "worker", "notify", "migration"} <= set(workflow)
    assert "status" not in workflow and "context" not in workflow
