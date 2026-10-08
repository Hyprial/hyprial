"""Atomic child compilation, binding, planner outcome, and placeholder request."""

from __future__ import annotations

import json
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from hyprial.identity import (
    PAC_EXPANSION_ALREADY_BOUND,
    PAC_EXPANSION_INVALID,
    PacError,
)
from hyprial.daemon.impl.pac.graphs.flags import write_flag_in_transaction
from hyprial.daemon.impl.pac.storage.journal import append_event
from hyprial.daemon.impl.pac.workflows.expansion.storage import (
    binding_for_parent,
    insert_binding,
    insert_worktree_plans,
)
from hyprial.daemon.impl.pac.workflows.graphs import (
    compile_workflow_in_transaction,
    input_token_from_connection,
)


def child_graph_id(parent_graph_id: str, placeholder_node_id: str) -> str:
    identity = f"pac-expand:{parent_graph_id}:{placeholder_node_id}:1"
    return "wf-" + uuid5(NAMESPACE_URL, identity).hex


def expansion_deadline_ms(
    *, authored_deadline_ms: int | None, timeout_ms: int, at: int
) -> int:
    """Return the fixed expansion cap from the authored and relative limits."""

    candidates = [at + int(timeout_ms)]
    if authored_deadline_ms is not None:
        candidates.append(int(authored_deadline_ms))
    deadline_ms = min(candidates)
    if at > deadline_ms:
        raise PacError("WORKFLOW_DEADLINE_EXPIRED", "no expansion time remains")
    return deadline_ms


def accept_prepared_expansion_in_transaction(
    store,
    context,
    *,
    actor: str,
    reason_ref: str | None,
    output_text: str | None,
    at: int,
    machine: str,
    local_owner: str,
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...], dict[str, Any]]:
    """Publish every authoritative expansion fact in the caller transaction."""

    db = store._db
    if not db.in_transaction:
        raise RuntimeError("expansion acceptance requires caller transaction")
    expected_child = child_graph_id(
        context.parent_graph_id, context.placeholder_node_id
    )
    if context.child_graph_id != expected_child:
        raise PacError(
            PAC_EXPANSION_INVALID,
            "preflight child identity mismatch",
            {"field": "childGraphId", "reason": "unexpected"},
        )
    if binding_for_parent(
        db, context.parent_graph_id, context.placeholder_node_id
    ) is not None:
        raise PacError(
            PAC_EXPANSION_ALREADY_BOUND,
            "the expansion placeholder is already bound",
        )
    graph = store.graph(context.parent_graph_id)
    if graph is None or graph["closed_at"] is not None:
        raise PacError("PAC_GRAPH_CLOSED", "the parent graph is closed")
    planner = db.execute(
        "SELECT * FROM workflow_nodes WHERE graph_id=? AND node_id=?",
        (context.parent_graph_id, context.planner_node_id),
    ).fetchone()
    placeholder = db.execute(
        "SELECT * FROM workflow_nodes WHERE graph_id=? AND node_id=?",
        (context.parent_graph_id, context.placeholder_node_id),
    ).fetchone()
    if (
        planner is None
        or planner["state"] != "requested"
        or planner["request_id"] != context.planner_request_id
        or planner["input_token"] != context.captured_input_token
        or planner["input_token"]
        != input_token_from_connection(
            db, context.parent_graph_id, context.planner_node_id
        )
    ):
        raise PacError(
            "WORKFLOW_REQUEST_STALE",
            "expansion belongs to a withdrawn planner request",
        )
    if placeholder is None or placeholder["node_kind"] != "expansion":
        raise PacError(
            PAC_EXPANSION_INVALID,
            "the target is not an expansion placeholder",
            {"field": "placeholder", "reason": "kind"},
        )
    if planner["deadline_ms"] is not None and at > planner["deadline_ms"]:
        raise PacError("WORKFLOW_DEADLINE_EXPIRED", "the planner deadline has passed")
    deadline_ms = expansion_deadline_ms(
        authored_deadline_ms=placeholder["deadline_ms"],
        timeout_ms=int(placeholder["timeout_ms"]),
        at=at,
    )
    remaining = deadline_ms - at
    if any(
        node.timeout_ms > remaining
        or (node.deadline_ms is not None and node.deadline_ms > deadline_ms)
        for node in context.spec.nodes
    ):
        raise PacError(
            PAC_EXPANSION_INVALID,
            "prepared child no longer fits the fixed expansion deadline",
            {"field": "deadlineMs", "reason": "timeout"},
        )

    compile_workflow_in_transaction(
        store,
        context.spec,
        sender=graph["created_by"],
        machine=machine,
        local_owner=local_owner,
        operation_key=(
            f"expand:{context.parent_graph_id}:{context.placeholder_node_id}:1"
        ),
        at=at,
        prepared_artifacts=context.prepared_artifacts,
        graph_id=context.child_graph_id,
        parent_graph_id=context.parent_graph_id,
        parent_node_id=context.placeholder_node_id,
        expansion_deadline_ms=deadline_ms,
    )
    insert_binding(
        db,
        parent_graph_id=context.parent_graph_id,
        parent_node_id=context.placeholder_node_id,
        child_graph_id=context.child_graph_id,
        planner_node_id=context.planner_node_id,
        planner_request_id=context.planner_request_id,
        expansion_digest=context.expansion_digest,
        limits=context.effective_limits,
        accepted_at=at,
        deadline_ms=deadline_ms,
    )
    insert_worktree_plans(db, context.worktrees, at=at)
    event = write_flag_in_transaction(
        store,
        graph=graph,
        node_id=context.planner_node_id,
        action="set",
        actor=actor,
        at=at,
        reason_ref=reason_ref,
        output_text=output_text,
        expected_request=context.planner_request_id,
        captured_input_token=context.captured_input_token,
    )
    placeholder_token = input_token_from_connection(
        db, context.parent_graph_id, context.placeholder_node_id
    )
    if placeholder_token is None:
        raise PacError(
            PAC_EXPANSION_INVALID,
            "planner completion did not release the placeholder",
            {"field": "placeholder", "reason": "required"},
        )
    request_id = "workflow-request:" + uuid5(
        NAMESPACE_URL,
        f"pac-expand-request:{context.parent_graph_id}:{context.placeholder_node_id}:1",
    ).hex
    updated = db.execute(
        "UPDATE workflow_nodes SET state='requested',request_id=?,input_token=?,"
        "generation=1,deadline_ms=?,reason_ref=NULL,output_text=NULL "
        "WHERE graph_id=? AND node_id=? AND state='pending' AND generation=0",
        (
            request_id,
            placeholder_token,
            deadline_ms,
            context.parent_graph_id,
            context.placeholder_node_id,
        ),
    ).rowcount
    if updated != 1:
        raise PacError(
            PAC_EXPANSION_ALREADY_BOUND,
            "the expansion placeholder activation is no longer fresh",
        )
    append_event(
        db,
        graph_id=context.parent_graph_id,
        version=graph["version"],
        type="workflow_changed",
        at=at,
        data={
            "nodeId": context.placeholder_node_id,
            "state": "requested",
            "requestId": request_id,
            "deadlineMs": deadline_ms,
            "expansionAccepted": True,
        },
    )
    alert = {
        "eventId": event["eventId"],
        "edge": f"workflow-expansion:{context.placeholder_node_id}:1",
        "kind": "actor_alert",
        "recipient": graph["created_by"],
        "nodeId": context.planner_node_id,
        "text": (
            f"Workflow {context.parent_graph_id} expansion "
            f"{context.placeholder_node_id} accepted as {context.child_graph_id}."
        ),
        "sender": actor,
    }
    db.execute(
        "INSERT INTO notifications "
        "(event_id,edge,kind,recipient,round_no,text,sender,message_id,at,"
        "delivered_at,failed_at,failure_code,failure_detail) "
        "VALUES (?,?,?, ?,NULL,?,?,NULL,?,NULL,NULL,NULL,NULL)",
        (
            alert["eventId"],
            alert["edge"],
            alert["kind"],
            alert["recipient"],
            alert["text"],
            alert["sender"],
            at,
        ),
    )
    result = {
        "ok": True,
        "eventId": event["eventId"],
        "requestId": context.planner_request_id,
        "event": event,
        "childGraphId": context.child_graph_id,
        "expansionDigest": context.expansion_digest,
    }
    db.execute(
        "INSERT INTO workflow_outcome_receipts "
        "(request_id,actor,action,reason_ref,result_json,output_text,graph_id,"
        "node_id,expansion_digest) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            context.planner_request_id,
            actor,
            "complete",
            reason_ref,
            json.dumps(result, sort_keys=True),
            output_text,
            context.parent_graph_id,
            context.planner_node_id,
            context.expansion_digest,
        ),
    )
    return event, (alert,), result


