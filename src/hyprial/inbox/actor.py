"""Actor-owned durable delivery mutation and stable read projections.

``DeliveryCustody`` is the sole mutation owner on this path.  It reuses the
existing SQLite state machine and schema, but disables the legacy service
business lock because the bounded actor mailbox supplies serialization.
SQLite transaction blocks remain the crash-consistency boundary.

The coordinator never waits for a result.  Submission reports only bounded
admission; every result, including overload/closing rejection, is published as
a correlated typed event from :mod:`hyprial.inbox.ports`.
"""

from __future__ import annotations

import threading
import time
import sqlite3
import hashlib
import json
from collections import deque
from uuid import NAMESPACE_URL, uuid4, uuid5
from dataclasses import dataclass, replace
from enum import StrEnum
from queue import Empty, Full, Queue
from collections.abc import Callable
from pathlib import Path

from hyprial.alarm import (
    ALARM_THROTTLE_WINDOW_MS,
    Alarm,
    AlarmDelivery,
    AlarmEmitter,
    AlarmResult,
    audience_for_sender,
)
from hyprial.actor_runtime import (
    STATE_AUTHORITY,
    ActorHandle,
    ActorRuntime,
    ActorSpec,
    ActorState,
    AdmissionResult,
    DrainReport,
)
from hyprial.contracts.ports import PortAdmission, PortCommandRejected

from .api import (
    DeliveryLifecycle,
    DeliveryTransport,
    InboxMessage,
    ReceiveResult,
    SubmissionResult,
)
from .ports import (
    AcceptCustodyCommand,
    AlarmCompleted,
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
    InboxCommand,
    InboxCountsProjection,
    InboxEvent,
    InboxEventSink,
    InboxProjectionPort,
    InboxPruneCompleted,
    MessagesMutationCompleted,
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
    RetryIoCompleted,
    SettleHarnessFailureCommand,
    SubmissionCompleted,
    SubmissionBatchCompleted,
    SubmissionProjection,
    SubmitMessageCommand,
    SubmitProgressCommand,
)
from .progress import PROGRESS_INTENT, encode_progress_event
from .service import InboxService, RetryPolicy, _bare_sender


class DispatchIoKind(StrEnum):
    DELIVERY = "delivery"
    ALARM = "alarm"
    PROGRESS = "progress"


class DispatchOutcomeKind(StrEnum):
    DELIVERED = "delivered"
    CUSTODY = "custody"
    QUEUED = "queued"
    FETCH_CONFIRMED = "fetch_confirmed"
    FETCH_UNCONFIRMED = "fetch_unconfirmed"
    ALARM_DELIVERED = "alarm_delivered"
    ALARM_LOCAL = "alarm_local"
    PROGRESS_DELIVERED = "progress_delivered"


@dataclass(frozen=True, slots=True)
class DispatchItem:
    message: InboxMessage
    retry: bool = False
    confirm_only: bool = False
    terminal_reason: str | None = None
    terminal_alarm: bool = False
    custody_retry: bool = False
    target_node: str | None = None


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    message_id: str
    kind: DispatchOutcomeKind
    recipient_online: bool = False
    direct_attempted: bool = False
    custody_mailbox: str | None = None
    notice: InboxMessage | None = None
    notice_local: bool = False


@dataclass(frozen=True, slots=True)
class DispatchIoRequested:
    correlation_id: str
    generation: int
    version: int
    kind: DispatchIoKind
    items: tuple[DispatchItem, ...] = ()
    alarm: Alarm | None = None
    alarm_terminal: bool = False
    alarm_claimed: bool = False
    completion_kind: str = ""
    receipt_token: str = ""


@dataclass(frozen=True, slots=True)
class DispatchIoCompleted:
    correlation_id: str
    generation: int
    version: int
    outcomes: tuple[DispatchOutcome, ...]
    completed_at_ms: int
    receipt_token: str = ""


@dataclass(frozen=True, slots=True)
class DispatchIoFailed:
    correlation_id: str
    generation: int
    version: int
    code: str
    detail: str
    receipt_token: str = ""


@dataclass(frozen=True, slots=True)
class ReassociateIoCompletion:
    correlation_id: str
    generation: int
    version: int
    request: DispatchIoRequested
    completion: DispatchIoCompleted | DispatchIoFailed


class CompletionReceiptState(StrEnum):
    PENDING = "pending"
    SETTLED = "settled"
    RETRY = "retry"


class CompletionReceiptClaim(StrEnum):
    NEW = "new"
    WAIT = "wait"
    SETTLED = "settled"
    FULL = "full"


class _CompletionReceipts:
    """Bounded actor-processed receipts; mailbox admission is not settlement."""

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._condition = threading.Condition()
        self._states: dict[str, CompletionReceiptState] = {}
        self._generations: dict[str, int] = {}
        self._order: deque[str] = deque()

    def claim(self, token: str, generation: int) -> CompletionReceiptClaim:
        with self._condition:
            state = self._states.get(token)
            if state is CompletionReceiptState.RETRY:
                self._states[token] = CompletionReceiptState.PENDING
                self._generations[token] = generation
                return CompletionReceiptClaim.NEW
            if state is CompletionReceiptState.PENDING:
                if self._generations[token] != generation:
                    self._generations[token] = generation
                    return CompletionReceiptClaim.NEW
                return CompletionReceiptClaim.WAIT
            if state is CompletionReceiptState.SETTLED:
                return CompletionReceiptClaim.SETTLED
            while len(self._states) >= self._capacity and self._order:
                oldest = self._order[0]
                if self._states[oldest] is CompletionReceiptState.PENDING:
                    return CompletionReceiptClaim.FULL
                self._order.popleft()
                self._states.pop(oldest, None)
                self._generations.pop(oldest, None)
            if len(self._states) >= self._capacity:
                return CompletionReceiptClaim.FULL
            self._states[token] = CompletionReceiptState.PENDING
            self._generations[token] = generation
            self._order.append(token)
            return CompletionReceiptClaim.NEW

    def acknowledge(self, token: str, state: CompletionReceiptState) -> None:
        with self._condition:
            if token not in self._states:
                return
            self._states[token] = state
            self._condition.notify_all()

    def wait(self, token: str, timeout: float) -> CompletionReceiptState | None:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while self._states.get(token) is CompletionReceiptState.PENDING:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
            return self._states.get(token)

    def is_settled(self, token: str) -> bool:
        with self._condition:
            return self._states.get(token) is CompletionReceiptState.SETTLED


DeliveryCustodyEvent = (
    InboxEvent | DispatchIoRequested | DispatchIoCompleted | DispatchIoFailed
)


