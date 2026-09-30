"""Typed PAC graph writer with a separate durable-notification I/O lane.

This port owns the graph writes routed to it.  The rest of the PAC producers
must be routed to the same authority before the database has one writer in
production; constructing this class alone does not make that claim true.
"""

from __future__ import annotations

import json
import secrets
import queue
from copy import deepcopy
import threading
from collections import OrderedDict
from collections.abc import Callable
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from hyprial.actor_runtime import ActorRuntime, ActorSpec, AdmissionResult
from hyprial.contracts.agent_task import AgentTaskStartInput, AgentTaskActivity

from .errors import (
    PAC_GRAPH_NOT_FOUND,
    PAC_GRAPH_NOT_OWNER,
    PAC_OPERATION_KEY_CONFLICT,
    PacError,
)
from .graph import activate_graph, add_node, close_graph, create_graph, show_graph
from .lifecycle import request_actor_stop
from .lifecycle import (
    ActorCoordinator, FileLaunchResolver, LaunchSpec, ResolvedLaunch,
    RuntimeObservation,
)
from .migrations import unrewritten_owners_note
from .reactor import (
    NotificationSender,
    PacReactor,
    permanent_delivery_failure,
    planned_to_json,
)
from .restore_facts import PacRestoreFacts
from .store import NodeRow, PacGraphStore
from .workflow_graph import compile_workflow, replay_graph
from .workflow_schema import load_workflow_text


_EMPTY_RESTORE_FACTS = PacRestoreFacts()


@dataclass(frozen=True, slots=True)
class SetFlag:
    correlation_id: str
    graph_id: str
    node_id: str
    actor: str
    reason_ref: str | None = None
    expected_request: str | None = None
    output_text: str | None = None


@dataclass(frozen=True, slots=True)
class ResetFlag:
    correlation_id: str
    graph_id: str
    node_id: str
    actor: str
    reason_ref: str | None = None
    expected_request: str | None = None


@dataclass(frozen=True, slots=True)
class ActivateGraph:
    correlation_id: str
    graph_id: str
    actor: str


@dataclass(frozen=True, slots=True)
class CloseGraph:
    correlation_id: str
    graph_id: str
    actor: str


@dataclass(frozen=True, slots=True)
class StopActor:
    correlation_id: str
    graph_id: str
    actor_name: str
    actor: str


@dataclass(frozen=True, slots=True)
class ClockTick:
    correlation_id: str
    graph_id: str


@dataclass(frozen=True, slots=True)
class RecordDelivery:
    correlation_id: str
    graph_id: str
    event_id: str
    edge: str
    message_id: str


@dataclass(frozen=True, slots=True)
class RecordFailure:
    """Terminalize one notification whose send failed permanently (#987).

    Routed through the writer like RecordDelivery: in actorized mode the
    workflow runtime drains from a read-only store and must never write.
    """

    correlation_id: str
    graph_id: str
    event_id: str
    edge: str
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class NotificationDelivered:
    generation: int
    graph_id: str
    event_id: str
    edge: str
    message_id: str


@dataclass(frozen=True, slots=True)
class WorkflowTick:
    correlation_id: str
    observed_at_ms: int


@dataclass(frozen=True, slots=True)
class ProbeWorkflow:
    correlation_id: str
    yaml_text: str
    sender: str
    operation_key: str
    routine_name: str | None
    task_key: str | None


@dataclass(frozen=True, slots=True)
class StartWorkflow:
    correlation_id: str
    yaml_text: str
    sender: str
    operation_key: str
    routine_name: str | None
    task_key: str | None
    machine: str
    local_owner: str
    at: int


@dataclass(frozen=True, slots=True)
class CancelWorkflow:
    correlation_id: str
    run_id: str
    actor: str
    reason_ref: str


@dataclass(frozen=True, slots=True)
class StopWorkflowWorker:
    correlation_id: str
    graph_id: str
    actor_name: str
    actor: str


@dataclass(frozen=True, slots=True)
class RestartWorkflowWorker:
    correlation_id: str
    graph_id: str
    actor_name: str
    actor: str


@dataclass(frozen=True, slots=True)
class FailWorkflow:
    correlation_id: str
    graph_id: str
    node_id: str
    actor: str
    request_id: str
    reason_ref: str
    output_text: str | None


@dataclass(frozen=True, slots=True)
class HarnessOutcome:
    correlation_id: str
    message_id: str
    recipient: str
    failed: bool
    failure_code: str | None


@dataclass(frozen=True, slots=True)
class RequestPruned:
    correlation_id: str
    message_id: str
    recipient: str


@dataclass(frozen=True, slots=True)
class EnsureRemoteKey:
    correlation_id: str


@dataclass(frozen=True, slots=True)
class StageRemoteOffer:
    correlation_id: str
    grant_json: str


@dataclass(frozen=True, slots=True)
class EnqueueRemoteOutcome:
    correlation_id: str
    request_id: str
    action: str
    reason_ref: str
    output_text: str | None


