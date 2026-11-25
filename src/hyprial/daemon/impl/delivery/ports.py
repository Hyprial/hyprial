"""Ports of the delivery authority (cell model phase A, slice S5; plan §1.1).

Decision notes (phase A, S5; merged plan §2.0 and executor decisions Q6, Q9):

- Delivery is the authority over the inbox engine (Q11): the engine stays
  where it is and imports these pure ports; nothing here imports the inbox,
  OrgFS, cells, conversations or threads.  The class accessor and the row
  decoder are injected by the engine (``message_class_of`` and
  ``stored_message``), so there is no second class rulebook.
- Body release is push-based: the conversation recorder (S6) grants a body
  by ``(source, position, payload_sha256)``.  ``position`` is the row's
  physical identity (``inbox.arrival_id``, ``sender_copies.rowid``), never a
  message id, because a consumed inbox row is replaced when its message id
  is reused.
- ``BodyGrant.retention_ms`` None means "the bound retention source"; the
  facade resolves it, and every value is clamped to 1 h..365 d.  A single
  param key, ``delivery.bodyRetentionMs``, feeds that source (Q9).
- The retention source takes the grant's subject (integration I1): the
  local agent of the granted row, the recipient of an inbox row and the
  sender of a sender copy (``grant_subject``).  Without a subject the source
  is not asked and the code default applies, so one agent's grant never
  reads the minimum over every agent.
- ``HumanWakeGate`` ships in observe mode only (Q6): it logs what it would
  suppress and suppresses nothing.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from hyprial.kernel import MessageClass

DEFAULT_BODY_RETENTION_MS = 604_800_000
MIN_BODY_RETENTION_MS = 3_600_000
MAX_BODY_RETENTION_MS = 365 * 86_400_000
BODY_RETENTION_PARAM = "delivery.bodyRetentionMs"
SOURCE_INBOX = "inbox"
SOURCE_SENDER_COPY = "sender_copies"
BODY_SOURCES = frozenset({SOURCE_INBOX, SOURCE_SENDER_COPY})
INVALID_TEXT_DIGEST = "!invalid"
BODY_REF_MAX_CHARS = 1024
WAKE_GATE_OBSERVE = "observe"

_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


class EnvelopeLike(Protocol):
    """The message shape delivery reads; ``InboxMessage`` satisfies it."""

    @property
    def message_id(self) -> str: ...

    @property
    def conversation_id(self) -> str: ...

    @property
    def sender(self) -> str: ...

    @property
    def recipient(self) -> str: ...

    @property
    def payload(self) -> bytes: ...

    @property
    def intent(self) -> str: ...

    @property
    def idempotency_key(self) -> str | None: ...

    @property
    def created_at_ms(self) -> int: ...

    @property
    def thread_id(self) -> str | None: ...

    @property
    def conversation_cell_id(self) -> str | None: ...

    @property
    def message_class(self) -> MessageClass | None: ...

    def with_payload(self, payload: bytes) -> EnvelopeLike: ...


ClassOf = Callable[[EnvelopeLike], MessageClass]
RowDecoder = Callable[[sqlite3.Row], EnvelopeLike]
RetentionSource = Callable[[str], object]
WakeLog = Callable[..., object]


def is_sha256_hex(value: object) -> bool:
    """Is *value* a lowercase hex SHA-256 digest?"""

    return isinstance(value, str) and _SHA256_HEX.fullmatch(value) is not None


def _require_position(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("position must be a positive integer")


def _require_source(value: object) -> None:
    if value not in BODY_SOURCES:
        raise ValueError(f"source must be one of {sorted(BODY_SOURCES)}")


def clamp_body_retention_ms(value: object) -> int:
    """A retention value clamped to 1 h..365 d; anything else is the default."""

    if isinstance(value, bool) or not isinstance(value, int):
        return DEFAULT_BODY_RETENTION_MS
    return max(MIN_BODY_RETENTION_MS, min(MAX_BODY_RETENTION_MS, value))


@dataclass(frozen=True, slots=True)
class InteractionRow:
    """One stored conversational row the conversation recorder may record.

    ``message_class`` is the row's class from the injected accessor; it is
    carried to the record, never used to leave the row out (plan slice S1).
    """

    source: str
    position: int
    message: EnvelopeLike
    payload_sha256: str
    message_class: MessageClass = MessageClass.INTERACTION


@dataclass(frozen=True, slots=True)
class InteractionPage:
    """A page of interaction rows and the cursor that continues after it.

    ``cursor`` is the last scanned position, so a page whose rows were all
    left out still advances the reader.
    """

    rows: tuple[InteractionRow, ...]
    cursor: int


@dataclass(frozen=True, slots=True)
class BodyGrant:
    """Permission to release one stored body once a record holds it."""

    source: str
    position: int
    payload_sha256: str
    ref: str
    retention_ms: int | None = None

    def __post_init__(self) -> None:
        _require_source(self.source)
        _require_position(self.position)
        if not is_sha256_hex(self.payload_sha256):
            raise ValueError("payload_sha256 must be a lowercase sha256 hex digest")
        if (
            not isinstance(self.ref, str)
            or not self.ref
            or len(self.ref) > BODY_REF_MAX_CHARS
        ):
            raise ValueError(f"ref must be a string of 1..{BODY_REF_MAX_CHARS} chars")
        if self.retention_ms is not None and (
            isinstance(self.retention_ms, bool) or not isinstance(self.retention_ms, int)
        ):
            raise ValueError("retention_ms must be an integer or None")


def grant_subject(row: InteractionRow | None) -> str | None:
    """The agent a grant on *row* is for: inbox recipient, sender-copy sender."""

    if row is None:
        return None
    if row.source == SOURCE_SENDER_COPY:
        subject = row.message.sender
    else:
        subject = row.message.recipient
    return subject if isinstance(subject, str) and subject else None


@dataclass(frozen=True, slots=True)
class BodyRef:
    """What a resolver needs to return the body of one dropped row."""

    source: str
    position: int
    message_id: str
    payload_sha256: str
    ref: str


class BodyResolver(Protocol):
    """Returns the original payload of a dropped row, or None."""

    def resolve(self, ref: BodyRef) -> bytes | None: ...


class HumanWakeGate(Protocol):
    """Decides whether a message may wake a human; observe mode allows all."""

    @property
    def mode(self) -> str: ...

    def observe(self, message: EnvelopeLike, *, path: str) -> bool: ...


__all__ = [
    "BODY_REF_MAX_CHARS",
    "BODY_RETENTION_PARAM",
    "BODY_SOURCES",
    "DEFAULT_BODY_RETENTION_MS",
    "INVALID_TEXT_DIGEST",
    "MAX_BODY_RETENTION_MS",
    "MIN_BODY_RETENTION_MS",
    "SOURCE_INBOX",
    "SOURCE_SENDER_COPY",
    "WAKE_GATE_OBSERVE",
    "BodyGrant",
    "BodyRef",
    "BodyResolver",
    "ClassOf",
    "EnvelopeLike",
    "HumanWakeGate",
    "InteractionPage",
    "InteractionRow",
    "RetentionSource",
    "RowDecoder",
    "WakeLog",
    "clamp_body_retention_ms",
    "grant_subject",
    "is_sha256_hex",
]
