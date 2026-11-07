"""Authoring-time owner resolution (design §3): resolve before the boundary.

T3/T4/T5 of the acceptance matrix.  Candidate sources are the REAL
serializations: ``AgentRegistry`` rows (never a hand-built ``agents(actor)``
table, §7.2) and real ``UserProfileStore`` files.  The port's one
auto-completion is graph scope; every partial view (registry, profiles,
live targets) reports its candidates and refuses to select.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hyprial.identity.impl.agents.registry._core import AgentRegistry
from hyprial.identity import (
    PAC_OWNER_AMBIGUOUS,
    PAC_OWNER_UNRESOLVED,
    PAC_PRINCIPAL_SHAPE_INVALID,
    PAC_RESOLUTION_UNAVAILABLE,
    PacError,
)
from hyprial.daemon.impl.pac.graphs.edits import add_node, create_graph
from hyprial.daemon.impl.pac.contracts.principals import resolve_owner
from hyprial.daemon.impl.pac.storage.store import PacGraphStore, default_database_path
from hyprial.identity import UserProfileStore

OWNER = "allen"
MACHINE = "test-node"


@pytest.fixture()
def state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    return state


def _graph_with(store: PacGraphStore, state: Path, owners: dict[str, str]) -> str:
    graph_id = create_graph(store, name="resolve-case", created_by="user:e2e")["graphId"]
    version = 1
    for node_id, owner in owners.items():
        version = add_node(
            store, state, graph_id=graph_id, node_id=node_id, owner=owner,
            brief_ref=f"r#{node_id}", expect_version=version,
        )["version"]
    return graph_id


# --------------------------------------------------------------------------- #
# rule 1: a full URI is pinned verbatim, never filtered by local knowledge
# --------------------------------------------------------------------------- #


def test_full_uri_input_is_pinned_without_local_knowledge(state: Path) -> None:
    report = resolve_owner(
        "agent:stranger:far-away.example.ts.net:squire", state_dir=state
    )
    assert report.selected_uri == "agent:stranger:far-away.example.ts.net:squire"
    assert report.completeness == "exact"
    assert report.candidates == []
    # a non-principal URI is a shape refusal, not a resolution question
    with pytest.raises(PacError) as raised:
        resolve_owner("route:lark:chat", state_dir=state)
    assert raised.value.code == PAC_PRINCIPAL_SHAPE_INVALID


# --------------------------------------------------------------------------- #
# rule 2/3: candidates come from every configured real source
# --------------------------------------------------------------------------- #


def test_candidates_collect_from_real_registry_and_profiles_side_by_side(
    state: Path,
) -> None:
    registry = AgentRegistry(state / "agents.sqlite3", owner=OWNER, machine=MACHINE)
    registry.create("squire")
    registry.close()
    UserProfileStore(state / "users.json").ensure(
        owner="squire",
        owner_key="squire",
        login_name="squire",
        machine=MACHINE,
        machine_key=MACHINE,
    )
    # partial views can never auto-select: the port refuses with every
    # candidate listed so the author resubmits with a concrete URI
    with pytest.raises(PacError) as raised:
        resolve_owner("squire", state_dir=state)
    assert raised.value.code == PAC_OWNER_AMBIGUOUS
    listed = raised.value.data["candidates"]
    assert {item["kind"] for item in listed} == {"agent", "user"}, (
        "person and agent candidates listed together"
    )
    assert {item["source"] for item in listed} == {"agents-registry", "user-profiles"}
    assert {item["uri"] for item in listed} == {
        f"agent:{OWNER}:{MACHINE}:squire",
        "user:squire",
    }


def test_kind_hint_filters_candidates(state: Path) -> None:
    registry = AgentRegistry(state / "agents.sqlite3", owner=OWNER, machine=MACHINE)
    registry.create("squire")
    registry.close()
    UserProfileStore(state / "users.json").ensure(
        owner="squire",
        owner_key="squire",
        login_name="squire",
        machine=MACHINE,
        machine_key=MACHINE,
    )
    with pytest.raises(PacError) as agent_side:
        resolve_owner("squire", state_dir=state, kind_hint="agent")
    assert [item["uri"] for item in agent_side.value.data["candidates"]] == [
        f"agent:{OWNER}:{MACHINE}:squire"
    ]
    with pytest.raises(PacError) as human_side:
        resolve_owner("squire", state_dir=state, kind_hint="human")
    assert [item["uri"] for item in human_side.value.data["candidates"]] == [
        "user:squire"
    ]
    with pytest.raises(PacError) as raised:
        resolve_owner("squire", state_dir=state, kind_hint="other")
    assert raised.value.code == PAC_PRINCIPAL_SHAPE_INVALID


# --------------------------------------------------------------------------- #
# rule 4: zero candidates vs a source failure are different verdicts
# --------------------------------------------------------------------------- #


def test_zero_candidates_is_unresolved_with_an_empty_list(state: Path) -> None:
    with pytest.raises(PacError) as raised:
        resolve_owner("ghost-nobody", state_dir=state)
    assert raised.value.code == PAC_OWNER_UNRESOLVED
    assert raised.value.data == {"candidates": []}


def test_source_failure_is_unavailable_never_an_empty_set(state: Path) -> None:
    broken = state / "agents.sqlite3"
    broken.write_bytes(b"not a database at all")
    with pytest.raises(PacError) as raised:
        resolve_owner("squire", state_dir=state)
    assert raised.value.code == PAC_RESOLUTION_UNAVAILABLE
    assert "agents registry" in str(raised.value)


def test_malformed_profiles_are_unavailable_not_an_empty_set(tmp_path: Path) -> None:
    fresh = tmp_path / "other-state"
    fresh.mkdir()
    malformed = fresh / "users.json"
    malformed.write_text('{"version": 2, "users": {}}', encoding="utf-8")
    with pytest.raises(PacError) as raised:
        resolve_owner("squire", state_dir=fresh)
    assert raised.value.code == PAC_RESOLUTION_UNAVAILABLE
    assert "refusing to guess" in str(raised.value)


# --------------------------------------------------------------------------- #
# rule 5: the only declarable-complete scope is the graph being edited
# --------------------------------------------------------------------------- #


def test_unique_on_this_graph_auto_completes(state: Path) -> None:
    store = PacGraphStore(default_database_path(state))
    try:
        graph_id = _graph_with(store, state, {"first": "user:pm"})
        report = resolve_owner("pm", state_dir=state, store=store, graph_id=graph_id)
        assert report.selected_uri == "user:pm"
        assert report.scope == "graph"
        assert report.completeness == "graph"
    finally:
        store.close()


def test_two_same_named_principals_on_the_graph_refuse_with_both_listed(
    state: Path,
) -> None:
    store = PacGraphStore(default_database_path(state))
    try:
        graph_id = _graph_with(
            store,
            state,
            {"person": "user:pm", "bot": f"agent:{OWNER}:{MACHINE}:pm"},
        )
        with pytest.raises(PacError) as raised:
            resolve_owner("pm", state_dir=state, store=store, graph_id=graph_id)
        assert raised.value.code == PAC_OWNER_AMBIGUOUS
        assert {item["uri"] for item in raised.value.data["candidates"]} == {
            "user:pm",
            f"agent:{OWNER}:{MACHINE}:pm",
        }
    finally:
        store.close()


def test_a_lone_partial_view_candidate_is_reported_not_selected(state: Path) -> None:
    # exactly one registry row matches, but the registry is a partial view:
    # an offline or foreign same-named principal is not disproved, so the
    # report carries the candidate and refuses to choose
    registry = AgentRegistry(state / "agents.sqlite3", owner=OWNER, machine=MACHINE)
    registry.create("squire")
    registry.close()
    store = PacGraphStore(default_database_path(state))
    try:
        graph_id = _graph_with(store, state, {"only": "user:e2e"})
        with pytest.raises(PacError) as raised:
            resolve_owner("squire", state_dir=state, store=store, graph_id=graph_id)
        assert raised.value.code == PAC_OWNER_AMBIGUOUS
        assert [item["uri"] for item in raised.value.data["candidates"]] == [
            f"agent:{OWNER}:{MACHINE}:squire"
        ]
        assert "complete scope" in str(raised.value)
    finally:
        store.close()


def test_graph_scope_does_not_reach_across_graphs(state: Path) -> None:
    store = PacGraphStore(default_database_path(state))
    try:
        _graph_with(store, state, {"first": "user:pm"})
        other = create_graph(store, name="other", created_by="user:e2e")["graphId"]
        with pytest.raises(PacError) as raised:
            resolve_owner("pm", state_dir=state, store=store, graph_id=other)
        assert raised.value.code in {PAC_OWNER_UNRESOLVED, PAC_OWNER_AMBIGUOUS}
    finally:
        store.close()


def test_legacy_short_name_rows_are_not_candidates(state: Path) -> None:
    store = PacGraphStore(default_database_path(state))
    try:
        graph_id = create_graph(store, name="legacy", created_by="user:e2e")["graphId"]
        with store.write() as db:
            db.execute(
                "INSERT INTO nodes(graph_id,node_id,owner,brief_ref,kind,flag) "
                "VALUES (?,?,?,?,?,?)",
                (graph_id, "old", "allen", "r#old", "task", 0),
            )
            db.commit()
        # a pre-URI row is NOT a URI candidate: the honest verdict is zero
        # candidates (the name stays ambiguous-by-era until the P3 migration)
        with pytest.raises(PacError) as raised:
            resolve_owner("allen", state_dir=state, store=store, graph_id=graph_id)
        assert raised.value.code == PAC_OWNER_UNRESOLVED
        assert raised.value.data == {"candidates": []}
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# T5: the confirmation pins the concrete URI, zero writes on any refusal
# --------------------------------------------------------------------------- #


def test_resolution_refusals_write_nothing(state: Path) -> None:
    store = PacGraphStore(default_database_path(state))
    try:
        graph_id = _graph_with(store, state, {"first": "user:pm"})
        version_before = store.graph(graph_id)["version"]
        with pytest.raises(PacError):
            resolve_owner("ghost-nobody", state_dir=state, store=store, graph_id=graph_id)
        with pytest.raises(PacError):
            resolve_owner("squire", state_dir=state, store=store, graph_id=graph_id)
        assert store.graph(graph_id)["version"] == version_before
        assert [node.node_id for node in store.nodes(graph_id)] == ["first"]
    finally:
        store.close()


def test_resolved_report_shape_is_the_full_evidence_product(state: Path) -> None:
    report = resolve_owner("user:pm", state_dir=state)
    document = report.to_json()
    assert document["input"] == "user:pm"
    assert document["selectedUri"] == "user:pm"
    assert document["completeness"] == "exact"
    assert document["observedAt"] > 0
    assert "a resolution report is not an authorization" not in document  # evidence, not proof


def test_registry_candidate_takes_the_stored_uri_verbatim(state: Path) -> None:
    registry = AgentRegistry(state / "agents.sqlite3", owner=OWNER, machine=MACHINE)
    registry.create("kanban")
    registry.close()
    with pytest.raises(PacError) as raised:
        resolve_owner("kanban", state_dir=state)
    [item] = raised.value.data["candidates"]
    assert item["uri"] == f"agent:{OWNER}:{MACHINE}:kanban"
    # a tampered uri column is surfaced as-is (never re-minted): the resolver
    # reports what the registry stores; the write boundary still validates
    tampered = sqlite3.connect(state / "agents.sqlite3")
    tampered.execute(
        "UPDATE agents SET uri='not-a-uri' WHERE actor='kanban'"
    )
    tampered.commit()
    tampered.close()
    with pytest.raises(PacError) as raised:
        resolve_owner("kanban", state_dir=state)
    [item] = raised.value.data["candidates"]
    assert item["uri"] == "not-a-uri"
