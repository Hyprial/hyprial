"""PAC v2 P0: the graph file + editing CLI semantics (spec ①, phases P0).

Covers the phases-P0 acceptance list: CAS versioning (sequential and
genuinely concurrent), the three edit-time rejections (undeclared back
edge == forward cycle, unknown owner, forward cycle), owner stored as a
short name, brief_ref as a bounded reference, and strict file separation
from v1's ``workflows.sqlite3``.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from hyprial.identity import (
    PAC_EDGE_EXISTS,
    PAC_EDGE_INVALID,
    PAC_GRAPH_FORWARD_CYCLE,
    PAC_GRAPH_NOT_FOUND,
    PAC_GRAPH_VERSION_CONFLICT,
    PAC_NODE_EXISTS,
    PAC_NODE_NOT_FOUND,
    PAC_NODE_SHAPE_INVALID,
    PAC_OPERATION_KEY_CONFLICT,
    PAC_PRINCIPAL_SHAPE_INVALID,
    PacError,
)
from hyprial.daemon.impl.pac.graphs.edits import add_edge, add_node, create_graph, show_graph
from hyprial.daemon.impl.pac.storage.store import PacGraphStore, default_database_path
from tests.support.pac.builders import ALLEN_BACK, ALLEN_FORWARD, ALLEN_OWNERS, build_allen_graph

__all__ = ["ALLEN_OWNERS", "build_allen_graph"]


@pytest.fixture()
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HARNESS_STATE_DIR", raising=False)
    (tmp_path / "home").mkdir(exist_ok=True)
    return state


# --------------------------------------------------------------------------- #
# acceptance 1: create/add-node/add-edge write the tables; show restates
# --------------------------------------------------------------------------- #


def test_create_then_edits_then_show_restates_the_whole_graph(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HYPRIAL_OWNER", "allen")
    store, graph_id = build_allen_graph(state_dir)
    try:
        raw = sqlite3.connect(default_database_path(state_dir))
        graph_row = raw.execute(
            "SELECT version FROM graphs WHERE graph_id = ?", (graph_id,)
        ).fetchone()
        assert graph_row is not None and graph_row[0] == 1 + 13 + 12  # create + nodes + edges
        node_count = raw.execute(
            "SELECT COUNT(*) FROM nodes WHERE graph_id = ?", (graph_id,)
        ).fetchone()[0]
        edge_count = raw.execute(
            "SELECT COUNT(*) FROM edges WHERE graph_id = ?", (graph_id,)
        ).fetchone()[0]
        raw.close()
        assert node_count == 13
        assert edge_count == 12

        document = show_graph(store, graph_id)
        assert document["name"] == "allen-example"
        assert {node["nodeId"] for node in document["nodes"]} == set(ALLEN_OWNERS)
        back_edges = {
            (edge["from"], edge["to"]) for edge in document["edges"] if edge["kind"] == "back"
        }
        forward_edges = {
            (edge["from"], edge["to"])
            for edge in document["edges"]
            if edge["kind"] == "forward"
        }
        assert back_edges == set(ALLEN_BACK)
        assert forward_edges == set(ALLEN_FORWARD)
        # Ruling 丙: pm's two confirmations are TWO nodes (same owner,
        # distinct briefs); each has exactly ONE forward in-edge, and the
        # forward subgraph is a pure DAG.
        confirm_1 = [
            edge for edge in document["edges"] if edge["to"] == "pm-confirm-1"
        ]
        confirm_2 = [
            edge for edge in document["edges"] if edge["to"] == "pm-confirm-2"
        ]
        assert confirm_1 == [{"from": "pm-helper", "to": "pm-confirm-1", "kind": "forward"}]
        assert confirm_2 == [{"from": "demo", "to": "pm-confirm-2", "kind": "forward"}]
        owners = {node["nodeId"]: node["owner"] for node in document["nodes"]}
        assert owners["pm-confirm-1"] == owners["pm-confirm-2"] == "user:pm"
        assert owners["pm-不通过"] == "user:pm"
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# acceptance 2: CAS — sequential and concurrent
# --------------------------------------------------------------------------- #


def test_cas_conflict_refuses_a_stale_edit(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HYPRIAL_OWNER", "allen")
    store = PacGraphStore(default_database_path(state_dir))
    try:
        head = create_graph(store, name="cas", created_by="user:allen")
        graph_id = head["graphId"]
        add_node(
            store,
            state_dir,
            graph_id=graph_id,
            node_id="a",
            owner="user:allen",
            brief_ref="r#a",
            expect_version=1,
        )
        with pytest.raises(PacError) as raised:
            add_node(
                store,
                state_dir,
                graph_id=graph_id,
                node_id="b",
                owner="user:allen",
                brief_ref="r#b",
                expect_version=1,  # stale: the graph is at 2 now
            )
        assert raised.value.code == PAC_GRAPH_VERSION_CONFLICT
        assert raised.value.data["current"] == 2
        # nothing was written for the refused edit
        assert store.node(graph_id, "b") is None
        assert store.graph(graph_id)["version"] == 2
    finally:
        store.close()


def test_stale_editor_after_commit_gets_version_conflict_for_add_node(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HYPRIAL_OWNER", "allen")
    store = PacGraphStore(default_database_path(state_dir))
    try:
        graph_id = create_graph(store, name="stale-node", created_by="user:allen")["graphId"]
        add_node(
            store,
            state_dir,
            graph_id=graph_id,
            node_id="x",
            owner="user:allen",
            brief_ref="r#x",
            expect_version=1,
        )
        with pytest.raises(PacError) as raised:
            add_node(
                store,
                state_dir,
                graph_id=graph_id,
                node_id="x",
                owner="user:allen",
                brief_ref="r#x",
                expect_version=1,
            )
        assert raised.value.code == PAC_GRAPH_VERSION_CONFLICT
    finally:
        store.close()


def test_stale_editor_after_commit_gets_version_conflict_for_add_edge(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HYPRIAL_OWNER", "allen")
    store = PacGraphStore(default_database_path(state_dir))
    try:
        graph_id = create_graph(store, name="stale-edge", created_by="user:allen")["graphId"]
        for node_id, expect_version in (("a", 1), ("b", 2)):
            add_node(
                store,
                state_dir,
                graph_id=graph_id,
                node_id=node_id,
                owner="user:allen",
                brief_ref=f"r#{node_id}",
                expect_version=expect_version,
            )
        add_edge(
            store,
            graph_id=graph_id,
            from_node="a",
            to_node="b",
            expect_version=3,
        )
        with pytest.raises(PacError) as raised:
            add_edge(
                store,
                graph_id=graph_id,
                from_node="a",
                to_node="b",
                expect_version=3,
            )
        assert raised.value.code == PAC_GRAPH_VERSION_CONFLICT
    finally:
        store.close()


def test_concurrent_same_expect_version_exactly_one_wins(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HYPRIAL_OWNER", "allen")
    store = PacGraphStore(default_database_path(state_dir))
    try:
        head = create_graph(store, name="race", created_by="user:allen")
        graph_id = head["graphId"]
        for node_id in ("a", "b"):
            add_node(
                store,
                state_dir,
                graph_id=graph_id,
                node_id=node_id,
                owner="user:allen",
                brief_ref=f"r#{node_id}",
                expect_version=1 if node_id == "a" else 2,
            )

        outcomes: list[str] = []
        barrier = threading.Barrier(4)

        def editor(name: str) -> None:
            local = PacGraphStore(default_database_path(state_dir))
            try:
                barrier.wait()
                add_edge(
                    local,
                    graph_id=graph_id,
                    from_node="a",
                    to_node="b",
                    kind="forward",
                    expect_version=3,
                )
                outcomes.append(f"ok:{name}")
            except PacError as error:
                outcomes.append(f"{error.code}:{name}")
            except Exception as error:  # noqa: BLE001 - a dying thread would hide its outcome
                outcomes.append(f"{type(error).__name__}:{name}:{error}")
            finally:
                local.close()

        threads = [threading.Thread(target=editor, args=(str(i),)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        # CI (2026-09-23, 6 PR runs + one dev push) went red at the second
        # assertion with exactly one winner but ZERO version conflicts: the
        # three losers failed with some other code, which the bare assertion
        # never showed.  Name every outcome so the red says what they got.
        assert len([outcome for outcome in outcomes if outcome.startswith("ok:")]) == 1, outcomes
        assert (
            len(
                [
                    outcome
                    for outcome in outcomes
                    if outcome.startswith("PAC_GRAPH_VERSION_CONFLICT")
                ]
            )
            == 3
        ), outcomes
        assert store.graph(graph_id)["version"] == 4
    finally:
        store.close()


def test_operation_key_replay_survives_store_restart(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HYPRIAL_OWNER", "allen")
    path = default_database_path(state_dir)
    store = PacGraphStore(path)
    first = create_graph(
        store,
        name="first-name",
        created_by="user:allen",
        operation_key="routine:daily:task-123",
    )
    add_node(
        store,
        state_dir,
        graph_id=first["graphId"],
        node_id="work",
        owner="user:allen",
        brief_ref="r#work",
        expect_version=1,
    )
    store.close()

    reopened = PacGraphStore(path)
    try:
        replay = create_graph(
            reopened,
            name="ignored-replay-name",
            created_by="user:allen",
            operation_key="routine:daily:task-123",
        )
        assert replay == {
            "graphId": first["graphId"],
            "name": "first-name",
            "version": 2,
            "operationKey": "routine:daily:task-123",
        }
        assert reopened.graph_ids() == [first["graphId"]]
        assert [
            event
            for event in reopened.journal_events(first["graphId"])
            if event["type"] == "structure_changed"
            and event["data"]["operation"] == "create"
        ] == [reopened.journal_events(first["graphId"])[0]]
    finally:
        reopened.close()


def test_operation_key_replay_with_a_different_principal_is_refused(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Design §6 tail: the durable key arbitrates replays, but a replay is
    only the same request when its creating principal matches -- a different
    ``--by`` under a known key is a structured refusal, never a silent
    re-bind and never a second graph."""

    monkeypatch.setenv("HYPRIAL_OWNER", "allen")
    store = PacGraphStore(default_database_path(state_dir))
    try:
        first = create_graph(
            store,
            name="bound",
            created_by="agent:allen:test-node:kanban",
            operation_key="routine:daily:task-123",
        )
        with pytest.raises(PacError) as raised:
            create_graph(
                store,
                name="bound",
                created_by="agent:allen:test-node:other",
                operation_key="routine:daily:task-123",
            )
        assert raised.value.code == PAC_OPERATION_KEY_CONFLICT
        assert raised.value.data == {
            "graphId": first["graphId"],
            "createdBy": "agent:allen:test-node:kanban",
            "presentedBy": "agent:allen:test-node:other",
        }
        # the refusal wrote nothing: one graph, still the original owner
        assert store.graph_ids() == [first["graphId"]]
        assert store.graph(first["graphId"])["created_by"] == "agent:allen:test-node:kanban"
    finally:
        store.close()


