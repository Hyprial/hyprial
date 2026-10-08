"""Workflow projection vocabulary, sender and close helper."""

from __future__ import annotations

from pathlib import Path
import json
import sqlite3
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

from hyprial.identity import PacError
from hyprial.daemon.impl.pac.storage.journal  import append_event
from hyprial.daemon.impl.pac.graphs.reactor  import NotificationSender
from hyprial.daemon.impl.pac.storage.store  import PacGraphStore
from hyprial.daemon.impl.pac.workflows.graphs  import input_token, read_specification

TERMINAL = {"completed", "failed", "cancelled"}

if TYPE_CHECKING:  # forward reference to the owner class, no runtime import cycle
    from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService


class NotificationReceiptAuthority(Protocol):
    def record_delivery(
        self, graph_id: str, event_id: str, edge: str, message_id: str
    ) -> None: ...
    def record_failure(
        self, graph_id: str, event_id: str, edge: str, *, code: str, detail: str
    ) -> None: ...


class WorkflowTickAuthority(Protocol):
    def attach_workflow(self, service: GraphWorkflowService) -> None: ...
    def workflow_tick(self, observed_at_ms: int) -> None: ...
    def probe_workflow(
        self, *, yaml_text: str, sender: str, operation_key: str,
        routine_name: str | None = None, task_key: str | None = None,
        spec: Any | None = None,
    ) -> str | None: ...
    def start_workflow(
        self, *, yaml_text: str, sender: str, operation_key: str,
        routine_name: str | None, task_key: str | None,
        machine: str, local_owner: str, at: int, spec: Any | None = None,
    ) -> str: ...
    def cancel_workflow(
        self, *, run_id: str, actor: str, reason_ref: str
    ) -> dict[str, Any]: ...
    def stop_workflow_worker(
        self, *, graph_id: str, actor_name: str, actor: str
    ) -> dict[str, Any]: ...
    def restart_workflow_worker(
        self, *, graph_id: str, actor_name: str, actor: str
    ) -> dict[str, Any]: ...
    def fail_workflow(
        self, *, graph_id: str, node_id: str, actor: str,
        request_id: str, reason_ref: str, output_text: str | None,
    ) -> dict[str, Any]: ...
    def record_harness_outcome(
        self, *, message_id: str, recipient: str, failed: bool,
        failure_code: str | None = None,
    ) -> bool: ...
    def record_request_pruned(self, *, message_id: str, recipient: str) -> bool: ...


class WorkflowServiceError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _changed(store: PacGraphStore, graph: dict, at: int, **data: Any) -> None:
    append_event(
        store._db,
        graph_id=graph["graph_id"],
        version=graph["version"],
        type="workflow_changed",
        at=at,
        data=data,
    )


def _request_expires_at_ms(row: sqlite3.Row) -> int | None:
    """Return the one authoritative inbox expiry for a workflow request.

    Deadline computation belongs to workflow projection.  Keeping the read in
    one helper gives deadline-policy changes one adaptation point instead of
    duplicating them across local and remote notification paths.
    """

    value = row["deadline_ms"]
    return None if value is None else int(value)


