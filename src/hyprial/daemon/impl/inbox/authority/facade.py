from __future__ import annotations
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4
from hyprial.daemon import Alarm, AlarmResult
from hyprial.kernel import PortAdmission
from hyprial.daemon.impl.inbox.actor.coordinator  import DeliveryCustodyCoordinator
from hyprial.daemon.impl.inbox.actor.errors  import InboxAuthorityTimeout
from hyprial.daemon.impl.inbox.contracts.api  import (
    AckResult,
    FailureResult,
    HarnessFailureAttempt,
    HarnessFailureSettlement,
    InboxMessage,
    InboxPruneItem,
    LinkInboundRow,
    LinkLedgerOpen,
    LinkLedgerResult,
    LinkLedgerRow,
    OutboxItem,
    OutboxPruneItem,
    ReceiveResult,
    SubmissionResult,
)
from hyprial.daemon.impl.inbox.contracts.ports  import (
    AcceptCustodyCommand,
    AdmittedDispatchableCommand,
    AdmittedPendingCommand,
    AlarmCompleted,
    BatchSnapshotCommand,
    BatchSnapshotCompleted,
    AcknowledgeCompleted,
    AcknowledgeMessageCommand,
    BoolMutationCompleted,
    CloseInboxCommand,
    DismissSystemNoticeCommand,
    DrainSystemNoticesCommand,
    EmitAlarmCommand,
    FailMessageCommand,
    FailureMutationCompleted,
    HarnessFailureSettled,
    FetchPendingCommand,
    InboxClosed,
    InboxPruneCompleted,
    MessagesMutationCompleted,
    OutboxPruneCompleted,
    PruneInboxCommand,
    PruneOutboxCommand,
    ReceiveCompleted,
    ReceiveMessageCommand,
    ReceiveProgressCommand,
    ReceiveSystemNoticeCommand,
    RefreshHoldCommand,
    RetireOutboxReceiptCommand,
    RetryCustodyCommand,
    RetryDueCommand,
    RetryIoCompleted,
    SettleHarnessFailureCommand,
    SubmissionBatchCompleted,
    SubmissionCompleted,
    SubmissionProjection,
    SubmitMessageCommand,
    SubmitProgressCommand,
    WakeOutboxRecipientCommand,
    LinkLedgerUpdated,
)
from hyprial.daemon.impl.inbox.tracking.progress  import ProgressEvent
from hyprial.daemon.impl.inbox.links.pull  import DeliveryStatus
from hyprial.daemon.impl.inbox.service.state  import ConsumptionState
from hyprial.daemon.impl.inbox.authority.read import (
    InboxReadProjection,
    _REPLY_COMPLETION_HANDOFF_GRACE_SECONDS,
)
from hyprial.daemon.impl.inbox.authority.status import DeliveryStatusProjection
from hyprial.daemon.impl.inbox.authority.emitter import ActorAlarmEmitter
from hyprial.daemon.impl.inbox.authority.errors import InboxSubmissionOutcomeUnknown

