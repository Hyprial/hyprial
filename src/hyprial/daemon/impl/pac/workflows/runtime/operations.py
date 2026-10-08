"""GraphWorkflowService operations: outcomes, status and worker control."""

from __future__ import annotations

from graphlib import TopologicalSorter
import json
import re
import sqlite3
from typing import Any

from hyprial.identity import PacError
from hyprial.daemon.impl.pac.graphs.reactor  import PacReactor, permanent_delivery_failure
from hyprial.daemon.impl.pac.storage.store  import PacGraphStore
from hyprial.daemon.impl.pac.workflows.graphs  import input_token
from hyprial.daemon.impl.pac.workflows.outputs  import validate_workflow_output_text
from hyprial.daemon.impl.pac.workflows.runtime.types import WorkflowServiceError, _changed, close_workflow
from hyprial.kernel import canonical_user_uri

# Graphs one workflow tick projects: open ones, unfinished workflows, and closed
# ones still holding an undelivered actor alert.  The alert test is an
# uncorrelated IN list, evaluated once per tick.  A correlated EXISTS re-scanned
# every notification (json_extract per row) for every graph: about 0.65 s of CPU
# per tick on production's 709 graphs x 1922 notifications (0.5.0, 2026-10-05).
TICK_GRAPH_SELECT = (
    "SELECT w.graph_id FROM workflow_graphs w JOIN graphs g USING(graph_id) "
    "WHERE g.closed_at IS NULL OR w.state NOT IN ('completed','failed','cancelled') "
    "OR g.graph_id IN (SELECT json_extract(n.plan_json,'$.graphId') FROM notifications n "
    "WHERE n.message_id IS NULL AND n.failed_at IS NULL AND n.kind='actor_alert') "
    "ORDER BY (g.closed_at IS NOT NULL), g.created_at"
)


