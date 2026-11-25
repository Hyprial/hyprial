"""Detection and notice planning for held-overdue PAC graphs (#1803).

Everything here is a read of PAC's own durable facts plus the one write the
feature owns: the recorded notification row.  No clock of its own, no second
store, and no memory of what was already sent -- the ``notifications`` table is
the record, and the deterministic notice id is the idempotency key.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
from typing import Any

from hyprial.daemon.impl.pac.graphs.reactor import PlannedNotification
from hyprial.daemon.impl.pac.storage.journal import append_event
from hyprial.daemon.impl.pac.storage.store import PacGraphStore

#: Every held reminder's event id starts here; the prefix is also the durable
#: marker the "was this graph already reminded?" window query ranges over.
HELD_REMINDER_EVENT_PREFIX = "held-reminder:"

#: One edge name for every held reminder: the reminder is per (graph, window),
#: not per node or per recipient, so the ``(event_id, edge)`` primary key makes
#: a replay a no-op.
HELD_REMINDER_EDGE = "held-reminder"

#: The notice kind reuses PAC's existing alert kind: the table's CHECK
#: constraint enumerates kinds, and a reminder is exactly a durable alert to a
#: principal that no flag event caused.
HELD_REMINDER_KIND = "actor_alert"

_OPTIONS = (
    "reset the failed node and retry",
    "cancel the graph to reclaim its workers",
    "raise the held-reminder threshold",
)


@dataclass(frozen=True, slots=True)
class BlockingNode:
    node_id: str
    state: str
    reason_ref: str | None


@dataclass(frozen=True, slots=True)
class HeldGraph:
    """One graph that has been ``held`` past the threshold."""

    graph_id: str
    version: int
    created_by: str
    held_at_ms: int | None
    held_age_ms: int | None
    reason_ref: str | None
    blocking: tuple[BlockingNode, ...]
    #: Workers whose activation is a settled ``up`` -- durable desired state,
    #: never proof of a running process (PAC keeps no liveness observation).
    workers_desired_up: tuple[str, ...]
    primary_node_id: str


def held_duration_text(age_ms: int | None) -> str:
    """A short human duration; unknown is said, never guessed."""

    if age_ms is None:
        return "an unknown duration"
    minutes = max(0, int(age_ms)) // 60_000
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h" if minutes == 0 else f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d" if hours == 0 else f"{days}d {hours}h"


def _has_held_column(db: sqlite3.Connection) -> bool:
    """Read-only compatibility: a pre-v20 database has no ``held_at_ms``.

    Such a database cannot date a hold; the reminder then treats the graph as
    overdue with an unknown age instead of refusing to look.
    """

    return any(
        str(row[1]) == "held_at_ms"
        for row in db.execute("PRAGMA table_info(workflow_graphs)")
    )


def _graph_facts(
    db: sqlite3.Connection, graph_id: str
) -> tuple[tuple[BlockingNode, ...], tuple[str, ...], str]:
    blocking = tuple(
        BlockingNode(str(row["node_id"]), str(row["state"]), row["reason_ref"])
        for row in db.execute(
            "SELECT node_id,state,reason_ref FROM workflow_nodes "
            "WHERE graph_id=? AND state IN ('failed','blocked') ORDER BY node_id",
            (graph_id,),
        )
    )
    # Desired state only: ``desired='up' AND op='done'`` is a settled
    # activation, which says nothing about a running process.  Restore
    # suppression does not clear it, so calling this "live" would report a
    # dormant worker as running.
    workers_desired_up = tuple(
        str(row[0])
        for row in db.execute(
            "SELECT n.actor_name FROM nodes n LEFT JOIN actor_activations a "
            "ON a.graph_id=n.graph_id AND a.node_id=n.node_id "
            "WHERE n.graph_id=? AND n.kind='actor' AND n.actor_name IS NOT NULL "
            "AND a.desired='up' AND a.op='done' ORDER BY n.actor_name",
            (graph_id,),
        )
    )
    row = db.execute(
        "SELECT node_id FROM nodes WHERE graph_id=? ORDER BY node_id LIMIT 1",
        (graph_id,),
    ).fetchone()
    return blocking, workers_desired_up, ("" if row is None else str(row[0]))


def scan_held_graphs(
    db: sqlite3.Connection, *, now_ms: int, threshold_ms: int
) -> list[HeldGraph]:
    """Every open graph held at least ``threshold_ms``, oldest id first."""

    dated = _has_held_column(db)
    column = "w.held_at_ms" if dated else "NULL"
    rows = db.execute(
        f"SELECT g.graph_id,g.version,g.created_by,w.reason_ref,{column} AS held_at_ms "
        "FROM workflow_graphs w JOIN graphs g ON g.graph_id=w.graph_id "
        "WHERE w.state='held' AND g.closed_at IS NULL ORDER BY g.graph_id"
    ).fetchall()
    held: list[HeldGraph] = []
    for row in rows:
        held_at = None if row["held_at_ms"] is None else int(row["held_at_ms"])
        age = None if held_at is None else max(0, int(now_ms) - held_at)
        # An undated hold predates the column: it is overdue by construction.
        if age is not None and age < threshold_ms:
            continue
        blocking, workers_desired_up, primary = _graph_facts(
            db, str(row["graph_id"])
        )
        if not primary:
            # A graph without nodes has nothing to remind about.
            continue
        held.append(
            HeldGraph(
                graph_id=str(row["graph_id"]),
                version=int(row["version"]),
                created_by=str(row["created_by"]),
                held_at_ms=held_at,
                held_age_ms=age,
                reason_ref=row["reason_ref"],
                blocking=blocking,
                workers_desired_up=workers_desired_up,
                primary_node_id=blocking[0].node_id if blocking else primary,
            )
        )
    return held


def reminder_notice_at(
    db: sqlite3.Connection, graph_id: str
) -> int | None:
    """When this graph was last reminded, or ``None``.

    Ranged over the primary key instead of ``LIKE``: the notice ids share the
    ``held-reminder:<graph>:`` prefix, and ``:`` + 1 == ``;`` bounds the range
    so graph ``g1`` can never match ``g10``.
    """

    lower = f"{HELD_REMINDER_EVENT_PREFIX}{graph_id}:"
    upper = f"{HELD_REMINDER_EVENT_PREFIX}{graph_id};"
    row = db.execute(
        "SELECT MAX(at) FROM notifications WHERE event_id >= ? AND event_id < ?",
        (lower, upper),
    ).fetchone()
    return None if row is None or row[0] is None else int(row[0])


def held_notice_text(
    held: HeldGraph,
    *,
    creator_missing: bool,
    operator_route: str | None,
) -> str:
    blocking = "; ".join(
        f"{item.node_id} ({item.state}"
        + (f", {item.reason_ref}" if item.reason_ref else "")
        + ")"
        for item in held.blocking
    ) or "none recorded"
    workers = ", ".join(held.workers_desired_up) or "none"
    lines = [
        f"PAC held-reminder: graph {held.graph_id} has been held for "
        f"{held_duration_text(held.held_age_ms)}"
        + (
            f" (since {held.held_at_ms})."
            if held.held_at_ms is not None
            else " (held before hold tracking existed; the exact age is unknown)."
        ),
        f"Blocking nodes: {blocking}.",
        "Workers with a settled up activation (durable desired state, not "
        f"observed liveness): {workers}.",
        "Options: " + "; ".join(_OPTIONS) + ".",
        "This is a reminder only: PAC did not reset or cancel the graph.",
    ]
    if creator_missing:
        lines.insert(
            1,
            f"Creator {held.created_by} is missing on this node; delivered to "
            f"the local node operator route {operator_route}.",
        )
    return "\n".join(lines)


def plan_held_notice(
    held: HeldGraph,
    *,
    now_ms: int,
    repeat_window_ms: int,
    threshold_ms: int,
    creator_missing: bool,
    operator_route: str | None,
) -> PlannedNotification:
    """The one recorded notification a held-overdue graph is owed."""

    event_id = (
        f"{HELD_REMINDER_EVENT_PREFIX}{held.graph_id}:"
        f"{int(now_ms) // repeat_window_ms}"
    )
    recipient = operator_route if creator_missing and operator_route else held.created_by
    plan: dict[str, Any] = {
        "graphId": held.graph_id,
        "version": held.version,
        "nodeId": held.primary_node_id,
        "heldReminder": {
            "noticeId": event_id,
            "heldSinceMs": held.held_at_ms,
            "heldAgeMs": held.held_age_ms,
            "thresholdMs": threshold_ms,
            "repeatWindowMs": repeat_window_ms,
            "creator": held.created_by,
            "creatorMissing": creator_missing,
            "blockingNodes": [
                {
                    "nodeId": item.node_id,
                    "state": item.state,
                    "reasonRef": item.reason_ref,
                }
                for item in held.blocking
            ],
            "workersDesiredUp": list(held.workers_desired_up),
            "options": list(_OPTIONS),
        },
    }
    return PlannedNotification(
        event_id=event_id,
        edge=HELD_REMINDER_EDGE,
        kind=HELD_REMINDER_KIND,
        recipient=str(recipient),
        node_id=held.primary_node_id,
        round_no=None,
        text=held_notice_text(
            held, creator_missing=creator_missing, operator_route=operator_route
        ),
        sender=held.created_by,
        plan=plan,
    )


def record_held_notices(
    store: PacGraphStore,
    notices: list[PlannedNotification],
    *,
    now_ms: int,
) -> list[str]:
    """Persist the notices in one transaction; return the newly recorded ids.

    The row insert and its ``notification_planned`` fact happen together, so a
    crash between them cannot produce a row nobody can account for.  A replay
    of the same ``(event_id, edge)`` inserts nothing and journals nothing.
    """

    from hyprial.daemon.impl.pac.graphs.reactor import planned_to_json

    db = store.write()
    recorded: list[str] = []
    try:
        still_held = {
            str(row[0])
            for row in db.execute("SELECT graph_id FROM workflow_graphs WHERE state='held'")
        }
        for notice in notices:
            plan = dict(notice.plan or {})
            graph_id = str(plan["graphId"])
            if graph_id not in still_held:
                continue
            cursor = db.execute(
                "INSERT OR IGNORE INTO notifications "
                "(event_id, edge, kind, recipient, round_no, text, sender, at, plan_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    notice.event_id,
                    notice.edge,
                    notice.kind,
                    notice.recipient,
                    None,
                    notice.text,
                    notice.sender,
                    now_ms,
                    json.dumps(plan, sort_keys=True),
                ),
            )
            if cursor.rowcount <= 0:
                continue
            append_event(
                db,
                graph_id=graph_id,
                version=int(plan["version"]),
                type="notification_planned",
                at=now_ms,
                data={
                    **planned_to_json(notice),
                    "sender": notice.sender,
                    "at": now_ms,
                    "messageId": None,
                    "deliveredAt": None,
                },
            )
            recorded.append(notice.event_id)
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return recorded
