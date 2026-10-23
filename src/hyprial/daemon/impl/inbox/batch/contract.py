"""Immutable inbox-batch types plus the one class/expected-action rule.

A batch is a read-only projection over live inbox rows: nothing here settles,
acknowledges or stores a message.  The types are shared by the authority
selection (``service.projection``), the cursor codec, the renderer and the
spill writer, so no layer re-derives ordering or action guidance.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Literal

from hyprial.daemon.impl.inbox.contracts.api import InboxMessage

__all__ = [
    "CLASS_HUMAN",
    "CLASS_REQUEST",
    "CLASS_SYSTEM",
    "CLASS_WORKFLOW",
    "DEFAULT_INLINE_LIMIT_BYTES",
    "FORMAT",
    "MAX_INLINE_LIMIT_BYTES",
    "MIN_INLINE_LIMIT_BYTES",
    "SCHEMA_VERSION",
    "SOURCE_INBOX",
    "SOURCE_NOTICE",
    "BatchBounds",
    "BatchCursor",
    "BatchItem",
    "BatchMember",
    "BatchRow",
    "BatchSelection",
    "BatchSnapshot",
    "class_rank",
    "classify",
    "inline_limit_from_environment",
]

SCHEMA_VERSION = 1
FORMAT = "inbox-batch-v1"

#: Aggregate UTF-8 budget for the inline rendering (headers, summaries,
#: bodies, delimiters and footer).  Above it the full rendering goes to the
#: actor-private spill file and the injection carries headers plus the path.
DEFAULT_INLINE_LIMIT_BYTES = 16 * 1024
MIN_INLINE_LIMIT_BYTES = 1024
MAX_INLINE_LIMIT_BYTES = 1024 * 1024

SOURCE_INBOX = "inbox"
SOURCE_NOTICE = "notice"

CLASS_HUMAN = "human"
CLASS_REQUEST = "request"
CLASS_WORKFLOW = "workflow"
CLASS_SYSTEM = "system"

_CLASS_RANK = {
    CLASS_HUMAN: 0,
    CLASS_REQUEST: 1,
    CLASS_WORKFLOW: 2,
    CLASS_SYSTEM: 3,
}

Source = Literal["inbox", "notice"]
BatchClass = Literal["human", "request", "workflow", "system"]
ExpectedAction = Literal["reply", "ack"]


@dataclass(frozen=True, slots=True)
class BatchMember:
    """One frozen snapshot identity: source table, message id, arrival order."""

    source: Source
    message_id: str
    ordinal: int


@dataclass(frozen=True, slots=True)
class BatchBounds:
    """Both source high-waters and the shared capture ordinal."""

    inbox_high: int
    notice_high: int
    capture_ordinal: int


@dataclass(frozen=True, slots=True)
class BatchCursor:
    """The authenticated snapshot identity (never a durable batch ledger)."""

    actor: str
    bounds: BatchBounds
    members: tuple[BatchMember, ...]
    deferred: tuple[BatchMember, ...]
    predecessor_digest: str | None = None


@dataclass(frozen=True, slots=True)
class BatchRow:
    """One live row the authority selected, before classification."""

    source: Source
    message: InboxMessage
    ordinal: int
    body_text: str
    body_bytes: int


@dataclass(frozen=True, slots=True)
class BatchSelection:
    """What the authority froze in one transaction, in durable arrival order."""

    actor: str
    bounds: BatchBounds
    rows: tuple[BatchRow, ...]
    deferred: tuple[BatchMember, ...] = ()
    predecessor_digest: str | None = None


@dataclass(frozen=True, slots=True)
class BatchItem:
    """One classified, ordered batch member with its full body."""

    message_id: str
    delivery_id: str
    conversation_id: str
    sender: str
    recipient: str
    intent: str
    kind: Literal["message", "notice"]
    created_at_ms: int
    summary: str
    body: str
    body_bytes: int
    origin: dict[str, object]
    cls: BatchClass
    expected_action: ExpectedAction
    #: The raw stored payload, kept only so the application layer can derive
    #: the sender-standing description; never part of the wire projection.
    payload: bytes = b""

    @classmethod
    def from_row(cls, row: BatchRow) -> BatchItem:
        message = row.message
        kind: Literal["message", "notice"] = (
            "notice" if row.source == SOURCE_NOTICE else "message"
        )
        batch_class, expected = classify(row)
        return cls(
            message_id=message.message_id,
            delivery_id=message.message_id,
            conversation_id=message.conversation_id,
            sender=message.sender,
            recipient=message.recipient,
            intent=message.intent,
            kind=kind,
            created_at_ms=message.created_at_ms,
            summary=message.summary or "",
            body=row.body_text,
            body_bytes=row.body_bytes,
            origin={},
            cls=batch_class,
            expected_action=expected,
            payload=message.payload,
        )

    def with_origin(self, origin: dict[str, object]) -> BatchItem:
        return replace(self, origin=origin)


@dataclass(frozen=True, slots=True)
class BatchSnapshot:
    """A frozen, ordered snapshot and its opaque cursor token."""

    actor: str
    cursor: str
    items: tuple[BatchItem, ...]
    bounds: BatchBounds
    total_body_bytes: int

    @property
    def item_count(self) -> int:
        return len(self.items)


def _is_workflow(row: BatchRow) -> bool:
    key = row.message.idempotency_key
    if isinstance(key, str) and key.startswith("pac-notify:"):
        return True
    return "pac-wf-" in row.message.conversation_id


def classify(row: BatchRow) -> tuple[BatchClass, ExpectedAction]:
    """Class and expected action from authoritative envelope metadata only."""

    message = row.message
    if message.sender.startswith(("user:", "adapter:")):
        cls: BatchClass = CLASS_HUMAN
    elif (
        row.source == SOURCE_INBOX
        and message.intent == "request"
        and not _is_workflow(row)
    ):
        cls = CLASS_REQUEST
    elif _is_workflow(row):
        cls = CLASS_WORKFLOW
    else:
        cls = CLASS_SYSTEM
    reply = (
        row.source == SOURCE_INBOX
        and message.intent == "request"
        and cls in (CLASS_HUMAN, CLASS_REQUEST)
    )
    return cls, ("reply" if reply else "ack")


def class_rank(item: BatchItem) -> int:
    return _CLASS_RANK[item.cls]


#: The one environment variable a daemon reads for the inline budget.
INLINE_LIMIT_ENV = "HYPRIAL_INBOX_BATCH_INLINE_BYTES"


def inline_limit_from_environment() -> int:
    """Read the aggregate inline budget once, loudly refusing junk.

    An unset variable keeps the documented 16 KiB default.  Anything that is
    not an integer in ``MIN..MAX`` is a startup configuration error: silently
    falling back would make the daemon spill (or inline) on an operator's typo.
    """

    raw = os.environ.get(INLINE_LIMIT_ENV)
    if raw is None or raw.strip() == "":
        return DEFAULT_INLINE_LIMIT_BYTES
    try:
        value = int(raw.strip(), 10)
    except ValueError as error:
        raise ValueError(
            f"{INLINE_LIMIT_ENV} must be an integer byte count"
        ) from error
    if not MIN_INLINE_LIMIT_BYTES <= value <= MAX_INLINE_LIMIT_BYTES:
        raise ValueError(
            f"{INLINE_LIMIT_ENV} must be between {MIN_INLINE_LIMIT_BYTES} and "
            f"{MAX_INLINE_LIMIT_BYTES} bytes"
        )
    return value