@dataclass(frozen=True, slots=True)
class _PendingDispatch:
    request: DispatchIoRequested
    completion_kind: str
    pre_results: tuple[SubmissionResult, ...] = ()


class InboxAuthorityUnavailable(RuntimeError):
    pass


class InboxAuthorityTimeout(TimeoutError):
    pass


class _SystemReply:
    def __init__(self, correlation_id: str, expected: type[object]) -> None:
        self.correlation_id = correlation_id
        self.expected = expected
        self._condition = threading.Condition()
        self._event: object | None = None

    def offer(self, event: object) -> bool:
        if not isinstance(event, (self.expected, PortCommandRejected)):
            return False
        with self._condition:
            if self._event is None:
                self._event = event
                self._condition.notify_all()
        return True

    def wait(self, timeout: float) -> object:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while self._event is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise InboxAuthorityTimeout(
                        f"inbox command timed out: {self.correlation_id}"
                    )
                self._condition.wait(remaining)
            return self._event


class _EventFanout:
    def __init__(self, primary: InboxEventSink) -> None:
        self._primary = primary
        self._lock = threading.Lock()
        self._replies: dict[str, _SystemReply] = {}

    def expect(self, correlation_id: str, expected: type[object]) -> _SystemReply:
        reply = _SystemReply(correlation_id, expected)
        with self._lock:
            if correlation_id in self._replies:
                raise ValueError(f"duplicate synchronous correlation: {correlation_id}")
            self._replies[correlation_id] = reply
        return reply

    def cancel(self, correlation_id: str) -> None:
        with self._lock:
            self._replies.pop(correlation_id, None)

    def publish(self, event: object) -> None:
        try:
            self._primary.publish(event)  # type: ignore[arg-type]
        finally:
            correlation_id = getattr(event, "correlation_id", None)
            if isinstance(correlation_id, str):
                with self._lock:
                    reply = self._replies.get(correlation_id)
                if reply is not None and reply.offer(event):
                    self.cancel(correlation_id)


@dataclass(frozen=True, slots=True)
class _DurableSubmissionReceipt:
    command_digest: str
    result: SubmissionProjection


class _SubmissionReceiptConflict(RuntimeError):
    pass


class _DurableCompletionHandoffs:
    """Bounded cross-thread notices backed by already-persisted outbox rows."""

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._lock = threading.Lock()
        self._correlations: set[str] = set()

    def record(self, correlation_id: str) -> bool:
        with self._lock:
            if correlation_id in self._correlations:
                return True
            if len(self._correlations) >= self._capacity:
                return False
            self._correlations.add(correlation_id)
            return True

    def consume_all(self) -> tuple[str, ...]:
        with self._lock:
            correlations = tuple(self._correlations)
            self._correlations.clear()
            return correlations


