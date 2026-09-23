"""Actor-owned workflow registry and pure transition coordinator.

The registry is the sole owner of active-run mutation and WorkflowStore writes.
Delivery, inbox observation, acknowledgement, and alarm emission are represented
as correlated effects and executed by the facade's effect pump, never by the
actor handler.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
import hashlib
import json
from typing import Any, TypeAlias
from uuid import uuid4

from hyprial.contracts import ipc_errors
from hyprial.contracts.agent_task import (
    AgentTaskCancelled,
    AgentTaskObserved,
    AgentTaskRunProjection,
    AgentTaskStarted,
    AgentTaskTargetProjection,
    CancelAgentTaskCommand,
    ObserveAgentTaskCommand,
    StartAgentTaskCommand,
    sha256_digest,
)
from hyprial.contracts.ports import PortCommandRejected
from hyprial.log import Logger

from .executor import (
    REPORT_MAX_ATTEMPTS,
    REPORT_RETRY_BACKOFF_MS,
    ReplyView,
    RunState,
    TargetRuntime,
    WorkflowDispatchError,
    WorkflowExecutor,
    WorkflowRun,
)
from .ports import (
    CancelWorkflowCommand,
    ObserveWorkflowActivityCommand,
    RecoverWorkflowsCommand,
    StartWorkflowCommand,
    StartWorkflowIdempotentCommand,
    WorkflowActivityObserved,
    WorkflowCancelled,
    WorkflowIoCompleted,
    WorkflowMutationProjection,
    WorkflowStarted,
    WorkflowStartProjection,
    WorkflowTimerCompleted,
    WorkflowTimerElapsedCommand,
    WorkflowsRecovered,
)
from .schema import WorkflowSchemaError, WorkflowSpec, load_workflow_text
from .store import (
    AgentTaskActivityWrite,
    AgentTaskExternalRefConflict,
    AgentTaskStartWrite,
    PersistedEffectBatch,
    WorkflowExternalRefConflict,
    WorkflowStore,
)
from hyprial.assign_reconcile import (
    ASSIGN_RECONCILE_INTERVAL_MS,
    AssignReconcileReport,
    RoutineExistsProbe,
    due_for_reconciliation,
    run_reconciliation_pass,
)


@dataclass(frozen=True, slots=True)
class WorkflowDeliveryEffect:
    effect_id: str
    parent_correlation_id: str
    generation: int
    version: int
    run_id: str
    target_ref: str
    sender: str
    conversation_id: str
    text: str
    operation: str
    synthetic_message_id: str


@dataclass(frozen=True, slots=True)
class WorkflowAckEffect:
    effect_id: str
    parent_correlation_id: str
    generation: int
    version: int
    run_id: str
    target_ref: str
    message_id: str
    operation: str = "ack"


@dataclass(frozen=True, slots=True)
class WorkflowAlarmEffect:
    effect_id: str
    parent_correlation_id: str
    generation: int
    version: int
    run_id: str
    target_ref: str
    to: str
    text: str
    operation: str = "alarm"


WorkflowEffect: TypeAlias = (
    WorkflowDeliveryEffect | WorkflowAckEffect | WorkflowAlarmEffect
)
RegistryOutput: TypeAlias = (
    WorkflowEffect
    | WorkflowStarted
    | WorkflowCancelled
    | WorkflowsRecovered
    | WorkflowTimerCompleted
    | WorkflowActivityObserved
    | PortCommandRejected
    | AgentTaskStarted
    | AgentTaskCancelled
    | AgentTaskObserved
)


@dataclass(slots=True)
class _ActiveRun:
    run: WorkflowRun
    yaml_text: str
    created_at_ms: int
    version: int


@dataclass(frozen=True, slots=True)
class _PendingEffect:
    effect: WorkflowEffect
    run_id: str


class _DeferredDispatch:
    def __init__(
        self,
        *,
        correlation_id: str,
        generation: int,
        version: int,
        run_id: str,
        effects: list[WorkflowEffect],
        report_to: str,
    ) -> None:
        self._correlation_id = correlation_id
        self._generation = generation
        self._version = version
        self._run_id = run_id
        self._effects = effects
        self._report_to = report_to

    def send(
        self,
        *,
        sender: str,
        target: str,
        conversation_id: str,
        text: str,
    ) -> str:
        effect_id = f"wf-io-{uuid4().hex}"
        synthetic = f"pending:{effect_id}"
        operation = "deliver:report" if target == self._report_to and conversation_id.endswith(
            "-report"
        ) else "deliver:target"
        self._effects.append(
            WorkflowDeliveryEffect(
                effect_id=effect_id,
                parent_correlation_id=self._correlation_id,
                generation=self._generation,
                version=self._version,
                run_id=self._run_id,
                target_ref=target,
                sender=sender,
                conversation_id=conversation_id,
                text=text,
                operation=operation,
                synthetic_message_id=synthetic,
            )
        )
        return synthetic


class _ObservedActivity:
    def __init__(
        self,
        *,
        activity: ObserveWorkflowActivityCommand | None,
        correlation_id: str,
        generation: int,
        version: int,
        run_id: str,
        target_ref: str,
        effects: list[WorkflowEffect],
    ) -> None:
        self._activity = activity
        self._correlation_id = correlation_id
        self._generation = generation
        self._version = version
        self._run_id = run_id
        self._target_ref = target_ref
        self._effects = effects

    def list_replies(
        self, *, conversation_id: str, exclude_sender: str
    ) -> tuple[ReplyView, ...]:
        del exclude_sender
        activity = self._activity
        if (
            activity is None
            or activity.kind != "reply"
            or activity.conversation_id != conversation_id
            or activity.message_id is None
        ):
            return ()
        return (
            ReplyView(
                message_id=activity.message_id,
                text=activity.payload_json.decode(errors="replace"),
            ),
        )

    def is_acknowledged(self, message_id: str) -> bool:
        activity = self._activity
        return bool(
            activity is not None
            and activity.kind == "ack"
            and activity.message_id == message_id
        )

    def ack(self, message_id: str) -> None:
        effect_id = f"wf-io-{uuid4().hex}"
        self._effects.append(
            WorkflowAckEffect(
                effect_id=effect_id,
                parent_correlation_id=self._correlation_id,
                generation=self._generation,
                version=self._version,
                run_id=self._run_id,
                target_ref=self._target_ref,
                message_id=message_id,
            )
        )


class _DeferredAlarm:
    def __init__(
        self,
        *,
        correlation_id: str,
        generation: int,
        version: int,
        run_id: str,
        effects: list[WorkflowEffect],
    ) -> None:
        self._correlation_id = correlation_id
        self._generation = generation
        self._version = version
        self._run_id = run_id
        self._effects = effects

    def escalate(self, *, to: str, text: str) -> None:
        self._effects.append(
            WorkflowAlarmEffect(
                effect_id=f"wf-io-{uuid4().hex}",
                parent_correlation_id=self._correlation_id,
                generation=self._generation,
                version=self._version,
                run_id=self._run_id,
                target_ref=to,
                to=to,
                text=text,
            )
        )


class WorkflowRegistry:
    """Serial actor handler for all workflow transitions and Store writes."""

    def __init__(
        self,
        *,
        store: WorkflowStore,
        generation: int,
        publish: Callable[[RegistryOutput], None],
        clock_ms: Callable[[], int],
        routine_probe: RoutineExistsProbe | None = None,
        assign_reconcile_sink: Callable[[AssignReconcileReport], None] | None = None,
        dispatch_gate: Callable[[WorkflowSpec, str | None, str], tuple[str, ...]] | None = None,
        logger: Logger | None = None,
    ) -> None:
        self._dispatch_gate = dispatch_gate
        self._store = store
        self._generation = generation
        self._publish = publish
        self._clock_ms = clock_ms
        # §C.1 周期性核对的两个注入点,都可为 None:
        #   routine_probe 缺席 ⇒ routine 边一律判 unknown(查不成 ⇒ 活着,§C.1.1②)
        #   assign_reconcile_sink 缺席 ⇒ 报告只随返回值存在,不出 tick
        self._routine_probe = routine_probe
        self._assign_reconcile_sink = assign_reconcile_sink
        self._assign_reconcile_due_ms = 0
        self._active: dict[str, _ActiveRun] = {}
        self._epoch = store.max_version()
        self._pending: dict[str, _PendingEffect] = {}
        self._pending_final: dict[str, RegistryOutput] = {}
        self._seen_activity: set[tuple[str, str]] = set()
        self._report_retry_at: dict[str, int] = {}
        # Same shape and same lifetime as _report_retry_at above: per-process,
        # never persisted, so a daemon restart reloads the open run and grants
        # a fresh budget. Deliberately identical to the executor's
        # _report_attempts -- two paths retrying the same delivery must not
        # disagree about what "give up" means.
        self._report_attempts: dict[str, int] = {}
        self._last_timer_version = 0
        self._logger = logger
        resumed, rejected = store.load_open_runs_report()
        for persisted in resumed:
            self._active[persisted.run.run_id] = _ActiveRun(
                run=persisted.run,
                yaml_text=persisted.yaml_text,
                created_at_ms=self._clock_ms(),
                version=persisted.version,
            )
        for rejection in rejected:
            # Loud by design: an unfinished run whose stored addresses no
            # longer validate must not resume (it would keep escalating to
            # nobody) and must not vanish. Every restart re-reports it until
            # an operator fixes or cancels the run.
            self._safe_log(
                "error",
                "workflow.recovery_rejected",
                runId=rejection.run_id,
                state=rejection.state,
                error=rejection.error,
            )
        for batch in store.load_effect_batches():
            final = self._decode_final(batch.final_kind, batch.final_payload)
            effects = [
                replace(self._decode_effect(payload), generation=self._generation)
                for payload in batch.effects
            ]
            refreshed = self._effect_batch(batch.correlation_id, final, effects)
            store.save_effect_batch(refreshed)
            self._pending_final[batch.correlation_id] = final
            if effects:
                self._register_effects(effects)
            else:
                # Crash window: the last completion transaction deleted the
                # effect row, then the process died before _finish_parent()
                # deleted/emitted the batch. The run state is already durable,
                # so recovery must settle this final exactly once instead of
                # leaving an immortal zero-effect batch.
                self._finish_parent(batch.correlation_id)

    @property
    def adopted(self) -> int:
        return len(self._active)

    def __call__(self, command: object) -> None:
        if isinstance(command, StartAgentTaskCommand):
            self._start_agent_task(command)
        elif isinstance(command, CancelAgentTaskCommand):
            self._cancel_agent_task(command)
        elif isinstance(command, (StartWorkflowCommand, StartWorkflowIdempotentCommand)):
            self._start(command)
        elif isinstance(command, CancelWorkflowCommand):
            self._cancel(command)
        elif isinstance(command, RecoverWorkflowsCommand):
            self._recover(command)
        elif isinstance(command, ObserveAgentTaskCommand):
            self._observe_agent_task(command)
        elif isinstance(command, ObserveWorkflowActivityCommand):
            self._observe(command)
        elif isinstance(command, WorkflowTimerElapsedCommand):
            self._timer(command)
        elif isinstance(command, WorkflowIoCompleted):
            self._io_completed(command)
        else:
            raise TypeError(f"unsupported workflow command: {type(command).__name__}")

    def _start(
        self, command: StartWorkflowCommand | StartWorkflowIdempotentCommand
    ) -> None:
        try:
            spec = load_workflow_text(command.yaml_text)
        except WorkflowSchemaError as error:
            self._reject(command.correlation_id, "WORKFLOW_SCHEMA_ERROR", str(error))
            return
        external_ref: str | None = None
        request_digest: str | None = None
        if isinstance(command, StartWorkflowIdempotentCommand):
            external_ref = command.external_ref.strip()
            if not external_ref or len(external_ref) > 240:
                self._reject(
                    command.correlation_id,
                    ipc_errors.INVALID_ARGUMENT,
                    "external_ref must contain 1..240 characters",
                )
                return
            request_digest = self._start_digest(command.yaml_text, command.sender)
            if self._publish_external_ref_replay(
                command.correlation_id, external_ref, request_digest
            ):
                return
        warnings: tuple[str, ...] = ()
        if self._dispatch_gate is not None:
            try:
                warnings = self._dispatch_gate(spec, None, "")
            except ipc_errors.DaemonRequestError as error:
                self._reject(command.correlation_id, error.code, str(error))
                return
        run_id = f"run-{uuid4().hex[:12]}"
        next_version = self._next_version()
        effects: list[WorkflowEffect] = []
        dispatch = _DeferredDispatch(
            correlation_id=command.correlation_id,
            generation=self._generation,
            version=next_version,
            run_id=run_id,
            effects=effects,
            report_to=spec.report_to or command.sender,
        )
        executor = WorkflowExecutor(
            spec=spec,
            sender=command.sender,
            dispatch=dispatch,
            observe=_ObservedActivity(
                activity=None,
                correlation_id=command.correlation_id,
                generation=self._generation,
                version=next_version,
                run_id=run_id,
                target_ref="",
                effects=effects,
            ),
            alarm=_DeferredAlarm(
                correlation_id=command.correlation_id,
                generation=self._generation,
                version=next_version,
                run_id=run_id,
                effects=effects,
            ),
            clock_ms=self._clock_ms,
            run_id=run_id,
        )
        created = self._clock_ms()
        executor.run.dispatch_warnings = warnings
        executor.start()
        active = _ActiveRun(
            run=executor.run,
            yaml_text=command.yaml_text,
            created_at_ms=created,
            version=next_version,
        )
        self._active[run_id] = active
        final = WorkflowStarted(
            correlation_id=command.correlation_id,
            generation=self._generation,
            version=active.version,
            result=WorkflowStartProjection(
                run_id=run_id,
                state=str(executor.run.state),
                targets=len(executor.run.targets),
            ),
        )
        try:
            self._persist_and_publish(
                active,
                command.correlation_id,
                effects,
                final,
                bump=False,
                external_ref=external_ref,
                request_digest=request_digest,
            )
        except WorkflowExternalRefConflict:
            self._active.pop(run_id, None)
            assert external_ref is not None and request_digest is not None
            if not self._publish_external_ref_replay(
                command.correlation_id, external_ref, request_digest
            ):
                self._reject(
                    command.correlation_id,
                    ipc_errors.EXTERNAL_REF_CONFLICT,
                    f"externalRef already reserved: {external_ref}",
                )

    def _start_agent_task(self, command: StartAgentTaskCommand) -> None:
        request = command.request
        reserved = self._store.resolve_agent_task_external_ref(
            command.service_actor, request.external_ref
        )
        if reserved is not None:
            if reserved.request_digest != request.request_digest:
                self._reject(
                    command.correlation_id,
                    ipc_errors.EXTERNAL_REF_CONFLICT,
                    f"externalRef already reserved with different input: {request.external_ref}",
                )
                return
            if not self._valid_agent_task_request_digest(request):
                self._reject(
                    command.correlation_id,
                    ipc_errors.INVALID_REQUEST,
                    "requestDigest does not match the canonical request",
                )
                return
            self._publish_agent_task_replay(command, reserved.run_id)
            return
        if not self._valid_agent_task_request_digest(request):
            self._reject(
                command.correlation_id,
                ipc_errors.INVALID_REQUEST,
                "requestDigest does not match the canonical request",
            )
            return
        try:
            spec = load_workflow_text(
                command.yaml_text, label=f"agent.task {request.external_ref}"
            )
        except WorkflowSchemaError as error:
            self._reject(command.correlation_id, ipc_errors.INVALID_REQUEST, str(error))
            return
        expected_targets = {target.target for target in request.targets}
        if {target.name for target in spec.targets} != expected_targets:
            self._reject(
                command.correlation_id,
                ipc_errors.PROTOCOL_ERROR,
                "agent.task PAC targets do not match the validated request",
            )
            return
        run_id = f"run-{uuid4().hex[:12]}"
        next_version = self._next_version()
        effects: list[WorkflowEffect] = []
        dispatch = _DeferredDispatch(
            correlation_id=command.correlation_id,
            generation=self._generation,
            version=next_version,
            run_id=run_id,
            effects=effects,
            report_to=command.caller,
        )
        conversation_ids = {
            target.target: f"at-{run_id}-{target.target_ref}"
            for target in request.targets
        }
        executor = WorkflowExecutor(
            spec=spec,
            sender=command.service_actor,
            dispatch=dispatch,
            observe=_ObservedActivity(
                activity=None,
                correlation_id=command.correlation_id,
                generation=self._generation,
                version=next_version,
                run_id=run_id,
                target_ref="",
                effects=effects,
            ),
            alarm=_DeferredAlarm(
                correlation_id=command.correlation_id,
                generation=self._generation,
                version=next_version,
                run_id=run_id,
                effects=effects,
            ),
            clock_ms=self._clock_ms,
            run_id=run_id,
            conversation_ids=conversation_ids,
            explicit_final=True,
            emit_report=False,
        )
        created = self._clock_ms()
        executor.start()
        active = _ActiveRun(
            run=executor.run,
            yaml_text=command.yaml_text,
            created_at_ms=created,
            version=next_version,
        )
        self._active[run_id] = active
        final = AgentTaskStarted(
            correlation_id=command.correlation_id,
            generation=self._generation,
            version=next_version,
            result=AgentTaskRunProjection(
                run_id=run_id,
                external_ref=request.external_ref,
                state="running",
                targets=tuple(
                    AgentTaskTargetProjection(
                        target_ref=target.target_ref,
                        target=target.target,
                        conversation_id=conversation_ids[target.target],
                        attempts=next(
                            item.attempts
                            for item in executor.run.targets
                            if item.name == target.target
                        ),
                        state="running",
                        result_ref=None,
                    )
                    for target in request.targets
                ),
                last_event_id=None,
                created=True,
            ),
        )
        try:
            self._persist_and_publish(
                active,
                command.correlation_id,
                effects,
                final,
                bump=False,
                agent_task_start=AgentTaskStartWrite(
                    service_actor=command.service_actor,
                    caller=command.caller,
                    request=request,
                ),
            )
        except AgentTaskExternalRefConflict as error:
            self._active.pop(run_id, None)
            raced = self._store.resolve_agent_task_external_ref(
                command.service_actor, request.external_ref
            )
            if raced is None:
                self._reject(
                    command.correlation_id,
                    ipc_errors.PROTOCOL_ERROR,
                    f"externalRef conflict disappeared: {request.external_ref}",
                )
            elif raced.request_digest != request.request_digest:
                self._reject(
                    command.correlation_id,
                    ipc_errors.EXTERNAL_REF_CONFLICT,
                    f"externalRef already reserved with different input: {request.external_ref}",
                )
            else:
                self._publish_agent_task_replay(command, error.existing_run_id)

    def _publish_agent_task_replay(
        self, command: StartAgentTaskCommand, run_id: str
    ) -> None:
        projection = self._store.read_agent_task_run(
            run_id, command.service_actor, created=False
        )
        if projection is None:
            self._reject(
                command.correlation_id,
                ipc_errors.PROTOCOL_ERROR,
                f"agent.task externalRef points to missing run: {run_id}",
            )
            return
        self._publish(
            AgentTaskStarted(
                correlation_id=command.correlation_id,
                generation=self._generation,
                version=self._epoch,
                result=projection,
            )
        )

    @staticmethod
    def _valid_agent_task_request_digest(request: Any) -> bool:
        return request.request_digest == sha256_digest(
            {
                "metadata": request.metadata,
                "payload": request.payload,
                "targets": [
                    {
                        "targetRef": target.target_ref,
                        "target": target.target,
                        "role": target.role,
                        "delegates": list(target.delegates),
                    }
                    for target in request.targets
                ],
                "completion": request.completion,
            }
        )

    def _cancel_agent_task(self, command: CancelAgentTaskCommand) -> None:
        projection = self._store.read_agent_task_run(
            command.run_id, command.service_actor
        )
        if projection is None:
            self._reject(
                command.correlation_id,
                ipc_errors.RUN_NOT_FOUND,
                f"no run in this service/namespace scope: {command.run_id}",
            )
            return
        if projection.state == "cancelled":
            self._publish(
                AgentTaskCancelled(
                    correlation_id=command.correlation_id,
                    generation=self._generation,
                    version=self._epoch,
                    result=projection,
                )
            )
            return
        if projection.state in {"completed", "failed"}:
            self._reject(
                command.correlation_id,
                ipc_errors.RUN_NOT_CANCELLABLE,
                f"run {command.run_id} is already {projection.state}",
            )
            return
        active = self._active.get(command.run_id)
        if active is None:
            self._reject(
                command.correlation_id,
                ipc_errors.PROTOCOL_ERROR,
                f"active agent.task run is missing: {command.run_id}",
            )
            return
        executor = self._executor(active, command.correlation_id, None, [])
        executor.cancel()
        active.version = self._next_version()
        self._store.save_run(
            active.run,
            yaml_text=active.yaml_text,
            created_at_ms=active.created_at_ms,
            finished_at_ms=self._clock_ms(),
            version=active.version,
            agent_task_cancel=True,
            agent_task_cancel_reason=command.reason,
        )
        self._active.pop(command.run_id, None)
        cancelled = self._store.read_agent_task_run(
            command.run_id, command.service_actor
        )
        assert cancelled is not None
        self._publish(
            AgentTaskCancelled(
                correlation_id=command.correlation_id,
                generation=self._generation,
                version=active.version,
                result=cancelled,
            )
        )

    def _cancel(self, command: CancelWorkflowCommand) -> None:
        active = self._active.get(command.run_id)
        if active is None:
            try:
                persisted = self._store.load_run(command.run_id)
            except WorkflowSchemaError as error:
                # A schema-broken run cannot be materialized.  Cancel it anyway
                # (cancelled is terminal) instead of trapping the operator with
                # SQL as the only escape (2026-09-14 S3).
                if self._store.cancel_unparseable_run(command.run_id):
                    self._safe_log(
                        "warn",
                        "workflow.cancelled_schema_error",
                        runId=command.run_id,
                        error=str(error),
                    )
                    self._publish(
                        WorkflowCancelled(
                            correlation_id=command.correlation_id,
                            generation=self._generation,
                            version=self._epoch,
                            result=WorkflowMutationProjection(
                                run_id=command.run_id,
                                state=str(RunState.CANCELLED),
                            ),
                        )
                    )
                else:
                    self._reject(
                        command.correlation_id,
                        "WORKFLOW_RUN_NOT_FOUND",
                        f"no such run: {command.run_id}",
                    )
                return
            code = "WORKFLOW_RUN_NOT_FOUND" if persisted is None else "WORKFLOW_RUN_NOT_ACTIVE"
            detail = (
                f"no such run: {command.run_id}"
                if persisted is None
                else f"run {command.run_id} is already {persisted.run.state}"
            )
            self._reject(command.correlation_id, code, detail)
            return
        executor = self._executor(active, command.correlation_id, None, [])
        executor.cancel()
        self._persist(active)
        self._active.pop(command.run_id, None)
        self._publish(
            WorkflowCancelled(
                correlation_id=command.correlation_id,
                generation=self._generation,
                version=active.version,
                result=WorkflowMutationProjection(
                    run_id=command.run_id, state=str(executor.run.state)
                ),
            )
        )

    def _recover(self, command: RecoverWorkflowsCommand) -> None:
        self._publish(
            WorkflowsRecovered(
                correlation_id=command.correlation_id,
                generation=self._generation,
                version=self._epoch,
                adopted=len(self._active),
            )
        )

    def _observe(self, command: ObserveWorkflowActivityCommand) -> None:
        if command.message_id is not None:
            key = (command.run_id, command.message_id)
            if key in self._seen_activity:
                self._publish(
                    WorkflowActivityObserved(
                        correlation_id=command.correlation_id,
                        generation=self._generation,
                        version=self._epoch,
                        run_id=command.run_id,
                        target_ref=command.target_ref,
                        conversation_id=command.conversation_id,
                        kind=command.kind,
                        payload_json=command.payload_json,
                        message_id=command.message_id,
                        result_ref=command.result_ref,
                    )
                )
                return
            self._seen_activity.add(key)
        active = self._active.get(command.run_id)
        if active is None:
            self._reject(command.correlation_id, "WORKFLOW_RUN_NOT_ACTIVE", command.run_id)
            return
        if self._dispatch_gate is not None and command.kind == "reply":
            warnings = self._dispatch_gate(
                active.run.spec, command.target_ref, command.payload_json.decode(errors="replace")
            )
            active.run.dispatch_warnings = tuple(dict.fromkeys((*active.run.dispatch_warnings, *warnings)))
        effects: list[WorkflowEffect] = []
        executor = self._executor(active, command.correlation_id, command, effects)
        if not self._report_in_flight(command.run_id):
            executor.tick()
        if self._has_report_effect(effects, command.run_id):
            executor.run.state = RunState.RUNNING
        if (
            executor.run.state is not RunState.RUNNING
            and not self._has_report_effect(effects, command.run_id)
        ):
            self._active.pop(command.run_id, None)
        final = WorkflowActivityObserved(
            correlation_id=command.correlation_id,
            generation=self._generation,
            version=active.version,
            run_id=command.run_id,
            target_ref=command.target_ref,
            conversation_id=command.conversation_id,
            kind=command.kind,
            payload_json=command.payload_json,
            message_id=command.message_id,
            result_ref=command.result_ref,
        )
        self._persist_and_publish(
            active, command.correlation_id, effects, final
        )

    def _observe_agent_task(self, command: ObserveAgentTaskCommand) -> None:
        activity = command.activity
        submitter = command.submitter
        service_actor = command.service_actor
        projection = self._store.read_agent_task_run(
            activity.run_id, service_actor
        )
        if projection is None:
            self._reject(
                command.correlation_id,
                ipc_errors.RUN_NOT_FOUND,
                f"no run in this service/namespace scope: {activity.run_id}",
            )
            return
        target = self._store.agent_task_target(
            activity.run_id, activity.target_ref
        )
        if target is None:
            self._reject(
                command.correlation_id,
                ipc_errors.TARGET_NOT_FOUND,
                f"no targetRef {activity.target_ref} in run {activity.run_id}",
            )
            return
        if submitter != target.target and submitter not in target.delegates:
            self._reject(
                command.correlation_id,
                ipc_errors.CALLER_NOT_AUTHORIZED,
                "submitter is neither the assigned target nor a scoped delegate",
            )
            return
        if activity.conversation_id != target.conversation_id:
            self._reject(
                command.correlation_id,
                ipc_errors.INVALID_REQUEST,
                "activity conversationId does not match target",
            )
            return
        result_ref = (
            str(activity.payload["resultRef"])
            if activity.kind == "result.submitted"
            else None
        )
        result_digest = (
            str(activity.payload["resultDigest"])
            if activity.kind == "result.submitted"
            else None
        )
        computed_result_digest = (
            sha256_digest(
                {
                    "result": activity.payload["result"],
                    "artifactRefs": activity.payload["artifactRefs"],
                }
            )
            if activity.kind == "result.submitted"
            else None
        )
        existing_result = self._store.agent_task_result_record(
            activity.run_id, activity.target_ref
        )
        if existing_result is not None:
            if (
                result_ref == existing_result.result_ref
                and computed_result_digest == existing_result.result_digest
                and result_digest == computed_result_digest
            ):
                self._publish_agent_task_observed(command, created=False)
            else:
                self._reject(
                    command.correlation_id,
                    ipc_errors.RESULT_REF_CONFLICT,
                    "target already has a different result",
                )
            return
        existing_digest = self._store.agent_task_event_digest(activity.event_id)
        if existing_digest is not None:
            if existing_digest == activity.event_digest:
                self._publish_agent_task_observed(command, created=False)
            else:
                self._reject(
                    command.correlation_id,
                    ipc_errors.INVALID_REQUEST,
                    "eventId is already used by different activity",
                )
            return
        if projection.state == "cancelled":
            self._reject(
                command.correlation_id,
                ipc_errors.INVALID_REQUEST,
                "cancelled runs do not accept new activity",
            )
            return
        if activity.kind == "result.submitted":
            if result_digest != computed_result_digest:
                self._reject(
                    command.correlation_id,
                    ipc_errors.INVALID_REQUEST,
                    "resultDigest does not match the canonical result",
                )
                return
        active = self._active.get(activity.run_id)
        if active is None:
            self._reject(
                command.correlation_id,
                ipc_errors.PROTOCOL_ERROR,
                f"active agent.task run is missing: {activity.run_id}",
            )
            return
        executor = self._executor(active, command.correlation_id, None, [])
        message_id = command.message_id or activity.event_id
        if activity.kind == "result.submitted":
            executor.complete_target(
                target.target,
                message_id=message_id,
                excerpt=f"result.submitted {result_ref}",
            )
        else:
            executor.record_activity(
                target.target,
                json.dumps(activity.payload, ensure_ascii=False, sort_keys=True),
            )
        active.version = self._next_version()
        self._store.save_run(
            active.run,
            yaml_text=active.yaml_text,
            created_at_ms=active.created_at_ms,
            finished_at_ms=(
                self._clock_ms()
                if active.run.state is not RunState.RUNNING
                else None
            ),
            version=active.version,
            agent_task_activity=AgentTaskActivityWrite(
                activity=activity,
                submitter=submitter,
                message_id=message_id,
            ),
        )
        if active.run.state is not RunState.RUNNING:
            self._active.pop(activity.run_id, None)
        self._publish_agent_task_observed(command, created=True)

    def _publish_agent_task_observed(
        self, command: ObserveAgentTaskCommand, *, created: bool
    ) -> None:
        activity = command.activity
        self._publish(
            AgentTaskObserved(
                correlation_id=command.correlation_id,
                generation=self._generation,
                version=self._epoch,
                run_id=activity.run_id,
                target_ref=activity.target_ref,
                event_id=activity.event_id,
                accepted=True,
                created=created,
            )
        )

    def _assign_run_state(self, run_id: str) -> str | None:
        try:
            persisted = self._store.load_run(run_id)
        except WorkflowSchemaError:
            # A run that cannot validate reads as unknown to reconcile: the
            # safe side is "do not release on a broken read", and the
            # rejection is already logged loudly at recovery.
            return None
        return None if persisted is None else str(persisted.run.state)

    def _timer(self, command: WorkflowTimerElapsedCommand) -> None:
        if (
            command.generation != self._generation
            or command.version <= self._last_timer_version
        ):
            self._reject(
                command.correlation_id,
                "WORKFLOW_STALE_TIMER",
                f"timer {command.generation}/{command.version} is stale for "
                f"{self._generation}/{self._last_timer_version}",
            )
            return
        self._last_timer_version = command.version
        # §C.1 周期性核对,挂在本 tick 上(不新开线程,§C.1 同一条纪律):
        # 先自逄到点,再跑隔离的 pass —— 核对出错只能变成一份 error 报告,
        # 决不能打断下面活跃 run 的 tick 逻辑(与 daemon 的 maintenance
        # 隔离边界同一条理由)。
        if due_for_reconciliation(command.observed_at_ms, self._assign_reconcile_due_ms):
            self._assign_reconcile_due_ms = (
                command.observed_at_ms + ASSIGN_RECONCILE_INTERVAL_MS
            )
            run_reconciliation_pass(
                store=self._store,
                run_state=self._assign_run_state,
                routine_exists=self._routine_probe,
                sink=self._assign_reconcile_sink,
                now_ms=command.observed_at_ms,
            )
        effects: list[WorkflowEffect] = []
        touched: list[_ActiveRun] = []
        for active in list(self._active.values()):
            retry_at = self._report_retry_at.get(active.run.run_id)
            if retry_at is not None and command.observed_at_ms < retry_at:
                continue
            if self._report_in_flight(active.run.run_id):
                # Every target is terminal and the report effect is still
                # awaiting its WorkflowIoCompleted: ticking would re-enter
                # _maybe_finish and record a SECOND report effect under a new
                # id -- a second message to report_to (hq-adjutant 09-17:
                # squire got each run's report twice).
                continue
            executor = self._executor(active, command.correlation_id, None, effects)
            executor.tick()
            touched.append(active)
            if self._has_report_effect(effects, executor.run.run_id):
                executor.run.state = RunState.RUNNING
            if (
                executor.run.state is not RunState.RUNNING
                and not self._has_report_effect(effects, executor.run.run_id)
            ):
                self._active.pop(executor.run.run_id, None)
        final = WorkflowTimerCompleted(
            correlation_id=command.correlation_id,
            generation=self._generation,
            version=self._epoch,
            active_runs=len(self._active),
        )
        if not touched and not effects:
            self._publish(final)
            return
        # One timer may touch several runs. Persist each run first; the final
        # run transaction atomically records the shared effect batch.
        if touched:
            for active in touched[:-1]:
                self._persist(active)
            self._persist_and_publish(
                touched[-1], command.correlation_id, effects, final
            )
        else:
            # Only terminal runs remain in local variables; their state was
            # already persisted while reducing above, so persist the outbox.
            self._store.save_effect_batch(
                self._effect_batch(command.correlation_id, final, effects)
            )
            self._publish_after_effects(command.correlation_id, effects, final)

    def _abandon_report(
        self,
        active: "_ActiveRun",
        effect: WorkflowDeliveryEffect,
        event: WorkflowIoCompleted,
        *,
        attempts: int,
        followups: list[WorkflowEffect],
    ) -> None:
        """Stop owing the report, loudly, and let the run finish.

        Mirrors WorkflowExecutor._abandon_report so the two retry paths cannot
        drift into two different meanings of "give up": COMPLETED because every
        target reached a terminal state and a report is not a target, and an
        alarm because the give-up must not be silent.

        The alarm matters more here than anywhere else: `report_text` has no
        consumer in the codebase -- it is only persisted -- so an undelivered
        report has no automated consequence at all.  A person is the only one
        who can act on it, and this alarm is their only notice.
        """

        intended = active.run.spec.report_to or active.run.sender
        reason = (
            "delivery is permanently refused"
            if event.permanent
            else f"delivery failed {attempts} times"
        )
        detail = event.detail or event.code or "no detail"
        _DeferredAlarm(
            correlation_id=effect.parent_correlation_id,
            generation=self._generation,
            version=self._epoch + 1,
            run_id=active.run.run_id,
            effects=followups,
        ).escalate(
            to=active.run.sender,
            text=(
                f"PAC workflow {active.run.spec.name} (run {active.run.run_id}) "
                f"finished, but its report could not be delivered to "
                f"{intended}: {reason} ({detail}). The run is completed; the "
                f"report text is on the run record and was not sent."
            ),
        )
        self._report_retry_at.pop(active.run.run_id, None)
        self._report_attempts.pop(active.run.run_id, None)
        active.run.state = RunState.COMPLETED
        self._active.pop(active.run.run_id, None)

    def _io_completed(self, event: WorkflowIoCompleted) -> None:
        pending = self._pending.get(event.correlation_id)
        if pending is None:
            return
        effect = pending.effect
        if event.generation != effect.generation or event.version != effect.version:
            return
        if (isinstance(effect, WorkflowDeliveryEffect)
                and effect.operation == "deliver:target" and event.succeeded
                and event.message_id and event.recipient):
            self._store.record_node_delivery(
                run_id=effect.run_id, target_ref=effect.target_ref,
                message_id=event.message_id, recipient=event.recipient,
                recorded_at_ms=self._clock_ms(),
            )
        self._pending.pop(event.correlation_id, None)
        active = self._active.get(pending.run_id)
        followups: list[WorkflowEffect] = []
        if active is not None and isinstance(effect, WorkflowDeliveryEffect):
            target = self._target(active, effect.target_ref)
            if effect.operation == "deliver:target" and target is not None:
                if event.succeeded and event.recipient:
                    # The assign edge is recorded here, not at dispatch: the
                    # row means "this run's work reached this actor", and a
                    # delivery that never succeeded did not reach anyone.
                    # event.recipient is the actor the delivery actually
                    # resolved to -- never re-resolved from the alias, which
                    # is mutable and would re-attribute history on a rename.
                    self._store.record_workflow_assign(
                        actor=event.recipient,
                        run_id=effect.run_id,
                        assigned_at_ms=self._clock_ms(),
                    )
                if target.last_message_id == effect.synthetic_message_id:
                    if event.succeeded and event.message_id is not None:
                        target.last_message_id = event.message_id
                    elif not event.succeeded:
                        target.last_error = WorkflowDispatchError(
                            event.detail or event.code or "workflow delivery failed",
                            permanent=event.permanent,
                        )
                        executor = self._executor(
                            active,
                            effect.parent_correlation_id,
                            None,
                            followups,
                        )
                        executor._on_timeout(target)
                        executor._maybe_finish()
                        if self._has_report_effect(followups, pending.run_id):
                            executor.run.state = RunState.RUNNING
            elif effect.operation == "deliver:report" and not event.succeeded:
                # This is the path the daemon actually takes.  The executor's
                # dispatch port here is _DeferredDispatch, which records an
                # effect and returns -- it never raises -- so the executor's
                # own report guard is unreachable in the daemon and the retry
                # loop lives entirely in these two branches.  Unbounded, and
                # blind to `permanent`, it kept a finished run RUNNING for the
                # life of the process; every timer pass then re-entered it.
                attempts = self._report_attempts.get(pending.run_id, 0) + 1
                self._report_attempts[pending.run_id] = attempts
                if event.permanent or attempts >= REPORT_MAX_ATTEMPTS:
                    self._abandon_report(
                        active, effect, event, attempts=attempts, followups=followups
                    )
                else:
                    active.run.state = RunState.RUNNING
                    self._active[active.run.run_id] = active
                    self._report_retry_at[pending.run_id] = (
                        self._clock_ms() + REPORT_RETRY_BACKOFF_MS
                    )
            elif effect.operation == "deliver:report" and event.succeeded:
                self._report_retry_at.pop(pending.run_id, None)
                self._report_attempts.pop(pending.run_id, None)
                active.run.state = RunState.COMPLETED
                self._active.pop(pending.run_id, None)
        if active is not None:
            self._persist_completion(
                active,
                completed_effect_id=event.correlation_id,
                parent_correlation_id=effect.parent_correlation_id,
                followups=followups,
            )
        else:
            self._store.delete_effect(event.correlation_id)
        self._register_effects(followups)
        self._finish_parent(effect.parent_correlation_id)

    def _executor(
        self,
        active: _ActiveRun,
        correlation_id: str,
        activity: ObserveWorkflowActivityCommand | None,
        effects: list[WorkflowEffect],
    ) -> WorkflowExecutor:
        run = active.run
        next_version = self._epoch + 1
        target_ref = activity.target_ref if activity is not None else ""
        agent_task = self._store.is_agent_task_run(run.run_id)
        return WorkflowExecutor(
            spec=run.spec,
            sender=run.sender,
            dispatch=_DeferredDispatch(
                correlation_id=correlation_id,
                generation=self._generation,
                version=next_version,
                run_id=run.run_id,
                effects=effects,
                report_to=run.spec.report_to or run.sender,
            ),
            observe=_ObservedActivity(
                activity=activity,
                correlation_id=correlation_id,
                generation=self._generation,
                version=next_version,
                run_id=run.run_id,
                target_ref=target_ref,
                effects=effects,
            ),
            alarm=_DeferredAlarm(
                correlation_id=correlation_id,
                generation=self._generation,
                version=next_version,
                run_id=run.run_id,
                effects=effects,
            ),
            clock_ms=self._clock_ms,
            restored=run,
            explicit_final=agent_task,
            emit_report=not agent_task,
        )

    def _persist(self, active: _ActiveRun, *, bump: bool = True) -> None:
        if bump:
            active.version = self._next_version()
        run = active.run
        self._store.save_run(
            run,
            yaml_text=active.yaml_text,
            created_at_ms=active.created_at_ms,
            finished_at_ms=(
                self._clock_ms()
                if run.state is not RunState.RUNNING
                else None
            ),
            version=active.version,
        )

    def _persist_and_publish(
        self,
        active: _ActiveRun,
        correlation_id: str,
        effects: list[WorkflowEffect],
        final: RegistryOutput,
        *,
        bump: bool = True,
        external_ref: str | None = None,
        request_digest: str | None = None,
        agent_task_start: AgentTaskStartWrite | None = None,
    ) -> None:
        if bump:
            active.version = self._next_version()
        run = active.run
        batch = (
            self._effect_batch(correlation_id, final, effects) if effects else None
        )
        self._store.save_run(
            run,
            yaml_text=active.yaml_text,
            created_at_ms=active.created_at_ms,
            finished_at_ms=(
                self._clock_ms() if run.state is not RunState.RUNNING else None
            ),
            version=active.version,
            effect_batch=batch,
            external_ref=external_ref,
            request_digest=request_digest,
            agent_task_start=agent_task_start,
        )
        self._publish_after_effects(correlation_id, effects, final)

    def _publish_external_ref_replay(
        self,
        correlation_id: str,
        external_ref: str,
        request_digest: str,
    ) -> bool:
        reserved = self._store.resolve_external_ref(external_ref)
        if reserved is None:
            return False
        if reserved.request_digest != request_digest:
            self._reject(
                correlation_id,
                ipc_errors.EXTERNAL_REF_CONFLICT,
                f"externalRef already reserved with different input: {external_ref}",
            )
            return True
        try:
            persisted = self._store.load_run(reserved.run_id)
        except WorkflowSchemaError as error:
            # externalRef replay must not crash the actor on a schema-broken
            # stored run (2026-09-14 S3); reject loudly instead.
            self._reject(
                correlation_id,
                ipc_errors.WORKFLOW_EXTERNAL_REF_CORRUPT,
                f"externalRef points to a run that no longer validates: "
                f"{external_ref}: {error}",
            )
            return True
        if persisted is None:
            self._reject(
                correlation_id,
                ipc_errors.WORKFLOW_EXTERNAL_REF_CORRUPT,
                f"externalRef points to a missing run: {external_ref}",
            )
            return True
        self._publish(
            WorkflowStarted(
                correlation_id=correlation_id,
                generation=self._generation,
                version=persisted.version,
                result=WorkflowStartProjection(
                    run_id=persisted.run.run_id,
                    state=str(persisted.run.state),
                    targets=len(persisted.run.targets),
                ),
            )
        )
        return True

    @staticmethod
    def _start_digest(yaml_text: str, sender: str) -> str:
        payload = json.dumps(
            {"sender": sender, "yaml": yaml_text},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def _persist_completion(
        self,
        active: _ActiveRun,
        *,
        completed_effect_id: str,
        parent_correlation_id: str,
        followups: list[WorkflowEffect],
    ) -> None:
        active.version = self._next_version()
        final = self._pending_final.get(parent_correlation_id)
        batch = (
            self._effect_batch(parent_correlation_id, final, followups)
            if final is not None and followups
            else None
        )
        self._store.save_run(
            active.run,
            yaml_text=active.yaml_text,
            created_at_ms=active.created_at_ms,
            finished_at_ms=(
                self._clock_ms()
                if active.run.state is not RunState.RUNNING
                else None
            ),
            version=active.version,
            effect_batch=batch,
            completed_effect_id=completed_effect_id,
        )

    def _next_version(self) -> int:
        self._epoch += 1
        return self._epoch

    def _publish_after_effects(
        self,
        correlation_id: str,
        effects: list[WorkflowEffect],
        final: RegistryOutput,
    ) -> None:
        if not effects:
            self._publish(final)
            return
        self._pending_final[correlation_id] = final
        self._register_effects(effects)

    def _register_effects(self, effects: list[WorkflowEffect]) -> None:
        for effect in effects:
            self._pending[effect.effect_id] = _PendingEffect(
                effect=effect, run_id=effect.run_id
            )
            self._publish(effect)

    def _finish_parent(self, correlation_id: str) -> None:
        if any(
            item.effect.parent_correlation_id == correlation_id
            for item in self._pending.values()
        ):
            return
        final = self._pending_final.pop(correlation_id, None)
        if final is None:
            return
        if isinstance(final, WorkflowTimerCompleted):
            final = replace(
                final,
                generation=self._generation,
                version=self._epoch,
                active_runs=len(self._active),
            )
        elif hasattr(final, "version"):
            final = replace(final, generation=self._generation, version=self._epoch)
        # Publish before deleting the durable batch. A crash after publish may
        # replay the same correlated final (idempotent at the facade), while a
        # delete-before-publish crash would lose settlement permanently.
        self._publish(final)
        self._store.delete_effect_batch(correlation_id)

    def _effect_batch(
        self,
        correlation_id: str,
        final: RegistryOutput,
        effects: list[WorkflowEffect],
    ) -> PersistedEffectBatch:
        final_kind, final_payload = self._encode_final(final)
        return PersistedEffectBatch(
            correlation_id=correlation_id,
            final_kind=final_kind,
            final_payload=final_payload,
            effects=tuple(self._encode_effect(effect) for effect in effects),
        )

    @staticmethod
    def _encode_effect(effect: WorkflowEffect) -> dict[str, Any]:
        return {"kind": type(effect).__name__, **asdict(effect)}

    @staticmethod
    def _decode_effect(payload: dict[str, Any]) -> WorkflowEffect:
        values = dict(payload)
        kind = str(values.pop("kind"))
        effect_types = {
            "WorkflowDeliveryEffect": WorkflowDeliveryEffect,
            "WorkflowAckEffect": WorkflowAckEffect,
            "WorkflowAlarmEffect": WorkflowAlarmEffect,
        }
        try:
            effect_type = effect_types[kind]
        except KeyError as error:
            raise ValueError(f"unknown workflow effect kind: {kind}") from error
        return effect_type(**values)

    @staticmethod
    def _encode_final(final: RegistryOutput) -> tuple[str, dict[str, Any]]:
        if isinstance(final, AgentTaskStarted):
            return "AgentTaskStarted", {
                "correlation_id": final.correlation_id,
                "generation": final.generation,
                "version": final.version,
                "result": asdict(final.result),
            }
        if isinstance(final, WorkflowStarted):
            return "WorkflowStarted", {
                "correlation_id": final.correlation_id,
                "generation": final.generation,
                "version": final.version,
                "result": asdict(final.result),
            }
        if isinstance(final, WorkflowActivityObserved):
            payload = asdict(final)
            payload["payload_json"] = final.payload_json.hex()
            return "WorkflowActivityObserved", payload
        if isinstance(final, WorkflowTimerCompleted):
            return "WorkflowTimerCompleted", asdict(final)
        raise TypeError(f"unsupported durable workflow final: {type(final).__name__}")

    def _decode_final(
        self, kind: str, payload: dict[str, Any]
    ) -> RegistryOutput:
        values = dict(payload)
        values["generation"] = self._generation
        if kind == "WorkflowStarted":
            values["result"] = WorkflowStartProjection(**values["result"])
            return WorkflowStarted(**values)
        if kind == "AgentTaskStarted":
            result = dict(values["result"])
            result["targets"] = tuple(
                AgentTaskTargetProjection(**target)
                for target in result["targets"]
            )
            values["result"] = AgentTaskRunProjection(**result)
            return AgentTaskStarted(**values)
        if kind == "WorkflowActivityObserved":
            values["payload_json"] = bytes.fromhex(str(values["payload_json"]))
            return WorkflowActivityObserved(**values)
        if kind == "WorkflowTimerCompleted":
            return WorkflowTimerCompleted(**values)
        raise ValueError(f"unknown workflow final kind: {kind}")

    def _target(self, active: _ActiveRun, target_ref: str) -> TargetRuntime | None:
        return next(
            (
                target
                for target in active.run.targets
                if target.name == target_ref
            ),
            None,
        )

    def _report_in_flight(self, run_id: str) -> bool:
        """True while this run's report effect awaits its completion event."""

        return any(
            item.run_id == run_id
            and isinstance(item.effect, WorkflowDeliveryEffect)
            and item.effect.operation == "deliver:report"
            for item in self._pending.values()
        )

    @staticmethod
    def _has_report_effect(effects: list[WorkflowEffect], run_id: str) -> bool:
        return any(
            isinstance(effect, WorkflowDeliveryEffect)
            and effect.operation == "deliver:report"
            and effect.run_id == run_id
            for effect in effects
        )

    def _reject(self, correlation_id: str, code: str, detail: str) -> None:
        self._publish(
            PortCommandRejected(
                correlation_id=correlation_id,
                domain="workflow",
                generation=self._generation,
                version=self._epoch,
                code=code,
                detail=detail,
                admission=None,
            )
        )

    def _safe_log(self, level: str, event: str, **fields: object) -> None:
        if self._logger is None:
            return
        try:
            self._logger.log(level, event, **fields)  # type: ignore[arg-type]
        except (NameError, ImportError):
            raise
        except Exception:  # noqa: BLE001 - logging must never break the actor
            pass
