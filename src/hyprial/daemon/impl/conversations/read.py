"""Conversation read side: queue heads, list cursors and the filtered walks.

Decision notes (cell model phase B, S6-read; design §9.3, §10.0, plan §2.0):

- A conversation is one cell per (local owner, thread, legacy
  ``conversationId``): the same ``conversation_cell_id`` the recorder
  writes into.  This module derives that id from the delivery queue; the
  record read model (``conversations.document``, ``conversations.merge``,
  plan slice RM1) adds the cells and messages only the record holds, and
  a read still answers from these queue heads alone when the record
  cannot be read.
- A row travels on its stored ``thread_id`` when it has one, else on the
  thread its two ends infer: the recorder's rule
  (``message.thread_id or thread_id_for(sender, recipient)``), applied by
  ``row_thread`` to the list, the thread filter and the queue threads
  alike, so every ``c.`` id a read names is the cell the recorder writes.
  A stored id the ends do not infer (a sender stamp, a reply inheriting
  its original's thread) keeps its own cell; its kind and agent side are
  taken from any row of the caller that infers that id, else unknown.
- ``conversation_heads`` folds the queue's per (legacy conversation,
  sender, recipient, stored thread) groups into conversations.  Groups
  that land in one cell merge: counts add up and the newest
  ``(createdAtMs, messageId)`` gives the peer and the message class.

Decision notes (plan slice S1, rulings R4, R6, D5):

- A conversation is filed under ``row_conversation``: the row's thread
  (``row_thread``) when it has one, else the pair key of its two ends,
  through the same ``conversation_attribution`` the recorder applies.  So
  reserved actors, internal agents and the three-segment adapter sender
  are listed, and a threaded row keeps the cell it had before.  A group
  with neither (a ``system:`` sender, a bare name) is left out, exactly as
  the recorder skips it as ``unthreaded``.
- ``row_thread`` and ``queue_threads`` stay thread-only: ``threads.*``
  read through them, and a pair key is not a send-link thread.
- A pair conversation has no thread kind (``kind`` null) and publishes
  ``threadId`` null; the head keeps its pair key internally, because the
  history read filters on it.
- ``messageClass`` of a head is the class of its newest message as the
  group read gives it (``message_class``), null when the group read does
  not carry one.
- A reader with more than one read key (release D9) gets one head set and
  one queue-thread map per key, each with that key as its participant;
  ``merge_heads`` and ``merge_queue_threads`` fold them.  Rows keep the
  thread their own ends infer (no rewrite).
- ``queue_threads`` is the caller's thread set as the queue records it;
  ``threads.*`` add it to the thread authority's projection so a thread a
  conversation names stays readable after the authority is rebuilt.
- The queue groups are already deduplicated by message id (an inbox row,
  a pending outbox row and a sender copy of one message count once); a
  conversation therefore counts each message once even when one node holds
  both ends of it.
- The list is ordered newest first by ``(lastMessageAtMs desc,
  conversationId asc)`` and paged by a keyset cursor bound to the
  participant and the optional thread filter.  A conversation that gets a
  new message between two pages moves to the front; the cursor never
  repeats a position, so a page never loops.
- ``thread_filtered`` turns one legacy-conversation reader into the reader
  of one thread inside it, through ``filtered_read``: it re-reads in
  bounded batches past rows it drops until the page is full or the source
  is exhausted, so the keyset positions it hands back are always those of
  returned rows and the shared history paging stays exact.  The record
  merge uses the same walk to drop queue rows the record already holds.
- (slice GC2) A list cursor also binds the listed ``kinds`` when they are
  not the default (direct conversations alone): the payload then carries
  the sorted kinds, and a cursor is refused under any other ``kinds``.  A
  default cursor has no ``kinds`` key, so its bytes are what they were.
- The module is pure: thread keys arrive as a callable (the thread
  authority's ``thread_key``), rows as duck-typed objects.  It imports no
  inbox engine, threads, application or composition module.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from hyprial.daemon.impl.conversations.record.contracts import (
    conversation_attribution,
    conversation_cell_id,
)
from hyprial.kernel import is_thread_id_segment

LIST_CURSOR_VERSION = 1
LIST_CURSOR_MAX_CHARS = 4096
_LIST_CURSOR_KEYS = frozenset(
    {"v", "participant", "lastMessageAtMs", "conversationId"}
)

__all__ = [
    "LIST_CURSOR_VERSION",
    "ConversationGroup",
    "ConversationHead",
    "QueueThread",
    "DirectKeyFor",
    "DirectKeyLike",
    "conversation_heads",
    "decode_list_cursor",
    "encode_list_cursor",
    "filtered_read",
    "head_order",
    "merge_heads",
    "merge_queue_threads",
    "page_heads",
    "peer_of",
    "published_thread_id",
    "queue_threads",
    "row_conversation",
    "row_thread",
    "thread_filtered",
]


class ConversationGroup(Protocol):
    """One (legacy conversation, sender, recipient, stored thread) group.

    A group may also carry ``message_class``, the class of its newest row.
    """

    conversation_id: str
    sender: str
    recipient: str
    last_created_at_ms: int
    last_message_id: str
    messages: int
    thread_id: str | None


class DirectKeyLike(Protocol):
    """The key fields a read needs (``conversations.DirectKey``)."""

    id: str
    kind: str
    agent: str
    counterpart: str


class Positioned(Protocol):
    """A history row: its keyset position, its two ends, its stored thread."""

    created_at_ms: int
    message_id: str
    sender: str
    recipient: str
    thread_id: str | None


DirectKeyFor = Callable[[str, str], DirectKeyLike | None]


@dataclass(frozen=True, slots=True)
class ConversationHead:
    """One conversation of a participant, as the list shows it."""

    conversation_id: str
    thread_id: str
    kind: str | None
    peer: str
    legacy_conversation_id: str
    last_message_at_ms: int
    last_message_id: str
    messages: int
    message_class: str | None = None

    @property
    def position(self) -> tuple[int, str]:
        return self.last_message_at_ms, self.conversation_id

    def to_json(self) -> dict[str, Any]:
        return {
            "conversationId": self.conversation_id,
            "kind": self.kind,
            "peer": self.peer,
            "legacyConversationId": self.legacy_conversation_id,
            "lastMessageAtMs": self.last_message_at_ms,
            "lastMessageId": self.last_message_id,
            "messages": self.messages,
            "messageClass": self.message_class,
        }


def published_thread_id(key: object) -> str | None:
    """*key* when it is a send-link ``t.`` id, else None (a pair key)."""

    return key if isinstance(key, str) and is_thread_id_segment(key) else None


def head_order(head: ConversationHead) -> tuple[int, str]:
    return -head.last_message_at_ms, head.conversation_id


def row_thread(
    sender: str,
    recipient: str,
    stored: str | None,
    thread_key: DirectKeyFor,
) -> tuple[str | None, DirectKeyLike | None]:
    """The thread one row travels on and its key when the ends infer it."""

    inferred = thread_key(sender, recipient)
    if stored:
        same = inferred is not None and inferred.id == stored
        return stored, inferred if same else None
    if inferred is None:
        return None, None
    return inferred.id, inferred


def row_conversation(
    sender: str,
    recipient: str,
    stored: str | None,
    thread_key: DirectKeyFor,
) -> tuple[str | None, DirectKeyLike | None]:
    """The key one row's conversation is filed under, and its thread key."""

    thread_id, key = row_thread(sender, recipient, stored, thread_key)
    return conversation_attribution(thread_id, sender, recipient), key


