from __future__ import annotations
from hyprial.daemon import AlarmResult
from hyprial.daemon.impl.inbox.contracts.api  import (
    DeliveryLifecycle,
    SubmissionResult,
)
from hyprial.daemon.impl.inbox.contracts.ports  import (
    AlarmCompleted,
    RetryIoCompleted,
    SubmissionCompleted,
    SubmissionBatchCompleted,
    SubmissionProjection,
)
from hyprial.daemon.impl.inbox.service.policy import online_retry_next_attempt_ms

from ..events import (
    DispatchItem,
    DispatchOutcome,
    DispatchOutcomeKind,
)
from ..internal import (
    _PendingDispatch,
)

class DeliveryCustodyOutcomesMixin:
    def _apply_outcome(
        self,
        item: DispatchItem,
        outcome: DispatchOutcome,
        now_ms: int,
        *,
        receipt_correlation_id: str | None = None,
    ) -> SubmissionResult:
        row = self._service._db.execute(
            "SELECT * FROM outbox WHERE message_id = ?",
            (item.message.message_id,),
        ).fetchone()
        if row is None:
            result = SubmissionResult(item.message.message_id, True, queued=False)
            if receipt_correlation_id is not None:
                self._persist_submission_receipt(
                    receipt_correlation_id,
                    item.message,
                    result,
                    now_ms,
                )
            return result
        if outcome.kind is DispatchOutcomeKind.DELIVERED:
            from hyprial.daemon.impl.inbox.links.pull  import HoldReason, TerminalState

            result = SubmissionResult(item.message.message_id, True, queued=False)
            with self._service._db:
                self._service._record_terminal(
                    item.message,
                    state=TerminalState.FETCHED,
                    reason=HoldReason.ACK_RECEIVED,
                    now_ms=now_ms,
                )
                self._service._db.execute(
                    "DELETE FROM outbox WHERE message_id = ?",
                    (item.message.message_id,),
                )
                self._settle_submission_receipt_locked(
                    receipt_correlation_id,
                    item.message,
                    result,
                    now_ms,
                )
            return result
        if outcome.kind is DispatchOutcomeKind.CUSTODY:
            result = SubmissionResult(
                item.message.message_id,
                True,
                queued=False,
                custody_mailbox=outcome.custody_mailbox,
            )
            with self._service._db:
                self._service._db.execute(
                    "DELETE FROM outbox WHERE message_id = ?",
                    (item.message.message_id,),
                )
                self._settle_submission_receipt_locked(
                    receipt_correlation_id,
                    item.message,
                    result,
                    now_ms,
                )
            return result
        if (
            item.message.lifecycle is DeliveryLifecycle.ONLINE_ONLY
            and not outcome.recipient_online
        ):
            result = SubmissionResult(
                item.message.message_id,
                False,
                code="TARGET_OFFLINE",
            )
            with self._service._db:
                self._service._db.execute(
                    "DELETE FROM outbox WHERE message_id = ?",
                    (item.message.message_id,),
                )
                self._settle_submission_receipt_locked(
                    receipt_correlation_id,
                    item.message,
                    result,
                    now_ms,
                )
            return result
        result = SubmissionResult(item.message.message_id, True, queued=True)
        with self._service._db:
            self._defer_outbox_locked(
                item.message,
                now_ms,
                count_attempt=outcome.direct_attempted,
            )
            if receipt_correlation_id is not None:
                self._persist_submission_receipt_locked(
                    receipt_correlation_id,
                    item.message,
                    result,
                    now_ms,
                )
        return result
    def _defer_after_failure(
        self,
        item: DispatchItem,
        now_ms: int,
        *,
        receipt_correlation_id: str | None = None,
    ) -> SubmissionResult:
        row = self._service._db.execute(
            "SELECT 1 FROM outbox WHERE message_id = ?",
            (item.message.message_id,),
        ).fetchone()
        result = SubmissionResult(
            item.message.message_id,
            True,
            queued=row is not None,
            code="DISPATCH_IO_FAILED",
        )
        with self._service._db:
            if row is not None:
                self._defer_outbox_locked(item.message, now_ms, count_attempt=True)
            if receipt_correlation_id is not None:
                self._persist_submission_receipt_locked(
                    receipt_correlation_id,
                    item.message,
                    result,
                    now_ms,
                )
        return result
    def _apply_custody_outcome(
        self,
        item: DispatchItem,
        outcome: DispatchOutcome,
        now_ms: int,
    ) -> SubmissionResult:
        row = self._service._db.execute(
            "SELECT * FROM custody WHERE message_id = ?",
            (item.message.message_id,),
        ).fetchone()
        if row is None:
            return SubmissionResult(item.message.message_id, True, queued=False)
        if outcome.kind is DispatchOutcomeKind.DELIVERED:
            from hyprial.daemon.impl.inbox.links.pull  import HoldReason, TerminalState

            with self._service._db:
                self._service._record_terminal(
                    item.message,
                    state=TerminalState.FETCHED,
                    reason=HoldReason.ACK_RECEIVED,
                    now_ms=now_ms,
                    holder=str(row["mailbox_node"]),
                )
                self._service._db.execute(
                    "DELETE FROM custody WHERE message_id = ?",
                    (item.message.message_id,),
                )
            return SubmissionResult(item.message.message_id, True, queued=False)
        return self._defer_custody_after_failure(
            item,
            now_ms,
            count_attempt=outcome.direct_attempted,
        )
    def _defer_custody_after_failure(
        self,
        item: DispatchItem,
        now_ms: int,
        *,
        count_attempt: bool = True,
    ) -> SubmissionResult:
        row = self._service._db.execute(
            "SELECT attempts, expires_at_ms FROM custody WHERE message_id = ?",
            (item.message.message_id,),
        ).fetchone()
        if row is None:
            return SubmissionResult(item.message.message_id, True, queued=False)
        attempts = int(row["attempts"]) + (1 if count_attempt else 0)
        next_attempt_ms = (
            online_retry_next_attempt_ms(
                message_id=item.message.message_id,
                attempts=attempts,
                now_ms=now_ms,
                expires_at_ms=int(row["expires_at_ms"]),
            )
            if count_attempt
            else int(row["expires_at_ms"])
        )
        with self._service._db:
            self._service._db.execute(
                "UPDATE custody SET attempts = ?, "
                "next_attempt_ms = MIN(expires_at_ms, ?) WHERE message_id = ?",
                (
                    attempts,
                    next_attempt_ms,
                    item.message.message_id,
                ),
            )
        self._service._alarm._safe_log(
            "warn",
            "outbox.retry_scheduled",
            messageId=item.message.message_id,
            recipient=item.message.recipient,
            sender=item.message.sender,
            attempts=attempts,
            nextAttemptMs=next_attempt_ms,
            reason="ack not received" if count_attempt else "recipient offline",
        )
        return SubmissionResult(item.message.message_id, True, queued=True)
    def _publish_final(
        self,
        pending: _PendingDispatch,
        version: int,
        results: tuple[SubmissionResult, ...],
    ) -> None:
        if pending.completion_kind == "submit":
            self._publish_submission(
                pending.request.correlation_id,
                version,
                results[0],
            )
            return
        if pending.completion_kind == "custody_retry":
            self._publish_submission_batch(
                pending.request.correlation_id,
                version,
                "retry_custody_due",
                results,
            )
            return
        self._publish(
            RetryIoCompleted(
                correlation_id=pending.request.correlation_id,
                generation=self._generation,
                version=version,
                results=tuple(SubmissionProjection.from_result(item) for item in results),
            )
        )
    def _publish_submission_batch(
        self,
        correlation_id: str,
        version: int,
        operation: str,
        results: tuple[SubmissionResult, ...],
    ) -> None:
        self._publish(
            SubmissionBatchCompleted(
                correlation_id,
                self._generation,
                version,
                operation,
                tuple(SubmissionProjection.from_result(item) for item in results),
            )
        )
    def _publish_submission(
        self,
        correlation_id: str,
        version: int,
        result: SubmissionResult,
    ) -> None:
        self._publish(
            SubmissionCompleted(
                correlation_id=correlation_id,
                generation=self._generation,
                version=version,
                result=SubmissionProjection.from_result(result),
            )
        )
    def _publish_alarm_completed(
        self,
        correlation_id: str,
        result: AlarmResult,
    ) -> None:
        version = self._committed_version()
        self._publish(
            AlarmCompleted(
                correlation_id,
                self._generation,
                version,
                result,
            )
        )
