"""RM1: the pure record/queue merge (``conversations.merge``).

``merged_read`` must hand back exactly the first ``limit`` messages of the
union past any keyset bound, in either direction, with the record winning
for an id both sides hold, whatever the overlap.  ``visible_messages`` folds
the inbox entry and the sender copy into one message and keeps only what
the reader is an end of.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

import pytest

from hyprial.daemon.impl.conversations import (
    RecordEntry,
    merged_read,
    visible_messages,
)

SHA = "0" * 64


@dataclass(frozen=True)
class Row:
    message_id: str
    created_at_ms: int
    sender: str = "a"
    recipient: str = "b"


def _entry(message_id: str, at: int, *, source: str = "inbox", body: bytes = b"{}",
           sender: str = "a", recipient: str = "b") -> RecordEntry:
    return RecordEntry(
        key=f"{source}:{at}:{message_id}", source=source, position=at,
        message_id=message_id, conversation_id="conv", thread_id="t.x",
        cell_id="c.x", sender=sender, recipient=recipient, intent="request",
        created_at_ms=at, recorded_at_ms=at, payload_sha256=SHA, body=body,
    )


def _queue(rows: list[Row]):
    ordered = sorted(rows, key=lambda row: (row.created_at_ms, row.message_id))

    def read(*, after=None, before=None, limit, newest_first=False):
        keyed = [(row.created_at_ms, row.message_id) for row in ordered]
        if newest_first:
            picked = [row for row, key in zip(ordered, keyed) if before is None or key < before]
            return tuple(reversed(picked))[:limit]
        picked = [row for row, key in zip(ordered, keyed) if after is None or key > after]
        return tuple(picked[:limit])

    return read


@pytest.mark.parametrize("seed", range(40))
def test_every_page_is_the_head_of_the_union(seed: int) -> None:
    rng = random.Random(seed)
    ids = [f"m{index:03d}" for index in range(rng.randint(0, 30))]
    times = {message_id: rng.randint(1, 12) for message_id in ids}
    in_record = {message_id for message_id in ids if rng.random() < 0.6}
    in_queue = {message_id for message_id in ids if message_id not in in_record or rng.random() < 0.5}
    record = visible_messages([_entry(m, times[m]) for m in in_record], {"a"})
    queue = _queue([Row(m, times[m]) for m in in_queue])
    read = merged_read(record, queue, batch=rng.randint(1, 4))
    union = sorted({(times[m], m) for m in in_record | in_queue})
    for limit in (1, 2, 5, 50):
        for index in range(-1, len(union)):
            bound = None if index < 0 else union[index]
            forward = read(after=bound, limit=limit)
            assert [(row.created_at_ms, row.message_id) for row in forward] == [
                key for key in union if bound is None or key > bound
            ][:limit]
            backward = read(before=bound, limit=limit, newest_first=True)
            assert [(row.created_at_ms, row.message_id) for row in backward] == [
                key for key in reversed(union) if bound is None or key < bound
            ][:limit]
    for row in read(limit=200):
        assert (row.message_id in in_record) == (type(row) is not Row)


def test_the_inbox_entry_and_the_sender_copy_are_one_message() -> None:
    copy = _entry("m1", 5, source="sender_copies", body=b'{"message": "copy"}')
    inbox = _entry("m1", 5, source="inbox", body=b'{"message": "inbox"}')
    for entries in ([copy, inbox], [inbox, copy]):
        [message] = visible_messages(entries, {"a"})
        assert message.payload == b'{"message": "inbox"}'


def test_only_the_readers_messages_are_visible() -> None:
    entries = [
        _entry("m1", 1),
        _entry("m2", 2, sender="b", recipient="a"),
        _entry("m3", 3, sender="c", recipient="b"),
    ]
    assert [m.message_id for m in visible_messages(entries, {"a"})] == ["m1", "m2"]
    assert [m.message_id for m in visible_messages(entries, {"c"})] == ["m3"]
    assert visible_messages(entries, {"d"}) == ()