@dataclass(frozen=True, slots=True)
class QueueThread:
    """One thread a participant's queued rows travel on.

    ``key`` is None for a stored thread id no row of the participant infers;
    ``peer`` is then the other end of the participant's newest row on it.
    """

    thread_id: str
    key: DirectKeyLike | None
    peer: str


def queue_threads(
    groups: Iterable[ConversationGroup],
    *,
    participant: str,
    thread_key: DirectKeyFor,
) -> dict[str, QueueThread]:
    """Every thread the groups travel on, with its key where one row infers it."""

    keys: dict[str, DirectKeyLike | None] = {}
    newest: dict[str, tuple[int, str, str]] = {}
    for group in groups:
        thread_id, key = row_thread(
            group.sender, group.recipient, group.thread_id, thread_key
        )
        if thread_id is None:
            continue
        if key is not None or thread_id not in keys:
            keys[thread_id] = key if key is not None else keys.get(thread_id)
        counterpart = group.recipient if group.sender == participant else group.sender
        last = (int(group.last_created_at_ms), str(group.last_message_id), counterpart)
        newest[thread_id] = max(newest.get(thread_id, last), last)
    return {
        thread_id: QueueThread(
            thread_id, key, peer_of(key, participant, newest[thread_id][2])
        )
        for thread_id, key in sorted(keys.items())
    }


