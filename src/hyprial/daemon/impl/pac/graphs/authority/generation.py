"""Generation-scoped write and notification executor.

One instance runs per database generation.  It holds generation identifiers
and callbacks only; durable state stays on the owning PacGraphAuthority.
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hyprial.identity import (
    PAC_GRAPH_NOT_FOUND,
    PAC_GRAPH_NOT_OWNER,
    PAC_OPERATION_KEY_CONFLICT,
    PacError,
)
from hyprial.daemon.impl.pac.graphs.edits  import activate_graph, add_node, close_graph, create_graph, show_graph
from hyprial.daemon.impl.pac.actors.coordinator  import ActorCoordinator, request_actor_stop
from hyprial.daemon.impl.pac.storage.migrations  import unrewritten_owners_note
from hyprial.daemon.impl.pac.graphs.reactor  import PacReactor, planned_to_json
from hyprial.daemon.impl.pac.storage.store  import PacGraphStore
from hyprial.daemon.impl.pac.workflows.graphs  import compile_workflow, replay_graph
from hyprial.daemon.impl.pac.contracts.workflow  import load_workflow_text
from hyprial.daemon.impl.pac.graphs.authority.commands import (
    ActivateGraph,
    CancelWorkflow,
    ClockTick,
    CloseGraph,
    EnqueueRemoteOutcome,
    EnsureRemoteKey,
    FailWorkflow,
    GraphCommand,
    GraphCommandCompleted,
    HarnessOutcome,
    MarkRemoteAttempt,
    NotificationDelivered,
    ProbeWorkflow,
    PruneRemoteRequest,
    QueryRoutineOccurrence,
    ReconcileActor,
    RecordDelivery,
    RecordFailure,
    RecordRemoteResult,
    RequestPruned,
    ResetFlag,
    RestartWorkflowWorker,
    SetFlag,
    StageRemoteOffer,
    StartLegacyRoutine,
    StartWorkflow,
    StopActor,
    StopWorkflowWorker,
    WorkflowTick,
    _ActorEffect,
    _ActorEffectCompleted,
    _ActorEffectNeeded,
    _DeferredResolver,
    _DeferredRuntime,
    _RESTORE_FACT_COMMANDS,
)

if TYPE_CHECKING:  # forward reference to the owner class, no runtime import cycle
    from hyprial.daemon.impl.pac.graphs.authority import PacGraphAuthority


class _Generation:
    def __init__(
        self, generation: int, database: Path,
        complete: Any, wake_delivery: Any, workflow_service: Any,
        owner: PacGraphAuthority,
    ) -> None:
        self.generation = generation
        self.database = database
        self.complete = complete
        self.wake_delivery = wake_delivery
        self.workflow_service = workflow_service
        self.owner = owner

    def __call__(
        self, command: GraphCommand | NotificationDelivered
        | ReconcileActor | _ActorEffectCompleted,
    ) -> None:
        if isinstance(command, ReconcileActor):
            # Only an external queued wake owns this exact claim. Release
            # before reading mutable state so arrivals during handling can
            # enqueue one follow-up. Internal continuations own no claim.
            self.owner._retire_reconcile_claim(command)
            try:
                self._reconcile_actor(command)
            except Exception as error:
                self.owner._log_actor_failure(_ActorEffect(
                    self.generation, command.graph_id, command.node_id,
                    "reconcile", "",
                ), f"{type(error).__name__}: {error}")
            return
        if isinstance(command, _ActorEffectCompleted):
            try:
                self._actor_effect_completed(command)
            except Exception as error:
                self.owner._log_actor_failure(command.effect, f"{type(error).__name__}: {error}")
            return
        if isinstance(command, NotificationDelivered):
            if command.generation != self.generation:
                return
            store = PacGraphStore(self.database)
            try:
                PacReactor(store)._mark_delivered(
                    command.event_id, command.edge, command.message_id
                )
            finally:
                store.close()
            return
        try:
            result = self._write(command)
        except Exception as error:
            self.complete(GraphCommandCompleted(command.correlation_id, self.generation, error=error))
        else:
            if isinstance(command, _RESTORE_FACT_COMMANDS):
                self.owner._refresh_restore_facts()
            self.complete(GraphCommandCompleted(command.correlation_id, self.generation, result=result))

    def _reconcile_actor(self, command: ReconcileActor) -> None:
        if command.generation not in (0, self.generation):
            return
        runtime = self.owner._actor_runtime
        if runtime is None:
            return
        if command.observation is None:
            store = PacGraphStore(self.database, read_only=True)
            try:
                node = store.node(command.graph_id, command.node_id)
                cleanup = store.workflow_worker_cleanup_intent(
                    command.graph_id, command.node_id
                )
                activation = store._db.execute(
                    "SELECT operation_id FROM actor_activations "
                    "WHERE graph_id=? AND node_id=?",
                    (command.graph_id, command.node_id),
                ).fetchone()
            finally:
                store.close()
            if node is None or node.kind != "actor" or node.actor_name is None:
                return
            self.owner._submit_actor_effect(_ActorEffect(
                self.generation, command.graph_id, command.node_id,
                "observe", node.actor_name,
                operation_id=(
                    None if cleanup is None else str(cleanup["operation_id"])
                ),
                expected_activation_id=(
                    None if activation is None else str(activation["operation_id"])
                ),
            ))
            return
        store = PacGraphStore(self.database)
        try:
            activation = store._db.execute(
                "SELECT operation_id FROM actor_activations WHERE graph_id=? AND node_id=?",
                (command.graph_id, command.node_id),
            ).fetchone()
            if (
                command.expected_activation_id is not None
                and (
                    activation is None
                    or activation["operation_id"] != command.expected_activation_id
                )
            ):
                return
            coordinator = ActorCoordinator(
                store, _DeferredRuntime(command),
                daemon_epoch=self.owner._actor_epoch,
                resolver=_DeferredResolver(command),
                defer_notifications=True,
                on_skip=self.owner._actor_skip,
            )
            try:
                coordinator.reconcile(command.graph_id, command.node_id)
            except _ActorEffectNeeded as needed:
                activation = store._db.execute(
                    "SELECT operation_id FROM actor_activations WHERE graph_id=? AND node_id=?",
                    (command.graph_id, command.node_id),
                ).fetchone()
                self.owner._submit_actor_effect(_ActorEffect(
                    self.generation, command.graph_id, command.node_id,
                    needed.kind, needed.actor_name,
                    observation=command.observation,
                    launch_ref=needed.launch_ref, node=needed.node,
                    launch=needed.launch, operation_id=needed.operation_id,
                    identity_marker=needed.identity_marker,
                    expected_activation_id=(
                        None if activation is None else str(activation["operation_id"])
                    ),
                    resolved=command.resolved,
                ))
        finally:
            store.close()
            self.wake_delivery()

    @staticmethod
    def _routine_occurrence(
        store: PacGraphStore, command: QueryRoutineOccurrence
    ) -> dict[str, Any]:
        if not command.task_uuid:
            prefix = f"routine:{command.routine_name}:"
            row = store._db.execute(
                "SELECT COALESCE(MAX(g.created_at), 0) "
                "FROM graphs g LEFT JOIN workflow_graphs w USING(graph_id) "
                "WHERE w.routine_name=? OR g.operation_key LIKE ? "
                "OR g.operation_key LIKE ?",
                (
                    command.routine_name,
                    f"{prefix}%",
                    f"workflow:{prefix}%",
                ),
            ).fetchone()
            return {"rearmBoundary": 0 if row is None else int(row[0])}

        def state(row: Any) -> str:
            workflow_state = row["workflow_state"]
            if workflow_state is not None:
                return (
                    "done"
                    if str(workflow_state) == "completed"
                    else "escalated"
                    if str(workflow_state) in {"failed", "cancelled"}
                    else "running"
                )
            work = store.node(str(row["graph_id"]), "work")
            deadline = store.node(str(row["graph_id"]), "deadline")
            if work is not None and work.flag:
                return "done"
            if (
                deadline is not None
                and deadline.deadline_ms is not None
                and command.observed_at_ms > deadline.deadline_ms
            ) or row["closed_at"] is not None:
                return "escalated"
            return "running"

        existing = store._db.execute(
            "SELECT g.*,w.state AS workflow_state,w.routine_name,w.task_key "
            "FROM graphs g LEFT JOIN workflow_graphs w USING(graph_id) "
            "WHERE g.operation_key IN (?,?) "
            "ORDER BY CASE WHEN g.operation_key=? THEN 0 ELSE 1 END LIMIT 1",
            (
                f"workflow:{command.operation_key}",
                command.operation_key,
                f"workflow:{command.operation_key}",
            ),
        ).fetchone()
        if existing is not None:
            if str(existing["created_by"]) != command.sender:
                raise PacError(
                    PAC_OPERATION_KEY_CONFLICT,
                    "source occurrence key belongs to another publication",
                )
            if existing["workflow_state"] is not None and (
                str(existing["routine_name"]) != command.routine_name
                or str(existing["task_key"]) != command.task_uuid
            ):
                raise PacError(
                    PAC_OPERATION_KEY_CONFLICT,
                    "source occurrence key belongs to another routine task",
                )
            return {
                "graphId": str(existing["graph_id"]),
                "state": state(existing),
                "legacy": existing["workflow_state"] is None,
                "existing": True,
            }

        base = f"routine:{command.routine_name}:{command.task_uuid}"
        rows = store._db.execute(
            "SELECT g.*,w.state AS workflow_state,w.routine_name,w.task_key "
            "FROM graphs g LEFT JOIN workflow_graphs w USING(graph_id) "
            "WHERE (w.routine_name=? AND w.task_key=?) "
            "OR (w.graph_id IS NULL AND (g.operation_key=? "
            "OR substr(g.operation_key,1,?)=?)) "
            "ORDER BY g.created_at DESC,g.graph_id DESC",
            (
                command.routine_name,
                command.task_uuid,
                base,
                len(base) + 1,
                f"{base}:",
            ),
        ).fetchall()
        for row in rows:
            if int(row["created_at"]) <= command.rearm_after_created_at_ms:
                continue
            key = str(row["operation_key"] or "")
            if row["workflow_state"] is None and key != base:
                suffix = key[len(base) + 1 :]
                if not suffix.isdigit():
                    continue
            settled = state(row)
            if settled != "running":
                return {
                    "graphId": str(row["graph_id"]),
                    "state": settled,
                    "blocked": settled == "escalated",
                    "existing": False,
                }
        return {
            "graphId": None,
            "state": None,
            "blocked": False,
            "existing": False,
        }
    def _actor_effect_completed(self, completion: _ActorEffectCompleted) -> None:
        effect = completion.effect
        self.owner._release_actor_effect(
            effect.graph_id, effect.node_id, completion.generation
        )
        if completion.generation != self.generation:
            return
        if completion.worktree_result is not None:
            state = completion.worktree_result.get("state")
            if state in {"prepared", "removed", "retained", "attention"}:
                store = PacGraphStore(self.database)
                try:
                    with store.write():
                        store._db.execute(
                            "UPDATE workflow_worktrees SET state=?,reason=?,updated_at=? "
                            "WHERE graph_id=? AND (node_id=? OR actor_node=?) "
                            "AND state IN ('planned','prepared','cleanup_pending') "
                            "AND (? != 'prepared' OR state='planned')",
                            (
                                state,
                                completion.worktree_result.get("reason"),
                                int(time.time_ns() // 1_000_000),
                                effect.graph_id,
                                effect.node_id,
                                effect.node_id,
                                state,
                            ),
                        )
                finally:
                    store.close()
        if effect.kind == "worktree-cleanup":
            if completion.error is not None:
                self.owner._log_actor_failure(effect, completion.error)
            return
        if completion.error is not None:
            if effect.kind in {"resolve", "start"}:
                self._record_actor_failure(effect, completion.error)
            elif effect.kind in {"observe", "stop"}:
                self._record_cleanup_effect_failure(
                    effect,
                    completion.error_code or "lifecycle_manager_unavailable",
                    completion.error_type or "RuntimeError",
                    completion.error,
                )
            else:
                self.owner._log_actor_failure(effect, completion.error)
            return
        if effect.expected_activation_id is not None:
            store = PacGraphStore(self.database, read_only=True)
            try:
                current = store._db.execute(
                    "SELECT operation_id FROM actor_activations "
                    "WHERE graph_id=? AND node_id=?",
                    (effect.graph_id, effect.node_id),
                ).fetchone()
                if (
                    current is None
                    or current["operation_id"] != effect.expected_activation_id
                ):
                    return
            finally:
                store.close()
        self._reconcile_actor(ReconcileActor(
            effect.graph_id, effect.node_id, self.generation,
            observation=(
                completion.observation if effect.kind == "observe"
                else effect.observation
            ),
            resolved=(
                completion.resolved if effect.kind == "resolve"
                else effect.resolved
            ),
            completed_kind=(effect.kind if effect.kind in {"start", "stop"} else None),
            completed_operation_id=effect.operation_id,
            completed_observation=(
                completion.observation if effect.kind in {"start", "stop"} else None
            ),
            expected_activation_id=effect.expected_activation_id,
        ))

    def _record_cleanup_effect_failure(
        self, effect: _ActorEffect, reason: str, error_type: str, detail: str
    ) -> None:
        store = PacGraphStore(self.database)
        try:
            if effect.kind == "observe" and effect.operation_id is None:
                self.owner._log_actor_failure(effect, detail)
                return
            if effect.kind == "observe" and effect.expected_activation_id is not None:
                activation = store._db.execute(
                    "SELECT operation_id FROM actor_activations "
                    "WHERE graph_id=? AND node_id=?",
                    (effect.graph_id, effect.node_id),
                ).fetchone()
                if (
                    activation is None
                    or activation["operation_id"] != effect.expected_activation_id
                ):
                    return
            if effect.kind == "stop":
                activation = store._db.execute(
                    "SELECT operation_id FROM actor_activations "
                    "WHERE graph_id=? AND node_id=?",
                    (effect.graph_id, effect.node_id),
                ).fetchone()
                if (
                    activation is None
                    or effect.expected_activation_id is None
                    or activation["operation_id"] != effect.expected_activation_id
                    or effect.operation_id != effect.expected_activation_id
                ):
                    return
            coordinator = ActorCoordinator(
                store,
                _DeferredRuntime(
                    ReconcileActor(
                        effect.graph_id,
                        effect.node_id,
                        self.generation,
                        observation=effect.observation,
                    )
                ),
                daemon_epoch=self.owner._actor_epoch,
                resolver=_DeferredResolver(
                    ReconcileActor(
                        effect.graph_id,
                        effect.node_id,
                        self.generation,
                        observation=effect.observation,
                    )
                ),
                defer_notifications=True,
                on_skip=self.owner._actor_skip,
            )
            settled = coordinator._settle_cleanup_effect_failure(
                effect.graph_id,
                effect.node_id,
                operation_id=effect.operation_id,
                reason=reason,
                error_type=error_type,
                present=effect.kind == "stop",
                detail=detail,
            )
            if not settled:
                self.owner._log_actor_failure(effect, detail)
        finally:
            store.close()
            self.wake_delivery()

    def _record_actor_failure(self, effect: _ActorEffect, detail: str) -> None:
        store = PacGraphStore(self.database)
        try:
            node = store.node(effect.graph_id, effect.node_id)
            row = store._db.execute(
                "SELECT * FROM actor_activations WHERE graph_id=? AND node_id=?",
                (effect.graph_id, effect.node_id),
            ).fetchone()
            if (
                node is None or row is None or effect.expected_activation_id is None
                or row["operation_id"] != effect.expected_activation_id
            ):
                return
            coordinator = ActorCoordinator(
                store, _DeferredRuntime(ReconcileActor(
                    effect.graph_id, effect.node_id, self.generation,
                    observation=effect.observation,
                )),
                daemon_epoch=self.owner._actor_epoch,
                resolver=_DeferredResolver(ReconcileActor(
                    effect.graph_id, effect.node_id, self.generation,
                    observation=effect.observation,
                )),
                defer_notifications=True,
            )
            coordinator._launch_failed(
                effect.graph_id, node, dict(row), RuntimeError(detail)
            )
        finally:
            store.close()
            self.wake_delivery()

    def _write(self, command: GraphCommand) -> dict[str, Any]:
        if isinstance(command, WorkflowTick):
            service = self.workflow_service()
            if service is None:
                raise RuntimeError("PAC workflow cadence has no attached service")
            service._tick(command.observed_at_ms)
            return {"ok": True}
        if isinstance(command, (CancelWorkflow, StopWorkflowWorker,
                                RestartWorkflowWorker, FailWorkflow)):
            service = self.workflow_service()
            if service is None:
                raise RuntimeError("PAC workflow mutation has no attached service")
            if isinstance(command, CancelWorkflow):
                return service._cancel_direct(
                    run_id=command.run_id, actor=command.actor,
                    reason_ref=command.reason_ref,
                )
            if isinstance(command, StopWorkflowWorker):
                return service._stop_worker_direct(
                    graph_id=command.graph_id, actor_name=command.actor_name,
                    actor=command.actor,
                )
            if isinstance(command, RestartWorkflowWorker):
                return service._restart_worker_direct(
                    graph_id=command.graph_id, actor_name=command.actor_name,
                    actor=command.actor,
                )
            return service._fail_direct(
                graph_id=command.graph_id, node_id=command.node_id,
                actor=command.actor, request_id=command.request_id,
                reason_ref=command.reason_ref, output_text=command.output_text,
            )
        if isinstance(command, (HarnessOutcome, RequestPruned)):
            service = self.workflow_service()
            if service is None:
                raise RuntimeError("PAC outcome has no attached workflow service")
            if isinstance(command, HarnessOutcome):
                accepted = service._record_harness_outcome_direct(
                    message_id=command.message_id, recipient=command.recipient,
                    failed=command.failed, failure_code=command.failure_code,
                )
            else:
                accepted = service._record_request_pruned_direct(
                    message_id=command.message_id, recipient=command.recipient,
                )
            return {"accepted": accepted}
        store = PacGraphStore(self.database)
        try:
            if isinstance(command, QueryRoutineOccurrence):
                return self._routine_occurrence(store, command)
            if isinstance(command, EnsureRemoteKey):
                with store.write():
                    store._db.execute(
                        "INSERT OR IGNORE INTO remote_workflow_key VALUES (1,?)",
                        (secrets.token_bytes(32),),
                    )
                    secret = bytes(store._db.execute(
                        "SELECT secret FROM remote_workflow_key WHERE singleton=1"
                    ).fetchone()[0])
                return {"secret": secret}
            if isinstance(command, StageRemoteOffer):
                grant = json.loads(command.grant_json)
                with store.write():
                    old = store._db.execute(
                        "SELECT grant_json FROM remote_workflow_requests WHERE request_id=?",
                        (grant["requestId"],),
                    ).fetchone()
                    if old and json.loads(old[0]) != grant:
                        raise PacError("WORKFLOW_REMOTE_INVALID", "request grant changed on replay")
                    store._db.execute(
                        "INSERT OR IGNORE INTO remote_workflow_requests VALUES (?,?,?,?,?,?,?,?)",
                        (
                            grant["requestId"], grant["graphId"], grant["nodeId"],
                            grant["owner"], grant["origin"], grant["messageId"],
                            grant["deadlineMs"], command.grant_json,
                        ),
                    )
                return {"ok": True}
            if isinstance(command, EnqueueRemoteOutcome):
                with store.write():
                    row = store._db.execute(
                        "SELECT * FROM remote_workflow_outbox "
                        "WHERE request_id=?", (command.request_id,),
                    ).fetchone()
                    desired = (
                        command.action,
                        command.reason_ref,
                        command.output_text,
                        command.expansion_text,
                        command.expansion_digest,
                    )
                    if row is not None:
                        existing = (
                            row["action"],
                            row["reason_ref"],
                            row["output_text"],
                            row["expansion_text"],
                            row["expansion_digest"],
                        )
                        if existing != desired:
                            prior = (
                                json.loads(row["result_json"])
                                if row["result_json"] is not None
                                else None
                            )
                            correctable = bool(
                                command.action == "complete"
                                and command.expansion_text is not None
                                and prior
                                and prior.get("returnState") == "rejected"
                                and prior.get("error", {}).get("code")
                                == "PAC_EXPANSION_INVALID"
                            )
                            if not correctable:
                                raise PacError(
                                    "WORKFLOW_OUTCOME_CONFLICT",
                                    "request already has a queued or accepted outcome",
                                )
                            store._db.execute(
                                "UPDATE remote_workflow_outbox SET action=?,"
                                "reason_ref=?,output_text=?,expansion_text=?,"
                                "expansion_digest=?,attempt_no=attempt_no+1,"
                                "result_json=NULL WHERE request_id=?",
                                (*desired, command.request_id),
                            )
                            return {"ok": True}
                    store._db.execute(
                        "INSERT OR IGNORE INTO remote_workflow_outbox"
                        "(request_id,action,reason_ref,result_json,output_text,"
                        "expansion_text,expansion_digest,attempt_no) "
                        "VALUES (?,?,?,NULL,?,?,?,0)",
                        (command.request_id, command.action, command.reason_ref,
                         command.output_text, command.expansion_text,
                         command.expansion_digest),
                    )
                return {"ok": True}
            if isinstance(command, MarkRemoteAttempt):
                with store.write():
                    store._db.execute(
                        "UPDATE remote_workflow_outbox SET attempted_at=? WHERE request_id=?",
                        (command.attempted_at_ms, command.request_id),
                    )
                return {"ok": True}
            if isinstance(command, RecordRemoteResult):
                with store.write():
                    store._db.execute(
                        "UPDATE remote_workflow_outbox SET result_json=? "
                        "WHERE request_id=? AND attempt_no=? AND result_json IS NULL",
                        (command.result_json, command.request_id, command.attempt_no),
                    )
                return {"ok": True}
            if isinstance(command, PruneRemoteRequest):
                with store.write():
                    row = store._db.execute(
                        "SELECT action,reason_ref FROM remote_workflow_outbox WHERE request_id=?",
                        (command.request_id,),
                    ).fetchone()
                    if row and tuple(row) != ("fail", "pac:request-expired"):
                        return {"accepted": False}
                    store._db.execute(
                        "INSERT OR IGNORE INTO remote_workflow_outbox"
                        "(request_id,action,reason_ref,result_json,output_text,"
                        "expansion_text,expansion_digest,attempt_no) "
                        "VALUES (?,'fail','pac:request-expired',NULL,NULL,NULL,NULL,0)",
                        (command.request_id,),
                    )
                return {"accepted": True}
            if isinstance(command, StartLegacyRoutine):
                from hyprial.kernel import operation_key
                from hyprial.kernel import WORK_NODE
                from hyprial.kernel import STATE_ESCALATED
                from hyprial.kernel import STATE_DONE
                from hyprial.kernel import DEADLINE_NODE

                key = command.operation_key or operation_key(
                    command.routine_name, command.task_uuid
                )
                try:
                    head = create_graph(
                        store, name=f"routine-{command.routine_name}",
                        created_by=command.sender, operation_key=key,
                    )
                except PacError as error:
                    if error.code != PAC_OPERATION_KEY_CONFLICT:
                        raise
                    raise RuntimeError(
                        f"operation key {key} is bound to another creator: {error}"
                    ) from error
                graph_id = str(head["graphId"])
                graph = store.graph(graph_id)
                assert graph is not None
                work = store.node(graph_id, WORK_NODE)
                deadline = store.node(graph_id, DEADLINE_NODE)
                settled = None
                if graph["activated_at"] is not None and work is not None and deadline is not None:
                    if work.flag:
                        settled = STATE_DONE
                    elif (
                        deadline.deadline_ms is not None
                        and command.observed_at_ms > deadline.deadline_ms
                    ) or graph["closed_at"] is not None:
                        settled = STATE_ESCALATED
                if settled is not None:
                    return {
                        "graphId": graph_id, "settledState": settled,
                        "deadlineMs": None if deadline is None else deadline.deadline_ms,
                    }
                deadline_ms = (
                    int(deadline.deadline_ms)
                    if deadline is not None and deadline.deadline_ms is not None
                    else command.observed_at_ms + int(command.timeout_seconds * 1000)
                )
                version = int(graph["version"])
                if work is None:
                    version = int(add_node(
                        store, self.database.parent, graph_id=graph_id,
                        node_id=WORK_NODE, owner=command.target,
                        brief_ref=f"routine:{command.routine_name}#{command.task_uuid}",
                        expect_version=version,
                    )["version"])
                if deadline is None:
                    version = int(add_node(
                        store, self.database.parent, graph_id=graph_id,
                        node_id=DEADLINE_NODE, owner=command.escalate_to,
                        brief_ref=f"routine:{command.routine_name}#{command.task_uuid}:deadline",
                        kind="clock", deadline_ms=deadline_ms,
                        guarded_by_node_id=WORK_NODE, expect_version=version,
                    )["version"])
                activate_graph(store, graph_id, actor=command.sender)
                return {
                    "graphId": graph_id, "settledState": None,
                    "deadlineMs": deadline_ms,
                }
            if isinstance(command, ProbeWorkflow):
                graph_id = replay_graph(
                    store, command.spec or load_workflow_text(command.yaml_text),
                    sender=command.sender, operation_key=command.operation_key,
                    routine_name=command.routine_name, task_key=command.task_key,
                )
                return {"graphId": graph_id}
            if isinstance(command, StartWorkflow):
                spec = command.spec or load_workflow_text(command.yaml_text)
                graph_id = replay_graph(
                    store, spec, sender=command.sender,
                    operation_key=command.operation_key,
                    routine_name=command.routine_name,
                    task_key=command.task_key,
                )
                if graph_id is None:
                    graph_id = compile_workflow(
                        store, spec, sender=command.sender,
                        machine=command.machine, local_owner=command.local_owner,
                        operation_key=command.operation_key, at=command.at,
                        routine_name=command.routine_name,
                        task_key=command.task_key,
                    )
                return {"graphId": graph_id}
            if isinstance(command, (SetFlag, ResetFlag)):
                reactor = PacReactor(
                    store,
                    expansion_context=(
                        command.expansion_context
                        if isinstance(command, SetFlag)
                        else None
                    ),
                )
                if isinstance(command, SetFlag):
                    outcome = reactor.set_flag(
                        command.graph_id, command.node_id, actor=command.actor,
                        reason_ref=command.reason_ref,
                        expected_request=command.expected_request,
                        output_text=command.output_text,
                        expansion=command.expansion,
                    )
                else:
                    outcome = reactor.reset_flag(
                        command.graph_id, command.node_id, actor=command.actor,
                        reason_ref=command.reason_ref,
                        expected_request=command.expected_request,
                    )
                if outcome.planned:
                    self.wake_delivery()
                result = {
                    "ok": True, "event": outcome.event,
                    "notifications": [planned_to_json(item) for item in outcome.planned],
                    "delivered": 0, "undelivered": len(outcome.planned),
                    "deliveryPending": bool(outcome.planned),
                }
                if isinstance(command, SetFlag) and command.expansion is not None:
                    from hyprial.daemon.impl.pac.workflows.expansion.results import (
                        accepted_replay_result,
                    )

                    replay = accepted_replay_result(
                        store._db,
                        graph_id=command.graph_id,
                        node_id=command.node_id,
                        request_id=command.expected_request,
                        actor=command.actor,
                        reason_ref=command.reason_ref,
                        output_text=command.output_text,
                        expansion_digest=(
                            command.expansion_context.expansion_digest
                            if command.expansion_context is not None
                            else None
                        ),
                    )
                    if replay is not None:
                        result.update(
                            childGraphId=replay["childGraphId"],
                            expansionDigest=replay["expansionDigest"],
                        )
                return result
            if isinstance(command, ActivateGraph):
                return {"ok": True, **activate_graph(store, command.graph_id, actor=command.actor)}
            if isinstance(command, CloseGraph):
                managed = store._db.execute(
                    "SELECT 1 FROM workflow_graphs WHERE graph_id=?",
                    (command.graph_id,),
                ).fetchone()
                if managed is not None:
                    from hyprial.daemon.impl.pac.workflows.runtime  import close_workflow

                    db = store.write()
                    try:
                        graph = store.graph(command.graph_id)
                        if graph is None:
                            raise PacError(
                                PAC_GRAPH_NOT_FOUND,
                                f"graph {command.graph_id!r} not found",
                            )
                        if graph["created_by"] != command.actor:
                            note = unrewritten_owners_note(db, command.graph_id)
                            raise PacError(
                                PAC_GRAPH_NOT_OWNER,
                                "only the graph owner can close it"
                                + (f"; {note}" if note else ""),
                            )
                        if graph["closed_at"] is None:
                            close_workflow(
                                store,
                                graph,
                                state="cancelled",
                                reason="pac:graph-closed",
                                at=time.time_ns() // 1_000_000,
                            )
                        db.commit()
                    except BaseException:
                        db.rollback()
                        raise
                    return {"ok": True, **show_graph(store, command.graph_id)}
                return {"ok": True, **close_graph(store, command.graph_id, actor=command.actor)}
            if isinstance(command, StopActor):
                request_actor_stop(
                    store, command.graph_id, command.actor_name, actor=command.actor
                )
                return {
                    "ok": True, "graphId": command.graph_id,
                    "actorName": command.actor_name, "desired": "down",
                }
            if isinstance(command, ClockTick):
                planned = PacReactor(store).tick_clocks(command.graph_id)
                if planned:
                    self.wake_delivery()
                return {"ok": True, "notifications": len(planned)}
            if isinstance(command, RecordDelivery):
                PacReactor(store)._mark_delivered(
                    command.event_id, command.edge, command.message_id
                )
                return {"ok": True}
            if isinstance(command, RecordFailure):
                changed = PacReactor(
                    store, logger=self.owner._notification_logger
                )._mark_failed(
                    command.graph_id, command.event_id, command.edge,
                    code=command.code, detail=command.detail,
                )
                return {"ok": True, "changed": changed}
            raise TypeError("unsupported PAC graph command")
        finally:
            store.close()
