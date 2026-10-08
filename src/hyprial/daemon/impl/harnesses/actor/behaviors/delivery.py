"""Delivery, result-claim and process-completion behaviour for the actor."""

from __future__ import annotations

import time
import uuid
from dataclasses import replace
from typing import cast
from hyprial.kernel import AdmissionResult
from hyprial.kernel import EffectCompleted, EffectLane, EffectRequest
from hyprial.daemon.impl.api  import (
    HarnessResult,
    StreamingHarnessProcess,
)
from hyprial.kernel  import (
    ManagedHarnessProcess,
    )
from hyprial.daemon.impl.harnesses.runtime.ports  import (
    BindHarnessLivenessCommand,
    DispatchHarnessDeliveryCommand,
    DrainHarnessProgressCommand,
    DrainHarnessResultsCommand,
    ClaimHarnessResultsCommand,
    SettleHarnessResultCommand,
    ClaimedHarnessResult,
    HarnessResultsClaimed,
    HarnessResultSettled,
    HarnessCallIoCompleted,
    HarnessDeliveryAdmitted,
    HarnessLivenessBound,
    HarnessMutationCompleted,
    HarnessProcessStarted,
    HarnessProgressObserved,
    HarnessReadyObserved,
    HarnessResultObserved,
    HarnessSessionRefProjection,
    HarnessStartTimerElapsedCommand,
    HarnessStopIoCompleted,
    HarnessesStopped,
    ReconcileHarnessSessionRefsCommand,
    StopHarnessesCommand,
)
from hyprial.kernel import ipc_errors

from ..contracts import ProcessIdentity, _ClaimScan, _ClaimScanOutcome, _DeliveryReservation, _SessionRefObservation, _StopAllOutcome

