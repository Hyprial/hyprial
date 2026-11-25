"""DaemonEventBridge result settlement: claimed results, terminal settlement, blocking failure observation and retry."""

from __future__ import annotations
from .settlement import (
    HARNESS_FAILURE_BACKOFF_MS, HARNESS_FAILURE_MAX_ATTEMPTS, _InflightAttempt,
)
from .cell_results import (
    normalize_root_result,
    pop_cell_hop,
    release_held_cell_origin,
    settle_cell_result,
)
from typing import TYPE_CHECKING, Any
from hyprial.daemon.impl.inbox import (
    InboxAuthorityTimeout,
    InboxAuthorityUnavailable,
)
from hyprial.daemon.impl.inbox.contracts.api import InboxMessage
from hyprial.daemon.impl.inbox.contracts.settlement import settle_hint_for
from hyprial.daemon.impl.harnesses.turn_delivery.protocol import (
    is_mechanism_hook_request,
    mechanism_of,
)
from hyprial.kernel import DECLARED_HARNESSES
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

_STRANDED_SWEEP_LIMIT = 256


#: Failure codes that block the agent's whole mailbox, not just the failed
#: delivery.  Deliberately EXCLUDES ``PROVIDER_RATE_LIMITED``: a 429 window
#: resets, and a registry block has no expiry, so mapping it here would
#: disable the agent until a human ran ``agent unblock``.  The rate-limited
#: code is permanent for its own delivery only.
_BLOCKING_FAILURE_CODES = frozenset(
    {
        "PROVIDER_USAGE_LIMIT",
        "PROVIDER_AUTHENTICATION_FAILED",
    }
)


def _worker_skip(result: HarnessResult) -> bool:
    from hyprial.daemon.impl.pac.contracts.delivery import (
        ALREADY_CONSUMED,
        EXPIRED,
        TERMINALLY_FAILED,
    )

    return (
        result.status is HarnessResultStatus.INTERRUPTED
        and result.failure_code in {ALREADY_CONSUMED, EXPIRED, TERMINALLY_FAILED}
    )


