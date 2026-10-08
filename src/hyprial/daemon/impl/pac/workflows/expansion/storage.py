"""PAC expansion storage helpers."""

from __future__ import annotations

import json
from typing import Any, Iterable


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def binding_for_parent(db, graph_id: str, node_id: str):
    return db.execute(
        "SELECT * FROM workflow_expansions "
        "WHERE parent_graph_id=? AND parent_node_id=?",
        (graph_id, node_id),
    ).fetchone()


def insert_binding(
    db,
    *,
    parent_graph_id: str,
    parent_node_id: str,
    child_graph_id: str,
    planner_node_id: str,
    planner_request_id: str,
    expansion_digest: str,
    limits: dict[str, Any],
    accepted_at: int,
    deadline_ms: int,
) -> None:
    db.execute(
        "INSERT INTO workflow_expansions "
        "(parent_graph_id,parent_node_id,child_graph_id,planner_node_id,"
        "planner_request_id,generation,expansion_digest,limits_json,accepted_at,"
        "deadline_ms) VALUES (?,?,?,?,?,1,?,?,?,?)",
        (
            parent_graph_id,
            parent_node_id,
            child_graph_id,
            planner_node_id,
            planner_request_id,
            expansion_digest,
            canonical_json(limits),
            accepted_at,
            deadline_ms,
        ),
    )


def insert_worktree_plans(
    db, plans: Iterable[dict[str, Any]], *, at: int
) -> None:
    for plan in plans:
        db.execute(
            "INSERT INTO workflow_worktrees "
            "(graph_id,node_id,actor_node,plan_json,state,reason,updated_at) "
            "VALUES (?,?,?,?,'planned',NULL,?)",
            (
                plan["graphId"],
                plan["nodeId"],
                plan["actorNode"],
                canonical_json(plan),
                at,
            ),
        )


__all__ = [
    "binding_for_parent",
    "canonical_json",
    "insert_binding",
    "insert_worktree_plans",
]