class _GraphWorkflowServiceOps:
    def _drain(self, store: PacGraphStore, graph_id: str):
        reactor = PacReactor(
            store, sender=self.sender, clock=self.clock, logger=self.logger
        )
        graph = store.graph(graph_id)
        assert graph is not None
        for item in [
            *store.notifications(graph_id),
            *store.notifications_by_synthetic_event(graph_id),
        ]:
            if item.message_id is not None or item.failed_at is not None:
                continue
            is_alert = item.kind == "actor_alert"
            if graph["closed_at"] is not None and not is_alert:
                continue
            try:
                text = item.text
                if item.kind == "turn" and not item.event_id.startswith(
                    "workflow-request:"
                ):
                    text += f"\nExplicit rework: inspect hyprial workflow inspect {graph_id}. Reset your old completion flag before acting on a new request; do not reuse an old request ID."
                message = self.sender.send(
                    recipient=item.recipient,
                    text=text,
                    sender=item.sender,
                    conversation_id=f"pac-{graph_id}",
                    idempotency_key=f"pac-notify:{item.event_id}:{item.edge}",
                )
                if self._delivery_receipt is None:
                    reactor._mark_delivered(item.event_id, item.edge, message)
                else:
                    self._delivery_receipt.record_delivery(
                        graph_id, item.event_id, item.edge, message
                    )
            except Exception as error:
                terminal = permanent_delivery_failure(error)
                if terminal is not None:
                    if self._delivery_receipt is None:
                        reactor._mark_failed(
                            graph_id,
                            item.event_id,
                            item.edge,
                            code=terminal[0],
                            detail=terminal[1],
                        )
                    else:
                        # Actorized: this drain holds a read-only store, so
                        # the terminal write goes through the graph writer.
                        self._delivery_receipt.record_failure(
                            graph_id,
                            item.event_id,
                            item.edge,
                            code=terminal[0],
                            detail=terminal[1],
                        )
                    continue
                if self.logger:
                    self.logger(
                        "warn",
                        "pac",
                        "workflow.delivery_pending",
                        graphId=graph_id,
                        detail=str(error),
                    )

    def _tick(self, at: int | None = None):
        at = self.clock() if at is None else at
        store = self._open()
        try:
            ids = [
                row[0]
                for row in store._db.execute(TICK_GRAPH_SELECT)
            ]
            for graph_id in ids:
                try:
                    self._project(store, graph_id, at)
                    if self._asynchronous:
                        self._queue_delivery(
                            graph_id,
                            closed=store.graph(graph_id)["closed_at"] is not None,
                        )
                    else:
                        self._drain(store, graph_id)
                except Exception as error:
                    if self.logger:
                        self.logger(
                            "error",
                            "pac",
                            "workflow.graph_failed",
                            graphId=graph_id,
                            detail=str(error),
                        )
                    else:
                        raise
        finally:
            store.close()

    def record_harness_outcome(
        self,
        *,
        message_id: str,
        recipient: str,
        failed: bool,
        failure_code: str | None = None,
    ) -> bool:
        """Retire a bound turn without reply matching or automatic task retries.

        A native completed turn is not a completed task. An observed failed
        turn is an explicit execution failure; persist it before inbox ACK.
        Late outcomes cannot overwrite an already accepted completion flag.
        """
        if self._graph_authority is not None:
            return self._graph_authority.record_harness_outcome(
                message_id=message_id, recipient=recipient, failed=failed,
                failure_code=failure_code,
            )
        return self._record_harness_outcome_direct(
            message_id=message_id, recipient=recipient, failed=failed,
            failure_code=failure_code,
        )

    def _record_harness_outcome_direct(
        self, *, message_id: str, recipient: str, failed: bool,
        failure_code: str | None = None,
    ) -> bool:
        store = self._open()
        try:
            with store.write():
                binding = store._db.execute(
                    "SELECT * FROM workflow_deliveries WHERE message_id=?",
                    (message_id,),
                ).fetchone()
                if binding is None:
                    return False
                graph = store.graph(binding["graph_id"])
                node = store.node(binding["graph_id"], binding["node_id"])
                row = store._db.execute(
                    "SELECT * FROM workflow_nodes WHERE graph_id=? AND node_id=?",
                    (binding["graph_id"], binding["node_id"]),
                ).fetchone()
                if node is None or node.owner != recipient:
                    raise WorkflowServiceError(
                        "WORKFLOW_NOT_OWNER",
                        "native outcome recipient does not own this request",
                    )
                if (
                    failed
                    and graph
                    and graph["closed_at"] is None
                    and not node.flag
                    and row
                    and row["state"] == "requested"
                    and row["request_id"] == binding["request_id"]
                    and row["input_token"]
                    == input_token(store, binding["graph_id"], binding["node_id"])
                ):
                    code = (
                        failure_code
                        if isinstance(failure_code, str)
                        and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", failure_code)
                        else "HARNESS_TURN_FAILED"
                    )
                    reason = (
                        "pac:deadline-expired"
                        if row["deadline_ms"] is not None
                        and self.clock() > row["deadline_ms"]
                        else f"harness:{code}"
                    )
                    self._failure(
                        store,
                        graph,
                        node.node_id,
                        reason,
                        self.clock(),
                        PacReactor(store),
                    )
        finally:
            store.close()
        self.submit_timer(self.clock())
        return True

    def record_request_pruned(self, *, message_id: str, recipient: str) -> bool:
        """Fail the still-current node request whose inbox row was pruned.

        The delivery binding is request-scoped.  An old row can therefore be
        swept after a new generation has been requested without authorizing a
        failure of that new generation.
        """

        if self._graph_authority is not None:
            return self._graph_authority.record_request_pruned(
                message_id=message_id, recipient=recipient,
            )
        return self._record_request_pruned_direct(
            message_id=message_id, recipient=recipient,
        )

    def _record_request_pruned_direct(
        self, *, message_id: str, recipient: str
    ) -> bool:

        failed = False
        store = self._open()
        try:
            with store.write():
                binding = store._db.execute(
                    "SELECT * FROM workflow_deliveries WHERE message_id=?",
                    (message_id,),
                ).fetchone()
                if binding is None:
                    return False
                graph = store.graph(binding["graph_id"])
                node = store.node(binding["graph_id"], binding["node_id"])
                row = store._db.execute(
                    "SELECT * FROM workflow_nodes WHERE graph_id=? AND node_id=?",
                    (binding["graph_id"], binding["node_id"]),
                ).fetchone()
                if not (
                    graph
                    and graph["closed_at"] is None
                    and node
                    and node.owner == recipient
                    and not node.flag
                    and row
                    and row["state"] == "requested"
                    and row["request_id"] == binding["request_id"]
                    and row["input_token"]
                    == input_token(store, binding["graph_id"], binding["node_id"])
                ):
                    return False
                self._failure(
                    store,
                    graph,
                    node.node_id,
                    "pac:request-expired",
                    self.clock(),
                    PacReactor(store),
                )
                failed = True
        finally:
            store.close()
        if failed:
            self.submit_timer(self.clock())
        return failed

    def status(self, *, run_id: str) -> dict[str, Any]:
        store = self._open(read_only=True)
        try:
            with store.read():
                graph = store.graph(run_id)
                meta = store._db.execute(
                    "SELECT * FROM workflow_graphs WHERE graph_id=?", (run_id,)
                ).fetchone()
                if graph is None or meta is None:
                    raise WorkflowServiceError(
                        "WORKFLOW_NOT_FOUND", f"workflow {run_id!r} not found"
                    )
                rows = {
                    r["node_id"]: r
                    for r in store._db.execute(
                        "SELECT * FROM workflow_nodes WHERE graph_id=?", (run_id,)
                    )
                }
                roster = store.workflow_roster(run_id)
                worker_cleanup = store.workflow_worker_cleanup(run_id)
                nodes = [
                    {
                        **node.to_json(),
                        "state": rows[node.node_id]["state"]
                        if rows[node.node_id]["state"]
                        in {"failed", "cancelled", "blocked"}
                        else (
                            (
                                "done"
                                if rows[node.node_id]["input_token"]
                                == input_token(
                                    store, run_id, node.node_id, ignore_actor=True
                                )
                                else "stale"
                            )
                            if node.flag
                            else rows[node.node_id]["state"]
                        ),
                        "requestId": rows[node.node_id]["request_id"],
                        "deadlineMs": rows[node.node_id]["deadline_ms"],
                        "timeoutMs": rows[node.node_id]["timeout_ms"],
                        "reasonRef": rows[node.node_id]["reason_ref"]
                        or node.flag_reason_ref,
                        **(
                            {"outputText": rows[node.node_id]["output_text"]}
                            if rows[node.node_id]["output_text"] is not None
                            else {}
                        ),
                        "actorNode": rows[node.node_id]["actor_node"],
                    }
                    for node in store.nodes(run_id)
                    if node.node_id in rows
                ]
                from hyprial.daemon.impl.pac.workflows.expansion.projection import (
                    placeholder_projection,
                )

                for projected in nodes:
                    if rows[projected["nodeId"]]["node_kind"] == "expansion":
                        projected.update(
                            placeholder_projection(
                                store._db, run_id, projected["nodeId"]
                            )
                        )
                for projected in nodes:
                    projected["localHumanCanComplete"] = (
                        projected["owner"] == canonical_user_uri(self.owner)
                        and projected["state"] == "requested"
                        and projected["deadlineMs"] is not None
                        and self.clock() <= projected["deadlineMs"]
                    )
                    notification = store._db.execute(
                        "SELECT message_id,delivered_at,failed_at,failure_code "
                        "FROM notifications WHERE event_id=?",
                        (projected["requestId"],),
                    ).fetchone()
                    projected["deliveryState"] = (
                        (
                            "failed"
                            if notification["failed_at"] is not None
                            else "accepted"
                            if notification["message_id"]
                            else "planned"
                            if projected["state"] == "requested"
                            and graph["closed_at"] is None
                            else "withdrawn"
                        )
                        if notification
                        else None
                    )
                    if notification and notification["failed_at"] is not None:
                        projected["deliveryFailureCode"] = notification[
                            "failure_code"
                        ]
                    projected["messageId"] = (
                        notification["message_id"] if notification else None
                    )
                return {
                    "runId": run_id,
                    "graphId": run_id,
                    "backend": "pac",
                    "name": graph["name"],
                    "state": meta["state"],
                    "onFailure": meta["on_failure"],
                    "sender": graph["created_by"],
                    "createdAtMs": graph["created_at"],
                    "closedAtMs": graph["closed_at"],
                    "nodes": nodes,
                    "edges": [e.to_json() for e in store.edges(run_id)],
                    "reasonRef": meta["reason_ref"],
                    **(
                        {
                            "parentGraphId": meta["parent_graph_id"],
                            "parentNodeId": meta["parent_node_id"],
                        }
                        if meta["parent_graph_id"] is not None
                        else {}
                    ),
                    **(
                        {"roster": roster, "rosterDigest": roster["digest"]}
                        if roster is not None
                        else {}
                    ),
                    **(
                        {"workerCleanup": worker_cleanup}
                        if worker_cleanup is not None
                        else {}
                    ),
                }
        finally:
            store.close()

    def list(self, *, limit: int = 50, viewer: str | None = None) -> dict[str, Any]:
        store = self._open(read_only=True)
        try:
            with store.read():
                rows = store._db.execute(
                    "SELECT g.*,w.state,w.on_failure,w.routine_name,"
                    "w.parent_graph_id,w.parent_node_id FROM graphs g "
                    "JOIN workflow_graphs w USING(graph_id) "
                    "WHERE (? IS NULL OR g.created_by=? OR EXISTS (SELECT 1 FROM nodes n WHERE n.graph_id=g.graph_id AND n.owner=?)) "
                    "ORDER BY g.created_at DESC,g.graph_id LIMIT ?",
                    (viewer, viewer, viewer, min(max(limit, 1), 500)),
                ).fetchall()

                def current_node(row: sqlite3.Row) -> dict[str, str] | None:
                    if row["closed_at"] is not None:
                        return None
                    states = {
                        str(node["node_id"]): str(node["state"])
                        for node in store._db.execute(
                            "SELECT w.node_id,w.state FROM workflow_nodes w "
                            "JOIN nodes n ON n.graph_id=w.graph_id AND n.node_id=w.node_id "
                            "WHERE w.graph_id=? ORDER BY n.rowid",
                            (row["graph_id"],),
                        )
                    }
                    predecessors = {node_id: set() for node_id in states}
                    for edge in store._db.execute(
                        "SELECT from_node,to_node FROM edges "
                        "WHERE graph_id=? AND kind='forward'",
                        (row["graph_id"],),
                    ):
                        source = str(edge["from_node"])
                        target = str(edge["to_node"])
                        if source in states and target in states:
                            predecessors[target].add(source)
                    for node_id in TopologicalSorter(predecessors).static_order():
                        state = states[node_id]
                        if state in {"pending", "requested"}:
                            return {"nodeId": node_id, "state": state}
                    return None

                def last_progress_at(row: sqlite3.Row) -> int | None:
                    progress = store._db.execute(
                        "SELECT MAX(at) AS at FROM journal WHERE graph_id=?",
                        (row["graph_id"],),
                    ).fetchone()["at"]
                    return int(progress) if progress is not None else None

                return {
                    "runs": [
                        {
                            "runId": r["graph_id"],
                            "graphId": r["graph_id"],
                            "backend": "pac",
                            "name": r["name"],
                            "state": r["state"],
                            "sender": r["created_by"],
                            "createdAtMs": r["created_at"],
                            "closedAtMs": r["closed_at"],
                            "onFailure": r["on_failure"],
                            "routineName": r["routine_name"],
                            **(
                                {
                                    "parentGraphId": r["parent_graph_id"],
                                    "parentNodeId": r["parent_node_id"],
                                }
                                if r["parent_graph_id"] is not None
                                else {}
                            ),
                            "lastProgressAtMs": last_progress_at(r),
                            "currentNode": current_node(r),
                        }
                        for r in rows
                    ]
                }
        finally:
            store.close()

    def routine_last_dispatches(self) -> dict[str, int]:
        """Latest graph creation time for each routine-backed workflow."""

        store = self._open(read_only=True)
        try:
            with store.read():
                return {
                    str(row["routine_name"]): int(row["created_at"])
                    for row in store._db.execute(
                        "SELECT w.routine_name,MAX(g.created_at) AS created_at "
                        "FROM workflow_graphs w JOIN graphs g USING(graph_id) "
                        "WHERE w.routine_name IS NOT NULL GROUP BY w.routine_name"
                    )
                }
        finally:
            store.close()

    def cancel(
        self, *, run_id: str, actor: str, reason_ref: str = "workflow:cancelled"
    ) -> dict[str, Any]:
        if self._graph_authority is not None:
            try:
                return self._graph_authority.cancel_workflow(
                    run_id=run_id, actor=actor, reason_ref=reason_ref
                )
            except PacError as error:
                raise WorkflowServiceError(error.code, str(error)) from error
        return self._cancel_direct(run_id=run_id, actor=actor, reason_ref=reason_ref)

    def _cancel_direct(
        self, *, run_id: str, actor: str, reason_ref: str
    ) -> dict[str, Any]:
        store = self._open()
        try:
            with store.write():
                graph = store.graph(run_id)
                if graph is None or graph["created_by"] != actor:
                    raise WorkflowServiceError(
                        "WORKFLOW_NOT_OWNER", "only the graph creator can cancel"
                    )
                if graph["closed_at"] is None:
                    close_workflow(
                        store,
                        graph,
                        state="cancelled",
                        reason=reason_ref,
                        at=self.clock(),
                    )
        finally:
            store.close()
        return self.status(run_id=run_id)

    def _workflow_actor(self, store: PacGraphStore, graph_id: str, actor_name: str):
        graph = store.graph(graph_id)
        if graph is None:
            raise WorkflowServiceError("WORKFLOW_NOT_FOUND", f"workflow {graph_id!r} not found")
        actor = next(
            (
                node
                for node in store.nodes(graph_id)
                if node.kind == "actor"
                and actor_name in {node.node_id, node.actor_name}
            ),
            None,
        )
        if actor is None:
            borrowed = any(
                node.owner == actor_name
                for node in store.nodes(graph_id)
                if node.kind != "actor"
            )
            raise WorkflowServiceError(
                "WORKFLOW_BORROWED_ACTOR" if borrowed else "WORKFLOW_WORKER_NOT_FOUND",
                "borrowed or remote actors are not graph-owned workers"
                if borrowed
                else f"worker {actor_name!r} is not owned by workflow {graph_id!r}",
            )
        return graph, actor

    def stop_worker(self, *, graph_id: str, actor_name: str, actor: str):
        """Give up an owned worker and fail its unfinished work immediately."""
        if self._graph_authority is not None:
            try:
                return self._graph_authority.stop_workflow_worker(
                    graph_id=graph_id, actor_name=actor_name, actor=actor
                )
            except PacError as error:
                raise WorkflowServiceError(error.code, str(error)) from error
        return self._stop_worker_direct(
            graph_id=graph_id, actor_name=actor_name, actor=actor
        )

    def _stop_worker_direct(self, *, graph_id: str, actor_name: str, actor: str):
        from hyprial.daemon.impl.pac.actors.coordinator import request_actor_stop

        store = self._open()
        try:
            with store.write():
                graph, worker = self._workflow_actor(store, graph_id, actor_name)
                if graph["created_by"] != actor:
                    raise WorkflowServiceError(
                        "WORKFLOW_NOT_OWNER", "only the workflow owner can stop its worker"
                    )
                if graph["closed_at"] is None:
                    reason = f"worker stopped by {actor}"
                    rows = store._db.execute(
                        "SELECT node_id FROM workflow_nodes WHERE graph_id=? AND actor_node=? "
                        "AND state NOT IN ('done','failed','cancelled','blocked')",
                        (graph_id, worker.node_id),
                    ).fetchall()
                    for row in rows:
                        store._db.execute(
                            "UPDATE workflow_nodes SET state='failed',reason_ref=? "
                            "WHERE graph_id=? AND node_id=?",
                            (reason, graph_id, row["node_id"]),
                        )
                        _changed(
                            store,
                            graph,
                            self.clock(),
                            nodeId=row["node_id"],
                            state="failed",
                            reasonRef=reason,
                        )
                    meta = store._db.execute(
                        "SELECT on_failure FROM workflow_graphs WHERE graph_id=?",
                        (graph_id,),
                    ).fetchone()
                    if meta["on_failure"] == "terminate":
                        close_workflow(
                            store,
                            graph,
                            state="failed",
                            reason=reason,
                            at=self.clock(),
                        )
                    elif meta["on_failure"] == "hold":
                        store._db.execute(
                            "UPDATE workflow_graphs SET state='held',reason_ref=? "
                            "WHERE graph_id=?",
                            (reason, graph_id),
                        )
                store._db.commit()
            try:
                request_actor_stop(store, graph_id, worker.actor_name, actor=actor)
            except (RuntimeError, KeyError):
                # A unit or freshly-created graph may not have a coordinator
                # activation yet; the durable worker failure is still final.
                pass
        finally:
            store.close()
        return self.status(run_id=graph_id)

    def restart_worker(self, *, graph_id: str, actor_name: str, actor: str):
        """Restart an owned worker and make its current work requestable again."""
        if self._graph_authority is not None:
            try:
                return self._graph_authority.restart_workflow_worker(
                    graph_id=graph_id, actor_name=actor_name, actor=actor
                )
            except PacError as error:
                raise WorkflowServiceError(error.code, str(error)) from error
        return self._restart_worker_direct(
            graph_id=graph_id, actor_name=actor_name, actor=actor
        )

    def _restart_worker_direct(self, *, graph_id: str, actor_name: str, actor: str):
        from hyprial.daemon.impl.pac.actors.coordinator import request_actor_wake

        store = self._open()
        try:
            with store.write():
                graph, worker = self._workflow_actor(store, graph_id, actor_name)
                if graph["created_by"] != actor:
                    raise WorkflowServiceError(
                        "WORKFLOW_NOT_OWNER", "only the workflow owner can restart its worker"
                    )
                store._db.execute(
                    "UPDATE workflow_nodes SET state='pending',request_id=NULL,input_token=NULL,reason_ref=NULL,output_text=NULL "
                    "WHERE graph_id=? AND actor_node=? AND state NOT IN ('done','cancelled')",
                    (graph_id, worker.node_id),
                )
                store._db.execute(
                    "UPDATE workflow_graphs SET state='running',reason_ref=NULL WHERE graph_id=?",
                    (graph_id,),
                )
                store._db.commit()
            try:
                request_actor_wake(store, graph_id, worker.actor_name)
            except (RuntimeError, KeyError):
                pass
        finally:
            store.close()
        self.submit_timer(self.clock())
        return self.status(run_id=graph_id)

    def fail(
        self,
        *,
        graph_id: str,
        node_id: str,
        actor: str,
        request_id: str,
        reason_ref: str,
        output_text: str | None = None,
    ):
        if self._graph_authority is not None:
            try:
                return self._graph_authority.fail_workflow(
                    graph_id=graph_id, node_id=node_id, actor=actor,
                    request_id=request_id, reason_ref=reason_ref,
                    output_text=output_text,
                )
            except PacError as error:
                raise WorkflowServiceError(error.code, str(error)) from error
        return self._fail_direct(
            graph_id=graph_id, node_id=node_id, actor=actor,
            request_id=request_id, reason_ref=reason_ref,
            output_text=output_text,
        )

    def _fail_direct(
        self, *, graph_id: str, node_id: str, actor: str,
        request_id: str, reason_ref: str, output_text: str | None,
    ):
        try:
            output_text = validate_workflow_output_text(output_text)
        except ValueError as error:
            raise WorkflowServiceError("WORKFLOW_OUTPUT_INVALID", str(error)) from error
        store = self._open()
        replay: dict[str, Any] | None = None
        try:
            with store.write():
                receipt = store._db.execute(
                    "SELECT * FROM workflow_outcome_receipts WHERE request_id=?",
                    (request_id,),
                ).fetchone()
                if receipt is not None:
                    if (
                        receipt["graph_id"] != graph_id
                        or receipt["node_id"] != node_id
                        or receipt["actor"] != actor
                        or receipt["action"] != "fail"
                        or receipt["reason_ref"] != reason_ref
                        or receipt["output_text"] != output_text
                        or receipt["expansion_digest"] is not None
                    ):
                        raise WorkflowServiceError(
                            "WORKFLOW_OUTCOME_CONFLICT",
                            "request already has a different accepted outcome",
                        )
                    replay = json.loads(receipt["result_json"])
                    return replay
                graph = store.graph(graph_id)
                node = store.node(graph_id, node_id)
                row = store._db.execute(
                    "SELECT * FROM workflow_nodes WHERE graph_id=? AND node_id=?",
                    (graph_id, node_id),
                ).fetchone()
                if row is not None and row["node_kind"] == "expansion":
                    raise WorkflowServiceError(
                        "PAC_EXPANSION_SYSTEM_NODE",
                        "expansion placeholders cannot accept public failure",
                    )
                if graph is None or node is None or node.owner != actor:
                    raise WorkflowServiceError(
                        "WORKFLOW_NOT_OWNER", "only the node owner can report failure"
                    )
                if (
                    graph["closed_at"] is not None
                    or row is None
                    or row["request_id"] != request_id
                    or row["state"] != "requested"
                    or (
                        row["deadline_ms"] is not None
                        and self.clock() > row["deadline_ms"]
                    )
                    or row["input_token"] != input_token(store, graph_id, node_id)
                ):
                    raise WorkflowServiceError(
                        "WORKFLOW_REQUEST_STALE",
                        "failure belongs to a withdrawn or completed request",
                    )
                self._failure(
                    store,
                    graph,
                    node_id,
                    reason_ref,
                    self.clock(),
                    PacReactor(store),
                    output_text,
                )
                store._db.execute(
                    "INSERT INTO workflow_outcome_receipts "
                    "(request_id,actor,action,reason_ref,result_json,output_text,"
                    "graph_id,node_id,expansion_digest) VALUES (?,?,?,?,?,?,?,?,NULL)",
                    (
                        request_id,
                        actor,
                        "fail",
                        reason_ref,
                        json.dumps({"ok": True, "requestId": request_id}),
                        output_text,
                        graph_id,
                        node_id,
                    ),
                )
        finally:
            store.close()
        # Production cadence owns follow-up projection and notification
        # delivery.  A caller reporting failure has already committed the
        # request-scoped fact; scanning every graph here can hold an IPC/RX
        # thread behind unrelated SQLite and delivery work.
        if self._asynchronous:
            self.submit_timer(self.clock())
        else:
            self._tick()
        return self.status(run_id=graph_id)
