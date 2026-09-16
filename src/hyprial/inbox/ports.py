from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeAlias

from hyprial.contracts.ports import CommandSink, EventSink, PortCommandRejected
from hyprial.alarm import Alarm, AlarmResult

from .api import (
    AckResult,
    FailureResult,
    HarnessFailureSettlement,
    InboxMessage,
    InboxPruneItem,
    OutboxPruneItem,
    ReceiveResult,
    SubmissionResult,
)
from .progress import ProgressEvent


@dataclass(frozen=True, slots=True)
class SubmitMessageCommand:
    correlation_id: str
    message: InboxMessage
    now_ms: int | None = None
    defer_direct: bool = False


@dataclass(frozen=True, slots=True)
class ReceiveMessageCommand:
    correlation_id: str
    message: InboxMessage
    now_ms: int | None = None


@dataclass(frozen=True, slots=True)
class AcknowledgeMessageCommand:
    correlation_id: str
    recipient: str
    message_id: str


@dataclass(frozen=True, slots=True)
class RetryDueCommand:
    correlation_id: str
    generation: int
    version: int
    now_ms: int


@dataclass(frozen=True, slots=True)
class PruneInboxCommand:
    correlation_id: str
    generation: int
    version: int
    now_ms: int


@dataclass(frozen=True, slots=True)
class CloseInboxCommand:
    correlation_id: str


@dataclass(frozen=True, slots=True)
class RefreshHoldCommand:
    correlation_id: str
    message_id: str
    now_ms: int | None = None


@dataclass(frozen=True, slots=True)
class FailMessageCommand:
    correlation_id: str
    recipient: str
    message_id: str
    detail: str


@dataclass(frozen=True, slots=True)
class SettleHarnessFailureCommand:
    correlation_id: str
    recipient: str
    message_id: str
    failure_code: str
    permanent: bool
    max_attempts: int
    backoff_ms: tuple[int, ...]
    now_ms: int | None = None


@dataclass(frozen=True, slots=True)
class AcceptCustodyCommand:
    correlation_id: str
    message: InboxMessage
    mailbox_node: str
    now_ms: int | None = None


@dataclass(frozen=True, slots=True)
class RetryCustodyCommand:
    correlation_id: str
    generation: int
    version: int
    now_ms: int


@dataclass(frozen=True, slots=True)
class RetireOutboxReceiptCommand:
    correlation_id: str
    sender: str
    message_id: str
    now_ms: int | None = None


@dataclass(frozen=True, slots=True)
class ReceiveSystemNoticeCommand:
    correlation_id: str
    notice: InboxMessage


@dataclass(frozen=True, slots=True)
class DrainSystemNoticesCommand:
    correlation_id: str
    recipient: str


@dataclass(frozen=True, slots=True)
class DismissSystemNoticeCommand:
    correlation_id: str
    message_id: str


@dataclass(frozen=True, slots=True)
class SubmitProgressCommand:
    correlation_id: str
    event: ProgressEvent
    recipient: str


@dataclass(frozen=True, slots=True)
class ReceiveProgressCommand:
    correlation_id: str
    message: InboxMessage


@dataclass(frozen=True, slots=True)
class FetchPendingCommand:
    correlation_id: str
    recipient: str
    now_ms: int | None = None


@dataclass(frozen=True, slots=True)
class PruneOutboxCommand:
    correlation_id: str
    generation: int
    version: int
    now_ms: int
    undeliverable_recipients: tuple[str, ...] = ()
    unresolvable_recipients: tuple[str, ...] = ()
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class EmitAlarmCommand:
    correlation_id: str
    alarm: Alarm
    terminal: bool = True
    throttle: bool = True


InboxCommand: TypeAlias = (
    SubmitMessageCommand
    | ReceiveMessageCommand
    | AcknowledgeMessageCommand
    | RetryDueCommand
    | PruneInboxCommand
    | RefreshHoldCommand
    | FailMessageCommand
    | SettleHarnessFailureCommand
    | AcceptCustodyCommand
    | RetryCustodyCommand
    | RetireOutboxReceiptCommand
    | ReceiveSystemNoticeCommand
    | DrainSystemNoticesCommand
    | DismissSystemNoticeCommand
    | SubmitProgressCommand
    | ReceiveProgressCommand
    | FetchPendingCommand
    | PruneOutboxCommand
    | EmitAlarmCommand
    | CloseInboxCommand
)