__all__ = [
    "accept_prepared_expansion_in_transaction",
    "child_graph_id",
    "expansion_deadline_ms",
]


def complete_prepared_expansion(
    reactor,
    context,
    *,
    actor: str,
    reason_ref: str | None,
    output_text: str | None,
):
    """Own the transaction and deliver only after the atomic fact commits."""

    from hyprial.daemon.impl.pac.graphs.reactor import (
        FlagEventOutcome,
        PlannedNotification,
    )

    graph = reactor._require_graph(context.parent_graph_id)
    actor_parts = actor.split(":", 3)
    local_owner = graph["created_by"].split(":", 1)[-1]
    machine = actor_parts[2] if len(actor_parts) == 4 else "local"
    db = reactor._store.write()
    try:
        event, alert_rows, _ = accept_prepared_expansion_in_transaction(
            reactor._store,
            context,
            actor=actor,
            reason_ref=reason_ref,
            output_text=output_text,
            at=int(reactor._clock()),
            machine=machine,
            local_owner=local_owner,
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise
    planned = tuple(
        PlannedNotification(
            event_id=row["eventId"],
            edge=row["edge"],
            kind=row["kind"],
            recipient=row["recipient"],
            node_id=row["nodeId"],
            round_no=None,
            text=row["text"],
            sender=row["sender"],
        )
        for row in alert_rows
    )
    delivered, undelivered, error = reactor._deliver(
        context.parent_graph_id, planned
    )
    return FlagEventOutcome(
        event=event,
        planned=planned,
        delivered=delivered,
        undelivered=undelivered,
        delivery_error=error,
    )


__all__.append("complete_prepared_expansion")
