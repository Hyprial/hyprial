from __future__ import annotations
from dataclasses import dataclass
from enum import StrEnum
from hyprial.daemon import (
    Alarm,
)
from hyprial.daemon.impl.inbox.contracts.api  import (
    InboxMessage,
)
from hyprial.daemon.impl.inbox.contracts.ports  import (
    InboxEvent,
)

class DispatchIoKind(StrEnum):
    DELIVERY = "delivery"
    ALARM = "alarm"
    NOTICE = "notice"
    PROGRESS = "progress"
class DispatchOutcomeKind(StrEnum):
    DELIVERED = "delivered"
    CUSTODY = "custody"
    QUEUED = "queued"
    ALARM_DELIVERED = "alarm_delivered"
    ALARM_LOCAL = "alarm_local"
    ALARM_UNROUTABLE = "alarm_unroutable"
    NOTICE_DELIVERED = "notice_delivered"
    PROGRESS_DELIVERED = "progress_delivered"
@dataclass(frozen=True, slots=True)
class DispatchItem:
    message: InboxMessage
    retry: bool = False
    custody_retry: bool = False
    target_node: str | None = None
    source_message_id: str | None = None
@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    message_id: str
    kind: DispatchOutcomeKind
    recipient_online: bool = False
    direct_attempted: bool = False
    custody_mailbox: str | None = None
    notice: InboxMessage | None = None
    notice_local: bool = False

def _alarm_failure(outcome: DispatchOutcome) -> str:
    """Why an alarm dispatch did not reach a reader, for the failure log."""

    if outcome.kind is DispatchOutcomeKind.ALARM_UNROUTABLE:
        return "recipient-not-an-address"
    return "delivery-rejected"

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

DeliveryCustodyEvent = (
    InboxEvent | DispatchIoRequested | DispatchIoCompleted | DispatchIoFailed
)