def close_workflow(
    store: PacGraphStore,
    graph: dict,
    *,
    state: str,
    reason: str | None,
    at: int,
    cancelled_reason: str | None = None,
    closed_by: str | None = None,
) -> None:
    """Caller owns the transaction; close and cancellation are one fact."""
    db = store._db
    from hyprial.daemon.impl.pac.workflows.expansion.projection import (
        cascade_open_children_in_transaction,
    )

    cascade_open_children_in_transaction(
        store,
        graph["graph_id"],
        reason=cancelled_reason or reason or "pac:parent-closed",
        at=at,
    )
    cancellation = reason if cancelled_reason is None else cancelled_reason
    closer = graph["created_by"] if closed_by is None else closed_by
    db.execute(
        "UPDATE workflow_graphs SET state=?,reason_ref=? WHERE graph_id=?",
        (state, reason, graph["graph_id"]),
    )
    for row in db.execute(
        "SELECT * FROM workflow_nodes WHERE graph_id=? AND state IN ('pending','requested','done')",
        (graph["graph_id"],),
    ).fetchall():
        node = store.node(graph["graph_id"], row["node_id"])
        accepted = (
            node is not None
            and node.flag
            and row["input_token"]
            == input_token(store, graph["graph_id"], row["node_id"], ignore_actor=True)
        )
        db.execute(
            "UPDATE workflow_nodes SET state=?,reason_ref=? WHERE graph_id=? AND node_id=?",
            (
                "done" if accepted else "cancelled",
                node.flag_reason_ref if accepted else cancellation,
                graph["graph_id"],
                row["node_id"],
            ),
        )
    for receipt in store.workflow_worker_receipts(graph["graph_id"]):
        operation_id = f"pac-cleanup:{uuid4().hex}"
        inserted = db.execute(
            "INSERT OR IGNORE INTO workflow_worker_cleanup_intents "
            "(graph_id,actor_node,actor_name,actor_uri,operation_id,state,"
            "attempts,created_at,updated_at) VALUES (?,?,?,?,?,'pending',0,?,?)",
            (
                graph["graph_id"],
                receipt["actor_node"],
                receipt["actor_name"],
                receipt["actor_uri"],
                operation_id,
                at,
                at,
            ),
        )
        if inserted.rowcount:
            append_event(
                db,
                graph_id=graph["graph_id"],
                version=graph["version"],
                type="workflow_changed",
                at=at,
                data={
                    "workerCleanup": "pending",
                    "actorNode": receipt["actor_node"],
                    "actorName": receipt["actor_name"],
                    "operationId": operation_id,
                },
            )
    db.execute(
        "UPDATE workflow_worktrees SET state='cleanup_pending',updated_at=? "
        "WHERE graph_id=? AND state IN ('planned','prepared')",
        (at, graph["graph_id"]),
    )
    if graph["closed_at"] is None:
        db.execute(
            "UPDATE graphs SET closed_at=?,closed_by=? WHERE graph_id=?",
            (at, closer, graph["graph_id"]),
        )
        append_event(
            db,
            graph_id=graph["graph_id"],
            version=graph["version"],
            type="graph_closed",
            at=at,
            data={
                "at": at,
                "by": closer,
                "state": state,
                "reasonRef": reason,
            },
        )
    _changed(store, graph, at, state=state, reasonRef=reason)