class DeliveryIoWorker:
    """One bounded external-I/O lane, independent from the state actor."""

    def __init__(
        self,
        transport: DeliveryTransport,
        completion_sink: Callable[
            [DispatchIoRequested, DispatchIoCompleted | DispatchIoFailed],
            AdmissionResult,
        ],
        handoff_sink: Callable[
            [DispatchIoRequested, DispatchIoCompleted | DispatchIoFailed, str], bool
        ],
        *,
        capacity: int = 32,
        alarm_human_delivery: AlarmDelivery | None = None,
        node_id: str = "local",
        completion_deadline: float = 1.0,
        completion_backoff: tuple[float, ...] = (0.005, 0.01, 0.02, 0.05),
    ) -> None:
        if capacity < 1:
            raise ValueError("I/O capacity must be at least 1")
        if completion_deadline <= 0:
            raise ValueError("completion deadline must be positive")
        if not completion_backoff or any(delay <= 0 for delay in completion_backoff):
            raise ValueError("completion backoff must contain positive delays")
        self._transport = transport
        self._completion_sink = completion_sink
        self._handoff_sink = handoff_sink
        self._alarm_human_delivery = alarm_human_delivery
        self._node_id = node_id
        self._completion_deadline = completion_deadline
        self._completion_backoff = completion_backoff
        self._queue: Queue[DispatchIoRequested | None] = Queue(maxsize=capacity)
        self._condition = threading.Condition()
        self._pending = 0
        self._unsettled: dict[
            str, tuple[DispatchIoRequested, DispatchIoCompleted | DispatchIoFailed]
        ] = {}
        self._closed = False
        self._thread = threading.Thread(
            target=self._run,
            name="hyprial-delivery-io",
            daemon=True,
        )
        self._thread.start()

    def submit(self, request: DispatchIoRequested) -> bool:
        with self._condition:
            if self._closed:
                return False
            try:
                self._queue.put_nowait(request)
            except Full:
                return False
            self._pending += 1
            return True

    def drain(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            self._closed = True
        self._retry_unsettled_handoffs()
        with self._condition:
            while self._pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
        try:
            self._queue.put_nowait(None)
        except Full:
            return False
        self._thread.join(max(0.0, deadline - time.monotonic()))
        return not self._thread.is_alive()

    def _run(self) -> None:
        while True:
            try:
                request = self._queue.get(timeout=0.1)
            except Empty:
                self._retry_unsettled_completions()
                with self._condition:
                    if self._closed and self._pending == 0:
                        return
                continue
            if request is None:
                return
            try:
                completion = self._execute(request)
            except Exception as exc:
                completion = DispatchIoFailed(
                    correlation_id=request.correlation_id,
                    generation=request.generation,
                    version=request.version,
                    code="DISPATCH_IO_FAILED",
                    detail=type(exc).__name__,
                    receipt_token=request.receipt_token,
                )
            settled = self._settle_completion(request, completion)
            if settled:
                with self._condition:
                    self._pending -= 1
                    self._condition.notify_all()
            else:
                with self._condition:
                    self._unsettled[request.correlation_id] = (request, completion)

    def _execute(self, request: DispatchIoRequested) -> DispatchIoCompleted:
        if request.kind is DispatchIoKind.ALARM:
            alarm = request.alarm
            if alarm is None:
                raise ValueError("alarm dispatch requires an alarm")
            delivered, outcome_kind, notice = self._dispatch_alarm(alarm)
            return DispatchIoCompleted(
                correlation_id=request.correlation_id,
                generation=request.generation,
                version=request.version,
                outcomes=(
                    DispatchOutcome(
                        message_id=alarm.message_id,
                        kind=outcome_kind,
                        recipient_online=bool(delivered),
                        notice=notice,
                        notice_local=outcome_kind is DispatchOutcomeKind.ALARM_LOCAL,
                    ),
                ),
                completed_at_ms=time.time_ns() // 1_000_000,
                receipt_token=request.receipt_token,
            )

        if request.kind is DispatchIoKind.PROGRESS:
            if len(request.items) != 1 or request.items[0].target_node is None:
                raise ValueError("progress dispatch requires one target node")
            item = request.items[0]
            deliver = getattr(self._transport, "deliver_progress", None)
            delivered = callable(deliver) and bool(
                deliver(item.target_node, item.message)
            )
            return DispatchIoCompleted(
                correlation_id=request.correlation_id,
                generation=request.generation,
                version=request.version,
                outcomes=(
                    DispatchOutcome(
                        message_id=item.message.message_id,
                        kind=DispatchOutcomeKind.PROGRESS_DELIVERED,
                        recipient_online=delivered,
                    ),
                ),
                completed_at_ms=time.time_ns() // 1_000_000,
                receipt_token=request.receipt_token,
            )

        outcomes = tuple(self._dispatch_item(item) for item in request.items)
        return DispatchIoCompleted(
            correlation_id=request.correlation_id,
            generation=request.generation,
            version=request.version,
            outcomes=outcomes,
            completed_at_ms=time.time_ns() // 1_000_000,
            receipt_token=request.receipt_token,
        )

    def _dispatch_item(self, item: DispatchItem) -> DispatchOutcome:
        message = item.message
        if item.confirm_only:
            confirm = getattr(self._transport, "confirm_fetch", None)
            confirmed = callable(confirm) and bool(confirm(message))
            notice: InboxMessage | None = None
            notice_local = False
            alarm_delivered = False
            if (
                not confirmed
                and item.terminal_reason is not None
                and item.terminal_alarm
            ):
                alarm = Alarm(
                    correlation_id=message.message_id,
                    message_id=message.message_id,
                    conversation_id=message.conversation_id,
                    sender=message.sender,
                    recipient=message.recipient,
                    reason=item.terminal_reason,
                    audience=audience_for_sender(message.sender),
                )
                alarm_delivered, alarm_kind, notice = self._dispatch_alarm(alarm)
                notice_local = alarm_kind is DispatchOutcomeKind.ALARM_LOCAL
            return DispatchOutcome(
                message_id=message.message_id,
                kind=(
                    DispatchOutcomeKind.FETCH_CONFIRMED
                    if confirmed
                    else DispatchOutcomeKind.FETCH_UNCONFIRMED
                ),
                recipient_online=alarm_delivered,
                notice=notice,
                notice_local=notice_local,
            )

        online = self._transport.is_online(message.recipient)
        direct_attempted = online
        if online and self._transport.deliver(message):
            return DispatchOutcome(
                message_id=message.message_id,
                kind=DispatchOutcomeKind.DELIVERED,
                recipient_online=True,
                direct_attempted=True,
            )
        if item.custody_retry:
            return DispatchOutcome(
                message_id=message.message_id,
                kind=DispatchOutcomeKind.QUEUED,
                recipient_online=online,
                direct_attempted=direct_attempted,
            )
        for mailbox in self._transport.online_mailboxes():
            if self._transport.transfer_custody(mailbox, message):
                return DispatchOutcome(
                    message_id=message.message_id,
                    kind=DispatchOutcomeKind.CUSTODY,
                    recipient_online=online,
                    direct_attempted=direct_attempted,
                    custody_mailbox=mailbox,
                )
        return DispatchOutcome(
            message_id=message.message_id,
            kind=DispatchOutcomeKind.QUEUED,
            recipient_online=online,
            direct_attempted=direct_attempted,
        )

    def _dispatch_alarm(
        self, alarm: Alarm
    ) -> tuple[bool, DispatchOutcomeKind, InboxMessage | None]:
        delivered = False
        outcome_kind = DispatchOutcomeKind.ALARM_DELIVERED
        notice: InboxMessage | None = None
        if alarm.audience == "human" and self._alarm_human_delivery is not None:
            delivered = self._alarm_human_delivery(
                alarm,
                AlarmEmitter.render(alarm),
            )
        elif alarm.audience != "human":
            notice = self._alarm_notice(alarm)
            target_node = self._agent_node(alarm.sender)
            if target_node is None or target_node == self._node_id:
                delivered = True
                outcome_kind = DispatchOutcomeKind.ALARM_LOCAL
            else:
                delivered = self._transport.deliver_notice(target_node, notice)
        return bool(delivered), outcome_kind, notice

    def _alarm_notice(self, alarm: Alarm) -> InboxMessage:
        notice_id = str(
            uuid5(
                NAMESPACE_URL,
                f"hyprial:alarm:{alarm.message_id}:{alarm.reason}:{alarm.sender}",
            )
        )
        payload = json.dumps(
            {
                "message": AlarmEmitter.render(alarm),
                "originalMessageId": alarm.message_id,
                "reason": alarm.reason,
                "systemNotice": True,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        return InboxMessage(
            message_id=notice_id,
            conversation_id=alarm.conversation_id,
            sender=f"system:hyprial:{self._node_id}",
            recipient=alarm.sender,
            payload=payload,
            intent="system",
            lifecycle=DeliveryLifecycle.ONLINE_ONLY,
            idempotency_key=f"alarm:{alarm.message_id}:{alarm.reason}",
            created_at_ms=time.time_ns() // 1_000_000,
        )

    @staticmethod
    def _agent_node(sender: str) -> str | None:
        from hyprial.uri import parse_agent_uri

        parsed = parse_agent_uri(sender)
        return parsed[1] if parsed is not None else None

    def _settle_completion(
        self,
        request: DispatchIoRequested,
        completion: DispatchIoCompleted | DispatchIoFailed,
    ) -> bool:
        deadline = time.monotonic() + self._completion_deadline
        attempt = 0
        while True:
            try:
                admission = self._completion_sink(request, completion)
            except Exception:
                admission = AdmissionResult.CLOSED
            if admission is AdmissionResult.ACCEPTED:
                return True
            if admission is AdmissionResult.CLOSED:
                return self._handoff_sink(request, completion, "state-actor-closed")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._handoff_sink(
                    request,
                    completion,
                    "completion-admission-deadline",
                )
            delay = self._completion_backoff[
                min(attempt, len(self._completion_backoff) - 1)
            ]
            attempt += 1
            time.sleep(min(delay, remaining))

    def _retry_unsettled_handoffs(self) -> None:
        with self._condition:
            unsettled = tuple(self._unsettled.items())
        for correlation_id, (request, completion) in unsettled:
            try:
                admission = self._completion_sink(request, completion)
            except Exception:
                admission = AdmissionResult.CLOSED
            if admission is AdmissionResult.ACCEPTED or self._handoff_sink(
                request,
                completion,
                "drain-handoff-recheck",
            ):
                self._settle_unresolved(correlation_id)

    def _retry_unsettled_completions(self) -> None:
        with self._condition:
            unsettled = tuple(self._unsettled.items())
        for correlation_id, (_request, completion) in unsettled:
            try:
                admission = self._completion_sink(_request, completion)
            except Exception:
                admission = AdmissionResult.CLOSED
            if admission is AdmissionResult.ACCEPTED:
                self._settle_unresolved(correlation_id)

    def _settle_unresolved(self, correlation_id: str) -> None:
        with self._condition:
            if self._unsettled.pop(correlation_id, None) is None:
                return
            self._pending -= 1
            self._condition.notify_all()


class _ProjectionState(InboxProjectionPort):
    """Atomically swapped immutable read face; readers never touch actor state."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts = InboxCountsProjection(
            version=0,
            outbox_count=0,
            custody_count=0,
        )
        self._pending: tuple[InboxMessage, ...] = ()

    @property
    def version(self) -> int:
        with self._lock:
            return self._counts.version

    def replace(
        self,
        *,
        version: int,
        outbox_count: int,
        custody_count: int,
        pending: tuple[InboxMessage, ...],
    ) -> None:
        counts = InboxCountsProjection(
            version=version,
            outbox_count=outbox_count,
            custody_count=custody_count,
        )
        with self._lock:
            self._counts = counts
            self._pending = pending

    def advance_closed(self, version: int) -> None:
        with self._lock:
            self._counts = InboxCountsProjection(
                version=version,
                outbox_count=self._counts.outbox_count,
                custody_count=self._counts.custody_count,
            )

    def read_counts(self) -> InboxCountsProjection:
        with self._lock:
            return self._counts

    def read_pending(self, recipient: str) -> tuple[InboxMessage, ...]:
        with self._lock:
            return tuple(
                message for message in self._pending if message.recipient == recipient
            )


class _NoStateActorIo:
    """Tripwire: state-owned service code must never cross a transport seam."""

    @staticmethod
    def _forbidden() -> None:
        raise RuntimeError("external I/O attempted on the delivery state actor")

    def is_online(self, recipient: str) -> bool:
        del recipient
        self._forbidden()

    def deliver(self, message: InboxMessage) -> bool:
        del message
        self._forbidden()

    def confirm_fetch(self, message: InboxMessage) -> bool:
        del message
        self._forbidden()

    def online_mailboxes(self) -> tuple[str, ...]:
        self._forbidden()

    def transfer_custody(self, mailbox: str, message: InboxMessage) -> bool:
        del mailbox, message
        self._forbidden()

    def deliver_notice(self, node: str, message: InboxMessage) -> bool:
        del node, message
        self._forbidden()


class _ActorOwnedInboxService(InboxService):
    """Existing schema/state transitions with all failure delivery externalized."""

    def __init__(
        self,
        database: Path,
        *,
        failure_sink: Callable[[InboxMessage, str], None],
        retry_policy: RetryPolicy | None,
        max_inbox_items: int,
        max_custody_bytes: int,
        node_id: str,
        service_options: dict[str, object] | None,
    ) -> None:
        self._failure_sink = failure_sink
        options = dict(service_options or {})
        options.pop("alarm_human_delivery", None)
        super().__init__(
            database,
            _NoStateActorIo(),
            retry_policy=retry_policy,
            max_inbox_items=max_inbox_items,
            max_custody_bytes=max_custody_bytes,
            node_id=node_id,
            _serialized_by_actor=True,
            **options,
        )

    def _emit_failure(self, message: InboxMessage, reason: str) -> None:
        self._failure_sink(message, reason)


class DeliveryCustody:
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
        if isinstance(command, PruneInboxCommand):
            if not self._current_fence(command.generation, command.version):
                self._reject_stale(command.correlation_id)
                return
            items = self._service.prune_inbox(now_ms=command.now_ms)
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

    def _stage_submit(self, command: SubmitMessageCommand) -> None:
        try:
            receipt = self._read_submission_receipt(command)
        except _SubmissionReceiptConflict as error:
            self._publish(
                PortCommandRejected(
                    correlation_id=command.correlation_id,
                    domain="inbox",
                    generation=self._generation,
                    version=self._version,
                    code="SUBMISSION_RECEIPT_CONFLICT",
                    detail=str(error),
                )
            )
            return
        if receipt is not None:
            self._publish(
                SubmissionCompleted(
                    correlation_id=command.correlation_id,
                    generation=self._generation,
                    version=self._version,
                    result=receipt.result,
                )
            )
            return
        if command.correlation_id in self._pending_io:
            self._reject_correlation(command.correlation_id)
            return
        now = self._service._now_ms() if command.now_ms is None else command.now_ms
        self._service._insert_outbox(command.message, now)
        if command.defer_direct:
            self._service._defer_outbox(command.message, now)
            version = self._committed_version()
            result = SubmissionResult(command.message.message_id, True, queued=True)
            self._persist_submission_receipt(
                command.correlation_id,
                command.message,
                result,
                now,
            )
            self._publish_submission(
                command.correlation_id,
                version,
                result,
            )
            return
        version = self._committed_version()
        request = DispatchIoRequested(
            correlation_id=command.correlation_id,
            generation=self._generation,
            version=version,
            kind=DispatchIoKind.DELIVERY,
            items=(DispatchItem(command.message),),
        )
        self._request_io(request, completion_kind="submit")

    def _publish_bool(
        self,
        correlation_id: str,
        operation: str,
        result: bool,
    ) -> None:
        version = self._committed_version()
        self._publish(
            BoolMutationCompleted(
                correlation_id,
                self._generation,
                version,
                operation,
                result,
            )
        )

    def _publish_messages(
        self,
        correlation_id: str,
        operation: str,
        messages: tuple[InboxMessage, ...],
    ) -> None:
        version = self._committed_version()
        self._publish(
            MessagesMutationCompleted(
                correlation_id,
                self._generation,
                version,
                operation,
                messages,
            )
        )

    def _publish_missing(self, correlation_id: str, message_id: str) -> None:
        self._publish(
            PortCommandRejected(
                correlation_id=correlation_id,
                domain="inbox",
                generation=self._generation,
                version=self._version,
                code="MESSAGE_NOT_FOUND",
                detail=message_id,
            )
        )

    def _submit_progress(self, command: SubmitProgressCommand) -> None:
        event = command.event
        message = InboxMessage(
            message_id=str(
                uuid5(
                    NAMESPACE_URL,
                    f"hyprial-progress:{event.delivery_id}:{event.seq}:{event.phase}",
                )
            ),
            conversation_id=event.conversation_id,
            sender=event.actor,
            recipient=command.recipient,
            payload=encode_progress_event(event),
            intent=PROGRESS_INTENT,
            lifecycle=DeliveryLifecycle.ONLINE_ONLY,
            idempotency_key=f"progress:{event.delivery_id}:{event.seq}",
            created_at_ms=event.emitted_at_ms,
        )
        target_node = self._service._agent_node(command.recipient)
        if target_node is None or target_node == self._service.node_id:
            self._publish_bool(
                command.correlation_id,
                "submit_progress_event",
                self._service.receive_progress_event(message),
            )
            return
        request = DispatchIoRequested(
            correlation_id=command.correlation_id,
            generation=self._generation,
            version=self._version,
            kind=DispatchIoKind.PROGRESS,
            items=(DispatchItem(message=message, target_node=target_node),),
        )
        self._request_io(request, completion_kind="progress")

    def _emit_explicit_alarm(self, command: EmitAlarmCommand) -> None:
        alarm = command.alarm
        fields = self._alarm_fields(alarm)
        if command.terminal:
            self._service._alarm._safe_log(
                "error", "terminal.failure", **fields
            )
        now_ms = self._service._now_ms()
        window_start_ms = (
            now_ms // ALARM_THROTTLE_WINDOW_MS
        ) * ALARM_THROTTLE_WINDOW_MS
        if command.throttle:
            claimed = self._service._claim_alarm(
                alarm.conversation_id,
                alarm.reason,
                window_start_ms,
                now_ms,
            )
            if not claimed:
                self._service._alarm._safe_log(
                    "info", "alarm.raised", **fields, throttled=True
                )
                self._publish_alarm_completed(
                    command.correlation_id,
                    AlarmResult("throttled", alarm.audience),
                )
                return
        self._service._alarm._safe_log("warn", "alarm.raised", **fields)
        request = DispatchIoRequested(
            correlation_id=command.correlation_id,
            generation=self._generation,
            version=self._version,
            kind=DispatchIoKind.ALARM,
            alarm=alarm,
            alarm_terminal=command.terminal,
            alarm_claimed=command.throttle,
        )
        self._request_io(request, completion_kind="explicit_alarm")

    @staticmethod
    def _alarm_fields(alarm: Alarm) -> dict[str, object]:
        return {
            "correlationId": alarm.correlation_id,
            "originalMessageId": alarm.message_id,
            "conversationId": alarm.conversation_id,
            "sender": alarm.sender,
            "recipient": alarm.recipient,
            "reason": alarm.reason,
            "audience": alarm.audience,
        }

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
        rows = self._service._db.execute(
            "SELECT * FROM outbox WHERE next_attempt_ms <= ? "
            "ORDER BY created_at_ms, rowid",
            (command.now_ms,),
        ).fetchall()
        items: list[DispatchItem] = []
        for row in rows:
            message = self._service._row_message(row)
            if message.message_id in already_claimed:
                continue
            maximum_attempts = self._service.retry_policy.maximum_attempts_for(
                interactive=self._service._interactive_recipient(message.recipient)
            )
            # A queued correlated reply owes its caller exactly one background
            # native attempt: the submit settled queued without any transport
            # I/O, so expiring the row before that single attempt would leave
            # the durable receipt queued forever with no terminal to join.
            never_attempted_reply = (
                int(row["attempts"]) == 0
                and self._stable_reply_correlation(message) is not None
            )
            expired = (
                command.now_ms >= int(row["expires_at_ms"])
                and not never_attempted_reply
            )
            exhausted = int(row["attempts"]) >= maximum_attempts
            reason = (
                "DELIVERY_EXPIRED"
                if expired
                else "DELIVERY_RETRY_EXHAUSTED" if exhausted else None
            )
            items.append(
                DispatchItem(
                    message=message,
                    retry=True,
                    confirm_only=reason is not None,
                    terminal_reason=reason,
                    terminal_alarm=reason is not None,
                )
            )
        if not items:
            self._publish(
                RetryIoCompleted(
                    correlation_id=command.correlation_id,
                    generation=self._generation,
                    version=self._version,
                    results=(),
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
        self._request_io(request, completion_kind="retry")

    def _claim_custody_retry(self, command: RetryCustodyCommand) -> None:
        if command.correlation_id in self._pending_io:
            self._reject_correlation(command.correlation_id)
            return
        rows = self._service._db.execute(
            "SELECT * FROM custody WHERE next_attempt_ms <= ? "
            "ORDER BY accepted_at_ms, rowid",
            (command.now_ms,),
        ).fetchall()
        items: list[DispatchItem] = []
        completed: list[SubmissionResult] = []
        for row in rows:
            message = self._service._row_message(row)
            if command.now_ms >= int(row["expires_at_ms"]):
                self._service._custody_to_dlq(row, "CUSTODY_EXPIRED", command.now_ms)
                completed.append(
                    SubmissionResult(
                        message.message_id,
                        False,
                        code="CUSTODY_EXPIRED",
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
                **({} if delivered else {"failure": "delivery-rejected"}),
            )
            self._publish(event)
            self._publish_alarm_completed(
                pending.request.correlation_id,
                AlarmResult("delivered" if delivered else "failed", alarm.audience),
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
                AlarmResult("failed", alarm.audience),
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
        version = self._committed_version()
        self._publish_final(pending, version, results)
        return True

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
        return pending

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
        if outcome.kind in {
            DispatchOutcomeKind.DELIVERED,
            DispatchOutcomeKind.FETCH_CONFIRMED,
        }:
            from .pull import HoldReason, TerminalState

            result = SubmissionResult(item.message.message_id, True, queued=False)
            with self._service._db:
                self._service._record_terminal(
                    item.message,
                    state=TerminalState.FETCHED,
                    reason=HoldReason.RECEIPT_CONFIRMED,
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
        if outcome.kind is DispatchOutcomeKind.FETCH_UNCONFIRMED:
            reason = item.terminal_reason or "DELIVERY_RETRY_EXHAUSTED"
            self._service._outbox_to_dlq(
                row,
                reason,
                now_ms,
                emit_failure=not item.terminal_alarm,
                local_notice=(
                    outcome.notice
                    if outcome.notice_local and outcome.notice is not None
                    else None
                ),
            )
            if item.terminal_alarm:
                alarm = Alarm(
                    correlation_id=item.message.message_id,
                    message_id=item.message.message_id,
                    conversation_id=item.message.conversation_id,
                    sender=item.message.sender,
                    recipient=item.message.recipient,
                    reason=reason,
                    audience=audience_for_sender(item.message.sender),
                )
                self._service._alarm._safe_log(
                    "info" if outcome.recipient_online else "error",
                    (
                        "alarm.delivered"
                        if outcome.recipient_online
                        else "alarm.failed"
                    ),
                    **self._alarm_fields(alarm),
                    **(
                        {}
                        if outcome.recipient_online
                        else {"failure": "delivery-rejected"}
                    ),
                )
            result = SubmissionResult(item.message.message_id, False, code=reason)
            with self._service._db:
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
                count_attempt=item.retry or outcome.direct_attempted,
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

    def _create_submission_receipts(self) -> None:
        """D22: durable effect-level settlement owned by the inbox database."""

        with self._service._db:
            self._service._db.execute(
                """CREATE TABLE IF NOT EXISTS actor_submission_receipts (
                       correlation_id TEXT PRIMARY KEY,
                       command_digest TEXT NOT NULL,
                       message_id TEXT NOT NULL,
                       accepted INTEGER NOT NULL,
                       queued INTEGER NOT NULL,
                       code TEXT,
                       custody_mailbox TEXT,
                       recorded_at_ms INTEGER NOT NULL
                   )"""
            )

    def _read_submission_receipt(
        self, command: SubmitMessageCommand
    ) -> _DurableSubmissionReceipt | None:
        row = self._service._db.execute(
            "SELECT * FROM actor_submission_receipts WHERE correlation_id = ?",
            (command.correlation_id,),
        ).fetchone()
        if row is None:
            return None
        digest = self._submission_command_digest(command.message)
        if str(row["command_digest"]) != digest:
            raise _SubmissionReceiptConflict(
                "correlation_id is already settled for a different inbox command"
            )
        return _DurableSubmissionReceipt(
            command_digest=digest,
            result=SubmissionProjection(
                message_id=str(row["message_id"]),
                accepted=bool(row["accepted"]),
                queued=bool(row["queued"]),
                code=(str(row["code"]) if row["code"] is not None else None),
                custody_mailbox=(
                    str(row["custody_mailbox"])
                    if row["custody_mailbox"] is not None
                    else None
                ),
            ),
        )

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

    def _advance_reply_receipt_locked(
        self,
        message: InboxMessage,
        result: SubmissionResult,
        now_ms: int,
    ) -> None:
        """D22: the background outbox settles the receipt its submit left queued.

        Once the single native I/O reaches a terminal outcome the queued
        durable receipt advances to delivered/failed under the same stable
        correlation, so a caller retry joins that terminal instead of holding
        a queued receipt forever.  Settled (non-queued) receipts are never
        rewritten, and the command digest keeps the advance bound to the same
        inbox command.
        """

        if result.queued:
            return
        correlation = self._stable_reply_correlation(message)
        if correlation is None:
            return
        self._service._db.execute(
            """UPDATE actor_submission_receipts
                  SET accepted = ?, queued = 0, code = ?, custody_mailbox = ?,
                      recorded_at_ms = ?
                WHERE correlation_id = ? AND command_digest = ?
                  AND message_id = ? AND queued = 1""",
            (
                int(result.accepted),
                result.code,
                result.custody_mailbox,
                now_ms,
                correlation,
                self._submission_command_digest(message),
                result.message_id,
            ),
        )

    def _settle_submission_receipt_locked(
        self,
        receipt_correlation_id: str | None,
        message: InboxMessage,
        result: SubmissionResult,
        now_ms: int,
    ) -> None:
        if receipt_correlation_id is not None:
            self._persist_submission_receipt_locked(
                receipt_correlation_id,
                message,
                result,
                now_ms,
            )
            return
        self._advance_reply_receipt_locked(message, result, now_ms)

    def _persist_submission_receipt(
        self,
        correlation_id: str,
        message: InboxMessage,
        result: SubmissionResult,
        now_ms: int,
    ) -> None:
        with self._service._db:
            self._persist_submission_receipt_locked(
                correlation_id,
                message,
                result,
                now_ms,
            )

    def _persist_submission_receipt_locked(
        self,
        correlation_id: str,
        message: InboxMessage,
        result: SubmissionResult,
        now_ms: int,
    ) -> None:
        digest = self._submission_command_digest(message)
        self._service._db.execute(
            """INSERT OR IGNORE INTO actor_submission_receipts
                   (correlation_id, command_digest, message_id, accepted, queued,
                    code, custody_mailbox, recorded_at_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                correlation_id,
                digest,
                result.message_id,
                int(result.accepted),
                int(result.queued),
                result.code,
                result.custody_mailbox,
                now_ms,
            ),
        )
        row = self._service._db.execute(
            "SELECT command_digest, message_id FROM actor_submission_receipts "
            "WHERE correlation_id = ?",
            (correlation_id,),
        ).fetchone()
        if (
            row is None
            or str(row["command_digest"]) != digest
            or str(row["message_id"]) != result.message_id
        ):
            raise _SubmissionReceiptConflict(
                "correlation_id is already settled for a different inbox command"
            )

    @staticmethod
    def _submission_command_digest(message: InboxMessage) -> str:
        body = json.dumps(
            {
                "conversation_id": message.conversation_id,
                "sender": message.sender,
                "recipient": message.recipient,
                "payload": message.payload.hex(),
                "intent": message.intent,
                "lifecycle": message.lifecycle.value,
                "idempotency_key": message.idempotency_key,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return "sha256:" + hashlib.sha256(body).hexdigest()

    def _defer_outbox_locked(
        self,
        message: InboxMessage,
        now_ms: int,
        *,
        count_attempt: bool,
    ) -> None:
        if count_attempt:
            row = self._service._db.execute(
                "SELECT attempts FROM outbox WHERE message_id = ?",
                (message.message_id,),
            ).fetchone()
            if row is None:
                return
            attempts = int(row["attempts"]) + 1
            schedule = self._service._retry_schedule(message.recipient)
            delay = schedule[min(attempts - 1, len(schedule) - 1)]
            self._service._db.execute(
                """UPDATE outbox
                      SET attempts = ?, next_attempt_ms = MIN(expires_at_ms, ?),
                          last_error = ?
                    WHERE message_id = ?""",
                (
                    attempts,
                    now_ms + delay * 1000,
                    "recipient offline",
                    message.message_id,
                ),
            )
            return
        self._service._db.execute(
            """UPDATE outbox
                  SET next_attempt_ms = MIN(expires_at_ms, ?)
                WHERE message_id = ?""",
            (
                now_ms + self._service._retry_schedule(message.recipient)[0] * 1000,
                message.message_id,
            ),
        )

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
            from .pull import HoldReason, TerminalState

            with self._service._db:
                self._service._record_terminal(
                    item.message,
                    state=TerminalState.FETCHED,
                    reason=HoldReason.RECEIPT_CONFIRMED,
                    now_ms=now_ms,
                    holder=str(row["mailbox_node"]),
                )
                self._service._db.execute(
                    "DELETE FROM custody WHERE message_id = ?",
                    (item.message.message_id,),
                )
            return SubmissionResult(item.message.message_id, True, queued=False)
        return self._defer_custody_after_failure(item, now_ms)

    def _defer_custody_after_failure(
        self,
        item: DispatchItem,
        now_ms: int,
    ) -> SubmissionResult:
        row = self._service._db.execute(
            "SELECT attempts FROM custody WHERE message_id = ?",
            (item.message.message_id,),
        ).fetchone()
        if row is None:
            return SubmissionResult(item.message.message_id, True, queued=False)
        attempts = int(row["attempts"]) + 1
        schedule = self._service._retry_schedule(item.message.recipient)
        delay = schedule[min(attempts - 1, len(schedule) - 1)]
        with self._service._db:
            self._service._db.execute(
                "UPDATE custody SET attempts = ?, "
                "next_attempt_ms = MIN(expires_at_ms, ?) WHERE message_id = ?",
                (
                    attempts,
                    now_ms + delay * 1000,
                    item.message.message_id,
                ),
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

    def _request_alarm(self, message: InboxMessage, reason: str) -> None:
        now_ms = self._service._now_ms()
        window_start_ms = (
            now_ms // ALARM_THROTTLE_WINDOW_MS
        ) * ALARM_THROTTLE_WINDOW_MS
        if not self._service._claim_alarm(
            message.conversation_id,
            reason,
            window_start_ms,
            now_ms,
        ):
            return
        correlation_id = f"alarm:{message.message_id}:{reason}:{self._version}"
        request = DispatchIoRequested(
            correlation_id=correlation_id,
            generation=self._generation,
            version=self._version,
            kind=DispatchIoKind.ALARM,
            alarm=Alarm(
                correlation_id=message.message_id,
                message_id=message.message_id,
                conversation_id=message.conversation_id,
                sender=message.sender,
                recipient=message.recipient,
                reason=str(reason),
                audience=audience_for_sender(message.sender),
            ),
            alarm_claimed=True,
        )
        self._request_io(request, completion_kind="alarm")

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
        )

    def _publish(self, event: DeliveryCustodyEvent) -> None:
        self._events.publish(event)

    def close_storage(self) -> None:
        """Release this stopped generation's SQLite resources."""

        self._service.close()


class DeliveryCustodyCoordinator(InboxProjectionPort):
    """Bounded, event-only command admission for the delivery authority."""

    def __init__(
        self,
        database: Path,
        transport: DeliveryTransport,
        event_sink: InboxEventSink,
        *,
        runtime: ActorRuntime | None = None,
        mailbox_capacity: int = 128,
        io_capacity: int = 32,
        completion_deadline: float = 1.0,
        completion_backoff: tuple[float, ...] = (0.005, 0.01, 0.02, 0.05),
        completion_receipt_timeout: float = 0.1,
        retry_policy: RetryPolicy | None = None,
        max_inbox_items: int = 10_000,
        max_custody_bytes: int = 1 << 30,
        node_id: str = "local",
        service_options: dict[str, object] | None = None,
    ) -> None:
        if completion_receipt_timeout <= 0:
            raise ValueError("completion receipt timeout must be positive")
        self._runtime = runtime or ActorRuntime()
        self._events = _EventFanout(event_sink)
        self._projection = _ProjectionState()
        self._lifecycle_lock = threading.Lock()
        self._closing = False
        self._factory_generation = 0
        self._active_handler: DeliveryCustody | None = None
        self._database = database
        self._handoffs = _DurableCompletionHandoffs(io_capacity + 1)
        self._receipts = _CompletionReceipts(mailbox_capacity + io_capacity * 8)
        self._completion_receipt_timeout = completion_receipt_timeout
        options = dict(service_options or {})
        alarm_human_delivery = options.pop("alarm_human_delivery", None)
        if alarm_human_delivery is not None and not callable(alarm_human_delivery):
            raise TypeError("alarm_human_delivery must be callable")
        self._io_worker = DeliveryIoWorker(
            transport,
            self._on_io_completion,
            self._record_durable_handoff,
            capacity=io_capacity,
            alarm_human_delivery=alarm_human_delivery,
            node_id=node_id,
            completion_deadline=completion_deadline,
            completion_backoff=completion_backoff,
        )

        def factory() -> Callable[[object], None]:
            with self._lifecycle_lock:
                self._factory_generation += 1
                generation = self._factory_generation
            handler = DeliveryCustody(
                database,
                self._events,
                self._projection,
                self._io_worker,
                self._handoffs,
                self._receipts,
                generation=generation,
                retry_policy=retry_policy,
                max_inbox_items=max_inbox_items,
                max_custody_bytes=max_custody_bytes,
                node_id=node_id,
                service_options=options,
            )
            with self._lifecycle_lock:
                self._active_handler = handler
            return handler

        self._handle: ActorHandle = self._runtime.start(
            ActorSpec(
                name="delivery-custody",
                handler_factory=factory,
                mailbox_capacity=mailbox_capacity,
                supervision_profile=STATE_AUTHORITY,
            )
        )

    @property
    def handle(self) -> ActorHandle:
        return self._handle

    @property
    def generation(self) -> int:
        return self._runtime.snapshot(self._handle).generation

    @property
    def version(self) -> int:
        return self._projection.version

    def submit(self, command: InboxCommand) -> PortAdmission:
        rejection: tuple[PortAdmission, str, str] | None = None
        with self._lifecycle_lock:
            if self._closing:
                rejection = (
                    PortAdmission.CLOSING,
                    "PORT_CLOSING",
                    "delivery coordinator is closing",
                )
            else:
                admission = self._runtime.tell(self._handle, command)
                if admission is AdmissionResult.ACCEPTED:
                    if isinstance(command, CloseInboxCommand):
                        self._closing = True
                    return PortAdmission.ACCEPTED
                if admission is AdmissionResult.OVERLOADED:
                    rejection = (
                        PortAdmission.OVERLOADED,
                        "PORT_OVERLOADED",
                        "delivery actor mailbox capacity is exhausted",
                    )
                else:
                    # CLOSED means "the current generation is not accepting",
                    # which a one-for-one restart produces transiently while
                    # the guardian rebuilds the handler (D12/D13).  Latching
                    # the port on that would make every supervised restart
                    # permanently unrecoverable, so only a terminal guardian
                    # state closes it for good.
                    terminal = self._runtime.snapshot(self._handle).state in {
                        ActorState.QUARANTINED,
                        ActorState.STOPPED,
                    }
                    self._closing = terminal
                    rejection = (
                        PortAdmission.CLOSING,
                        "PORT_CLOSING",
                        (
                            "delivery actor is not accepting commands"
                            if terminal
                            else "delivery actor is restarting"
                        ),
                    )
        assert rejection is not None
        mapped, code, detail = rejection
        self._publish_admission_rejection(
            command,
            admission=mapped,
            code=code,
            detail=detail,
        )
        return mapped

    def call(
        self,
        command: InboxCommand,
        expected: type[object],
        *,
        timeout: float,
    ) -> object:
        """Bounded synchronous wait permitted only at system protocol edges."""

        reply = self._events.expect(command.correlation_id, expected)
        admission = self.submit(command)
        if admission is not PortAdmission.ACCEPTED:
            self._events.cancel(command.correlation_id)
            raise InboxAuthorityUnavailable(
                f"inbox command admission failed: {admission.value}"
            )
        try:
            event = reply.wait(timeout)
        finally:
            self._events.cancel(command.correlation_id)
        if isinstance(event, PortCommandRejected):
            if event.code == "MESSAGE_NOT_FOUND":
                raise KeyError(event.detail)
            raise InboxAuthorityUnavailable(f"{event.code}: {event.detail}")
        return event

    def read_counts(self) -> InboxCountsProjection:
        return self._projection.read_counts()

    def read_pending(self, recipient: str) -> tuple[InboxMessage, ...]:
        return self._projection.read_pending(recipient)

    def drain(self, timeout: float = 5.0) -> DrainReport:
        """Drain state, then I/O, then fenced completions for this domain only."""

        started = time.monotonic()
        deadline = started + max(0.0, timeout)
        with self._lifecycle_lock:
            self._closing = True

        if not self._wait_state_idle(deadline):
            return DrainReport(
                complete=False,
                elapsed=time.monotonic() - started,
                remaining=(self._handle,),
            )
        if not self._io_worker.drain(max(0.0, deadline - time.monotonic())):
            return DrainReport(
                complete=False,
                elapsed=time.monotonic() - started,
                remaining=(self._handle,),
            )
        if not self._wait_state_idle(deadline):
            return DrainReport(
                complete=False,
                elapsed=time.monotonic() - started,
                remaining=(self._handle,),
            )
        complete = False
        complete = self._runtime.stop(
            self._handle,
            timeout=max(0.0, deadline - time.monotonic()),
        )
        if complete:
            with self._lifecycle_lock:
                handler = self._active_handler
            if handler is not None:
                handler.close_storage()
            self._handoffs.consume_all()
        return DrainReport(
            complete=complete,
            elapsed=time.monotonic() - started,
            remaining=() if complete else (self._handle,),
        )

    def _wait_state_idle(self, deadline: float) -> bool:
        while time.monotonic() < deadline:
            snapshot = self._runtime.snapshot(self._handle)
            if snapshot.queued == 0 and snapshot.in_flight == 0:
                return True
            time.sleep(min(0.005, max(0.0, deadline - time.monotonic())))
        snapshot = self._runtime.snapshot(self._handle)
        return snapshot.queued == 0 and snapshot.in_flight == 0

    def _on_io_completion(
        self,
        request: DispatchIoRequested,
        event: DispatchIoCompleted | DispatchIoFailed,
    ) -> AdmissionResult:
        generation = self._runtime.snapshot(self._handle).generation
        if generation != event.generation:
            if (
                request.kind is DispatchIoKind.DELIVERY
                and request.completion_kind != "submit"
            ):
                return AdmissionResult.CLOSED
            command: object = ReassociateIoCompletion(
                correlation_id=event.correlation_id,
                generation=generation,
                version=self._projection.version,
                request=request,
                completion=event,
            )
        else:
            command = event
        if not event.receipt_token:
            return AdmissionResult.CLOSED
        claim = self._receipts.claim(event.receipt_token, generation)
        if claim is CompletionReceiptClaim.FULL:
            return AdmissionResult.OVERLOADED
        if claim is CompletionReceiptClaim.SETTLED:
            return AdmissionResult.ACCEPTED
        if claim is CompletionReceiptClaim.NEW:
            admission = self._runtime.tell(self._handle, command)
            if admission is not AdmissionResult.ACCEPTED:
                self._receipts.acknowledge(
                    event.receipt_token,
                    CompletionReceiptState.RETRY,
                )
                return admission
        receipt = self._receipts.wait(
            event.receipt_token,
            self._completion_receipt_timeout,
        )
        if receipt is CompletionReceiptState.SETTLED:
            return AdmissionResult.ACCEPTED
        if receipt is CompletionReceiptState.RETRY:
            return AdmissionResult.CLOSED
        return AdmissionResult.OVERLOADED

    def _record_durable_handoff(
        self,
        request: DispatchIoRequested,
        completion: DispatchIoCompleted | DispatchIoFailed,
        reason: str,
    ) -> bool:
        if request.kind is not DispatchIoKind.DELIVERY:
            return False
        outbox_ids = tuple(
            item.message.message_id
            for item in request.items
            if not item.custody_retry
        )
        custody_ids = tuple(
            item.message.message_id
            for item in request.items
            if item.custody_retry
        )
        if not self._table_holds_all("outbox", outbox_ids):
            return False
        if not self._table_holds_all("custody", custody_ids):
            return False
        if not self._handoffs.record(request.correlation_id):
            return False
        try:
            self._events.publish(
                DispatchIoFailed(
                    correlation_id=completion.correlation_id,
                    generation=completion.generation,
                    version=completion.version,
                    code="COMPLETION_DURABLY_HANDED_OFF",
                    detail=reason,
                )
            )
        except Exception:
            pass
        if request.receipt_token:
            self._receipts.acknowledge(
                request.receipt_token,
                CompletionReceiptState.SETTLED,
            )
        return True

    def _table_holds_all(self, table: str, message_ids: tuple[str, ...]) -> bool:
        if not message_ids:
            return True
        if table not in {"outbox", "custody"}:
            raise ValueError(f"unsupported durable handoff table: {table}")
        try:
            connection = sqlite3.connect(
                f"{self._database.resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=0.05,
            )
            try:
                placeholders = ",".join("?" for _ in message_ids)
                rows = connection.execute(
                    f"SELECT message_id FROM {table} "
                    f"WHERE message_id IN ({placeholders})",
                    message_ids,
                ).fetchall()
            finally:
                connection.close()
        except sqlite3.Error:
            return False
        held = {str(row[0]) for row in rows}
        return all(message_id in held for message_id in message_ids)

    def _publish_admission_rejection(
        self,
        command: InboxCommand,
        *,
        admission: PortAdmission,
        code: str,
        detail: str,
    ) -> None:
        self._events.publish(
            PortCommandRejected(
                correlation_id=command.correlation_id,
                domain="inbox",
                generation=self._runtime.snapshot(self._handle).generation,
                version=self._projection.version,
                code=code,
                detail=detail,
                admission=admission,
            )
        )
