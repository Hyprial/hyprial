"""PAC expansion authority preflight and command helpers.

Preflight owns bounded filesystem reads and git resolution.  It deliberately
does not create a worktree or mutate SQLite; the graph writer consumes the
returned :class:`PreparedExpansion` and rechecks request-scoped facts.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.pac.actors.worktrees import resolve_base
from hyprial.daemon.impl.pac.contracts.expansion import (
    effective_limits,
    expansion_digest,
    load_expansion_policy,
    validate_expansion,
)
from hyprial.daemon.impl.pac.contracts.expansion.types import PreparedExpansion
from hyprial.daemon.impl.pac.storage.store import PacGraphStore
from hyprial.daemon.impl.pac.workflows.expansion.transactions import (
    child_graph_id,
    expansion_deadline_ms,
)
from hyprial.daemon.impl.pac.workflows.graphs import (
    input_token_from_connection,
    prepare_workflow_artifacts,
    read_specification,
)
from hyprial.identity import PAC_EXPANSION_INVALID, PacError
from hyprial.kernel import PAC_WORKTREE_GIT_TIMEOUT_SECONDS


def _invalid(field: str, reason: str, message: str) -> None:
    raise PacError(PAC_EXPANSION_INVALID, message, {"field": field, "reason": reason})


def _current_limits(snapshot: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Intersect a persisted ceiling with the current trusted ceiling."""

    if snapshot.get("workRoot") != current.get("workRoot"):
        _invalid("policy.workRoot", "cwd", "trusted workRoot changed after parent admission")
    old = snapshot.get("limits")
    new = current.get("limits")
    if not isinstance(old, dict) or not isinstance(new, dict):
        _invalid("policy.limits", "required", "trusted policy snapshot is incomplete")

    old_cwds = [Path(value).resolve(strict=False) for value in old.get("cwdUnder", [])]
    new_cwds = [Path(value).resolve(strict=False) for value in new.get("cwdUnder", [])]
    cwd_under: list[str] = []
    for new_path in new_cwds:
        for old_path in old_cwds:
            try:
                new_path.relative_to(old_path)
            except ValueError:
                continue
            cwd_under.append(str(new_path))
            break
    return {
        "version": 1,
        "workRoot": snapshot["workRoot"],
        "limits": {
            "maxNodes": min(int(old["maxNodes"]), int(new["maxNodes"])),
            "tiers": [value for value in old["tiers"] if value in new["tiers"]],
            "owners": [value for value in old["owners"] if value in new["owners"]],
            "cwdUnder": cwd_under,
        },
    }


def _parent_facts(store: PacGraphStore, graph_id: str, node_id: str, actor: str):
    graph = store.graph(graph_id)
    meta = store._db.execute(
        "SELECT * FROM workflow_graphs WHERE graph_id=?", (graph_id,)
    ).fetchone()
    planner = store._db.execute(
        "SELECT w.*,n.owner FROM workflow_nodes w JOIN nodes n USING(graph_id,node_id) "
        "WHERE w.graph_id=? AND w.node_id=?",
        (graph_id, node_id),
    ).fetchone()
    if graph is None or meta is None or graph["closed_at"] is not None:
        raise PacError("PAC_GRAPH_CLOSED", "the parent workflow is unavailable or closed")
    if planner is None or planner["owner"] != actor:
        raise PacError("WORKFLOW_NOT_OWNER", "only the current planner owner may expand")
    specification = read_specification(meta)
    planner_spec = next(
        (item for item in specification.get("nodes", ()) if item.get("id") == node_id),
        None,
    )
    placeholder_id = planner_spec.get("expands") if isinstance(planner_spec, dict) else None
    if not isinstance(placeholder_id, str):
        _invalid("expansion", "unexpected", "the completed node is not an expansion planner")
    placeholder_spec = next(
        (
            item
            for item in specification.get("nodes", ())
            if item.get("id") == placeholder_id and item.get("kind") == "expansion"
        ),
        None,
    )
    placeholder = store._db.execute(
        "SELECT * FROM workflow_nodes WHERE graph_id=? AND node_id=?",
        (graph_id, placeholder_id),
    ).fetchone()
    if placeholder_spec is None or placeholder is None:
        _invalid("placeholder", "required", "the expansion placeholder is unavailable")
    return graph, planner, placeholder, specification, placeholder_spec, placeholder_id


