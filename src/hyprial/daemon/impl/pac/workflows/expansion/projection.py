"""PAC expansion state projection helpers."""

from __future__ import annotations

import json
from typing import Any

from hyprial.daemon.impl.pac.graphs.flags import write_flag_in_transaction
from hyprial.daemon.impl.pac.storage.journal import append_event
from hyprial.daemon.impl.pac.workflows.expansion.results import child_result_summary
from hyprial.daemon.impl.pac.workflows.graphs import input_token_from_connection


def placeholder_projection(db, graph_id: str, node_id: str) -> dict[str, Any]:
    binding = db.execute(
        "SELECT e.*,w.state AS child_state FROM workflow_expansions e "
        "JOIN workflow_graphs w ON w.graph_id=e.child_graph_id "
        "WHERE e.parent_graph_id=? AND e.parent_node_id=?",
        (graph_id, node_id),
    ).fetchone()
    if binding is None:
        return {
            "kind": "expansion",
            "systemOwned": True,
            "childGraphId": None,
            "childState": None,
            "expansionDigest": None,
        }
    children = [
        {
            "nodeId": row["node_id"],
            "state": row["state"],
            "reasonRef": row["reason_ref"],
        }
        for row in db.execute(
            "SELECT node_id,state,reason_ref FROM workflow_nodes "
            "WHERE graph_id=? ORDER BY node_id LIMIT 100",
            (binding["child_graph_id"],),
        )
    ]
    cleanup = [
        {"nodeId": row["node_id"], "state": row["state"], "reason": row["reason"]}
        for row in db.execute(
            "SELECT node_id,state,reason FROM workflow_worktrees "
            "WHERE graph_id=? ORDER BY node_id LIMIT 100",
            (binding["child_graph_id"],),
        )
    ]
    return {
        "kind": "expansion",
        "systemOwned": True,
        "childGraphId": binding["child_graph_id"],
        "childState": binding["child_state"],
        "expansionDigest": binding["expansion_digest"],
        "deadlineMs": binding["deadline_ms"],
        "childNodes": children,
        **({"worktreeCleanup": cleanup} if cleanup else {}),
    }


