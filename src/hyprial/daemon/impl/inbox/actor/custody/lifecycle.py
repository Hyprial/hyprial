from __future__ import annotations
from pathlib import Path
from hyprial.kernel import PortCommandRejected
from hyprial.daemon.impl.inbox.contracts.api  import (
    InboxMessage,
    ReceiveResult,
)
from hyprial.daemon.impl.inbox.contracts.ports  import (
    AcceptCustodyCommand,
    AcknowledgeCompleted,
    AcknowledgeMessageCommand,
    CloseInboxCommand,
    DismissSystemNoticeCommand,
    DrainSystemNoticesCommand,
    EmitAlarmCommand,
    FailMessageCommand,
    FailureMutationCompleted,
    HarnessFailureSettled,
    FetchPendingCommand,
    InboxClosed,
    InboxEventSink,
    InboxPruneCompleted,
    OutboxPruneCompleted,
    PruneOutboxCommand,
    PruneInboxCommand,
    ReceiveCompleted,
    ReceiveMessageCommand,
    ReceiveProgressCommand,
    ReceiveSystemNoticeCommand,
    RefreshHoldCommand,
    RetireOutboxReceiptCommand,
    RetryCustodyCommand,
    RetryDueCommand,
    SettleHarnessFailureCommand,
    SubmitMessageCommand,
    SubmitProgressCommand,
    WakeOutboxRecipientCommand,
)
from hyprial.daemon.impl.inbox.tracking.progress  import PROGRESS_INTENT
from hyprial.daemon.impl.inbox.service.policy  import (
    RetryPolicy,
    online_retry_next_attempt_ms,
)
from hyprial.daemon.impl.inbox.service.delivery.notices  import _bare_sender

from ..events import (
    CompletionReceiptState,
    DeliveryCustodyEvent,
    DispatchIoCompleted,
    DispatchIoFailed,
    ReassociateIoCompletion,
)
from ..internal import (
    _ActorOwnedInboxService,
    _CompletionReceipts,
    _DurableCompletionHandoffs,
    _PendingDispatch,
    _ProjectionState,
)
from ..worker import DeliveryIoWorker

