"""Public, protocol-neutral durable inbox interfaces.

Callers hand this layer opaque encoded bytes.  This keeps the daemon and proto
packages dependent on a small stable seam and avoids a transport/proto cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable


class DeliveryLifecycle(StrEnum):
    ONLINE_ONLY = "online_only"
    DURABLE_SERVICE = "durable_service"


@dataclass(frozen=True, slots=True)
class InboxMessage:
    message_id: str
    conversation_id: str
    sender: str
    recipient: str
    payload: bytes
    intent: str
    lifecycle: DeliveryLifecycle
    created_at_ms: int
    idempotency_key: str | None = None
    # Transport-layer provenance: the node that put this message on the
    # wire.  Self-reported senders cannot be trusted for forensics (a bare
    # sender name used to be stored verbatim, leaving no trace of the
    # origin); the delivery transports stamp this at the boundary so the
    # physical source survives even when a client bypasses sender
    # canonicalization.  None on frames from pre-stamp peers.
    origin_node: str | None = None

    def with_payload(self, payload: bytes) -> InboxMessage:
        return replace(self, payload=payload)


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    message_id: str
    accepted: bool
    queued: bool = False
    code: str | None = None
    custody_mailbox: str | None = None


@dataclass(frozen=True, slots=True)
class ReceiveResult:
    message_id: str
    accepted: bool
    acknowledged: bool
    duplicate: bool = False
    wake: bool = False
    auto_reply: bool = False
    code: str | None = None


@dataclass(frozen=True, slots=True)
class AckResult:
    message_id: str
    acknowledged: bool
    code: str | None = None


@dataclass(frozen=True, slots=True)
class FailureResult:
    message_id: str
    code: str
    reply_to: str


@dataclass(frozen=True, slots=True)
class HarnessFailureSettlement:
    """Durable per-message harness-failure circuit and terminal tombstone."""

    message_id: str
    recipient: str
    cycle: int
    failure_code: str
    attempts: int
    max_attempts: int
    next_attempt_ms: int | None
    terminal: bool
    permanent: bool
    terminal_reason: str | None
    first_failed_at_ms: int
    updated_at_ms: int
    terminal_at_ms: int | None


@dataclass(frozen=True, slots=True)
class HarnessFailureAttempt:
    """One append-only, secret-free failure observation."""

    message_id: str
    cycle: int
    attempt: int
    failure_code: str
    permanent: bool
    failed_at_ms: int


@dataclass(frozen=True, slots=True)
class OutboxItem:
    message: InboxMessage
    attempts: int
    next_attempt_ms: int
    expires_at_ms: int


@dataclass(frozen=True, slots=True)
class OutboxPruneItem:
    message_id: str
    recipient: str
    reason: str
    created_at_ms: int
    attempts: int


@dataclass(frozen=True, slots=True)
class InboxPruneItem:
    """One unconsumed inbox row evicted by the TTL sweep.

    The recipient node committed the message; no actor ever consumed it
    before its ``expires_at_ms`` deadline (recipient URI mismatch after a
    node rename, a decommissioned actor, a consumer that stopped polling).
    ``reason`` is ``INBOX_TTL_EXPIRED`` for those, or ``TERMINAL_SETTLED``
    when the harness delivery had already failed terminally -- in that case
    the settlement row remains as the durable tombstone and only the dead
    inbox row is evicted.
    """

    message_id: str
    recipient: str
    reason: str
    created_at_ms: int
    received_at_ms: int


@runtime_checkable
class DeliveryTransport(Protocol):
    """Network seam used by the durable state machine."""

    def is_online(self, recipient: str) -> bool: ...

    def deliver(self, message: InboxMessage) -> bool: ...

    def confirm_fetch(self, message: InboxMessage) -> bool:
        """Return durable proof that a pull consumer fetched this message."""
        ...

    def online_mailboxes(self) -> tuple[str, ...]: ...

    def transfer_custody(self, mailbox: str, message: InboxMessage) -> bool: ...

    def deliver_notice(self, node: str, message: InboxMessage) -> bool:
        """Publish a system notice without creating a receipt obligation."""
        ...


@runtime_checkable
class InboxPort(Protocol):
    """Daemon-facing durable queue seam."""

    def submit(
        self, message: InboxMessage, *, now_ms: int | None = None
    ) -> SubmissionResult: ...

    def receive(
        self, message: InboxMessage, *, now_ms: int | None = None
    ) -> ReceiveResult: ...

    def next(self, recipient: str) -> InboxMessage | None: ...

    def ack(self, recipient: str, message_id: str) -> AckResult: ...

    def pending_messages(self, recipient: str) -> tuple[InboxMessage, ...]: ...

    def dispatchable_messages(
        self, recipient: str, *, now_ms: int | None = None
    ) -> tuple[InboxMessage, ...]: ...

    def retry_due(self, *, now_ms: int | None = None) -> list[SubmissionResult]: ...

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
    ) -> HarnessFailureSettlement: ...

    def harness_failure_settlement(
        self, message_id: str
    ) -> HarnessFailureSettlement | None: ...

    def harness_failure_attempts(
        self, message_id: str
    ) -> tuple[HarnessFailureAttempt, ...]: ...

    def prune_inbox(
        self, *, now_ms: int | None = None
    ) -> tuple[InboxPruneItem, ...]: ...

    # Both hold counts belong on the seam: custody is ownership transfer, so
    # a message is in exactly one of them and reporting only the outbox makes
    # a transferred message look like a message that vanished.
    def outbox_count(self) -> int: ...

    def custody_count(self) -> int: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class InboxPaths:
    state_dir: Path

    @property
    def database(self) -> Path:
        return self.state_dir / "inbox.sqlite3"
