"""The record read model merged with the delivery queue (plan slice RM1, R13, R15).

Decision notes (plan §11.1; brief RM1):

- The conversation record is the read model: a conversation's messages are
  the union of every ``rec-*`` segment of its cell.  The inbox row and the
  sender copy of one message are two entries with one message id; they
  fold into one message.  The inbox entry wins (it is the two-ended
  delivery fact, the queue's ``source_rank`` 0), then the lowest entry
  key, so the winner does not depend on segment order.
- A reader sees only the entries it is an end of (sender or recipient is
  one of its read keys), exactly what the queue read selects with
  ``sender = ? OR recipient = ?``.  The owner-home space holds every
  agent's cells, so this filter is the access rule, not a convenience.
- The answer is "record messages, plus queue rows of the same
  conversation whose message id has no record entry".  ``merged_read``
  gives both sources the same keyset bound and direction, takes up to
  ``limit`` from each and cuts the sorted union at ``limit``.  The queue
  side skips every id the record holds (not only ids inside the bound),
  re-reading in batches like ``thread_filtered``, so a message is never
  shown twice and never at two positions.
- A record message keeps the recorded body, except a redacted one: until
  the owner rules otherwise, a reader keeps seeing what it sees today, so
  when the entry is redacted and the queue still holds that message with
  the original payload (its digest equals the entry's ``payloadSha256``),
  the queue's body is shown at the record's position with the record's
  other fields.  Only when the queue no longer holds it does the redacted
  body show.  The recorder never grants a redacted row, so the queue keeps
  its body until the row itself is pruned.  No published entry field says
  an entry is redacted, so none is added.
- ``record_heads`` gives one head per cell from the visible messages: the
  newest message names the peer and class; ``kind`` is the thread kind
  when the newest message's ends infer the entry's thread, else null (a
  pair key or a foreign stamp).  ``merge_head_sets`` folds them with the
  queue heads by ``c.`` id: the queue head keeps its kind and peer, the
  newest message of either side gives the last position and class, and
  ``messages`` is the larger of the two counts.  The queue counts
  messages without ids, so the union cannot be counted exactly; the
  larger count is exact whenever one side holds the other (recorder
  caught up, or rows pruned from the queue) and otherwise undercounts by
  the messages only the smaller side holds.
- The module is pure: entries and queue rows are duck-typed, the thread
  key arrives as a callable.

Decision notes (plan slice S2, owner ruling 2026-11-17):

- A placeholder entry (``bodyOmitted``: the recorder could not record the
  body) is a message of the document like any other: it takes its
  position, counts on the head, and replaces the queue fallback for its
  message id.  It is redacted, so the redacted rule gives it the queue's
  original body while the queue holds it, and only then is
  ``body_omitted`` cleared.  Otherwise it keeps its empty body and
  ``body_omitted`` names the reason, which the history publishes as
  ``bodyOmitted``.
- One message id with both a body-bearing entry and a placeholder (one
  side recorded, the other refused) shows the body: a placeholder ranks
  after every entry with a body, then the inbox rule applies.
"""

from __future__ import annotations

import dataclasses
import hashlib
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass

from hyprial.daemon.impl.conversations.read import (
    ConversationHead,
    Positioned,
    DirectKeyFor,
    filtered_read,
    head_order,
    peer_of,
)
from hyprial.daemon.impl.conversations.record.contracts import RecordEntry
from hyprial.daemon.impl.delivery.ports import SOURCE_INBOX
from hyprial.kernel import MessageClass

HistoryRead = Callable[..., Sequence[Positioned]]
_LAST_ID = chr(0x10FFFF)


@dataclass(frozen=True, slots=True)
class RecordMessage:
    """One message of a conversation record, shaped like a queue row."""

    message_id: str
    conversation_id: str
    sender: str
    recipient: str
    intent: str
    created_at_ms: int
    payload: bytes
    message_class: MessageClass
    thread_id: str
    cell_id: str
    redacted: bool = False
    payload_sha256: str = ""
    body_omitted: str | None = None

    @property
    def position(self) -> tuple[int, str]:
        return self.created_at_ms, self.message_id

    @classmethod
    def of(cls, entry: RecordEntry) -> RecordMessage:
        try:
            message_class = MessageClass(entry.message_class)
        except ValueError:
            message_class = MessageClass.INTERACTION
        return cls(
            message_id=entry.message_id,
            conversation_id=entry.conversation_id,
            sender=entry.sender,
            recipient=entry.recipient,
            intent=entry.intent,
            created_at_ms=int(entry.created_at_ms),
            payload=bytes(entry.body),
            message_class=message_class,
            thread_id=entry.thread_id,
            cell_id=entry.cell_id,
            redacted=entry.redacted,
            payload_sha256=entry.payload_sha256,
            body_omitted=entry.body_omitted,
        )


def _rank(entry: RecordEntry) -> tuple[bool, int, str]:
    return entry.body_omitted is not None, (0 if entry.source == SOURCE_INBOX else 1), entry.key