@dataclass(frozen=True, slots=True)
class SubmissionProjection:
    message_id: str
    accepted: bool
    queued: bool = False
    code: str | None = None
    custody_mailbox: str | None = None

    @classmethod
    def from_result(cls, result: SubmissionResult) -> SubmissionProjection:
        return cls(
            message_id=result.message_id,
            accepted=result.accepted,
            queued=result.queued,
            code=result.code,
            custody_mailbox=result.custody_mailbox,
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "messageId": self.message_id,
            "accepted": self.accepted,
            "queued": self.queued,
            **({"code": self.code} if self.code is not None else {}),
            **(
                {"custodyMailbox": self.custody_mailbox}
                if self.custody_mailbox is not None
                else {}
            ),
        }


@dataclass(frozen=True, slots=True)
class InboxCountsProjection:
    version: int
    outbox_count: int
    custody_count: int


@dataclass(frozen=True, slots=True)
class SubmissionCompleted:
    correlation_id: str
    generation: int
    version: int
    result: SubmissionProjection


@dataclass(frozen=True, slots=True)
class ReceiveCompleted:
    correlation_id: str
    generation: int
    version: int
    result: ReceiveResult


@dataclass(frozen=True, slots=True)
class AcknowledgeCompleted:
    correlation_id: str
    generation: int
    version: int
    result: AckResult


@dataclass(frozen=True, slots=True)
class RetryIoCompleted:
    correlation_id: str
    generation: int
    version: int
    results: tuple[SubmissionProjection, ...]


@dataclass(frozen=True, slots=True)
class InboxPruneCompleted:
    correlation_id: str
    generation: int
    version: int
    items: tuple[InboxPruneItem, ...]


@dataclass(frozen=True, slots=True)
class InboxClosed:
    correlation_id: str
    generation: int
    version: int


@dataclass(frozen=True, slots=True)
class BoolMutationCompleted:
    correlation_id: str
    generation: int
    version: int
    operation: str
    result: bool


@dataclass(frozen=True, slots=True)
class FailureMutationCompleted:
    correlation_id: str
    generation: int
    version: int
    result: FailureResult


@dataclass(frozen=True, slots=True)
class HarnessFailureSettled:
    correlation_id: str
    generation: int
    version: int
    result: HarnessFailureSettlement


@dataclass(frozen=True, slots=True)
class MessagesMutationCompleted:
    correlation_id: str
    generation: int
    version: int
    operation: str
    messages: tuple[InboxMessage, ...]


@dataclass(frozen=True, slots=True)
class SubmissionBatchCompleted:
    correlation_id: str
    generation: int
    version: int
    operation: str
    results: tuple[SubmissionProjection, ...]


@dataclass(frozen=True, slots=True)
class OutboxPruneCompleted:
    correlation_id: str
    generation: int
    version: int
    items: tuple[OutboxPruneItem, ...]


@dataclass(frozen=True, slots=True)
class AlarmCompleted:
    correlation_id: str
    generation: int
    version: int
    result: AlarmResult


InboxEvent: TypeAlias = (
    SubmissionCompleted
    | ReceiveCompleted
    | AcknowledgeCompleted
    | RetryIoCompleted
    | InboxPruneCompleted
    | BoolMutationCompleted
    | FailureMutationCompleted
    | HarnessFailureSettled
    | MessagesMutationCompleted
    | SubmissionBatchCompleted
    | OutboxPruneCompleted
    | AlarmCompleted
    | InboxClosed
    | PortCommandRejected
)
InboxCommandSink: TypeAlias = CommandSink[InboxCommand]
InboxEventSink: TypeAlias = EventSink[InboxEvent]


class InboxProjectionPort(Protocol):
    def read_counts(self) -> InboxCountsProjection: ...

    def read_pending(self, recipient: str) -> tuple[InboxMessage, ...]: ...