class _BridgeResultsMixin:
    """DaemonEventBridge cluster; the composing class owns the state."""

    def _release_origin_claims_if_present(self, message_ids: tuple[str, ...]) -> None:
        release = getattr(self, "_release_origin_claims", None)
        if callable(release):
            release(message_ids)

    def _forget_origin_claim_if_present(self, message_id: str) -> None:
        forget = getattr(self, "_forget_origin_claim_record", None)
        if callable(forget):
            forget(message_id)

    def _fetch_completed_turn_origin(self, original: InboxMessage) -> None:
        fetch_origin = getattr(self, "_fetch_with_headless_origin", None)
        if callable(fetch_origin):
            fetch_origin(
                original.recipient, (original.message_id,), completed_turn=True
            )
            return
        self.inbox.fetch_pending(
            original.recipient,
            only=frozenset({original.message_id}),
            completed_turn=True,
        )

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
            return bool(settle(claim.claim_token, result.delivery_id))
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
        release_batch_claim = getattr(self, "_release_batch_claim", None)
        if callable(release_batch_claim):
            release_batch_claim(result.delivery_id, result.message_ids)
        if result.message_ids:
            remaining = self._pending_batch_members.get(result.delivery_id)
            pending = self._pending_batch_members[result.delivery_id] = {
                member.delivery_id: member
                for member in self._batch_member_results(result)
                if remaining is None or member.delivery_id in remaining
            }
            for member, member_result in tuple(pending.items()):
                if self._settle_claimed_result_member(member_result):
                    pending.pop(member, None)
            if pending:
                return False
            self._pending_batch_members.pop(result.delivery_id, None)
            return True
        return self._settle_claimed_result_member(result)

    def _settle_claimed_result_member(self, result: HarnessResult) -> bool:
        """Apply the pre-batch classifier to one inbox member."""

        self._queued_delivery_holds.pop(result.delivery_id, None)
        terminal_attempt = self._finish_attempt(result.delivery_id)
        if terminal_attempt is None:
            terminal_attempt = self._pending_workflow_attempts.get(result.delivery_id)
        self._pending_settle_results.pop(result.delivery_id, None)
        if self._workflow_outcome is not None and not _worker_skip(result):
            try:
                if self._workflow_outcome(result):
                    acknowledged = self.inbox.ack(
                        result.recipient, result.delivery_id
                    ).acknowledged
                    if acknowledged:
                        self._forget_origin_claim_if_present(result.delivery_id)
                        self._settle_delivery_hooks(result)
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
                self._forget_origin_claim_if_present(result.delivery_id)
                self._settle_delivery_hooks(result)
            return acknowledged
        hop_state = pop_cell_hop(self, result.delivery_id)
        hop_index = hop_state.hop_index if hop_state is not None else 0
        hop_stamp = (
            hop_state.stamp
            if hop_state is not None
            else (terminal_attempt.stamp if terminal_attempt is not None else None)
        )
        if result.status is HarnessResultStatus.INTERRUPTED:
            target_id = (
                hop_state.original.message_id
                if hop_state is not None
                else result.delivery_id
            )
            self._release_origin_claims_if_present((target_id,))
            return True
        original = (
            hop_state.original
            if hop_state is not None
            else next(
                (
                    message
                    for message in self.inbox.pending_messages(result.recipient)
                    if message.message_id == result.delivery_id
                ),
                None,
            )
        )
        if original is None:
            self._forget_origin_claim_if_present(result.delivery_id)
            self._settle_delivery_hooks(result)
            if result.status is HarnessResultStatus.FAILED:
                fallback = self._failure_original(result.delivery_id)
                if fallback is None:
                    fallback = self._failure_route(result.delivery_id)
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
        if hop_state is not None:
            result = normalize_root_result(original, result)
        cell_outcome = settle_cell_result(
            self, original, result, hop_index=hop_index, stamp=hop_stamp
        )
        if cell_outcome is not None:
            return True
        mechanism = self._settle_mechanism_answer(original, result)
        if mechanism is not None:
            return mechanism
        try:
            self._fetch_completed_turn_origin(original)
            pending = next(
                (
                    message
                    for message in self.inbox.pending_messages(original.recipient)
                    if message.message_id == original.message_id
                ),
                None,
            )
            hint = settle_hint_for(
                pending=pending,
                actor=original.recipient,
                message_id=original.message_id,
            )
            if hint == "ack":
                # A refused or unavailable ack means the row is already
                # settled; it is never a reason to turn a keep into an ack.
                self.inbox.ack(original.recipient, original.message_id)
                self._forget_origin_claim_if_present(original.message_id)
            else:
                self._release_origin_claims_if_present((original.message_id,))
        except (InboxAuthorityTimeout, InboxAuthorityUnavailable) as error:
            self._defer_ordinary_settlement(original, result, error)
            return False
        self._pending_settle_results.pop(result.delivery_id, None)
        self._settle_delivery_hooks(result)
        return True

    def _complete_harness_results(self) -> int:
        settled = 0
        raw_results = [
            *self._pending_workflow_results.values(),
            *self._pending_settle_results.values(),
            *self.harnesses.drain_results(),
        ]
        results: list[HarnessResult] = []
        for result in raw_results:
            # The managed batch has no identity of its own.  Every outcome
            # becomes one ordinary result per member before any inbox lookup.
            try:
                members = (
                    self._batch_member_results(result)
                    if result.message_ids else (result,)
                )
            except (InboxAuthorityTimeout, InboxAuthorityUnavailable) as error:
                self._defer_batch_expansion(result, error)
                continue
            release_batch_claim = getattr(self, "_release_batch_claim", None)
            if callable(release_batch_claim):
                release_batch_claim(result.delivery_id, result.message_ids)
            results.extend(members)
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
            self._pending_settle_results.pop(result.delivery_id, None)
            if self._workflow_outcome is not None and not _worker_skip(result):
                try:
                    handled = self._workflow_outcome(result)
                    if handled:
                        self.inbox.ack(result.recipient, result.delivery_id)
                        self._forget_origin_claim_if_present(result.delivery_id)
                        self._settle_delivery_hooks(result)
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
                self._forget_origin_claim_if_present(result.delivery_id)
                self._finish_attempt(result.delivery_id)
                self._settle_delivery_hooks(result)
                settled += 1
                continue
            hop_state = pop_cell_hop(self, result.delivery_id)
            hop_index = hop_state.hop_index if hop_state is not None else 0
            hop_stamp = (
                hop_state.stamp
                if hop_state is not None
                else (terminal_attempt.stamp if terminal_attempt is not None else None)
            )
            if result.status is HarnessResultStatus.INTERRUPTED:
                target_id = (
                    hop_state.original.message_id
                    if hop_state is not None
                    else result.delivery_id
                )
                release_held_cell_origin(self, hop_state.original) if hop_state is not None else None
                self._release_origin_claims_if_present((target_id,))
                continue
            original = (
                hop_state.original
                if hop_state is not None
                else next(
                    (
                        message
                        for message in self.inbox.pending_messages(result.recipient)
                        if message.message_id == result.delivery_id
                    ),
                    None,
                )
            )
            if original is None:
                self._forget_origin_claim_if_present(result.delivery_id)
                self._settle_delivery_hooks(result)
                # The row can be fetched/consumed (or already carry a terminal
                # settlement) before this failure lands.  That used to be a
                # silent drop; the authoritative sender is still durable in
                # the settlement tombstone, so look it up and report instead.
                if result.status is HarnessResultStatus.FAILED:
                    fallback = self._failure_original(result.delivery_id)
                    if fallback is None:
                        fallback = self._failure_route(result.delivery_id)
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
            if hop_state is not None:
                result = normalize_root_result(original, result)
            cell_outcome = settle_cell_result(
                self, original, result, hop_index=hop_index, stamp=hop_stamp
            )
            if cell_outcome is not None:
                if cell_outcome == "settled":
                    settled += 1
                continue
            self._finish_attempt(result.delivery_id)
            mechanism = self._settle_mechanism_answer(original, result)
            if mechanism is not None:
                if mechanism:
                    settled += 1
                continue
            try:
                self._fetch_completed_turn_origin(original)
                pending = next(
                    (
                        message
                        for message in self.inbox.pending_messages(original.recipient)
                        if message.message_id == original.message_id
                    ),
                    None,
                )
                hint = settle_hint_for(
                    pending=pending,
                    actor=original.recipient,
                    message_id=original.message_id,
                )
                if hint == "ack":
                    self.inbox.ack(original.recipient, original.message_id)
                    self._forget_origin_claim_if_present(original.message_id)
                else:
                    self._release_origin_claims_if_present((original.message_id,))
            except (InboxAuthorityTimeout, InboxAuthorityUnavailable) as error:
                # Settled per result: raising here used to abort the whole
                # tick after drain_results() had already released every
                # result in the batch, so the rest of the batch was lost.
                self._defer_ordinary_settlement(original, result, error)
                continue
            self._pending_settle_results.pop(result.delivery_id, None)
            self._settle_delivery_hooks(result)
            settled += 1
        return settled

    def _settle_mechanism_answer(
        self, original: InboxMessage, result: HarnessResult
    ) -> bool | None:
        """Answer a mechanism exchange, or None when this is not one.

        Only two exchanges reply without a model (rulings 58/59): a hook
        request authenticated by the reserved sender, and a python_worker
        ``result`` frame.  Everything else, including a
        forged hook marker, goes through the ordinary settle-hint path.
        """

        if original.intent != "request":
            return None
        hook = is_mechanism_hook_request(original)
        if not hook and not self._mechanism_is_python_worker(original):
            return None
        try:
            acknowledged = (
                self._answer_hook_request(original, result)
                if hook
                else self._answer_mechanism_result(original, result)
            )
        except (InboxAuthorityTimeout, InboxAuthorityUnavailable) as error:
            self._mechanism_answer_settled(original, result, error)
            return False
        if acknowledged:
            self._forget_origin_claim_if_present(original.message_id)
            self._pending_settle_results.pop(result.delivery_id, None)
            self._settle_delivery_hooks(result)
        return acknowledged

    def _mechanism_is_python_worker(self, original: InboxMessage) -> bool:
        """Is the row's recipient a python_worker agent?

        The kind is read through the worker the recipient key names: an
        id-minted agent's rows are keyed at its ``a.`` id URI, whose segment
        is not the worker name.
        """

        name = self._recipient_worker_name(original.recipient)
        if name is None:
            return False
        harness = self._harness_kind(name)
        return harness is not None and mechanism_of(harness) == "python_worker"

    def _settle_stranded_rows(self, harnesses: tuple[Any, ...]) -> int:
        """Startup sweep (ruling 74): settle rows stamped before a crash.

        A turn that crashed between ``completed_turn`` (the ``fetched`` stamp)
        and its ack leaves a stamped pending row nothing will dispatch again.
        Ask the one ``settle_hint_for`` once per such row: ``ack`` is acked,
        ``keep`` is left alone.  The model never re-runs; rows with a failure
        record belong to the retry path.  Runs once, at most
        ``_STRANDED_SWEEP_LIMIT`` rows.
        """

        has_fetched = getattr(self.inbox, "has_fetched", None)
        failure_of = getattr(self.inbox, "harness_failure_settlement", None)
        if not callable(has_fetched) or not callable(failure_of):
            return 0  # a narrow inbox port has no stamp evidence to sweep
        reader = getattr(self, "_actor_read_keys", None)

        def keys_of(name: str) -> tuple[str, ...]:
            return reader(name) if callable(reader) else (self.harness_actor_uri(name),)

        actors = {key for n in self.harnesses.streaming_actors() for key in keys_of(n)}
        actors |= {
            key for spec in harnesses
            if spec.headless and spec.harness in DECLARED_HARNESSES
            for key in keys_of(spec.name)
        }
        budget = _STRANDED_SWEEP_LIMIT
        settled = 0
        for recipient in sorted(actors):
            try:
                for message in self.inbox.pending_messages(recipient):
                    if budget <= 0:
                        return settled
                    if not has_fetched(message.message_id):
                        continue
                    if failure_of(message.message_id):
                        continue
                    budget -= 1
                    hint = settle_hint_for(
                        pending=message,
                        actor=recipient,
                        message_id=message.message_id,
                    )
                    if hint == "ack" and self.inbox.ack(
                        recipient, message.message_id
                    ).acknowledged:
                        settled += 1
            except (InboxAuthorityTimeout, InboxAuthorityUnavailable) as error:
                if self._logger is not None:
                    self._logger(
                        "warn", "daemon", "harness.stranded_sweep_deferred",
                        recipient=recipient, error=str(error),
                    )
        return settled

    def _defer_ordinary_settlement(
        self,
        original: InboxMessage,
        result: HarnessResult,
        error: InboxAuthorityTimeout | InboxAuthorityUnavailable,
    ) -> None:
        first = result.delivery_id not in self._pending_settle_results
        self._pending_settle_results[result.delivery_id] = result
        if first and self._logger is not None:
            self._logger(
                "warn",
                "daemon",
                "harness.settle.deferred",
                messageId=original.message_id,
                recipient=original.recipient,
                errorType=type(error).__name__,
                detail=str(error)[:300],
            )

    def _defer_batch_expansion(
        self,
        result: HarnessResult,
        error: InboxAuthorityTimeout | InboxAuthorityUnavailable,
    ) -> None:
        """Keep a drained batch result whose member lookup did not confirm.

        The whole batch result is retried next tick and its batch claim is
        kept, so no member is lost or re-offered while the inbox is busy.
        """

        first = result.delivery_id not in self._pending_settle_results
        self._pending_settle_results[result.delivery_id] = result
        if first and self._logger is not None:
            self._logger(
                "warn",
                "daemon",
                "harness.settle.deferred",
                messageId=result.delivery_id,
                recipient=result.recipient,
                errorType=type(error).__name__,
                detail=str(error)[:300],
            )

    def _mechanism_answer_settled(
        self,
        original: InboxMessage,
        result: HarnessResult,
        error: InboxAuthorityTimeout | InboxAuthorityUnavailable,
    ) -> bool:
        """Record a mechanism answer whose submit or ack did not confirm.

        The answer id is derived from the delivery, so the same result is kept
        and the SAME answer is retried next tick; the handler is never re-run.
        The answer functions already ack a committed receipt conflict, so this
        path only defers.
        """

        first_deferral = result.delivery_id not in self._pending_settle_results
        self._pending_settle_results[result.delivery_id] = result
        if first_deferral and self._logger is not None:
            self._logger(
                "warn",
                "daemon",
                "harness.mechanism_answer.deferred",
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
        if failure_code in _BLOCKING_FAILURE_CODES:
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
            self._forget_origin_claim_if_present(original.message_id)
            self._settle_delivery_hooks(result)
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
            failure.failure_code in _BLOCKING_FAILURE_CODES
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
        # this attempt's own evidence.  The owner's quota alert (the
        # unified hook bus carries it as turn.failed today) is a
        # separate, retained channel and does not stand in
        # for this one (the 2026-09-21 incident: the owner was told
        # within a second and the sender was told nothing).
        self._loud_harness_failure(result, original, failure=failure, attempt=attempt)
        self._finish_attempt(result.delivery_id)
        if failure.terminal:
            self._forget_origin_claim_if_present(original.message_id)
            self._settle_delivery_hooks(result)
        else:
            self._release_origin_claims_if_present((original.message_id,))
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
