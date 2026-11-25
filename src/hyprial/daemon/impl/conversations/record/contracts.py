"""Conversation record contracts: ids, keys, refs, entries and ports (plan §2.6).

Decision notes (phase A, S6; merged plan §2.0, executor decisions Q2, Q3, Q9):

- One conversation record id per (owner, thread, legacy conversationId):
  ``keyed_cell_id('conversation', authority=<local owner>,
  key=thread_id + '\\0' + (conversationId or ''))``.  ``conversation`` is a
  storage category, not a cell kind (owner ruling 2026-11-16): no cell
  document exists, the id only names the record directory.  Each owner
  derives its own copy locally, so a cross-owner thread has one record set
  per owner and the envelope ``conversation_cell_id`` stamp is never
  trusted here.
- A record entry is keyed by the stored row's physical identity and payload
  digest, ``<source>:<position>:<payload sha256>``, the same triple a
  ``BodyGrant`` names; never by message id, which can be reused.
- One JSON line per entry, ``format`` ``hyprial-conversation-record/v1``.
  The recorded body is base64, so any payload bytes survive exactly;
  ``payloadSha256`` is the digest of the original stored payload and
  ``redacted`` says whether the redaction hook changed the body.
- A ``RecordRef`` is the grant's ``ref``: ``rec1 <space> <path> <key>``.  It
  names the space, so a resolver never has to guess which owner-home copy
  holds the line.
- Ports are plain protocols: the delivery facade (``InteractionFeed``),
  ``app._conversation_stamp`` (``ThreadIdFor``) and a per-row retention source
  (``RetentionFor``).  This package imports no inbox engine, application or
  composition module.

Decision notes (plan slice S1, rulings R4, R6, R7, D5):

- Every row between two addressed actors is attributed to a conversation.
  ``conversation_attribution`` is the one rule the recorder and the read
  side share: the row's send-link thread (stored stamp, else the thread
  its ends infer) when it has one, else ``conversation_pair_key`` of its
  two ends.  A row that has a thread keeps the exact cell it had before
  this rule existed.
- The pair key covers what has no send-link thread: reserved actors
  (``dispatch-coordinator``, ``_turn-hooks``), link-exempt internal agents,
  the three-segment adapter sender ``adapter:<platform>:<name>``
  and two ends of one kind (user and user).  It is a ``p.``
  digest of the two canonical ends, unordered, so both directions share
  it; it never looks like a ``t.`` id, so the two key spaces cannot meet.
  It is not a send-link: admission and the threads package are unchanged.
- An address that is none of agent, user or adapter (a retired
  ``route:`` peer since I4a, 2026-11-24, a
  ``system:hyprial:<node>`` notice, a bare name) has no pair key, so a
  peerless daemon notice stays unrecorded (D5).
- The entry keeps the attribution key in ``threadId`` (older readers
  require a string there) and gains ``messageClass``.  The format stays
  ``v1``: older readers ignore unknown fields, and a line without
  ``messageClass`` reads as ``interaction`` because nothing else was
  recorded before the field existed.

Decision notes (plan slice S2, owner ruling 2026-11-17):

- A delivered row whose body the recorder may not write (the redaction
  hook refused it, or the entry would be too large) is still recorded as
  a placeholder: the full entry minus the body, plus ``bodyOmitted`` with
  the reason.  The line has no ``body`` key and ``redacted`` is true, so
  no part of the refused content is ever written, nothing grants or
  resolves a body from it, and ``payloadSha256`` still names the
  original payload.
- The format stays ``v1``.  A reader that predates ``bodyOmitted``
  requires ``body`` to be a string, so it drops a placeholder line as
  malformed and keeps answering from its queue exactly as before; it never
  shows an empty body as if it were the message.  A reader that knows the
  field accepts any non-empty reason (another device may write a reason
  this one does not know) and refuses a line that has both a body and a
  reason, or a reason without ``redacted``.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from hyprial.daemon.impl.delivery.ports import (
    BODY_REF_MAX_CHARS,
    BodyGrant,
    EnvelopeLike,
    InteractionPage,
    InteractionRow,
    is_sha256_hex,
)
from hyprial.kernel import (
    ADAPTER_URI_PREFIX,
    MessageClass,
    canonical_agent_uri,
    canonical_user_uri,
    keyed_cell_id,
    parse_adapter_short_uri,
    parse_agent_uri,
    parse_adapter_uri,
    parse_user_uri,
)

RECORD_FORMAT = "hyprial-conversation-record/v1"
REF_SCHEME = "rec1"
CONVERSATION_KIND = "conversation"
RECORDS_DATABASE = "conversation-records.sqlite3"
RECORDER_THREAD = "conversation-recorder"
LOG_COMPONENT = "conversations"
PAIR_KEY_PREFIX = "p."
PAIR_SEED_TAG = b"hyprial-conversation-pair-v1\0"
DEFAULT_MESSAGE_CLASS = MessageClass.INTERACTION.value


class RecordUnavailable(Exception):
    """The record store cannot be reached now; the row is retried later."""


def pair_endpoint(address: object) -> str | None:
    """The canonical spelling of one addressed actor, or None."""

    if not isinstance(address, str) or not address:
        return None
    agent = parse_agent_uri(address)
    if agent is not None:
        return canonical_agent_uri(*agent)
    owner = parse_user_uri(address)
    if owner is not None:
        return canonical_user_uri(owner)
    adapter = parse_adapter_uri(address)
    if adapter is not None:
        return ADAPTER_URI_PREFIX + ":".join(adapter)
    short = parse_adapter_short_uri(address)
    if short is not None:
        return ADAPTER_URI_PREFIX + ":".join(short)
    return None


def conversation_pair_key(a: str, b: str) -> str | None:
    """The ``p.`` key of the unordered pair *a*, *b*, or None."""

    ends = (pair_endpoint(a), pair_endpoint(b))
    if ends[0] is None or ends[1] is None:
        return None
    framed = bytearray(PAIR_SEED_TAG)
    for end in sorted(ends):
        encoded = end.encode("utf-8")
        framed += len(encoded).to_bytes(4, "big") + encoded
    digest = hashlib.sha256(bytes(framed)).digest()[:16]
    return PAIR_KEY_PREFIX + base64.b32encode(digest).decode("ascii").rstrip("=").lower()


def conversation_attribution(
    attribution: str | None, sender: str, recipient: str
) -> str | None:
    """The key a row's conversation is filed under: its thread, else its pair."""

    return attribution or conversation_pair_key(sender, recipient)


