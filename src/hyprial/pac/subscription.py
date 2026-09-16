"""The public snapshot@cursor / events>cursor contract, over read-only SQLite.

No writes, inbox, or reference dereferencing. Structure changes invalidate a
consumer's structure; active graphs are frozen, so historical structure replay
is not part of the protocol. Assignment views are folded from durable requests,
withdrawals and flag completions, never inferred from today's edges.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .errors import PAC_EVENTS_RESYNC, PAC_GRAPH_NOT_FOUND, PacError
from .journal import activation_id, envelope
from .migrations import SCHEMA_VERSION
from .store import connect


@contextmanager
def read_transaction(path: Path):
    if not Path(path).is_file():
        raise PacError(PAC_GRAPH_NOT_FOUND, "PAC database does not exist")
    db = connect(path, read_only=True)
    try:
        db.execute("BEGIN")
        if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise PacError(PAC_EVENTS_RESYNC, "upgrade the PAC store before subscribing", {"resync": True})
        yield db
    finally:
        db.rollback()
        db.close()


def _head(db: sqlite3.Connection, graph_id: str) -> sqlite3.Row:
    graph = db.execute("SELECT * FROM graphs WHERE graph_id=?", (graph_id,)).fetchone()
    if graph is None:
        raise PacError(PAC_GRAPH_NOT_FOUND, f"graph {graph_id!r} not found")
    return graph


def _watermark(db: sqlite3.Connection, graph_id: str) -> tuple[str, int, int]:
    identity = db.execute("SELECT journal_id FROM journal_meta WHERE singleton=1").fetchone()[0]
    high = db.execute("SELECT COALESCE(MAX(seq),0) FROM journal").fetchone()[0]
    floor = db.execute(
        "SELECT COALESCE(MAX(seq),0) FROM journal WHERE graph_id=? AND type='migration_baseline'",
        (graph_id,),
    ).fetchone()[0]
    return identity, high, floor


def _structure(db: sqlite3.Connection, graph: sqlite3.Row) -> tuple[dict[str, Any], list[sqlite3.Row]]:
    nodes = db.execute("SELECT * FROM nodes WHERE graph_id=? ORDER BY node_id", (graph["graph_id"],)).fetchall()
    structure = {
        "name": graph["name"], "createdBy": graph["created_by"],
        **(
            {"operationKey": graph["operation_key"]}
            if graph["operation_key"] is not None
            else {}
        ),
        "nodes": [
            {
                "nodeId": n["node_id"],
                "owner": n["owner"],
                "briefRef": n["brief_ref"],
                "kind": n["kind"],
                "deadlineMs": n["deadline_ms"],
                **({"actorName": n["actor_name"], "launchRef": n["launch_ref"]}
                   if n["kind"] == "actor" else {}),
                **({"requires": json.loads(n["requires_json"])} if n["requires_json"] else {}),
                **(
                    {"guardedByNodeId": n["guarded_by_node_id"]}
                    if n["guarded_by_node_id"] is not None
                    else {}
                ),
            }
            for n in nodes
        ],
        "edges": [{"from": e["from_node"], "to": e["to_node"], "kind": e["kind"]}
                  for e in db.execute("SELECT * FROM edges WHERE graph_id=? ORDER BY from_node,to_node", (graph["graph_id"],))],
    }
    return structure, nodes


def notification(row: sqlite3.Row) -> dict[str, Any]:
    plan = json.loads(row["plan_json"]) if row["plan_json"] else None
    if plan is not None:
        target = plan["nodeId"]
    elif row["kind"] == "overdue":
        target = row["edge"].split("->", 1)[-1] if row["edge"].startswith("clock-up:") else row["edge"][len("clock:"):]
    else:
        # Only legacy rows lack a plan. Slice-1 prohibited '>' in node ids,
        # making this old canonical edge encoding unambiguous.
        target = row["edge"].split(" -> ", 1)[1]
    return {"eventId": row["event_id"], "edge": row["edge"], "kind": row["kind"],
            "recipient": row["recipient"], "node": target, "round": row["round_no"],
            "text": row["text"], "sender": row["sender"], "at": row["at"],
            "messageId": row["message_id"], "deliveredAt": row["delivered_at"], "plan": plan}


def _requests_and_counts(db: sqlite3.Connection, graph_id: str):
    """Fold recorded plans in flag order; no current-structure interpretation."""
    rows = db.execute(
        "SELECT n.*, e.seq AS flag_seq FROM notifications n JOIN flag_events e "
        "ON e.event_id=n.event_id WHERE e.graph_id=? ORDER BY e.seq,n.edge", (graph_id,),
    ).fetchall()
    by_event: dict[str, list[dict[str, Any]]] = {}
    notifications = []
    for row in rows:
        item = notification(row)
        notifications.append(item)
        by_event.setdefault(row["event_id"], []).append(item)
    counts: dict[str, int] = {}
    active: dict[tuple[str, int], dict[str, Any]] = {}
    for event in db.execute("SELECT * FROM flag_events WHERE graph_id=? ORDER BY seq", (graph_id,)):
        node = event["node_id"]
        if event["action"] == "set":
            counts[node] = counts.get(node, 0) + 1
            active = {key: item for key, item in active.items()
                      if key[0] != node or key[1] > counts[node]}
        for item in by_event.get(event["event_id"], []):
            if item["kind"] == "turn":
                key = (item["node"], item["round"])
                assignment = active.setdefault(key, {
                    "activationId": activation_id(graph_id, *key), "nodeId": key[0],
                    "owner": item["recipient"], "round": key[1], "requests": {},
                })
                # Same edge may request an unfinished round repeatedly. It is
                # still one assignment; the newest request supersedes that edge.
                assignment["requests"][item["edge"]] = item
            elif item["kind"] == "withdraw":
                for assignment in active.values():
                    assignment["requests"].pop(item["edge"], None)
                active = {key: value for key, value in active.items() if value["requests"]}
    for row in db.execute(
        "SELECT n.* FROM notifications n WHERE NOT EXISTS "
        "(SELECT 1 FROM flag_events e WHERE e.event_id=n.event_id) "
        "AND (json_extract(n.plan_json,'$.graphId')=? OR "
        "(n.plan_json IS NULL AND substr(n.event_id,1,?)=?)) ORDER BY n.event_id,n.edge",
        (graph_id, len(f"overdue:{graph_id}:"), f"overdue:{graph_id}:"),
    ):
        notifications.append(notification(row))
    assignments = [
        {**value, "requests": sorted(value["requests"].values(), key=lambda r: (r["eventId"], r["edge"]))}
        for _, value in sorted(active.items())
    ]
    return assignments, counts, sorted(notifications, key=lambda r: (r["eventId"], r["edge"]))


def snapshot(path: Path, graph_id: str) -> dict[str, Any]:
    with read_transaction(path) as db:
        graph = _head(db, graph_id)  # first read pins the SQLite snapshot
        structure, nodes = _structure(db, graph)
        assignments, counts, notifications = _requests_and_counts(db, graph_id)
        journal_id, high, floor = _watermark(db, graph_id)
        closed = ({"at": graph["closed_at"], "by": graph["closed_by"]}
                  if graph["closed_at"] is not None else None)
        actor_names = {n["actor_name"] for n in nodes if n["actor_name"] is not None}
        edges = structure["edges"]

        def blocked_on(node: sqlite3.Row) -> str:
            if closed is not None or bool(node["flag"]):
                return "none"
            predecessors = [edge["from"] for edge in edges
                            if edge["kind"] == "forward" and edge["to"] == node["node_id"]]
            if not all(next(bool(item["flag"]) for item in nodes if item["node_id"] == pred)
                       for pred in predecessors):
                return "none"
            return "agent" if node["owner"] in actor_names or node["requires_json"] is not None else "human"

        blocked = {node["node_id"]: blocked_on(node) for node in nodes}
        assignments = [
            {**item, "blockedOn": blocked[item["nodeId"]]}
            for item in assignments
        ]
        actors = []
        for node in nodes:
            if node["kind"] != "actor":
                continue
            activation = db.execute(
                "SELECT * FROM actor_activations WHERE graph_id=? AND node_id=?",
                (graph_id, node["node_id"]),
            ).fetchone()
            actors.append({
                "nodeId": node["node_id"],
                "actorName": node["actor_name"],
                "launchRef": node["launch_ref"],
                "activation": (
                    {
                        "incarnation": activation["incarnation"],
                        "desired": activation["desired"],
                        "op": activation["op"],
                        "effectId": activation["effect_id"],
                        "operationId": activation["operation_id"],
                        "identityMarker": activation["identity_marker"],
                        "daemonEpoch": activation["daemon_epoch"],
                    }
                    if activation is not None
                    else None
                ),
            })
        return {
            "schemaVersion": 1, "type": "snapshot", "graphId": graph_id,
            "journalId": journal_id, "cursor": high, "cursorFloor": floor,
            "version": graph["version"], "structure": structure,
            "flags": {n["node_id"]: {
                "flag": bool(n["flag"]), "setBy": n["flag_set_by"], "setAt": n["flag_set_at"],
                "reasonRef": n["flag_reason_ref"], "completedCount": counts.get(n["node_id"], 0),
            } for n in nodes},
            "active": ({"at": graph["activated_at"], "by": graph["activated_by"]}
                       if graph["activated_at"] is not None else None),
            "closed": closed,
            "actors": actors,
            "blockedOn": blocked,
            "assignments": [] if closed is not None else assignments,
            "notifications": notifications,
        }


def events_since(path: Path, graph_id: str, after: int, *, journal_id: str | None = None,
                 limit: int = 256, until: int | None = None) -> dict[str, Any]:
    """A bounded consistent page; seq is global, ordered, and may have gaps."""
    with read_transaction(path) as db:
        _head(db, graph_id)
        identity, high, floor = _watermark(db, graph_id)
        if after < floor or after > high or (journal_id is not None and journal_id != identity):
            raise PacError(PAC_EVENTS_RESYNC, "cursor expired or belongs to another journal; take a snapshot",
                           {"resync": True, "journalId": identity, "cursorFloor": floor, "cursor": high})
        high = min(high, until) if until is not None else high
        rows = db.execute("SELECT * FROM journal WHERE graph_id=? AND seq>? AND seq<=? ORDER BY seq LIMIT ?",
                          (graph_id, after, high, limit)).fetchall()
        events = [{**envelope(row), "journalId": identity} for row in rows]
        return {"events": events, "journalId": identity, "highWatermark": high,
                "cursor": events[-1]["seq"] if len(events) == limit else high}
