"""The harness actor port client and its lifecycle submit surface."""

from __future__ import annotations

from __future__ import annotations
from __future__ import annotations
import threading
import time
import uuid
from hyprial.kernel import (
    LifecycleMutationCompleted,
    LifecycleMutationRequest,
)
from hyprial.kernel import PortAdmission
from hyprial.kernel import ReadinessReport
from hyprial.daemon.impl.api import HarnessResult
from hyprial.kernel import AdmissionResult
from hyprial.daemon.impl.correlation.bounded_cadence  import BoundedCadence, CadenceCompleted
from hyprial.daemon.impl.lifecycle.lifecycle_receipts  import (
    LifecycleMutationFailed,
    ConfirmLifecycleReceiptCommand,
    LifecycleReceiptCompleted,
    RetireLifecycleReceiptCommand,
)
from hyprial.daemon.impl.harnesses.actor.actor import HarnessRuntimeActor
from hyprial.daemon.impl.harnesses.runtime.ports import (
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
    HarnessAdapterRegistrationProjection,
    HarnessDeliveryAdmitted,
    HarnessDesiredStateManaged,
    HarnessFailedDrained,
    HarnessLivenessBound,
    HarnessMutationCompleted,
    HarnessProgressObserved,
    HarnessProjectionsRefreshed,
    HarnessReadinessDrained,
    HarnessReadyObserved,
    HarnessRestoreCompleted,
    HarnessResultObserved,
    HarnessResultsClaimed,
    HarnessResultSettled,
    HarnessSessionRefsReconciled,
    HarnessTimerCompleted,
    HarnessTimerElapsedCommand,
    HarnessesStopped,
    RefreshHarnessProjectionsCommand,
    ReconcileHarnessSessionRefsCommand,
    RemoveAdapterRegistrationCommand,
    RemoveHarnessCommand,
    RestoreHarnessesCommand,
    RestoreEligibilityProjection,
    RestoreAdapterRegistrationCommand,
    UpdateRestoreEligibilityCommand,
    SnapshotAdapterRegistrationCommand,
    StageHarnessDesiredCommand,
    StopHarnessesCommand,
    WaitHarnessReadyCommand,
)
from hyprial.kernel import DesiredStateError
from hyprial.kernel import HarnessLaunchSpec

from .events import (
    CorrelatedDomainEvents,
    DomainCommandError,
    _EventT,
    _check_domain_receipt,
)
from .views import (
    harness_launch_projection,
)