@dataclass(frozen=True, slots=True)
class MarkRemoteAttempt:
    correlation_id: str
    request_id: str
    attempted_at_ms: int


@dataclass(frozen=True, slots=True)
class RecordRemoteResult:
    correlation_id: str
    request_id: str
    result_json: str


@dataclass(frozen=True, slots=True)
class PruneRemoteRequest:
    correlation_id: str
    request_id: str


@dataclass(frozen=True, slots=True)
class StartLegacyRoutine:
    correlation_id: str
    routine_name: str
    task_uuid: str
    target: str
    escalate_to: str
    timeout_seconds: float
    sender: str
    observed_at_ms: int
    operation_key: str = ""


@dataclass(frozen=True, slots=True)
class QueryRoutineOccurrence:
    correlation_id: str
    routine_name: str
    task_uuid: str
    operation_key: str
    sender: str
    observed_at_ms: int
    rearm_after_created_at_ms: int = 0


@dataclass(frozen=True, slots=True)
class ProvisionPacDatabase:
    correlation_id: str


@dataclass(frozen=True, slots=True)
class StartAgentTask:
    correlation_id: str
    request: AgentTaskStartInput
    caller: str


@dataclass(frozen=True, slots=True)
class RecordAgentTaskDelivery:
    correlation_id: str
    run_id: str
    target_ref: str
    effect_id: str
    message_id: str
    at_ms: int


@dataclass(frozen=True, slots=True)
class CancelAgentTask:
    correlation_id: str
    run_id: str
    caller: str
    reason: str | None


@dataclass(frozen=True, slots=True)
class ObserveAgentTask:
    correlation_id: str
    activity: AgentTaskActivity
    submitter: str
    message_id: str | None


@dataclass(frozen=True, slots=True)
class ReconcileActor:
    graph_id: str
    node_id: str
    generation: int = 0
    observation: RuntimeObservation | None = None
    resolved: ResolvedLaunch | None = None
    completed_kind: str | None = None
    completed_operation_id: str | None = None
    completed_observation: RuntimeObservation | None = None
    expected_activation_id: str | None = None


@dataclass(frozen=True, slots=True)
class _ActorEffect:
    generation: int
    graph_id: str
    node_id: str
    kind: str
    actor_name: str
    observation: RuntimeObservation | None = None
    launch_ref: str | None = None
    node: NodeRow | None = None
    launch: LaunchSpec | None = None
    operation_id: str | None = None
    identity_marker: str | None = None
    expected_activation_id: str | None = None
    resolved: ResolvedLaunch | None = None


@dataclass(frozen=True, slots=True)
class _ActorEffectCompleted:
    generation: int
    effect: _ActorEffect
    observation: RuntimeObservation | None = None
    resolved: ResolvedLaunch | None = None
    error_code: str | None = None
    error_type: str | None = None
    error: str | None = None


class _ActorEffectNeeded(BaseException):
    """Internal stack-unwind only; never sent to a caller or persisted."""

    def __init__(
        self, kind: str, *, actor_name: str = "", launch_ref: str | None = None,
        node: NodeRow | None = None, launch: LaunchSpec | None = None,
        operation_id: str | None = None, identity_marker: str | None = None,
    ) -> None:
        super().__init__(kind)
        self.kind = kind
        self.actor_name = actor_name
        self.launch_ref = launch_ref
        self.node = node
        self.launch = launch
        self.operation_id = operation_id
        self.identity_marker = identity_marker


class _DeferredRuntime:
    def __init__(self, command: ReconcileActor) -> None:
        self.command = command

    def observe(self, _actor_name: str) -> RuntimeObservation:
        assert self.command.observation is not None
        return self.command.observation

    def start(
        self, actor_name: str, launch: LaunchSpec, *,
        operation_id: str, identity_marker: str,
    ) -> RuntimeObservation:
        if (
            self.command.completed_kind == "start"
            and self.command.completed_operation_id == operation_id
            and self.command.completed_observation is not None
        ):
            return self.command.completed_observation
        raise _ActorEffectNeeded(
            "start", actor_name=actor_name, launch=launch,
            operation_id=operation_id, identity_marker=identity_marker,
        )

    def stop(
        self, actor_name: str, launch: LaunchSpec, *,
        operation_id: str, identity_marker: str,
    ) -> RuntimeObservation:
        if (
            self.command.completed_kind == "stop"
            and self.command.completed_operation_id == operation_id
            and self.command.completed_observation is not None
        ):
            return self.command.completed_observation
        raise _ActorEffectNeeded(
            "stop", actor_name=actor_name, launch=launch,
            operation_id=operation_id, identity_marker=identity_marker,
        )


class _DeferredResolver:
    def __init__(self, command: ReconcileActor) -> None:
        self.command = command

    def __call__(self, launch_ref: str, node: NodeRow) -> ResolvedLaunch:
        if self.command.resolved is not None:
            return self.command.resolved
        raise _ActorEffectNeeded(
            "resolve", launch_ref=launch_ref, node=node,
            actor_name=node.actor_name or "",
        )


