"""PAC graph authority command vocabulary and internal state carriers.

This module owns the immutable command records and the small internal state
objects they reference.  No database writer or runtime authority lives here.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any


from hyprial.identity import PacError
from hyprial.daemon.impl.pac.actors.coordinator  import LaunchSpec, ResolvedLaunch, RuntimeObservation
from hyprial.daemon.impl.pac.contracts.restore  import PacRestoreFacts
from hyprial.daemon.impl.pac.storage.store  import NodeRow
from hyprial.daemon.impl.pac.contracts.expansion.types import PreparedExpansion
from hyprial.daemon.impl.pac.contracts.workflow import WorkflowSpec


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
    expansion: str | None = None
    expansion_context: PreparedExpansion | None = None


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
    spec: WorkflowSpec | None = None


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
    spec: WorkflowSpec | None = None


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
    expansion_text: str | None = None
    expansion_digest: str | None = None


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
    attempt_no: int = 0


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


@dataclass(slots=True)
class _ReconcileClaim:
    command: ReconcileActor
    admitted: bool = False


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
    worktree_result: dict[str, Any] | None = None
    correlation_id: str | None = None
    preflight: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class _ActorEffectCompleted:
    generation: int
    effect: _ActorEffect
    observation: RuntimeObservation | None = None
    resolved: ResolvedLaunch | None = None
    error_code: str | None = None
    error_type: str | None = None
    error: str | None = None
    worktree_result: dict[str, Any] | None = None
    prepared_expansion: PreparedExpansion | None = None
    exception: Exception | None = None


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
