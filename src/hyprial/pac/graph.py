"""Editing the PAC graph file: CAS-versioned structure edits + validation.

Concept §1/§2 and the slice-1 spec ①: humans and agents edit the graph
through ``hyprial workflow plan/run``; every structural edit
bumps ``graphs.version`` under compare-and-swap (the caller must present
``--expect-version <n>``, a mismatch is ``PAC_GRAPH_VERSION_CONFLICT`` and
nothing is written).

Write-time validation (fail-closed, dif.sh ④ borrowed as *edit-time*
checks — earlier than build-time):

- a **back** edge must be declared ``kind=back``; the practical form of
  that rule is the cycle rule below;
- **forward** edges must never close a cycle — the forward subgraph
  stays a DAG, full stop (dispatcher ruling 2026-09-07 丙: loops live in
  the graph or nowhere; a cycle the graph did not declare is refused
  whether or not it passes through a convergence node).  A multi-round
  confirmation is modelled as distinct nodes (``pm-confirm-1`` /
  ``pm-confirm-2``), not as a forward re-entry edge;
- the **owner** must already exist on this graph or be a known
  principal/agent short name (never a derived ``agent:`` URI — the node
  stores the short name);
- ``brief_ref`` is a bounded single-line reference, never a body.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from time import time_ns
from typing import Any
from uuid import uuid4

from hyprial.dispatch.matrix import TIERS

from .errors import (
    PAC_EDGE_EXISTS,
    PAC_EDGE_INVALID,
    PAC_GRAPH_EXISTS,
    PAC_GRAPH_FORWARD_CYCLE,
    PAC_GRAPH_FROZEN,
    PAC_GRAPH_CLOSED,
    PAC_GRAPH_NOT_OWNER,
    PAC_GRAPH_NOT_FOUND,
    PAC_GRAPH_VERSION_CONFLICT,
    PAC_NODE_EXISTS,
    PAC_NODE_NOT_FOUND,
    PAC_NODE_SHAPE_INVALID,
    PAC_OPERATION_KEY_CONFLICT,
    PacError,
)
from .principal import parse_principal
from .store import MAX_BRIEF_REF_LENGTH, MAX_OPERATION_KEY_LENGTH, PacGraphStore
from .journal import append_event
from .migrations import unrewritten_owners_note

#: Actor names stay LOCAL short names (the run-owned runtime key); only the
#: owner side moved to full principal URIs.  Used for --actor-name validation.
OWNER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: The one implementation restriction on node ids: non-empty, no arrows
#: (the notification ``edge`` encoding is ``from -> to``), bounded.
NODE_ID_PATTERN = re.compile(r"^[^>\n\r]{1,128}$")

FORWARD = "forward"
BACK = "back"


def canonical_edge(from_node: str, to_node: str) -> str:
    """The stable edge encoding used as the notifications idempotency key."""

    return f"{from_node} -> {to_node}"


# --------------------------------------------------------------------------- #
# Owner validation (write boundary: full principal URIs only)
# --------------------------------------------------------------------------- #


def known_agent_names(state_dir: Path) -> set[str]:
    """Agent short names registered in this state root's agents database.

    Read straight from ``agents.sqlite3`` (same state root the CLI is
    pinned to) so edit-time owner validation sees exactly the identities
    this node knows, without instantiating the registry machinery.
    """

    database = Path(state_dir) / "agents.sqlite3"
    if not database.is_file():
        return set()
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        rows = connection.execute("SELECT actor FROM agents").fetchall()
        return {str(row[0]) for row in rows}
    except sqlite3.Error:
        return set()
    finally:
        if connection is not None:
            connection.close()


def _validate_owner(
    store: PacGraphStore,
    state_dir: Path,
    graph_id: str,
    owner: str,
) -> None:
    # The write boundary accepts exactly a full principal URI (D1): shape and
    # character hygiene only.  Whether THIS node knows the principal is not a
    # legality input (§1.3): a foreign, offline, or not-yet-launched agent is
    # a storable owner.  Bare short names are resolved BEFORE this boundary
    # (pac.resolve); reaching it unqualified is a caller defect, not a lookup.
    parse_principal(owner)


def _validate_reference(value: str, *, field: str) -> None:
    if not value or not value.strip():
        raise PacError(PAC_NODE_SHAPE_INVALID, f"{field} must not be empty")
    if len(value) > MAX_BRIEF_REF_LENGTH:
        raise PacError(
            PAC_NODE_SHAPE_INVALID,
            f"{field} is a reference, not a body (max "
            f"{MAX_BRIEF_REF_LENGTH} chars, got {len(value)})",
        )
    if any(character in value for character in "\n\r"):
        raise PacError(
            PAC_NODE_SHAPE_INVALID,
            f"{field} must be a single line (a reference, never a body)",
        )


def _validate_brief_ref(brief_ref: str) -> None:
    _validate_reference(brief_ref, field="brief_ref")


def _validate_operation_key(operation_key: str | None) -> None:
    if operation_key is None:
        return
    if len(operation_key) > MAX_OPERATION_KEY_LENGTH:
        raise PacError(
            PAC_NODE_SHAPE_INVALID,
            f"operation_key is bounded at {MAX_OPERATION_KEY_LENGTH} chars",
        )
    if not operation_key.strip() or any(character in operation_key for character in "\n\r"):
        raise PacError(
            PAC_NODE_SHAPE_INVALID,
            "operation_key must be a non-empty single line when provided",
        )


def _validate_requires(requires: dict[str, Any] | None) -> None:
    if requires is None:
        return
    if not isinstance(requires, dict) or set(requires) != {"tier"}:
        raise PacError(
            PAC_NODE_SHAPE_INVALID,
            "requires currently supports exactly one enforced field: tier",
        )
    if requires["tier"] not in TIERS:
        raise PacError(PAC_NODE_SHAPE_INVALID, "requires.tier is invalid")


def _require_graph(store: PacGraphStore, graph_id: str) -> dict[str, Any]:
    graph = store.graph(graph_id)
    if graph is None:
        raise PacError(PAC_GRAPH_NOT_FOUND, f"graph {graph_id!r} not found")
    return graph


def _require_version(
    store: PacGraphStore, graph_id: str, expect_version: int
) -> dict[str, Any]:
    graph = _require_graph(store, graph_id)
    if graph["activated_at"] is not None or graph["closed_at"] is not None:
        raise PacError(PAC_GRAPH_FROZEN, "active/closed graph structure is frozen; create a new graph")
    if graph["version"] != expect_version:
        raise PacError(
            PAC_GRAPH_VERSION_CONFLICT,
            f"graph {graph_id!r} is at version {graph['version']}, not the "
            f"expected {expect_version}; re-read it and retry your edit",
            {
                "graphId": graph_id,
                "expected": expect_version,
                "current": graph["version"],
            },
        )
    return graph


def _forward_reaches(store: PacGraphStore, graph_id: str) -> dict[str, list[str]]:
    adjacency: dict[str, list[str]] = {}
    for edge in store.edges(graph_id):
        if edge.kind != FORWARD:
            continue
        adjacency.setdefault(edge.from_node, []).append(edge.to_node)
    return adjacency


def _forward_cycle_path(
    store: PacGraphStore, graph_id: str, added: tuple[str, str]
) -> list[str] | None:
    """The node path of the cycle a candidate forward edge would close.

    Walks the existing forward subgraph from the candidate's ``to`` node;
    if it can reach the candidate's ``from`` node, the candidate closes a
    loop — and a loop is only legal as a declared ``back`` edge.
    """

    start, target = added[1], added[0]
    adjacency = _forward_reaches(store, graph_id)
    stack: list[tuple[str, list[str]]] = [(start, [start])]
    seen: set[str] = set()
    while stack:
        node, path = stack.pop()
        for nxt in adjacency.get(node, []):
            extended = [*path, nxt]
            if nxt == target:
                return [*extended, target]
            if nxt in seen:
                continue
            seen.add(nxt)
            stack.append((nxt, extended))
    return None


# --------------------------------------------------------------------------- #
# Edit operations
# --------------------------------------------------------------------------- #


def _graph_head(graph: dict[str, Any]) -> dict[str, Any]:
    return {
        "graphId": graph["graph_id"],
        "name": graph["name"],
        "version": graph["version"],
        **(
            {"operationKey": graph["operation_key"]}
            if graph["operation_key"] is not None
            else {}
        ),
    }


def create_graph(
    store: PacGraphStore,
    *,
    name: str,
    created_by: str,
    operation_key: str | None = None,
) -> dict[str, Any]:
    """Create a graph, or return the existing graph for ``operation_key``.

    The UNIQUE index, not a process-local check, arbitrates concurrent and
    replayed keyed creation.  Keyless calls retain create-new behavior.
    """

    if not name or not name.strip():
        raise PacError(PAC_NODE_SHAPE_INVALID, "graph name must not be empty")
    _validate_operation_key(operation_key)
    # The creator is a principal URI (D1 §1.1): graph activate/close/stop
    # authorize against it by exact equality, so it must be storable verbatim.
    parse_principal(created_by)
    graph_id = f"{name.strip()}-{uuid4().hex[:8]}"
    at = time_ns() // 1_000_000
    db = store.write()
    try:
        if store.graph(graph_id) is not None:  # pragma: no cover - uuid collision
            raise PacError(PAC_GRAPH_EXISTS, f"graph {graph_id!r} already exists")
        try:
            db.execute(
                "INSERT INTO graphs "
                "(graph_id, name, version, created_by, created_at, operation_key) "
                "VALUES (?, ?, 1, ?, ?, ?)",
                (graph_id, name.strip(), created_by, at, operation_key),
            )
        except sqlite3.IntegrityError:
            existing = (
                store.graph_by_operation_key(operation_key)
                if operation_key is not None
                else None
            )
            if existing is None:
                raise
            db.rollback()
            # The durable key arbitrates replays, but a replay is only the
            # same request when its creating principal matches the graph the
            # key minted (design §6 tail): a different --by under a known key
            # is a conflict, never a silent re-bind and never a second graph.
            if existing["created_by"] != created_by:
                raise PacError(
                    PAC_OPERATION_KEY_CONFLICT,
                    f"operation key is already bound to graph "
                    f"{existing['graph_id']!r} created by "
                    f"{existing['created_by']!r}; replaying it as "
                    f"{created_by!r} is refused",
                    {
                        "graphId": existing["graph_id"],
                        "createdBy": existing["created_by"],
                        "presentedBy": created_by,
                    },
                )
            return _graph_head(existing)
        append_event(
            db,
            graph_id=graph_id,
            version=1,
            type="structure_changed",
            at=at,
            data={"operation": "create", "resync": True},
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise
    graph = store.graph(graph_id)
    assert graph is not None
    return _graph_head(graph)


def add_node(
    store: PacGraphStore,
    state_dir: Path,
    *,
    graph_id: str,
    node_id: str,
    owner: str,
    brief_ref: str,
    kind: str = "task",
    deadline_ms: int | None = None,
    actor_name: str | None = None,
    launch_ref: str | None = None,
    requires: dict[str, Any] | None = None,
    guarded_by_node_id: str | None = None,
    expect_version: int,
) -> dict[str, Any]:
    """Add one node under CAS; bumps the graph version by exactly one."""

    if not NODE_ID_PATTERN.fullmatch(node_id):
        raise PacError(
            PAC_NODE_SHAPE_INVALID,
            f"node id {node_id!r} must be 1-128 chars without '>' or newlines "
            "(the notification edge encoding is 'from -> to')",
        )
    if kind not in ("task", "clock", "actor", "end"):
        raise PacError(
            PAC_NODE_SHAPE_INVALID,
            f"node kind must be task, clock, actor, or end; not {kind!r}",
        )
    if kind == "clock":
        if not isinstance(deadline_ms, int) or deadline_ms <= 0:
            raise PacError(
                PAC_NODE_SHAPE_INVALID,
                "a clock node carries a positive --deadline-ms",
            )
    elif deadline_ms is not None:
        raise PacError(
            PAC_NODE_SHAPE_INVALID,
            f"a {kind} node carries no deadline; declare --kind clock for that",
        )
    if guarded_by_node_id is not None and kind != "clock":
        raise PacError(
            PAC_NODE_SHAPE_INVALID,
            "guarded_by_node_id belongs only to kind=clock nodes",
        )
    if kind == "actor":
        if actor_name is None or not OWNER_NAME_PATTERN.fullmatch(actor_name):
            raise PacError(PAC_NODE_SHAPE_INVALID, "an actor node requires a short --actor-name")
        if launch_ref is None:
            raise PacError(PAC_NODE_SHAPE_INVALID, "an actor node requires --launch-ref")
        _validate_reference(launch_ref, field="launch_ref")
    elif actor_name is not None or launch_ref is not None:
        raise PacError(
            PAC_NODE_SHAPE_INVALID,
            "actor_name and launch_ref belong only to kind=actor nodes",
        )
    _validate_brief_ref(brief_ref)
    _validate_requires(requires)
    if actor_name is not None and actor_name in known_agent_names(state_dir):
        raise PacError(
            PAC_NODE_SHAPE_INVALID,
            f"actor name {actor_name!r} already belongs to a registered runtime; "
            "a run cannot adopt it",
            {"actorName": actor_name},
        )

    db = store.write()
    try:
        _require_version(store, graph_id, expect_version)
        if guarded_by_node_id is not None:
            guarded = store.node(graph_id, guarded_by_node_id)
            if guarded is None or guarded.kind != "task":
                raise PacError(
                    PAC_NODE_SHAPE_INVALID,
                    "guarded_by_node_id must name an existing task node on this graph",
                    {"guardedByNodeId": guarded_by_node_id},
                )
        if store.node(graph_id, node_id) is not None:
            raise PacError(
                PAC_NODE_EXISTS, f"node {node_id!r} already exists on {graph_id!r}"
            )
        _validate_owner(store, state_dir, graph_id, owner)
        if actor_name is not None and db.execute(
            "SELECT 1 FROM nodes WHERE actor_name = ?", (actor_name,)
        ).fetchone() is not None:
            raise PacError(
                PAC_NODE_SHAPE_INVALID,
                f"actor name {actor_name!r} is already owned by another run",
                {"actorName": actor_name},
            )
        db.execute(
            "INSERT INTO nodes "
            "(graph_id, node_id, owner, brief_ref, kind, deadline_ms, actor_name, "
            "launch_ref, requires_json, guarded_by_node_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                graph_id,
                node_id,
                owner,
                brief_ref,
                kind,
                deadline_ms,
                actor_name,
                launch_ref,
                json.dumps(requires, sort_keys=True) if requires is not None else None,
                guarded_by_node_id,
            ),
        )
        db.execute(
            "UPDATE graphs SET version = version + 1 WHERE graph_id = ?",
            (graph_id,),
        )
        append_event(db, graph_id=graph_id, version=expect_version + 1, type="structure_changed",
                     at=time_ns() // 1_000_000, data={"operation": "add_node", "resync": True})
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return {
        "graphId": graph_id,
        "nodeId": node_id,
        "owner": owner,
        "kind": kind,
        **({"actorName": actor_name, "launchRef": launch_ref} if actor_name else {}),
        **({"requires": requires} if requires is not None else {}),
        **(
            {"guardedByNodeId": guarded_by_node_id}
            if guarded_by_node_id is not None
            else {}
        ),
        "version": expect_version + 1,
    }


def add_edge(
    store: PacGraphStore,
    *,
    graph_id: str,
    from_node: str,
    to_node: str,
    kind: str = FORWARD,
    expect_version: int,
) -> dict[str, Any]:
    """Add one edge under CAS; bumps the graph version by exactly one.

    The loop rule (concept §2, spec ①, dispatcher ruling 丙 2026-09-07):
    a ``forward`` edge may never close a cycle — through a convergence
    node or not.  Loops are only legal declared as ``kind=back``; a
    node visited in two rounds is two nodes (``pm-confirm-1`` /
    ``pm-confirm-2``), never a forward re-entry edge.
    """

    if kind not in (FORWARD, BACK):
        raise PacError(
            PAC_EDGE_INVALID, f"edge kind must be 'forward' or 'back', not {kind!r}"
        )
    if from_node == to_node:
        raise PacError(
            PAC_EDGE_INVALID,
            f"self-edge {from_node!r} -> {to_node!r} is not a step; loops "
            "are declared as back edges between distinct nodes",
        )
    db = store.write()
    try:
        _require_version(store, graph_id, expect_version)
        for node_id in (from_node, to_node):
            if store.node(graph_id, node_id) is None:
                raise PacError(
                    PAC_NODE_NOT_FOUND,
                    f"node {node_id!r} not found on graph {graph_id!r}",
                )
        if any(
            edge.from_node == from_node and edge.to_node == to_node
            for edge in store.edges(graph_id)
        ):
            raise PacError(
                PAC_EDGE_EXISTS,
                f"edge {canonical_edge(from_node, to_node)} already exists",
            )
        if kind == FORWARD:
            cycle = _forward_cycle_path(store, graph_id, (from_node, to_node))
            if cycle is not None:
                raise PacError(
                    PAC_GRAPH_FORWARD_CYCLE,
                    "forward edges must not close an implicit cycle "
                    f"({' -> '.join(cycle)}); declare this edge --kind back "
                    "if it is a loop",
                    {"path": cycle},
                )
        db.execute(
            "INSERT INTO edges (graph_id, from_node, to_node, kind) VALUES (?, ?, ?, ?)",
            (graph_id, from_node, to_node, kind),
        )
        db.execute(
            "UPDATE graphs SET version = version + 1 WHERE graph_id = ?",
            (graph_id,),
        )
        append_event(db, graph_id=graph_id, version=expect_version + 1, type="structure_changed",
                     at=time_ns() // 1_000_000, data={"operation": "add_edge", "resync": True})
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return {
        "graphId": graph_id,
        "edge": canonical_edge(from_node, to_node),
        "kind": kind,
        "version": expect_version + 1,
    }


def activate_graph(store: PacGraphStore, graph_id: str, *, actor: str) -> dict[str, Any]:
    """Freeze the completed structure. No runtime/actor is launched here."""
    with store.write() as db:
        graph = _require_graph(store, graph_id)
        if actor != graph["created_by"]:
            note = unrewritten_owners_note(db, graph_id)
            raise PacError(
                PAC_GRAPH_NOT_OWNER,
                "only the graph owner can activate it"
                + (f"; {note}" if note else ""),
            )
        if graph["closed_at"] is not None:
            raise PacError(PAC_GRAPH_CLOSED, "a closed graph cannot be activated again")
        if graph["activated_at"] is None:
            at = time_ns() // 1_000_000
            db.execute("UPDATE graphs SET activated_at=?, activated_by=? WHERE graph_id=?",
                       (at, actor, graph_id))
            append_event(db, graph_id=graph_id, version=graph["version"], type="graph_activated",
                         at=at, data={"at": at, "by": actor})
    return show_graph(store, graph_id)


def close_graph(store: PacGraphStore, graph_id: str, *, actor: str) -> dict[str, Any]:
    """Owner closes a task/clock run monotonically; no flags or actors inferred."""
    with store.write() as db:
        graph = _require_graph(store, graph_id)
        if actor != graph["created_by"]:
            note = unrewritten_owners_note(db, graph_id)
            raise PacError(
                PAC_GRAPH_NOT_OWNER,
                "only the graph owner can close it"
                + (f"; {note}" if note else ""),
            )
        if graph["closed_at"] is None:
            at = time_ns() // 1_000_000
            db.execute("UPDATE graphs SET closed_at=?, closed_by=? WHERE graph_id=?", (at, actor, graph_id))
            append_event(db, graph_id=graph_id, version=graph["version"], type="graph_closed",
                         at=at, data={"at": at, "by": actor})
            db.execute("UPDATE workflow_graphs SET state='cancelled',reason_ref='pac:graph-closed' "
                       "WHERE graph_id=? AND state NOT IN ('completed','failed','cancelled')", (graph_id,))
            db.execute("UPDATE workflow_nodes SET state='cancelled',reason_ref='pac:graph-closed' "
                       "WHERE graph_id=? AND state IN ('pending','requested')", (graph_id,))
    return show_graph(store, graph_id)


def show_graph(store: PacGraphStore, graph_id: str) -> dict[str, Any]:
    """Read the current version and restate the whole graph."""

    with store.read():
        graph = _require_graph(store, graph_id)
        return {
            "graphId": graph_id,
            "name": graph["name"],
            "version": graph["version"],
            "createdBy": graph["created_by"],
            **(
                {"operationKey": graph["operation_key"]}
                if graph["operation_key"] is not None
                else {}
            ),
            "active": ({"at": graph["activated_at"], "by": graph["activated_by"]}
                       if graph["activated_at"] is not None else None),
            "closed": ({"at": graph["closed_at"], "by": graph["closed_by"]}
                       if graph["closed_at"] is not None else None),
            "nodes": [node.to_json() for node in store.nodes(graph_id)],
            "edges": [edge.to_json() for edge in store.edges(graph_id)],
        }