GraphCommand = (
    SetFlag | ResetFlag | ActivateGraph | CloseGraph | StopActor | ClockTick
    | RecordDelivery | RecordFailure | WorkflowTick | ProbeWorkflow | StartWorkflow
    | CancelWorkflow | StopWorkflowWorker | RestartWorkflowWorker | FailWorkflow
    | HarnessOutcome | RequestPruned
    | EnsureRemoteKey | StageRemoteOffer | EnqueueRemoteOutcome
    | MarkRemoteAttempt | RecordRemoteResult | PruneRemoteRequest
    | StartLegacyRoutine | QueryRoutineOccurrence
    | ProvisionPacDatabase | StartAgentTask | RecordAgentTaskDelivery
    | CancelAgentTask | ObserveAgentTask
)

_RESTORE_FACT_COMMANDS = (
    SetFlag,
    CloseGraph,
    WorkflowTick,
    StartWorkflow,
    CancelWorkflow,
    StopWorkflowWorker,
    RestartWorkflowWorker,
    FailWorkflow,
    HarnessOutcome,
    RequestPruned,
    StageRemoteOffer,
    EnqueueRemoteOutcome,
    RecordRemoteResult,
    PruneRemoteRequest,
)


@dataclass(frozen=True, slots=True)
class GraphCommandCompleted:
    correlation_id: str
    generation: int
    result: dict[str, Any] | None = None
    error: Exception | None = None


@dataclass(frozen=True, slots=True)
class _Delivery:
    graph_id: str
    event_id: str
    edge: str
    kind: str
    recipient: str
    text: str
    sender: str


class PacGraphOverloaded(PacError):
    def __init__(self, detail: str) -> None:
        super().__init__("PAC_GRAPH_OVERLOADED", detail)


class PacGraphTimeout(PacError):
    def __init__(self, correlation_id: str) -> None:
        super().__init__(
            "PAC_GRAPH_PENDING",
            f"PAC graph operation remains unsettled: {correlation_id}",
            {"correlationId": correlation_id},
        )
        self.correlation_id = correlation_id


class _Pending:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.completion: GraphCommandCompleted | None = None
        self.abandoned = False


class _Generation:
    def __init__(
        self, generation: int, database: Path,
        complete: Any, wake_delivery: Any, workflow_service: Any,
        owner: PacGraphAuthority, agent_task_service: Any,
    ) -> None:
        self.generation = generation
        self.database = database
        self.complete = complete
        self.wake_delivery = wake_delivery
        self.workflow_service = workflow_service
        self.owner = owner
        self.agent_task_service = agent_task_service

    def __call__(
        self, command: GraphCommand | NotificationDelivered
        | ReconcileActor | _ActorEffectCompleted,
    ) -> None:
        if isinstance(command, ReconcileActor):
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
        if isinstance(command, (StartAgentTask, RecordAgentTaskDelivery,
                                CancelAgentTask, ObserveAgentTask)):
            service = self.agent_task_service()
            if service is None:
                raise RuntimeError("PAC agent.task writer has no attached service")
            if isinstance(command, StartAgentTask):
                created, run_id = service._start_persist_direct(
                    command.request, command.caller
                )
                return {"created": created, "runId": run_id}
            if isinstance(command, RecordAgentTaskDelivery):
                service._record_delivery_direct(
                    command.run_id, command.target_ref, command.effect_id,
                    command.message_id, command.at_ms,
                )
                return {"ok": True}
            if isinstance(command, CancelAgentTask):
                return service._cancel_direct(
                    run_id=command.run_id, caller=command.caller,
                    reason=command.reason,
                )
            return service._observe_direct(
                activity=command.activity, submitter=command.submitter,
                message_id=command.message_id,
            )
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
                        "SELECT action,reason_ref,output_text FROM remote_workflow_outbox "
                        "WHERE request_id=?", (command.request_id,),
                    ).fetchone()
                    if row and tuple(row) != (
                        command.action, command.reason_ref, command.output_text
                    ):
                        raise PacError(
                            "WORKFLOW_OUTCOME_CONFLICT",
                            "request already has a queued or accepted outcome",
                        )
                    store._db.execute(
                        "INSERT OR IGNORE INTO remote_workflow_outbox"
                        "(request_id,action,reason_ref,result_json,output_text) "
                        "VALUES (?,?,?,NULL,?)",
                        (command.request_id, command.action, command.reason_ref,
                         command.output_text),
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
                        "WHERE request_id=? AND result_json IS NULL",
                        (command.result_json, command.request_id),
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
                        "(request_id,action,reason_ref,result_json) VALUES (?,'fail','pac:request-expired',NULL)",
                        (command.request_id,),
                    )
                return {"accepted": True}
            if isinstance(command, StartLegacyRoutine):
                from hyprial.routine.pac_dispatch import (
                    DEADLINE_NODE, WORK_NODE, STATE_DONE, STATE_ESCALATED,
                    operation_key,
                )

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
            if isinstance(command, ProvisionPacDatabase):
                return {"ok": True}
            if isinstance(command, ProbeWorkflow):
                graph_id = replay_graph(
                    store, load_workflow_text(command.yaml_text),
                    sender=command.sender, operation_key=command.operation_key,
                    routine_name=command.routine_name, task_key=command.task_key,
                )
                return {"graphId": graph_id}
            if isinstance(command, StartWorkflow):
                spec = load_workflow_text(command.yaml_text)
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
                reactor = PacReactor(store)
                if isinstance(command, SetFlag):
                    outcome = reactor.set_flag(
                        command.graph_id, command.node_id, actor=command.actor,
                        reason_ref=command.reason_ref,
                        expected_request=command.expected_request,
                        output_text=command.output_text,
                    )
                else:
                    outcome = reactor.reset_flag(
                        command.graph_id, command.node_id, actor=command.actor,
                        reason_ref=command.reason_ref,
                        expected_request=command.expected_request,
                    )
                if outcome.planned:
                    self.wake_delivery()
                return {
                    "ok": True, "event": outcome.event,
                    "notifications": [planned_to_json(item) for item in outcome.planned],
                    "delivered": 0, "undelivered": len(outcome.planned),
                    "deliveryPending": bool(outcome.planned),
                }
            if isinstance(command, ActivateGraph):
                return {"ok": True, **activate_graph(store, command.graph_id, actor=command.actor)}
            if isinstance(command, CloseGraph):
                managed = store._db.execute(
                    "SELECT 1 FROM workflow_graphs WHERE graph_id=?",
                    (command.graph_id,),
                ).fetchone()
                if managed is not None:
                    from .workflow_runtime import close_workflow

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


