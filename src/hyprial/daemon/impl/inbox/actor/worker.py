from __future__ import annotations
import threading
import time
import json
from collections import deque
from uuid import NAMESPACE_URL, uuid5
from collections.abc import Callable
from hyprial.daemon import (
    Alarm,
    AlarmDelivery,
    AlarmEmitter,
)
from hyprial.kernel import (
    AdmissionResult,
)
from hyprial.daemon.impl.inbox.contracts.api  import (
    DeliveryLifecycle,
    DeliveryTransport,
    InboxMessage,
)


from .events import (
    DispatchIoCompleted,
    DispatchIoFailed,
    DispatchIoKind,
    DispatchIoRequested,
    DispatchItem,
    DispatchOutcome,
    DispatchOutcomeKind,
)

class DeliveryIoWorker:
    """Bounded independent I/O lanes with FIFO custody per recipient.

    The state actor retains business decisions. This adapter only schedules
    already-typed effects and holds their credits through receipt handoff.
    """

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
        self._queue: deque[DispatchIoRequested] = deque()
        self._condition = threading.Condition()
        self._capacity = capacity + 1  # preserve prior queued + running credit bound
        self._active_keys: dict[str, frozenset[str]] = {}
        self._pending = 0
        self._unsettled: dict[
            str, tuple[DispatchIoRequested, DispatchIoCompleted | DispatchIoFailed]
        ] = {}
        self._closed = False
        self._threads = tuple(threading.Thread(
            target=self._run, name=f"hyprial-delivery-io-{index}", daemon=True,
        ) for index in range(min(4, self._capacity)))
        for thread in self._threads:
            thread.start()

    def submit(self, request: DispatchIoRequested) -> bool:
        with self._condition:
            if self._closed or self._pending >= self._capacity:
                return False
            self._queue.append(request)
            self._pending += 1
            self._condition.notify_all()
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
        with self._condition:
            self._condition.notify_all()
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        return not any(thread.is_alive() for thread in self._threads)

    @staticmethod
    def _request_keys(request: DispatchIoRequested) -> frozenset[str]:
        keys = {item.message.recipient for item in request.items}
        if request.alarm is not None:
            keys.add(request.alarm.sender)
        return frozenset(keys or {request.correlation_id})

    def _take_ready(self) -> DispatchIoRequested | None:
        with self._condition:
            unavailable = set().union(*self._active_keys.values())
            for index, request in enumerate(self._queue):
                keys = self._request_keys(request)
                if keys.isdisjoint(unavailable):
                    del self._queue[index]
                    self._active_keys[request.correlation_id] = keys
                    return request
                # An earlier request blocked on another key must retain its
                # order relative to every later request for this recipient.
                unavailable.update(keys)
            self._condition.wait(0.05)
        return None

    def _run(self) -> None:
        while True:
            request = self._take_ready()
            if request is None:
                self._retry_unsettled_completions()
                with self._condition:
                    if self._closed and self._pending == 0:
                        return
                continue
            try:
                completion = self._execute(request)
            except BaseException as exc:
                completion = DispatchIoFailed(
                    correlation_id=request.correlation_id,
                    generation=request.generation,
                    version=request.version,
                    code="DISPATCH_IO_FAILED",
                    detail=type(exc).__name__,
                    receipt_token=request.receipt_token,
                )
            try:
                settled = self._settle_completion(request, completion)
            except BaseException:
                settled = False  # retain native outcome, never repeat its effect
            if settled:
                with self._condition:
                    self._pending -= 1
                    self._active_keys.pop(request.correlation_id, None)
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

        if request.kind is DispatchIoKind.NOTICE:
            outcomes = []
            for item in request.items:
                if item.target_node is None:
                    raise ValueError("notice dispatch requires a target node")
                try:
                    delivered = bool(
                        self._transport.deliver_notice(
                            item.target_node,
                            item.message,
                        )
                    )
                except Exception:  # noqa: BLE001 - each notice is best-effort
                    delivered = False
                outcomes.append(
                    DispatchOutcome(
                        message_id=item.message.message_id,
                        kind=DispatchOutcomeKind.NOTICE_DELIVERED,
                        recipient_online=delivered,
                    )
                )
            return DispatchIoCompleted(
                correlation_id=request.correlation_id,
                generation=request.generation,
                version=request.version,
                outcomes=tuple(outcomes),
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
        elif alarm.audience == "operator":
            # An operator alarm is local by construction: its sender is this
            # node's own id (a bare node name, not a routable agent URI) and
            # the local operator reads it back via system_notices(node_id).
            # It must NOT be rejected as an unrouteable bare name (2026-09-14
            # defect class A is about user-typed addresses, not the daemon's
            # own operator notice).
            notice = self._alarm_notice(alarm)
            delivered = True
            outcome_kind = DispatchOutcomeKind.ALARM_LOCAL
        elif alarm.audience != "human":
            target_node = self._agent_node(alarm.sender)
            if target_node is None:
                # A recipient that is not an address (a bare name like
                # "hq-adjutant") has no reader: the notices table is keyed by
                # exact URI on every read path, so writing here would record
                # delivery nobody can observe (2026-09-14 defect class A).
                # Fail loud instead -- alarm.failed, no notice row.
                return (
                    False,
                    DispatchOutcomeKind.ALARM_UNROUTABLE,
                    None,
                )
            notice = self._alarm_notice(alarm)
            if target_node == self._node_id:
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
        from hyprial.kernel import parse_agent_uri

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
            self._active_keys.pop(correlation_id, None)
            self._condition.notify_all()