class _DeliveryBehavior:
    """Delivery, result-claim and process-completion behaviour for the actor."""


    def _on_dispatch(self, command: DispatchHarnessDeliveryCommand) -> None:
        if command.delivery.delivery_id in self._delivery_reservations:
            self._emit_event(
                HarnessDeliveryAdmitted(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    "",
                    command.delivery.delivery_id,
                    False,
                )
            )
            return
        selected = next(
            (
                (key, record)
                for key, record in self._records.items()
                if record.spec.name == command.name
                and isinstance(record.process, StreamingHarnessProcess)
                and record.process.running
            ),
            None,
        )
        if selected is None or selected[1].process is None:
            self._emit_event(
                HarnessDeliveryAdmitted(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    "",
                    command.delivery.delivery_id,
                    False,
                )
            )
            return
        selected_key, selected_record = selected
        process = cast(StreamingHarnessProcess, selected_record.process)
        self._delivery_reservations[command.delivery.delivery_id] = (
            _DeliveryReservation(selected_key, selected_record.generation)
        )
        self._result_reservation_snapshot = frozenset(self._delivery_reservations)
        correlation = self._register_call(
            command.correlation_id,
            selected_record.generation,
            operation="dispatch",
            harness_id=selected_key,
            delivery_id=command.delivery.delivery_id,
        )
        assert self._io is not None
        self._io.call(
            correlation,
            selected_record.generation,
            self._version,
            lambda: process.enqueue(command.delivery),
        )


    def _on_drain_results(self, command: DrainHarnessResultsCommand) -> None:
        processes = tuple(
            cast(StreamingHarnessProcess, record.process)
            for record in self._records.values()
            if isinstance(record.process, StreamingHarnessProcess)
        )
        correlation = self._register_call(
            command.correlation_id,
            self._handler_generation,
            operation="results",
        )

        def drain() -> tuple[HarnessResult, ...]:
            results: list[HarnessResult] = []
            for process in processes:
                results.extend(process.drain_results())
            return tuple(results)

        assert self._io is not None
        self._io.call(
            correlation,
            self._handler_generation,
            self._version,
            drain,
        )


    def _scan_results(self, scan: _ClaimScan) -> _ClaimScanOutcome:
        rows: list[tuple[str, int, HarnessResult]] = []
        errors: list[str] = []
        for harness_id, generation, process in scan.processes:
            remaining = scan.limit - len(rows)
            if remaining <= 0:
                break
            try:
                results = process.drain_results(remaining)
            except Exception as error:
                errors.append(f"{harness_id}:{type(error).__name__}")
                continue
            rows.extend((harness_id, generation, result) for result in results)
        return _ClaimScanOutcome(tuple(rows), tuple(errors))


    def _on_claim_results(self, command: ClaimHarnessResultsCommand) -> None:
        if command.limit < 1 or command.limit > self._result_claim_capacity:
            self._reject(
                command, ipc_errors.INVALID_ARGUMENT, "result claim limit is out of range"
            )
            return
        if self._claim_scan_id is not None:
            if len(self._claim_waiters) >= self._result_claim_capacity:
                self._reject(
                    command, "HARNESS_RESULT_CLAIM_OVERLOADED",
                    "result claim waiters are full",
                )
                return
            self._claim_waiters.append((command.correlation_id, command.limit))
            return
        available = min(
            command.limit,
            self._result_claim_capacity - len(self._claimed_results),
        )
        processes = tuple(
            (harness_id, record.generation, cast(StreamingHarnessProcess, record.process))
            for harness_id, record in self._records.items()
            if isinstance(record.process, StreamingHarnessProcess)
        )
        if available <= 0 or not processes or self._closing:
            self._emit_event(HarnessResultsClaimed(
                command.correlation_id, self._handler_generation, self._version,
                tuple(self._claimed_results.values())[:command.limit],
            ))
            return
        operation_id = uuid.uuid4().hex
        scan = _ClaimScan(operation_id, available, processes)
        self._claim_scan_id = operation_id
        self._claim_scan_generations = frozenset(
            (harness_id, generation) for harness_id, generation, _ in processes
        )
        self._claim_waiters = [(command.correlation_id, command.limit)]
        if self._result_effects is None:
            self._result_effects = EffectLane(
                name="harness-result-claim-io",
                execute=self._scan_results,
                complete=lambda event: self._runtime.tell(self._handle, event),
                capacity=1,
            )
        admitted = self._result_effects.submit(
            EffectRequest(operation_id, self._handler_generation, scan)
        )
        if admitted is not AdmissionResult.ACCEPTED:
            self._claim_scan_id = None
            self._claim_scan_generations = frozenset()
            self._claim_waiters.clear()
            self._reject(
                command, "HARNESS_RESULT_CLAIM_OVERLOADED",
                f"result claim admission {admitted.value}",
            )


    def _on_claim_scan_completed(self, event: EffectCompleted[_ClaimScanOutcome]) -> None:
        assert self._result_effects is not None
        if event.operation_id != self._claim_scan_id:
            self._result_effects.acknowledge(event.operation_id, event.generation)
            return
        outcome = event.result or _ClaimScanOutcome((), (event.error or "claim-io-failed",))
        for harness_id, generation, result in outcome.rows:
            reservation = self._delivery_reservations.get(result.delivery_id)
            if (
                reservation is not None
                and reservation.claim_token is not None
                and reservation.process_generation == generation
            ):
                continue
            token = uuid.uuid4().hex
            claim = ClaimedHarnessResult(token, harness_id, generation, result)
            self._claimed_results[token] = claim
            # A confirmed stop can release an unclaimed old reservation while
            # this native drain is still in flight.  If the same delivery was
            # offered to a replacement, retain the old result without taking
            # ownership of the replacement's reservation.
            if reservation is None or reservation.process_generation == generation:
                self._delivery_reservations[result.delivery_id] = (
                    _DeliveryReservation(harness_id, generation, token)
                )
        self._result_reservation_snapshot = frozenset(self._delivery_reservations)
        claims = tuple(self._claimed_results.values())
        for correlation_id, limit in self._claim_waiters:
            self._emit_event(HarnessResultsClaimed(
                correlation_id, self._handler_generation, self._version,
                claims[:limit], outcome.errors,
            ))
        self._claim_waiters.clear()
        self._claim_scan_id = None
        self._claim_scan_generations = frozenset()
        for harness_id, generation in self._retire_after_claim_scan:
            for delivery_id, reservation in tuple(self._delivery_reservations.items()):
                if (
                    reservation.harness_id == harness_id
                    and reservation.process_generation == generation
                ):
                    self._release_unclaimed_delivery(delivery_id)
        self._retire_after_claim_scan.clear()
        self._result_effects.acknowledge(event.operation_id, event.generation)


    def _on_settle_result(self, command: SettleHarnessResultCommand) -> None:
        claim = self._claimed_results.get(command.claim_token)
        reservation = self._delivery_reservations.get(command.delivery_id)
        settled = bool(
            claim is not None
            and claim.result.delivery_id == command.delivery_id
        )
        if settled:
            self._claimed_results.pop(command.claim_token)
            if (
                reservation is not None
                and reservation.claim_token == command.claim_token
                and reservation.process_generation == claim.process_generation
            ):
                self._delivery_reservations.pop(command.delivery_id, None)
                self._result_reservation_snapshot = frozenset(
                    self._delivery_reservations
                )
        self._emit_event(HarnessResultSettled(
            command.correlation_id, self._handler_generation, self._version,
            command.claim_token, command.delivery_id, settled,
        ))


    def _release_unclaimed_delivery(
        self, delivery_id: str, *, harness_id: str | None = None,
        generation: int | None = None,
    ) -> None:
        reservation = self._delivery_reservations.get(delivery_id)
        if reservation is None or reservation.claim_token is not None:
            return
        if harness_id is not None and reservation.harness_id != harness_id:
            return
        if generation is not None and reservation.process_generation != generation:
            return
        self._delivery_reservations.pop(delivery_id, None)
        self._result_reservation_snapshot = frozenset(self._delivery_reservations)


    def _release_unclaimed_for(
        self, harness_id: str, *, before_generation: int | None = None,
    ) -> None:
        for delivery_id, reservation in tuple(self._delivery_reservations.items()):
            if reservation.harness_id != harness_id:
                continue
            if (
                before_generation is not None
                and reservation.process_generation >= before_generation
            ):
                continue
            scan_key = (reservation.harness_id, reservation.process_generation)
            if scan_key in self._claim_scan_generations:
                # A native drain may already have removed the only result.
                # Keep its reservation until the claim completion enters this
                # mailbox; stopping the old process cannot reopen the offer.
                self._retire_after_claim_scan.add(scan_key)
                continue
            self._release_unclaimed_delivery(delivery_id)


    def _on_drain_progress(self, command: DrainHarnessProgressCommand) -> None:
        processes = tuple(
            cast(StreamingHarnessProcess, record.process)
            for record in self._records.values()
            if isinstance(record.process, StreamingHarnessProcess)
        )
        correlation = self._register_call(
            command.correlation_id,
            self._handler_generation,
            operation="progress",
        )

        def drain() -> tuple[object, ...]:
            events: list[object] = []
            for process in processes:
                method = getattr(process, "drain_progress", None)
                if callable(method):
                    events.extend(method())
            return tuple(events)

        assert self._io is not None
        self._io.call(
            correlation,
            self._handler_generation,
            self._version,
            drain,
        )


    def _on_bind_liveness(self, command: BindHarnessLivenessCommand) -> None:
        harness_id = f"{command.harness}:{command.name}"
        record = self._records.get(harness_id)
        if record is None:
            self._emit_event(
                HarnessLivenessBound(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    harness_id,
                    False,
                )
            )
            return
        old = record.liveness_binding
        record.liveness_binding = command.binding
        self._emit_event(
            HarnessLivenessBound(
                command.correlation_id,
                self._handler_generation,
                self._version,
                harness_id,
                True,
            )
        )
        if old is not None and old is not command.binding:
            assert self._io is not None
            self._io.call(
                uuid.uuid4().hex,
                record.generation,
                self._version,
                lambda: old.close(),
            )


    def _on_stop_all(self, command: StopHarnessesCommand) -> None:
        self._closing = True
        self._terminate_pending(
            "HARNESS_RUNTIME_STOPPED", "harness runtime stopped"
        )
        deadline = command.deadline_ms / 1000.0
        for harness_id, record in self._records.items():
            record.generation += 1
            record.starting = False
            if record.explicit_correlation_id is not None:
                self._reject_correlation(
                    record.explicit_correlation_id,
                    "HARNESS_RUNTIME_STOPPED",
                    "harness runtime stopped",
                )
                record.explicit_correlation_id = None
            if record.process is not None:
                self._stop_retry_processes.setdefault(
                    harness_id, (record.process, record.identity)
                )
                record.process = None
                record.identity = None
            if record.liveness_binding is not None:
                self._stop_retry_bindings.setdefault(
                    harness_id, record.liveness_binding
                )
                record.liveness_binding = None
        processes = tuple(self._stop_retry_processes.items())
        bindings = tuple(self._stop_retry_bindings.items())
        correlation = self._register_call(
            command.correlation_id,
            self._handler_generation,
            operation="stop_all",
        )

        def stop_all() -> _StopAllOutcome:
            errors: list[str] = []
            stopped_ids: list[str] = []
            bindings_closed: list[str] = []
            assert self._io is not None
            for harness_id, (process, identity) in processes:
                if time.monotonic() >= deadline:
                    errors.append(f"{harness_id}: harness stop deadline elapsed")
                    continue
                stopped, detail = self._io._stop_checked(
                    process, identity, harness_id=harness_id
                )
                if stopped:
                    stopped_ids.append(harness_id)
                else:
                    errors.append(detail or "harness did not stop")
            for harness_id, binding in bindings:
                if time.monotonic() >= deadline:
                    errors.append(f"{harness_id}: binding stop deadline elapsed")
                    continue
                try:
                    binding.close()
                except BaseException as error:
                    errors.append(str(error))
                else:
                    bindings_closed.append(harness_id)
            return _StopAllOutcome(
                tuple(stopped_ids), tuple(bindings_closed), tuple(errors)
            )

        assert self._io is not None
        self._io.call(
            correlation,
            self._handler_generation,
            self._version,
            stop_all,
        )


    def _on_start_completed(self, event: HarnessProcessStarted) -> None:
        tracked = self._lifecycle_ensure_io.pop(event.correlation_id, None)
        if (
            tracked is not None
            and tracked[0] in self._lifecycle_ensure_failure_fences
        ):
            attempt_token, harness_id, generation = tracked
            self._cancel_start_timer(harness_id, generation)
            record = self._records.get(harness_id)
            if (
                record is not None
                and record.generation == event.generation
                and record.attempt_correlation_id == event.correlation_id
            ):
                record.starting = False
                record.explicit_correlation_id = None
                record.attempt_correlation_id = None
            if event.process is not None:
                self._schedule_ensure_failure_stop(
                    attempt_token,
                    harness_id,
                    event.generation,
                    cast(ManagedHarnessProcess, event.process),
                    ProcessIdentity(event.pid, event.identity_marker),
                )
            self._publish()
            self._maybe_finish_ensure_failure_fence(attempt_token)
            return
        self._cancel_start_timer(event.harness_id, event.generation)
        record = self._records.get(event.harness_id)
        if (
            record is None
            or event.generation != record.generation
            or event.correlation_id != record.attempt_correlation_id
            or not record.starting
        ):
            if event.process is not None:
                assert self._io is not None
                self._io.stop(
                    uuid.uuid4().hex,
                    event.harness_id,
                    event.generation,
                    self._version,
                    cast(ManagedHarnessProcess, event.process),
                    ProcessIdentity(event.pid, event.identity_marker),
                )
            return
        record.starting = False
        if record.attempt_kind == "automatic":
            record.starting = True
            record.completed_start = event
            return
        if event.error is not None:
            self._record_failure(
                event.harness_id,
                record,
                str(event.error),
                permanent=bool(
                    getattr(event.error, "permanent_start_failure", False)
                ),
            )
            self._finish_start_attempt(event.harness_id, record, False, event.error)
            return
        assert event.process is not None
        record.process = cast(ManagedHarnessProcess, event.process)
        record.identity = ProcessIdentity(event.pid, event.identity_marker)
        record.last_error = None
        record.failures = 0
        record.restart_after = None
        record.restore_failed_terminal = False
        self._finish_start_attempt(event.harness_id, record, True, None)


    def _on_start_timeout(self, event: HarnessStartTimerElapsedCommand) -> None:
        self._cancel_start_timer(event.harness_id, event.generation)
        record = self._records.get(event.harness_id)
        if (
            record is None
            or record.generation != event.generation
            or record.attempt_correlation_id != event.correlation_id
            or not record.starting
        ):
            return
        record.starting = False
        record.generation += 1
        error = TimeoutError(
            "harness start did not complete within "
            f"{self._start_timeout_seconds:g}s"
        )
        self._record_failure(event.harness_id, record, str(error))
        self._finish_start_attempt(event.harness_id, record, False, error)


    def _on_stop_completed(self, event: HarnessStopIoCompleted) -> None:
        fenced = self._lifecycle_ensure_failure_stops.pop(
            event.correlation_id, None
        )
        if fenced is not None:
            attempt_token, harness_id, generation, process, identity = fenced
            fence = self._lifecycle_ensure_failure_fences.get(attempt_token)
            if fence is None:
                return
            if not event.stopped:
                fence.unstopped.append(
                    (harness_id, generation, process, identity)
                )
            self._maybe_finish_ensure_failure_fence(attempt_token)
            return
        record = self._records.get(event.harness_id)
        if record is None or record.generation != event.generation:
            return
        if not event.stopped and event.detail:
            record.last_error = event.detail
        if event.stopped:
            self._release_unclaimed_for(
                event.harness_id, before_generation=event.generation
            )


    def _on_call_completed(self, event: HarnessCallIoCompleted) -> None:
        pending = self._calls.pop(event.correlation_id)
        if pending is None:
            if event.error is not None:
                self._reject_correlation(
                    event.correlation_id,
                    "HARNESS_IO_CANCELLED",
                    str(event.error),
                )
            return
        if pending.reply_correlation_id is not None:
            # Each observation has its own I/O settlement identity. A fleet
            # change may require another observation for the same caller;
            # only the reply/retry uses that caller's public correlation.
            event = replace(event, correlation_id=pending.reply_correlation_id)
        if event.generation != pending.expected_generation:
            if pending.operation == "dispatch" and pending.delivery_id is not None:
                self._release_unclaimed_delivery(pending.delivery_id)
            self._reject_correlation(
                event.correlation_id,
                "STALE_HARNESS_IO_COMPLETION",
                "stale process I/O completion",
            )
            return
        if pending.harness_id is not None:
            record = self._records.get(pending.harness_id)
            if record is None or record.generation != pending.expected_generation:
                if pending.operation == "dispatch" and pending.delivery_id is not None:
                    self._release_unclaimed_delivery(pending.delivery_id)
                self._reject_correlation(
                    event.correlation_id,
                    "STALE_HARNESS_IO_COMPLETION",
                    "stale process I/O completion",
                )
                return
        if event.error is not None:
            if pending.operation == "dispatch" and pending.delivery_id is not None:
                self._release_unclaimed_delivery(pending.delivery_id)
            self._reject_correlation(
                event.correlation_id,
                "HARNESS_IO_FAILED",
                str(event.error),
            )
            return
        if pending.operation == "session_refs":
            self._settle_session_refs_completion(event, pending)
            return
        self._dispatch_call_completion(event, pending)

    def _settle_session_refs_completion(
        self,
        event: HarnessCallIoCompleted,
        pending,
    ) -> None:
        from hyprial.daemon.impl.processes.process_owner  import ProcessOwner

        observed = event.value
        if (
            not isinstance(observed, tuple)
            or not all(isinstance(item, _SessionRefObservation) for item in observed)
        ):
            self._reject_correlation(
                event.correlation_id,
                "STALE_HARNESS_IO_COMPLETION",
                "session-ref facts no longer cover the current harness fleet",
            )
            return
        if len(observed) != len(self._records):
            self._on_reconcile_session_refs(
                ReconcileHarnessSessionRefsCommand(event.correlation_id)
            )
            return
        for item in observed:
            record = self._records.get(item.key)
            if (
                record is None
                or record.incarnation != item.incarnation
                or record.generation != item.generation
                or getattr(record.process, "process_token", None)
                != item.process_token
                or (
                    isinstance(record.process, ProcessOwner)
                    and record.process.facts().session_ref != item.session_ref
                )
            ):
                # A concurrent lifecycle remove/restart changed the fleet
                # while native facts were read. Join the new owner (or
                # settle empty after removal) under the same correlation;
                # never write the old process's ref into a new row.
                self._on_reconcile_session_refs(
                    ReconcileHarnessSessionRefsCommand(event.correlation_id)
                )
                return
        self._publish()
        refs = tuple(
            HarnessSessionRefProjection(item.harness, item.name, item.session_ref)
            for item in observed
            if item.session_ref is not None
        )
        self._complete_session_ref_reconcile(
            ReconcileHarnessSessionRefsCommand(event.correlation_id), refs
        )


    def _dispatch_call_completion(
        self,
        event: HarnessCallIoCompleted,
        pending,
    ) -> None:
        if pending.operation == "remove":
            if bool(event.value) and pending.subject_id is not None:
                self._release_unclaimed_for(pending.subject_id)
            self._emit_event(
                HarnessMutationCompleted(
                    event.correlation_id,
                    self._handler_generation,
                    self._version,
                    pending.subject_id or "",
                    bool(event.value),
                )
            )
        elif pending.operation == "lifecycle-remove":
            if pending.subject_id is not None:
                if bool(event.value):
                    self._release_unclaimed_for(pending.subject_id)
                self._records.pop(pending.subject_id, None)
            self._emit_event(
                HarnessMutationCompleted(
                    event.correlation_id,
                    self._handler_generation,
                    self._version,
                    pending.subject_id or "",
                    bool(event.value),
                )
            )
        elif pending.operation == "ready":
            self._emit_event(
                HarnessReadyObserved(
                    event.correlation_id,
                    self._handler_generation,
                    self._version,
                    pending.harness_id or "",
                    bool(event.value),
                )
            )
        elif pending.operation == "dispatch":
            if not bool(event.value) and pending.delivery_id is not None:
                self._release_unclaimed_delivery(pending.delivery_id)
            self._emit_event(
                HarnessDeliveryAdmitted(
                    event.correlation_id,
                    self._handler_generation,
                    self._version,
                    pending.harness_id or "",
                    pending.delivery_id or "",
                    bool(event.value),
                )
            )
        elif pending.operation == "results":
            for result in cast(tuple[HarnessResult, ...], event.value):
                self._release_unclaimed_delivery(result.delivery_id)
            self._emit_event(
                HarnessResultObserved(
                    event.correlation_id,
                    self._handler_generation,
                    self._version,
                    cast(tuple[HarnessResult, ...], event.value),
                )
            )
        elif pending.operation == "progress":
            self._emit_event(
                HarnessProgressObserved(
                    event.correlation_id,
                    self._handler_generation,
                    self._version,
                    cast(tuple[object, ...], event.value),
                )
            )
        elif pending.operation == "stop_all":
            outcome = cast(_StopAllOutcome, event.value)
            for harness_id in outcome.stopped:
                self._stop_retry_processes.pop(harness_id, None)
                self._release_unclaimed_for(harness_id)
            for harness_id in outcome.bindings_closed:
                self._stop_retry_bindings.pop(harness_id, None)
            remaining = tuple(
                sorted({
                    *self._stop_retry_processes,
                    *self._stop_retry_bindings,
                    *(
                        key
                        for key, record in self._records.items()
                        if record.process is not None
                        or record.liveness_binding is not None
                    ),
                })
            )
            # Stop-all failures are teardown diagnostics, not harnesses that
            # entered the terminal failed state. Mixing arbitrary error text
            # into this queue makes maintenance render it as a harness key.
            for detail in outcome.errors:
                if self._logger is not None:
                    try:
                        self._logger(
                            "error",
                            "daemon",
                            "harness.stop_failed",
                            detail=detail,
                        )
                    except Exception:
                        pass
            self._emit_event(
                HarnessesStopped(
                    event.correlation_id,
                    self._handler_generation,
                    self._version,
                    remaining,
                    drain_complete=(
                        not outcome.errors
                        and not remaining
                        and (self._io is None or self._io.in_flight <= 1)
                    ),
                )
            )
