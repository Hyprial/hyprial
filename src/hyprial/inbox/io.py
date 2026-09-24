"""Typed outbound IO over the frozen inbox command/event ports.

The adapter is shared by legacy workflow effects and PAC notifications.  It
never invokes a transport directly: one stable inbox command is submitted and
the correlated terminal event is awaited.  The existing ``workflow-*`` wire
message/idempotency prefix is intentionally retained for durable compatibility;
module ownership no longer depends on the legacy workflow package.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
import json
import threading
import time
from typing import Protocol
from hyprial.dispatch.identity import dispatch_message_id

from hyprial.contracts.ports import PortAdmission, PortCommandRejected
from hyprial.inbox.api import DeliveryLifecycle, InboxMessage
from hyprial.inbox.ports import (
    AcknowledgeCompleted,
    AcknowledgeMessageCommand,
    InboxCommand,
    InboxCommandSink,
    InboxEvent,
    SubmissionCompleted,
    SubmitMessageCommand,
)
from hyprial.inbox.service import ConsumptionState



class InboxIoDeferred(RuntimeError):
    """No terminal result exists yet; retain and retry the same effect."""


class InboxIoError(RuntimeError):
    """A terminal inbox result violated the outbound delivery contract."""

    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


@dataclass(frozen=True, slots=True)
class DeliveredMessage:
    """The stable message id and canonical recipient a delivery reached."""

    message_id: str
    recipient: str


class InboxEventRouterPort(Protocol):
    def claim(self, correlation_id: str) -> bool: ...

    def wait(self, correlation_id: str, timeout: float) -> InboxEvent | None: ...

    def release(self, correlation_id: str) -> None: ...


class CorrelatedInboxEventRouter:
    """Bounded replay cache and waiter router for typed inbox completions."""

    def __init__(self, *, capacity: int = 4096) -> None:
        if capacity < 1:
            raise ValueError("event router capacity must be positive")
        self._capacity = capacity
        self._condition = threading.Condition()
        self._events: OrderedDict[str, InboxEvent] = OrderedDict()
        self._claimed: set[str] = set()

    def publish(self, event: object) -> None:
        if not isinstance(
            event,
            (SubmissionCompleted, AcknowledgeCompleted, PortCommandRejected),
        ):
            return
        with self._condition:
            correlation_id = event.correlation_id
            self._events[correlation_id] = event
            self._events.move_to_end(correlation_id)
            self._claimed.discard(correlation_id)
            while len(self._events) > self._capacity:
                self._events.popitem(last=False)
            self._condition.notify_all()

    def claim(self, correlation_id: str) -> bool:
        with self._condition:
            if correlation_id in self._events or correlation_id in self._claimed:
                return False
            self._claimed.add(correlation_id)
            return True

    def wait(self, correlation_id: str, timeout: float) -> InboxEvent | None:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while correlation_id not in self._events:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
            return self._events[correlation_id]

    def release(self, correlation_id: str) -> None:
        with self._condition:
            self._claimed.discard(correlation_id)
            event = self._events.get(correlation_id)
            if isinstance(event, PortCommandRejected):
                self._events.pop(correlation_id, None)
            self._condition.notify_all()


class InboxDeliveryIoAdapter:
    """Idempotent delivery and acknowledgement over one inbox authority."""

    def __init__(
        self,
        command_sink: InboxCommandSink,
        event_router: InboxEventRouterPort,
        *,
        resolve_target: Callable[[str], str],
        clock_ms: Callable[[], int] | None = None,
        completion_timeout: float = 2.0,
        deliver_user: Callable[[str, str, str], bool] | None = None,
    ) -> None:
        if completion_timeout <= 0:
            raise ValueError("completion timeout must be positive")
        self._commands = command_sink
        self._events = event_router
        self._resolve_target = resolve_target
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._completion_timeout = completion_timeout
        # Owner DM path for ``user:<owner>`` recipients.  Without it a
        # user:-addressed report becomes a durable inbox message that no
        # transport consumes -- retries exhaust into the DLQ and the report
        # is lost while looking delivered to its sender (2026-09-14 defect
        # class B).  Split here, at the delivery layer, so every caller
        # (run reports, PAC notifications) inherits the same behavior.
        self._deliver_user = deliver_user

    @staticmethod
    def message_id(effect_id: str) -> str:
        return dispatch_message_id(effect_id)

    def deliver(
        self,
        *,
        effect_id: str,
        sender: str,
        target: str,
        conversation_id: str,
        text: str,
    ) -> DeliveredMessage:
        message_id = self.message_id(effect_id)
        recipient = str(self._resolve_target(target))
        if recipient.startswith("user:"):
            return self._deliver_to_user(
                message_id=message_id,
                recipient=recipient,
                text=text,
            )
        message = InboxMessage(
            message_id=message_id,
            conversation_id=conversation_id,
            sender=sender,
            recipient=recipient,
            payload=json.dumps(
                {"message": text},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode(),
            intent="request",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=int(self._clock_ms()),
            idempotency_key=f"workflow:{effect_id}",
        )
        event = self._submit_and_wait(
            effect_id,
            SubmitMessageCommand(correlation_id=effect_id, message=message),
        )
        if not isinstance(event, SubmissionCompleted):
            raise InboxIoDeferred(f"delivery {effect_id} has no terminal event")
        if not event.result.accepted:
            raise InboxIoError(
                f"typed inbox refused {message_id}: {event.result.code or 'rejected'}"
            )
        if event.result.message_id != message_id:
            raise InboxIoError(
                f"typed inbox correlation mismatch for {effect_id}", permanent=True
            )
        return DeliveredMessage(message_id=message_id, recipient=recipient)

    def _deliver_to_user(
        self, *, message_id: str, recipient: str, text: str
    ) -> DeliveredMessage:
        """Route one ``user:<owner>`` recipient to its squire DM path.

        There is deliberately no inbox-message fallback: an inbox message
        for ``user:`` has no consumer, so queueing one would only defer the
        failure to retry exhaustion (and the DLQ) while the sender believes
        delivery is pending.  A failed or unwired DM path is a terminal,
        loud error the caller escalates through the alarm path instead.
        """

        if self._deliver_user is None:
            raise InboxIoError(
                f"no owner-DM route for {recipient} on this node; "
                "user:-addressed delivery is not wired here",
                permanent=True,
            )
        try:
            delivered = self._deliver_user(recipient, text, message_id)
        except InboxIoError as error:
            if error.permanent:
                raise
            # A transient squire-receipt timeout is retryable, not terminal
            # (2026-09-14 S1): surface it as deferred so the workflow effect
            # is retained and retried rather than marked permanently failed.
            raise InboxIoDeferred(
                f"owner-DM delivery for {recipient} is retryable: {error}"
            ) from error
        if not delivered:
            raise InboxIoError(
                f"owner-DM delivery for {recipient} was rejected; "
                "no inbox message was written",
                permanent=True,
            )
        return DeliveredMessage(message_id=message_id, recipient=recipient)

    def acknowledge(
        self, *, effect_id: str, recipient: str, message_id: str
    ) -> bool:
        event = self._submit_and_wait(
            effect_id,
            AcknowledgeMessageCommand(
                correlation_id=effect_id,
                recipient=recipient,
                message_id=message_id,
            ),
        )
        if not isinstance(event, AcknowledgeCompleted):
            raise InboxIoDeferred(f"acknowledgement {effect_id} is unsettled")
        return event.result.acknowledged

    def _submit_and_wait(
        self, correlation_id: str, command: InboxCommand
    ) -> InboxEvent | None:
        owner = self._events.claim(correlation_id)
        if owner:
            admission = self._commands.submit(command)
            if admission is not PortAdmission.ACCEPTED:
                self._events.release(correlation_id)
                raise InboxIoDeferred(
                    f"inbox admission for {correlation_id}: {admission}"
                )
        event = self._events.wait(correlation_id, self._completion_timeout)
        if isinstance(event, PortCommandRejected):
            self._events.release(correlation_id)
            raise InboxIoDeferred(
                f"inbox command {correlation_id} rejected: {event.code}"
            )
        if event is None:
            # The admitted command still owns custody.  Keep the claim so a
            # late completion is cached and retry cannot enqueue a duplicate.
            raise InboxIoDeferred(
                f"inbox completion for {correlation_id} is still pending"
            )
        return event


class InboxProjectionAdapter:
    """Read-only projection over the inbox authority's existing RO seam."""

    def __init__(self, reads: object) -> None:
        self._reads = reads

    def read_pending(self) -> tuple[InboxMessage, ...]:
        pending = getattr(self._reads, "pending_all")
        return tuple(pending())

    def read_consumption_state(self, message_id: str) -> str | None:
        consumption_state = getattr(self._reads, "consumption_state")
        try:
            state = consumption_state(message_id)
        except KeyError:
            return None
        if isinstance(state, ConsumptionState):
            return state.value
        return str(state)


__all__ = [
    "CorrelatedInboxEventRouter",
    "DeliveredMessage",
    "InboxDeliveryIoAdapter",
    "InboxEventRouterPort",
    "InboxIoDeferred",
    "InboxIoError",
    "InboxProjectionAdapter",
]