def conversation_key(attribution: str, conversation_id: str | None) -> str:
    """The keyed value one conversation's record id is derived from."""

    return f"{attribution}\0{conversation_id or ''}"


def conversation_cell_id(
    authority: str, attribution: str, conversation_id: str | None
) -> str:
    """The ``c.`` id of the conversation *authority* records into."""

    return keyed_cell_id(
        CONVERSATION_KIND, authority, conversation_key(attribution, conversation_id)
    )


def entry_key(source: str, position: int, payload_sha256: str) -> str:
    """The idempotency key of one recorded row."""

    return f"{source}:{position}:{payload_sha256}"


def _plain(value: str) -> bool:
    return bool(value) and not any(character.isspace() for character in value)


@dataclass(frozen=True, slots=True)
class RecordRef:
    """Where one entry lives: space, segment path and entry key."""

    space_id: str
    path: str
    key: str

    def encode(self) -> str:
        if not (_plain(self.space_id) and _plain(self.path) and _plain(self.key)):
            raise ValueError("record ref parts must be non-empty and contain no spaces")
        text = f"{REF_SCHEME} {self.space_id} {self.path} {self.key}"
        if len(text) > BODY_REF_MAX_CHARS:
            raise ValueError("record ref is too long for a body grant")
        return text

    @classmethod
    def parse(cls, text: object) -> RecordRef | None:
        if not isinstance(text, str):
            return None
        parts = text.split(" ")
        if len(parts) != 4 or parts[0] != REF_SCHEME or not all(parts[1:]):
            return None
        return cls(parts[1], parts[2], parts[3])


@dataclass(frozen=True, slots=True)
class RecordDraft:
    """One row about to be recorded; the redaction hook may change ``body``."""

    source: str
    position: int
    payload_sha256: str
    message: EnvelopeLike
    thread_id: str
    conversation_id: str
    body: bytes


_TEXT_FIELDS = (
    ("key", "key"),
    ("source", "source"),
    ("message_id", "messageId"),
    ("conversation_id", "conversationId"),
    ("thread_id", "threadId"),
    ("cell_id", "cellId"),
    ("sender", "sender"),
    ("recipient", "recipient"),
    ("intent", "intent"),
    ("payload_sha256", "payloadSha256"),
)
_INT_FIELDS = (
    ("position", "position"),
    ("created_at_ms", "createdAtMs"),
    ("recorded_at_ms", "recordedAtMs"),
)


