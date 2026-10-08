"""Desired-state persistence and event emission behaviour."""

from __future__ import annotations

import threading
import uuid
from hyprial.kernel import AdmissionResult
from hyprial.kernel import capped_exponential
from hyprial.daemon.impl.desired_state_io  import (
    DesiredStateIoCompleted,
    DesiredStateIoRequest,
    DesiredStateOperation,
)
from hyprial.daemon.impl.harnesses.runtime.ports  import (
    EnsureHarnessCommand,
    HarnessDesiredStateManaged,
    HarnessMutationCompleted,
    HarnessSessionRefsReconciled,
    RemoveHarnessCommand,
)
from hyprial.kernel import PortCommandRejected

from ..contracts import _launch_spec

class _DesiredBehavior:
    """Desired-state persistence and event emission behaviour."""


    def _persist_desired(
        self,
        operation: DesiredStateOperation,
        command: object,
        args: tuple[object, ...],
        *,
        kwargs: tuple[tuple[str, object], ...] = (),
        context: object,
    ) -> bool:
        persistence = self._persistence
        if persistence is None:
            self._reject_correlation(
                str(getattr(command, "correlation_id", "")),
                "HARNESS_PERSISTENCE_UNAVAILABLE",
                "Harness desired-state persistence is unavailable",
            )
            return False
        request = DesiredStateIoRequest(
            operation_id=f"harness-state-{uuid.uuid4().hex}",
            owner_generation=self._handler_generation,
            owner_version=self._version,
            operation=operation,
            args=args,
            kwargs=kwargs,
            context=context,
        )
        admission = persistence.submit(request)
        if admission is AdmissionResult.ACCEPTED:
            return True
        self._reject_correlation(
            str(getattr(command, "correlation_id", "")),
            "HARNESS_PERSISTENCE_OVERLOADED",
            f"Harness persistence admission is {admission.value}",
        )
        return False


    def _on_desired_completed(self, completion: DesiredStateIoCompleted) -> None:
        request = completion.request
        context = request.context
        if not isinstance(context, tuple) or len(context) < 2:
            raise TypeError("Harness persistence completion has no typed context")
        kind = context[0]
        command = context[1]
        if (
            request.owner_generation > self._handler_generation
            or request.owner_version > self._version
        ):
            self._reject_correlation(
                str(getattr(command, "correlation_id", "")),
                "HARNESS_PERSISTENCE_FENCE_INVALID",
                "Harness persistence completion is from a future owner fence",
            )
            return
        if completion.error_code is not None:
            self._handle_desired_error(kind, context, command, completion)
            return
        if kind == 'receipt':
            self._handle_desired_receipt(context, command, completion)
            return
        if kind == 'mark_failed':
            self._handle_desired_mark_failed(context, command, completion)
            return
        if kind == 'restore_mark_failed':
            self._handle_desired_restore_mark_failed(context, command, completion)
            return
        if kind == 'lifecycle_rejection_count':
            self._handle_desired_lifecycle_rejection_count(context, command, completion)
            return
        if kind == 'failed_removal':
            self._handle_desired_failed_removal(context, command, completion)
            return
        if kind == 'failed_ensure':
            self._handle_desired_failed_ensure(context, command, completion)
            return
        if kind == 'terminalize':
            self._handle_desired_terminalize(context, command, completion)
            return
        if kind == 'session_refs':
            self._handle_desired_session_refs(context, command, completion)
            return
        if kind == 'manage':
            self._handle_desired_manage(context, command, completion)
            return
        if kind == 'lifecycle_apply':
            self._handle_desired_lifecycle_apply(context, command, completion)
            return
        if kind == 'fail_apply':
            self._handle_desired_fail_apply(context, command, completion)
            return
        if kind == 'lifecycle_noop':
            self._handle_desired_lifecycle_noop(context, command, completion)
            return
        if kind == 'lifecycle_finalize':
            self._handle_desired_lifecycle_finalize(context, command, completion)
            return
        raise TypeError(f"unsupported Harness persistence context: {kind}")

    def _handle_desired_error(
        self,
        kind: object,
        context: tuple[object, ...],
        command: object,
        completion: DesiredStateIoCompleted,
    ) -> None:
        if kind == "failed_ensure":
            self._lifecycle_ensure_settling.discard(context[1])
        if kind == "restore_mark_failed":
            self._fail_restore_persistence(
                context[1],
                context[2],
                "HARNESS_PERSISTENCE_FAILED",
                f"{completion.error_code}: {completion.error_detail}",
            )
            return
        if kind == "mark_failed":
            return
        if kind == "session_refs":
            self._publish_event(
                HarnessSessionRefsReconciled(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    context[2],
                    f"{completion.error_code}: {completion.error_detail}",
                )
            )
            return
        if kind == "manage":
            self._publish_event(
                HarnessDesiredStateManaged(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    context[2],
                    False,
                    context[4],
                    error_code=completion.error_code,
                    error_detail=completion.error_detail,
                    error_is_oserror=completion.error_is_oserror,
                    error_errno=completion.error_errno,
                    error_strerror=completion.error_strerror,
                    error_filename=completion.error_filename,
                    error_filename2=completion.error_filename2,
                )
            )
            return
        self._reject_correlation(
            str(getattr(command, "correlation_id", "")),
            "HARNESS_PERSISTENCE_FAILED",
            f"{completion.error_code}: {completion.error_detail}",
        )
        return
    def _handle_desired_receipt(
        self,
        context: tuple[object, ...],
        command: object,
        completion: DesiredStateIoCompleted,
    ) -> None:
        from hyprial.daemon.impl.lifecycle.lifecycle_receipts  import (
            LifecycleReceiptCompleted,
        )

        self._publish_event(
            LifecycleReceiptCompleted(
                command.correlation_id,
                self._handler_generation,
                self._version,
                "harness",
                command.attempt_token,
                command.resource_token,
                context[2],
                bool(completion.result),
            )
        )
        return
    def _handle_desired_mark_failed(
        self,
        context: tuple[object, ...],
        command: object,
        completion: DesiredStateIoCompleted,
    ) -> None:

        return
    def _handle_desired_restore_mark_failed(
        self,
        context: tuple[object, ...],
        command: object,
        completion: DesiredStateIoCompleted,
    ) -> None:

        batch_id, key, record_incarnation, record_generation = context[1:5]
        batch = self._restore_batches.get(batch_id)
        if batch is None or key not in batch.pending:
            return
        harness, name = key.split(":", 1)
        committed = (
            completion.result is True
            and any(
                spec.harness == harness
                and spec.name == name
                and spec.status == "failed"
                for spec in completion.snapshot.harnesses
            )
        )
        if not committed:
            self._fail_restore_persistence(
                batch_id,
                key,
                "HARNESS_PERSISTENCE_FAILED",
                f"Failed restore status was not committed for {key}",
            )
            return
        record = self._records.get(key)
        if (
            record is not None
            and record.incarnation == record_incarnation
            and record.generation == record_generation
            and self._is_failed(record)
        ):
            self._failed_events.append(key)
            self._report_readiness(key, "failed")
        self._settle_restore_key(batch_id, key, success=False)
        return
    def _handle_desired_lifecycle_rejection_count(
        self,
        context: tuple[object, ...],
        command: object,
        completion: DesiredStateIoCompleted,
    ) -> None:

        self._continue_lifecycle_rejection(
            context[1], context[2], context[3], completion.result
        )
        return
    def _handle_desired_failed_removal(
        self,
        context: tuple[object, ...],
        command: object,
        completion: DesiredStateIoCompleted,
    ) -> None:

        failure = self._finish_failed_removal(
            context[1],
            context[2],
            context[3],
            context[4],
            bool(completion.result),
        )
        self._publish_event(failure)
        control_correlation = context[5]
        if isinstance(control_correlation, str):
            from hyprial.daemon.impl.lifecycle.lifecycle_receipts  import HarnessLifecycleFailureSettled

            self._publish_event(
                HarnessLifecycleFailureSettled(control_correlation, failure)
            )
        return
    def _handle_desired_failed_ensure(
        self,
        context: tuple[object, ...],
        command: object,
        completion: DesiredStateIoCompleted,
    ) -> None:

        attempt_token = context[1]
        self._lifecycle_ensure_settling.discard(attempt_token)
        fence = self._lifecycle_ensure_failure_fences.pop(
            attempt_token, None
        )
        if fence is None:
            return
        failure = self._finish_failed_ensure(
            fence.request,
            fence.provenance,
            fence.code,
            fence.detail,
            bool(completion.result),
        )
        self._publish_event(failure)
        from hyprial.daemon.impl.lifecycle.lifecycle_receipts  import HarnessLifecycleFailureSettled
        for correlation in fence.control_correlations:
            self._publish_event(
                HarnessLifecycleFailureSettled(correlation, failure)
            )
        return
    def _handle_desired_terminalize(
        self,
        context: tuple[object, ...],
        command: object,
        completion: DesiredStateIoCompleted,
    ) -> None:

        from hyprial.daemon.impl.lifecycle.lifecycle_receipts  import (
            LifecycleMutationFailed,
            TerminalizeHarnessLifecycleCommand,
        )
        terminalize, receipt, lifecycle_request, remaining, requests = context[1:6]
        assert isinstance(terminalize, TerminalizeHarnessLifecycleCommand)
        resource_id = receipt.resource_key.removeprefix("harness:")
        correlation_id = receipt.correlation_id
        if correlation_id is None and lifecycle_request is not None:
            raw = getattr(lifecycle_request, "correlation_id", None)
            correlation_id = raw if isinstance(raw, str) else None
        if lifecycle_request is not None and isinstance(
            lifecycle_request.payload, RemoveHarnessCommand
        ):
            self._publish_event(
                self._finish_failed_removal(
                    lifecycle_request,
                    receipt.provenance,
                    terminalize.code,
                    terminalize.detail,
                    bool(completion.result),
                )
            )
        else:
            self._remember_settled_lifecycle(receipt.attempt_token)
            if correlation_id is not None:
                self._publish_event(
                    LifecycleMutationFailed(
                        correlation_id,
                        receipt.attempt_token,
                        self._handler_generation,
                        self._version,
                        "harness",
                        terminalize.code,
                        terminalize.detail,
                        bool(completion.result),
                    )
                )
            self._lifecycle_effect_resources.discard(resource_id)
            self._lifecycle_effect_requests.pop(resource_id, None)
        self._terminalize_next(terminalize, remaining, requests)
        return
    def _handle_desired_session_refs(
        self,
        context: tuple[object, ...],
        command: object,
        completion: DesiredStateIoCompleted,
    ) -> None:

        self._publish_event(
            HarnessSessionRefsReconciled(
                command.correlation_id,
                self._handler_generation,
                self._version,
                context[2],
                None,
            )
        )
        return
    def _handle_desired_manage(
        self,
        context: tuple[object, ...],
        command: object,
        completion: DesiredStateIoCompleted,
    ) -> None:

        self._publish_event(
            HarnessDesiredStateManaged(
                command.correlation_id,
                self._handler_generation,
                self._version,
                context[2],
                bool(context[3]),
                context[4],
                None,
            )
        )
        return
    def _handle_desired_lifecycle_apply(
        self,
        context: tuple[object, ...],
        command: object,
        completion: DesiredStateIoCompleted,
    ) -> None:

        self._continue_lifecycle_apply(command, completion.result)
        return
    def _handle_desired_fail_apply(
        self,
        context: tuple[object, ...],
        command: object,
        completion: DesiredStateIoCompleted,
    ) -> None:
        from hyprial.kernel  import (
            MutationProvenance,
        )

        from hyprial.daemon.impl.lifecycle.lifecycle_receipts  import FailHarnessLifecycleCommand
        assert isinstance(command, FailHarnessLifecycleCommand)
        if (
            not isinstance(completion.result, tuple)
            or len(completion.result) != 2
            or not isinstance(completion.result[0], MutationProvenance)
        ):
            raise TypeError("Harness failure persistence returned invalid result")
        provenance = completion.result[0]
        lifecycle_request = command.request
        if isinstance(lifecycle_request.payload, EnsureHarnessCommand):
            self._fence_failed_ensure(
                command,
                lifecycle_request,
                provenance,
                command.code,
                command.detail,
            )
        else:
            self._begin_failed_removal(
                lifecycle_request,
                provenance,
                command.code,
                command.detail,
                control_correlation=command.correlation_id,
            )
        return
    def _handle_desired_lifecycle_noop(
        self,
        context: tuple[object, ...],
        command: object,
        completion: DesiredStateIoCompleted,
    ) -> None:
        from hyprial.kernel  import (
            MutationProvenance,
        )
        from hyprial.kernel import LifecycleMutationCompleted
        from hyprial.kernel import LifecycleMutationRequest

        lifecycle_request, provenance, base = context[1:4]
        assert isinstance(lifecycle_request, LifecycleMutationRequest)
        assert isinstance(provenance, MutationProvenance)
        if completion.result is not True:
            self._reject_correlation(
                lifecycle_request.correlation_id,
                "HARNESS_LIFECYCLE_RECEIPT_MISMATCH",
                "Harness lifecycle receipt could not settle",
            )
            return
        self._remember_settled_lifecycle(lifecycle_request.attempt_token)
        self._publish_event(
            LifecycleMutationCompleted(
                lifecycle_request.correlation_id,
                lifecycle_request.attempt_token,
                self._handler_generation,
                self._version,
                "harness",
                provenance,
                base,
            )
        )
        return
    def _handle_desired_lifecycle_finalize(
        self,
        context: tuple[object, ...],
        command: object,
        completion: DesiredStateIoCompleted,
    ) -> None:
        from hyprial.kernel  import (
            MutationProvenance,
        )
        from hyprial.kernel import LifecycleMutationCompleted
        from hyprial.kernel import LifecycleMutationRequest

        lifecycle_request, provenance, event = context[1:4]
        assert isinstance(lifecycle_request, LifecycleMutationRequest)
        assert isinstance(provenance, MutationProvenance)
        assert isinstance(event, HarnessMutationCompleted)
        if completion.result is not True:
            self._reject_correlation(
                lifecycle_request.correlation_id,
                "HARNESS_LIFECYCLE_RECEIPT_MISMATCH",
                "Harness lifecycle receipt could not settle",
            )
            return
        self._pop_lifecycle_pending(event.correlation_id)
        self._remember_settled_lifecycle(lifecycle_request.attempt_token)
        retry = self._lifecycle_retry_timers.pop(event.correlation_id, None)
        if retry is not None:
            retry.cancel()
        self._lifecycle_effect_resources.discard(event.harness_id)
        self._lifecycle_effect_requests.pop(event.harness_id, None)
        base = HarnessMutationCompleted(
            event.correlation_id,
            event.generation,
            event.version,
            event.harness_id,
            provenance.changed,
        )
        self._publish_event(
            LifecycleMutationCompleted(
                lifecycle_request.correlation_id,
                lifecycle_request.attempt_token,
                event.generation,
                event.version,
                "harness",
                provenance,
                base,
            )
        )
        return

    def _continue_lifecycle_rejection(
        self,
        request: object,
        provenance: object,
        event: object,
        result: object,
    ) -> None:
        from hyprial.kernel  import MutationProvenance
        from hyprial.kernel import LifecycleMutationRequest

        assert isinstance(request, LifecycleMutationRequest)
        assert isinstance(provenance, MutationProvenance)
        assert isinstance(event, PortCommandRejected)
        if not isinstance(result, int):
            raise TypeError("Harness lifecycle failure count must be an integer")
        correlation_id = event.correlation_id
        retry_budget = self._failure_budget or 3
        if result < retry_budget and not self._closing:
            self._pop_lifecycle_pending(correlation_id)
            delay = min(
                capped_exponential(
                    self._lifecycle_retry_base_seconds,
                    1.0,
                    max(0, result - 1),
                ),
                1.0,
            )
            prior = self._lifecycle_retry_timers.pop(correlation_id, None)
            if prior is not None:
                prior.cancel()

            def retry() -> None:
                self._lifecycle_retry_timers.pop(correlation_id, None)
                self._emit_completion(request)

            timer = threading.Timer(delay, retry)
            timer.daemon = True
            self._lifecycle_retry_timers[correlation_id] = timer
            timer.start()
            return
        if self._closing:
            # Close owns terminalization of incomplete receipts. A late
            # failure-count completion must not publish a competing outcome.
            self._pop_lifecycle_pending(correlation_id)
            self._remember_settled_lifecycle(request.attempt_token)
            return
        if isinstance(request.payload, RemoveHarnessCommand):
            self._pop_lifecycle_pending(correlation_id)
            self._remember_settled_lifecycle(request.attempt_token)
            self._begin_failed_removal(
                request, provenance, event.code, event.detail
            )
            return
        self._fence_failed_ensure(
            None,
            request,
            provenance,
            event.code,
            event.detail,
        )


    def _emit_event(self, event: object) -> None:
        from hyprial.kernel  import (
            MutationProvenance,
        )
        from hyprial.kernel import LifecycleMutationRequest

        correlation_id = str(getattr(event, "correlation_id", ""))
        pending = self._lifecycle_pending.get(correlation_id)
        if pending is not None and isinstance(event, PortCommandRejected):
            request, provenance = pending
            assert isinstance(request, LifecycleMutationRequest)
            assert isinstance(provenance, MutationProvenance)
            self._persist_desired(
                DesiredStateOperation.RECORD_HARNESS_LIFECYCLE_FAILURE,
                request,
                (request.attempt_token, provenance.resource_token),
                context=(
                    "lifecycle_rejection_count",
                    request,
                    provenance,
                    event,
                ),
            )
            return
        elif pending is not None and isinstance(event, HarnessMutationCompleted):
            request, provenance = pending
            assert isinstance(request, LifecycleMutationRequest)
            assert isinstance(provenance, MutationProvenance)
            if isinstance(request.payload, EnsureHarnessCommand):
                # U0b: the process is up (this event IS the start-success
                # settlement), so THIS is where the desired-state row is
                # earned.  The spec that actually started is the actor's
                # record; the payload spec is the fallback when the record
                # was superseded mid-flight.
                record = self._records.get(event.harness_id)
                confirmed_spec = (
                    record.spec
                    if record is not None
                    else _launch_spec(request.payload.spec)
                )
                operation = DesiredStateOperation.CONFIRM_HARNESS_LIFECYCLE
                args = (
                    request.attempt_token,
                    provenance.resource_token,
                    confirmed_spec,
                )
            else:
                operation = DesiredStateOperation.COMPLETE_HARNESS_LIFECYCLE
                args = (request.attempt_token, provenance.resource_token)
            self._persist_desired(
                operation,
                request,
                args,
                kwargs=(
                    ("generation", event.generation),
                    ("version", event.version),
                ),
                context=(
                    "lifecycle_finalize",
                    request,
                    provenance,
                    event,
                ),
            )
            return
        self._publish_event(event)


    def _publish_event(self, event: object) -> None:
        for sink in tuple(self._event_sinks):
            publish = getattr(sink, "publish", None)
            if callable(publish):
                publish(event)
            elif callable(sink):
                sink(event)
            else:
                raise TypeError("event sink must be callable or expose publish(event)")
