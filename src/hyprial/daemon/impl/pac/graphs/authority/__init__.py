"""Typed PAC graph writer with a separate durable-notification I/O lane.

This port owns the graph writes routed to it.  The rest of the PAC producers
must be routed to the same authority before the database has one writer in
production; constructing this class alone does not make that claim true.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from hyprial.kernel import ActorRuntime, ActorSpec, AdmissionResult
from hyprial.kernel import PAC_WORKTREE_GIT_TIMEOUT_SECONDS

from hyprial.daemon.impl.pac.actors.coordinator  import FileLaunchResolver
from hyprial.daemon.impl.pac.graphs.reactor  import NotificationSender, permanent_delivery_failure
from hyprial.daemon.impl.pac.contracts.restore  import PacRestoreFacts
from hyprial.daemon.impl.pac.storage.store  import PacGraphStore
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
    PacGraphOverloaded,
    PacGraphTimeout,
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
    _ActorEffectNeeded,  # noqa: F401  (facade compatibility)
    _DeferredResolver,  # noqa: F401  (facade compatibility)
    _DeferredRuntime,  # noqa: F401  (facade compatibility)
    _Delivery,
    _EMPTY_RESTORE_FACTS,
    _Pending,
    _ReconcileClaim,
    _RESTORE_FACT_COMMANDS,  # noqa: F401  (facade compatibility)
)
from hyprial.daemon.impl.pac.graphs.authority.generation import _Generation


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
        self._reconcile_capacity = mailbox_capacity
        self._reconcile_claims: dict[tuple[str, str], _ReconcileClaim] = {}
        self._late_results: OrderedDict[str, GraphCommandCompleted] = OrderedDict()
        self._lock = threading.Lock()
        self._closed = False
        self._generation = 0
        self._workflow_service: Any | None = None
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
        self._preflight_pending: dict[
            str, tuple[threading.Event, list[Any]]
        ] = {}
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
        if isinstance(command, ReconcileActor):
            self._retire_reconcile_claim(command)
            return
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
        expansion: str | None = None, expansion_context: Any | None = None,
    ) -> dict[str, Any]:
        return self._call(SetFlag(
            f"pac-set-{uuid4().hex}", graph_id, node_id, actor, reason_ref,
            expected_request, output_text, expansion, expansion_context,
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
        key = (graph_id, node_id)
        with self._lock:
            if self._closed:
                return AdmissionResult.CLOSED
            existing = self._reconcile_claims.get(key)
            if existing is not None:
                # A concurrent tell may still refuse. Share custody only
                # after real admission, never while its result is unknown.
                return (
                    AdmissionResult.ACCEPTED if existing.admitted
                    else AdmissionResult.OVERLOADED
                )
            if len(self._reconcile_claims) >= self._reconcile_capacity:
                return AdmissionResult.OVERLOADED
            claim = _ReconcileClaim(ReconcileActor(graph_id, node_id))
            self._reconcile_claims[key] = claim
        try:
            # tell can synchronously call the undelivered sink; never hold
            # the owner lock across this boundary.
            admission = self._runtime.tell(self._handle, claim.command)
        except BaseException:
            self._retire_reconcile_claim(claim.command)
            raise
        with self._lock:
            # The handler or undelivered callback may already have retired
            # this ticket and a later caller may own the same key now.
            if self._reconcile_claims.get(key) is claim:
                if admission is AdmissionResult.ACCEPTED:
                    claim.admitted = True
                else:
                    del self._reconcile_claims[key]
        return admission

    def reconcile_worktree(self, graph_id: str, node_id: str) -> AdmissionResult:
        """Admit one independently replayable closed-worktree cleanup."""

        with self._lock:
            if self._closed:
                return AdmissionResult.CLOSED
        effect = _ActorEffect(
            self._generation,
            graph_id,
            node_id,
            "worktree-cleanup",
            "",
        )
        self._submit_actor_effect(effect)
        return AdmissionResult.ACCEPTED

    def prepare_expansion(self, **facts: Any):
        """Run bounded policy/artifact/git preflight off the caller thread."""

        correlation = f"pac-expansion-preflight-{uuid4().hex}"
        reserve = min(0.05, self._call_timeout / 2)
        caller_deadline = time.monotonic() + self._call_timeout
        facts = {
            **facts,
            "git_timeout_s": min(
                PAC_WORKTREE_GIT_TIMEOUT_SECONDS,
                max(0.001, self._call_timeout - reserve),
            ),
            "_caller_deadline_monotonic": caller_deadline,
        }
        event = threading.Event()
        box: list[Any] = []
        with self._actor_lock:
            if self._closed:
                raise PacGraphOverloaded("PAC graph authority is closing")
            self._preflight_pending[correlation] = (event, box)
            try:
                self._actor_effects.put_nowait(
                    _ActorEffect(
                        self._generation,
                        str(facts["graph_id"]),
                        str(facts["node_id"]),
                        "expansion-preflight",
                        "",
                        correlation_id=correlation,
                        preflight=facts,
                    )
                )
            except queue.Full as error:
                self._preflight_pending.pop(correlation, None)
                raise PacGraphOverloaded("PAC expansion preflight lane is full") from error
        if not event.wait(self._call_timeout):
            with self._actor_lock:
                self._preflight_pending.pop(correlation, None)
            raise PacGraphTimeout(correlation)
        with self._actor_lock:
            self._preflight_pending.pop(correlation, None)
        result = box[0]
        if result.exception is not None:
            raise result.exception
        assert result.prepared_expansion is not None
        return result.prepared_expansion

    def _retire_reconcile_claim(self, command: ReconcileActor) -> None:
        key = (command.graph_id, command.node_id)
        with self._lock:
            claim = self._reconcile_claims.get(key)
            if claim is not None and claim.command is command:
                del self._reconcile_claims[key]

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
            git_operation_deadline = (
                time.monotonic() + PAC_WORKTREE_GIT_TIMEOUT_SECONDS
            )
            runtime = self._actor_runtime
            worktree_result = None
            has_worktree_plan = False
            try:
                if runtime is None and effect.kind not in {
                    "worktree-cleanup",
                    "expansion-preflight",
                }:
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
                    if effect.kind == "start":
                        from hyprial.daemon.impl.pac.actors.worktrees import (
                            prepare_worktree,
                        )

                        read = PacGraphStore(self.database, read_only=True)
                        try:
                            row = read._db.execute(
                                "SELECT w.plan_json,w.state,g.expansion_deadline_ms "
                                "FROM workflow_worktrees w "
                                "JOIN workflow_graphs g ON g.graph_id=w.graph_id "
                                "WHERE w.graph_id=? AND w.actor_node=?",
                                (effect.graph_id, effect.node_id),
                            ).fetchone()
                        finally:
                            read.close()
                        has_worktree_plan = row is not None
                        if row is not None:
                            if row["state"] == "planned":
                                remaining = (
                                    git_operation_deadline - time.monotonic()
                                )
                                if row["expansion_deadline_ms"] is not None:
                                    remaining = min(
                                        remaining,
                                        (
                                            int(row["expansion_deadline_ms"])
                                            - time.time_ns() // 1_000_000
                                        )
                                        / 1000,
                                    )
                                if remaining <= 0:
                                    raise TimeoutError(
                                        "managed worktree prepare deadline passed"
                                    )
                                worktree_result = prepare_worktree(
                                    json.loads(row["plan_json"]),
                                    timeout_s=min(
                                        PAC_WORKTREE_GIT_TIMEOUT_SECONDS,
                                        remaining,
                                    ),
                                )
                            elif row["state"] != "prepared":
                                raise RuntimeError(
                                    f"managed worktree is not startable: {row['state']}"
                                )
                    invoke = runtime.start if effect.kind == "start" else runtime.stop
                    observation = invoke(
                        effect.actor_name, effect.launch,
                        operation_id=effect.operation_id,
                        identity_marker=effect.identity_marker,
                    )
                    completed = _ActorEffectCompleted(
                        effect.generation, effect, observation=observation,
                        worktree_result=worktree_result,
                    )
                elif effect.kind == "worktree-cleanup":
                    from hyprial.daemon.impl.pac.actors.worktrees import (
                        cleanup_worktree,
                    )

                    read = PacGraphStore(self.database, read_only=True)
                    try:
                        row = read._db.execute(
                            "SELECT plan_json,state FROM workflow_worktrees "
                            "WHERE graph_id=? AND node_id=?",
                            (effect.graph_id, effect.node_id),
                        ).fetchone()
                    finally:
                        read.close()
                    if row is None or row["state"] != "cleanup_pending":
                        worktree_result = None
                    else:
                        worktree_result = cleanup_worktree(
                            json.loads(row["plan_json"]),
                            timeout_s=min(
                                PAC_WORKTREE_GIT_TIMEOUT_SECONDS,
                                max(
                                    0.001,
                                    git_operation_deadline - time.monotonic(),
                                ),
                            ),
                        )
                    completed = _ActorEffectCompleted(
                        effect.generation,
                        effect,
                        worktree_result=worktree_result,
                    )
                elif effect.kind == "expansion-preflight":
                    from hyprial.daemon.impl.pac.graphs.authority.expansion import (
                        prepare_expansion,
                    )

                    assert effect.preflight is not None
                    preflight = dict(effect.preflight)
                    caller_deadline = float(
                        preflight.pop("_caller_deadline_monotonic")
                    )
                    remaining = caller_deadline - time.monotonic() - 0.01
                    if remaining <= 0:
                        raise PacGraphTimeout(effect.correlation_id or "")
                    preflight["git_timeout_s"] = min(
                        float(preflight["git_timeout_s"]), remaining
                    )
                    context = prepare_expansion(**preflight)
                    completed = _ActorEffectCompleted(
                        effect.generation,
                        effect,
                        prepared_expansion=context,
                    )
                else:
                    raise TypeError("unsupported PAC actor effect")
            except Exception as error:
                if worktree_result is None and (
                    effect.kind == "worktree-cleanup"
                    or (effect.kind == "start" and has_worktree_plan)
                ):
                    worktree_result = {
                        "state": "attention",
                        "reason": "prepare-or-cleanup-failed",
                    }
                completed = _ActorEffectCompleted(
                    effect.generation, effect,
                    error_code=(
                        "stop_timeout"
                        if isinstance(error, TimeoutError)
                        else "lifecycle_manager_unavailable"
                    ),
                    error_type=type(error).__name__,
                    error=f"{type(error).__name__}: {error}",
                    worktree_result=worktree_result,
                    exception=error,
                )
            if effect.kind == "expansion-preflight":
                with self._actor_lock:
                    pending = self._preflight_pending.get(
                        effect.correlation_id or ""
                    )
                    if pending is not None:
                        pending[1].append(completed)
                        pending[0].set()
                continue
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
        spec: Any | None = None,
    ) -> str | None:
        return self._call(ProbeWorkflow(
            f"pac-workflow-probe-{uuid4().hex}", yaml_text, sender,
            operation_key, routine_name, task_key, spec,
        ))["graphId"]

    def start_workflow(
        self, *, yaml_text: str, sender: str, operation_key: str,
        routine_name: str | None, task_key: str | None,
        machine: str, local_owner: str, at: int, spec: Any | None = None,
    ) -> str:
        return str(self._call(StartWorkflow(
            f"pac-workflow-start-{uuid4().hex}", yaml_text, sender,
            operation_key, routine_name, task_key, machine, local_owner, at, spec,
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
        output_text: str | None, expansion_text: str | None = None,
        expansion_digest: str | None = None,
    ) -> None:
        self._call(EnqueueRemoteOutcome(
            f"pac-remote-enqueue-{uuid4().hex}", request_id, action,
            reason_ref, output_text, expansion_text, expansion_digest,
        ))

    def mark_remote_attempt(self, request_id: str, attempted_at_ms: int) -> None:
        self._call(MarkRemoteAttempt(
            f"pac-remote-attempt-{uuid4().hex}", request_id,
            attempted_at_ms,
        ))

    def record_remote_result(
        self, request_id: str, result_json: str, attempt_no: int = 0
    ) -> None:
        self._call(RecordRemoteResult(
            f"pac-remote-result-{uuid4().hex}", request_id, result_json,
            attempt_no,
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
        with self._lock:
            claims_settled = not self._reconcile_claims
        return (
            not self._delivery.is_alive()
            and not any(worker.is_alive() for worker in self._actor_workers)
            and drained.complete
            and claims_settled
        )
