"""PAC expansion result and replay helpers."""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Iterable

from hyprial.identity import PacError


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def child_result_summary(
    nodes: Iterable[dict[str, Any]],
    *,
    worktrees: Iterable[dict[str, Any]] = (),
    maximum_bytes: int = 65_536,
) -> dict[str, Any]:
    """Return a bounded human summary plus a digest of the complete result.

    The digest covers full canonical values.  Human-facing output heads are
    deliberately bounded and never become the replay or completion authority.
    """

    if type(maximum_bytes) is not int or maximum_bytes < 128:
        raise ValueError("maximum_bytes must be an integer of at least 128")
    plans = {
        str(plan.get("nodeId")): plan
        for plan in worktrees
        if isinstance(plan, dict) and isinstance(plan.get("nodeId"), str)
    }
    complete = []
    projected = []
    for row in sorted(nodes, key=lambda item: str(item.get("nodeId", ""))):
        node_id = str(row.get("nodeId", ""))
        plan = plans.get(node_id, {})
        full = {
            "nodeId": node_id,
            "state": row.get("state"),
            "reasonRef": row.get("reasonRef"),
            "output": row.get("outputText"),
            "branch": plan.get("branch"),
            "managedPath": plan.get("path"),
            "effectiveCwd": plan.get("effectiveCwd"),
        }
        complete.append(full)
        projected.append(
            {
                key: value
                for key, value in {
                    **full,
                    "output": (
                        full["output"][:256]
                        if isinstance(full["output"], str)
                        else None
                    ),
                }.items()
                if value is not None
            }
        )
    digest = sha256(_canonical(complete).encode("utf-8")).hexdigest()
    prefix = f"Child result sha256:{digest}\n"
    lines = [prefix.rstrip("\n")]
    for item in projected:
        line = _canonical(item)
        candidate = "\n".join((*lines, line))
        if len(candidate.encode("utf-8")) > maximum_bytes:
            break
        lines.append(line)
    text = "\n".join(lines)
    if len(text.encode("utf-8")) > maximum_bytes:
        text = text.encode("utf-8")[:maximum_bytes].decode("utf-8", "ignore")
    return {"digest": digest, "nodes": projected, "text": text}


def accepted_replay_event(
    db,
    *,
    graph_id: str,
    node_id: str,
    request_id: str | None,
    actor: str,
    reason_ref: str | None,
    output_text: str | None,
    expansion_digest: str | None,
) -> dict[str, Any] | None:
    """Resolve an accepted outcome before current-request/deadline checks."""

    if request_id is None:
        return None
    result = accepted_replay_result(
        db,
        graph_id=graph_id,
        node_id=node_id,
        request_id=request_id,
        actor=actor,
        reason_ref=reason_ref,
        output_text=output_text,
        expansion_digest=expansion_digest,
    )
    return None if result is None else result["event"]


def accepted_replay_result(
    db,
    *,
    graph_id: str,
    node_id: str,
    request_id: str | None,
    actor: str,
    reason_ref: str | None,
    output_text: str | None,
    expansion_digest: str | None,
) -> dict[str, Any] | None:
    """Return the exact stored response or reject a changed replay."""

    if request_id is None:
        return None
    row = db.execute(
        "SELECT * FROM workflow_outcome_receipts WHERE request_id=?", (request_id,)
    ).fetchone()
    if row is None:
        return None
    if (
        row["graph_id"] != graph_id
        or row["node_id"] != node_id
        or row["actor"] != actor
        or row["action"] != "complete"
        or row["reason_ref"] != reason_ref
        or row["output_text"] != output_text
        or row["expansion_digest"] != expansion_digest
    ):
        raise PacError(
            "WORKFLOW_OUTCOME_CONFLICT",
            "request already has a different accepted outcome",
        )
    return json.loads(row["result_json"])


__all__ = [
    "accepted_replay_event",
    "accepted_replay_result",
    "child_result_summary",
]