@dataclass(frozen=True, slots=True)
class RecordEntry:
    """One line of a conversation record segment."""

    key: str
    source: str
    position: int
    message_id: str
    conversation_id: str
    thread_id: str
    cell_id: str
    sender: str
    recipient: str
    intent: str
    created_at_ms: int
    recorded_at_ms: int
    payload_sha256: str
    body: bytes
    redacted: bool = False
    message_class: str = DEFAULT_MESSAGE_CLASS
    body_omitted: str | None = None

    def placeholder(self, reason: str) -> RecordEntry:
        """This entry without its body, recorded with the *reason* it is omitted."""

        if not reason:
            raise ValueError("a placeholder needs a reason")
        return dataclasses.replace(self, body=b"", redacted=True, body_omitted=reason)

    def to_json(self) -> dict[str, Any]:
        document: dict[str, Any] = {"format": RECORD_FORMAT}
        for attribute, name in (*_TEXT_FIELDS, *_INT_FIELDS):
            document[name] = getattr(self, attribute)
        if self.body_omitted is None:
            document["body"] = base64.b64encode(self.body).decode("ascii")
        else:
            document["bodyOmitted"] = self.body_omitted
        document["redacted"] = self.redacted
        document["messageClass"] = self.message_class
        return document

    def to_line(self) -> str:
        return json.dumps(self.to_json(), separators=(",", ":"), sort_keys=True) + "\n"

    @classmethod
    def from_line(cls, line: str) -> RecordEntry | None:
        """The entry one line holds, or None for anything else."""

        try:
            document = json.loads(line)
        except ValueError:
            return None
        if not isinstance(document, dict) or document.get("format") != RECORD_FORMAT:
            return None
        fields: dict[str, Any] = {}
        for attribute, name in _TEXT_FIELDS:
            value = document.get(name)
            if not isinstance(value, str):
                return None
            fields[attribute] = value
        for attribute, name in _INT_FIELDS:
            value = document.get(name)
            if isinstance(value, bool) or not isinstance(value, int):
                return None
            fields[attribute] = value
        body = document.get("body")
        redacted = document.get("redacted")
        message_class = document.get("messageClass", DEFAULT_MESSAGE_CLASS)
        omitted = document.get("bodyOmitted")
        if not isinstance(redacted, bool):
            return None
        if omitted is not None:
            if not isinstance(omitted, str) or not omitted or body is not None or not redacted:
                return None
            body = ""
        elif not isinstance(body, str):
            return None
        if not isinstance(message_class, str) or not message_class:
            return None
        if not is_sha256_hex(fields["payload_sha256"]):
            return None
        try:
            fields["body"] = base64.b64decode(body.encode("ascii"), validate=True)
        except (ValueError, binascii.Error):
            return None
        return cls(redacted=redacted, message_class=message_class, body_omitted=omitted,
                   **fields)


def parse_segment(content: str) -> dict[str, RecordEntry]:
    """Every well-formed entry of one segment by key; the first line wins."""

    entries: dict[str, RecordEntry] = {}
    for line in content.splitlines():
        entry = RecordEntry.from_line(line) if line else None
        if entry is not None:
            entries.setdefault(entry.key, entry)
    return entries


class InteractionFeed(Protocol):
    """The delivery facade operations the recorder uses (``app._inbox``)."""

    def interaction_rows_after(
        self, source: str, after: int, *, limit: int = 64
    ) -> InteractionPage: ...

    def interaction_row_at(self, source: str, position: int) -> InteractionRow | None: ...

    def grant_body_release(self, grant: BodyGrant) -> bool: ...


ThreadIdFor = Callable[[str, str], str | None]
RetentionFor = Callable[[InteractionRow], int | None]
Redaction = Callable[[RecordDraft], RecordDraft | None]
Logger = Callable[..., object]


__all__ = [
    "CONVERSATION_KIND",
    "DEFAULT_MESSAGE_CLASS",
    "LOG_COMPONENT",
    "PAIR_KEY_PREFIX",
    "RECORDER_THREAD",
    "RECORDS_DATABASE",
    "RECORD_FORMAT",
    "REF_SCHEME",
    "InteractionFeed",
    "Logger",
    "RecordDraft",
    "RecordEntry",
    "RecordRef",
    "RecordUnavailable",
    "Redaction",
    "RetentionFor",
    "ThreadIdFor",
    "conversation_attribution",
    "conversation_cell_id",
    "conversation_key",
    "conversation_pair_key",
    "entry_key",
    "pair_endpoint",
    "parse_segment",
]