def prepare_expansion(
    database: Path,
    *,
    home: Path,
    raw: str,
    graph_id: str,
    node_id: str,
    request_id: str,
    actor: str,
    at: int,
    machine: str,
    local_owner: str,
    persist_artifacts: bool = True,
    git_timeout_s: float = PAC_WORKTREE_GIT_TIMEOUT_SECONDS,
) -> PreparedExpansion:
    """Validate and resolve one current planner completion outside the writer."""

    store = PacGraphStore(Path(database), read_only=True)
    try:
        graph, planner, placeholder, document, placeholder_spec, placeholder_id = (
            _parent_facts(store, graph_id, node_id, actor)
        )
        captured = input_token_from_connection(store._db, graph_id, node_id)
        if (
            planner["state"] != "requested"
            or planner["request_id"] != request_id
            or planner["input_token"] != captured
        ):
            raise PacError(
                "WORKFLOW_REQUEST_STALE",
                "expansion belongs to a withdrawn planner request",
            )
        if planner["deadline_ms"] is not None and at > planner["deadline_ms"]:
            raise PacError(
                "WORKFLOW_DEADLINE_EXPIRED", "the planner deadline has passed"
            )
        deadline_ms = expansion_deadline_ms(
            authored_deadline_ms=placeholder["deadline_ms"],
            timeout_ms=int(placeholder["timeout_ms"]),
            at=at,
        )

        snapshot = document.get("expansionPolicy")
        if not isinstance(snapshot, dict):
            _invalid("expansionPolicy", "required", "parent has no trusted policy snapshot")
        policy = _current_limits(snapshot, load_expansion_policy(Path(home)))
        limits = effective_limits(placeholder_spec.get("limits"), policy=policy)
        child_id = child_graph_id(graph_id, placeholder_id)
        spec = validate_expansion(
            raw,
            limits=limits,
            child_graph_id=child_id,
            deadline_ms=deadline_ms,
            at=at,
            name=f"expand-{placeholder_id}",
            escalate_to=document.get("escalateTo"),
        )

        plans: list[dict[str, Any]] = []
        nodes = []
        worktree = limits.get("worktree")
        base_oid: str | None = None
        managed_nodes = [
            node
            for node in spec.nodes
            if node.owner is None
            and node.role == "execute"
            and (node.launch is None or not node.launch.get("cwd"))
        ]
        if any(
            node.launch is None or not node.launch.get("cwd")
            for node in managed_nodes
        ):
            if not isinstance(worktree, dict):
                _invalid("limits.worktree", "required", "managed execute nodes require a worktree policy")
        if managed_nodes and isinstance(worktree, dict):
            base_oid = resolve_base(
                Path(worktree["repo"]),
                worktree["base"],
                timeout_s=min(
                    PAC_WORKTREE_GIT_TIMEOUT_SECONDS,
                    float(git_timeout_s),
                    (deadline_ms - at) / 1000,
                ),
            )
        for node in spec.nodes:
            launch = node.launch
            if node in managed_nodes and isinstance(worktree, dict):
                assert base_oid is not None and isinstance(worktree, dict)
                path = (
                    Path(limits["workRoot"])
                    / ".pac-worktrees"
                    / child_id
                    / node.id
                ).resolve(strict=False)
                assert launch is not None
                effective_cwd = str(launch.get("cwd") or path)
                launch = {**launch, "cwd": effective_cwd}
                plans.append(
                    {
                        "graphId": child_id,
                        "nodeId": node.id,
                        "actorNode": f"_actor.{node.worker}",
                        "repo": worktree["repo"],
                        "baseRef": worktree["base"],
                        "baseOid": base_oid,
                        "branch": f"pac/{child_id}/{node.id}",
                        "path": str(path),
                        "effectiveCwd": effective_cwd,
                        "operationId": f"pac-worktree:{child_id}:{node.id}",
                    }
                )
            nodes.append(replace(node, launch=launch))
        spec = replace(spec, nodes=tuple(nodes))
        artifacts = prepare_workflow_artifacts(store, spec) if persist_artifacts else {}
        return PreparedExpansion(
            parent_graph_id=graph_id,
            planner_node_id=node_id,
            planner_request_id=request_id,
            placeholder_node_id=placeholder_id,
            child_graph_id=child_id,
            expansion_digest=expansion_digest(raw),
            captured_input_token=str(captured),
            effective_limits=limits,
            policy_snapshot=policy,
            spec=spec,
            prepared_artifacts=artifacts,
            worktrees=tuple(plans),
        )
    finally:
        store.close()


def expansion_preview(context: PreparedExpansion, *, deadline_ms: int) -> dict[str, Any]:
    return {
        "ok": True,
        "graphId": context.parent_graph_id,
        "nodeId": context.placeholder_node_id,
        "expansionDigest": context.expansion_digest,
        "deadlineMs": deadline_ms,
        "effectiveLimits": context.effective_limits,
        "plan": context.spec.to_json(),
        "warnings": [
            "explicit cwd bypasses managed worktree isolation"
            for node in context.spec.nodes
            if node.owner is None and node.launch and node.launch.get("cwd")
            and not any(plan["nodeId"] == node.id for plan in context.worktrees)
        ],
    }


__all__ = ["expansion_preview", "prepare_expansion"]