def test_concurrent_same_operation_key_returns_one_durable_graph(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HYPRIAL_OWNER", "allen")
    path = default_database_path(state_dir)
    PacGraphStore(path).close()  # migrate before the concurrent open race
    barrier = threading.Barrier(4)
    heads: list[dict[str, object]] = []
    failures: list[BaseException] = []

    def creator(index: int) -> None:
        local = PacGraphStore(path)
        try:
            barrier.wait(timeout=5)
            heads.append(
                create_graph(
                    local,
                    name=f"concurrent-{index}",
                    created_by="user:routine-owner",
                    operation_key="routine:hourly:task-456",
                )
            )
        except BaseException as error:  # surfaced below, never lost in a thread
            failures.append(error)
        finally:
            local.close()

    threads = [threading.Thread(target=creator, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not failures
    assert len(heads) == 4
    assert len({head["graphId"] for head in heads}) == 1
    assert all(head["operationKey"] == "routine:hourly:task-456" for head in heads)

    store = PacGraphStore(path)
    try:
        assert len(store.graph_ids()) == 1
        graph_id = store.graph_ids()[0]
        assert len(store.journal_events(graph_id)) == 1
        with pytest.raises(sqlite3.IntegrityError):
            store._db.execute(
                "INSERT INTO graphs "
                "(graph_id,name,version,created_by,created_at,operation_key) "
                "VALUES ('duplicate','duplicate',1,'allen',1,?)",
                ("routine:hourly:task-456",),
            )
    finally:
        store._db.rollback()
        store.close()


# --------------------------------------------------------------------------- #
# acceptance 3: the three edit-time rejections, nothing persisted
# --------------------------------------------------------------------------- #


def test_forward_edge_closing_a_loop_is_rejected(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A back edge that forgot to declare itself is a forward cycle — the
    exact shape spec ① rejects (环只由 back 边表达)."""
    monkeypatch.setenv("HYPRIAL_OWNER", "allen")
    store = PacGraphStore(default_database_path(state_dir))
    try:
        graph_id = create_graph(store, name="cycle", created_by="user:allen")["graphId"]
        add_node(store, state_dir, graph_id=graph_id, node_id="a", owner="user:allen", brief_ref="r", expect_version=1)
        add_node(store, state_dir, graph_id=graph_id, node_id="b", owner="user:allen", brief_ref="r", expect_version=2)
        add_edge(store, graph_id=graph_id, from_node="a", to_node="b", kind="forward", expect_version=3)
        with pytest.raises(PacError) as raised:
            add_edge(  # a->b exists; b->a would close the loop
                store,
                graph_id=graph_id,
                from_node="b",
                to_node="a",
                kind="forward",  # forgot to declare back
                expect_version=4,
            )
        assert raised.value.code == PAC_GRAPH_FORWARD_CYCLE
        assert store.graph(graph_id)["version"] == 4
        assert store.edges(graph_id) and all(
            edge.kind == "forward" for edge in store.edges(graph_id)
        )
        # declared as back, the same edge is accepted: loops exist only as back edges
        add_edge(store, graph_id=graph_id, from_node="b", to_node="a", kind="back", expect_version=4)
        assert store.graph(graph_id)["version"] == 5
    finally:
        store.close()


def test_forward_cycle_is_rejected_even_through_a_convergence_node(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling 丙 2026-09-07: forward cycles are refused period — including
    one that closes through a convergence node.  A node visited in two
    rounds is two nodes, never a forward re-entry edge."""

    monkeypatch.setenv("HYPRIAL_OWNER", "allen")
    store = PacGraphStore(default_database_path(state_dir))
    try:
        graph_id = create_graph(store, name="convergence-cycle", created_by="user:allen")["graphId"]
        version = 1
        for node_id in ("s", "d", "c"):
            version = add_node(
                store, state_dir, graph_id=graph_id, node_id=node_id,
                owner="user:allen", brief_ref="r", expect_version=version,
            )["version"]
        for source, target in (("s", "c"), ("d", "c")):  # c is a convergence node
            version = add_edge(
                store, graph_id=graph_id, from_node=source, to_node=target,
                kind="forward", expect_version=version,
            )["version"]
        # c -> s closes s -> c -> s; s reaches c, so this cycles — refused
        # even though the edge re-enters a node with two forward in-edges.
        with pytest.raises(PacError) as raised:
            add_edge(
                store, graph_id=graph_id, from_node="c", to_node="s",
                kind="forward", expect_version=version,
            )
        assert raised.value.code == PAC_GRAPH_FORWARD_CYCLE
        assert store.graph(graph_id)["version"] == version
        # declared as back, the same edge is accepted: loops live in the graph
        add_edge(
            store, graph_id=graph_id, from_node="c", to_node="s",
            kind="back", expect_version=version,
        )
    finally:
        store.close()


def test_self_edge_is_rejected(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HYPRIAL_OWNER", "allen")
    store = PacGraphStore(default_database_path(state_dir))
    try:
        graph_id = create_graph(store, name="self", created_by="user:allen")["graphId"]
        add_node(store, state_dir, graph_id=graph_id, node_id="a", owner="user:allen", brief_ref="r", expect_version=1)
        with pytest.raises(PacError) as raised:
            add_edge(
                store, graph_id=graph_id, from_node="a", to_node="a", kind="back", expect_version=2
            )
        assert raised.value.code == PAC_EDGE_INVALID
    finally:
        store.close()


def test_bare_owner_names_are_rejected_at_the_write_boundary(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D1: the write boundary stores full principal URIs only.  A bare short
    name reaching the boundary is a caller defect (resolution happens BEFORE
    it), rejected with the shape code and zero writes."""

    monkeypatch.setenv("HYPRIAL_OWNER", "allen")
    monkeypatch.setenv("HYPRIAL_HOME", str(state_dir.parent / "home"))
    store = PacGraphStore(default_database_path(state_dir))
    try:
        graph_id = create_graph(store, name="owners", created_by="user:allen")["graphId"]
        add_node(store, state_dir, graph_id=graph_id, node_id="a", owner="user:allen", brief_ref="r", expect_version=1)
        for bad in (
            "nobody-here",          # bare short name
            "agent:squire",         # two-segment agent address
            "host:machine",         # a node is not a responsible party
            "route:lark:chat",      # a route is a delivery exit
            "adapter:allen:m:lark", # an adapter is a delivery exit
            "user:",                # empty owner
            "agent:allen:test-node",  # missing actor segment
        ):
            with pytest.raises(PacError) as raised:
                add_node(
                    store,
                    state_dir,
                    graph_id=graph_id,
                    node_id=f"bad-{len(store.nodes(graph_id))}",
                    owner=bad,
                    brief_ref="r",
                    expect_version=2,
                )
            assert raised.value.code == PAC_PRINCIPAL_SHAPE_INVALID, bad
        assert [node.node_id for node in store.nodes(graph_id)] == ["a"]
        assert store.graph(graph_id)["version"] == 2
    finally:
        store.close()


def test_foreign_agent_uri_is_storable_without_local_knowledge(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D1 §1.3 (T2): a fully-specified URI this node does NOT know is a legal
    owner -- legality is shape, never local registration or reachability."""

    monkeypatch.setenv("HYPRIAL_OWNER", "allen")
    store = PacGraphStore(default_database_path(state_dir))
    try:
        graph_id = create_graph(store, name="foreign", created_by="user:allen")["graphId"]
        foreign = "agent:someone-else:far-away-node.example.ts.net:squire"
        add_node(
            store,
            state_dir,
            graph_id=graph_id,
            node_id="remote-work",
            owner=foreign,
            brief_ref="r",
            expect_version=1,
        )
        node = store.node(graph_id, "remote-work")
        assert node is not None and node.owner == foreign
        # whitespace / control characters are not part of any legal segment
        for bad in ("agent:a b:machine:actor", "user: allen", "agent:allen:\tnode:x"):
            with pytest.raises(PacError) as raised:
                add_node(
                    store, state_dir, graph_id=graph_id, node_id="bad",
                    owner=bad, brief_ref="r", expect_version=2,
                )
            assert raised.value.code == PAC_PRINCIPAL_SHAPE_INVALID, bad
    finally:
        store.close()


def test_owner_stored_as_full_principal_uri(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stored owner IS the full principal URI (the 2026-09-14 boundary
    replaces the old short-name rule for PAC owners); actor_name stays a
    local short name."""

    monkeypatch.setenv("HYPRIAL_OWNER", "allen")
    store, graph_id = build_allen_graph(state_dir)
    try:
        for node in store.nodes(graph_id):
            assert node.owner.startswith(("user:", "agent:")), node.owner
            if node.actor_name is not None:
                assert ":" not in node.actor_name
    finally:
        store.close()


def test_actor_and_end_nodes_store_owned_identity_and_references_only(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HYPRIAL_OWNER", "allen")
    store = PacGraphStore(default_database_path(state_dir))
    try:
        graph_id = create_graph(store, name="actor-shape", created_by="user:allen")["graphId"]
        actor = add_node(
            store,
            state_dir,
            graph_id=graph_id,
            node_id="worker",
            owner="user:allen",
            brief_ref="notes/work.md#worker",
            kind="actor",
            actor_name="w",
            launch_ref="launch/codex-luna.yaml",
            expect_version=1,
        )
        assert actor["actorName"] == "w"
        # A later assignment names a full principal URI; it does not create a
        # second actor node.
        add_node(
            store,
            state_dir,
            graph_id=graph_id,
            node_id="work",
            owner="agent:allen:test-node:w",
            brief_ref="notes/work.md#task",
            expect_version=2,
        )
        add_node(
            store,
            state_dir,
            graph_id=graph_id,
            node_id="done",
            owner="user:allen",
            brief_ref="notes/work.md#done",
            kind="end",
            expect_version=3,
        )
        document = show_graph(store, graph_id)
        worker = next(node for node in document["nodes"] if node["nodeId"] == "worker")
        assert worker["kind"] == "actor"
        assert worker["actorName"] == "w"
        assert worker["launchRef"] == "launch/codex-luna.yaml"
        assert "agent:" not in str(worker)
        assert next(node for node in document["nodes"] if node["nodeId"] == "done")["kind"] == "end"
    finally:
        store.close()


def test_actor_shape_and_global_run_ownership_are_enforced(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HYPRIAL_OWNER", "allen")
    store = PacGraphStore(default_database_path(state_dir))
    try:
        first = create_graph(store, name="first", created_by="user:allen")["graphId"]
        second = create_graph(store, name="second", created_by="user:allen")["graphId"]
        for kwargs in (
            {"actor_name": None, "launch_ref": "launch/x.yaml"},
            {"actor_name": "w", "launch_ref": None},
            {"actor_name": "agent:w", "launch_ref": "launch/x.yaml"},
            {"actor_name": "a." + "a" * 26, "launch_ref": "launch/x.yaml"},
            {"actor_name": "d." + "b" * 32, "launch_ref": "launch/x.yaml"},
            {"actor_name": "A." + "A" * 26, "launch_ref": "launch/x.yaml"},
            {"actor_name": "D." + "B" * 32, "launch_ref": "launch/x.yaml"},
            {"actor_name": "U.OU_" + "B" * 32, "launch_ref": "launch/x.yaml"},
            {"actor_name": "u.ou_" + "b" * 32, "launch_ref": "launch/x.yaml"},
            {
                "actor_name": "u.6f8b1d21-33a8-4688-a32e-b1b23e87d5d9",
                "launch_ref": "launch/x.yaml",
            },
            {"actor_name": "w", "launch_ref": "inline\nbody"},
        ):
            with pytest.raises(PacError) as raised:
                add_node(
                    store, state_dir, graph_id=first, node_id="bad", owner="user:allen",
                    brief_ref="brief#bad", kind="actor", expect_version=1, **kwargs,
                )
            assert raised.value.code == PAC_NODE_SHAPE_INVALID
        add_node(
            store, state_dir, graph_id=first, node_id="actor", owner="user:allen",
            brief_ref="brief#actor", kind="actor", actor_name="w",
            launch_ref="launch/x.yaml", expect_version=1,
        )
        for version, actor_name in enumerate(("d.lane", "a.smith", "u.zhang"), 2):
            add_node(
                store, state_dir, graph_id=first, node_id=f"actor-{version}",
                owner="user:allen", brief_ref="brief#actor", kind="actor",
                actor_name=actor_name, launch_ref="launch/x.yaml",
                expect_version=version,
            )
        with pytest.raises(PacError, match="already owned by another run"):
            add_node(
                store, state_dir, graph_id=second, node_id="actor", owner="user:allen",
                brief_ref="brief#actor", kind="actor", actor_name="w",
                launch_ref="launch/x.yaml", expect_version=1,
            )
        with pytest.raises(PacError, match="belong only"):
            add_node(
                store, state_dir, graph_id=second, node_id="task", owner="user:allen",
                brief_ref="brief#task", actor_name="x", launch_ref="launch/x.yaml",
                expect_version=1,
            )
    finally:
        store.close()


def test_capability_requirements_are_typed_node_data(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HYPRIAL_OWNER", "allen")
    store = PacGraphStore(default_database_path(state_dir))
    try:
        graph_id = create_graph(store, name="requires", created_by="user:allen")["graphId"]
        requirement = {"tier": "strong"}
        add_node(
            store, state_dir, graph_id=graph_id, node_id="work", owner="user:allen",
            brief_ref="brief#work", requires=requirement, expect_version=1,
        )
        assert store.node(graph_id, "work").requires == requirement
        assert show_graph(store, graph_id)["nodes"][0]["requires"] == requirement
        for invalid in (
            {"tier": "guess"},
            {},
            {"role": "execute", "tier": "fast"},
            {"tier": "fast", "proactive": True},
            {"tier": "fast", "context_min": 8192},
            {"tier": "fast", "surprise": True},
        ):
            with pytest.raises(PacError) as raised:
                add_node(
                    store, state_dir, graph_id=graph_id, node_id="bad", owner="user:allen",
                    brief_ref="brief#bad", requires=invalid, expect_version=2,
                )
            assert raised.value.code == PAC_NODE_SHAPE_INVALID
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# acceptance 4: brief_ref is a bounded reference, never a body
# --------------------------------------------------------------------------- #


def test_brief_ref_is_a_bounded_single_line_reference(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HYPRIAL_OWNER", "allen")
    store = PacGraphStore(default_database_path(state_dir))
    try:
        graph_id = create_graph(store, name="refs", created_by="user:allen")["graphId"]
        with pytest.raises(PacError) as raised:
            add_node(
                store,
                state_dir,
                graph_id=graph_id,
                node_id="a",
                owner="user:allen",
                brief_ref="x" * 513,
                expect_version=1,
            )
        assert raised.value.code == PAC_NODE_SHAPE_INVALID
        with pytest.raises(PacError) as raised:
            add_node(
                store,
                state_dir,
                graph_id=graph_id,
                node_id="a",
                owner="user:allen",
                brief_ref="line one\nline two",
                expect_version=1,
            )
        assert raised.value.code == PAC_NODE_SHAPE_INVALID
        with pytest.raises(PacError) as raised:
            add_node(
                store,
                state_dir,
                graph_id=graph_id,
                node_id="a",
                owner="user:allen",
                brief_ref="   ",
                expect_version=1,
            )
        assert raised.value.code == PAC_NODE_SHAPE_INVALID
        assert store.nodes(graph_id) == []
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# acceptance 5: file separation from v1 + misc shape rejections
# --------------------------------------------------------------------------- #


def test_pac_database_is_a_separate_file_from_v1(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HYPRIAL_OWNER", "allen")
    store, graph_id = build_allen_graph(state_dir)
    try:
        assert default_database_path(state_dir).name == "pac-graph.sqlite3"
        assert not (state_dir / "workflows.sqlite3").exists(), (
            "P0 never touches v1's database; creating a v2 graph must not "
            "create a workflows.sqlite3"
        )
    finally:
        store.close()


def test_missing_graph_missing_node_duplicate_shapes(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HYPRIAL_OWNER", "allen")
    store = PacGraphStore(default_database_path(state_dir))
    try:
        with pytest.raises(PacError) as raised:
            show_graph(store, "no-such-graph")
        assert raised.value.code == PAC_GRAPH_NOT_FOUND

        graph_id = create_graph(store, name="shapes", created_by="user:allen")["graphId"]
        add_node(store, state_dir, graph_id=graph_id, node_id="a", owner="user:allen", brief_ref="r", expect_version=1)
        with pytest.raises(PacError) as raised:
            add_node(store, state_dir, graph_id=graph_id, node_id="a", owner="user:allen", brief_ref="r", expect_version=2)
        assert raised.value.code == PAC_NODE_EXISTS
        with pytest.raises(PacError) as raised:
            add_edge(store, graph_id=graph_id, from_node="a", to_node="ghost", kind="forward", expect_version=2)
        assert raised.value.code == PAC_NODE_NOT_FOUND
        add_node(store, state_dir, graph_id=graph_id, node_id="b", owner="user:allen", brief_ref="r", expect_version=2)
        add_edge(store, graph_id=graph_id, from_node="a", to_node="b", kind="forward", expect_version=3)
        with pytest.raises(PacError) as raised:
            add_edge(store, graph_id=graph_id, from_node="a", to_node="b", kind="forward", expect_version=4)
        assert raised.value.code == PAC_EDGE_EXISTS
    finally:
        store.close()
