"""Remote PAC expansion custody and preview helpers."""

from __future__ import annotations

import json
from typing import Any

from hyprial.daemon.impl.pac.contracts.expansion.document import (
    MAX_EXPANSION_TEXT,
    expansion_digest,
)
from hyprial.identity import PAC_EXPANSION_INVALID, PacError


def _invalid(field: str, reason: str, message: str) -> None:
    raise PacError(PAC_EXPANSION_INVALID, message, {"field": field, "reason": reason})


def expansion_outcome_fields(raw: str, *, attempt_no: int) -> dict[str, Any]:
    if type(attempt_no) is not int or attempt_no < 0:
        _invalid("attemptNo", "count", "remote expansion attempt must be nonnegative")
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_EXPANSION_TEXT:
        _invalid("expansion", "transport-size", "remote expansion exceeds 65536 UTF-8 bytes")
    return {
        "expansion": raw,
        "expansionDigest": expansion_digest(raw),
        "attemptNo": attempt_no,
    }


def validate_expansion_outcome(value: dict[str, Any]) -> str:
    raw = value.get("expansion")
    attempt = value.get("attemptNo")
    if type(attempt) is not int or attempt < 0:
        _invalid("attemptNo", "count", "remote expansion attempt must be nonnegative")
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_EXPANSION_TEXT:
        _invalid("expansion", "transport-size", "remote expansion exceeds 65536 UTF-8 bytes")
    digest = value.get("expansionDigest")
    if not isinstance(digest, str) or digest != expansion_digest(raw):
        _invalid("expansionDigest", "transport-size", "remote expansion digest mismatch")
    return raw


def enqueue_outcome_row(
    db,
    *,
    request_id: str,
    action: str,
    reason: str,
    output_text: str | None,
    expansion_text: str | None,
    expansion_digest_value: str | None,
) -> None:
    """Insert an outcome or correct only a definitive invalid expansion."""

    row = db.execute(
        "SELECT * FROM remote_workflow_outbox WHERE request_id=?", (request_id,)
    ).fetchone()
    desired = (
        action,
        reason,
        output_text,
        expansion_text,
        expansion_digest_value,
    )
    if row:
        existing = (
            row["action"], row["reason_ref"], row["output_text"],
            row["expansion_text"], row["expansion_digest"],
        )
        if existing == desired:
            return
        prior = json.loads(row["result_json"]) if row["result_json"] else None
        if not (
            expansion_text is not None
            and prior
            and prior.get("returnState") == "rejected"
            and prior.get("error", {}).get("code") == PAC_EXPANSION_INVALID
        ):
            raise PacError(
                "WORKFLOW_OUTCOME_CONFLICT",
                "request already has a queued or accepted outcome",
            )
        db.execute(
            "UPDATE remote_workflow_outbox SET action=?,reason_ref=?,output_text=?,"
            "expansion_text=?,expansion_digest=?,attempt_no=attempt_no+1,"
            "result_json=NULL WHERE request_id=?",
            (*desired, request_id),
        )
        return
    db.execute(
        "INSERT INTO remote_workflow_outbox"
        "(request_id,action,reason_ref,result_json,output_text,expansion_text,"
        "expansion_digest,attempt_no) VALUES (?,?,?,NULL,?,?,?,0)",
        (request_id, action, reason, output_text, expansion_text, expansion_digest_value),
    )


def preview_origin_expansion(app, database, grant, data) -> dict[str, Any]:
    """Preview on the graph origin under the exact signed planner grant."""

    from time import time_ns

    from hyprial.daemon.impl.pac.graphs.authority.expansion import (
        expansion_preview,
        prepare_expansion,
    )
    from hyprial.daemon.impl.pac.storage.store import PacGraphStore
    from hyprial.daemon.impl.pac.workflows.expansion.transactions import (
        expansion_deadline_ms,
    )

    placeholder = data.get("nodeId")
    raw = data.get("yaml")
    if not isinstance(placeholder, str) or not isinstance(raw, str):
        _invalid("placeholder", "required", "remote preview target and YAML are required")
    now = time_ns() // 1_000_000
    authority = getattr(app, "_pac_graph_authority", None)
    prepare = (
        authority.prepare_expansion if authority is not None else prepare_expansion
    )
    context = prepare(
        database=database,
        home=app.hyprial_home,
        raw=raw,
        graph_id=grant["graphId"],
        node_id=grant["nodeId"],
        request_id=grant["requestId"],
        actor=grant["owner"],
        at=now,
        machine=app.node_id,
        local_owner=app.owner,
        persist_artifacts=False,
    )
    if context.placeholder_node_id != placeholder:
        _invalid("placeholder", "unexpected", "remote preview target is not planner-bound")
    app._workflow_admit(context.spec, grant["owner"])
    store = PacGraphStore(database, read_only=True)
    try:
        row = store._db.execute(
            "SELECT deadline_ms,timeout_ms FROM workflow_nodes "
            "WHERE graph_id=? AND node_id=?",
            (grant["graphId"], placeholder),
        ).fetchone()
        if now > int(grant["deadlineMs"]):
            raise PacError(
                "WORKFLOW_DEADLINE_EXPIRED", "the planner deadline has passed"
            )
        deadline_ms = expansion_deadline_ms(
            authored_deadline_ms=row["deadline_ms"],
            timeout_ms=int(row["timeout_ms"]),
            at=now,
        )
    finally:
        store.close()
    return expansion_preview(context, deadline_ms=deadline_ms)


def forward_expansion_preview(remote, params, caller):
    grant = remote._lookup(graph_id=params.get("graphId"), actor=caller)
    if grant is None:
        return None
    if "pac-expansion-v1" not in grant.get("features", ()):
        raise PacError(
            "WORKFLOW_REMOTE_INVALID",
            "remote peer does not support PAC expansion",
            {"field": "expansion", "reason": "unsupported-peer"},
        )
    return remote.wire.call(
        grant["origin"],
        "expansion_preview",
        {
            "grant": grant,
            "nodeId": params.get("nodeId"),
            "yaml": params.get("yaml"),
        },
    )


__all__ = [
    "enqueue_outcome_row",
    "expansion_outcome_fields",
    "forward_expansion_preview",
    "preview_origin_expansion",
    "validate_expansion_outcome",
]
