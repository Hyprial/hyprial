"""Workflow IPC facade over the actor-owned :mod:`hyprial.workflow.registry`.

The facade may wait at the daemon boundary and perform external effects. It
never owns workflow state: every transition and WorkflowStore write is
serialized by ``WorkflowRegistry``.
"""

from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
from queue import Empty, Full, Queue
import threading
import time
from typing import TypeVar
from uuid import uuid4
import yaml

from hyprial.actor_runtime import ActorRuntime
from hyprial.actor_runtime.contracts import ActorSpec, AdmissionResult
from hyprial.alarm import Alarm, AlarmEmitter, AlarmResult
from hyprial.assign_reconcile import AssignReconcileReport, RoutineExistsProbe
from hyprial.contracts import ipc_errors
from hyprial.contracts.agent_task import (
    AgentTaskActivity,
    AgentTaskCancelled,
    AgentTaskObserved,
    AgentTaskStartInput,
    AgentTaskStarted,
    CancelAgentTaskCommand,
    NAMESPACE as AGENT_TASK_NAMESPACE,
    OPERATIONS as AGENT_TASK_OPERATIONS,
    PROTOCOL_VERSION as AGENT_TASK_PROTOCOL_VERSION,
    ObserveAgentTaskCommand,
    StartAgentTaskCommand,
)
from hyprial.contracts.ports import PortAdmission, PortCommandRejected
from hyprial.dispatch.identity import DISPATCH_SERVICE_ACTOR_NAME
from hyprial.inbox.io import InboxIoDeferred, InboxIoError
from hyprial.log import Logger

from .executor import TargetState, WorkflowDispatchError
from .ports import (
    CancelWorkflowCommand,
    ObserveWorkflowActivityCommand,
    RecoverWorkflowsCommand,
    StartWorkflowCommand,
    StartWorkflowIdempotentCommand,
    WorkflowCommand,
    WorkflowAcknowledgeIoPort,
    WorkflowCancelled,
    WorkflowDeliveryIoPort,
    WorkflowInboxProjectionPort,
    WorkflowIoCompleted,
    WorkflowStarted,
    WorkflowTimerElapsedCommand,
    WorkflowsRecovered,
)
from .registry import (
    RegistryOutput,
    WorkflowAckEffect,
    WorkflowAlarmEffect,
    WorkflowDeliveryEffect,
    WorkflowEffect,
    WorkflowRegistry,
)
from .store import WorkflowStore
from .schema import WorkflowSchemaError, WorkflowSpec


class WorkflowServiceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _wall_ms() -> int:
    import time

    return time.time_ns() // 1_000_000


def _audience_for_recipient(recipient: str) -> str:
    return "human" if recipient.startswith("user:") else "agent"


