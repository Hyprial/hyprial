"""Canonical single harness runtime actor.

HarnessRuntimeActor is the sole owner of the mailbox, handler generation,
in-flight custody and all mutable lifecycle state.  Behaviour mixins borrow
that state through ``self`` but never own a second copy of it.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import get_args
from hyprial.kernel import ActorHandle, ActorRuntime, ActorSpec, AdmissionResult
from hyprial.kernel import EffectCompleted, EffectLane
from hyprial.daemon.impl.api  import (
    HarnessLauncher,
    )
from hyprial.kernel  import (
    ManagedHarnessProcess,
)
from hyprial.daemon.impl.desired_state  import DesiredStateStore
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.desired_state_io  import (
    DesiredStateIoCompleted,
    DesiredStateIoPort,
    DesiredStateOperation,
)
from hyprial.daemon.impl.state_persistence  import StateCommandCompleted
from hyprial.daemon.impl.processes.orphan_processes  import OrphanProcessRegistry, OrphanProcessAuthority
from hyprial.daemon.impl.harnesses.runtime.ports  import (
    BindHarnessLivenessCommand,
    DispatchHarnessDeliveryCommand,
    DrainHarnessFailedCommand,
    DrainHarnessProgressCommand,
    DrainHarnessReadinessCommand,
    DrainHarnessResultsCommand,
    ClaimHarnessResultsCommand,
    SettleHarnessResultCommand,
    ClaimedHarnessResult,
    EnsureHarnessCommand,
    HarnessCallIoCompleted,
    HarnessCommand,
    HarnessFailedDrained,
    HarnessProcessStarted,
    HarnessProjectionsRefreshed,
    HarnessReadinessDrained,
    HarnessSessionRefsProjection,
    HarnessStartTimerElapsedCommand,
    HarnessStatusProjection,
    HarnessStopIoCompleted,
    HarnessStreamingProjection,
    HarnessTimerElapsedCommand,
    RemoveHarnessCommand,
    RemoveAdapterRegistrationCommand,
    RefreshHarnessProjectionsCommand,
    ReconcileHarnessSessionRefsCommand,
    RestoreEligibilityProjection,
    AgentIdentityProjectionPort,
    RestoreAdapterRegistrationCommand,
    RestoreHarnessesCommand,
    UpdateRestoreEligibilityCommand,
    StopHarnessesCommand,
    SnapshotAdapterRegistrationCommand,
    StageHarnessDesiredCommand,
    WaitHarnessReadyCommand,
)
from hyprial.daemon.impl.correlation.readiness_budget  import (
    START_ADMISSION_WIDTH_DEFAULT,
    START_TIMEOUT_SECONDS_DEFAULT,
    restore_rounds,
)
from hyprial.kernel import ipc_errors
from hyprial.kernel import PortAdmission, PortCommandRejected
from hyprial.kernel import ReadinessReport

from .behaviors.delivery import _DeliveryBehavior
from .behaviors.desired import _DesiredBehavior
from .behaviors.lifecycle import _LifecycleBehavior
from .behaviors.process_events import _ProcessEventsBehavior
from .behaviors.reconcile import _ReconcileBehavior
from .contracts import HarnessProjection, ProcessIdentity, _ClaimScan, _ClaimScanOutcome, _CompletionReceipt, _DeliveryReservation, _EnsureFailureFence, _HarnessActorGeneration, _LIFECYCLE_SETTLED_CAPACITY, _PendingCallRegistry, _Record, _RestoreBatch, _default_identity_reader, _is_io_completion
from .io_port import ProcessIoPort

class HarnessRuntimeActor(
    _DesiredBehavior,
    _ProcessEventsBehavior,
    _DeliveryBehavior,
    _ReconcileBehavior,
    _LifecycleBehavior
):
    """Single-writer coordinator for the complete harness lifecycle domain."""

    def __init__(
        self,
        launcher: HarnessLauncher,
        *,
        runtime: ActorRuntime | None = None,
        projection: HarnessProjection | None = None,
        clock: Callable[[], float] = time.monotonic,
        restart_backoff_seconds: float = 0.0,
        start_timeout_seconds: float = START_TIMEOUT_SECONDS_DEFAULT,
        start_backoff_max_seconds: float = 60.0,
        failure_budget: int = 3,
        start_max_workers: int = START_ADMISSION_WIDTH_DEFAULT,
        mailbox_capacity: int = 128,
        lifecycle_retry_base_seconds: float = 0.05,
        lifecycle_replay_capacity: int = _LIFECYCLE_SETTLED_CAPACITY,
        identity_reader: Callable[[int], str | None] = _default_identity_reader,
        orphan_processes: OrphanProcessRegistry | None = None,
        orphan_state_path: Path | None = None,
        orphan_logger: Callable[..., None] | None = None,
        event_sink: object | None = None,
        desired_state: DesiredStateStore | None = None,
        automatic_restore_allowed: Callable[[HarnessLaunchSpec], bool] | None = None,
        persistence_late_result: (
            Callable[[str], StateCommandCompleted | None] | None
        ) = None,
        agent_identity: AgentIdentityProjectionPort | None = None,
    ) -> None:
        self._runtime = runtime or ActorRuntime()
        self._projection = projection or HarnessProjection()
        self._clock = clock
        self._restart_backoff_seconds = restart_backoff_seconds
        self._start_timeout_seconds = start_timeout_seconds
        self._start_backoff_max_seconds = start_backoff_max_seconds
        self._failure_budget = failure_budget
        self._lifecycle_retry_base_seconds = max(
            0.001, lifecycle_retry_base_seconds
        )
        if lifecycle_replay_capacity < 1:
            raise ValueError("lifecycle_replay_capacity must be positive")
        self._lifecycle_replay_capacity = lifecycle_replay_capacity
        self._records: dict[str, _Record] = {}
        self._stop_retry_processes: dict[
            str, tuple[ManagedHarnessProcess, ProcessIdentity | None]
        ] = {}
        self._stop_retry_bindings: dict[str, object] = {}
        self._restore_batches: dict[str, _RestoreBatch] = {}
        self._calls = _PendingCallRegistry()
        self._result_claim_capacity = 256
        self._delivery_reservations: dict[str, _DeliveryReservation] = {}
        self._claimed_results: dict[str, ClaimedHarnessResult] = {}
        self._result_reservation_snapshot: frozenset[str] = frozenset()
        self._claim_scan_id: str | None = None
        self._claim_scan_generations: frozenset[tuple[str, int]] = frozenset()
        self._retire_after_claim_scan: set[tuple[str, int]] = set()
        self._claim_waiters: list[tuple[str, int]] = []
        self._timers: dict[tuple[str, int], threading.Timer] = {}
        self._failed_events: list[str] = []
        # Readiness reports (phase ③): one per disposition that happened --
        # a settled start attempt, an already-running ensure, a deferred
        # restore target.  Deliberately the same drain shape as
        # `_failed_events` but NOT the same semantics: those announce a
        # state entry (failed, edge-triggered on the budget tripping),
        # these announce that a disposition occurred, so a restore round
        # leaves exactly one report per desired target and every later
        # settlement adds one more.  The daemon aggregates arrival and
        # verdict; it never interprets report content.
        self._readiness_reports: list[ReadinessReport] = []
        self._version = 0
        self._last_timer_sequence = 0
        self._settled_completion_ids: set[str] = set()
        self._settled_completion_order: deque[str] = deque()
        self._event_sinks: list[object] = []
        if event_sink is not None:
            self._event_sinks.append(event_sink)
        self._closing = False
        self._desired_state = desired_state
        self._automatic_restore_allowed = automatic_restore_allowed
        self._logger = orphan_logger
        self._restore_eligibility: dict[str, RestoreEligibilityProjection] = {}
        self._restore_eligibility_capacity = 10_000
        self._agent_identity = agent_identity
        self._lifecycle_pending: dict[str, tuple[object, object]] = {}
        self._lifecycle_retry_timers: dict[str, threading.Timer] = {}
        self._lifecycle_effect_resources: set[str] = (
            set()
            if desired_state is None
            else set(desired_state.incomplete_harness_lifecycle_resources())
        )
        self._lifecycle_effect_requests: dict[str, tuple[object, object]] = {}
        # Exact internal correlations whose native start/stop request crossed
        # into ProcessIoPort custody, mapped to their logical attempt token.
        # A later correlation under the same attempt must earn its own phase.
        self._lifecycle_native_admitted: dict[str, str] = {}
        # A lifecycle ensure can own multiple launcher calls when actor-level
        # retries overlap late I/O completions.  Keep every call fenced until
        # it reports and any process it created is stopped.
        self._lifecycle_ensure_io: dict[str, tuple[str, str, int]] = {}
        self._lifecycle_ensure_failure_fences: dict[
            str, _EnsureFailureFence
        ] = {}
        self._lifecycle_ensure_settling: set[str] = set()
        self._lifecycle_ensure_failure_stops: dict[
            str,
            tuple[
                str,
                str,
                int,
                ManagedHarnessProcess,
                ProcessIdentity | None,
            ],
        ] = {}
        # Terminal failures replay to re-admissions just like successful receipts.
        # Evicted with the existing bounded settled-attempt ledger.
        self._lifecycle_failures: dict[str, object] = {}
        self._settled_lifecycle_attempts: set[str] = set()
        self._settled_lifecycle_order: deque[str] = deque()
        self._lifecycle_replay_claims: set[str] = set()
        self._handler_generation = 0
        self._generation_lock = threading.Lock()
        self._handler_token = ""
        self._start_admission_width = max(1, start_max_workers)
        self._handle: ActorHandle | None = None
        self._io: ProcessIoPort | None = None
        self._persistence: DesiredStateIoPort | None = None
        self._owns_orphan_processes = orphan_processes is None
        self._orphan_processes = orphan_processes or OrphanProcessAuthority(
            orphan_state_path,
            identity_reader=identity_reader,
            logger=orphan_logger,
        )
        self._handle = self._runtime.start(
            ActorSpec(
                name="harness-runtime",
                handler_factory=self._new_generation_handler,
                mailbox_capacity=mailbox_capacity,
                supervision_profile="process_lifecycle",
                undelivered_sink=self._on_undelivered,
            )
        )
        if self._desired_state is not None:
            self._persistence = DesiredStateIoPort(
                self._desired_state,
                complete=lambda event: self._admit(event),
                late_result=persistence_late_result,
                capacity=mailbox_capacity,
            )
        self._result_effects: EffectLane[_ClaimScan, _ClaimScanOutcome] | None = None
        self._io = ProcessIoPort(
            launcher,
            emit=self._emit_completion,
            observe=self._admit,
            delivery_failed=self._fail_completion_delivery,
            generation_reader=self._read_generation,
            identity_reader=identity_reader,
            orphan_processes=self._orphan_processes,
            max_workers=start_max_workers,
        )
        self._publish()


    def _on_undelivered(self, command: object, reason_code: str) -> None:
        # ProcessIoPort and EffectLane retain the exact completion until the
        # replacement generation processes/acknowledges it.  Reporting a
        # second failure here would release or duplicate that custody.
        if isinstance(command, EffectCompleted):
            if isinstance(command.result, DesiredStateIoCompleted):
                persistence = self._persistence
                if persistence is not None:
                    persistence.redeliver(command.result.request)
            return
        if _is_io_completion(command):
            return
        correlation_id = str(getattr(command, "correlation_id", ""))
        if correlation_id:
            self._reject_correlation(
                correlation_id,
                reason_code,
                "accepted harness command did not begin before actor restart",
            )


    @property
    def projection(self) -> HarnessProjection:
        return self._projection


    def collect_orphans(self) -> int:
        return self._orphan_processes.collect_once()


    def orphan_status(self) -> tuple[dict[str, object], ...]:
        return self._orphan_processes.status()


    def restore_settlement_budget(self, target_count: int) -> float | None:
        """Wall-clock backstop for one restore round's settlement wait.

        Replaces the flat 45s wall-clock ceiling U7 removed (the constant
        whose name is now tripwired out of the tree).  The primary bound is
        structural, and the fix keeps it true: every desired target either
        settles immediately as deferred or is an admitted start whose
        settlement timer bounds it, so the batch cannot outlive
        ``ceil(targets / width)`` rounds of one start timeout each.  This
        method returns that bound plus one round of margin for admission
        and scheduler jitter -- a derived value that scales with the fleet
        instead of a constant that guessed at one.  Its only job is to
        convert a *lost settlement* (a bug, by definition -- no key without
        a disposition owner) into a ``TimeoutError`` on the caller's
        degraded-start path instead of a gate that never opens.

        ``None`` when start timers are disabled (start timeout 0): that is
        the operator's explicit opt-out of settlement deadlines, and no
        ceiling is invented for them -- the wait stays
        ``_AWAIT_SETTLEMENT``.
        """
        if self._start_timeout_seconds <= 0:
            return None
        rounds = restore_rounds(target_count, self._start_admission_width)
        return (rounds + 1) * self._start_timeout_seconds


    def read_harness(self, harness_id: str) -> HarnessStatusProjection | None:
        return self._projection.read_harness(harness_id)


    def read_harnesses(self) -> tuple[HarnessStatusProjection, ...]:
        return self._projection.read_harnesses()


    def read_streaming(self) -> HarnessStreamingProjection:
        return self._projection.read_streaming()


    def read_session_refs(self) -> HarnessSessionRefsProjection:
        return self._projection.read_session_refs()


    def read_worker_session_refs(self) -> HarnessSessionRefsProjection:
        return self._projection.read_worker_session_refs()


    def read_pending_result_delivery_ids(self) -> frozenset[str]:
        """Immutable reservation projection; native drain cannot reopen a row."""

        return self._result_reservation_snapshot


    @property
    def generation(self) -> int:
        return self._read_generation()


    @property
    def start_admission_width(self) -> int:
        """Concurrent restore starts, so callers can size their deadlines."""

        return self._start_admission_width


    @property
    def start_timeout(self) -> float:
        return self._start_timeout_seconds


    def _read_generation(self) -> int:
        with self._generation_lock:
            return self._handler_generation


    @property
    def version(self) -> int:
        return self._version


    def subscribe_events(self, sink: object) -> None:
        self._event_sinks.append(sink)


    def cancel_correlation(self, correlation_id: str) -> None:
        """Cancel only edge reply custody; domain state remains actor-owned."""

        self._calls.cancel(correlation_id)


    def submit(self, command: object) -> PortAdmission:
        with self._generation_lock:
            closing = self._closing and not isinstance(
                command,
                (StopHarnessesCommand, ClaimHarnessResultsCommand, SettleHarnessResultCommand),
            )
            admission = AdmissionResult.CLOSED if closing else self._admit(command)
        if closing:
            self._emit_event(
                PortCommandRejected(
                    correlation_id=command.correlation_id,
                    domain="harness",
                    generation=self._handler_generation,
                    version=self._version,
                    code="PORT_CLOSING",
                    detail="harness runtime is closing",
                    admission=PortAdmission.CLOSING,
                )
            )
            return PortAdmission.CLOSING
        if admission is AdmissionResult.ACCEPTED:
            return PortAdmission.ACCEPTED
        mapped = (
            PortAdmission.OVERLOADED
            if admission is AdmissionResult.OVERLOADED
            else PortAdmission.CLOSING
        )
        self._emit_event(
            PortCommandRejected(
                correlation_id=str(getattr(command, "correlation_id", "")),
                domain="harness",
                generation=self._handler_generation,
                version=self._version,
                code=(
                    "PORT_OVERLOADED"
                    if mapped is PortAdmission.OVERLOADED
                    else "PORT_CLOSING"
                ),
                detail=f"harness command admission is {mapped.value}",
                admission=mapped,
            )
        )
        return mapped


    def _admit(self, command: object) -> AdmissionResult:
        handle = self._handle
        if handle is None:
            return AdmissionResult.CLOSED
        return self._runtime.tell(handle, command)


    def receive(self, handler_generation: int, command: object) -> None:

        if handler_generation != self._handler_generation:
            return
        if (
            _is_io_completion(command)
            and command.correlation_id in self._settled_completion_ids
        ):
            receipt = command.processed_receipt
            if isinstance(receipt, _CompletionReceipt):
                receipt.mark_processed()
            return
        self._version += 1
        if self._receive_lifecycle_command(command):
            return
        if (
            self._closing
            and isinstance(command, get_args(HarnessCommand))
            and type(command) is not StopHarnessesCommand
        ):
            self._reject(command, "PORT_CLOSING", "harness runtime is closing")
            self._publish()
            return
        if self._dispatch_command(command):
            return
        self._publish()
        receipt = getattr(command, "processed_receipt", None)
        if isinstance(receipt, _CompletionReceipt):
            self._remember_settled_completion(
                str(getattr(command, "correlation_id", ""))
            )
            receipt.mark_processed()
    def _receive_lifecycle_command(self, command: object) -> bool:
        from hyprial.daemon.impl.lifecycle.lifecycle_receipts  import (
            FailHarnessLifecycleCommand,
            TerminalizeHarnessLifecycleCommand,
            ConfirmLifecycleReceiptCommand,
            RetireLifecycleReceiptCommand,
        )
        from hyprial.kernel import LifecycleMutationRequest

        if isinstance(command, RetireLifecycleReceiptCommand):
            self._persist_desired(
                DesiredStateOperation.RETIRE_LIFECYCLE_RECEIPT,
                command,
                ("harness", command.attempt_token, command.resource_token),
                context=("receipt", command, "retire"),
            )
            return True
        if isinstance(command, ConfirmLifecycleReceiptCommand):
            self._persist_desired(
                DesiredStateOperation.CONFIRM_LIFECYCLE_RECEIPT_RETIRED,
                command,
                ("harness", command.attempt_token, command.resource_token),
                context=("receipt", command, "confirm"),
            )
            return True
        if isinstance(command, TerminalizeHarnessLifecycleCommand):
            self._terminalize_incomplete_lifecycle(command)
            return True
        if isinstance(command, FailHarnessLifecycleCommand):
            with self._generation_lock:
                self._on_fail_lifecycle(command)
            self._publish()
            return True
        if isinstance(command, LifecycleMutationRequest):
            with self._generation_lock:
                durable_receipt = self._durable_lifecycle_receipt(
                    command.attempt_token
                )
                if durable_receipt is not None and durable_receipt.completed:
                    replay_claim = self._claim_lifecycle_replay(
                        command.attempt_token
                    )
                    if replay_claim == "wait":
                        return True
                    if replay_claim == "full":
                        self._emit_event(
                            PortCommandRejected(
                                correlation_id=command.correlation_id,
                                domain="harness",
                                generation=self._handler_generation,
                                version=self._version,
                                code="PORT_OVERLOADED",
                                detail="Harness lifecycle replay capacity is full",
                                admission=PortAdmission.OVERLOADED,
                            )
                        )
                        return True
                    self._replay_lifecycle_completion(command, durable_receipt)
                    return True
                if (
                    command.attempt_token in self._settled_lifecycle_attempts
                    and durable_receipt is None
                ):
                    failure = self._settled_lifecycle_failure(
                        command.attempt_token
                    )
                    if failure is not None:
                        self._emit_event(failure)
                    return True
                if self._closing:
                    self._remember_settled_lifecycle(command.attempt_token)
                    from hyprial.daemon.impl.lifecycle.lifecycle_receipts  import LifecycleMutationFailed

                    self._emit_event(
                        LifecycleMutationFailed(
                            command.correlation_id,
                            command.attempt_token,
                            self._handler_generation,
                            self._version,
                            "harness",
                            "HARNESS_RUNTIME_CLOSING",
                            "Harness runtime is closing before lifecycle admission",
                            True,
                        )
                    )
                    return True
                self._on_lifecycle(command)
            self._publish()
            return True
        return False


    def _dispatch_command(self, command: object) -> bool:
        from hyprial.daemon.impl.processes.process_owner  import ProcessFactsObserved

        if isinstance(command, RestoreHarnessesCommand):
            self._on_restore(command)
        elif isinstance(command, EnsureHarnessCommand):
            self._on_ensure(command)
        elif isinstance(command, RemoveHarnessCommand):
            self._restore_eligibility.pop(command.name, None)
            self._on_remove(command)
        elif isinstance(command, HarnessTimerElapsedCommand):
            self._on_reconcile(command)
        elif isinstance(command, UpdateRestoreEligibilityCommand):
            candidate = command.eligibility
            if not self._restore_entity_current(candidate):
                current = (
                    self._restore_eligibility[candidate.actor]
                    if candidate.actor in self._restore_eligibility
                    else None
                )
                if current is not None and not self._restore_entity_current(current):
                    self._restore_eligibility.pop(candidate.actor, None)
                    self._version += 1
                return True
            current = (
                self._restore_eligibility[candidate.actor]
                if candidate.actor in self._restore_eligibility
                else None
            )
            if current is None and len(self._restore_eligibility) >= (
                self._restore_eligibility_capacity
            ):
                return True
            if current is None or (
                candidate.source_generation,
                candidate.source_version,
            ) > (current.source_generation, current.source_version):
                self._restore_eligibility[candidate.actor] = candidate
                self._version += 1
        elif isinstance(command, ProcessFactsObserved):
            self._on_process_facts(command)
        elif isinstance(command, DrainHarnessFailedCommand):
            events = tuple(self._failed_events)
            self._failed_events.clear()
            self._emit_event(
                HarnessFailedDrained(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    events,
                )
            )
        elif isinstance(command, DrainHarnessReadinessCommand):
            reports = tuple(self._readiness_reports)
            self._readiness_reports.clear()
            self._emit_event(
                HarnessReadinessDrained(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    reports,
                )
            )
        elif isinstance(command, WaitHarnessReadyCommand):
            self._on_wait_ready(command)
        elif isinstance(command, DispatchHarnessDeliveryCommand):
            self._on_dispatch(command)
        elif isinstance(command, DrainHarnessResultsCommand):
            self._on_drain_results(command)
        elif isinstance(command, ClaimHarnessResultsCommand):
            self._on_claim_results(command)
        elif isinstance(command, SettleHarnessResultCommand):
            self._on_settle_result(command)
        elif isinstance(command, EffectCompleted) and isinstance(
            command.result, DesiredStateIoCompleted
        ):
            persistence = self._persistence
            try:
                self._on_desired_completed(command.result)
            except BaseException:
                if persistence is not None:
                    persistence.redeliver(command.result.request)
                raise
            if persistence is not None:
                persistence.acknowledge(command.result.request)
        elif isinstance(command, EffectCompleted):
            self._on_claim_scan_completed(command)
        elif isinstance(command, DrainHarnessProgressCommand):
            self._on_drain_progress(command)
        elif isinstance(command, BindHarnessLivenessCommand):
            self._on_bind_liveness(command)
        elif isinstance(command, StopHarnessesCommand):
            self._on_stop_all(command)
        elif isinstance(command, HarnessProcessStarted):
            self._on_start_completed(command)
        elif isinstance(command, HarnessStopIoCompleted):
            self._on_stop_completed(command)
        elif isinstance(command, HarnessCallIoCompleted):
            self._on_call_completed(command)
        elif isinstance(command, HarnessStartTimerElapsedCommand):
            self._on_start_timeout(command)
        elif isinstance(command, RefreshHarnessProjectionsCommand):
            self._publish()
            self._emit_event(
                HarnessProjectionsRefreshed(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                )
            )
        elif isinstance(command, StageHarnessDesiredCommand):
            self._on_manage_desired(command)
        elif isinstance(command, SnapshotAdapterRegistrationCommand):
            self._on_manage_desired(command)
        elif isinstance(command, RemoveAdapterRegistrationCommand):
            self._on_manage_desired(command)
        elif isinstance(command, RestoreAdapterRegistrationCommand):
            self._on_manage_desired(command)
        elif isinstance(command, ReconcileHarnessSessionRefsCommand):
            self._on_reconcile_session_refs(command)
        else:
            self._reject(command, ipc_errors.INVALID_ARGUMENT, "unsupported harness command")
        return False




    def _new_generation_handler(self) -> _HarnessActorGeneration:
        """Construct one fresh handler and fence every prior async callback.

        The stable coordinator retains desired/process authority and pending
        completion correlations.  I/O workers replay the exact same frozen
        completion until the replacement generation marks its processed
        receipt, so a mailbox admission lost with the old actor is not
        mistaken for completion.
        """

        with self._generation_lock:
            self._handler_generation += 1
            generation = self._handler_generation
        if generation > 1:
            self._version += 1
            self._publish()
        self._handler_token = uuid.uuid4().hex
        return _HarnessActorGeneration(self, generation, self._handler_token)


    def _publish(self) -> None:
        self._projection.publish(
            self._records,
            self._version,
            failure_budget=self._failure_budget,
        )


    @staticmethod
    def _key(spec: HarnessLaunchSpec) -> str:
        return f"{spec.harness}:{spec.name}"
