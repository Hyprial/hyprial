from __future__ import annotations
from uuid import uuid4
from dataclasses import replace
from hyprial.daemon import (
    AlarmResult,
)
from hyprial.kernel import PortCommandRejected
from hyprial.daemon.impl.inbox.contracts.api  import (
    SubmissionResult,
)
from hyprial.daemon.impl.inbox.contracts.ports  import (
    RetryCustodyCommand,
    RetryDueCommand,
    RetryIoCompleted,
    SubmissionProjection,
)
from hyprial.daemon.impl.inbox.service.policy import DELIVERY_RETRY_CLAIM_LIMIT

from ..events import (
    CompletionReceiptState,
    DispatchIoKind,
    DispatchIoCompleted,
    DispatchIoFailed,
    DispatchIoRequested,
    DispatchItem,
    DispatchOutcomeKind,
    ReassociateIoCompletion,
    _alarm_failure,
)
from ..internal import (
    _PendingDispatch,
)

class DeliveryCustodyRetryMixin:
    def _reassociate_io_completion(
        self,
        command: ReassociateIoCompletion,
    ) -> bool:
        if command.generation != self._generation:
            self._reject_stale(command.correlation_id)
            return False
        completion_kind = command.request.completion_kind
        if completion_kind not in {
            "submit",
            "progress",
            "alarm",
            "explicit_alarm",
            "expiry_notice",
        }:
            self._reject_stale(command.correlation_id)
            return False
        request = replace(
            command.request,
            generation=self._generation,
            version=self._version,
        )
        completion = replace(
            command.completion,
            generation=self._generation,
            version=self._version,
        )
        self._pending_io[command.correlation_id] = _PendingDispatch(
            request=request,
            completion_kind=completion_kind,
        )
        self._refresh_claimed_projection()
        if isinstance(completion, DispatchIoCompleted):
            return self._complete_dispatch(completion)
        return self._fail_dispatch(completion)
    def _acknowledge_completion(
        self,
        completion: DispatchIoCompleted | DispatchIoFailed,
        state: CompletionReceiptState,
    ) -> None:
        if completion.receipt_token:
            self._receipts.acknowledge(completion.receipt_token, state)
    def _claim_retry(self, command: RetryDueCommand) -> None:
        if command.correlation_id in self._pending_io:
            self._reject_correlation(command.correlation_id)
            return
        with self._service._db:
            self._service._status.purge(now_ms=command.now_ms)
        already_claimed = {
            item.message.message_id
            for pending in self._pending_io.values()
            for item in pending.request.items
        }
        placeholders = ", ".join("?" for _ in already_claimed)
        exclusion = (
            f" AND message_id NOT IN ({placeholders})"  # noqa: S608
            if already_claimed
            else ""
        )
        rows = self._service._db.execute(
            "SELECT * FROM outbox WHERE next_attempt_ms <= ?"
            f"{exclusion} ORDER BY created_at_ms, rowid LIMIT ?",  # noqa: S608
            (command.now_ms, *sorted(already_claimed), DELIVERY_RETRY_CLAIM_LIMIT),
        ).fetchall()
        items: list[DispatchItem] = []
        completed: list[SubmissionResult] = []
        for row in rows:
            message = self._service._row_message(row)
            # A queued correlated reply has a durable submit receipt that is
            # still marked queued. TTL wins even before its first native I/O;
            # the flag below selects the atomic receipt-settlement path.
            never_attempted_reply = (
                int(row["attempts"]) == 0
                and self._stable_reply_correlation(message) is not None
            )
            expired = command.now_ms >= int(row["expires_at_ms"])
            if expired:
                result = SubmissionResult(
                    message.message_id,
                    False,
                    code="TTL_EXPIRED",
                )
                if never_attempted_reply:
                    with self._service._db:
                        self._service._outbox_to_dlq_locked(
                            row,
                            "TTL_EXPIRED",
                            command.now_ms,
                        )
                        self._settle_submission_receipt_locked(
                            None,
                            message,
                            result,
                            command.now_ms,
                        )
                    self._service._finish_outbox_dlq(
                        row,
                        "TTL_EXPIRED",
                        command.now_ms,
                    )
                else:
                    self._service._outbox_to_dlq(
                        row,
                        "TTL_EXPIRED",
                        command.now_ms,
                    )
                completed.append(result)
                continue
            items.append(
                DispatchItem(
                    message=message,
                    retry=True,
                )
            )
        if not items:
            self._publish(
                RetryIoCompleted(
                    correlation_id=command.correlation_id,
                    generation=self._generation,
                    version=self._version,
                    results=tuple(
                        SubmissionProjection.from_result(result)
                        for result in completed
                    ),
                )
            )
            return
        request = DispatchIoRequested(
            correlation_id=command.correlation_id,
            generation=self._generation,
            version=self._version,
            kind=DispatchIoKind.DELIVERY,
            items=tuple(items),
        )
        self._request_io(
            request,
            completion_kind="retry",
            pre_results=tuple(completed),
        )
    def _claim_custody_retry(self, command: RetryCustodyCommand) -> None:
        if command.correlation_id in self._pending_io:
            self._reject_correlation(command.correlation_id)
            return
        already_claimed = {
            item.message.message_id
            for pending in self._pending_io.values()
            for item in pending.request.items
        }
        placeholders = ", ".join("?" for _ in already_claimed)
        exclusion = (
            f" AND message_id NOT IN ({placeholders})"  # noqa: S608
            if already_claimed
            else ""
        )
        rows = self._service._db.execute(
            "SELECT * FROM custody WHERE next_attempt_ms <= ?"
            f"{exclusion} ORDER BY accepted_at_ms, rowid LIMIT ?",  # noqa: S608
            (command.now_ms, *sorted(already_claimed), DELIVERY_RETRY_CLAIM_LIMIT),
        ).fetchall()
        items: list[DispatchItem] = []
        completed: list[SubmissionResult] = []
        for row in rows:
            message = self._service._row_message(row)
            if command.now_ms >= int(row["expires_at_ms"]):
                self._service._custody_to_dlq(row, "TTL_EXPIRED", command.now_ms)
                completed.append(
                    SubmissionResult(
                        message.message_id,
                        False,
                        code="TTL_EXPIRED",
                    )
                )
                continue
            items.append(
                DispatchItem(
                    message=message,
                    retry=True,
                    custody_retry=True,
                )
            )
        if not items:
            version = self._committed_version()
            self._publish_submission_batch(
                command.correlation_id,
                version,
                "retry_custody_due",
                tuple(completed),
            )
            return
        request = DispatchIoRequested(
            correlation_id=command.correlation_id,
            generation=self._generation,
            version=self._version,
            kind=DispatchIoKind.DELIVERY,
            items=tuple(items),
        )
        self._request_io(
            request,
            completion_kind="custody_retry",
            pre_results=tuple(completed),
        )
    def _request_io(
        self,
        request: DispatchIoRequested,
        *,
        completion_kind: str,
        pre_results: tuple[SubmissionResult, ...] = (),
    ) -> None:
        if request.completion_kind != completion_kind:
            request = replace(request, completion_kind=completion_kind)
        if not request.receipt_token:
            request = replace(request, receipt_token=uuid4().hex)
        self._pending_io[request.correlation_id] = _PendingDispatch(
            request=request,
            completion_kind=completion_kind,
            pre_results=pre_results,
        )
        self._refresh_claimed_projection()
        self._publish(request)
        if self._io_worker.submit(request):
            return
        self._fail_dispatch(
            DispatchIoFailed(
                correlation_id=request.correlation_id,
                generation=request.generation,
                version=request.version,
                code="DISPATCH_IO_OVERLOADED",
                detail="delivery I/O queue is full or closing",
                receipt_token=request.receipt_token,
            )
        )
    def _complete_dispatch(self, event: DispatchIoCompleted) -> bool:
        pending = self._pop_current_completion(event)
        if pending is None:
            return False
        if pending.completion_kind == "alarm":
            outcome = event.outcomes[0] if event.outcomes else None
            if (
                outcome is not None
                and outcome.kind is DispatchOutcomeKind.ALARM_LOCAL
                and outcome.notice is not None
            ):
                self._service.receive_system_notice(outcome.notice)
            self._publish(event)
            return True
        if pending.completion_kind == "explicit_alarm":
            outcome = event.outcomes[0] if event.outcomes else None
            delivered = outcome is not None and outcome.recipient_online
            if (
                outcome is not None
                and outcome.kind is DispatchOutcomeKind.ALARM_LOCAL
                and outcome.notice is not None
            ):
                delivered = self._service.receive_system_notice(outcome.notice)
            alarm = pending.request.alarm
            assert alarm is not None
            fields = self._alarm_fields(alarm)
            self._service._alarm._safe_log(
                "info" if delivered else "error",
                "alarm.delivered" if delivered else "alarm.failed",
                **fields,
                **({} if delivered else {"failure": _alarm_failure(outcome)}),
            )
            self._publish(event)
            self._publish_alarm_completed(
                pending.request.correlation_id,
                AlarmResult(
                    "delivered" if delivered else "failed",
                    alarm.audience,
                    claimed=pending.request.alarm_claimed,
                ),
            )
            return True
        if pending.completion_kind == "progress":
            delivered = bool(event.outcomes and event.outcomes[0].recipient_online)
            self._publish(event)
            self._publish_bool(
                pending.request.correlation_id,
                "submit_progress_event",
                delivered,
            )
            return True
        if pending.completion_kind == "expiry_notice":
            outcomes = {outcome.message_id: outcome for outcome in event.outcomes}
            for item in pending.request.items:
                message_id = item.message.message_id
                outcome = outcomes[message_id] if message_id in outcomes else None
                self._service._log_expired_sender_notice(
                    item.source_message_id or item.message.message_id,
                    item.target_node,
                    bool(outcome and outcome.recipient_online),
                )
            self._publish(event)
            return True
        outcomes = {outcome.message_id: outcome for outcome in event.outcomes}
        results: list[SubmissionResult] = list(pending.pre_results)
        for item in pending.request.items:
            if item.message.message_id not in outcomes:
                results.append(
                    self._defer_after_failure(
                        item,
                        event.completed_at_ms,
                        receipt_correlation_id=(
                            pending.request.correlation_id
                            if pending.completion_kind == "submit"
                            else None
                        ),
                    )
                )
                continue
            outcome = outcomes[item.message.message_id]
            if pending.completion_kind == "custody_retry":
                results.append(
                    self._apply_custody_outcome(
                        item,
                        outcome,
                        event.completed_at_ms,
                    )
                )
            else:
                results.append(
                    self._apply_outcome(
                        item,
                        outcome,
                        event.completed_at_ms,
                        receipt_correlation_id=(
                            pending.request.correlation_id
                            if pending.completion_kind == "submit"
                            else None
                        ),
                    )
                )
        self._apply_inflight_wakes(pending, event.completed_at_ms)
        version = self._committed_version()
        self._publish(event)
        self._publish_final(pending, version, tuple(results))
        return True
    def _fail_dispatch(self, event: DispatchIoFailed) -> bool:
        pending = self._pop_current_completion(event)
        if pending is None:
            return False
        self._publish(event)
        if pending.completion_kind == "alarm":
            return True
        if pending.completion_kind == "explicit_alarm":
            alarm = pending.request.alarm
            assert alarm is not None
            self._service._alarm._safe_log(
                "error",
                "alarm.failed",
                **self._alarm_fields(alarm),
                failure=event.detail,
            )
            self._publish_alarm_completed(
                pending.request.correlation_id,
                AlarmResult(
                    "failed",
                    alarm.audience,
                    claimed=pending.request.alarm_claimed,
                ),
            )
            return True
        now = self._service._now_ms()
        if pending.completion_kind == "progress":
            self._publish_bool(
                pending.request.correlation_id,
                "submit_progress_event",
                False,
            )
            return True
        if pending.completion_kind == "expiry_notice":
            for item in pending.request.items:
                self._service._log_expired_sender_notice(
                    item.source_message_id or item.message.message_id,
                    item.target_node,
                    False,
                )
            return True
        if pending.completion_kind == "custody_retry":
            deferred = tuple(
                self._defer_custody_after_failure(item, now)
                for item in pending.request.items
            )
        else:
            deferred = tuple(
                self._defer_after_failure(
                    item,
                    now,
                    receipt_correlation_id=(
                        pending.request.correlation_id
                        if pending.completion_kind == "submit"
                        else None
                    ),
                )
                for item in pending.request.items
            )
        results = pending.pre_results + deferred
        self._apply_inflight_wakes(pending, now)
        version = self._committed_version()
        self._publish_final(pending, version, results)
        return True
    def _retain_inflight_wake(self, recipient: str, now_ms: int) -> bool:
        retained = False
        for correlation_id, pending in self._pending_io.items():
            if pending.completion_kind not in {"submit", "retry", "custody_retry"}:
                continue
            wakes = dict(pending.online_wakes)
            for item in pending.request.items:
                if item.message.recipient != recipient:
                    continue
                # This state belongs to an existing exact dispatch claim, not
                # an independent recipient queue. Repeated wakes coalesce.
                message_id = item.message.message_id
                previous = wakes[message_id] if message_id in wakes else now_ms
                wakes[message_id] = max(now_ms, previous)
                retained = True
            if wakes:
                self._pending_io[correlation_id] = replace(
                    pending, online_wakes=tuple(wakes.items()),
                )
        return retained
    def _apply_inflight_wakes(self, pending: _PendingDispatch, settled_at_ms: int) -> None:
        # Exact completion matching has already retired the claim. Only rows
        # still present and unexpired may be advanced; success, terminalization
        # and cancellation must never be undone. Attempts/TTL are untouched.
        now_ms = max(settled_at_ms, self._service._now_ms())
        custody_ids = {
            item.message.message_id
            for item in pending.request.items
            if item.custody_retry
        }
        with self._service._db:
            for message_id, wake_ms in pending.online_wakes:
                due_ms = max(now_ms, wake_ms)
                table = "custody" if message_id in custody_ids else "outbox"
                self._service._db.execute(
                    f"UPDATE {table} SET next_attempt_ms = MIN(next_attempt_ms, ?) "
                    "WHERE message_id = ? AND expires_at_ms > ?",
                    (due_ms, message_id, due_ms),
                )
    def _pop_current_completion(
        self,
        event: DispatchIoCompleted | DispatchIoFailed,
    ) -> _PendingDispatch | None:
        pending = (
            self._pending_io[event.correlation_id]
            if event.correlation_id in self._pending_io
            else None
        )
        if (
            pending is None
            or pending.request.generation != event.generation
            or pending.request.version != event.version
        ):
            self._publish(
                PortCommandRejected(
                    correlation_id=event.correlation_id,
                    domain="inbox",
                    generation=self._generation,
                    version=self._version,
                    code="STALE_IO_COMPLETION",
                    detail="dispatch completion did not match an active claim",
                )
            )
            return None
        self._pending_io.pop(event.correlation_id)
        self._refresh_claimed_projection()
        return pending