class _EmitterAlarm:
    def __init__(
        self,
        emitter: AlarmEmitter,
        deliver_user: Callable[[str, str, str], bool] | None = None,
        logger: Logger | None = None,
    ) -> None:
        self._emitter = emitter
        self._deliver_user = deliver_user
        self._logger = logger

    def escalate(
        self,
        *,
        to: str,
        text: str,
        reason: str | None = None,
        conversation_id: str = "workflow",
    ) -> AlarmResult:
        # The escalation text (routine name, breaker reason) is the payload --
        # it must survive to the reader verbatim.  The pre-2026-09-14 code
        # dropped it and rendered only "delivery failed ...
        # WORKFLOW_TARGET_TIMEOUT", which erased why the alarm existed.
        # ``conversation_id`` is the throttle ledger key's first component;
        # callers pass a per-routine / per-run id so one routine's alarm does
        # not eat another routine's (B3, 2026-09-14).  The returned status is
        # the completion code the routine/workflow caller records (S1).
        effective_reason = reason or "WORKFLOW_TARGET_TIMEOUT"
        if to.startswith("user:"):
            try:
                delivered = self._deliver_user is not None and self._deliver_user(
                    to, text, f"workflow-{uuid4().hex[:12]}"
                )
            except InboxIoError as error:
                # The DM callback raises transient (timeout) / permanent
                # failures as InboxIoError; the alarm path is best-effort and
                # must record loud rather than propagate into the routine loop.
                self._log_alarm_failed(
                    to,
                    text,
                    effective_reason,
                    conversation_id,
                    "user-delivery-timeout"
                    if not error.permanent
                    else "user-delivery-rejected",
                )
                return AlarmResult("failed", "human")
            if delivered:
                return AlarmResult("delivered", "human")
            # DM path failed or unwired: fail LOUD with the text preserved,
            # never degrade to a system notice keyed by an unreadable
            # recipient (user: has no notice reader).
            failure = (
                "user-delivery-unwired"
                if self._deliver_user is None
                else "user-delivery-rejected"
            )
            self._log_alarm_failed(to, text, effective_reason, conversation_id, failure)
            return AlarmResult("failed", "human")
        return self._emitter.emit(
            Alarm(
                correlation_id=f"workflow-{uuid4().hex[:12]}",
                message_id=f"workflow-{uuid4().hex[:12]}",
                conversation_id=conversation_id,
                sender=to,
                recipient="workflow",
                reason=effective_reason,
                audience=_audience_for_recipient(to),  # type: ignore[arg-type]
                text=text,
            ),
            delivery=None,
            terminal=False,
        )

    def _log_alarm_failed(
        self, to: str, text: str, reason: str, conversation_id: str, failure: str
    ) -> None:
        if self._logger is None:
            return
        try:
            self._logger.log(
                "error",
                "alarm.failed",
                **{
                    "correlationId": f"workflow-escalate-{uuid4().hex[:12]}",
                    "conversationId": conversation_id,
                    "sender": to,
                    "recipient": to,
                    "noticeRecipient": to,
                    "originalRecipient": to,
                    "reason": reason,
                    "audience": _audience_for_recipient(to),
                    "textLength": len(text),
                    "failure": failure,
                },
            )
        except (NameError, ImportError):
            raise
        except Exception:  # noqa: BLE001 - logging must never break escalation
            pass


_ResultT = TypeVar("_ResultT")
_STOP = object()


