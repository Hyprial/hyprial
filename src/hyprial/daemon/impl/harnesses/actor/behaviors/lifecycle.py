"""Lifecycle command behaviour for the harness runtime actor."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from typing import cast
from hyprial.kernel import AdmissionResult
from hyprial.daemon.impl.desired_state_io  import (
    DesiredStateOperation,
)
from hyprial.daemon.impl.harnesses.runtime.ports  import (
    EnsureHarnessCommand,
    HarnessMutationCompleted,
    HarnessRestoreCompleted,
    HarnessRestoreProjection,
    HarnessTimerElapsedCommand,
    RemoveHarnessCommand,
    RestoreEligibilityProjection,
    RestoreHarnessesCommand,
)
from hyprial.kernel import ipc_errors

from ..contracts import _Record, _RestoreBatch, _launch_spec

class _LifecycleBehavior:
    """Lifecycle command behaviour for the harness runtime actor."""


    def _on_lifecycle(self, request: object) -> None:
        from hyprial.kernel import LifecycleMutationRequest

        assert isinstance(request, LifecycleMutationRequest)
        if self._desired_state is None:
            self._reject_correlation(
                request.correlation_id,
                "HARNESS_LIFECYCLE_STORE_MISSING",
                "Harness lifecycle authority requires DesiredStateStore",
            )
            return
        payload = request.payload
        if not isinstance(payload, (EnsureHarnessCommand, RemoveHarnessCommand)):
            self._reject_correlation(
                request.correlation_id,
                ipc_errors.INVALID_ARGUMENT,
                f"unsupported Harness lifecycle payload: {type(payload).__name__}",
            )
            return
        self._persist_desired(
            DesiredStateOperation.APPLY_HARNESS_LIFECYCLE,
            request,
            (request,),
            kwargs=(
                ("generation", self._handler_generation),
                ("version", self._version),
            ),
            context=("lifecycle_apply", request),
        )


    def _continue_lifecycle_apply(
        self, request: object, result: object
    ) -> None:
        from hyprial.kernel  import (
            MutationProvenance,
        )
        from hyprial.kernel import LifecycleMutationRequest

        assert isinstance(request, LifecycleMutationRequest)
        if (
            not isinstance(result, tuple)
            or len(result) != 2
            or not isinstance(result[0], MutationProvenance)
            or not isinstance(result[1], bool)
        ):
            raise TypeError("harness lifecycle persistence returned invalid result")
        provenance, _replayed = result
        payload = request.payload
        assert isinstance(payload, (EnsureHarnessCommand, RemoveHarnessCommand))
        harness_id = (
            f"{payload.spec.harness}:{payload.spec.name}"
            if isinstance(payload, EnsureHarnessCommand)
            else f"{payload.harness}:{payload.name}"
        )
        active = self._lifecycle_effect_requests.get(harness_id)
        pending_correlation = next(
            (
                correlation
                for correlation, (pending_request, _provenance)
                in self._lifecycle_pending.items()
                if pending_request.attempt_token == request.attempt_token
            ),
            None,
        )
        same_attempt = bool(
            active is not None
            and active[0].attempt_token == request.attempt_token
            and pending_correlation is not None
        )
        if (
            same_attempt
            and self._lifecycle_native_admitted.get(pending_correlation)
            == request.attempt_token
        ):
            # The original continuation already transferred exact start/stop
            # custody to ProcessIoPort.  Its completion will settle the
            # retained pending correlation; replaying the frozen persistence
            # result must not supersede that native generation.
            return
        record = self._records.get(harness_id)
        runtime_satisfied = bool(
            isinstance(payload, EnsureHarnessCommand)
            and record is not None
            and record.spec == _launch_spec(payload.spec)
            and record.process is not None
            and record.process.running
            and not record.starting
        )
        if not provenance.changed and (
            isinstance(payload, RemoveHarnessCommand) or runtime_satisfied
        ):
            base = HarnessMutationCompleted(
                request.correlation_id,
                self._handler_generation,
                self._version,
                harness_id,
                False,
            )
            if isinstance(payload, EnsureHarnessCommand):
                # U0b: even the no-op "already running" completion must own
                # the desired-state row (idempotently) -- under start-after-
                # success semantics the row's ONLY writers are this confirm,
                # the migration, and offline staging.
                operation = DesiredStateOperation.CONFIRM_HARNESS_LIFECYCLE
                args = (
                    request.attempt_token,
                    provenance.resource_token,
                    _launch_spec(payload.spec),
                )
            else:
                operation = DesiredStateOperation.COMPLETE_HARNESS_LIFECYCLE
                args = (request.attempt_token, provenance.resource_token)
            self._persist_desired(
                operation,
                request,
                args,
                kwargs=(
                    ("generation", self._handler_generation),
                    ("version", self._version),
                ),
                context=(
                    "lifecycle_noop",
                    request,
                    provenance,
                    base,
                ),
            )
            return
        internal_correlation = (
            pending_correlation
            if same_attempt
            else f"{request.correlation_id}:io:{uuid.uuid4().hex}"
        )
        assert internal_correlation is not None
        internal_payload = replace(payload, correlation_id=internal_correlation)
        resource_id = harness_id
        if not same_attempt:
            self._lifecycle_pending[internal_correlation] = (request, provenance)
            self._lifecycle_effect_resources.add(resource_id)
            self._lifecycle_effect_requests[resource_id] = (request, provenance)
        if isinstance(internal_payload, EnsureHarnessCommand):
            self._on_ensure(internal_payload, lifecycle=True)
            record = self._records.get(resource_id)
            if (
                record is not None
                and record.starting
                and record.attempt_correlation_id == internal_correlation
            ):
                self._lifecycle_ensure_io[internal_correlation] = (
                    request.attempt_token,
                    resource_id,
                    record.generation,
                )
        else:
            assert isinstance(internal_payload, RemoveHarnessCommand)
            self._on_lifecycle_remove(internal_payload)


    def _replay_lifecycle_completion(
        self, request: object, receipt: object
    ) -> None:
        from hyprial.daemon.impl.lifecycle.lifecycle_receipts  import (
            StoredLifecycleReceipt,
        )
        from hyprial.kernel import LifecycleMutationCompleted
        from hyprial.kernel import LifecycleMutationRequest

        assert isinstance(request, LifecycleMutationRequest)
        assert isinstance(receipt, StoredLifecycleReceipt)
        payload = request.payload
        if not isinstance(payload, (EnsureHarnessCommand, RemoveHarnessCommand)):
            self.release_lifecycle_replay(request.attempt_token)
            self._reject(
                request,
                ipc_errors.INVALID_ARGUMENT,
                f"unsupported Harness lifecycle payload: {type(payload).__name__}",
            )
            return
        generation = receipt.generation or self._handler_generation
        version = receipt.version if receipt.version is not None else self._version
        harness_id = (
            f"{payload.spec.harness}:{payload.spec.name}"
            if isinstance(payload, EnsureHarnessCommand)
            else f"{payload.harness}:{payload.name}"
        )
        correlation_id = receipt.correlation_id or request.correlation_id
        base = HarnessMutationCompleted(
            correlation_id,
            generation,
            version,
            harness_id,
            receipt.provenance.changed,
        )
        self._emit_event(
            LifecycleMutationCompleted(
                correlation_id,
                request.attempt_token,
                generation,
                version,
                "harness",
                receipt.provenance,
                base,
                replayed=True,
            )
        )


    def _on_lifecycle_remove(self, command: RemoveHarnessCommand) -> None:
        """Stop without dropping actor custody until the I/O effect succeeds."""

        self._restore_eligibility.pop(command.name, None)
        harness_id = f"{command.harness}:{command.name}"
        record = self._records.get(harness_id)
        if record is None:
            self._emit_event(
                HarnessMutationCompleted(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    harness_id,
                    True,
                )
            )
            return
        record.generation += 1
        process = record.process
        binding = record.liveness_binding
        correlation = self._register_call(
            command.correlation_id,
            record.generation,
            operation="lifecycle-remove",
            harness_id=harness_id,
            subject_id=harness_id,
        )

        def stop_all() -> bool:
            detail: list[str] = []
            if binding is not None:
                try:
                    cast(Callable[[], object], getattr(binding, "close"))()
                except BaseException as error:
                    detail.append(str(error))
            if process is not None:
                assert self._io is not None
                stopped, error = self._io._stop_checked(
                    process,
                    record.identity,
                    harness_id=harness_id,
                    interruption_reason=command.interruption_reason,
                )
                if not stopped:
                    detail.append(error or "harness did not stop")
            if detail:
                raise RuntimeError("; ".join(detail))
            return True

        assert self._io is not None
        self._io.call(correlation, record.generation, self._version, stop_all)
        self._mark_lifecycle_native_admitted(correlation)


    def close_runtime(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._generation_lock:
            self._closing = True
        for timer in tuple(self._lifecycle_retry_timers.values()):
            timer.cancel()
        self._lifecycle_retry_timers.clear()
        terminalized = self._close_lifecycle_in_actor(deadline)
        if self._persistence is not None:
            if not self._persistence.close(
                max(0.0, deadline - time.monotonic())
            ):
                return False
        if self._claimed_results:
            return False
        if self._result_effects is not None:
            if not self._result_effects.close(max(0.0, deadline - time.monotonic())):
                return False
        if self._claimed_results:
            return False
        drained = True
        if self._io is not None:
            drained = self._io.close(deadline)
        if drained and self._owns_orphan_processes:
            drained = self._orphan_processes.close(max(0.0, deadline - time.monotonic()))
        if not drained:
            # Process completions still need the actor to acknowledge their
            # exact receipt.  Keep it addressable so a later close retry can
            # join that custody after the native operation settles.
            return False
        handle = self._handle
        if handle is not None:
            stopped = self._runtime.stop(
                handle, timeout=max(0.0, deadline - time.monotonic())
            )
            if not stopped:
                # Actor/backend ownership is still live.  Retain the stable
                # handle and every dependent call so a later close can join
                # the same stop future instead of losing cleanup custody.
                return False
            self._handle = None
        # Edge correlation custody is independently synchronized and can be
        # cleared after the actor is no longer addressable.  Domain records
        # are never mutated here outside their actor.
        self._calls.clear()
        return drained and terminalized


    def _close_lifecycle_in_actor(self, deadline: float) -> bool:
        from hyprial.daemon.impl.lifecycle.lifecycle_receipts  import (
            TerminalizeHarnessLifecycleCommand, HarnessLifecycleTerminalized,
        )

        if self._handle is None:
            return True
        command = TerminalizeHarnessLifecycleCommand(
            f"harness:closing:{uuid.uuid4().hex}", "HARNESS_RUNTIME_CLOSING",
            "Harness runtime closed before its lifecycle process effect completed",
        )
        settled = threading.Event()

        def observe(event: object) -> None:
            if isinstance(event, HarnessLifecycleTerminalized) and event.correlation_id == command.correlation_id:
                settled.set()

        self.subscribe_events(observe)
        try:
            while time.monotonic() < deadline:
                admission = self._admit(command)
                if admission is AdmissionResult.ACCEPTED:
                    return settled.wait(max(0.0, deadline - time.monotonic()))
                if admission is not AdmissionResult.OVERLOADED:
                    return False
                time.sleep(min(0.002, max(0.0, deadline - time.monotonic())))
            return False
        finally:
            self._event_sinks.remove(observe)


    def _terminalize_incomplete_lifecycle(self, command: object) -> None:
        if self._desired_state is None:
            from hyprial.daemon.impl.lifecycle.lifecycle_receipts  import HarnessLifecycleTerminalized

            self._publish_event(HarnessLifecycleTerminalized(command.correlation_id))
            return
        state = self._desired_state.load()
        receipts = tuple(
            receipt
            for receipt in state.lifecycle_receipts
            if receipt.domain == "harness" and not receipt.completed
        )
        requests = tuple(
            (resource_id, entry[0], entry[1])
            for resource_id, entry in self._lifecycle_effect_requests.items()
        )
        self._lifecycle_pending.clear()
        self._lifecycle_native_admitted.clear()
        self._terminalize_next(command, receipts, requests)


    def _terminalize_next(
        self,
        command: object,
        receipts: tuple[object, ...],
        requests: tuple[tuple[str, object, object], ...],
    ) -> None:
        from hyprial.daemon.impl.lifecycle.lifecycle_receipts  import HarnessLifecycleTerminalized

        if not receipts:
            self._publish()
            self._publish_event(HarnessLifecycleTerminalized(command.correlation_id))
            return
        receipt, remaining = receipts[0], receipts[1:]
        resource_id = receipt.resource_key.removeprefix("harness:")
        request_entry = next(
            (entry for entry in requests if entry[0] == resource_id), None
        )
        request = None if request_entry is None else request_entry[1]
        if request is not None and isinstance(request.payload, RemoveHarnessCommand):
            operation = DesiredStateOperation.FAIL_HARNESS_REMOVAL
        else:
            operation = DesiredStateOperation.ROLLBACK_HARNESS_LIFECYCLE
        self._persist_desired(
            operation,
            command,
            (receipt.attempt_token, receipt.provenance.resource_token),
            context=(
                "terminalize",
                command,
                receipt,
                request,
                remaining,
                requests,
            ),
        )


    def _on_restore(self, command: RestoreHarnessesCommand) -> None:
        self._closing = False
        desired_specs = tuple(_launch_spec(spec) for spec in command.specs)
        desired = {self._key(spec): spec for spec in desired_specs}
        self._records = {key: _Record(spec=spec) for key, spec in desired.items()}
        if not desired:
            self._emit_event(
                HarnessRestoreCompleted(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    HarnessRestoreProjection(0, 0, 0, 0),
                )
            )
            return
        batch_id = uuid.uuid4().hex
        self._restore_batches[batch_id] = _RestoreBatch(
            correlation_id=command.correlation_id,
            attempted=len(desired),
            pending=set(desired),
        )
        batch = self._restore_batches[batch_id]
        for key, record in self._records.items():
            if key in self._lifecycle_effect_resources:
                # Desired but not dispositionable this round: a lifecycle
                # effect owns the resource, so admission never opens for it.
                # The round still owes one report per desired target --
                # silence would read as "never desired" in the projection.
                # F1: the deferred report is also the round's disposition,
                # so it settles the batch's interest in the key right here;
                # leaving it in ``batch.pending`` waited forever on a start
                # this round never admits, and the gate never opened.
                self._settle_deferred_restore_target(key, batch_id)
                continue
            record.restore_batch = batch_id
            batch.queued.append(key)
        self._admit_restore_starts(batch_id)


    def _settle_deferred_restore_target(self, key: str, batch_id: str) -> None:
        """Disposition one restore target as deferred and settle its batch.

        Two doors reach here, and neither may leave the key in
        ``batch.pending``: ``_on_restore`` seeing a target a lifecycle
        effect already owns, and ``_begin_start`` hitting the same claim
        after admission queued the key.  In both cases no start attempt of
        this round will ever settle the key -- the claiming saga starts the
        connector through the lifecycle path with its own disposition --
        so the round settles it as a handoff: report ``deferred``, take it
        out of the batch, and complete the batch when nothing else is
        pending.  A batch whose every target is deferred still completes.
        """
        self._report_readiness(key, "deferred")
        batch = self._restore_batches.get(batch_id)
        if batch is None or key not in batch.pending:
            return
        batch.pending.remove(key)
        batch.deferred += 1
        if not batch.pending:
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


    def _starts_in_flight(self) -> int:
        """Starts currently handed to the I/O port, derived not tracked.

        Counting `starting` records rather than keeping a counter means there
        is no increment/decrement pair to get wrong across the several places
        a start can settle.  It over-counts slightly -- a reconcile-kind start
        stays `starting` until the next tick harvests it -- and over-counting
        only ever admits fewer starts, which is the safe direction.
        """

        return sum(1 for record in self._records.values() if record.starting)


    def _admit_restore_starts(self, batch_id: str) -> None:
        """Hand queued restore starts to the I/O port, up to executor width.

        The bound is the executor width, not the port's semaphore (which is
        four times wider).  A start admitted past the width is accepted by
        the port but waits for a worker thread, and its start timer is
        already running — so a fleet that merely starts slowly reports
        timeouts for harnesses nothing has contacted yet.  Keeping in-flight
        starts at the width means every running timer measures a launcher
        call that is actually happening.
        """

        batch = self._restore_batches.get(batch_id)
        if batch is None:
            return
        while batch.queued and self._starts_in_flight() < self._start_admission_width:
            key = batch.queued.popleft()
            record = self._records.get(key)
            if record is None or record.restore_batch != batch_id:
                continue
            self._begin_start(
                key,
                record,
                "restore",
                correlation_id=f"{batch.correlation_id}:{key}",
            )


    def _on_ensure(
        self, command: EnsureHarnessCommand, *, lifecycle: bool = False
    ) -> None:
        spec = _launch_spec(command.spec)
        key = self._key(spec)
        if key in self._lifecycle_effect_resources and not lifecycle:
            self._reject(
                command,
                "HARNESS_LIFECYCLE_IN_PROGRESS",
                f"{key} is owned by an incomplete lifecycle effect",
            )
            return
        record = self._records.get(key)
        if (
            record is not None
            and record.spec == spec
            and record.process is not None
            and record.process.running
            and not record.starting
        ):
            # Already running IS the disposition for an ensure: the target
            # was examined and needed no action.  Report it ready.
            self._report_readiness(key, "ready")
            self._emit_event(
                HarnessMutationCompleted(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    key,
                    False,
                )
            )
            return
        if record is None:
            record = _Record(spec=spec)
            self._records[key] = record
        elif record.explicit_correlation_id is not None:
            same_lifecycle_retry = bool(
                lifecycle
                and record.explicit_correlation_id == command.correlation_id
            )
            if not same_lifecycle_retry:
                self._reject_correlation(
                    record.explicit_correlation_id,
                    "HARNESS_START_SUPERSEDED",
                    "harness start superseded by a newer command",
                )
                record.explicit_correlation_id = None
        incumbent = (
            (record.process, record.identity)
            if record.process is not None
            else None
        )
        old_binding = record.liveness_binding
        record.liveness_binding = None
        if old_binding is not None:
            assert self._io is not None
            self._io.call(
                uuid.uuid4().hex,
                record.generation,
                self._version,
                lambda binding=old_binding: binding.close(),
            )
        record.spec = spec
        record.failures = 0
        record.restart_after = None
        # An explicit start is the documented way out of both failed
        # states (budget-exhausted and the U0b restore verdict).
        record.restore_failed_terminal = False
        record.explicit_correlation_id = command.correlation_id
        self._begin_start(
            key,
            record,
            "explicit",
            correlation_id=command.correlation_id,
            replace=incumbent,
            allow_lifecycle=lifecycle,
        )


    def _on_remove(self, command: RemoveHarnessCommand) -> None:
        harness_id = f"{command.harness}:{command.name}"
        if harness_id in self._lifecycle_effect_resources:
            self._reject(
                command,
                "HARNESS_LIFECYCLE_IN_PROGRESS",
                f"{harness_id} is owned by an incomplete lifecycle effect",
            )
            return
        record = self._records.pop(harness_id, None)
        if record is None:
            self._emit_event(
                HarnessMutationCompleted(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    harness_id,
                    False,
                )
            )
            return
        record.generation += 1
        process = record.process
        binding = record.liveness_binding
        if process is None and binding is None:
            self._emit_event(
                HarnessMutationCompleted(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    harness_id,
                    True,
                )
            )
            return
        correlation = self._register_call(
            command.correlation_id,
            record.generation,
            operation="remove",
            harness_id=None,
            subject_id=harness_id,
        )

        def stop_all() -> bool:
            detail: list[str] = []
            if binding is not None:
                try:
                    cast(Callable[[], object], getattr(binding, "close"))()
                except BaseException as error:
                    detail.append(str(error))
            if process is not None:
                assert self._io is not None
                stopped, error = self._io._stop_checked(
                    process,
                    record.identity,
                    harness_id=harness_id,
                    interruption_reason=command.interruption_reason,
                )
                if not stopped:
                    detail.append(error or "harness did not stop")
            if detail:
                raise RuntimeError("; ".join(detail))
            return True

        assert self._io is not None
        self._io.call(correlation, record.generation, self._version, stop_all)


    def _on_process_facts(self, command) -> None:
        record = self._records.get(command.harness_id)
        if (
            record is not None
            and getattr(record.process, "process_token", None) == command.process_token
            and not self._closing
        ):
            if not command.running:
                self._on_reconcile(
                    HarnessTimerElapsedCommand(
                        f"process-facts:{command.process_token}",
                        self._handler_generation,
                        self._last_timer_sequence,
                        int(time.time() * 1000),
                    ),
                    observation=True,
                )


    def _restore_entity_current(
        self, eligibility: RestoreEligibilityProjection
    ) -> bool:
        identity = self._agent_identity
        if identity is None:
            return True
        try:
            current = identity.entity_token(eligibility.actor)
        except Exception:  # cache view failure must fail open for process restore
            return False
        return current is not None and current == eligibility.entity_token
