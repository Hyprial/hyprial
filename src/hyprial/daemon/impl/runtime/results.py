"""DaemonEventBridge result settlement: claimed results, terminal settlement, blocking failure observation and retry."""

from __future__ import annotations
from .settlement import (
    FORWARD_UNAVAILABLE, HARNESS_FAILURE_BACKOFF_MS, HARNESS_FAILURE_MAX_ATTEMPTS,
    ForwardOutcome, _InflightAttempt, _reply_already_answered,
)
import queue
from dataclasses import replace
from typing import TYPE_CHECKING
from hyprial.daemon.impl.inbox import (
    InboxAuthorityTimeout,
    InboxAuthorityUnavailable,
)
from hyprial.daemon.impl.inbox.contracts.api import InboxMessage
from hyprial.daemon.impl.api  import (
    HarnessResult,
    HarnessResultStatus,
    classify_harness_failure,
    harness_failure_is_permanent,
)
from hyprial.daemon.impl.correlation.bounded_cadence  import CadenceCompleted
from hyprial.daemon.impl.harnesses.runtime.ports  import ClaimedHarnessResult, HarnessResultsClaimed
from hyprial.kernel import AdmissionResult
if TYPE_CHECKING:
    pass

class _BridgeResultsMixin:
    """DaemonEventBridge cluster; the composing class owns the state."""

    def _claimed_results_available(
        self, completion: CadenceCompleted[HarnessResultsClaimed]
    ) -> None:
        if completion.error is not None or completion.result is None:
            if self._logger is not None:
                self._logger(
                    "warn", "daemon", "harness.result_claim_failed",
                    error=completion.error,
                    detail=completion.detail,
                )
            return
        owner = self._result_settlement
        if owner is None:
            return
        for claim in completion.result.claims:
            admitted = owner.submit(claim)
            if admitted is AdmissionResult.OVERLOADED and self._logger is not None:
                self._logger(
                    "warn", "daemon", "harness.result_settlement_overloaded",
                    deliveryId=claim.result.delivery_id,
                )
        if completion.result.errors and self._logger is not None:
            self._logger(
                "warn", "daemon", "harness.result_claim_partial",
                errors=completion.result.errors,
            )

    def _settle_claimed_result(self, claim: ClaimedHarnessResult) -> bool:
        """Settle one claimed turn on a bounded effect worker.

        The Harness claim remains authoritative until both the inbox/PAC
        decision and the token-fenced Harness settle command confirm.
        """

        result = claim.result
        try:
            terminal = self._settle_claimed_result_business(result)
            if not terminal:
                return False
            settle = getattr(self.harnesses, "settle_result")
            settled = bool(settle(claim.claim_token, result.delivery_id))
            if settled and result.forward_to is not None:
                self._pending_forward_outcomes.pop(result.delivery_id, None)
                if self._forward_settlement is not None:
                    decision = self._forward_settlement.completed(
                        result.delivery_id
                    )
                    if decision is not None:
                        self._forward_settlement.retire(
                            result.delivery_id, decision.decision_token
                        )
            return settled
        except (NameError, ImportError):
            raise
        except Exception as error:
            if self._logger is not None:
                self._logger(
                    "warn", "daemon", "harness.result_settlement_deferred",
                    deliveryId=result.delivery_id,
                    errorType=type(error).__name__,
                    detail=str(error)[:300],
                )
            return False

    def _settle_claimed_result_business(self, result: HarnessResult) -> bool:
        self._queued_delivery_holds.pop(result.delivery_id, None)
        terminal_attempt = self._finish_attempt(result.delivery_id)
        if terminal_attempt is None:
            terminal_attempt = self._pending_workflow_attempts.get(result.delivery_id)
        self._pending_reply_results.pop(result.delivery_id, None)
        if self._workflow_outcome is not None:
            try:
                if self._workflow_outcome(result):
                    acknowledged = self.inbox.ack(
                        result.recipient, result.delivery_id
                    ).acknowledged
                    if acknowledged:
                        self._forget_turn_hook_delivery(result.delivery_id)
                        self._pending_workflow_results.pop(result.delivery_id, None)
                        self._pending_workflow_attempts.pop(result.delivery_id, None)
                    return acknowledged
                self._pending_workflow_results.pop(result.delivery_id, None)
                self._pending_workflow_attempts.pop(result.delivery_id, None)
            except (NameError, ImportError):
                raise
            except Exception as error:
                self._pending_workflow_results[result.delivery_id] = result
                if terminal_attempt is not None:
                    self._pending_workflow_attempts[result.delivery_id] = terminal_attempt
                if self._logger is not None:
                    self._logger(
                        "warn", "pac", "workflow.outcome_deferred",
                        messageId=result.delivery_id, detail=str(error),
                    )
                return False
        from hyprial.daemon.impl.pac.contracts.delivery import WITHDRAWN, delivery_current

        if result.failure_code == WITHDRAWN and not delivery_current(
            self.state_dir, result.delivery_id, now_ms=self._clock_ms()
        ):
            acknowledged = self.inbox.ack(
                result.recipient, result.delivery_id
            ).acknowledged
            if acknowledged:
                self._forget_turn_hook_delivery(result.delivery_id)
            return acknowledged
        if result.status is HarnessResultStatus.INTERRUPTED:
            return True
        if (
            result.forward_to is not None
            and self._forward_settlement is not None
            and result.delivery_id in self._pending_forward_outcomes
        ):
            # Native forwarding and its inbox decision already settled.  The
            # retained Harness claim is retrying only its token-fenced retire.
            return True
        original = next(
            (
                message
                for message in self.inbox.pending_messages(result.recipient)
                if message.message_id == result.delivery_id
            ),
            None,
        )
        if original is None:
            self._forget_turn_hook_delivery(result.delivery_id)
            if result.status is HarnessResultStatus.FAILED:
                fallback = self._failure_original(result.delivery_id)
                if fallback is not None:
                    self._loud_harness_failure(
                        result, fallback, attempt=terminal_attempt
                    )
                else:
                    self._log_missing_failure_route(result)
            return True
        if result.status is HarnessResultStatus.FAILED:
            settled = self._settle_failed_result(
                result, original, attempt=terminal_attempt
            )
            if not settled and terminal_attempt is not None:
                # The Harness authority still owns the frozen result claim.
                # Restore its process-custody identity so the next bounded
                # settlement attempt uses the same incarnation fence.
                self._inflight[result.delivery_id] = terminal_attempt
            return settled
        if result.forward_to is not None:
            if self._forwarder is None:
                self._settle_failed_result(
                    replace(
                        result,
                        status=HarnessResultStatus.FAILED,
                        error="this daemon cannot forward",
                        failure_code=FORWARD_UNAVAILABLE,
                    ),
                    original,
                    attempt=terminal_attempt,
                )
                return True
            forward_owner = self._forward_settlement
            completed_forward = (
                forward_owner.completed(result.delivery_id)
                if forward_owner is not None else None
            )
            if forward_owner is not None and completed_forward is None:
                admission = forward_owner.submit(original, result, terminal_attempt)
                if admission is AdmissionResult.OVERLOADED and self._logger is not None:
                    self._logger(
                        "warn", "daemon", "harness.forward_overloaded",
                        deliveryId=result.delivery_id,
                    )
                return False
            if completed_forward is not None:
                outcome = completed_forward.outcome
                terminal_attempt = completed_forward.attempt or terminal_attempt
            else:
                # Compatibility for directly constructed bridges without the
                # production actor composition.
                outcome = self._pending_forward_outcomes.get(result.delivery_id)
                if outcome is None:
                    try:
                        outcome = self._forwarder(
                            original, result.forward_to, result.output
                        )
                    except Exception as error:
                        outcome = ForwardOutcome(
                            False, "HARNESS_TRANSIENT_FAILURE",
                            f"forward raised {type(error).__name__}",
                        )
                    if outcome.accepted:
                        self._pending_forward_outcomes[result.delivery_id] = outcome
            if outcome.accepted:
                acknowledged = self.inbox.ack(
                    original.recipient, original.message_id
                ).acknowledged
                if acknowledged:
                    self._pending_forward_outcomes[result.delivery_id] = outcome
                    self._forget_turn_hook_delivery(result.delivery_id)
                return acknowledged
            failed = replace(
                result,
                status=HarnessResultStatus.FAILED,
                output="",
                error=outcome.error or outcome.failure_code,
                failure_code=outcome.failure_code or "HARNESS_TRANSIENT_FAILURE",
            )
            self._settle_failed_result(failed, original, attempt=terminal_attempt)
            if forward_owner is not None:
                self._pending_forward_outcomes[result.delivery_id] = outcome
            return True
        try:
            if original.intent == "reply":
                acknowledged = self.inbox.ack(
                    original.recipient, original.message_id
                ).acknowledged
            else:
                acknowledged = self._reply_and_ack(original, result)
        except (InboxAuthorityTimeout, InboxAuthorityUnavailable) as error:
            acknowledged = (
                self.inbox.ack(original.recipient, original.message_id).acknowledged
                if self._reply_already_settled(original, result, error)
                else False
            )
        if acknowledged:
            self._pending_reply_results.pop(result.delivery_id, None)
            self._forget_turn_hook_delivery(result.delivery_id)
        return acknowledged

    def _complete_harness_results(self) -> int:
        settled = 0
        # Forwards the pump finished since the last tick settle first, here,
        # under the same settlement owner as every other harness result.
        while True:
            try:
                original, result, outcome, attempt = self._forward_outcomes.get_nowait()
            except queue.Empty:
                break
            if outcome.accepted:
                self._finish_attempt(result.delivery_id)
                if self.inbox.ack(original.recipient, original.message_id).acknowledged:
                    self._forget_turn_hook_delivery(result.delivery_id)
                    settled += 1
                continue
            failed = replace(
                result,
                status=HarnessResultStatus.FAILED,
                output="",
                error=outcome.error or outcome.failure_code,
                failure_code=outcome.failure_code or "HARNESS_TRANSIENT_FAILURE",
            )
            if self._settle_failed_result(failed, original, attempt=attempt):
                settled += 1
        results = [
            *self._pending_workflow_results.values(),
            *self._pending_reply_results.values(),
            *self.harnesses.drain_results(),
        ]
        for result in results:
            # The worker released this delivery from its queue before exposing
            # the result.  Reply/ack settlement has its own retry ownership.
            self._queued_delivery_holds.pop(result.delivery_id, None)
            # A terminal turn ends this attempt's silence budget regardless of
            # whether delivery settlement is accepted, deferred, retried, or
            # rejected because its PAC node has already closed.  Any later
            # retry registers a new generation when that retry actually starts.
            terminal_attempt = self._finish_attempt(result.delivery_id)
            if terminal_attempt is None:
                terminal_attempt = self._pending_workflow_attempts.get(
                    result.delivery_id
                )
            # Re-added below only if its reply fails to confirm again.
            self._pending_reply_results.pop(result.delivery_id, None)
            if self._workflow_outcome is not None:
                try:
                    handled = self._workflow_outcome(result)
                    if handled:
                        self.inbox.ack(result.recipient, result.delivery_id)
                        self._forget_turn_hook_delivery(result.delivery_id)
                        self._pending_workflow_results.pop(result.delivery_id, None)
                        self._pending_workflow_attempts.pop(result.delivery_id, None)
                        settled += 1
                        continue
                    self._pending_workflow_results.pop(result.delivery_id, None)
                    self._pending_workflow_attempts.pop(result.delivery_id, None)
                except (NameError, ImportError):
                    raise
                except Exception as error:
                    self._pending_workflow_results[result.delivery_id] = result
                    if terminal_attempt is not None:
                        self._pending_workflow_attempts[result.delivery_id] = (
                            terminal_attempt
                        )
                    if self._logger:
                        self._logger(
                            "warn",
                            "pac",
                            "workflow.outcome_deferred",
                            messageId=result.delivery_id,
                            detail=str(error),
                        )
                    continue
            from hyprial.daemon.impl.pac.contracts.delivery import WITHDRAWN, delivery_current

            if result.failure_code == WITHDRAWN and not delivery_current(
                self.state_dir, result.delivery_id, now_ms=self._clock_ms()
            ):
                self.inbox.ack(result.recipient, result.delivery_id)
                self._finish_attempt(result.delivery_id)
                self._forget_turn_hook_delivery(result.delivery_id)
                settled += 1
                continue
            if result.status is HarnessResultStatus.INTERRUPTED:
                continue
            original = next(
                (
                    message
                    for message in self.inbox.pending_messages(result.recipient)
                    if message.message_id == result.delivery_id
                ),
                None,
            )
            if original is None:
                self._forget_turn_hook_delivery(result.delivery_id)
                # The row can be fetched/consumed (or already carry a terminal
                # settlement) before this failure lands.  That used to be a
                # silent drop; the authoritative sender is still durable in
                # the settlement tombstone, so look it up and report instead.
                if result.status is HarnessResultStatus.FAILED:
                    fallback = self._failure_original(result.delivery_id)
                    if fallback is not None:
                        self._loud_harness_failure(
                            result, fallback, attempt=terminal_attempt
                        )
                        continue
                    self._log_missing_failure_route(result)
                continue
            if result.status is HarnessResultStatus.FAILED:
                if self._settle_failed_result(
                    result, original, attempt=terminal_attempt
                ):
                    settled += 1
                continue
            if result.forward_to is not None:
                # Before the reply-intent short-circuit below: a person
                # answering in-thread arrives as a reply, and acking it
                # without sending would drop the forward silently.  Forward
                # settlement continues independently after the turn's silence
                # clock has finished.
                if self._forwarder is None:
                    if self._settle_failed_result(
                        replace(
                            result,
                            status=HarnessResultStatus.FAILED,
                            error="this daemon cannot forward",
                            failure_code=FORWARD_UNAVAILABLE,
                        ),
                        original,
                        attempt=terminal_attempt,
                    ):
                        settled += 1
                    continue
                self._start_forward_thread()
                self._forward_jobs.put((original, result, terminal_attempt))
                continue
            self._finish_attempt(result.delivery_id)
            try:
                if original.intent == "reply":
                    acknowledged = self.inbox.ack(
                        original.recipient, original.message_id
                    ).acknowledged
                else:
                    acknowledged = self._reply_and_ack(original, result)
            except (InboxAuthorityTimeout, InboxAuthorityUnavailable) as error:
                # Settled per result: raising here used to abort the whole
                # tick after drain_results() had already released every
                # result in the batch, so the rest of the batch was lost.
                acknowledged = (
                    self.inbox.ack(original.recipient, original.message_id).acknowledged
                    if self._reply_already_settled(original, result, error)
                    else False
                )
            if acknowledged:
                self._forget_turn_hook_delivery(result.delivery_id)
                settled += 1
        return settled

    def _reply_already_settled(
        self,
        original: InboxMessage,
        result: HarnessResult,
        error: InboxAuthorityTimeout | InboxAuthorityUnavailable,
    ) -> bool:
        """Classify an answer whose reply or ack did not confirm on this tick.

        True: the delivery is already durably answered -- the reply id is
        derived from the delivery, so a receipt conflict means a reply
        committed (after its submit timed out) and a re-run turn produced
        different text; the caller acks it, nothing is answered again.
        False: any other authority error; the answer is kept and the SAME
        reply is retried next tick.
        """

        if _reply_already_answered(error):
            if self._logger is not None:
                self._logger(
                    "warn",
                    "daemon",
                    "harness.reply.already_answered",
                    messageId=original.message_id,
                    recipient=original.recipient,
                )
            return True
        first_deferral = result.delivery_id not in self._pending_reply_results
        self._pending_reply_results[result.delivery_id] = result
        if first_deferral and self._logger is not None:
            self._logger(
                "warn",
                "daemon",
                "harness.reply.deferred",
                messageId=original.message_id,
                recipient=original.recipient,
                errorType=type(error).__name__,
                detail=str(error)[:300],
            )
        return False

    def _settle_failed_result(
        self,
        result: HarnessResult,
        original: InboxMessage,
        *,
        attempt: _InflightAttempt | None = None,
    ) -> bool:
        """Settle one FAILED turn against its still-pending inbox row."""

        failure_code = result.failure_code or classify_harness_failure(result.error)
        if failure_code in {
            "PROVIDER_USAGE_LIMIT",
            "PROVIDER_AUTHENTICATION_FAILED",
        }:
            admission = self._observe_blocking_failure(
                original.recipient,
                failure_code,
                None if attempt is None else attempt.agent_entity_token,
            )
            if admission is not AdmissionResult.ACCEPTED:
                return False
        try:
            failure = self.inbox.settle_harness_failure(
                original.recipient,
                original.message_id,
                failure_code,
                permanent=harness_failure_is_permanent(failure_code),
                max_attempts=HARNESS_FAILURE_MAX_ATTEMPTS,
                backoff_ms=HARNESS_FAILURE_BACKOFF_MS,
                now_ms=self._clock_ms(),
            )
        except KeyError:
            # A concurrent public ack can retire the row after the
            # read above.  That is a completed ownership decision,
            # not a reason to crash/restart the inbox authority.
            self._forget_turn_hook_delivery(result.delivery_id)
            return False
        if self._logger is not None:
            self._logger(
                "error" if failure.terminal else "warn",
                "daemon",
                (
                    "harness.delivery.terminal_failed"
                    if failure.terminal
                    else "harness.delivery.retry_scheduled"
                ),
                messageId=failure.message_id,
                recipient=failure.recipient,
                failureCode=failure.failure_code,
                attempts=failure.attempts,
                maxAttempts=failure.max_attempts,
                permanent=failure.permanent,
                terminal=failure.terminal,
                **(
                    {"terminalReason": failure.terminal_reason}
                    if failure.terminal_reason is not None
                    else {"nextAttemptMs": failure.next_attempt_ms}
                ),
            )
        if (
            failure.failure_code == "PROVIDER_USAGE_LIMIT"
            and self._usage_limit_observer is not None
        ):
            try:
                self._usage_limit_observer(failure.recipient)
            except Exception as error:  # noqa: BLE001 - an observer never breaks settlement
                if self._logger is not None:
                    self._logger(
                        "error",
                        "daemon",
                        "quota_watchdog.observe_failed",
                        recipient=failure.recipient,
                        error=type(error).__name__,
                    )
        if (
            failure.failure_code
            in {"PROVIDER_USAGE_LIMIT", "PROVIDER_AUTHENTICATION_FAILED"}
            and self._blocking_failure_observer is not None
            and self._blocking_failure_identity is None
        ):
            # Legacy explicitly injected observers have no identity port.
            # Actor composition supplies it and uses the retained three-argument path.
            try:
                self._blocking_failure_observer(
                    failure.recipient, failure.failure_code
                )
            except Exception as error:  # noqa: BLE001 - observer never breaks settlement
                if self._logger is not None:
                    self._logger(
                        "error",
                        "daemon",
                        "agent.block.observe_failed",
                        recipient=failure.recipient,
                        error=type(error).__name__,
                    )
        # The sender that is waiting for this receipt is told with
        # this attempt's own evidence.  The owner's quota watchdog
        # above is a separate, retained channel and does not stand in
        # for this one (the 2026-09-21 incident: the owner was told
        # within a second and the sender was told nothing).
        self._loud_harness_failure(result, original, failure=failure, attempt=attempt)
        self._finish_attempt(result.delivery_id)
        if failure.terminal:
            self._forget_turn_hook_delivery(result.delivery_id)
        return True

    def _observe_blocking_failure(
        self, recipient: str, code: str, entity_token: str | None
    ) -> AdmissionResult:
        observer = self._blocking_failure_observer
        if observer is None:
            return AdmissionResult.ACCEPTED
        if not entity_token:
            if self._logger is not None:
                self._logger(
                    "error",
                    "daemon",
                    "agent.block.identity_unavailable",
                    recipient=recipient,
                )
            return AdmissionResult.ACCEPTED
        key = (recipient, code, entity_token)
        try:
            admission = observer(recipient, code, entity_token)
        except Exception as error:  # noqa: BLE001 - settlement already committed
            admission = AdmissionResult.OVERLOADED
            if self._logger is not None:
                self._logger(
                    "error",
                    "daemon",
                    "agent.block.observe_failed",
                    recipient=recipient,
                    error=type(error).__name__,
                )
        if admission in {None, AdmissionResult.ACCEPTED}:
            with self._blocking_failure_lock:
                self._pending_blocking_failures.pop(key, None)
            return AdmissionResult.ACCEPTED
        if admission is AdmissionResult.CLOSED:
            return AdmissionResult.CLOSED
        with self._blocking_failure_lock:
            if key in self._pending_blocking_failures:
                self._pending_blocking_failures.move_to_end(key)
                return AdmissionResult.ACCEPTED
            full = (
                len(self._pending_blocking_failures)
                >= self._blocking_failure_capacity
            )
            if not full:
                self._pending_blocking_failures[key] = None
                return AdmissionResult.ACCEPTED
        if full:
            if self._logger is not None:
                self._logger(
                    "error",
                    "daemon",
                    "agent.block.observer_capacity_exhausted",
                    capacity=self._blocking_failure_capacity,
                    recipient=recipient,
                )
            return AdmissionResult.OVERLOADED
        return AdmissionResult.OVERLOADED

    def _retry_blocking_failures(self) -> None:
        observer = self._blocking_failure_observer
        if observer is None:
            with self._blocking_failure_lock:
                self._pending_blocking_failures.clear()
            return
        with self._blocking_failure_lock:
            pending = tuple(self._pending_blocking_failures)
        for recipient, code, entity_token in pending:
            try:
                admission = observer(recipient, code, entity_token)
            except Exception:
                admission = AdmissionResult.OVERLOADED
            if admission in {None, AdmissionResult.ACCEPTED}:
                with self._blocking_failure_lock:
                    self._pending_blocking_failures.pop(
                        (recipient, code, entity_token), None
                    )
