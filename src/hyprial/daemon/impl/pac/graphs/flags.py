"""Transaction-local PAC flag publication helpers."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from hyprial.identity import PacError
from hyprial.daemon.impl.pac.storage.journal import append_event
from hyprial.daemon.impl.pac.workflows.graphs import input_token_from_connection


def write_flag_in_transaction(
    store,
    *,
    graph: dict[str, Any],
    node_id: str,
    action: str,
    actor: str,
    at: int,
    reason_ref: str | None,
    output_text: str | None = None,
    expected_request: str | None = None,
    captured_input_token: str | None = None,
) -> dict[str, Any]:
    """Write one flag and its workflow projection in the caller transaction."""

    db = store._db
    if not db.in_transaction:
        raise RuntimeError("flag publication requires caller transaction")
    if action not in {"set", "reset"}:
        raise ValueError(f"unknown flag action: {action}")
    node = store.node(graph["graph_id"], node_id)
    if node is None:
        raise PacError("PAC_GRAPH_NOT_FOUND", f"node {node_id!r} not found")
    if action == "set" and node.flag:
        raise PacError("PAC_FLAG_ALREADY_SET", f"{node_id!r} is already set")
    if action == "reset" and not node.flag:
        raise PacError("PAC_FLAG_NOT_SET", f"{node_id!r} is not set")
    workflow = db.execute(
        "SELECT * FROM workflow_nodes WHERE graph_id=? AND node_id=?",
        (graph["graph_id"], node_id),
    ).fetchone()
    if expected_request is not None:
        current_token = input_token_from_connection(db, graph["graph_id"], node_id)
        if (
            workflow is None
            or workflow["request_id"] != expected_request
            or workflow["state"] != "requested"
            or workflow["input_token"] != current_token
            or (
                captured_input_token is not None
                and workflow["input_token"] != captured_input_token
            )
        ):
            raise PacError(
                "WORKFLOW_REQUEST_STALE",
                "flag belongs to a withdrawn or superseded request",
            )
    event_id = str(uuid4())
    db.execute(
        "INSERT INTO flag_events "
        "(event_id,graph_id,version,node_id,action,actor,at,reason_ref) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            event_id,
            graph["graph_id"],
            graph["version"],
            node_id,
            action,
            actor,
            at,
            reason_ref,
        ),
    )
    if action == "set":
        db.execute(
            "UPDATE nodes SET flag=1,flag_set_by=?,flag_set_at=?,flag_reason_ref=? "
            "WHERE graph_id=? AND node_id=?",
            (actor, at, reason_ref, graph["graph_id"], node_id),
        )
    else:
        db.execute(
            "UPDATE nodes SET flag=0,flag_set_by=NULL,flag_set_at=NULL,"
            "flag_reason_ref=NULL WHERE graph_id=? AND node_id=?",
            (graph["graph_id"], node_id),
        )
    append_event(
        db,
        graph_id=graph["graph_id"],
        version=graph["version"],
        type=f"flag_{action}",
        at=at,
        event_id=event_id,
        data={
            "nodeId": node_id,
            "action": action,
            "actor": actor,
            "reasonRef": reason_ref,
        },
    )
    db.execute(
        "UPDATE workflow_nodes SET state=?,reason_ref=?,output_text=?,"
        "request_id=CASE WHEN ?='reset' THEN NULL ELSE request_id END,"
        "input_token=CASE WHEN ?='reset' THEN NULL ELSE input_token END "
        "WHERE graph_id=? AND node_id=?",
        (
            "done" if action == "set" else "pending",
            reason_ref,
            output_text if action == "set" else None,
            action,
            action,
            graph["graph_id"],
            node_id,
        ),
    )
    return {
        "eventId": event_id,
        "graphId": graph["graph_id"],
        "nodeId": node_id,
        "action": action,
        "actor": actor,
        "at": at,
        "version": graph["version"],
    }


__all__ = ["write_flag_in_transaction"]