def peer_of(key: DirectKeyLike | None, participant: str, counterpart: str) -> str:
    if key is not None and participant == key.agent:
        return key.counterpart
    if key is not None and participant == key.counterpart:
        return key.agent
    return counterpart


def conversation_heads(
    groups: Iterable[ConversationGroup],
    *,
    participant: str,
    authority: str,
    thread_key: DirectKeyFor,
) -> tuple[ConversationHead, ...]:
    """Every attributed conversation of *participant*, newest first."""

    groups = tuple(groups)
    threads = queue_threads(groups, participant=participant, thread_key=thread_key)
    heads: dict[str, ConversationHead] = {}
    for group in groups:
        thread_id, _ = row_conversation(
            group.sender, group.recipient, group.thread_id, thread_key
        )
        if thread_id is None:
            continue
        queued = threads.get(thread_id)
        key = None if queued is None else queued.key
        counterpart = group.recipient if group.sender == participant else group.sender
        cell_id = conversation_cell_id(authority, thread_id, group.conversation_id)
        head = ConversationHead(
            conversation_id=cell_id,
            thread_id=thread_id,
            kind=None if key is None else key.kind,
            peer=peer_of(key, participant, counterpart),
            legacy_conversation_id=group.conversation_id,
            last_message_at_ms=int(group.last_created_at_ms),
            last_message_id=str(group.last_message_id),
            messages=int(group.messages),
            message_class=getattr(group, "message_class", None),
        )
        known = heads.get(cell_id)
        heads[cell_id] = head if known is None else _merged_head(known, head)
    return tuple(sorted(heads.values(), key=head_order))


def _merged_head(known: ConversationHead, head: ConversationHead) -> ConversationHead:
    """Two heads of one cell: counts add up, the newest gives kind, peer, class."""

    newest = max(
        (known, head),
        key=lambda item: (item.last_message_at_ms, item.last_message_id),
    )
    return ConversationHead(
        conversation_id=head.conversation_id,
        thread_id=head.thread_id,
        kind=newest.kind,
        peer=newest.peer,
        legacy_conversation_id=head.legacy_conversation_id,
        last_message_at_ms=newest.last_message_at_ms,
        last_message_id=newest.last_message_id,
        messages=known.messages + head.messages,
        message_class=newest.message_class,
    )


def merge_heads(
    head_sets: Iterable[Iterable[ConversationHead]],
) -> tuple[ConversationHead, ...]:
    """The heads of one reader read under several keys, newest first.

    Each set is ``conversation_heads`` of one read key (the key as its
    participant, so each peer is the other end of that key's rows).  Heads
    that name one cell merge as groups of one cell do inside a set.
    """

    heads: dict[str, ConversationHead] = {}
    for head_set in head_sets:
        for head in head_set:
            known = heads.get(head.conversation_id)
            heads[head.conversation_id] = (
                head if known is None else _merged_head(known, head)
            )
    return tuple(sorted(heads.values(), key=head_order))


def merge_queue_threads(
    thread_maps: Iterable[dict[str, QueueThread]],
) -> dict[str, QueueThread]:
    """The queue threads of one reader read under several keys.

    The first map is the reader's own key; a later key adds the threads it
    alone travels on and fills in a thread key the earlier maps lack.
    """

    merged: dict[str, QueueThread] = {}
    for thread_map in thread_maps:
        for thread_id, thread in thread_map.items():
            known = merged.get(thread_id)
            if known is None or (known.key is None and thread.key is not None):
                merged[thread_id] = thread
    return {thread_id: merged[thread_id] for thread_id in sorted(merged)}