"""Full inbox mutation authority facade and read-only projection seam."""
DELIVERY_CUSTODY_CALL_TIMEOUT_SECONDS = 2.0
class DeliveryCustodyFacade:
    """Compatibility/system-edge API; every mutation is an actor command."""

    def __init__(
        self,
        coordinator: DeliveryCustodyCoordinator,
        database: Path,
        *,
        timeout: float = DELIVERY_CUSTODY_CALL_TIMEOUT_SECONDS,
    ) -> None:
        self._coordinator = coordinator
        self._database = Path(database)
        self._reads = InboxReadProjection(database)
        self._timeout = timeout
        self.delivery_status = DeliveryStatusProjection(self._reads)
        self.alarm_emitter = ActorAlarmEmitter(self)

    @property
    def generation(self) -> int:
        return self._coordinator.generation

    @property
    def version(self) -> int:
        return self._coordinator.version

    def wait_for_version(self, version: int, timeout: float) -> int:
        """Block until any committed inbox change, or ``timeout``; no I/O."""

        return self._coordinator.wait_for_version(version, timeout)

    def _correlation(self, operation: str) -> str:
        return f"inbox:{operation}:{uuid4().hex}"

    def _call(self, command: object, expected: type[object]) -> object:
        return self._coordinator.call(  # type: ignore[arg-type]
            command,  # type: ignore[arg-type]
            expected,
            timeout=self._timeout,
        )

    @staticmethod
    def _submission(value: SubmissionProjection) -> SubmissionResult:
        return SubmissionResult(
            value.message_id,
            value.accepted,
            queued=value.queued,
            code=value.code,
            custody_mailbox=value.custody_mailbox,
        )

    def submit(
        self,
        message: InboxMessage,
        *,
        now_ms: int | None = None,
        defer_direct: bool = False,
        correlation_id: str | None = None,
        link_hold: str | None = None,
        link_open: LinkLedgerOpen | None = None,
        link_reply_close: bool = False,
    ) -> SubmissionResult:
        # Correlated reply messages retain the same message id across public
        # message.reply retries.  Use it as the durable command identity so a
        # caller timeout or actor-generation change rejoins the existing
        # receipt rather than launching the native reply again.
        correlation = correlation_id
        if (
            correlation is None
            and message.intent == "reply"
            and isinstance(message.idempotency_key, str)
            and message.idempotency_key.startswith("reply:")
        ):
            correlation = f"inbox:reply-submit:{message.message_id}"
        correlation = correlation or self._correlation("submit")
        try:
            event = self._call(
                SubmitMessageCommand(
                    correlation,
                    message,
                    now_ms,
                    defer_direct,
                    link_hold,
                    link_open,
                    link_reply_close,
                ),
                SubmissionCompleted,
            )
        except InboxAuthorityTimeout as error:
            if not correlation.startswith("inbox:reply-submit:"):
                # call() raises this only after successful mailbox admission.
                # Receipt publication can lag a committed result; use that
                # exact durable receipt before classifying the result unknown.
                try:
                    settled = self._reads.submission_result(
                        correlation, message.message_id
                    )
                except (sqlite3.Error, OSError):
                    settled = None
                if settled is not None:
                    return settled
                raise InboxSubmissionOutcomeUnknown(message, correlation) from error
            # The transport timeout is unchanged.  A reply may already have
            # succeeded while its actor completion is crossing the mailbox;
            # join only that same durable receipt for a small handoff grace.
            # Never resubmit the command or repeat external I/O.
            deadline = (
                time.monotonic() + _REPLY_COMPLETION_HANDOFF_GRACE_SECONDS
            )
            while True:
                settled = self._reads.submission_result(
                    correlation,
                    message.message_id,
                )
                if settled is not None:
                    return settled
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(0.01, remaining))
        assert isinstance(event, SubmissionCompleted)
        return self._submission(event.result)

    def receive(
        self,
        message: InboxMessage,
        *,
        now_ms: int | None = None,
    ) -> ReceiveResult:
        event = self._call(
            ReceiveMessageCommand(self._correlation("receive"), message, now_ms),
            ReceiveCompleted,
        )
        assert isinstance(event, ReceiveCompleted)
        return event.result

    def admit_ingress(
        self,
        command: ReceiveMessageCommand | AcceptCustodyCommand
        | ReceiveSystemNoticeCommand | ReceiveProgressCommand
        | RetireOutboxReceiptCommand,
    ) -> PortAdmission:
        """Admit immutable RX input without waiting for commit on the RX lane.

        Admission never signs a receipt. The sender retains durable custody
        until the existing receipt query observes the writer's committed row.
        Overload/closing is returned explicitly so the adapter can report it.
        """
        if not isinstance(command, (
            ReceiveMessageCommand, AcceptCustodyCommand, ReceiveSystemNoticeCommand,
            ReceiveProgressCommand, RetireOutboxReceiptCommand,
        )):
            raise TypeError("unsupported inbox ingress command")
        return self._coordinator.submit(command)

    def ack(self, recipient: str, message_id: str) -> AckResult:
        event = self._call(
            AcknowledgeMessageCommand(
                self._correlation("ack"), recipient, message_id
            ),
            AcknowledgeCompleted,
        )
        assert isinstance(event, AcknowledgeCompleted)
        return event.result

    def refresh_hold(self, message_id: str, *, now_ms: int | None = None) -> bool:
        return self._bool(
            RefreshHoldCommand(
                self._correlation("refresh-hold"), message_id, now_ms
            )
        )

    def fail(self, recipient: str, message_id: str, detail: str) -> FailureResult:
        event = self._call(
            FailMessageCommand(
                self._correlation("fail"), recipient, message_id, detail
            ),
            FailureMutationCompleted,
        )
        assert isinstance(event, FailureMutationCompleted)
        return event.result

    def settle_harness_failure(
        self,
        recipient: str,
        message_id: str,
        failure_code: str,
        *,
        permanent: bool,
        max_attempts: int,
        backoff_ms: tuple[int, ...],
        now_ms: int | None = None,
    ) -> HarnessFailureSettlement:
        event = self._call(
            SettleHarnessFailureCommand(
                self._correlation("settle-harness-failure"),
                recipient,
                message_id,
                failure_code,
                permanent,
                max_attempts,
                backoff_ms,
                now_ms,
            ),
            HarnessFailureSettled,
        )
        assert isinstance(event, HarnessFailureSettled)
        return event.result

    def harness_failure_settlement(
        self, message_id: str
    ) -> HarnessFailureSettlement | None:
        return self._reads.harness_failure_settlement(message_id)

    def harness_failure_attempts(
        self, message_id: str
    ) -> tuple[HarnessFailureAttempt, ...]:
        return self._reads.harness_failure_attempts(message_id)

    def harness_failure_original(self, message_id: str) -> InboxMessage | None:
        return self._reads.harness_failure_original(message_id)

    def harness_failure_route(self, message_id: str) -> InboxMessage | None:
        return self._reads.harness_failure_route(message_id)

    def terminal_failure_settlements(
        self, *, since_ms: int
    ) -> tuple[HarnessFailureSettlement, ...]:
        return self._reads.terminal_failure_settlements(since_ms=since_ms)

    def accept_custody(
        self,
        message: InboxMessage,
        *,
        mailbox_node: str,
        now_ms: int | None = None,
    ) -> AckResult:
        event = self._call(
            AcceptCustodyCommand(
                self._correlation("accept-custody"),
                message,
                mailbox_node,
                now_ms,
            ),
            AcknowledgeCompleted,
        )
        assert isinstance(event, AcknowledgeCompleted)
        return event.result

    def retry_due(self, *, now_ms: int | None = None) -> list[SubmissionResult]:
        now = time.time_ns() // 1_000_000 if now_ms is None else now_ms
        event = self._call(
            RetryDueCommand(
                self._correlation("retry"), self.generation, self.version, now
            ),
            RetryIoCompleted,
        )
        assert isinstance(event, RetryIoCompleted)
        results = [self._submission(item) for item in event.results]
        results.extend(self.retry_custody_due(now_ms=now))
        return results

    def wake_outbox_recipient(
        self, recipient: str, *, now_ms: int | None = None
    ) -> bool:
        now = time.time_ns() // 1_000_000 if now_ms is None else now_ms
        return self._bool(
            WakeOutboxRecipientCommand(
                self._correlation("wake-outbox-recipient"), recipient, now
            )
        )

    def retry_custody_due(
        self, *, now_ms: int | None = None
    ) -> list[SubmissionResult]:
        now = time.time_ns() // 1_000_000 if now_ms is None else now_ms
        event = self._call(
            RetryCustodyCommand(
                self._correlation("retry-custody"),
                self.generation,
                self.version,
                now,
            ),
            SubmissionBatchCompleted,
        )
        assert isinstance(event, SubmissionBatchCompleted)
        return [self._submission(item) for item in event.results]


    def retire_outbox_receipt(
        self,
        sender: str,
        message_id: str,
        *,
        now_ms: int | None = None,
    ) -> bool:
        return self._bool(
            RetireOutboxReceiptCommand(
                self._correlation("retire-receipt"), sender, message_id, now_ms
            )
        )

    def receive_system_notice(self, notice: InboxMessage) -> bool:
        return self._bool(
            ReceiveSystemNoticeCommand(
                self._correlation("receive-notice"), notice
            )
        )

    def drain_system_notices(self, recipient: str) -> tuple[InboxMessage, ...]:
        return self._messages(
            DrainSystemNoticesCommand(
                self._correlation("drain-notices"), recipient
            )
        )

    def dismiss_system_notice(
        self, message_id: str, *, recipient: str | None = None
    ) -> bool:
        return self._bool(
            DismissSystemNoticeCommand(
                self._correlation("dismiss-notice"), message_id, recipient
            )
        )

    def batch_snapshot(
        self,
        recipient: str,
        *,
        keys: tuple[str, ...],
        now_ms: int | None = None,
        cursor: object | None = None,
        after: object | None = None,
        after_digest: str | None = None,
        include_notices: bool = True,
        only: tuple[str, ...] | None = None,
    ) -> object:
        """One frozen batch selection, admitted on the inbox actor thread."""

        event = self._call(
            BatchSnapshotCommand(
                self._correlation("batch-snapshot"),
                recipient,
                tuple(keys),
                now_ms,
                cursor,
                after,
                after_digest,
                include_notices,
                only,
            ),
            BatchSnapshotCompleted,
        )
        assert isinstance(event, BatchSnapshotCompleted)
        return event.selection

    def batch_cursor_key(self) -> bytes:
        """The persistent cursor-MAC key for this installation's inbox."""

        from hyprial.daemon.impl.inbox.batch.cursor import cursor_key

        return cursor_key(self._database, "")

    def submit_progress_event(self, event: ProgressEvent, *, recipient: str) -> bool:
        return self._bool(
            SubmitProgressCommand(
                self._correlation("submit-progress"), event, recipient
            )
        )

    def receive_progress_event(self, message: InboxMessage) -> bool:
        return self._bool(
            ReceiveProgressCommand(
                self._correlation("receive-progress"), message
            )
        )

    def emit_alarm(
        self,
        alarm: Alarm,
        *,
        terminal: bool = True,
        throttle: bool = True,
    ) -> AlarmResult:
        event = self._call(
            EmitAlarmCommand(
                self._correlation("emit-alarm"),
                alarm,
                terminal,
                throttle,
            ),
            AlarmCompleted,
        )
        assert isinstance(event, AlarmCompleted)
        return event.result

    def fetch_pending(
        self,
        recipient: str,
        *,
        now_ms: int | None = None,
        only: frozenset[str] | None = None,
        completed_turn: bool = False,
    ) -> tuple[InboxMessage, ...]:
        return self._messages(
            FetchPendingCommand(
                self._correlation("fetch"),
                recipient,
                now_ms,
                tuple(sorted(only)) if only is not None else None,
                completed_turn,
            )
        )

    def admitted_pending(
        self,
        recipient: str,
        *,
        now_ms: int | None = None,
        exclude: frozenset[str] = frozenset(),
    ) -> tuple[InboxMessage, ...]:
        return self._messages(
            AdmittedPendingCommand(
                self._correlation("admitted-pending"),
                recipient,
                now_ms,
                tuple(sorted(exclude)),
            )
        )

    def admitted_dispatchable(
        self,
        recipient: str,
        *,
        now_ms: int | None = None,
        exclude: frozenset[str] = frozenset(),
    ) -> tuple[InboxMessage, ...]:
        return self._messages(
            AdmittedDispatchableCommand(
                self._correlation("admitted-dispatchable"),
                recipient,
                now_ms,
                tuple(sorted(exclude)),
            )
        )

    def prune_inbox(
        self, *, now_ms: int | None = None
    ) -> tuple[InboxPruneItem, ...]:
        now = time.time_ns() // 1_000_000 if now_ms is None else now_ms
        event = self._call(
            PruneInboxCommand(
                self._correlation("prune-inbox"),
                self.generation,
                self.version,
                now,
            ),
            InboxPruneCompleted,
        )
        assert isinstance(event, InboxPruneCompleted)
        return event.items

    def prune_outbox(
        self,
        *,
        undeliverable: Callable[[str], bool],
        unresolvable: Callable[[str], bool] | None = None,
        now_ms: int | None = None,
        dry_run: bool = False,
    ) -> tuple[OutboxPruneItem, ...]:
        now = time.time_ns() // 1_000_000 if now_ms is None else now_ms
        recipients = {item.message.recipient for item in self._reads.outbox_items()}
        dead = tuple(sorted(recipient for recipient in recipients if undeliverable(recipient)))
        missing = tuple(
            sorted(
                recipient
                for recipient in recipients
                if unresolvable is not None and unresolvable(recipient)
            )
        )
        event = self._call(
            PruneOutboxCommand(
                self._correlation("prune-outbox"),
                self.generation,
                self.version,
                now,
                dead,
                missing,
                dry_run,
            ),
            OutboxPruneCompleted,
        )
        assert isinstance(event, OutboxPruneCompleted)
        return event.items

    def shutdown(self) -> None:
        try:
            event = self._call(
                CloseInboxCommand(self._correlation("close")),
                InboxClosed,
            )
        except InboxAuthorityTimeout:
            # The close is still queued; an unanswered reply is no reason to
            # abandon custody.  Drain anyway so a stalled-but-live actor
            # finishes, stops and releases its SQLite descriptors.  The
            # caller still sees the timeout.
            self._coordinator.drain(self._timeout)
            raise
        assert isinstance(event, InboxClosed)
        report = self._coordinator.drain(self._timeout)
        if not report.complete:
            raise RuntimeError("inbox custody did not drain before its deadline")

    def close(self) -> None:
        """Context-manager compatibility; production composition uses shutdown."""

        self.shutdown()

    def __enter__(self) -> DeliveryCustodyFacade:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _bool(self, command: object) -> bool:
        event = self._call(command, BoolMutationCompleted)
        assert isinstance(event, BoolMutationCompleted)
        return event.result

    def _messages(self, command: object) -> tuple[InboxMessage, ...]:
        event = self._call(command, MessagesMutationCompleted)
        assert isinstance(event, MessagesMutationCompleted)
        return event.messages

    def consumption_state(self, message_id: str) -> ConsumptionState:
        return self._reads.consumption_state(message_id)

    def pending_all(self) -> tuple[InboxMessage, ...]:
        return self._reads.pending_all()

    def next(self, recipient: str) -> InboxMessage | None:
        return self._reads.next(recipient)

    def pending_messages(
        self,
        recipient: str,
        *,
        now_ms: int | None = None,
    ) -> tuple[InboxMessage, ...]:
        return self._reads.pending_messages(recipient, now_ms=now_ms)

    def dispatchable_messages(
        self, recipient: str, *, now_ms: int | None = None
    ) -> tuple[InboxMessage, ...]:
        return self._reads.dispatchable_messages(recipient, now_ms=now_ms)

    def fetched_between(
        self,
        recipient: str,
        *,
        since_ms: int,
        until_ms: int,
        since_inclusive: bool,
    ) -> tuple[str, ...]:
        """Read-only injection window; same semantics as the inbox service."""

        return self._reads.fetched_between(
            recipient,
            since_ms=since_ms,
            until_ms=until_ms,
            since_inclusive=since_inclusive,
        )

    def system_notices(self, recipient: str) -> tuple[InboxMessage, ...]:
        return self._reads.system_notices(recipient)

    def list_progress_events(
        self, recipient: str, *, delivery_id: str | None = None
    ) -> tuple[InboxMessage, ...]:
        return self._reads.list_progress_events(recipient, delivery_id=delivery_id)

    def workflow_replies(self, recipient: str, conversation_id: str) -> tuple[InboxMessage, ...]:
        return self._reads.workflow_replies(recipient, conversation_id)

    def conversation_messages(
        self,
        participant: str,
        conversation_id: str,
        *,
        after: tuple[int, str] | None = None,
        before: tuple[int, str] | None = None,
        limit: int = 50,
        newest_first: bool = False,
    ) -> tuple[InboxMessage, ...]:
        return self._reads.conversation_messages(
            participant,
            conversation_id,
            after=after,
            before=before,
            limit=limit,
            newest_first=newest_first,
        )

    def outbox_item(self, message_id: str) -> OutboxItem:
        return self._reads.outbox_item(message_id)

    def outbox_items(self) -> tuple[OutboxItem, ...]:
        return self._reads.outbox_items()

    def outbox_recipient_page(
        self, *, after: str | None = None, limit: int = 64
    ) -> tuple[str, ...]:
        return self._reads.outbox_recipient_page(after=after, limit=limit)

    def outbox_count(self) -> int:
        return self._reads.outbox_count()

    def next_retry_due_ms(self) -> int | None:
        return self._reads.next_retry_due_ms(
            exclude_message_ids=self._coordinator.read_claimed_message_ids()
        )

    def reply_submission_result(self, message_id: str) -> SubmissionResult | None:
        """Read the durable receipt for one deterministic reply message."""

        return self._reads.submission_result(
            f"inbox:reply-submit:{message_id}",
            message_id,
        )

    def custody_count(self) -> int:
        return self._reads.custody_count()

    # -- send-link ledger (WP2 §3.9) ------------------------------------

    def update_link_ledger(
        self, command: object
    ) -> LinkLedgerResult:
        """Run one durable ledger update on the inbox actor thread."""

        event = self._call(command, LinkLedgerUpdated)
        assert isinstance(event, LinkLedgerUpdated)
        return event.result

    def link_ledger_snapshot(self, now_ms: int) -> tuple[LinkLedgerRow, ...]:
        return self._reads.link_ledger_snapshot(now_ms)

    def link_outbound_row(self, actor: str, message_id: str) -> LinkLedgerRow | None:
        return self._reads.link_outbound_row(actor, message_id)

    def link_held_outbox(self) -> tuple[OutboxItem, ...]:
        return self._reads.link_held_outbox()

    def link_inbound_live(
        self, actor: str, peer: str | None = None
    ) -> tuple[LinkInboundRow, ...]:
        return self._reads.link_inbound_live(actor, peer)

    def link_inbound_row(
        self, actor: str, message_id: str
    ) -> LinkInboundRow | None:
        return self._reads.link_inbound_row(actor, message_id)

    def link_task_bindings(self) -> tuple[tuple[str, str, str, int], ...]:
        return self._reads.link_task_bindings()

    def outbox_item_for_message(self, message_id: str) -> OutboxItem | None:
        return self._reads.outbox_item_for_message(message_id)

    def submission_result_for_message(self, message_id: str) -> SubmissionResult | None:
        """The latest durable submit receipt for one message id, or None."""

        return self._reads.submission_result_by_message(message_id)

    def dlq_count(self) -> int:
        return self._reads.dlq_count()

    def pending_count(self, recipient: str) -> int:
        return self._reads.pending_count(recipient)

    def inbound_awaiting_injection(
        self, recipient: str, candidate_ids: frozenset[str]
    ) -> int:
        """Read-only: alive inbox rows still held out of the context (B6)."""

        return self._reads.held_unfetched_count(recipient, candidate_ids)

    def has_pending_work(self, recipient: str) -> bool:
        """Cache-only actor projection; never opens SQLite on its caller."""

        return self._coordinator.has_pending_work(recipient)

    def pending_recipient_counts(self) -> tuple[tuple[str, int], ...]:
        return self._reads.pending_recipient_counts()

    def pending_recipient_stats(self) -> tuple[tuple[str, int, int], ...]:
        return self._reads.pending_recipient_stats()

    def unfetched_recipient_stats(self) -> tuple[tuple[str, int, int], ...]:
        return self._reads.unfetched_recipient_stats()

    def has_fetched(self, message_id: str) -> bool:
        return self._reads.has_fetched(message_id)

    def is_acknowledged(self, message_id: str) -> bool:
        return self._reads.is_acknowledged(message_id)

    def has_received(self, message_id: str) -> bool:
        return self._reads.has_received(message_id)

    def has_custody(self, message_id: str) -> bool:
        return self._reads.has_custody(message_id)

    def held_expiry_ms(self, message_id: str) -> int | None:
        return self._reads.held_expiry_ms(message_id)

    def delivery_status_records(
        self, sender: str, *, message_id: str | None = None, limit: int = 500
    ) -> tuple[DeliveryStatus, ...]:
        return self._reads.delivery_status_records(
            sender, message_id=message_id, limit=limit
        )

    def delivery_status_for_recipient(
        self, recipient: str, message_id: str
    ) -> DeliveryStatus | None:
        return self._reads.delivery_status_for_recipient(recipient, message_id)