class HarnessPortClient:
    """System-edge waits over the frozen Harness command/event seam."""

    def __init__(
        self,
        actor: HarnessRuntimeActor,
        events: CorrelatedDomainEvents,
        *,
        timeout: float = 65.0,
    ) -> None:
        self.actor = actor
        self._events = events
        self._timeout = timeout
        self._timer_sequence = 0
        self.drain_complete = True
        self._orphan_collection_lock = threading.Lock()
        self._orphan_collection: BoundedCadence[int] | None = None
        self._orphan_completed_sequence = 0
        self._orphan_reported_sequence = 0
        self._orphan_last_retired = 0
        self._orphan_last_error: str | None = None

    @property
    def generation(self) -> int:
        return self.actor.generation

    @property
    def version(self) -> int:
        return self.actor.version

    def call(
        self,
        command: object,
        expected: type[_EventT],
        *,
        timeout: float | None = None,
    ) -> _EventT:
        admission = self.actor.submit(command)  # type: ignore[arg-type]
        if admission is not PortAdmission.ACCEPTED:
            raise DomainCommandError(
                f"PORT_{admission.value.upper()}",
                f"harness command admission is {admission.value}",
            )
        return self._events.wait(
            str(getattr(command, "correlation_id")),
            expected,
            timeout=self._timeout if timeout is None else timeout,
        )

    def wait_lifecycle(
        self, correlation_id: str, expected: type[_EventT]
    ) -> _EventT:
        return self._events.wait(correlation_id, expected, timeout=self._timeout)

    def retire_lifecycle_receipt(
        self, attempt_token: str, resource_token: str
    ) -> bool:
        generation = self.actor.generation
        event = self.call(
            RetireLifecycleReceiptCommand(
                f"harness:receipt:retire:{uuid.uuid4().hex}",
                attempt_token, resource_token,
            ),
            LifecycleReceiptCompleted,
            timeout=5.0,
        )
        _check_domain_receipt(
            event, "harness", generation, attempt_token, resource_token, "retire"
        )
        return event.matched

    def confirm_lifecycle_receipt_retired(
        self, attempt_token: str, resource_token: str
    ) -> None:
        generation = self.actor.generation
        event = self.call(
            ConfirmLifecycleReceiptCommand(
                f"harness:receipt:confirm:{uuid.uuid4().hex}",
                attempt_token, resource_token,
            ),
            LifecycleReceiptCompleted,
            timeout=5.0,
        )
        _check_domain_receipt(
            event, "harness", generation, attempt_token, resource_token, "confirm"
        )

    def submit_lifecycle(self, command: object) -> PortAdmission:
        return self.actor.submit(command)

    def fail_lifecycle(
        self, request: LifecycleMutationRequest, code: str, detail: str, timeout: float,
    ) -> LifecycleMutationCompleted | LifecycleMutationFailed:
        from hyprial.daemon.impl.lifecycle.lifecycle_receipts import (
            FailHarnessLifecycleCommand, HarnessLifecycleFailureSettled,
        )

        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            # Each admission has its own edge correlation: an OVERLOADED
            # rejection must not poison the subsequent accepted reply.
            command = FailHarnessLifecycleCommand(
                f"harness:fail:{uuid.uuid4().hex}", request, code, detail,
            )
            admission = self.actor.submit(command)
            remaining = deadline - time.monotonic()
            if admission is PortAdmission.ACCEPTED:
                try:
                    return self._events.wait(
                        command.correlation_id, HarnessLifecycleFailureSettled,
                        timeout=max(0.0, remaining),
                    ).result
                except DomainCommandError as error:
                    raise TimeoutError(f"harness failure settlement unresolved: {error}") from error
            if admission is not PortAdmission.OVERLOADED or remaining <= 0:
                raise TimeoutError(
                    f"harness failure settlement admission is {admission.value}"
                )
            time.sleep(min(0.002, remaining))

    def restore(self, desired: tuple[HarnessLaunchSpec, ...]) -> object:
        return self.call(
            RestoreHarnessesCommand(
                f"harness:restore:{uuid.uuid4().hex}",
                tuple(harness_launch_projection(item) for item in desired),
            ),
            HarnessRestoreCompleted,
        ).result

    def start(self, spec: HarnessLaunchSpec) -> bool:
        return self.call(
            EnsureHarnessCommand(
                f"harness:start:{uuid.uuid4().hex}", harness_launch_projection(spec)
            ),
            HarnessMutationCompleted,
        ).changed

    def remove(self, harness: str, name: str) -> bool:
        return self.call(
            RemoveHarnessCommand(
                f"harness:remove:{uuid.uuid4().hex}", harness, name
            ),
            HarnessMutationCompleted,
        ).changed

    def reconcile(self) -> int:
        self._timer_sequence += 1
        return self.call(
            HarnessTimerElapsedCommand(
                f"harness:timer:{uuid.uuid4().hex}",
                self.generation,
                self._timer_sequence,
                time.time_ns() // 1_000_000,
            ),
            HarnessTimerCompleted,
        ).restarted

    def submit_restore_eligibility(
        self, eligibility: RestoreEligibilityProjection
    ) -> PortAdmission:
        return self.actor.submit(
            UpdateRestoreEligibilityCommand(
                f"harness:restore-eligibility:{uuid.uuid4().hex}", eligibility
            )
        )

    def drain_failed_events(self) -> tuple[str, ...]:
        return self.call(
            DrainHarnessFailedCommand(
                f"harness:failed:{uuid.uuid4().hex}"
            ),
            HarnessFailedDrained,
        ).harness_ids

    def drain_readiness_reports(self) -> tuple[ReadinessReport, ...]:
        return self.call(
            DrainHarnessReadinessCommand(
                f"harness:readiness:{uuid.uuid4().hex}"
            ),
            HarnessReadinessDrained,
        ).reports

    def status(self) -> tuple[dict[str, object], ...]:
        return tuple(item.to_payload() for item in self.actor.read_harnesses())

    def collect_orphans(self) -> int:
        """Admit a bounded probe and report only newly completed retirements."""

        with self._orphan_collection_lock:
            lane = self._orphan_collection
            if lane is None:
                lane = BoundedCadence(
                    "harness-orphan-collection",
                    lambda _at_ms: self.actor.collect_orphans(),
                    self._orphan_collection_completed,
                )
                self._orphan_collection = lane
            if self._orphan_completed_sequence != self._orphan_reported_sequence:
                retired = self._orphan_last_retired
                self._orphan_reported_sequence = self._orphan_completed_sequence
            else:
                retired = 0
        admitted = lane.submit(time.time_ns() // 1_000_000)
        if admitted is AdmissionResult.CLOSED:
            raise DomainCommandError("ORPHAN_COLLECTION_CLOSED", "orphan collection closed")
        return retired

    def _orphan_collection_completed(self, completion: CadenceCompleted[int]) -> None:
        with self._orphan_collection_lock:
            self._orphan_completed_sequence += 1
            self._orphan_last_retired = completion.result or 0
            self._orphan_last_error = (
                None if completion.error is None else str(completion.error)[:500]
            )

    def orphan_collection_status(self) -> dict[str, object]:
        with self._orphan_collection_lock:
            lane = self._orphan_collection
            error = self._orphan_last_error
        if lane is None:
            return {"accepted": 0, "overloaded": 0, "completed": 0, "failed": 0}
        projection = lane.projection()
        return {
            "accepted": projection.accepted,
            "overloaded": projection.overloaded,
            "coalesced": projection.coalesced,
            "completed": projection.completed,
            "failed": projection.failed,
            **({"lastError": error} if error is not None else {}),
        }

    def orphan_status(self) -> tuple[dict[str, object], ...]:
        return self.actor.orphan_status()

    def streaming_actors(self) -> tuple[str, ...]:
        return self.actor.read_streaming().actors

    def streaming_generations(self) -> dict[str, int]:
        return dict(self.actor.read_streaming().process_generations)

    def session_refs(self) -> dict[tuple[str, str], str]:
        self.call(
            RefreshHarnessProjectionsCommand(
                f"harness:refresh:{uuid.uuid4().hex}"
            ),
            HarnessProjectionsRefreshed,
        )
        return {
            (item.harness, item.name): item.session_ref
            for item in self.actor.read_session_refs().refs
        }

    def projected_session_refs(self) -> dict[tuple[str, str], str]:
        """Immutable projection read; never refreshes or waits on the actor."""

        return {
            (item.harness, item.name): item.session_ref
            for item in self.actor.read_session_refs().refs
        }

    def reconcile_session_refs(self) -> dict[tuple[str, str], str]:
        event = self.call(
            ReconcileHarnessSessionRefsCommand(
                f"harness:session-refs:{uuid.uuid4().hex}"
            ),
            HarnessSessionRefsReconciled,
        )
        if event.error is not None:
            raise DesiredStateError(event.error)
        return {
            (item.harness, item.name): item.session_ref for item in event.refs
        }

    def stage_harness_desired(self, spec: HarnessLaunchSpec) -> bool:
        event = self.call(
            StageHarnessDesiredCommand(
                f"harness:stage:{uuid.uuid4().hex}", harness_launch_projection(spec)
            ),
            HarnessDesiredStateManaged,
        )
        if event.error is not None:
            raise event.error
        return event.changed

    def snapshot_adapter_registration(
        self, name: str
    ) -> HarnessAdapterRegistrationProjection:
        event = self.call(
            SnapshotAdapterRegistrationCommand(
                f"harness:adapter-snapshot:{uuid.uuid4().hex}", name
            ),
            HarnessDesiredStateManaged,
        )
        if event.error is not None:
            raise event.error
        assert event.adapter is not None
        return event.adapter

    def remove_adapter_registration(
        self, snapshot: HarnessAdapterRegistrationProjection
    ) -> bool:
        event = self.call(
            RemoveAdapterRegistrationCommand(
                f"harness:adapter-remove:{uuid.uuid4().hex}",
                snapshot.name,
                snapshot.spec,
                snapshot.legacy_pin,
            ),
            HarnessDesiredStateManaged,
        )
        if event.error is not None:
            raise event.error
        return event.changed

    def restore_adapter_registration(
        self, snapshot: HarnessAdapterRegistrationProjection
    ) -> bool:
        event = self.call(
            RestoreAdapterRegistrationCommand(
                f"harness:adapter-restore:{uuid.uuid4().hex}",
                snapshot.name,
                snapshot.spec,
                snapshot.legacy_pin,
            ),
            HarnessDesiredStateManaged,
        )
        if event.error is not None:
            raise event.error
        return event.changed

    def projected_worker_session_refs(self) -> dict[tuple[str, str], str]:
        """Worker MCP session fences, distinct from native model sessions."""

        return {
            (item.harness, item.name): item.session_ref
            for item in self.actor.read_worker_session_refs().refs
        }

    def projected_worker_process_identities(
        self,
    ) -> dict[tuple[str, str], tuple[int, str]]:
        """Live worker PID plus birth marker, kept off diagnostic payloads."""

        return {
            (item.runtime, item.name): (item.pid, item.identity_marker)
            for item in self.actor.read_harnesses()
            if item.running
            and item.pid is not None
            and item.identity_marker is not None
        }

    def wait_ready(self, harness: str, name: str, timeout: float) -> bool:
        return self.call(
            WaitHarnessReadyCommand(
                f"harness:ready:{uuid.uuid4().hex}", harness, name, timeout
            ),
            HarnessReadyObserved,
            timeout=max(timeout + 1.0, self._timeout),
        ).ready

    def dispatch(self, name: str, delivery: object) -> bool:
        return self.call(
            DispatchHarnessDeliveryCommand(
                f"harness:dispatch:{uuid.uuid4().hex}", name, delivery
            ),
            HarnessDeliveryAdmitted,
        ).accepted

    def drain_results(self) -> tuple[HarnessResult, ...]:
        return self.call(
            DrainHarnessResultsCommand(f"harness:results:{uuid.uuid4().hex}"),
            HarnessResultObserved,
        ).results

    def claim_results(self, limit: int = 64) -> tuple[ClaimedHarnessResult, ...]:
        return self.claim_result_batch(limit).claims

    def claim_result_batch(self, limit: int = 64) -> HarnessResultsClaimed:
        return self.call(
            ClaimHarnessResultsCommand(
                f"harness:claim-results:{uuid.uuid4().hex}", limit
            ),
            HarnessResultsClaimed,
        )

    def settle_result(self, claim_token: str, delivery_id: str) -> bool:
        return self.call(
            SettleHarnessResultCommand(
                f"harness:settle-result:{uuid.uuid4().hex}",
                claim_token, delivery_id,
            ),
            HarnessResultSettled,
        ).settled

    def pending_result_delivery_ids(self) -> frozenset[str]:
        return self.actor.read_pending_result_delivery_ids()

    def drain_progress(self) -> tuple[object, ...]:
        return self.call(
            DrainHarnessProgressCommand(f"harness:progress:{uuid.uuid4().hex}"),
            HarnessProgressObserved,
        ).progress

    def bind_liveness(self, harness: str, name: str, binding: object) -> bool:
        return self.call(
            BindHarnessLivenessCommand(
                f"harness:liveness:{uuid.uuid4().hex}", harness, name, binding
            ),
            HarnessLivenessBound,
        ).changed

    def stop(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + max(0.0, timeout)
        lane = self._orphan_collection
        if lane is not None and not lane.close(max(0.0, deadline - time.monotonic())):
            self.drain_complete = False
            raise RuntimeError("orphan collection did not drain before harness teardown")
        try:
            command_complete = self.call(
                StopHarnessesCommand(
                    f"harness:stop:{uuid.uuid4().hex}", int(deadline * 1000)
                ),
                HarnessesStopped,
                timeout=timeout,
            ).drain_complete
        except BaseException:
            self.drain_complete = False
            raise
        if not command_complete:
            self.drain_complete = False
            return
        runtime_complete = self.actor.close_runtime(
            max(0.0, deadline - time.monotonic())
        )
        self.drain_complete = runtime_complete