class WorkflowSender:
    """Resolve immutable task references at the delivery boundary.

    The PAC notification itself contains only the brief reference. Its single
    outbox identity also identifies the body delivered to the actor.
    """

    def __init__(self, database: Path, downstream: NotificationSender):
        self.database = database
        self.downstream = downstream

    def send(
        self,
        *,
        recipient: str,
        text: str,
        sender: str,
        conversation_id: str,
        idempotency_key: str,
    ) -> str:
        request_expires_at_ms = None
        if idempotency_key.startswith("pac-notify:workflow-request:"):
            request_id = idempotency_key.split(":", 3)[2]
            store = PacGraphStore(self.database, read_only=True)
            try:
                with store.read():
                    row = store._db.execute(
                        "SELECT w.*,g.specification_ref,g.specification_digest,"
                        "g.roster_json,g.roster_digest,n.owner,n.flag "
                        "FROM workflow_nodes w JOIN workflow_graphs g USING(graph_id) "
                        "JOIN nodes n ON n.graph_id=w.graph_id AND n.node_id=w.node_id WHERE w.request_id=?",
                        (f"workflow-request:{request_id}",),
                    ).fetchone()
                    # request ids include no ':' after the prefix; the edge is
                    # the suffix of the outer PAC notification identity.
                    if row is None:
                        raise PacError(
                            "WORKFLOW_REQUEST_STALE", "request no longer exists"
                        )
                    graph = store.graph(row["graph_id"])
                    assert graph is not None
                    if (
                        graph["closed_at"] is not None
                        or row["state"] != "requested"
                        or row["flag"]
                        or row["owner"] != recipient
                        or input_token(store, row["graph_id"], row["node_id"])
                        != row["input_token"]
                    ):
                        raise PacError(
                            "WORKFLOW_REQUEST_STALE",
                            "task was withdrawn or closed before delivery",
                        )
                    spec = read_specification(row)
                    roster = store.workflow_roster(str(row["graph_id"]))
                    roster_line = (
                        f"Workflow roster: {row['roster_json']}\n"
                        if roster is not None
                        else ""
                    )
                    request_expires_at_ms = _request_expires_at_ms(row)
                    node = next(n for n in spec["nodes"] if n["id"] == row["node_id"])
                    predecessors = [
                        edge.from_node
                        for edge in store.edges(row["graph_id"])
                        if edge.to_node == row["node_id"] and edge.kind == "forward"
                    ]
                    inputs = []
                    input_bytes = 0
                    for name in predecessors:
                        predecessor = store.node(row["graph_id"], name)
                        if predecessor is None or predecessor.kind == "actor":
                            continue
                        item = predecessor.to_json()
                        projection = store._db.execute(
                            "SELECT output_text FROM workflow_nodes "
                            "WHERE graph_id=? AND node_id=?",
                            (row["graph_id"], name),
                        ).fetchone()
                        if projection is not None and projection["output_text"] is not None:
                            item["outputText"] = projection["output_text"][:4096]
                        encoded = json.dumps(item, ensure_ascii=False).encode("utf-8")
                        if input_bytes + len(encoded) > 16_384:
                            break
                        input_bytes += len(encoded)
                        inputs.append(item)
                    planner_line = (
                        "Expansion planner: complete with the required bounded "
                        f"child document for target {node['expands']}; preview it "
                        "before completion and reuse the current request only after "
                        "a definitive validation rejection.\n"
                        if node.get("expands") is not None
                        else ""
                    )
                    remote_request = {
                        "graphId": row["graph_id"], "nodeId": row["node_id"],
                        "requestId": row["request_id"], "owner": recipient,
                        "inputToken": row["input_token"],
                        "deadlineMs": request_expires_at_ms,
                        "role": node["role"], "firstOutputEta": node.get("first_output_eta"),
                        "humanGatesDeclared": node.get("human_gates") is not None,
                        **(
                            {"expands": node["expands"]}
                            if node.get("expands") is not None
                            else {}
                        ),
                    }
                    command = f"{row['graph_id']} {row['node_id']} --request-id {row['request_id']}"
                    text = (
                        f"{node['task']}\n\n"
                        f"Input evidence references: {json.dumps(inputs, ensure_ascii=False)}\n"
                        f"{roster_line}"
                        f"Inspect current work before acting: hyprial workflow inspect {row['graph_id']} --node {row['node_id']} --json\n"
                        f"Graph: {row['graph_id']}; node: {row['node_id']}; role: {node['role']}\n"
                        f"{planner_line}"
                        f"Fixed deadline: {row['deadline_ms']} (epoch ms).\n"
                        f"Complete explicitly: hyprial workflow complete {command} --reason-ref <evidence-reference> [--output-text <result>]\n"
                        f"Report failure: hyprial workflow fail {command} --reason-ref <failure-reference> [--output-text <result>]\n"
                        "Without a shell, use the workflow_complete / workflow_fail tool with the same "
                        "graphId, nodeId, requestId and reasonRef; outputText optionally carries the result inline.\n"
                        "If returnState is pending, the outcome is durably queued; inspect until accepted or rejected.\n"
                        "A reply is not completion. Do not repeat a withdrawn request.\n"
                    )
            finally:
                store.close()
        remote_send = getattr(self.downstream, "send_workflow_request", None)
        if idempotency_key.startswith("pac-notify:workflow-request:") and callable(remote_send):
            message_id = remote_send(remote_request, text=text, idempotency_key=idempotency_key)
            if message_id is not None:
                return message_id
        expiry = (
            {"expires_at_ms": request_expires_at_ms}
            if request_expires_at_ms is not None
            else {}
        )
        return self.downstream.send(
            recipient=recipient,
            text=text,
            sender=sender,
            conversation_id=conversation_id,
            idempotency_key=idempotency_key,
            **expiry,
        )


def _handoff_sender(store: PacGraphStore, graph: dict, node_id: str) -> str:
    """Who hands this node its work: the principal that set the most recent
    predecessor flag (forward or back edge), or the creator for a root.

    Allen, 2026-09-25: when worker-2 asks who handed the work off, the answer
    is worker-1; ownership (the creator) is a separate question.
    """

    sources = {edge.from_node for edge in store.edges(graph["graph_id"]) if edge.to_node == node_id}
    latest = None
    for name in sources:
        row = store.node(graph["graph_id"], name)
        if row is None or not row.flag or not row.flag_set_by or row.flag_set_at is None:
            continue
        if ":" not in row.flag_set_by:  # the system ("reactor"), not a principal
            continue
        if latest is None or row.flag_set_at > latest.flag_set_at:
            latest = row
    return latest.flag_set_by if latest is not None else graph["created_by"]
