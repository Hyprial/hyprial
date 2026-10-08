"""Start attempts, failure/fence and completion-publication behaviour."""

from __future__ import annotations

import threading
import uuid
from hyprial.kernel import AdmissionResult
from hyprial.kernel import capped_exponential
from hyprial.kernel  import (
    ManagedHarnessProcess,
)
from hyprial.daemon.impl.desired_state_io  import (
    DesiredStateIoRequest,
    DesiredStateOperation,
)
from hyprial.daemon.impl.harnesses.runtime.ports  import (
    EnsureHarnessCommand,
    HarnessMutationCompleted,
    HarnessRestoreCompleted,
    HarnessRestoreProjection,
    HarnessStartTimerElapsedCommand,
    RemoveHarnessCommand,
)
from hyprial.kernel import ipc_errors
from hyprial.kernel import PortCommandRejected
from hyprial.kernel import ReadinessReport

from ..contracts import ProcessIdentity, _EnsureFailureFence, _LIFECYCLE_SETTLED_CAPACITY, _PendingCall, _Record

class _ProcessEventsBehavior:
    """Start attempts, failure/fence and completion-publication behaviour."""


    def _begin_start(
        self,
        key: str,
        record: _Record,
        kind: str,
        *,
        correlation_id: str,
        replace: tuple[ManagedHarnessProcess, ProcessIdentity | None] | None = None,
        allow_lifecycle: bool = False,
    ) -> None:
        if key in self._lifecycle_effect_resources and not allow_lifecycle:
            # A lifecycle effect claimed the resource while this key sat in
            # the batch queue (admission drains one settlement at a time),
            # or between the restore command and this call.  F1 door 2: a
            # silent return here used to strand the key in ``batch.pending``
            # forever -- no start, no report, no batch completion, gate
            # never opening.  The claim's own saga starts the connector
            # through the lifecycle path, so this round hands the key over:
            # settle it as deferred.
            batch_id = record.restore_batch
            if batch_id is not None:
                record.restore_batch = None
                self._settle_deferred_restore_target(key, batch_id)
            return
        record.generation += 1
        record.starting = True
        record.attempt_kind = kind
        record.process = None
        record.identity = None
        record.attempt_correlation_id = correlation_id
        generation = record.generation
        scheduled_version = self._version
        # Publish ownership/starting before a fast or blocked I/O worker can
        # report completion; projection readers never observe a ghost gap.
        self._publish()
        assert self._io is not None
        self._io.start(
            correlation_id,
            key,
            generation,
            self._version,
            record.spec,
            replace=replace,
        )
        if allow_lifecycle:
            self._mark_lifecycle_native_admitted(correlation_id)
        if self._start_timeout_seconds > 0:
            timer = threading.Timer(
                self._start_timeout_seconds,
                lambda: self._emit_completion(
                    HarnessStartTimerElapsedCommand(
                        correlation_id,
                        generation,
                        scheduled_version,
                        key,
                    )
                ),
            )
            timer.daemon = True
            self._timers[(key, generation)] = timer
            timer.start()


    def _is_failed(self, record: _Record) -> bool:
        """Derived, never stored: the failure budget is spent, retries stop.

        Budget 0 disables failure termination entirely (retries continue
        forever under backoff), which is why the check is not just a
        comparison.  Unlike the quarantine this replaced, failed does NOT
        lift itself: there is no cooldown and no probe start, so this state
        is where the harness stays until an explicit start.  The one
        exception to "budget decides" is ``restore_failed_terminal``: a
        restore-kind start gets a single attempt by ruling, even at
        budget 0.
        """

        return record.restore_failed_terminal or (
            self._failure_budget > 0
            and record.failures >= self._failure_budget
        )


    def _report_readiness(self, key: str, verdict: str) -> None:
        """Record one disposition report for the daemon's phase-③ drain.

        Emitted exactly where a disposition happens, never on a schedule:
        the readiness boundary is "the connector actor was established and
        handed back its first report", so each settled start attempt (or an
        ensure that found the connector already running, or a restore target
        admission could not take this round) appends exactly one report.
        """

        self._readiness_reports.append(
            ReadinessReport(phase="connector-up", verdict=verdict, source=key)
        )


    def _record_failure(
        self,
        key: str,
        record: _Record,
        detail: str,
        *,
        permanent: bool = False,
    ) -> None:
        record.last_error = detail
        was_failed = self._is_failed(record)
        record.failures += 1
        if permanent:
            record.restore_failed_terminal = True
        if self._is_failed(record):
            record.restart_after = None
            if not was_failed and (
                record.restore_batch is None or self._desired_state is None
            ):
                self._failed_events.append(key)
                # U0b: the failure budget just ran out.  If this harness
                # previously came up (its desired-state row exists), the row
                # must now read "ran before, did not come back" so a daemon
                # restart displays it and refuses to auto-retry it.  No row
                # (an explicit first start that never succeeded) leaves no
                # trace -- mark_harness_failed is a no-op then.  Persistence
                # is best-effort for reconcile: the budget verdict itself
                # must not be hostage to a status write. Restore keeps its
                # own exact write and batch correlation below.
                if self._desired_state is not None and record.restore_batch is None:
                    harness, name = key.split(":", 1)
                    self._persist_desired(
                        DesiredStateOperation.MARK_HARNESS_FAILED,
                        (harness, name),
                        (harness, name),
                        context=("mark_failed", harness, name),
                    )
            return
        base = self._restart_backoff_seconds
        delay = (
            min(
                capped_exponential(
                    base,
                    self._start_backoff_max_seconds,
                    max(0, record.failures - 1),
                ),
                self._start_backoff_max_seconds,
            )
            if base > 0
            else 0.0
        )
        record.restart_after = self._clock() + delay


    def _finish_start_attempt(
        self,
        key: str,
        record: _Record,
        success: bool,
        error: BaseException | None,
    ) -> None:
        # One settled start attempt = one disposition = one report. A failed
        # restore delays that report until its status write commits. On the
        # failure path `_record_failure` has already run, so the verdict
        # reads the post-settlement budget state.
        restore_single_attempt_failed = (
            not success and record.restore_batch is not None
        )
        if restore_single_attempt_failed:
            # U0b (Allen 2026-09-03): a restore-kind start gets exactly one
            # attempt, so its failure verdict is terminal BEFORE the report
            # reads it.  The row being restored had status "running" -- it
            # ran before -- so a failed bring-back is "previously up, did
            # not come back": persist failed (displayed, human-owned) and
            # keep the reconcile loop from retrying it.  Budget 0 is the
            # documented infinite-retry opt-in for in-generation reconcile;
            # it does not buy back this ruling, so the record is made
            # terminal regardless.
            was_failed = self._is_failed(record)
            record.restart_after = None
            record.failures = max(
                record.failures,
                self._failure_budget if self._failure_budget > 0 else 1,
            )
            if self._failure_budget <= 0:
                # _is_failed refuses to terminate at budget 0 by contract;
                # pin the verdict through the dedicated flag instead.
                record.restore_failed_terminal = True
            if (
                not was_failed
                and self._is_failed(record)
                and (record.restore_batch is None or self._desired_state is None)
            ):
                # Same alarm as the budget trip: entering failed must be
                # observable (watchdog/readiness drain these events).
                self._failed_events.append(key)
        batch_id = record.restore_batch
        if restore_single_attempt_failed and self._desired_state is not None:
            # The failed restore disposition is not durable until the exact
            # accepted desired-state operation reports its committed snapshot.
            # Keep the batch key pending; process start admission is separate.
            record.restore_batch = None
            assert batch_id is not None
            self._persist_restore_failure(
                batch_id, key, record.incarnation, record.generation
            )
            return
        self._report_readiness(
            key,
            "ready"
            if success
            else ("failed" if self._is_failed(record) else "retrying"),
        )
        if record.explicit_correlation_id is not None:
            correlation_id = record.explicit_correlation_id
            record.explicit_correlation_id = None
            if success:
                self._emit_event(
                    HarnessMutationCompleted(
                        correlation_id,
                        self._handler_generation,
                        self._version,
                        key,
                        True,
                    )
                )
            else:
                failure = error or RuntimeError(record.last_error or "start failed")
                failure_code = getattr(failure, "code", None)
                self._reject_correlation(
                    correlation_id,
                    (
                        "HARNESS_START_TIMEOUT"
                        if isinstance(failure, TimeoutError)
                        else (
                            failure_code
                            if isinstance(failure_code, str) and failure_code
                            else "HARNESS_START_FAILED"
                        )
                    ),
                    str(failure),
                )
        record.restore_batch = None
        if batch_id is None:
            return
        batch = self._restore_batches.get(batch_id)
        if batch is None or key not in batch.pending:
            return
        self._settle_restore_key(batch_id, key, success=success)


    def _settle_restore_key(
        self, batch_id: str, key: str, *, success: bool | None
    ) -> None:
        batch = self._restore_batches.get(batch_id)
        if batch is None or key not in batch.pending:
            return
        batch.pending.remove(key)
        if success:
            batch.restored += 1
        elif success is False:
            batch.failed += 1
        # One start settled, so one admission slot is free.  Restore now
        # drains at the pace starts actually complete instead of dumping the
        # whole fleet at a port that can only hold part of it.
        self._admit_restore_starts(batch_id)
        # Admission can synchronously defer the final queued target and
        # consume this batch. Only the still-owning path publishes its result.
        if not batch.pending and self._restore_batches.get(batch_id) is batch:
            self._restore_batches.pop(batch_id, None)
            if not batch.terminal_rejected:
                self._emit_event(
                    HarnessRestoreCompleted(
                        batch.correlation_id,
                        self._handler_generation,
                        self._version,
                        HarnessRestoreProjection(
                            batch.attempted,
                            batch.restored,
                            batch.failed,
                            batch.deferred,
                        ),
                    )
                )


    def _persist_restore_failure(
        self, batch_id: str, key: str, record_incarnation: str,
        record_generation: int,
    ) -> None:
        batch = self._restore_batches.get(batch_id)
        if batch is None or key not in batch.pending:
            return
        persistence = self._persistence
        if persistence is None:
            self._fail_restore_persistence(
                batch_id, key, "HARNESS_PERSISTENCE_UNAVAILABLE",
                "Harness desired-state persistence is unavailable",
            )
            return
        harness, name = key.split(":", 1)
        request = DesiredStateIoRequest(
            operation_id=f"harness-state-{uuid.uuid4().hex}",
            owner_generation=self._handler_generation,
            owner_version=self._version,
            operation=DesiredStateOperation.MARK_HARNESS_FAILED,
            args=(harness, name),
            context=(
                "restore_mark_failed", batch_id, key,
                record_incarnation, record_generation,
            ),
        )
        admission = persistence.submit(request)
        if admission is not AdmissionResult.ACCEPTED:
            self._fail_restore_persistence(
                batch_id, key, "HARNESS_PERSISTENCE_OVERLOADED",
                f"Harness persistence admission is {admission.value}",
            )


    def _fail_restore_persistence(
        self, batch_id: str, key: str, code: str, detail: str
    ) -> None:
        batch = self._restore_batches.get(batch_id)
        if batch is None or key not in batch.pending:
            return
        if not batch.terminal_rejected:
            batch.terminal_rejected = True
            self._reject_correlation(batch.correlation_id, code, detail)
        self._settle_restore_key(batch_id, key, success=None)


    def _register_call(
        self,
        correlation: str,
        generation: int,
        *,
        operation: str,
        harness_id: str | None = None,
        delivery_id: str | None = None,
        subject_id: str | None = None,
        reply_correlation_id: str | None = None,
    ) -> str:
        self._calls.register(
            correlation,
            _PendingCall(
                expected_generation=generation,
                operation=operation,
                harness_id=harness_id,
                delivery_id=delivery_id,
                subject_id=subject_id,
                reply_correlation_id=reply_correlation_id,
            ),
        )
        return correlation


    def _mark_lifecycle_native_admitted(self, correlation_id: str) -> None:
        pending = self._lifecycle_pending.get(correlation_id)
        if pending is None:
            return
        attempt_token = getattr(pending[0], "attempt_token", None)
        if isinstance(attempt_token, str) and attempt_token:
            self._lifecycle_native_admitted[correlation_id] = attempt_token


    def _pop_lifecycle_pending(self, correlation_id: str) -> tuple[object, object] | None:
        self._lifecycle_native_admitted.pop(correlation_id, None)
        return self._lifecycle_pending.pop(correlation_id, None)


    def _cancel_start_timer(self, key: str, generation: int) -> None:
        timer = self._timers.pop((key, generation), None)
        if timer is not None:
            timer.cancel()


    def _emit_completion(self, completion: object) -> tuple[AdmissionResult, int]:
        from hyprial.kernel import LifecycleMutationRequest

        with self._generation_lock:
            generation = self._handler_generation
            admission = (
                AdmissionResult.CLOSED
                if self._closing
                and isinstance(completion, LifecycleMutationRequest)
                else self._admit(completion)
            )
        return admission, generation


    def _fail_completion_delivery(
        self, completion: object, error: BaseException
    ) -> None:
        correlation_id = str(getattr(completion, "correlation_id", ""))
        if correlation_id:
            pending = self._calls.pop(correlation_id)
            if pending is not None and pending.reply_correlation_id is not None:
                correlation_id = pending.reply_correlation_id
            self._reject_correlation(
                correlation_id,
                "HARNESS_COMPLETION_HANDOFF_FAILED",
                str(error),
            )


    def _terminate_pending(self, code: str, detail: str) -> None:
        for correlation_id, pending in self._calls.clear():
            self._reject_correlation(
                pending.reply_correlation_id or correlation_id, code, detail
            )
        for batch in tuple(self._restore_batches.values()):
            if not batch.terminal_rejected:
                self._reject_correlation(batch.correlation_id, code, detail)
        self._restore_batches.clear()
        for record in self._records.values():
            if record.explicit_correlation_id is not None:
                self._reject_correlation(
                    record.explicit_correlation_id, code, detail
                )
                record.explicit_correlation_id = None
            record.restore_batch = None


    def _remember_settled_completion(self, correlation_id: str) -> None:
        if not correlation_id or correlation_id in self._settled_completion_ids:
            return
        if len(self._settled_completion_order) >= 1024:
            expired = self._settled_completion_order.popleft()
            self._settled_completion_ids.discard(expired)
        self._settled_completion_order.append(correlation_id)
        self._settled_completion_ids.add(correlation_id)


    def _durable_lifecycle_receipt(self, attempt_token: str) -> object | None:
        if self._desired_state is None:
            return None
        return next(
            (
                receipt
                for receipt in self._desired_state.load().lifecycle_receipts
                if receipt.domain == "harness"
                and receipt.attempt_token == attempt_token
            ),
            None,
        )


    def _settled_lifecycle_failure(self, attempt_token: str) -> object | None:
        return self._lifecycle_failures.get(attempt_token)


    def _remember_settled_lifecycle(self, attempt_token: str) -> None:
        """Keep only the short duplicate window; durable state owns recovery."""

        for correlation_id, owner in tuple(
            self._lifecycle_native_admitted.items()
        ):
            if owner == attempt_token:
                self._lifecycle_native_admitted.pop(correlation_id, None)
        if not attempt_token or attempt_token in self._settled_lifecycle_attempts:
            return
        if len(self._settled_lifecycle_order) >= _LIFECYCLE_SETTLED_CAPACITY:
            expired = self._settled_lifecycle_order.popleft()
            self._settled_lifecycle_attempts.discard(expired)
            self._lifecycle_failures.pop(expired, None)
        self._settled_lifecycle_order.append(attempt_token)
        self._settled_lifecycle_attempts.add(attempt_token)


    def _claim_lifecycle_replay(self, attempt_token: str) -> str:
        if attempt_token in self._lifecycle_replay_claims:
            return "wait"
        if len(self._lifecycle_replay_claims) >= self._lifecycle_replay_capacity:
            return "full"
        self._lifecycle_replay_claims.add(attempt_token)
        return "new"


    def release_lifecycle_replay(self, attempt_token: str) -> None:
        self._lifecycle_replay_claims.discard(attempt_token)


    def _reject(self, command: object, code: str, detail: str) -> None:
        self._reject_correlation(
            str(getattr(command, "correlation_id", "")), code, detail
        )


    def _reject_correlation(self, correlation_id: str, code: str, detail: str) -> None:
        self._emit_event(
            PortCommandRejected(
                correlation_id=correlation_id,
                domain="harness",
                generation=self._handler_generation,
                version=self._version,
                code=code,
                detail=detail,
            )
        )


    def _finish_failed_removal(
        self,
        request: object,
        provenance: object,
        code: str,
        detail: str,
        owns_resource: bool,
    ) -> object:
        from hyprial.daemon.impl.lifecycle.lifecycle_receipts  import LifecycleMutationFailed
        from hyprial.kernel  import MutationProvenance
        from hyprial.kernel import LifecycleMutationRequest

        assert isinstance(request, LifecycleMutationRequest)
        assert isinstance(provenance, MutationProvenance)
        assert isinstance(request.payload, RemoveHarnessCommand)
        key = f"{request.payload.harness}:{request.payload.name}"
        active = self._lifecycle_effect_requests.get(key)
        owns_custody = active is None or active[0].attempt_token == request.attempt_token
        # Durable intent first, then memory, with no actor handoff in between.
        # Neither decision depends on a pid or on whether stop returned.
        if owns_resource and owns_custody:
            self._records.pop(key, None)
        if owns_custody:
            self._lifecycle_effect_resources.discard(key)
            self._lifecycle_effect_requests.pop(key, None)
        for correlation, (pending, _) in tuple(self._lifecycle_pending.items()):
            if pending.attempt_token == request.attempt_token:
                self._pop_lifecycle_pending(correlation)
                self._calls.pop(correlation)
        for correlation, timer in tuple(self._lifecycle_retry_timers.items()):
            if correlation.startswith(f"{request.correlation_id}:io:"):
                timer.cancel()
                self._lifecycle_retry_timers.pop(correlation)
        self._remember_settled_lifecycle(request.attempt_token)
        failure = LifecycleMutationFailed(
            request.correlation_id, request.attempt_token,
            self._handler_generation, self._version, "harness", code, detail, False,
        )
        self._lifecycle_failures[request.attempt_token] = failure
        self._publish()
        return failure


    def _begin_failed_removal(
        self,
        request: object,
        provenance: object,
        code: str,
        detail: str,
        *,
        control_correlation: str | None = None,
    ) -> None:
        from hyprial.kernel  import MutationProvenance
        from hyprial.kernel import LifecycleMutationRequest

        assert isinstance(request, LifecycleMutationRequest)
        assert isinstance(provenance, MutationProvenance)
        self._persist_desired(
            DesiredStateOperation.FAIL_HARNESS_REMOVAL,
            request,
            (request.attempt_token, provenance.resource_token),
            context=(
                "failed_removal",
                request,
                provenance,
                code,
                detail,
                control_correlation,
            ),
        )


    def _finish_failed_ensure(
        self,
        request: object,
        provenance: object,
        code: str,
        detail: str,
        rolled_back: bool,
    ) -> object:
        from hyprial.daemon.impl.lifecycle.lifecycle_receipts  import (
            LifecycleMutationFailed,
            )
        from hyprial.kernel  import (
            MutationProvenance,
        )
        from hyprial.kernel import LifecycleMutationRequest

        assert isinstance(request, LifecycleMutationRequest)
        assert isinstance(provenance, MutationProvenance)
        assert isinstance(request.payload, EnsureHarnessCommand)
        key = f"{request.payload.spec.harness}:{request.payload.spec.name}"
        active = self._lifecycle_effect_requests.get(key)
        owns_custody = (
            active is None or active[0].attempt_token == request.attempt_token
        )
        if owns_custody:
            self._records.pop(key, None)
            self._lifecycle_effect_resources.discard(key)
            self._lifecycle_effect_requests.pop(key, None)
        for correlation, (pending, _) in tuple(self._lifecycle_pending.items()):
            if pending.attempt_token == request.attempt_token:
                self._pop_lifecycle_pending(correlation)
                self._calls.pop(correlation)
        for correlation, timer in tuple(self._lifecycle_retry_timers.items()):
            if correlation.startswith(f"{request.correlation_id}:io:"):
                timer.cancel()
                self._lifecycle_retry_timers.pop(correlation)
        self._remember_settled_lifecycle(request.attempt_token)
        failure = LifecycleMutationFailed(
            request.correlation_id,
            request.attempt_token,
            self._handler_generation,
            self._version,
            "harness",
            code,
            detail,
            rolled_back,
        )
        self._lifecycle_failures[request.attempt_token] = failure
        self._publish()
        return failure


    def _schedule_ensure_failure_stop(
        self,
        attempt_token: str,
        harness_id: str,
        generation: int,
        process: ManagedHarnessProcess,
        identity: ProcessIdentity | None,
    ) -> None:
        correlation = f"harness:fail-stop:{uuid.uuid4().hex}"
        self._lifecycle_ensure_failure_stops[correlation] = (
            attempt_token,
            harness_id,
            generation,
            process,
            identity,
        )
        assert self._io is not None
        self._io.stop(
            correlation,
            harness_id,
            generation,
            self._version,
            process,
            identity,
        )


    def _drive_ensure_failure_fence(self, attempt_token: str) -> None:
        fence = self._lifecycle_ensure_failure_fences.get(attempt_token)
        if fence is None:
            return
        from hyprial.kernel import LifecycleMutationRequest

        request = fence.request
        assert isinstance(request, LifecycleMutationRequest)
        for correlation, timer in tuple(self._lifecycle_retry_timers.items()):
            if correlation.startswith(f"{request.correlation_id}:io:"):
                timer.cancel()
                self._lifecycle_retry_timers.pop(correlation)
        for _correlation, (attempt, harness_id, generation) in tuple(
            self._lifecycle_ensure_io.items()
        ):
            if attempt == attempt_token:
                self._cancel_start_timer(harness_id, generation)
        if not any(
            pending[0] == attempt_token
            for pending in self._lifecycle_ensure_failure_stops.values()
        ):
            unstopped = tuple(fence.unstopped)
            fence.unstopped.clear()
            for harness_id, generation, process, identity in unstopped:
                self._schedule_ensure_failure_stop(
                    attempt_token,
                    harness_id,
                    generation,
                    process,
                    identity,
                )
        self._maybe_finish_ensure_failure_fence(attempt_token)


    def _maybe_finish_ensure_failure_fence(self, attempt_token: str) -> None:
        fence = self._lifecycle_ensure_failure_fences.get(attempt_token)
        if fence is None or fence.unstopped:
            return
        if any(
            tracked[0] == attempt_token
            for tracked in self._lifecycle_ensure_io.values()
        ):
            return
        if any(
            pending[0] == attempt_token
            for pending in self._lifecycle_ensure_failure_stops.values()
        ):
            return
        if attempt_token in self._lifecycle_ensure_settling:
            return
        self._lifecycle_ensure_settling.add(attempt_token)
        accepted = self._persist_desired(
            DesiredStateOperation.ROLLBACK_HARNESS_LIFECYCLE,
            fence.request,
            (attempt_token, fence.provenance.resource_token),
            context=("failed_ensure", attempt_token),
        )
        if not accepted:
            self._lifecycle_ensure_settling.discard(attempt_token)


    def _fence_failed_ensure(
        self,
        command: object,
        request: object,
        provenance: object,
        code: str,
        detail: str,
    ) -> None:
        from hyprial.daemon.impl.lifecycle.lifecycle_receipts  import (
            FailHarnessLifecycleCommand,
            )
        from hyprial.kernel import LifecycleMutationRequest

        assert isinstance(request, LifecycleMutationRequest)
        fence = self._lifecycle_ensure_failure_fences.get(request.attempt_token)
        if fence is None:
            fence = _EnsureFailureFence(request, provenance, code, detail)
            self._lifecycle_ensure_failure_fences[request.attempt_token] = fence
        if isinstance(command, FailHarnessLifecycleCommand):
            fence.control_correlations.append(command.correlation_id)
        self._drive_ensure_failure_fence(request.attempt_token)


    def _on_fail_lifecycle(self, command: object) -> None:
        from hyprial.daemon.impl.lifecycle.lifecycle_receipts  import (
            FailHarnessLifecycleCommand, HarnessLifecycleFailureSettled,
            )
        from hyprial.kernel import LifecycleMutationCompleted

        assert isinstance(command, FailHarnessLifecycleCommand)
        request = command.request
        if not isinstance(
            request.payload, (EnsureHarnessCommand, RemoveHarnessCommand)
        ):
            self._reject(
                command,
                ipc_errors.INVALID_ARGUMENT,
                "only a harness ensure or removal may be failed by deadline",
            )
            return
        assert self._desired_state is not None
        receipt = self._durable_lifecycle_receipt(request.attempt_token)
        if receipt is not None and receipt.completed:
            # The process completion won the mailbox race. Do not turn success
            # into deletion of a newer incarnation or lie to the journal.
            result = LifecycleMutationCompleted(
                request.correlation_id, request.attempt_token,
                self._handler_generation, self._version, "harness", receipt.provenance,
                HarnessMutationCompleted(
                    request.correlation_id, self._handler_generation, self._version,
                    (
                        f"{request.payload.spec.harness}:{request.payload.spec.name}"
                        if isinstance(request.payload, EnsureHarnessCommand)
                        else f"{request.payload.harness}:{request.payload.name}"
                    ),
                    receipt.provenance.changed,
                ),
            )
        elif request.attempt_token in self._lifecycle_failures:
            result = self._lifecycle_failures[request.attempt_token]
        elif request.attempt_token in self._settled_lifecycle_attempts:
            self._reject(command, "LIFECYCLE_ATTEMPT_SETTLED", "the successful receipt was already retired")
            return
        else:
            if isinstance(request.payload, EnsureHarnessCommand):
                key = f"{request.payload.spec.harness}:{request.payload.spec.name}"
                active = self._lifecycle_effect_requests.get(key)
                if (
                    active is not None
                    and active[0].attempt_token == request.attempt_token
                ):
                    provenance = active[1]
                else:
                    # The control can overtake a queued (not yet admitted)
                    # mutation. Apply the genuine request before settling it.
                    self._persist_desired(
                        DesiredStateOperation.APPLY_HARNESS_LIFECYCLE,
                        command,
                        (request,),
                        kwargs=(
                            ("generation", self._handler_generation),
                            ("version", self._version),
                        ),
                        context=("fail_apply", command),
                    )
                    return
                self._fence_failed_ensure(
                    command,
                    request,
                    provenance,
                    command.code,
                    command.detail,
                )
                return
            # The control can overtake a queued (not yet admitted) mutation.
            # Apply its genuine request through the same domain transaction;
            # never guess absence from a journal's 'dispatched' label.
            self._persist_desired(
                DesiredStateOperation.APPLY_HARNESS_LIFECYCLE,
                command,
                (request,),
                kwargs=(
                    ("generation", self._handler_generation),
                    ("version", self._version),
                ),
                context=("fail_apply", command),
            )
            return
        self._emit_event(result)
        self._emit_event(HarnessLifecycleFailureSettled(command.correlation_id, result))