class PacGraphAuthority:
    """One bounded command writer for local graph mutations.

    Delivery reads the durable notifications outbox off the actor thread.
    It sends with the existing idempotency key, then returns the message id
    to the graph owner for the receipt write.  Queue saturation delays work;
    the outbox scan recovers it on the next wake/restart.
    """

    def __init__(
        self, database: Path, sender: NotificationSender, *,
        mailbox_capacity: int = 128, completion_capacity: int = 256,
        call_timeout: float = 15.0,
        logger: Callable[..., None] | None = None,
    ) -> None:
        if mailbox_capacity < 1 or completion_capacity < mailbox_capacity:
            raise ValueError("invalid PAC graph authority capacity")
        self.database = Path(database)
        # Bootstrap/migrate before the writer actor is published.  Once the
        # runtime starts, every writable open belongs to this authority.
        try:
            self.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            PacGraphStore(self.database).close()
        except Exception as error:
            if logger is not None:
                logger(
                    "error", "pac", "pac.database.provision_failed",
                    database=str(self.database), detail=str(error),
                )
            raise
        self.sender = sender
        self._notification_logger = logger
        self._call_timeout = call_timeout
        self._completion_capacity = completion_capacity
        self._pending: dict[str, _Pending] = {}
        self._late_results: OrderedDict[str, GraphCommandCompleted] = OrderedDict()
        self._lock = threading.Lock()
        self._closed = False
        self._generation = 0
        self._workflow_service: Any | None = None
        self._agent_task_service: Any | None = None
        self._actor_runtime: Any | None = None
        self._actor_root: Path | None = None
        self._actor_epoch = ""
        self._actor_skip: Any = None
        self._actor_logger: Any = None
        self._restore_facts_lock = threading.Lock()
        self._restore_facts: dict[str, PacRestoreFacts] = {}
        self._actor_effects: queue.Queue[_ActorEffect] = queue.Queue(maxsize=128)
        self._actor_lock = threading.Lock()
        self._actor_active: dict[tuple[str, str], int] = {}
        self._actor_overloads = 0
        self._wake = threading.Event()
        self._runtime = ActorRuntime()
        self._refresh_restore_facts()

        def factory() -> _Generation:
            self._generation += 1
            with self._actor_lock:
                self._actor_active.clear()
            return _Generation(
                self._generation, self.database, self._complete, self._wake.set,
                lambda: self._workflow_service, self,
                lambda: self._agent_task_service,
            )

        self._handle = self._runtime.start(
            ActorSpec(
                name="pac-graph-authority",
                handler_factory=factory,
                mailbox_capacity=mailbox_capacity,
                supervision_profile="state_authority",
                undelivered_sink=self._undelivered,
            )
        )
        self._delivery = threading.Thread(
            target=self._delivery_loop, name="pac-graph-delivery", daemon=True
        )
        self._delivery.start()
        self._actor_workers = tuple(
            threading.Thread(
                target=self._actor_effect_loop,
                name=f"pac-actor-effect-{index}", daemon=True,
            )
            for index in range(4)
        )
        for worker in self._actor_workers:
            worker.start()
        self._wake.set()

    def _undelivered(self, command: object, reason: str) -> None:
        correlation = getattr(command, "correlation_id", None)
        if correlation:
            self._complete(GraphCommandCompleted(
                correlation, self._generation, error=PacGraphOverloaded(reason)
            ))

    def restore_facts(self, actor: str) -> PacRestoreFacts:
        """Return an immutable committed snapshot without caller-side I/O."""

        with self._restore_facts_lock:
            return self._restore_facts.get(actor, _EMPTY_RESTORE_FACTS)

    def _refresh_restore_facts(self) -> None:
        try:
            store = PacGraphStore(self.database, read_only=True)
            try:
                facts = store.pac_restore_facts()
            finally:
                store.close()
        except Exception as error:
            # This is an observational cache after an already-committed write.
            # Never replace that mutation's receipt with a cache refresh error;
            # empty is the agreed degraded/fail-open projection.
            facts = {}
            logger = self._actor_logger
            if logger is not None:
                try:
                    logger(
                        "warn",
                        "pac",
                        "pac.restore_facts.degraded",
                        errorType=type(error).__name__,
                        detail=str(error)[:500],
                    )
                except Exception:
                    pass
        with self._restore_facts_lock:
            self._restore_facts = facts

    def set_flag(
        self, graph_id: str, node_id: str, *, actor: str,
        reason_ref: str | None = None, expected_request: str | None = None,
        output_text: str | None = None,
    ) -> dict[str, Any]:
        return self._call(SetFlag(
            f"pac-set-{uuid4().hex}", graph_id, node_id, actor, reason_ref,
            expected_request, output_text,
        ))

    def reset_flag(
        self, graph_id: str, node_id: str, *, actor: str,
        reason_ref: str | None = None, expected_request: str | None = None,
    ) -> dict[str, Any]:
        return self._call(ResetFlag(
            f"pac-reset-{uuid4().hex}", graph_id, node_id, actor, reason_ref,
            expected_request,
        ))

    def activate_graph(self, graph_id: str, *, actor: str) -> dict[str, Any]:
        return self._call(ActivateGraph(f"pac-activate-{uuid4().hex}", graph_id, actor))

    def close_graph(self, graph_id: str, *, actor: str) -> dict[str, Any]:
        return self._call(CloseGraph(f"pac-close-{uuid4().hex}", graph_id, actor))

    def stop_actor(self, graph_id: str, actor_name: str, *, actor: str) -> dict[str, Any]:
        return self._call(StopActor(
            f"pac-stop-{uuid4().hex}", graph_id, actor_name, actor,
        ))

    def clock_tick(self, graph_id: str) -> dict[str, Any]:
        return self._call(ClockTick(f"pac-clock-{uuid4().hex}", graph_id))

    def attach_reconciler(
        self, runtime: Any, reference_root: Path, daemon_epoch: str, *,
        on_skip: Any = None, logger: Any = None,
    ) -> None:
        if not daemon_epoch:
            raise ValueError("PAC actor reconciler requires daemon epoch")
        with self._actor_lock:
            if self._actor_runtime is not None and self._actor_runtime is not runtime:
                raise RuntimeError("PAC actor runtime already attached")
            self._actor_runtime = runtime
            self._actor_root = Path(reference_root)
            self._actor_epoch = daemon_epoch
            self._actor_skip = on_skip
            self._actor_logger = logger

    def reconcile_actor(self, graph_id: str, node_id: str) -> AdmissionResult:
        with self._lock:
            if self._closed:
                return AdmissionResult.CLOSED
        return self._runtime.tell(self._handle, ReconcileActor(graph_id, node_id))

    def _submit_actor_effect(self, effect: _ActorEffect) -> None:
        key = (effect.graph_id, effect.node_id)
        overloaded = False
        with self._actor_lock:
            if self._closed or key in self._actor_active:
                return
            self._actor_active[key] = effect.generation
            try:
                self._actor_effects.put_nowait(effect)
            except queue.Full:
                self._actor_active.pop(key, None)
                self._actor_overloads += 1
                overloaded = True
        if overloaded:
            self._log_actor_failure(effect, "actor effect queue is full")

    def _release_actor_effect(
        self, graph_id: str, node_id: str, generation: int
    ) -> None:
        with self._actor_lock:
            key = (graph_id, node_id)
            if self._actor_active.get(key) == generation:
                self._actor_active.pop(key, None)

    def _log_actor_failure(self, effect: _ActorEffect, detail: str) -> None:
        logger = self._actor_logger
        if logger is None:
            return
        try:
            logger(
                "error", "pac", "pac.actor.reconcile_failed",
                graphId=effect.graph_id, nodeId=effect.node_id,
                detail=detail[:500],
            )
        except Exception:
            pass

    def actor_effect_stats(self) -> dict[str, int]:
        with self._actor_lock:
            return {
                "queued": self._actor_effects.qsize(),
                "capacity": self._actor_effects.maxsize,
                "inFlight": len(self._actor_active),
                "overloaded": self._actor_overloads,
            }

    def _actor_effect_loop(self) -> None:
        while not self._closed:
            try:
                effect = self._actor_effects.get(timeout=0.1)
            except queue.Empty:
                continue
            runtime = self._actor_runtime
            try:
                if runtime is None:
                    raise RuntimeError("PAC actor runtime is unavailable")
                if effect.kind == "observe":
                    observation = runtime.observe(effect.actor_name)
                    completed = _ActorEffectCompleted(
                        effect.generation, effect, observation=observation,
                    )
                elif effect.kind == "resolve":
                    assert effect.launch_ref is not None and effect.node is not None
                    assert self._actor_root is not None
                    resolved = FileLaunchResolver(self._actor_root)(
                        effect.launch_ref, effect.node
                    )
                    completed = _ActorEffectCompleted(
                        effect.generation, effect, resolved=resolved,
                    )
                elif effect.kind in {"start", "stop"}:
                    assert effect.launch is not None and effect.operation_id is not None
                    assert effect.identity_marker is not None
                    invoke = runtime.start if effect.kind == "start" else runtime.stop
                    observation = invoke(
                        effect.actor_name, effect.launch,
                        operation_id=effect.operation_id,
                        identity_marker=effect.identity_marker,
                    )
                    completed = _ActorEffectCompleted(
                        effect.generation, effect, observation=observation,
                    )
                else:
                    raise TypeError("unsupported PAC actor effect")
            except Exception as error:
                completed = _ActorEffectCompleted(
                    effect.generation, effect,
                    error_code=(
                        "stop_timeout"
                        if isinstance(error, TimeoutError)
                        else "lifecycle_manager_unavailable"
                    ),
                    error_type=type(error).__name__,
                    error=f"{type(error).__name__}: {error}",
                )
            while not self._closed:
                admission = self._runtime.tell(self._handle, completed)
                if admission is AdmissionResult.ACCEPTED:
                    break
                if admission is AdmissionResult.CLOSED:
                    break
                time.sleep(0.01)

    def record_delivery(
        self, graph_id: str, event_id: str, edge: str, message_id: str
    ) -> None:
        self._call(RecordDelivery(
            f"pac-delivery-{uuid4().hex}", graph_id, event_id, edge,
            message_id,
        ))

    def record_failure(
        self, graph_id: str, event_id: str, edge: str, *, code: str, detail: str
    ) -> None:
        self._call(RecordFailure(
            f"pac-delivery-failure-{uuid4().hex}", graph_id, event_id, edge,
            code, detail,
        ))

    def attach_workflow(self, service: Any) -> None:
        if Path(service.database).resolve() != self.database.resolve():
            raise ValueError("workflow and graph authority must own the same PAC file")
        with self._lock:
            if self._workflow_service is not None and self._workflow_service is not service:
                raise RuntimeError("workflow service already attached")
            self._workflow_service = service

    def workflow_tick(self, observed_at_ms: int) -> None:
        self._call(WorkflowTick(f"pac-workflow-tick-{uuid4().hex}", observed_at_ms))

    def probe_workflow(
        self, *, yaml_text: str, sender: str, operation_key: str,
        routine_name: str | None = None, task_key: str | None = None,
    ) -> str | None:
        return self._call(ProbeWorkflow(
            f"pac-workflow-probe-{uuid4().hex}", yaml_text, sender,
            operation_key, routine_name, task_key,
        ))["graphId"]

    def start_workflow(
        self, *, yaml_text: str, sender: str, operation_key: str,
        routine_name: str | None, task_key: str | None,
        machine: str, local_owner: str, at: int,
    ) -> str:
        return str(self._call(StartWorkflow(
            f"pac-workflow-start-{uuid4().hex}", yaml_text, sender,
            operation_key, routine_name, task_key, machine, local_owner, at,
        ))["graphId"])

    def cancel_workflow(
        self, *, run_id: str, actor: str, reason_ref: str
    ) -> dict[str, Any]:
        return self._call(CancelWorkflow(
            f"pac-workflow-cancel-{uuid4().hex}", run_id, actor, reason_ref,
        ))

    def stop_workflow_worker(
        self, *, graph_id: str, actor_name: str, actor: str
    ) -> dict[str, Any]:
        return self._call(StopWorkflowWorker(
            f"pac-workflow-stop-{uuid4().hex}", graph_id, actor_name, actor,
        ))

    def restart_workflow_worker(
        self, *, graph_id: str, actor_name: str, actor: str
    ) -> dict[str, Any]:
        return self._call(RestartWorkflowWorker(
            f"pac-workflow-restart-{uuid4().hex}", graph_id, actor_name, actor,
        ))

    def fail_workflow(
        self, *, graph_id: str, node_id: str, actor: str,
        request_id: str, reason_ref: str, output_text: str | None,
    ) -> dict[str, Any]:
        return self._call(FailWorkflow(
            f"pac-workflow-fail-{uuid4().hex}", graph_id, node_id, actor,
            request_id, reason_ref, output_text,
        ))

    def record_harness_outcome(
        self, *, message_id: str, recipient: str, failed: bool,
        failure_code: str | None = None,
    ) -> bool:
        return bool(self._call(HarnessOutcome(
            f"pac-outcome-{uuid4().hex}", message_id, recipient, failed,
            failure_code,
        ))["accepted"])

    def record_request_pruned(self, *, message_id: str, recipient: str) -> bool:
        return bool(self._call(RequestPruned(
            f"pac-pruned-{uuid4().hex}", message_id, recipient,
        ))["accepted"])

    def ensure_remote_key(self) -> bytes:
        return bytes(self._call(EnsureRemoteKey(
            f"pac-remote-key-{uuid4().hex}"
        ))["secret"])

    def stage_remote_offer(self, grant_json: str) -> None:
        self._call(StageRemoteOffer(
            f"pac-remote-offer-{uuid4().hex}", grant_json,
        ))

    def enqueue_remote_outcome(
        self, request_id: str, action: str, reason_ref: str,
        output_text: str | None,
    ) -> None:
        self._call(EnqueueRemoteOutcome(
            f"pac-remote-enqueue-{uuid4().hex}", request_id, action,
            reason_ref, output_text,
        ))

    def mark_remote_attempt(self, request_id: str, attempted_at_ms: int) -> None:
        self._call(MarkRemoteAttempt(
            f"pac-remote-attempt-{uuid4().hex}", request_id,
            attempted_at_ms,
        ))

    def record_remote_result(self, request_id: str, result_json: str) -> None:
        self._call(RecordRemoteResult(
            f"pac-remote-result-{uuid4().hex}", request_id, result_json,
        ))

    def prune_remote_request(self, request_id: str) -> bool:
        return bool(self._call(PruneRemoteRequest(
            f"pac-remote-prune-{uuid4().hex}", request_id,
        ))["accepted"])

    def start_legacy_routine_graph(
        self, *, routine_name: str, task_uuid: str, target: str,
        escalate_to: str, timeout_seconds: float, sender: str,
        observed_at_ms: int, operation_key: str,
    ) -> dict[str, Any]:
        return self._call(StartLegacyRoutine(
            f"pac-routine-{uuid4().hex}", routine_name, task_uuid, target,
            escalate_to, timeout_seconds, sender, observed_at_ms, operation_key,
        ))

    def query_routine_occurrence(
        self,
        *,
        routine_name: str,
        task_uuid: str,
        operation_key: str,
        sender: str,
        observed_at_ms: int,
        rearm_after_created_at_ms: int = 0,
    ) -> dict[str, Any]:
        return self._call(
            QueryRoutineOccurrence(
                f"pac-routine-query-{uuid4().hex}",
                routine_name,
                task_uuid,
                operation_key,
                sender,
                observed_at_ms,
                rearm_after_created_at_ms,
            )
        )

    def attach_agent_task(self, service: Any) -> None:
        if Path(service.database).resolve() != self.database.resolve():
            raise ValueError("agent.task and graph authority must own the same PAC file")
        with self._lock:
            if self._agent_task_service is not None and self._agent_task_service is not service:
                raise RuntimeError("agent.task service already attached")
            self._agent_task_service = service
        self._call(ProvisionPacDatabase(f"pac-agent-task-provision-{uuid4().hex}"))

    def start_agent_task(
        self, request: AgentTaskStartInput, caller: str
    ) -> tuple[bool, str]:
        result = self._call(StartAgentTask(
            f"pac-agent-task-start-{uuid4().hex}", deepcopy(request), caller,
        ))
        return bool(result["created"]), str(result["runId"])

    def record_agent_task_delivery(
        self, run_id: str, target_ref: str, effect_id: str,
        message_id: str, at_ms: int,
    ) -> None:
        self._call(RecordAgentTaskDelivery(
            f"pac-agent-task-delivery-{uuid4().hex}", run_id, target_ref,
            effect_id, message_id, at_ms,
        ))

    def cancel_agent_task(
        self, run_id: str, caller: str, reason: str | None
    ) -> dict[str, object]:
        return self._call(CancelAgentTask(
            f"pac-agent-task-cancel-{uuid4().hex}", run_id, caller, reason,
        ))

    def observe_agent_task(
        self, activity: AgentTaskActivity, submitter: str,
        message_id: str | None,
    ) -> dict[str, object]:
        return self._call(ObserveAgentTask(
            f"pac-agent-task-observe-{uuid4().hex}", deepcopy(activity),
            submitter, message_id,
        ))

    def _call(self, command: GraphCommand) -> dict[str, Any]:
        pending = _Pending()
        with self._lock:
            if self._closed or len(self._pending) >= self._completion_capacity:
                raise PacGraphOverloaded("PAC graph authority is closing or full")
            self._pending[command.correlation_id] = pending
            admission = self._runtime.tell(self._handle, command)
            if admission is not AdmissionResult.ACCEPTED:
                del self._pending[command.correlation_id]
                raise PacGraphOverloaded(f"PAC graph admission: {admission.value}")
        if not pending.event.wait(self._call_timeout):
            with self._lock:
                if pending.completion is None:
                    pending.abandoned = True
                    raise PacGraphTimeout(command.correlation_id)
        completion = self.result(command.correlation_id)
        assert completion is not None
        if completion.error is not None:
            raise completion.error
        assert completion.result is not None
        return completion.result

    def result(self, correlation_id: str) -> GraphCommandCompleted | None:
        with self._lock:
            late = self._late_results.pop(correlation_id, None)
            if late is not None:
                return late
            pending = self._pending.get(correlation_id)
            if pending is None or pending.completion is None:
                return None
            del self._pending[correlation_id]
            return pending.completion

    def settled_result(
        self, correlation_id: str, timeout: float | None = None
    ) -> GraphCommandCompleted | None:
        """Join one accepted late graph mutation without owning capacity forever."""

        with self._lock:
            late = self._late_results.pop(correlation_id, None)
            if late is not None:
                return late
            pending = self._pending.get(correlation_id)
        if pending is None or not pending.event.wait(timeout):
            return None
        with self._lock:
            late = self._late_results.pop(correlation_id, None)
            if late is not None:
                return late
            current = self._pending.get(correlation_id)
            if pending.completion is None:
                return None
            if current is pending:
                del self._pending[correlation_id]
            return pending.completion

    def _complete(self, completion: GraphCommandCompleted) -> None:
        with self._lock:
            pending = self._pending.get(completion.correlation_id)
            if pending is not None:
                pending.completion = completion
                pending.event.set()
                if pending.abandoned:
                    del self._pending[completion.correlation_id]
                    self._late_results[completion.correlation_id] = completion
                    while len(self._late_results) > self._completion_capacity:
                        self._late_results.popitem(last=False)

    def _scan_deliveries(self) -> tuple[_Delivery, ...]:
        if not self.database.exists():
            return ()
        store = PacGraphStore(self.database, read_only=True)
        try:
            rows = store._db.execute(
                "SELECT n.*, e.graph_id FROM notifications n "
                "LEFT JOIN flag_events e ON e.event_id=n.event_id "
                "WHERE n.message_id IS NULL AND n.failed_at IS NULL "
                "ORDER BY n.at,n.event_id,n.edge LIMIT 128"
            ).fetchall()
            deliveries: list[_Delivery] = []
            for row in rows:
                graph_id = row["graph_id"]
                if graph_id is None:
                    plan = json.loads(row["plan_json"]) if row["plan_json"] else None
                    graph_id = plan.get("graphId") if isinstance(plan, dict) else None
                if not isinstance(graph_id, str):
                    continue
                # Workflow requests have their own sender, deadline and
                # remote-offer protocol.  This legacy graph delivery lane
                # must never claim or send those shared-table rows.
                if store._db.execute(
                    "SELECT 1 FROM workflow_graphs WHERE graph_id=?", (graph_id,)
                ).fetchone() is not None:
                    continue
                graph = store.graph(graph_id)
                if graph is None or (graph["closed_at"] is not None and row["kind"] != "actor_alert"):
                    continue
                deliveries.append(_Delivery(
                    graph_id, row["event_id"], row["edge"], row["kind"],
                    row["recipient"], row["text"], row["sender"],
                ))
            return tuple(deliveries)
        finally:
            store.close()

    def _delivery_loop(self) -> None:
        while not self._closed:
            self._wake.wait(1.0)
            self._wake.clear()
            try:
                pending = self._scan_deliveries()
            except Exception:
                continue  # durable rows remain for the next scan
            for item in pending:
                if self._closed:
                    return
                try:
                    message_id = self.sender.send(
                        recipient=item.recipient, text=item.text,
                        sender=item.sender,
                        conversation_id=f"pac-{item.graph_id}",
                        idempotency_key=f"pac-notify:{item.event_id}:{item.edge}",
                    )
                except Exception as error:
                    terminal = permanent_delivery_failure(error)
                    if terminal is not None:
                        # A permanent refusal is recorded once and never
                        # re-sent; anything else retries from the durable row.
                        try:
                            self.record_failure(
                                item.graph_id, item.event_id, item.edge,
                                code=terminal[0], detail=terminal[1],
                            )
                        except Exception:  # noqa: BLE001 - row stays pending
                            pass
                    continue
                command = NotificationDelivered(
                    self._runtime.snapshot(self._handle).generation,
                    item.graph_id, item.event_id, item.edge, str(message_id),
                )
                while not self._closed:
                    admission = self._runtime.tell(self._handle, command)
                    if admission is AdmissionResult.ACCEPTED:
                        break
                    if admission is AdmissionResult.CLOSED:
                        return
                    time.sleep(0.01)

    def close(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            self._closed = True
        with self._restore_facts_lock:
            self._restore_facts = {}
        self._wake.set()
        self._delivery.join(max(0.0, deadline - time.monotonic()))
        for worker in self._actor_workers:
            worker.join(max(0.0, deadline - time.monotonic()))
        drained = self._runtime.drain(max(0.0, deadline - time.monotonic()))
        return (
            not self._delivery.is_alive()
            and not any(worker.is_alive() for worker in self._actor_workers)
            and drained.complete
        )
