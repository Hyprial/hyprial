"""Graph workflow request projection and durable dispatch, owned by PAC.

No inbox polling or interpretation of replies. Flags are completion facts;
workflow rows bind a dispatch request, its fixed deadline and failure policy.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import queue
import json
import re
import sqlite3
import threading
from time import time_ns, monotonic
from typing import Any
from uuid import uuid4

from hyprial.contracts import ipc_errors

from .errors import PacError
from .journal import append_event
from .reactor import NotificationSender, PacReactor, PlannedNotification, turn_text
from .store import PacGraphStore, default_database_path
from .workflow_graph import (
    compile_workflow,
    input_token,
    read_specification,
    replay_graph,
)
from .workflow_schema import WorkflowSchemaError, WorkflowSpec, load_workflow_text

TERMINAL = {"completed", "failed", "cancelled"}


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


def close_workflow(
    store: PacGraphStore, graph: dict, *, state: str, reason: str, at: int
) -> None:
    """Caller owns the transaction; close and cancellation are one fact."""
    db = store._db
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
                node.flag_reason_ref if accepted else reason,
                graph["graph_id"],
                row["node_id"],
            ),
        )
    if graph["closed_at"] is None:
        db.execute(
            "UPDATE graphs SET closed_at=?,closed_by=? WHERE graph_id=?",
            (at, graph["created_by"], graph["graph_id"]),
        )
        append_event(
            db,
            graph_id=graph["graph_id"],
            version=graph["version"],
            type="graph_closed",
            at=at,
            data={
                "at": at,
                "by": graph["created_by"],
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
        if idempotency_key.startswith("pac-notify:workflow-request:"):
            request_id = idempotency_key.split(":", 3)[2]
            store = PacGraphStore(self.database, read_only=True)
            try:
                with store.read():
                    row = store._db.execute(
                        "SELECT w.*,g.specification_ref,g.specification_digest,n.owner,n.flag "
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
                    node = next(n for n in spec["nodes"] if n["id"] == row["node_id"])
                    predecessors = [
                        edge.from_node
                        for edge in store.edges(row["graph_id"])
                        if edge.to_node == row["node_id"] and edge.kind == "forward"
                    ]
                    inputs = [
                        n.to_json()
                        for name in predecessors
                        if (n := store.node(row["graph_id"], name)) is not None
                        and n.kind != "actor"
                    ]
                    remote_request = {
                        "graphId": row["graph_id"], "nodeId": row["node_id"],
                        "requestId": row["request_id"], "owner": recipient,
                        "inputToken": row["input_token"], "deadlineMs": row["deadline_ms"],
                        "role": node["role"], "firstOutputEta": node.get("first_output_eta"),
                        "humanGatesDeclared": node.get("human_gates") is not None,
                    }
                    command = f"{row['graph_id']} {row['node_id']} --request-id {row['request_id']}"
                    text = (
                        f"{node['task']}\n\n"
                        f"Input evidence references: {json.dumps(inputs, ensure_ascii=False)}\n"
                        f"Inspect current work before acting: hyprial workflow inspect {row['graph_id']} --node {row['node_id']} --json\n"
                        f"Graph: {row['graph_id']}; node: {row['node_id']}; role: {node['role']}\n"
                        f"Fixed deadline: {row['deadline_ms']} (epoch ms).\n"
                        f"Complete explicitly: hyprial workflow complete {command} --reason-ref <evidence-reference>\n"
                        f"Report failure: hyprial workflow fail {command} --reason-ref <failure-reference>\n"
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
        return self.downstream.send(
            recipient=recipient,
            text=text,
            sender=sender,
            conversation_id=conversation_id,
            idempotency_key=idempotency_key,
        )


class GraphWorkflowService:
    """Bounded resident cadence; the PAC database owns all durable facts."""

    def __init__(
        self,
        *,
        state_dir: Path,
        owner: str,
        machine: str,
        sender: NotificationSender,
        admit: Callable[[WorkflowSpec, str], None] | None = None,
        clock_ms: Callable[[], int] | None = None,
        logger: Any = None,
        start_thread: bool = True,
    ):
        self.database = default_database_path(state_dir)
        self.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.owner, self.machine = owner, machine
        self.sender = WorkflowSender(self.database, sender)
        self.admit = admit
        self.clock = clock_ms or (lambda: time_ns() // 1_000_000)
        self.logger = logger
        self._queue: queue.Queue[int | None] = queue.Queue(maxsize=1)
        self._closed = False
        self._asynchronous = start_thread
        self._delivery_queue: queue.PriorityQueue[tuple[int, int, str]] = (
            queue.PriorityQueue(maxsize=128)
        )
        self._delivery_active: set[str] = set()
        self._delivery_lock = threading.Lock()
        self._delivery_sequence = 0
        self._delivery_threads: list[threading.Thread] = []
        self._thread: threading.Thread | None = None
        # Construction is a readiness boundary.  Publishing a service whose
        # database could not be opened makes a later empty recovery look
        # healthy and lets callers enqueue work into a dead cadence.
        self._open().close()
        if start_thread:
            for index in range(2):
                worker = threading.Thread(
                    target=self._delivery_worker,
                    name=f"hyprial-workflow-notify-{index}",
                    daemon=True,
                )
                self._delivery_threads.append(worker)
                worker.start()
            self._thread = threading.Thread(
                target=self._run, name="hyprial-pac-workflow", daemon=True
            )
            self._thread.start()

    def _open(self, *, read_only: bool = False) -> PacGraphStore:
        try:
            return PacGraphStore(self.database, read_only=read_only)
        except (sqlite3.Error, OSError, RuntimeError, PacError) as error:
            raise WorkflowServiceError(
                ipc_errors.WORKFLOW_UNAVAILABLE, str(error)
            ) from error

    def _queue_delivery(self, graph_id: str, *, closed: bool) -> None:
        with self._delivery_lock:
            if self._closed or graph_id in self._delivery_active:
                return
            self._delivery_sequence += 1
            try:
                self._delivery_queue.put_nowait(
                    (int(closed), self._delivery_sequence, graph_id)
                )
            except queue.Full:
                return  # durable outbox remains eligible at the next cadence
            self._delivery_active.add(graph_id)

    def _delivery_worker(self) -> None:
        while not self._closed:
            try:
                _, _, graph_id = self._delivery_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                store = self._open()
                try:
                    self._drain(store, graph_id)
                finally:
                    store.close()
            except Exception as error:
                if self.logger:
                    self.logger(
                        "error",
                        "pac",
                        "workflow.notification_failed",
                        graphId=graph_id,
                        detail=str(error),
                    )
            finally:
                with self._delivery_lock:
                    self._delivery_active.discard(graph_id)

    def _run(self):
        while True:
            at = self._queue.get()
            if at is None:
                return
            try:
                self._tick(self.clock())
            except (
                Exception
            ) as error:  # one bad graph must not kill the resident cadence
                if self.logger:
                    self.logger(
                        "error", "pac", "workflow.tick_failed", detail=str(error)
                    )

    def submit_timer(self, observed_at_ms: int) -> None:
        if not self._closed:
            try:
                self._queue.put_nowait(observed_at_ms)
            except queue.Full:
                pass

    def close(self, timeout: float = 5.0) -> bool:
        self._closed = True
        if self._thread is None:
            return True
        try:
            self._queue.get_nowait()
        except queue.Empty:
            pass
        self._queue.put_nowait(None)
        deadline = monotonic() + timeout
        for thread in (self._thread, *self._delivery_threads):
            thread.join(max(0.0, deadline - monotonic()))
        return not any(
            thread.is_alive() for thread in (self._thread, *self._delivery_threads)
        )

    def recover(self) -> int:
        store = self._open()
        try:
            try:
                count = store._db.execute(
                    "SELECT COUNT(*) FROM workflow_graphs WHERE state IN ('running','held')"
                ).fetchone()[0]
            except (sqlite3.Error, OSError, RuntimeError, PacError) as error:
                raise WorkflowServiceError(
                    ipc_errors.WORKFLOW_UNAVAILABLE, str(error)
                ) from error
        finally:
            store.close()
        self.submit_timer(self.clock())
        return int(count)

    def start(
        self,
        *,
        yaml_text: str,
        sender: str,
        operation_key: str | None = None,
        routine_name: str | None = None,
        task_key: str | None = None,
    ) -> dict[str, Any]:
        if operation_key is not None and (
            not isinstance(operation_key, str)
            or not operation_key.strip()
            or len(operation_key) > 400
            or any(ord(c) < 32 for c in operation_key)
        ):
            raise WorkflowServiceError(
                ipc_errors.INVALID_ARGUMENT,
                "operation key must be nonempty, single-line text of at most 400 characters",
            )
        if self._closed:
            raise WorkflowServiceError(
                ipc_errors.WORKFLOW_UNAVAILABLE, "workflow service is closing"
            )
        try:
            spec = load_workflow_text(yaml_text)
            store = self._open()
            try:
                graph_id = (
                    replay_graph(
                        store,
                        spec,
                        sender=sender,
                        operation_key=operation_key,
                        routine_name=routine_name,
                        task_key=task_key,
                    )
                    if operation_key
                    else None
                )
                if graph_id is None:
                    if self.admit:
                        self.admit(spec, sender)
                    graph_id = compile_workflow(
                        store,
                        spec,
                        sender=sender,
                        machine=self.machine,
                        local_owner=self.owner,
                        operation_key=operation_key or uuid4().hex,
                        at=self.clock(),
                        routine_name=routine_name,
                        task_key=task_key,
                    )
            finally:
                store.close()
        except (WorkflowSchemaError, PacError) as error:
            raise WorkflowServiceError(error.code, str(error)) from error
        self.submit_timer(self.clock())
        return {
            "graphId": graph_id,
            "runId": graph_id,
            "backend": "pac",
            "state": self.status(run_id=graph_id)["state"],
        }

    def _request(
        self,
        store: PacGraphStore,
        graph: dict,
        node,
        row,
        token: str,
        at: int,
        reactor: PacReactor,
    ):
        request = f"workflow-request:{uuid4().hex}"
        round_no = store.set_event_count(graph["graph_id"], node.node_id) + 1
        store._db.execute(
            "UPDATE workflow_nodes SET state='requested',request_id=?,input_token=?,generation=generation+1 "
            "WHERE graph_id=? AND node_id=?",
            (request, token, graph["graph_id"], node.node_id),
        )
        _changed(
            store,
            graph,
            at,
            nodeId=node.node_id,
            state="requested",
            requestId=request,
            deadlineMs=row["deadline_ms"],
        )
        planned = PlannedNotification(
            event_id=request,
            edge=f"workflow:{node.node_id}:{request}",
            kind="turn",
            recipient=node.owner,
            node_id=node.node_id,
            round_no=round_no,
            text=turn_text(node.node_id, node.brief_ref, request, round_no),
            sender=graph["created_by"],
        )
        from hyprial.dispatch.identity import dispatch_message_id

        message_id = dispatch_message_id(
            f"pac:pac-notify:{request}:{planned.edge}"
        )
        store._db.execute(
            "INSERT INTO workflow_deliveries VALUES (?,?,?,?)",
            (message_id, graph["graph_id"], node.node_id, request),
        )
        reactor._insert_notifications(
            [planned],
            at,
            db=store._db,
            graph_id=graph["graph_id"],
            version=graph["version"],
        )

    def _failure(
        self,
        store: PacGraphStore,
        graph: dict,
        node_id: str,
        reason: str,
        at: int,
        reactor: PacReactor,
    ):
        store._db.execute(
            "UPDATE workflow_nodes SET state='failed',reason_ref=? WHERE graph_id=? AND node_id=?",
            (reason, graph["graph_id"], node_id),
        )
        event = f"workflow-failure:{uuid4().hex}"
        _changed(store, graph, at, nodeId=node_id, state="failed", reasonRef=reason)
        row = store._db.execute(
            "SELECT * FROM workflow_graphs WHERE graph_id=?", (graph["graph_id"],)
        ).fetchone()
        try:
            escalation = read_specification(row)["escalateTo"] or graph["created_by"]
        except PacError:
            escalation = graph["created_by"]
        alert = PlannedNotification(
            event_id=event,
            edge=f"workflow-failure:{node_id}:{event}",
            kind="actor_alert",
            recipient=escalation,
            node_id=node_id,
            round_no=None,
            sender=graph["created_by"],
            text=f"Workflow {graph['graph_id']} node {node_id} failed: {reason}. Policy: {row['on_failure']}.",
        )
        reactor._insert_notifications(
            [alert],
            at,
            db=store._db,
            graph_id=graph["graph_id"],
            version=graph["version"],
        )
        if row["on_failure"] == "terminate":
            close_workflow(
                store, graph, state="failed", reason=f"pac:node-failed:{node_id}", at=at
            )
        elif row["on_failure"] == "hold":
            store._db.execute(
                "UPDATE workflow_graphs SET state='held' WHERE graph_id=?",
                (graph["graph_id"],),
            )

    def _project(self, store: PacGraphStore, graph_id: str, at: int):
        db = store.write()
        try:
            graph = store.graph(graph_id)
            assert graph is not None
            meta = db.execute(
                "SELECT * FROM workflow_graphs WHERE graph_id=?", (graph_id,)
            ).fetchone()
            reactor = PacReactor(store, clock=lambda: at)
            if graph["closed_at"] is not None:
                if meta["state"] not in TERMINAL:
                    # An explicit end flag is acceptance; manual graph close
                    # without one is cancellation, never inferred success.
                    accepted = any(
                        n.kind == "end" and n.flag for n in store.nodes(graph_id)
                    )
                    close_workflow(
                        store,
                        graph,
                        state="completed" if accepted else "cancelled",
                        reason="pac:graph-closed",
                        at=at,
                    )
                db.commit()
                return
            nodes = {n.node_id: n for n in store.nodes(graph_id)}
            for row in db.execute(
                "SELECT * FROM workflow_nodes WHERE graph_id=?", (graph_id,)
            ).fetchall():
                if store.graph(graph_id)["closed_at"] is not None:
                    break
                node = nodes[row["node_id"]]
                if row["state"] in {"failed", "blocked", "cancelled"}:
                    continue
                if node.flag:
                    if row["input_token"] != input_token(
                        store, graph_id, node.node_id, ignore_actor=True
                    ):
                        if at > row["deadline_ms"]:
                            self._failure(
                                store,
                                graph,
                                node.node_id,
                                "pac:deadline-expired",
                                at,
                                reactor,
                            )
                            continue
                        db.execute(
                            "UPDATE workflow_nodes SET state='pending',reason_ref='pac:stale-inputs' WHERE graph_id=? AND node_id=?",
                            (graph_id, node.node_id),
                        )
                        continue
                    if row["state"] != "done":
                        db.execute(
                            "UPDATE workflow_nodes SET state='done',reason_ref=? WHERE graph_id=? AND node_id=?",
                            (node.flag_reason_ref, graph_id, node.node_id),
                        )
                    continue
                if row["state"] in {"failed", "blocked", "cancelled"}:
                    continue
                if at > row["deadline_ms"]:
                    self._failure(
                        store, graph, node.node_id, "pac:deadline-expired", at, reactor
                    )
                    continue
                if row["actor_node"]:
                    bad = db.execute(
                        "SELECT type FROM journal WHERE graph_id=? AND type IN ('actor_lost','actor_unowned','launch_failed') "
                        "AND json_extract(data_json,'$.nodeId')=? ORDER BY seq DESC LIMIT 1",
                        (graph_id, row["actor_node"]),
                    ).fetchone()
                    if bad:
                        self._failure(
                            store,
                            graph,
                            node.node_id,
                            f"pac:{bad['type']}",
                            at,
                            reactor,
                        )
                        continue
                token = input_token(store, graph_id, node.node_id)
                if row["state"] == "requested" and row["input_token"] != token:
                    db.execute(
                        "UPDATE workflow_nodes SET state='pending',request_id=NULL,input_token=NULL WHERE graph_id=? AND node_id=?",
                        (graph_id, node.node_id),
                    )
                    _changed(
                        store,
                        graph,
                        at,
                        nodeId=node.node_id,
                        state="withdrawn",
                        requestId=row["request_id"],
                    )
                elif row["state"] == "done":
                    db.execute(
                        "UPDATE workflow_nodes SET state='pending',request_id=NULL,input_token=NULL WHERE graph_id=? AND node_id=?",
                        (graph_id, node.node_id),
                    )
            graph = store.graph(graph_id)
            if graph["closed_at"] is not None:
                db.commit()
                return
            failed = db.execute(
                "SELECT node_id FROM workflow_nodes WHERE graph_id=? AND state='failed'",
                (graph_id,),
            ).fetchall()
            if failed and meta["on_failure"] == "terminate":
                close_workflow(
                    store,
                    graph,
                    state="failed",
                    reason=f"pac:node-failed:{failed[0]['node_id']}",
                    at=at,
                )
            elif failed and meta["on_failure"] == "hold":
                db.execute(
                    "UPDATE workflow_graphs SET state='held' WHERE graph_id=?",
                    (graph_id,),
                )
            else:
                # A failed predecessor cannot become a success by skipping it.
                while True:
                    changed = db.execute(
                        "UPDATE workflow_nodes SET state='blocked',reason_ref='pac:predecessor-failed' "
                        "WHERE graph_id=? AND state IN ('pending','requested') AND node_id IN ("
                        "SELECT e.to_node FROM edges e JOIN workflow_nodes p ON p.graph_id=e.graph_id AND p.node_id=e.from_node "
                        "WHERE e.graph_id=? AND e.kind='forward' AND p.state IN ('failed','blocked','cancelled'))",
                        (graph_id, graph_id),
                    ).rowcount
                    if not changed:
                        break
                rows = db.execute(
                    "SELECT * FROM workflow_nodes WHERE graph_id=?", (graph_id,)
                ).fetchall()
                if all(
                    row["state"] in {"done", "failed", "blocked", "cancelled"}
                    for row in rows
                ):
                    has_end = any(n.kind == "end" for n in nodes.values())
                    if failed or not has_end:
                        close_workflow(
                            store,
                            graph,
                            state="failed" if failed else "completed",
                            reason="pac:workflow-settled",
                            at=at,
                        )
                else:
                    for row in rows:
                        if row["state"] != "pending" or nodes[row["node_id"]].flag:
                            continue
                        token = input_token(store, graph_id, row["node_id"])
                        if token is not None:
                            self._request(
                                store,
                                graph,
                                nodes[row["node_id"]],
                                row,
                                token,
                                at,
                                reactor,
                            )
            db.commit()
        except BaseException:
            db.rollback()
            raise

    def _drain(self, store: PacGraphStore, graph_id: str):
        reactor = PacReactor(store, sender=self.sender, clock=self.clock)
        graph = store.graph(graph_id)
        assert graph is not None
        for item in [
            *store.notifications(graph_id),
            *store.notifications_by_synthetic_event(graph_id),
        ]:
            if item.message_id is not None:
                continue
            is_alert = item.event_id.startswith("workflow-failure:")
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
                reactor._mark_delivered(item.event_id, item.edge, message)
            except Exception as error:
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
                for row in store._db.execute(
                    "SELECT w.graph_id FROM workflow_graphs w JOIN graphs g USING(graph_id) "
                    "WHERE g.closed_at IS NULL OR w.state NOT IN ('completed','failed','cancelled') OR EXISTS "
                    "(SELECT 1 FROM notifications n WHERE json_extract(n.plan_json,'$.graphId')=g.graph_id "
                    "AND n.message_id IS NULL AND n.event_id LIKE 'workflow-failure:%') "
                    "ORDER BY (g.closed_at IS NOT NULL), g.created_at"
                )
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
                        if self.clock() > row["deadline_ms"]
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
                        "reasonRef": rows[node.node_id]["reason_ref"]
                        or node.flag_reason_ref,
                        "actorNode": rows[node.node_id]["actor_node"],
                    }
                    for node in store.nodes(run_id)
                    if node.node_id in rows
                ]
                for projected in nodes:
                    projected["localHumanCanComplete"] = (
                        projected["owner"] == f"user:{self.owner}"
                        and projected["state"] == "requested"
                        and self.clock() <= projected["deadlineMs"]
                    )
                    notification = store._db.execute(
                        "SELECT message_id,delivered_at FROM notifications WHERE event_id=?",
                        (projected["requestId"],),
                    ).fetchone()
                    projected["deliveryState"] = (
                        (
                            "accepted"
                            if notification["message_id"]
                            else "planned"
                            if projected["state"] == "requested"
                            and graph["closed_at"] is None
                            else "withdrawn"
                        )
                        if notification
                        else None
                    )
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
                }
        finally:
            store.close()

    def list(self, *, limit: int = 50, viewer: str | None = None) -> dict[str, Any]:
        store = self._open(read_only=True)
        try:
            with store.read():
                rows = store._db.execute(
                    "SELECT g.*,w.state,w.on_failure,w.routine_name FROM graphs g JOIN workflow_graphs w USING(graph_id) "
                    "WHERE (? IS NULL OR g.created_by=? OR EXISTS (SELECT 1 FROM nodes n WHERE n.graph_id=g.graph_id AND n.owner=?)) "
                    "ORDER BY g.created_at DESC,g.graph_id LIMIT ?",
                    (viewer, viewer, viewer, min(max(limit, 1), 500)),
                ).fetchall()
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
                        }
                        for r in rows
                    ]
                }
        finally:
            store.close()

    def cancel(
        self, *, run_id: str, actor: str, reason_ref: str = "workflow:cancelled"
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
        from hyprial.pac.lifecycle import request_actor_stop

        store = self._open()
        try:
            with store.write():
                graph, worker = self._workflow_actor(store, graph_id, actor_name)
                if graph["created_by"] != actor:
                    raise WorkflowServiceError(
                        "WORKFLOW_NOT_OWNER", "only the workflow owner can stop its worker"
                    )
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
                    "SELECT on_failure FROM workflow_graphs WHERE graph_id=?", (graph_id,)
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
                        "UPDATE workflow_graphs SET state='held',reason_ref=? WHERE graph_id=?",
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
        from hyprial.pac.lifecycle import request_actor_wake

        store = self._open()
        try:
            with store.write():
                graph, worker = self._workflow_actor(store, graph_id, actor_name)
                if graph["created_by"] != actor:
                    raise WorkflowServiceError(
                        "WORKFLOW_NOT_OWNER", "only the workflow owner can restart its worker"
                    )
                store._db.execute(
                    "UPDATE workflow_nodes SET state='pending',request_id=NULL,input_token=NULL,reason_ref=NULL "
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
    ):
        store = self._open()
        try:
            with store.write():
                graph = store.graph(graph_id)
                node = store.node(graph_id, node_id)
                row = store._db.execute(
                    "SELECT * FROM workflow_nodes WHERE graph_id=? AND node_id=?",
                    (graph_id, node_id),
                ).fetchone()
                if graph is None or node is None or node.owner != actor:
                    raise WorkflowServiceError(
                        "WORKFLOW_NOT_OWNER", "only the node owner can report failure"
                    )
                if (
                    graph["closed_at"] is not None
                    or row is None
                    or row["request_id"] != request_id
                    or row["state"] != "requested"
                    or self.clock() > row["deadline_ms"]
                    or row["input_token"] != input_token(store, graph_id, node_id)
                ):
                    raise WorkflowServiceError(
                        "WORKFLOW_REQUEST_STALE",
                        "failure belongs to a withdrawn or completed request",
                    )
                self._failure(
                    store, graph, node_id, reason_ref, self.clock(), PacReactor(store)
                )
                store._db.execute("INSERT INTO workflow_outcome_receipts VALUES (?,?,?,?,?)",
                    (request_id, actor, "fail", reason_ref,
                     json.dumps({"ok": True, "requestId": request_id})))
        finally:
            store.close()
        self._tick()
        return self.status(run_id=graph_id)