class WorkflowFacade:
    """Backward-compatible workflow.* facade; Registry owns all mutation."""

    def __init__(
        self,
        *,
        alarm: AlarmEmitter,
        state_dir: Path,
        delivery_io: WorkflowDeliveryIoPort,
        acknowledge_io: WorkflowAcknowledgeIoPort,
        inbox_projection: WorkflowInboxProjectionPort,
        deliver_user: Callable[[str, str, str], bool] | None = None,
        clock_ms: Callable[[], int] | None = None,
        mailbox_capacity: int = 128,
        service_actor: str | None = None,
        logger: Logger | None = None,
        routine_probe: RoutineExistsProbe | None = None,
        assign_reconcile_sink: Callable[[AssignReconcileReport], None] | None = None,
        dispatch_gate: Callable[[WorkflowSpec, str | None, str], tuple[str, ...]] | None = None,
    ) -> None:
        self._inbox_projection = inbox_projection
        self._alarm = _EmitterAlarm(alarm, deliver_user, logger)
        self._logger = logger
        self._delivery_io = delivery_io
        self._acknowledge_io = acknowledge_io
        self._clock_ms = clock_ms or _wall_ms
        self._service_actor = service_actor
        self._database = state_dir / "workflows.sqlite3"
        self._projection = WorkflowStore(self._database)
        self._condition = threading.Condition()
        self._results: dict[str, object] = {}
        self._waiters: set[str] = set()
        self._effects: Queue[WorkflowEffect | object] = Queue(
            maxsize=mailbox_capacity * 2
        )
        self._writer_store: WorkflowStore | None = None
        # §C.1 周期性核对的注入(语义见 hyprial.assign_reconcile)。两者都可为 None,
        # 而 None 的后果只会是核对更保守(routine 边判 unknown、报告不出 tick),
        # 不会是更激进 —— 缺席注入绝不能变成误杀。
        self._routine_probe = routine_probe
        self._assign_reconcile_sink = assign_reconcile_sink
        self._effect_ids: dict[str, int] = {}
        self._effect_ids_lock = threading.Lock()
        self._generation = 0
        self._timer_sequence = 0
        self._closed = False
        self._runtime = ActorRuntime()

        def handler_factory() -> WorkflowRegistry:
            self._generation += 1
            if self._writer_store is not None:
                self._writer_store.close()
            store = WorkflowStore(self._database)
            self._writer_store = store
            return WorkflowRegistry(
                store=store,
                generation=self._generation,
                publish=self._publish,
                clock_ms=self._clock_ms,
                routine_probe=self._routine_probe,
                assign_reconcile_sink=self._assign_reconcile_sink,
                dispatch_gate=dispatch_gate,
                logger=self._logger,
            )

        self._handle = self._runtime.start(
            ActorSpec(
                name="workflow-registry",
                handler_factory=handler_factory,
                mailbox_capacity=mailbox_capacity,
                supervision_profile="state_authority",
            )
        )
        self._effect_thread = threading.Thread(
            target=self._effect_loop,
            name="hyprial-workflow-effects",
            daemon=True,
        )
        self._effect_thread.start()

    @property
    def alarm_sink(self) -> _EmitterAlarm:
        return self._alarm

    def submit(self, command: WorkflowCommand) -> PortAdmission:
        if not isinstance(
            command,
            (
                StartWorkflowCommand,
                StartWorkflowIdempotentCommand,
                CancelWorkflowCommand,
                RecoverWorkflowsCommand,
                WorkflowTimerElapsedCommand,
                ObserveWorkflowActivityCommand,
                StartAgentTaskCommand,
                CancelAgentTaskCommand,
                ObserveAgentTaskCommand,
            ),
        ):
            raise TypeError(f"unsupported workflow command: {type(command).__name__}")
        if self._closed:
            return PortAdmission.CLOSING
        admission = self._runtime.tell(self._handle, command)
        return {
            AdmissionResult.ACCEPTED: PortAdmission.ACCEPTED,
            AdmissionResult.OVERLOADED: PortAdmission.OVERLOADED,
            AdmissionResult.CLOSED: PortAdmission.CLOSING,
        }[admission]

    def start(self, *, yaml_text: str, sender: str) -> dict[str, object]:
        correlation = self._correlation()
        event = self._submit_wait(
            StartWorkflowCommand(correlation, yaml_text, sender),
            correlation,
            WorkflowStarted,
        )
        return event.result.to_payload()

    def start_idempotent(
        self, *, external_ref: str, yaml_text: str, sender: str
    ) -> dict[str, object]:
        """Start once for a stable external reference, or replay its run."""

        correlation = self._correlation()
        event = self._submit_wait(
            StartWorkflowIdempotentCommand(
                correlation_id=correlation,
                external_ref=external_ref,
                yaml_text=yaml_text,
                sender=sender,
            ),
            correlation,
            WorkflowStarted,
        )
        return event.result.to_payload()

    def agent_task_capabilities(self) -> dict[str, object]:
        service_actor = self._require_agent_task_service()
        return {
            "protocolVersion": AGENT_TASK_PROTOCOL_VERSION,
            "namespace": AGENT_TASK_NAMESPACE,
            "operations": list(AGENT_TASK_OPERATIONS),
            "features": {
                "externalRefIdempotency": True,
                "typedActivity": True,
                "explicitFinalResult": True,
                "durableResult": True,
                "multiTarget": True,
            },
            "serviceIdentity": {
                "actorName": DISPATCH_SERVICE_ACTOR_NAME,
                "actorUri": service_actor,
                "binding": "daemon-managed",
            },
        }

    def agent_task_start(
        self, *, request: AgentTaskStartInput, caller: str
    ) -> dict[str, object]:
        service_actor = self._require_agent_task_service()
        correlation = self._correlation()
        event = self._submit_wait(
            StartAgentTaskCommand(
                correlation_id=correlation,
                service_actor=service_actor,
                caller=caller,
                request=request,
                yaml_text=self._agent_task_yaml(request),
            ),
            correlation,
            AgentTaskStarted,
        )
        return event.result.to_payload()

    def agent_task_status(self, *, run_id: str) -> dict[str, object]:
        projection = self._projection.read_agent_task_run(
            run_id, self._require_agent_task_service()
        )
        if projection is None:
            raise WorkflowServiceError(
                ipc_errors.RUN_NOT_FOUND,
                f"no run in this service/namespace scope: {run_id}",
            )
        return projection.to_payload()

    def agent_task_result(
        self, *, run_id: str, target_ref: str | None
    ) -> dict[str, object]:
        service_actor = self._require_agent_task_service()
        projection = self._projection.read_agent_task_result(run_id, service_actor)
        if projection is None:
            raise WorkflowServiceError(
                ipc_errors.RUN_NOT_FOUND,
                f"no run in this service/namespace scope: {run_id}",
            )
        if target_ref is not None:
            target = next(
                (item for item in projection.targets if item.target_ref == target_ref),
                None,
            )
            if target is None:
                raise WorkflowServiceError(
                    ipc_errors.TARGET_NOT_FOUND,
                    f"no targetRef {target_ref} in run {run_id}",
                )
            if target.result is None:
                raise WorkflowServiceError(
                    ipc_errors.RESULT_NOT_READY,
                    f"target {target_ref} has no submitted result",
                )
        return projection.to_payload()

    def agent_task_cancel(
        self, *, run_id: str, caller: str, reason: str | None
    ) -> dict[str, object]:
        correlation = self._correlation()
        event = self._submit_wait(
            CancelAgentTaskCommand(
                correlation_id=correlation,
                service_actor=self._require_agent_task_service(),
                caller=caller,
                run_id=run_id,
                reason=reason,
            ),
            correlation,
            AgentTaskCancelled,
        )
        return event.result.to_payload()

    def agent_task_observe(
        self,
        *,
        activity: AgentTaskActivity,
        submitter: str,
        message_id: str | None = None,
    ) -> dict[str, object]:
        """Submit typed activity to WorkflowRegistry; never mutate locally."""

        correlation = self._correlation()
        event = self._submit_wait(
            ObserveAgentTaskCommand(
                correlation_id=correlation,
                service_actor=self._require_agent_task_service(),
                submitter=submitter,
                activity=activity,
                message_id=message_id or activity.event_id,
            ),
            correlation,
            AgentTaskObserved,
        )
        return {
            "accepted": event.accepted,
            "created": event.created,
            "eventId": event.event_id,
        }

    def status(self, *, run_id: str) -> dict[str, object]:
        try:
            persisted = self._projection.load_run(run_id)
        except WorkflowSchemaError as error:
            # A stored run that no longer validates (e.g. a legacy bare-name
            # address) must answer loud, not crash the read path.
            raise WorkflowServiceError(
                "WORKFLOW_SCHEMA_ERROR",
                f"stored run {run_id} no longer validates: {error}",
            ) from error
        if persisted is None:
            raise WorkflowServiceError(
                "WORKFLOW_RUN_NOT_FOUND", f"no such run: {run_id}"
            )
        return persisted.run.status()

    def node_context(self, *, run_id: str, target_ref: str, sender: str) -> dict[str, object]:
        try:
            persisted = self._projection.load_run(run_id)
        except WorkflowSchemaError as error:
            # A stored run that no longer validates must answer loud, not
            # crash the read path (2026-09-14 S3).
            raise WorkflowServiceError(
                "WORKFLOW_SCHEMA_ERROR",
                f"stored run {run_id} no longer validates: {error}",
            ) from error
        if persisted is None or persisted.run.sender != sender:
            raise WorkflowServiceError("WORKFLOW_NODE_FORBIDDEN", "Run is not readable by this sender")
        run = persisted.run
        target = next((t for t in run.targets if t.name == target_ref), None)
        if target is None:
            raise WorkflowServiceError("WORKFLOW_TARGET_NOT_FOUND", "Target does not belong to this run")
        deliveries = self._projection.node_deliveries(run_id, target_ref)
        return {"runId": run_id, "target": target_ref, "sender": sender,
                "conversationId": target.conversation_id, "deliveries": deliveries,
                "tracking": next(t for t in run.status()["targets"] if t["target"] == target_ref),
                "awaitKind": run.spec.await_.kind,
                "match": run.spec.await_.match.replace("{{nonce}}", run.nonce).replace("{{target}}", target_ref)
                if run.spec.await_.match else None}

    def list(self, *, limit: int = 50) -> dict[str, object]:
        runs, rejections = self._projection.list_runs_report(limit=limit)
        # Rejections ride the listing so a schema-broken run cannot vanish
        # from `workflow list` -- it stays visible with its error.
        return {
            "runs": [
                item.run.status() for item in runs
            ]
            + [
                {
                    "runId": rejection.run_id,
                    "name": None,
                    "state": "schema-error",
                    "schemaError": rejection.error,
                }
                for rejection in rejections
            ]
        }

    def cancel(self, *, run_id: str) -> dict[str, object]:
        correlation = self._correlation()
        event = self._submit_wait(
            CancelWorkflowCommand(correlation, run_id),
            correlation,
            WorkflowCancelled,
        )
        return event.result.to_payload()

    def recover(self) -> int:
        correlation = self._correlation()
        event = self._submit_wait(
            RecoverWorkflowsCommand(correlation),
            correlation,
            WorkflowsRecovered,
        )
        return event.adopted

    def submit_timer(self, observed_at_ms: int | None = None) -> PortAdmission:
        """Submit one generation-fenced cadence command to the Workflow actor."""

        self._observe_pending()
        snapshot = self._runtime.snapshot(self._handle)
        correlation = self._correlation()
        self._timer_sequence += 1
        return self.submit(
            WorkflowTimerElapsedCommand(
                correlation_id=correlation,
                generation=snapshot.generation,
                version=self._timer_sequence,
                observed_at_ms=(
                    self._clock_ms()
                    if observed_at_ms is None
                    else observed_at_ms
                ),
            )
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        drained = self._runtime.drain(timeout=2.0)
        if not drained.complete:
            self._runtime.stop(self._handle, timeout=0.0)
        try:
            self._effects.put_nowait(_STOP)
        except Full:
            pass
        self._effect_thread.join(timeout=2.0)
        self._projection.close()
        if drained.complete and self._writer_store is not None:
            self._writer_store.close()

    def _observe_pending(self) -> None:
        pending = self._inbox_projection.read_pending()
        for persisted in self._projection.load_open_runs():
            run = persisted.run
            for target in run.targets:
                if target.state is not TargetState.DISPATCHED:
                    continue
                if run.spec.await_.kind == "ack":
                    message_id = target.last_message_id
                    if (
                        message_id is not None
                        and not message_id.startswith("pending:")
                        and self._inbox_projection.read_consumption_state(message_id)
                        == "consumed"
                    ):
                        self._submit_activity(
                            run.run_id,
                            target.name,
                            target.conversation_id,
                            "ack",
                            b"",
                            message_id,
                        )
                    continue
                for message in pending:
                    if message.conversation_id != target.conversation_id:
                        continue
                    if message.sender == run.sender and message.intent == "request":
                        continue
                    self._submit_activity(
                        run.run_id,
                        target.name,
                        target.conversation_id,
                        "reply",
                        self._text_of(message.payload).encode(),
                        message.message_id,
                    )

    def _submit_activity(
        self,
        run_id: str,
        target_ref: str,
        conversation_id: str,
        kind: str,
        payload: bytes,
        message_id: str,
    ) -> None:
        correlation = self._correlation()
        self.submit(
            ObserveWorkflowActivityCommand(
                correlation_id=correlation,
                run_id=run_id,
                target_ref=target_ref,
                conversation_id=conversation_id,
                kind=kind,
                payload_json=payload,
                message_id=message_id,
            ),
        )

    def _require_agent_task_service(self) -> str:
        if self._service_actor is None:
            raise WorkflowServiceError(
                ipc_errors.SERVICE_BINDING_NOT_FOUND,
                "the daemon-managed mfu-coordinator service actor is not registered",
            )
        return self._service_actor

    @staticmethod
    def _agent_task_yaml(request: AgentTaskStartInput) -> str:
        dispatch = {
            "schemaVersion": "hyprial.agent-task.dispatch/v1",
            "externalRef": request.external_ref,
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
            "completion": {"kind": "result.submitted"},
            "assignedTarget": "{{target}}",
        }
        document = {
            "version": 1,
            "name": f"agent-task-{request.external_ref}",
            "task": json.dumps(
                dispatch,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ),
            "targets": [target.target for target in request.targets],
            "await": {"kind": "reply", "timeout": "10m"},
            "on_timeout": {"action": "report", "max_attempts": 1},
            "limits": {"max_targets": len(request.targets)},
        }
        return yaml.safe_dump(document, sort_keys=False, allow_unicode=True)

    def _submit_wait(
        self,
        command: WorkflowCommand,
        correlation_id: str,
        expected: type[_ResultT],
    ) -> _ResultT:
        with self._condition:
            self._waiters.add(correlation_id)
        admission = self.submit(command)
        if admission is not PortAdmission.ACCEPTED:
            with self._condition:
                self._waiters.discard(correlation_id)
            raise WorkflowServiceError(
                (
                    "WORKFLOW_OVERLOADED"
                    if admission is PortAdmission.OVERLOADED
                    else "WORKFLOW_CLOSING"
                ),
                f"workflow registry admission: {admission}",
            )
        with self._condition:
            ready = self._condition.wait_for(
                lambda: correlation_id in self._results,
                timeout=30.0,
            )
            if not ready:
                self._waiters.discard(correlation_id)
                self._results.pop(correlation_id, None)
                raise WorkflowServiceError(
                    "WORKFLOW_COMMAND_TIMEOUT",
                    f"workflow command {correlation_id} did not settle",
                )
            result = self._results.pop(correlation_id)
            self._waiters.discard(correlation_id)
        if isinstance(result, PortCommandRejected):
            raise WorkflowServiceError(result.code, result.detail)
        if not isinstance(result, expected):
            raise WorkflowServiceError(
                "WORKFLOW_PROTOCOL_ERROR",
                f"expected {expected.__name__}, got {type(result).__name__}",
            )
        return result

    def _publish(self, output: RegistryOutput) -> None:
        if isinstance(
            output,
            (WorkflowDeliveryEffect, WorkflowAckEffect, WorkflowAlarmEffect),
        ):
            self._enqueue_effect(output)
            return
        with self._condition:
            if output.correlation_id not in self._waiters:
                return
            self._results[output.correlation_id] = output
            self._condition.notify_all()

    def _effect_loop(self) -> None:
        while True:
            try:
                effect = self._effects.get(timeout=0.1)
            except Empty:
                if self._closed:
                    return
                self._refill_effects()
                continue
            if effect is _STOP:
                return
            assert isinstance(
                effect,
                (WorkflowDeliveryEffect, WorkflowAckEffect, WorkflowAlarmEffect),
            )
            try:
                completion = self._execute_effect(effect)
            except InboxIoDeferred:
                with self._effect_ids_lock:
                    self._effect_ids.pop(effect.effect_id, None)
                if self._closed:
                    return
                time.sleep(0.01)
                self._refill_effects()
                continue
            self._submit_completion(effect, completion)
            self._refill_effects()

    def _enqueue_effect(self, effect: WorkflowEffect) -> None:
        with self._effect_ids_lock:
            if effect.effect_id in self._effect_ids:
                return
            self._effect_ids[effect.effect_id] = effect.generation
        try:
            self._effects.put_nowait(effect)
        except Full:
            with self._effect_ids_lock:
                self._effect_ids.pop(effect.effect_id, None)

    def _refill_effects(self) -> None:
        payloads = self._projection.pending_effect_payloads(limit=10_000)
        pending_ids = {str(payload["effect_id"]) for payload in payloads}
        with self._effect_ids_lock:
            for effect_id in tuple(self._effect_ids):
                if effect_id not in pending_ids:
                    self._effect_ids.pop(effect_id, None)
        for payload in payloads:
            self._enqueue_effect(WorkflowRegistry._decode_effect(payload))

    def _submit_completion(
        self,
        effect: WorkflowEffect,
        completion: WorkflowIoCompleted,
    ) -> None:
        while not self._closed:
            admission = self._runtime.tell(self._handle, completion)
            if admission is AdmissionResult.ACCEPTED:
                return
            try:
                generation = self._runtime.snapshot(self._handle).generation
            except Exception:
                generation = effect.generation
            if generation != effect.generation:
                with self._effect_ids_lock:
                    self._effect_ids.pop(effect.effect_id, None)
                return
            time.sleep(0.01)

    def _execute_effect(self, effect: WorkflowEffect) -> WorkflowIoCompleted:
        succeeded = False
        message_id: str | None = None
        recipient: str | None = None
        code: str | None = None
        detail: str | None = None
        permanent = False
        try:
            if isinstance(effect, WorkflowDeliveryEffect):
                if self._delivery_io is None:
                    raise WorkflowDispatchError(
                        "no WorkflowDeliveryIoPort wired; the daemon must provide its canonical idempotent delivery path",
                        permanent=True,
                    )
                delivered = self._delivery_io.deliver(
                    effect_id=effect.effect_id,
                    sender=effect.sender,
                    target=effect.target_ref,
                    conversation_id=effect.conversation_id,
                    text=effect.text,
                )
                message_id = delivered.message_id
                recipient = delivered.recipient
            elif isinstance(effect, WorkflowAckEffect):
                for message in self._inbox_projection.read_pending():
                    if message.message_id == effect.message_id:
                        if not self._acknowledge_io.acknowledge(
                            effect_id=effect.effect_id,
                            recipient=message.recipient,
                            message_id=message.message_id,
                        ):
                            raise WorkflowDispatchError(
                                f"typed inbox did not acknowledge {message.message_id}"
                            )
                        break
            else:
                self._alarm.escalate(
                    to=effect.to,
                    text=effect.text,
                    conversation_id=f"workflow:{effect.run_id}",
                )
            succeeded = True
        except InboxIoDeferred:
            raise
        except InboxIoError as error:
            code = "WORKFLOW_DELIVERY_FAILED"
            detail = str(error)
            permanent = error.permanent
        except WorkflowDispatchError as error:
            code = "WORKFLOW_DELIVERY_FAILED"
            detail = str(error)
            permanent = error.permanent
        except Exception as error:
            code = type(error).__name__
            detail = str(error)
        return WorkflowIoCompleted(
            correlation_id=effect.effect_id,
            generation=effect.generation,
            version=effect.version,
            run_id=effect.run_id,
            target_ref=effect.target_ref,
            operation=effect.operation,
            succeeded=succeeded,
            message_id=message_id,
            code=code,
            detail=detail,
            permanent=permanent,
            recipient=recipient,
        )

    @staticmethod
    def _text_of(payload: bytes) -> str:
        try:
            body = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return payload.decode(errors="replace")
        if isinstance(body, dict) and isinstance(body.get("message"), str):
            return body["message"]
        return payload.decode(errors="replace")

    @staticmethod
    def _correlation() -> str:
        return f"wf-corr-{uuid4().hex}"


# Compatibility import only. The class is a facade, not a second state owner.
WorkflowService = WorkflowFacade

__all__ = [
    "WorkflowDeliveryIoPort",
    "WorkflowFacade",
    "WorkflowService",
    "WorkflowServiceError",
]
