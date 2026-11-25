from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, TypeAlias

from hyprial.kernel import CommandSink, EventSink, PortCommandRejected
from hyprial.daemon import Alarm, AlarmResult
from hyprial.daemon.impl.delivery.ports import BodyGrant

from hyprial.daemon.impl.inbox.contracts.api  import (
    AckResult,
    FailureResult,
    HarnessFailureSettlement,
    InboxMessage,
    InboxPruneItem,
    OutboxPruneItem,
    ReceiveResult,
    SubmissionResult,
)
from hyprial.daemon.impl.inbox.contracts.origin import (
    OriginClaimResult,
    OriginClaimant,
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
class GrantBodyReleaseCommand:
    correlation_id: str
    grant: BodyGrant
    retention_ms: int
    now_ms: int | None = None


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
    allow_replay: bool = False


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
    origin_claim: OriginClaimant | None = None


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
    origin_claim: OriginClaimant | None = None


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
    origin_claim: OriginClaimant | None = None


@dataclass(frozen=True, slots=True)
class ClaimOriginCommand:
    """Atomically admit and claim scalar origin rows on the inbox actor."""

    correlation_id: str
    recipient: str
    message_ids: tuple[str, ...]
    claimant: OriginClaimant
    now_ms: int | None = None


@dataclass(frozen=True, slots=True)
class ReleaseOriginClaimsCommand:
    """CAS-release headless origin claims matching ``(owner_actor, claim_seq)``."""

    correlation_id: str
    message_ids: tuple[str, ...]
    owner_actor: str
    claim_seq: int


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
    | ClaimOriginCommand
    | ReleaseOriginClaimsCommand
    | PruneOutboxCommand
    | EmitAlarmCommand
    | GrantBodyReleaseCommand
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
class OriginClaimCompleted:
    correlation_id: str
    generation: int
    version: int
    result: OriginClaimResult


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
    | OriginClaimCompleted
    | InboxClosed
    | PortCommandRejected
)
InboxCommandSink: TypeAlias = CommandSink[InboxCommand]
InboxEventSink: TypeAlias = EventSink[InboxEvent]


class InboxProjectionPort(Protocol):
    def read_counts(self) -> InboxCountsProjection: ...

    def read_pending(self, recipient: str) -> tuple[InboxMessage, ...]: ...

    def has_pending_work(self, recipient: str) -> bool: ...

    def read_claimed_message_ids(self) -> frozenset[str]: ...