def page_heads(
    heads: Sequence[ConversationHead],
    *,
    after: tuple[int, str] | None,
    limit: int,
) -> tuple[tuple[ConversationHead, ...], bool]:
    """One page of *heads* (already ordered) after a keyset position."""

    if after is None:
        rest = list(heads)
    else:
        bound = (-after[0], after[1])
        rest = [head for head in heads if head_order(head) > bound]
    return tuple(rest[:limit]), len(rest) > limit


def encode_list_cursor(
    head: Any,
    *,
    participant: str,
    kinds: Sequence[str] | None = None,
) -> str:
    """The opaque position after *head*, bound to the caller and kinds."""

    payload: dict[str, Any] = {
        "v": LIST_CURSOR_VERSION,
        "participant": participant,
        "lastMessageAtMs": head.last_message_at_ms,
        "conversationId": head.conversation_id,
    }
    if kinds is not None:
        payload["kinds"] = sorted(kinds)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_list_cursor(
    value: object,
    *,
    participant: str,
    kinds: Sequence[str] | None = None,
) -> tuple[int, str]:
    """The position a list cursor holds; ValueError for anything else."""

    if not isinstance(value, str) or not value or len(value) > LIST_CURSOR_MAX_CHARS:
        raise ValueError("not a conversation list cursor")
    try:
        padded = value + "=" * (-len(value) % 4)
        raw = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeError, ValueError):
        raise ValueError("not a conversation list cursor") from None
    expected = _LIST_CURSOR_KEYS if kinds is None else _LIST_CURSOR_KEYS | {"kinds"}
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ValueError("not a conversation list cursor")
    if kinds is not None and payload.get("kinds") != sorted(kinds):
        raise ValueError("not a conversation list cursor")
    at_ms = payload.get("lastMessageAtMs")
    cell_id = payload.get("conversationId")
    version = payload.get("v")
    if (
        isinstance(version, bool)
        or version != LIST_CURSOR_VERSION
        or payload.get("participant") != participant
        or isinstance(at_ms, bool)
        or not isinstance(at_ms, int)
        or at_ms < 0
        or not isinstance(cell_id, str)
        or not cell_id
    ):
        raise ValueError("not a conversation list cursor")
    return at_ms, cell_id


def filtered_read(
    read: Callable[..., Sequence[Positioned]],
    *,
    keep: Callable[[Positioned], bool],
    batch: int,
) -> Callable[..., tuple[Positioned, ...]]:
    """*read* restricted to the rows *keep* accepts.

    *read* takes ``after``, ``before``, ``limit`` and ``newest_first`` like
    the delivery history read; the returned reader takes the same keywords
    and returns at most ``limit`` kept rows in the requested direction,
    re-reading in batches of *batch* past rows it drops.
    """

    if batch < 1:
        raise ValueError("batch must be positive")

    def walk(
        *,
        after: tuple[int, str] | None = None,
        before: tuple[int, str] | None = None,
        limit: int,
        newest_first: bool = False,
    ) -> tuple[Positioned, ...]:
        if newest_first and after is not None:
            raise ValueError("a newest-first walk is bounded by before only")
        if not newest_first and before is not None:
            raise ValueError("an oldest-first walk is bounded by after only")
        kept: list[Positioned] = []
        while True:
            rows = read(after=after, before=before, limit=batch, newest_first=newest_first)
            for row in rows:
                if keep(row):
                    kept.append(row)
                    if len(kept) == limit:
                        return tuple(kept)
            if len(rows) < batch:
                return tuple(kept)
            last = rows[-1]
            position = (int(last.created_at_ms), str(last.message_id))
            if newest_first:
                before = position
            else:
                after = position

    return walk


def thread_filtered(
    read: Callable[..., Sequence[Positioned]],
    *,
    thread_id: str,
    thread_key: DirectKeyFor,
    batch: int,
) -> Callable[..., tuple[Positioned, ...]]:
    """*read* restricted to the rows filed under *thread_id* (a thread or pair key)."""

    def on_thread(row: Positioned) -> bool:
        stored = getattr(row, "thread_id", None)
        travels, _ = row_conversation(row.sender, row.recipient, stored, thread_key)
        return travels == thread_id

    return filtered_read(read, keep=on_thread, batch=batch)