def project_child_terminals_in_transaction(store, graph: dict, *, at: int) -> None:
    """Fold terminal child facts into their system placeholder exactly once."""

    from hyprial.daemon.impl.pac.workflows.runtime.types import close_workflow

    db = store._db
    for binding in db.execute(
        "SELECT e.*,w.state AS child_state,w.reason_ref AS child_reason "
        "FROM workflow_expansions e JOIN workflow_graphs w "
        "ON w.graph_id=e.child_graph_id WHERE e.parent_graph_id=?",
        (graph["graph_id"],),
    ).fetchall():
        placeholder = db.execute(
            "SELECT * FROM workflow_nodes WHERE graph_id=? AND node_id=?",
            (graph["graph_id"], binding["parent_node_id"]),
        ).fetchone()
        if placeholder is None or placeholder["state"] != "requested":
            continue
        planner = db.execute(
            "SELECT input_token FROM workflow_nodes WHERE graph_id=? AND node_id=?",
            (graph["graph_id"], binding["planner_node_id"]),
        ).fetchone()
        current_planner_token = input_token_from_connection(
            db, graph["graph_id"], binding["planner_node_id"]
        )
        if planner is None or planner["input_token"] != current_planner_token:
            child = store.graph(binding["child_graph_id"])
            if child is None:
                raise RuntimeError(
                    "expansion child graph disappeared during ancestor invalidation"
                )
            if binding["child_state"] not in {"completed", "failed", "cancelled"}:
                close_workflow(
                    store,
                    child,
                    state="cancelled",
                    reason="pac:ancestor-invalidated",
                    at=at,
                    closed_by=graph["graph_id"],
                )
            db.execute(
                "UPDATE workflow_nodes SET state='failed',reason_ref=? "
                "WHERE graph_id=? AND node_id=?",
                (
                    "pac:ancestor-invalidated",
                    graph["graph_id"],
                    binding["parent_node_id"],
                ),
            )
            continue
        if at > binding["deadline_ms"] and binding["child_state"] not in {
            "completed",
            "failed",
            "cancelled",
        }:
            child = store.graph(binding["child_graph_id"])
            if child is None:
                raise RuntimeError(
                    "expansion child graph disappeared while deadline expired"
                )
            close_workflow(
                store,
                child,
                state="cancelled",
                reason="pac:deadline-expired",
                at=at,
                closed_by=graph["graph_id"],
            )
            db.execute(
                "UPDATE workflow_nodes SET state='failed',reason_ref=? "
                "WHERE graph_id=? AND node_id=?",
                (
                    "pac:deadline-expired",
                    graph["graph_id"],
                    binding["parent_node_id"],
                ),
            )
            continue
        if binding["child_state"] == "completed":
            rows = [
                dict(row)
                for row in db.execute(
                    "SELECT node_id AS nodeId,state,reason_ref AS reasonRef,"
                    "output_text AS outputText FROM workflow_nodes "
                    "WHERE graph_id=? ORDER BY node_id",
                    (binding["child_graph_id"],),
                )
            ]
            plans = [
                {
                    **json.loads(row["plan_json"]),
                    "nodeId": row["node_id"],
                }
                for row in db.execute(
                    "SELECT node_id,plan_json FROM workflow_worktrees "
                    "WHERE graph_id=? ORDER BY node_id",
                    (binding["child_graph_id"],),
                )
            ]
            summary = child_result_summary(rows, worktrees=plans)
            node = store.node(graph["graph_id"], binding["parent_node_id"])
            if node is None:
                raise RuntimeError(
                    "expansion placeholder disappeared while projecting completion"
                )
            write_flag_in_transaction(
                store,
                graph=graph,
                node_id=binding["parent_node_id"],
                action="set",
                actor=node.owner,
                at=at,
                reason_ref=(
                    f"workflow:{binding['child_graph_id']}#completed;"
                    f"sha256:{summary['digest']}"
                ),
                output_text=summary["text"],
                expected_request=placeholder["request_id"],
                captured_input_token=placeholder["input_token"],
            )
        elif binding["child_state"] in {"failed", "cancelled"}:
            db.execute(
                "UPDATE workflow_nodes SET state='failed',reason_ref=? "
                "WHERE graph_id=? AND node_id=?",
                (
                    binding["child_reason"]
                    or f"pac:child-{binding['child_state']}",
                    graph["graph_id"],
                    binding["parent_node_id"],
                ),
            )


def cascade_open_children_in_transaction(
    store, parent_graph_id: str, *, reason: str, at: int
) -> None:
    db = store._db
    for row in db.execute(
        "SELECT e.child_graph_id,g.version FROM workflow_expansions e "
        "JOIN graphs g ON g.graph_id=e.child_graph_id WHERE e.parent_graph_id=?",
        (parent_graph_id,),
    ).fetchall():
        child = row["child_graph_id"]
        db.execute(
            "UPDATE workflow_graphs SET state='cancelled',reason_ref=? "
            "WHERE graph_id=? AND state NOT IN ('completed','failed','cancelled')",
            (reason, child),
        )
        if db.execute("SELECT changes()").fetchone()[0]:
            db.execute(
                "UPDATE workflow_nodes SET state='cancelled',reason_ref=? "
                "WHERE graph_id=? AND state IN ('pending','requested')",
                (reason, child),
            )
            db.execute(
                "UPDATE graphs SET closed_at=?,closed_by=? "
                "WHERE graph_id=? AND closed_at IS NULL",
                (at, parent_graph_id, child),
            )
            append_event(
                db,
                graph_id=child,
                version=row["version"],
                type="graph_closed",
                at=at,
                data={
                    "at": at,
                    "by": parent_graph_id,
                    "state": "cancelled",
                    "reasonRef": reason,
                },
            )
            db.execute(
                "UPDATE workflow_worktrees SET state='cleanup_pending',updated_at=? "
                "WHERE graph_id=? AND state IN ('planned','prepared')",
                (at, child),
            )


__all__ = [
    "cascade_open_children_in_transaction",
    "placeholder_projection",
    "project_child_terminals_in_transaction",
]
