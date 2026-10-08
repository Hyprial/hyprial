"""Reconcile / desired-state / readiness behaviour for the actor."""

from __future__ import annotations

import uuid
from typing import cast
from hyprial.kernel  import (
    ManagedHarnessProcess,
)
from hyprial.daemon.impl.desired_state_io  import (
    DesiredStateOperation,
)
from hyprial.daemon.impl.harnesses.runtime.ports  import (
    HarnessAdapterRegistrationProjection,
    HarnessDesiredStateManaged,
    HarnessReadyObserved,
    HarnessSessionRefProjection,
    HarnessSessionRefsReconciled,
    HarnessTimerCompleted,
    HarnessTimerElapsedCommand,
    RemoveAdapterRegistrationCommand,
    ReconcileHarnessSessionRefsCommand,
    RestoreAdapterRegistrationCommand,
    SnapshotAdapterRegistrationCommand,
    StageHarnessDesiredCommand,
    WaitHarnessReadyCommand,
    harness_desired_generation,
)

from ..contracts import ProcessIdentity, _SessionRefObservation, _launch_projection, _launch_spec

class _ReconcileBehavior:
    """Reconcile / desired-state / readiness behaviour for the actor."""


    def _on_reconcile(
        self, command: HarnessTimerElapsedCommand, *, observation: bool = False
    ) -> None:
        from hyprial.daemon.impl.processes.process_owner  import ProcessOwner

        if not observation and (
            command.generation != self._handler_generation
            or command.version <= self._last_timer_sequence
        ):
            self._reject(
                command,
                "STALE_HARNESS_TIMER",
                "timer generation/version no longer owns harness reconciliation",
            )
            return
        if not observation:
            self._last_timer_sequence = command.version
        if self._closing:
            self._emit_event(
                HarnessTimerCompleted(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    0,
                )
            )
            return
        now = self._clock()
        restarted = 0
        for key, record in self._records.items():
            if not observation and isinstance(record.process, ProcessOwner):
                record.process.refresh()
            if key in self._lifecycle_effect_resources:
                continue
            completed = record.completed_start
            if completed is not None:
                record.completed_start = None
                record.starting = False
                # A reconcile-kind start settles here, on the tick that
                # harvests it -- that harvest is the disposition, so this
                # is where its readiness report is written (reconcile-kind
                # starts never reach `_finish_start_attempt`).
                if completed.error is not None:
                    self._record_failure(
                        key,
                        record,
                        str(completed.error),
                        permanent=bool(
                            getattr(
                                completed.error,
                                "permanent_start_failure",
                                False,
                            )
                        ),
                    )
                    self._report_readiness(
                        key,
                        "failed" if self._is_failed(record) else "retrying",
                    )
                else:
                    assert completed.process is not None
                    record.process = cast(ManagedHarnessProcess, completed.process)
                    record.identity = ProcessIdentity(
                        completed.pid, completed.identity_marker
                    )
                    record.last_error = None
                    record.failures = 0
                    record.restart_after = None
                    record.restore_failed_terminal = False
                    self._report_readiness(key, "ready")
                    restarted += 1
            if self._is_failed(record):
                # Failed is terminal: nothing in reconcile ever puts the
                # harness back into the start queue.  The only ways out are
                # an explicit operator start and a start that actually
                # succeeds -- both clear the failure counter.  External
                # faults (quota exhausted, a dead peer) recover on a
                # hours-to-weeks scale, so automatic re-probing was noise,
                # and the alarm raised on entering failed ("explicit start
                # is required") now literally means it.
                continue
            if record.starting:
                continue
            process = record.process
            if process is not None and process.running:
                continue
            if process is not None:
                runtime_error = getattr(process, "last_error", None)
                if runtime_error:
                    record.last_error = str(runtime_error)
                old_identity = record.identity
                record.process = None
                record.identity = None
                record.generation += 1
                assert self._io is not None
                self._io.stop(
                    uuid.uuid4().hex,
                    key,
                    record.generation,
                    self._version,
                    process,
                    old_identity,
                )
                old_binding = record.liveness_binding
                record.liveness_binding = None
                if old_binding is not None:
                    self._io.call(
                        uuid.uuid4().hex,
                        record.generation,
                        self._version,
                        lambda binding=old_binding: binding.close(),
                    )
                if self._restart_backoff_seconds > 0:
                    record.restart_after = now + self._restart_backoff_seconds
                    continue
            if self._automatic_restore_allowed is not None:
                try:
                    allowed = self._automatic_restore_allowed(record.spec)
                except Exception:  # noqa: BLE001 - degraded policy restores as before
                    allowed = True
                if not allowed:
                    continue
            if record.restart_after is not None and now < record.restart_after:
                continue
            eligibility = self._restore_eligibility.get(record.spec.name)
            if eligibility is not None and not self._restore_entity_current(
                eligibility
            ):
                self._restore_eligibility.pop(record.spec.name, None)
                self._version += 1
                eligibility = None
            if (
                eligibility is not None
                and eligibility.suppressed
                and eligibility.desired_generation
                == harness_desired_generation(record.spec)
            ):
                continue
            if self._starts_in_flight() >= self._start_admission_width:
                # Same bound as restore, for the same reason: the process I/O
                # port rejects submissions past its capacity outright, and a
                # rejected start is charged a failure for a harness nothing
                # contacted.  Reconcile runs about once a second, so the
                # remainder simply comes up over the next few ticks.
                continue
            record.restart_after = None
            self._begin_start(
                key,
                record,
                "automatic",
                correlation_id=uuid.uuid4().hex,
            )
        self._emit_event(
            HarnessTimerCompleted(
                command.correlation_id,
                self._handler_generation,
                self._version,
                restarted,
            )
        )


    def _on_reconcile_session_refs(
        self, command: ReconcileHarnessSessionRefsCommand
    ) -> None:
        """Join native facts before persisting the current session refs."""

        from hyprial.daemon.impl.processes.process_owner  import ProcessOwner

        captured = tuple(
            (
                key, record.incarnation, record.generation,
                getattr(record.process, "process_token", None),
                record.spec.harness, record.spec.name, record.process,
            )
            for key, record in sorted(self._records.items())
        )
        if not any(entry[6] is not None for entry in captured):
            self._publish()
            self._complete_session_ref_reconcile(command, ())
            return

        def observe() -> tuple[_SessionRefObservation, ...]:
            observed = []
            for key, incarnation, generation, token, harness, name, process in captured:
                if isinstance(process, ProcessOwner):
                    value = process.observe_facts().session_ref
                else:
                    # Compatibility processes still run on ProcessIoPort's
                    # external I/O worker, never on the Harness mailbox.
                    value = getattr(process, "session_ref", None) if process else None
                observed.append(
                    _SessionRefObservation(
                        key, incarnation, generation, token, harness, name,
                        value if isinstance(value, str) and value else None,
                    )
                )
            return tuple(observed)

        correlation = self._register_call(
            f"{command.correlation_id}:observe:{uuid.uuid4().hex}",
            self._handler_generation,
            operation="session_refs",
            reply_correlation_id=command.correlation_id,
        )
        assert self._io is not None
        self._io.call(correlation, self._handler_generation, self._version, observe)


    def _complete_session_ref_reconcile(
        self,
        command: ReconcileHarnessSessionRefsCommand,
        refs: tuple[HarnessSessionRefProjection, ...],
    ) -> None:
        if refs and self._desired_state is not None:
            # Every pass observes every live ref; only a difference is worth
            # a turn on the single state writer.  The projection read stays
            # off the writer; a write still in flight is seen next pass.
            persisted = {
                (spec.harness, spec.name): spec.session_ref
                for spec in self._desired_state.load().harnesses
            }
            refs_changed = any(
                (item.harness, item.name) in persisted
                and persisted[(item.harness, item.name)] != item.session_ref
                for item in refs
            )
        else:
            refs_changed = bool(refs)
        if refs_changed:
            if self._desired_state is None:
                self._emit_event(
                    HarnessSessionRefsReconciled(
                        command.correlation_id,
                        self._handler_generation,
                        self._version,
                        refs,
                        "Harness session-ref authority requires DesiredStateStore",
                    )
                )
            else:
                pairs = tuple(
                    ((item.harness, item.name), item.session_ref) for item in refs
                )
                self._persist_desired(
                    DesiredStateOperation.SYNC_HARNESS_SESSION_REFS,
                    command,
                    (pairs,),
                    context=("session_refs", command, refs),
                )
            return
        self._emit_event(
            HarnessSessionRefsReconciled(
                command.correlation_id,
                self._handler_generation,
                self._version,
                refs,
                None,
            )
        )


    def _on_manage_desired(
        self,
        command: (
            StageHarnessDesiredCommand
            | SnapshotAdapterRegistrationCommand
            | RemoveAdapterRegistrationCommand
            | RestoreAdapterRegistrationCommand
        ),
    ) -> None:
        """Run offline-management registry work on the Harness authority."""

        operation = type(command).__name__
        store = self._desired_state
        if store is None:
            self._emit_event(
                HarnessDesiredStateManaged(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    operation,
                    False,
                    None,
                    error_detail=(
                        "Harness desired-state management requires DesiredStateStore"
                    ),
                )
            )
            return
        if isinstance(command, StageHarnessDesiredCommand):
            spec = _launch_spec(command.spec)
            before = next(
                (
                    item
                    for item in store.load().harnesses
                    if (item.harness, item.name) == (spec.harness, spec.name)
                ),
                None,
            )
            self._persist_desired(
                DesiredStateOperation.UPSERT_HARNESS,
                command,
                (spec,),
                context=("manage", command, operation, before != spec, None),
            )
            return
        if isinstance(command, SnapshotAdapterRegistrationCommand):
            try:
                state = store.load()
                spec = next(
                    (
                        item
                        for item in state.harnesses
                        if (item.harness, item.name) == ("lark", command.name)
                    ),
                    None,
                )
                legacy_pin = dict(state.channel_pins).get(command.name)
                adapter = HarnessAdapterRegistrationProjection(
                    command.name,
                    None if spec is None else _launch_projection(spec),
                    legacy_pin,
                )
                error = None
            except Exception as caught:  # noqa: BLE001 - typed actor result
                adapter = None
                error = caught
            metadata = (
                {}
                if error is None
                else {
                    "error_code": type(error).__name__,
                    "error_detail": str(error),
                    "error_is_oserror": isinstance(error, OSError),
                    "error_errno": error.errno if isinstance(error, OSError) else None,
                    "error_strerror": error.strerror if isinstance(error, OSError) else None,
                    "error_filename": error.filename if isinstance(error, OSError) else None,
                    "error_filename2": error.filename2 if isinstance(error, OSError) else None,
                }
            )
            self._emit_event(
                HarnessDesiredStateManaged(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    operation,
                    False,
                    adapter,
                    **metadata,
                )
            )
            return
        if isinstance(command, RemoveAdapterRegistrationCommand):
            expected = (
                None
                if command.expected_spec is None
                else _launch_spec(command.expected_spec)
            )
            adapter = HarnessAdapterRegistrationProjection(
                command.name,
                command.expected_spec,
                command.expected_legacy_pin,
            )
            self._persist_desired(
                DesiredStateOperation.REMOVE_ADAPTER_REGISTRATION,
                command,
                (command.name,),
                kwargs=(
                    ("expected_spec", expected),
                    ("expected_legacy_pin", command.expected_legacy_pin),
                ),
                context=(
                    "manage",
                    command,
                    operation,
                    expected is not None or command.expected_legacy_pin is not None,
                    adapter,
                ),
            )
            return
        assert isinstance(command, RestoreAdapterRegistrationCommand)
        restored = None if command.spec is None else _launch_spec(command.spec)
        adapter = HarnessAdapterRegistrationProjection(
            command.name, command.spec, command.legacy_pin
        )
        self._persist_desired(
            DesiredStateOperation.RESTORE_ADAPTER_REGISTRATION,
            command,
            (command.name,),
            kwargs=(("spec", restored), ("legacy_pin", command.legacy_pin)),
            context=(
                "manage",
                command,
                operation,
                restored is not None or command.legacy_pin is not None,
                adapter,
            ),
        )


    def _on_wait_ready(self, command: WaitHarnessReadyCommand) -> None:
        harness_id = f"{command.harness}:{command.name}"
        record = self._records.get(harness_id)
        process = record.process if record else None
        if process is None:
            self._emit_event(
                HarnessReadyObserved(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    harness_id,
                    False,
                )
            )
            return
        correlation = self._register_call(
            command.correlation_id,
            record.generation,
            operation="ready",
            harness_id=harness_id,
        )

        def wait_ready() -> bool:
            wait = getattr(process, "wait_ready", None)
            if callable(wait):
                return bool(wait(timeout=command.timeout_seconds))
            return bool(process.running)

        assert self._io is not None
        self._io.call(correlation, record.generation, self._version, wait_ready)
