from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping, Protocol, TypeAlias, runtime_checkable

from hyprial.kernel import CommandSink, EventSink, PortCommandRejected
from hyprial.daemon import Alarm, AlarmResult

from hyprial.daemon.impl.inbox.contracts.api  import (
    AckResult,
    FailureResult,
    HarnessFailureSettlement,
    InboxMessage,
    InboxPruneItem,
    LinkLedgerCensor,
    LinkLedgerOpen,
    LinkLedgerResult,
    OutboxPruneItem,
    ReceiveResult,
    SubmissionResult,
)
from hyprial.daemon.impl.inbox.tracking.progress  import ProgressEvent


class PinnedEventClaim(StrEnum):
    OWNER = "owner"
    WAIT = "wait"
    READY = "ready"
    FULL = "full"


@dataclass(frozen=True, slots=True)
class SubmitMessageCommand:
    correlation_id: str
    message: InboxMessage
    now_ms: int | None = None
    defer_direct: bool = False
    #: Send-link hold reason (WP2 §2.4, §3.6): non-None parks the row in the
    #: outbox with ``next_attempt_ms = expires_at_ms`` and no I/O.
    link_hold: str | None = None
    #: Durable outbound ledger row written in this same transaction (§3.9).
    link_open: LinkLedgerOpen | None = None
    #: Reply accepted: close the original inbound ledger row in this
    #: transaction (B5, §3.9).
    link_reply_close: bool = False


@dataclass(frozen=True, slots=True)
class UpdateLinkLedgerCommand:
    """One inbox transaction's worth of durable link bookkeeping (§3.6)."""

    correlation_id: str
    release: tuple[str, ...] = ()
    reasons: Mapping[str, str] = field(default_factory=dict)
    fail: Mapping[str, str] = field(default_factory=dict)
    drain: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    cancel_outbound: Mapping[str, str] = field(default_factory=dict)
    release_outbound: tuple[tuple[str, str], ...] = ()
    censor: tuple[LinkLedgerCensor, ...] = ()
    sampled: tuple[tuple[str, str], ...] = ()
    open_outbound: tuple[LinkLedgerOpen, ...] = ()
    close_inbound: tuple[tuple[str, str, int], ...] = ()
    bind_tasks: tuple[tuple[str, str, str, int], ...] = ()
    unbind_tasks: tuple[tuple[str, str, str], ...] = ()
    purge_before_ms: int | None = None
    now_ms: int | None = None


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
class WakeOutboxRecipientCommand:
    correlation_id: str
    recipient: str
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
    recipient: str | None = None


@dataclass(frozen=True, slots=True)
class BatchSnapshotCommand:
    """Select one frozen inbox batch on the inbox actor thread (§3.3)."""

    correlation_id: str
    recipient: str
    keys: tuple[str, ...]
    now_ms: int | None = None
    cursor: object | None = None
    after: object | None = None
    after_digest: str | None = None
    include_notices: bool = True
    only: tuple[str, ...] | None = None
    mark_fetched: bool = True


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
    #: Candidate message ids the pull may take.  None means "all live rows".
    #: Admission still happens inside the actor (review B3.3).
    only: tuple[str, ...] | None = None
    #: WP5-A §3.3 (B1): a successful headless turn stamps the first fetch and
    #: clears the row's non-terminal failure retries in the same transaction,
    #: so a body read before a failed turn does not suppress its retry.
    completed_turn: bool = False


@dataclass(frozen=True, slots=True)
class AdmittedPendingCommand:
    """The live pending rows this recipient may inject (review B3.1/B3.2).

    The gate must run on the inbox actor thread; the runtime and the
    interactive list/wait read the admitted set through this command instead
    of re-implementing admission on their own threads.
    """

    correlation_id: str
    recipient: str
    now_ms: int | None = None
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AdmittedDispatchableCommand:
    """Dispatchable (failure backoff elapsed) rows this recipient may inject."""

    correlation_id: str
    recipient: str
    now_ms: int | None = None
    exclude: tuple[str, ...] = ()


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
    | WakeOutboxRecipientCommand
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
    | BatchSnapshotCommand
    | SubmitProgressCommand
    | ReceiveProgressCommand
    | FetchPendingCommand
    | AdmittedPendingCommand
    | AdmittedDispatchableCommand
    | PruneOutboxCommand
    | EmitAlarmCommand
    | UpdateLinkLedgerCommand
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
class BatchSnapshotCompleted:
    correlation_id: str
    generation: int
    version: int
    selection: object


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


@dataclass(frozen=True, slots=True)
class LinkLedgerUpdated:
    correlation_id: str
    generation: int
    version: int
    result: LinkLedgerResult


InboxEvent: TypeAlias = (
    SubmissionCompleted
    | ReceiveCompleted
    | AcknowledgeCompleted
    | RetryIoCompleted
    | InboxPruneCompleted
    | BoolMutationCompleted
    | BatchSnapshotCompleted
    | FailureMutationCompleted
    | HarnessFailureSettled
    | MessagesMutationCompleted
    | SubmissionBatchCompleted
    | OutboxPruneCompleted
    | AlarmCompleted
    | LinkLedgerUpdated
    | InboxClosed
    | PortCommandRejected
)
InboxCommandSink: TypeAlias = CommandSink[InboxCommand]
InboxEventSink: TypeAlias = EventSink[InboxEvent]


@runtime_checkable
class LinkLedgerSink(Protocol):
    """What the inbox tells the link service after a commit (WP2 §3.1).

    All three callbacks run synchronously on the inbox actor thread after the
    transaction commits and change memory only.  The inbox never imports the
    links implementation; it resolves the sink from the one bound gate slot.
    """

    def injected(
        self,
        *,
        actor: str,
        message_id: str,
        sender: str,
        intent: str,
        reply_to: str | None,
        received_at_ms: int,
    ) -> None: ...

    def reply_received(
        self,
        *,
        actor: str,
        reply_message_id: str,
        reply_to: str,
        sender: str,
        received_at_ms: int,
    ) -> None: ...

    def discard(self, *, actor: str, message_ids: object) -> None: ...


class InboxProjectionPort(Protocol):
    def read_counts(self) -> InboxCountsProjection: ...

    def read_pending(self, recipient: str) -> tuple[InboxMessage, ...]: ...

    def has_pending_work(self, recipient: str) -> bool: ...

    def read_claimed_message_ids(self) -> frozenset[str]: ...
