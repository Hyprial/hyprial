"""GraphWorkflowService core: construction, delivery and projection."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import queue
import sqlite3
import threading
from time import time_ns, monotonic
from typing import Any
from uuid import uuid4

from hyprial.kernel import ipc_errors

from hyprial.identity import PacError
from hyprial.daemon.impl.pac.graphs.reactor  import (
    NotificationSender,
    PacReactor,
    PlannedNotification,
    turn_text,
)
from hyprial.daemon.impl.pac.storage.store  import PacGraphStore, default_database_path
from hyprial.daemon.impl.pac.workflows.graphs  import (
    compile_workflow,
    input_token,
    read_specification,
    replay_graph,
)
from hyprial.daemon.impl.pac.contracts.workflow  import WorkflowSchemaError, WorkflowSpec, load_workflow_text
from hyprial.daemon.impl.pac.workflows.runtime.types import (
    NotificationReceiptAuthority,
    TERMINAL,
    WorkflowSender,
    WorkflowServiceError,
    WorkflowTickAuthority,
    _changed,
    _handoff_sender,
    close_workflow,
)

class _GraphWorkflowServiceCore:
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
        delivery_receipt: NotificationReceiptAuthority | None = None,
        graph_authority: WorkflowTickAuthority | None = None,
    ):
        if graph_authority is not None and not start_thread:
            raise ValueError("PAC graph authority requires asynchronous workflow delivery")
        self.database = default_database_path(state_dir)
        self.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.owner, self.machine = owner, machine
        self.sender = WorkflowSender(self.database, sender)
        self.admit = admit
        self.clock = clock_ms or (lambda: time_ns() // 1_000_000)
        self.logger = logger
        self._delivery_receipt = delivery_receipt
        self._graph_authority = graph_authority
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
        self._open(read_only=self._graph_authority is not None).close()
        if self._graph_authority is not None:
            self._graph_authority.attach_workflow(self)
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
                store = self._open(read_only=self._delivery_receipt is not None)
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
                if self._graph_authority is None:
                    self._tick(self.clock())
                else:
                    self._graph_authority.workflow_tick(self.clock())
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
        store = self._open(read_only=True)
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
            graph_id = None
            if operation_key:
                replay_store = self._open(
                    read_only=self._graph_authority is not None
                )
                try:
                    graph_id = replay_graph(
                        replay_store,
                        spec,
                        sender=sender,
                        operation_key=operation_key,
                        routine_name=routine_name,
                        task_key=task_key,
                    )
                finally:
                    replay_store.close()
            if self._graph_authority is not None:
                if graph_id is None:
                    if self.admit:
                        admitted = self.admit(spec, sender)
                        if admitted is not None:
                            spec = admitted
                    graph_id = self._graph_authority.start_workflow(
                        yaml_text=yaml_text,
                        sender=sender,
                        machine=self.machine,
                        local_owner=self.owner,
                        operation_key=operation_key or uuid4().hex,
                        at=self.clock(),
                        routine_name=routine_name,
                        task_key=task_key,
                        **({"spec": spec} if spec.expansion_policy is not None else {}),
                    )
            else:
                if graph_id is None:
                    store = self._open()
                    try:
                        if self.admit:
                            admitted = self.admit(spec, sender)
                            if admitted is not None:
                                spec = admitted
                        graph_id = compile_workflow(
                            store, spec, sender=sender, machine=self.machine,
                            local_owner=self.owner,
                            operation_key=operation_key or uuid4().hex,
                            at=self.clock(), routine_name=routine_name,
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
        if row["node_kind"] == "expansion":
            binding = store._db.execute(
                "SELECT 1 FROM workflow_expansions "
                "WHERE parent_graph_id=? AND parent_node_id=?",
                (graph["graph_id"], node.node_id),
            ).fetchone()
            if binding is None:
                store._db.execute(
                    "UPDATE workflow_nodes SET state='failed',reason_ref=? "
                    "WHERE graph_id=? AND node_id=?",
                    (
                        "pac:expansion-context-missing",
                        graph["graph_id"],
                        node.node_id,
                    ),
                )
            return
        request = f"workflow-request:{uuid4().hex}"
        round_no = store.set_event_count(graph["graph_id"], node.node_id) + 1
        deadline = row["deadline_ms"]
        graph_cap = store._db.execute(
            "SELECT expansion_deadline_ms FROM workflow_graphs WHERE graph_id=?",
            (graph["graph_id"],),
        ).fetchone()[0]
        if deadline is None:
            timeout = row["timeout_ms"]
            assert timeout is not None  # new rows always store their relative timeout
            deadline = at + timeout
            if graph_cap is not None:
                deadline = min(deadline, int(graph_cap))
            store._db.execute(
                "UPDATE workflow_nodes SET deadline_ms=? WHERE graph_id=? AND node_id=?",
                (deadline, graph["graph_id"], node.node_id),
            )
            metadata = store._db.execute(
                "SELECT * FROM workflow_graphs WHERE graph_id=?",
                (graph["graph_id"],),
            ).fetchone()
            clock_owner = (
                read_specification(metadata)["escalateTo"] or graph["created_by"]
            )
            store._db.execute(
                "INSERT INTO nodes(graph_id,node_id,owner,brief_ref,kind,deadline_ms,guarded_by_node_id) "
                "VALUES (?,?,?,?,'clock',?,?)",
                (
                    graph["graph_id"],
                    f"_deadline.{node.node_id}",
                    clock_owner,
                    f"workflow:{graph['graph_id']}#{node.node_id}:deadline",
                    deadline,
                    node.node_id,
                ),
            )
        elif graph_cap is not None and deadline > graph_cap:
            deadline = int(graph_cap)
            store._db.execute(
                "UPDATE workflow_nodes SET deadline_ms=? "
                "WHERE graph_id=? AND node_id=?",
                (deadline, graph["graph_id"], node.node_id),
            )
            store._db.execute(
                "UPDATE nodes SET deadline_ms=? WHERE graph_id=? AND node_id=?",
                (deadline, graph["graph_id"], f"_deadline.{node.node_id}"),
            )
        store._db.execute(
            "UPDATE workflow_nodes SET state='requested',request_id=?,input_token=?,generation=generation+1,"
            "reason_ref=NULL,output_text=NULL "
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
            deadlineMs=deadline,
        )
        planned = PlannedNotification(
            event_id=request,
            edge=f"workflow:{node.node_id}:{request}",
            kind="turn",
            recipient=node.owner,
            node_id=node.node_id,
            round_no=round_no,
            text=turn_text(node.node_id, node.brief_ref, request, round_no),
            sender=_handoff_sender(store, graph, node.node_id),
        )
        from hyprial.daemon.impl.dispatch.identity import dispatch_message_id

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
        output_text: str | None = None,
    ):
        store._db.execute(
            "UPDATE workflow_nodes SET state='failed',reason_ref=?,output_text=? "
            "WHERE graph_id=? AND node_id=?",
            (reason, output_text, graph["graph_id"], node_id),
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
            text=(
                f"Workflow {graph['graph_id']} node {node_id} failed: {reason}. "
                f"Policy: {row['on_failure']}."
                + (f"\nOutput:\n{output_text}" if output_text is not None else "")
            ),
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
            from hyprial.daemon.impl.pac.workflows.expansion.projection import (
                project_child_terminals_in_transaction,
            )

            graph = store.graph(graph_id)
            assert graph is not None
            meta = db.execute(
                "SELECT * FROM workflow_graphs WHERE graph_id=?", (graph_id,)
            ).fetchone()
            if (
                meta["expansion_deadline_ms"] is not None
                and at > meta["expansion_deadline_ms"]
                and meta["state"] not in TERMINAL
            ):
                close_workflow(
                    store,
                    graph,
                    state="cancelled",
                    reason="pac:deadline-expired",
                    at=at,
                )
                db.commit()
                return
            project_child_terminals_in_transaction(store, graph, at=at)
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
                        if (
                            row["deadline_ms"] is not None
                            and at > row["deadline_ms"]
                        ):
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
                            "UPDATE workflow_nodes SET state='pending',reason_ref='pac:stale-inputs',output_text=NULL "
                            "WHERE graph_id=? AND node_id=?",
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
                if row["deadline_ms"] is not None and at > row["deadline_ms"]:
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
                        "UPDATE workflow_nodes SET state='pending',request_id=NULL,input_token=NULL,output_text=NULL "
                        "WHERE graph_id=? AND node_id=?",
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
                        "UPDATE workflow_nodes SET state='pending',request_id=NULL,input_token=NULL,output_text=NULL "
                        "WHERE graph_id=? AND node_id=?",
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