def visible_messages(
    entries: Iterable[RecordEntry], keys: Collection[str]
) -> tuple[RecordMessage, ...]:
    """The messages *keys* take part in, one per message id, oldest first."""

    chosen: dict[str, RecordEntry] = {}
    for entry in entries:
        if entry.sender not in keys and entry.recipient not in keys:
            continue
        known = chosen.get(entry.message_id)
        if known is None or _rank(entry) < _rank(known):
            chosen[entry.message_id] = entry
    return tuple(
        sorted((RecordMessage.of(entry) for entry in chosen.values()),
               key=lambda message: message.position)
    )


def _position(row: Positioned) -> tuple[int, str]:
    return int(row.created_at_ms), str(row.message_id)


def _queued_original(
    queue: HistoryRead, message: RecordMessage, *, batch: int
) -> RecordMessage:
    """*message* with the queue's original body when the queue still holds it."""

    after: tuple[int, str] = (message.created_at_ms - 1, _LAST_ID)
    while True:
        rows = queue(after=after, limit=batch)
        for row in rows:
            position = _position(row)
            if position > message.position:
                return message
            if str(row.message_id) != message.message_id:
                continue
            payload = bytes(getattr(row, "payload", b""))
            if hashlib.sha256(payload).hexdigest() == message.payload_sha256:
                return dataclasses.replace(message, payload=payload, body_omitted=None)
            return message
        if len(rows) < batch:
            return message
        after = _position(rows[-1])


def merged_read(
    record: Sequence[RecordMessage],
    queue: HistoryRead,
    *,
    batch: int,
) -> Callable[..., tuple[Positioned, ...]]:
    """A history read over *record* (oldest first) plus the queue rows it lacks."""

    held = frozenset(message.message_id for message in record)
    positions = [message.position for message in record]
    queue_only = filtered_read(
        queue, keep=lambda row: str(row.message_id) not in held, batch=batch
    )

    def read(
        *,
        after: tuple[int, str] | None = None,
        before: tuple[int, str] | None = None,
        limit: int,
        newest_first: bool = False,
    ) -> tuple[Positioned, ...]:
        rows = queue_only(after=after, before=before, limit=limit, newest_first=newest_first)
        if newest_first:
            end = len(record) if before is None else bisect_left(positions, before)
            own = list(reversed(record[max(0, end - limit):end]))
        else:
            start = 0 if after is None else bisect_right(positions, after)
            own = list(record[start:start + limit])
        merged = sorted([*own, *rows], key=_position, reverse=newest_first)[:limit]
        return tuple(
            _queued_original(queue, row, batch=batch)
            if isinstance(row, RecordMessage) and row.redacted
            else row
            for row in merged
        )

    return read


def record_heads(
    messages_by_cell: Mapping[str, Sequence[RecordMessage]],
    *,
    keys: Collection[str],
    thread_key: DirectKeyFor,
) -> tuple[ConversationHead, ...]:
    """One head per cell with visible messages, newest first."""

    heads: list[ConversationHead] = []
    for cell_id, messages in messages_by_cell.items():
        if not messages:
            continue
        newest = messages[-1]
        participant = newest.sender if newest.sender in keys else newest.recipient
        counterpart = newest.recipient if newest.sender == participant else newest.sender
        inferred = thread_key(newest.sender, newest.recipient)
        key = inferred if inferred is not None and inferred.id == newest.thread_id else None
        heads.append(
            ConversationHead(
                conversation_id=cell_id,
                thread_id=newest.thread_id,
                kind=None if key is None else key.kind,
                peer=peer_of(key, participant, counterpart),
                legacy_conversation_id=newest.conversation_id,
                last_message_at_ms=newest.created_at_ms,
                last_message_id=newest.message_id,
                messages=len(messages),
                message_class=newest.message_class.value,
            )
        )
    return tuple(sorted(heads, key=head_order))


def _union_head(queued: ConversationHead, recorded: ConversationHead) -> ConversationHead:
    newest = max(
        (queued, recorded),
        key=lambda head: (head.last_message_at_ms, head.last_message_id),
    )
    return ConversationHead(
        conversation_id=queued.conversation_id,
        thread_id=queued.thread_id,
        kind=queued.kind,
        peer=queued.peer,
        legacy_conversation_id=queued.legacy_conversation_id,
        last_message_at_ms=newest.last_message_at_ms,
        last_message_id=newest.last_message_id,
        messages=max(queued.messages, recorded.messages),
        message_class=newest.message_class,
    )


def merge_head_sets(
    queued: Iterable[ConversationHead], recorded: Iterable[ConversationHead]
) -> tuple[ConversationHead, ...]:
    """Queue heads and record heads, one per ``c.`` id, newest first."""

    heads = {head.conversation_id: head for head in queued}
    for head in recorded:
        known = heads.get(head.conversation_id)
        heads[head.conversation_id] = head if known is None else _union_head(known, head)
    return tuple(sorted(heads.values(), key=head_order))


__all__ = [
    "RecordMessage",
    "merge_head_sets",
    "merged_read",
    "record_heads",
    "visible_messages",
]
