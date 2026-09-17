"""Monitoring layer: read-only projections over ``flag_events``.

Concept §1 layer 3 + slice-1 spec ⑤ and the headline acceptance: any
subscriber restates the flow's current state from
the graph structure + the ``flag_events`` stream alone — nothing else is
consulted, which is the property "整个流程可由 flag_events 表复述" names.

Two shapes share one fold:

- :class:`Projection` — the subscriber.  Opens the database
  **read-only** (``mode=ro``; a write attempt raises at the driver), pulls
  events past a cursor, and projects "whose turn it is / which round /
  whether anything was withdrawn".  It has no write method at all.
- :func:`restate` — the pure function behind the headline acceptance: feed
  it the structure and the event list, get the ordered restatement of the
  whole flow (every turn and withdraw, with round numbers) plus the final
  state.  Hidden ``hyprial pac debug restate`` also cross-checks the
  live ``notifications`` table against the fold so the two can never
  silently drift.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .graph import FORWARD, canonical_edge
from .journal import activation_id
from .principal import principal_kind
from .reactor import TURN, WITHDRAW, turn_text, withdraw_text
from .store import connect


def _blocked_on_kind(owner: str, actor_names: set[str], requires: Any) -> str:
    """blockedOn classification by URI kind (design §5.3).

    A full principal decides by its own kind -- ``requires`` never turns a
    person into an agent.  A pre-URI legacy short name is not reclassified
    by guessing: the historical heuristic applies to exactly those rows.
    """

    kind = principal_kind(owner)
    if kind is not None:
        return "agent" if kind == "agent" else "human"
    return "agent" if owner in actor_names or requires is not None else "human"


@dataclass(slots=True)
class ProjectionState:
    """The fold's accumulator — also the incremental subscriber's cache."""

    flags: dict[str, bool] = field(default_factory=dict)
    set_counts: dict[str, int] = field(default_factory=dict)
    flag_metadata: dict[str, dict[str, Any]] = field(default_factory=dict)
    turns: list[dict[str, Any]] = field(default_factory=list)
    withdraws: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    #: edges (canonical) that have ever delivered a turn — the withdraw
    #: condition (③ only withdraws downstreams already turned).
    turned_edges: set[str] = field(default_factory=set)


def _forward_predecessors(edges: list[dict[str, Any]], target: str) -> list[str]:
    """Join predecessors for ``target``: EVERY forward predecessor.

    Mirrors :meth:`PacReactor._forward_predecessors` exactly (concept
    §3.1 letter, dispatcher ruling 丙 2026-09-07); the drift test pins
    the agreement.
    """

    return [
        edge["from"]
        for edge in edges
        if edge["kind"] == FORWARD and edge["to"] == target
    ]


def fold_event(state: ProjectionState, structure: dict[str, Any], event: dict[str, Any]) -> None:
    """Fold exactly one flag event; mirrors the reactor's decision function.

    The reactor decides against the live store inside one transaction; this
    fold decides against the accumulated state.  They must agree —
    ``tests/test_pac_restate.py`` (headline acceptance #1) and the drift
    check in hidden ``hyprial pac debug restate`` pin that agreement.
    """

    if event.get("action") not in {"set", "reset"} or "type" in event:
        raise ValueError("flag fold accepts only explicit set/reset facts, not journal envelopes")
    before_turns, before_withdraws = len(state.turns), len(state.withdraws)
    node_id = event["nodeId"]
    nodes = structure["nodes"]
    edges = structure["edges"]
    state.events.append(event)
    if event["action"] == "set":
        state.flags[node_id] = True
        state.flag_metadata[node_id] = {"flagSetBy": event["actor"], "flagSetAt": event["at"],
                                        "flagReasonRef": event.get("reasonRef")}
        state.set_counts[node_id] = state.set_counts.get(node_id, 0) + 1
        for edge in edges:
            if edge["from"] != node_id:
                continue
            if edge["kind"] == FORWARD:
                target = edge["to"]
                predecessors = _forward_predecessors(edges, target)
                if not all(state.flags.get(pred, False) for pred in predecessors):
                    continue
            target = edge["to"]
            round_no = state.set_counts.get(target, 0) + 1
            record = {
                "eventId": event["eventId"],
                "edge": canonical_edge(node_id, target),
                "kind": TURN,
                "recipient": nodes[target]["owner"],
                "node": target,
                "round": round_no,
                "text": turn_text(
                    target, nodes[target]["briefRef"], event["eventId"], round_no
                ),
            }
            state.turns.append(record)
            state.turned_edges.add(record["edge"])
    else:
        state.flags[node_id] = False
        state.flag_metadata.pop(node_id, None)
        for edge in edges:
            if edge["from"] != node_id:
                continue
            target = edge["to"]
            edge_key = canonical_edge(node_id, target)
            if edge_key not in state.turned_edges:
                continue
            state.withdraws.append(
                {
                    "eventId": event["eventId"],
                    "edge": edge_key,
                    "kind": WITHDRAW,
                    "recipient": nodes[target]["owner"],
                    "node": target,
                    "text": withdraw_text(
                        target, nodes[target]["briefRef"], event["eventId"]
                    ),
                }
            )

    for item in [*state.turns[before_turns:], *state.withdraws[before_withdraws:]]:
        plan = {"graphId": structure["graphId"], "version": event["version"], "nodeId": item["node"],
                "predecessors": [
                    {"nodeId": pred, "flag": state.flags.get(pred, False),
                     "flagSetBy": state.flag_metadata.get(pred, {}).get("flagSetBy"),
                     "flagSetAt": state.flag_metadata.get(pred, {}).get("flagSetAt"),
                     "flagReasonRef": state.flag_metadata.get(pred, {}).get("flagReasonRef")}
                    for pred in _forward_predecessors(edges, item["node"])
                ]}
        if item["kind"] == TURN:
            plan["activationId"] = activation_id(structure["graphId"], item["node"], item["round"])
        item.update({"sender": event["actor"], "at": event["at"], "plan": plan})


def _load_structure(connection: Any, graph_id: str) -> dict[str, Any] | None:
    graph = connection.execute(
        "SELECT * FROM graphs WHERE graph_id = ?", (graph_id,)
    ).fetchone()
    if graph is None:
        return None
    nodes = {
        row["node_id"]: {
            "owner": row["owner"],
            "briefRef": row["brief_ref"],
            "kind": row["kind"],
            **({"actorName": row["actor_name"], "launchRef": row["launch_ref"]}
               if row["kind"] == "actor" else {}),
            **({"requires": json.loads(row["requires_json"])} if row["requires_json"] else {}),
            **(
                {"guardedByNodeId": row["guarded_by_node_id"]}
                if row["guarded_by_node_id"] is not None
                else {}
            ),
        }
        for row in connection.execute(
            "SELECT * FROM nodes WHERE graph_id = ?", (graph_id,)
        )
    }
    edges = [
        {"from": row["from_node"], "to": row["to_node"], "kind": row["kind"]}
        for row in connection.execute(
            "SELECT * FROM edges WHERE graph_id = ? ORDER BY from_node, to_node",
            (graph_id,),
        )
    ]
    return {
        "graphId": graph_id,
        "name": graph["name"],
        "version": graph["version"],
        **(
            {"operationKey": graph["operation_key"]}
            if graph["operation_key"] is not None
            else {}
        ),
        "nodes": nodes,
        "edges": edges,
    }


def _events_since(connection: Any, graph_id: str, after_seq: int) -> list[dict[str, Any]]:
    return [
        {
            "seq": row["seq"],
            "eventId": row["event_id"],
            "nodeId": row["node_id"],
            "action": row["action"],
            "actor": row["actor"],
            "at": row["at"],
            "version": row["version"],
            "reasonRef": row["reason_ref"],
        }
        for row in connection.execute(
            "SELECT * FROM flag_events WHERE graph_id = ? AND seq > ? ORDER BY seq",
            (graph_id, after_seq),
        )
    ]


def restate(structure: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    """Restate the whole flow from structure + flag events (pure).

    This is headline acceptance #1's function: the ordered narrative
    (every event with the turns/withdraws it caused, rounds included) and
    the final state, derived from nothing but ``flag_events`` and the
    static graph.
    """

    state = ProjectionState()
    for event in events:
        fold_event(state, structure, event)

    steps: list[dict[str, Any]] = []
    for event in state.events:
        caused = [
            *(item for item in state.turns if item["eventId"] == event["eventId"]),
            *(item for item in state.withdraws if item["eventId"] == event["eventId"]),
        ]
        steps.append(
            {
                "seq": event["seq"],
                "event": event["eventId"],
                "action": event["action"],
                "node": event["nodeId"],
                "actor": event["actor"],
                "caused": [
                    {key: value for key, value in item.items() if key != "eventId"}
                    for item in caused
                ],
            }
        )

    nodes_out: list[dict[str, Any]] = []
    actor_names = {
        node.get("actorName")
        for node in structure["nodes"].values()
        if node.get("actorName") is not None
    }
    for node_id, node in structure["nodes"].items():
        predecessors = _forward_predecessors(structure["edges"], node_id)
        ready = all(state.flags.get(pred, False) for pred in predecessors)
        waiting = bool(predecessors) and not ready
        # A node whose only exits are back edges is a loop *button* (e.g.
        # pm-不通过), not a work turn: it is never listed as a current turn,
        # even though firing it is always legal.  Terminal nodes (no exits)
        # are work nodes.
        exits = [edge for edge in structure["edges"] if edge["from"] == node_id]
        is_button = bool(exits) and not any(
            edge["kind"] == FORWARD for edge in exits
        )
        current = ready and not state.flags.get(node_id, False) and not is_button
        nodes_out.append(
            {
                "node": node_id,
                "owner": node["owner"],
                "flag": state.flags.get(node_id, False),
                "round": state.set_counts.get(node_id, 0),
                "turn": "current" if current else None,
                "blockedOn": (
                    _blocked_on_kind(node["owner"], actor_names, node.get("requires"))
                    if current
                    else "none"
                ),
                **(
                    {"waitingOn": [p for p in predecessors if not state.flags.get(p, False)]}
                    if waiting
                    else {}
                ),
            }
        )
    return {
        "graphId": structure["graphId"],
        "version": structure["version"],
        "steps": steps,
        "turns": state.turns,
        "withdraws": state.withdraws,
        "state": {
            "nodes": nodes_out,
            "currentTurns": [
                {
                    "node": item["node"],
                    "owner": item["owner"],
                    # the round being ASKED now = completed rounds + 1
                    "round": state.set_counts.get(item["node"], 0) + 1,
                }
                for item in nodes_out
                if item["turn"] == "current"
            ],
        },
    }


def audit(database_path: Any, graph_id: str) -> dict[str, Any]:
    """Compare complete decision semantics at one read watermark, not just keys."""
    from .subscription import notification, read_transaction
    with read_transaction(database_path) as db:
        structure = _load_structure(db, graph_id)
        if structure is None:
            raise KeyError(graph_id)
        document = restate(structure, _events_since(db, graph_id, 0))
        recorded = {(row["event_id"], row["edge"]): notification(row) for row in db.execute(
            "SELECT n.* FROM notifications n JOIN flag_events e ON e.event_id=n.event_id "
            "WHERE e.graph_id=?", (graph_id,))}
        folded = {(item["eventId"], item["edge"]): item for item in [*document["turns"], *document["withdraws"]]}
        fields = ("kind", "recipient", "node", "round", "text", "sender", "at", "plan")
        drift = []
        for key in sorted(recorded.keys() | folded.keys()):
            actual, expected = recorded.get(key), folded.get(key)
            if actual is None or expected is None:
                drift.append({"eventId": key[0], "edge": key[1], "fields": ["missing_record" if actual is None else "missing_fold"]})
            else:
                changed = [field for field in fields if actual.get(field) != expected.get(field)]
                if changed:
                    drift.append({"eventId": key[0], "edge": key[1], "fields": changed})
        return {**document, "ok": not drift, "drift": drift}


class Projection:
    """The first ``flag_events`` subscriber; strictly read-only.

    Opens the database through ``mode=ro`` — the driver itself refuses a
    write — and exposes two reads: a full :meth:`snapshot` and the
    incremental :meth:`changes_since` a subscriber would poll.  There is
    no write API on this class, on purpose.
    """

    def __init__(self, database_path: Any) -> None:
        self._path = database_path

    def _connect(self) -> Any:
        return connect(self._path, read_only=True)

    def public_snapshot(self, graph_id: str) -> dict[str, Any]:
        from .subscription import snapshot
        return snapshot(self._path, graph_id)

    def events_since(self, graph_id: str, after: int, *, journal_id: str | None = None) -> dict[str, Any]:
        from .subscription import events_since
        return events_since(self._path, graph_id, after, journal_id=journal_id)

    def snapshot(self, graph_id: str) -> dict[str, Any]:
        """Project the graph's current state from all of its events."""

        connection = self._connect()
        try:
            connection.execute("BEGIN")
            structure = _load_structure(connection, graph_id)
            if structure is None:
                raise KeyError(graph_id)
            events = _events_since(connection, graph_id, 0)
        finally:
            connection.close()
        return restate(structure, events)

    def changes_since(self, graph_id: str, last_seq: int) -> dict[str, Any]:
        """The subscription seam: events past ``last_seq`` + new cursor.

        An incremental consumer folds only these events into its cached
        state (structure changes re-read the current version); the
        acceptance test pins incremental-fold == full-snapshot.
        """

        connection = self._connect()
        try:
            connection.execute("BEGIN")
            structure = _load_structure(connection, graph_id)
            if structure is None:
                raise KeyError(graph_id)
            events = _events_since(connection, graph_id, last_seq)
        finally:
            connection.close()
        cursor = events[-1]["seq"] if events else last_seq
        return {
            "graphId": graph_id,
            "version": structure["version"],
            "events": events,
            "cursor": cursor,
        }