class DeliveryCustodyLifecycleMixin:
    """One actor generation owning every durable delivery mutation it accepts."""
    def __init__(
        self,
        database: Path,
        event_sink: InboxEventSink,
        projection: _ProjectionState,
        io_worker: DeliveryIoWorker,
        handoffs: _DurableCompletionHandoffs,
        receipts: _CompletionReceipts,
        *,
        generation: int,
        retry_policy: RetryPolicy | None = None,
        max_inbox_items: int = 10_000,
        max_custody_bytes: int = 1 << 30,
        node_id: str = "local",
        service_options: dict[str, object] | None = None,
    ) -> None:
        self._events = event_sink
        self._projection = projection
        self._io_worker = io_worker
        self._handoffs = handoffs
        self._receipts = receipts
        self._generation = generation
        self._version = projection.version
        self._pending_io: dict[str, _PendingDispatch] = {}
        self._refresh_claimed_projection()
        self._service = _ActorOwnedInboxService(
            database,
            failure_sink=self._request_alarm,
            retry_policy=retry_policy,
            max_inbox_items=max_inbox_items,
            max_custody_bytes=max_custody_bytes,
            node_id=node_id,
            service_options=service_options,
        )
        self._create_submission_receipts()
        self._refresh_projection()
    def __call__(self, command: object) -> None:
        try:
            self._handle(command)
        except BaseException:
            # A failed runtime generation is never called again.  Release its
            # SQLite descriptors before guardian supervision constructs the
            # replacement, while preserving any transaction that committed
            # before the external-I/O exception.
            try:
                self._events.publish(
                    PortCommandRejected(
                        correlation_id=command.correlation_id,
                        domain="inbox",
                        generation=self._generation,
                        version=self._version,
                        code="DELIVERY_COMMAND_FAILED",
                        detail="delivery command failed before completion",
                    )
                )
            except Exception:
                pass
            finally:
                self._service.close()
            raise
    def _handle(self, command: object) -> None:
        self._reconcile_handoffs()
        if isinstance(command, SubmitMessageCommand):
            self._stage_submit(command)
            return
        if isinstance(command, ReceiveMessageCommand):
            if command.message.intent == PROGRESS_INTENT:
                accepted = self._service.receive_progress_event(command.message)
                result = ReceiveResult(
                    message_id=command.message.message_id,
                    accepted=accepted,
                    acknowledged=accepted,
                )
            elif _bare_sender(command.message.sender):
                result = ReceiveResult(
                    message_id=command.message.message_id,
                    accepted=False,
                    acknowledged=False,
                    code="SENDER_NOT_CANONICAL",
                )
            else:
                result = self._service.receive(command.message, now_ms=command.now_ms)
            version = self._committed_version()
            self._publish(
                ReceiveCompleted(
                    correlation_id=command.correlation_id,
                    generation=self._generation,
                    version=version,
                    result=result,
                )
            )
            return
        if isinstance(command, AcknowledgeMessageCommand):
            result = self._service.ack(command.recipient, command.message_id)
            version = self._committed_version()
            self._publish(
                AcknowledgeCompleted(
                    correlation_id=command.correlation_id,
                    generation=self._generation,
                    version=version,
                    result=result,
                )
            )
            return
        if isinstance(command, RetryDueCommand):
            if not self._current_fence(command.generation, command.version):
                self._reject_stale(command.correlation_id)
                return
            self._claim_retry(command)
            return
        if isinstance(command, WakeOutboxRecipientCommand):
            retained = self._retain_inflight_wake(command.recipient, command.now_ms)
            advanced = self._service.wake_outbox_recipient(
                command.recipient, now_ms=command.now_ms,
            )
            self._publish_bool(
                command.correlation_id,
                "wake_outbox_recipient",
                advanced or retained,
            )
            return
        if isinstance(command, PruneInboxCommand):
            if not self._current_fence(command.generation, command.version):
                self._reject_stale(command.correlation_id)
                return
            items, notices = self._service._prune_inbox_state(command.now_ms)
            self._stage_expired_sender_notices(command.correlation_id, notices)
            version = self._committed_version()
            self._publish(
                InboxPruneCompleted(
                    correlation_id=command.correlation_id,
                    generation=self._generation,
                    version=version,
                    items=items,
                )
            )
            return
        if isinstance(command, RefreshHoldCommand):
            self._publish_bool(
                command.correlation_id,
                "refresh_hold",
                self._service.refresh_hold(
                    command.message_id,
                    now_ms=command.now_ms,
                ),
            )
            return
        if isinstance(command, FailMessageCommand):
            try:
                result = self._service.fail(
                    command.recipient,
                    command.message_id,
                    command.detail,
                )
            except KeyError:
                self._publish_missing(command.correlation_id, command.message_id)
                return
            version = self._committed_version()
            self._publish(
                FailureMutationCompleted(
                    command.correlation_id,
                    self._generation,
                    version,
                    result,
                )
            )
            return
        if isinstance(command, SettleHarnessFailureCommand):
            try:
                result = self._service.settle_harness_failure(
                    command.recipient,
                    command.message_id,
                    command.failure_code,
                    permanent=command.permanent,
                    max_attempts=command.max_attempts,
                    backoff_ms=command.backoff_ms,
                    now_ms=command.now_ms,
                )
            except KeyError:
                self._publish_missing(command.correlation_id, command.message_id)
                return
            version = self._committed_version()
            self._publish(
                HarnessFailureSettled(
                    command.correlation_id,
                    self._generation,
                    version,
                    result,
                )
            )
            return
        if isinstance(command, AcceptCustodyCommand):
            result = self._service.accept_custody(
                command.message,
                mailbox_node=command.mailbox_node,
                now_ms=command.now_ms,
            )
            version = self._committed_version()
            self._publish(
                AcknowledgeCompleted(
                    command.correlation_id,
                    self._generation,
                    version,
                    result,
                )
            )
            return
        if isinstance(command, RetryCustodyCommand):
            if not self._current_fence(command.generation, command.version):
                self._reject_stale(command.correlation_id)
                return
            self._claim_custody_retry(command)
            return
        if isinstance(command, RetireOutboxReceiptCommand):
            self._publish_bool(
                command.correlation_id,
                "retire_outbox_receipt",
                self._service.retire_outbox_receipt(
                    command.sender,
                    command.message_id,
                    now_ms=command.now_ms,
                ),
            )
            return
        if isinstance(command, ReceiveSystemNoticeCommand):
            self._publish_bool(
                command.correlation_id,
                "receive_system_notice",
                self._service.receive_system_notice(command.notice),
            )
            return
        if isinstance(command, DrainSystemNoticesCommand):
            self._publish_messages(
                command.correlation_id,
                "drain_system_notices",
                self._service.drain_system_notices(command.recipient),
            )
            return
        if isinstance(command, DismissSystemNoticeCommand):
            self._publish_bool(
                command.correlation_id,
                "dismiss_system_notice",
                self._service.dismiss_system_notice(command.message_id),
            )
            return
        if isinstance(command, SubmitProgressCommand):
            self._submit_progress(command)
            return
        if isinstance(command, ReceiveProgressCommand):
            self._publish_bool(
                command.correlation_id,
                "receive_progress_event",
                self._service.receive_progress_event(command.message),
            )
            return
        if isinstance(command, FetchPendingCommand):
            self._publish_messages(
                command.correlation_id,
                "fetch_pending",
                self._service.fetch_pending(
                    command.recipient,
                    now_ms=command.now_ms,
                ),
            )
            return
        if isinstance(command, PruneOutboxCommand):
            if not self._current_fence(command.generation, command.version):
                self._reject_stale(command.correlation_id)
                return
            items = tuple(
                self._service.prune_outbox(
                    undeliverable=lambda recipient: (
                        recipient in command.undeliverable_recipients
                    ),
                    unresolvable=lambda recipient: (
                        recipient in command.unresolvable_recipients
                    ),
                    now_ms=command.now_ms,
                    dry_run=command.dry_run,
                )
            )
            version = self._committed_version()
            self._publish(
                OutboxPruneCompleted(
                    command.correlation_id,
                    self._generation,
                    version,
                    items,
                )
            )
            return
        if isinstance(command, EmitAlarmCommand):
            self._emit_explicit_alarm(command)
            return
        if isinstance(command, ReassociateIoCompletion):
            if self._receipts.is_settled(command.completion.receipt_token):
                return
            settled = self._reassociate_io_completion(command)
            self._acknowledge_completion(
                command.completion,
                CompletionReceiptState.SETTLED
                if settled
                else CompletionReceiptState.RETRY,
            )
            return
        if isinstance(command, DispatchIoCompleted):
            if self._receipts.is_settled(command.receipt_token):
                return
            settled = self._complete_dispatch(command)
            self._acknowledge_completion(
                command,
                CompletionReceiptState.SETTLED
                if settled
                else CompletionReceiptState.RETRY,
            )
            return
        if isinstance(command, DispatchIoFailed):
            if self._receipts.is_settled(command.receipt_token):
                return
            settled = self._fail_dispatch(command)
            self._acknowledge_completion(
                command,
                CompletionReceiptState.SETTLED
                if settled
                else CompletionReceiptState.RETRY,
            )
            return
        if isinstance(command, CloseInboxCommand):
            if self._pending_io:
                self._publish(
                    PortCommandRejected(
                        correlation_id=command.correlation_id,
                        domain="inbox",
                        generation=self._generation,
                        version=self._version,
                        code="IO_DRAIN_REQUIRED",
                        detail="delivery I/O must drain before storage closes",
                    )
                )
                return
            self._service.close()
            self._version += 1
            self._projection.advance_closed(self._version)
            self._publish(
                InboxClosed(
                    correlation_id=command.correlation_id,
                    generation=self._generation,
                    version=self._version,
                )
            )
            return
        raise TypeError(f"unsupported delivery command: {type(command).__name__}")
    @staticmethod
    def _stable_reply_correlation(message: InboxMessage) -> str | None:
        """The durable command identity of a correlated reply submit.

        Mirrors ``DeliveryCustodyFacade.submit``: a public ``message.reply``
        retry reuses the deterministic reply message id, so its receipt row
        is keyed by this correlation regardless of which caller submitted it.
        """

        if (
            message.intent == "reply"
            and isinstance(message.idempotency_key, str)
            and message.idempotency_key.startswith("reply:")
        ):
            return f"inbox:reply-submit:{message.message_id}"
        return None
    def _defer_outbox_locked(
        self,
        message: InboxMessage,
        now_ms: int,
        *,
        count_attempt: bool,
    ) -> None:
        if count_attempt:
            row = self._service._db.execute(
                "SELECT attempts, expires_at_ms FROM outbox WHERE message_id = ?",
                (message.message_id,),
            ).fetchone()
            if row is None:
                return
            attempts = int(row["attempts"]) + 1
            expires_at_ms = int(row["expires_at_ms"])
            next_attempt_ms = online_retry_next_attempt_ms(
                message_id=message.message_id,
                attempts=attempts,
                now_ms=now_ms,
                expires_at_ms=expires_at_ms,
            )
            self._service._db.execute(
                """UPDATE outbox
                      SET attempts = ?, next_attempt_ms = MIN(expires_at_ms, ?),
                          last_error = ?
                    WHERE message_id = ?""",
                (
                    attempts,
                    next_attempt_ms,
                    "ack not received",
                    message.message_id,
                ),
            )
            self._service._alarm._safe_log(
                "warn",
                "outbox.retry_scheduled",
                messageId=message.message_id,
                recipient=message.recipient,
                sender=message.sender,
                attempts=attempts,
                nextAttemptMs=next_attempt_ms,
                reason="ack not received",
            )
            return
        self._service._db.execute(
            """UPDATE outbox
                  SET next_attempt_ms = expires_at_ms,
                      last_error = ?
                WHERE message_id = ?""",
            ("recipient offline", message.message_id),
        )
    def _reject_correlation(self, correlation_id: str) -> None:
        self._publish(
            PortCommandRejected(
                correlation_id=correlation_id,
                domain="inbox",
                generation=self._generation,
                version=self._version,
                code="CORRELATION_IN_FLIGHT",
                detail="correlation already owns an active dispatch claim",
            )
        )
    def _reconcile_handoffs(self) -> None:
        for correlation_id in self._handoffs.consume_all():
            self._pending_io.pop(correlation_id, None)
        self._refresh_claimed_projection()

    def _refresh_claimed_projection(self) -> None:
        self._projection.replace_claimed(
            frozenset(
                item.message.message_id
                for pending in self._pending_io.values()
                for item in pending.request.items
            )
        )
    def _current_fence(self, generation: int, version: int) -> bool:
        return generation == self._generation and version == self._version
    def _reject_stale(self, correlation_id: str) -> None:
        self._publish(
            PortCommandRejected(
                correlation_id=correlation_id,
                domain="inbox",
                generation=self._generation,
                version=self._version,
                code="STALE_COMMAND",
                detail="delivery command generation/version fence is stale",
            )
        )
    def _committed_version(self) -> int:
        self._version += 1
        self._refresh_projection()
        return self._version
    def _refresh_projection(self) -> None:
        self._projection.replace(
            version=self._version,
            outbox_count=self._service.outbox_count(),
            custody_count=self._service.custody_count(),
            pending=self._service.pending_all(),
            pending_work=self._service.pending_work_recipients(),
        )
    def _publish(self, event: DeliveryCustodyEvent) -> None:
        self._events.publish(event)
    def close_storage(self) -> None:
        """Release this stopped generation's SQLite resources."""

        self._service.close()
