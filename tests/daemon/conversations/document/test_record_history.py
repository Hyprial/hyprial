"""RM1: ``conversations.history`` reads the conversation record, merged with the queue.

The record is the read model; queue rows of the conversation that have no
record entry are merged in at their keyset position, so nothing a caller
could read before disappears: (a) rows the recorder has not reached yet,
(b) rows from before the recorder covered them, (c) rows the recorder
skips.  A message forgotten by the queue (``forget``) can only come from
the record, so every record claim here is made on such a message or on the
``recordAvailable`` flag.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from hyprial.daemon.impl.conversations import RecordEntry, SegmentStore
from hyprial.daemon.impl.conversations.record.store import SKIPPED_PREFIX
from hyprial.daemon.impl.delivery.ports import SOURCE_INBOX
from hyprial.daemon.impl.conversations.keys import direct_key
from tests.daemon.conversations.conversation_support import record_lines
from tests.daemon.conversations.document.document_support import (
    RecordDaemon,
    cell_of,
    ids,
    record_daemon,
)
from tests.support.golden.app import ALPHA, BETA, GAMMA, golden_daemon as golden_daemon

CONV = "conv-doc"


def _three_recorded(booted: RecordDaemon) -> list[str]:
    first = booted.send(ALPHA, BETA, CONV, "one")
    second = booted.reply(BETA, first, "two")
    third = booted.send(ALPHA, BETA, CONV, "three")
    booted.settle()
    return [first, second, third]


def test_a_record_only_message_is_read_from_the_document(golden_daemon, monkeypatch) -> None:
    booted = record_daemon(golden_daemon, monkeypatch)
    sent = _three_recorded(booted)
    cell = cell_of(ALPHA, BETA, CONV)
    before = booted.history(ALPHA, cell)
    booted.forget(*sent)
    for actor in (ALPHA, BETA):
        page = booted.history(actor, cell)
        assert page["recordAvailable"] is True
        assert ids(page) == sent
        assert page["legacyConversationId"] == CONV
        assert "threadId" not in page
    assert booted.history(ALPHA, cell) == before
    assert [entry["message"] for entry in before["entries"]] == ["body one", "reply two", "body three"]


def test_the_record_entry_projects_like_the_queue_row(golden_daemon, monkeypatch) -> None:
    booted = record_daemon(golden_daemon, monkeypatch)
    sent = _three_recorded(booted)
    cell = cell_of(ALPHA, BETA, CONV)
    booted.recorder.close()
    queued = booted.history(ALPHA, cell, mode="tail")
    booted.fs.down = True
    assert booted.history(ALPHA, cell, mode="tail")["recordAvailable"] is False
    booted.fs.down = False
    booted.forget(*sent)
    recorded = booted.history(ALPHA, cell, mode="tail")
    assert recorded["recordAvailable"] is True
    assert recorded["entries"] == queued["entries"]


def test_a_message_in_both_record_and_queue_appears_once(golden_daemon, monkeypatch) -> None:
    booted = record_daemon(golden_daemon, monkeypatch)
    sent = _three_recorded(booted)
    cell = cell_of(ALPHA, BETA, CONV)
    lines = booted.fs.inner.listdir(booted.space(), f"cells/conversation/{cell}", recursive=True)
    assert any(node.kind == "doc" for node in lines)
    for actor in (ALPHA, BETA):
        page = booted.history(actor, cell, limit=50)
        assert page["recordAvailable"] is True
        assert ids(page) == sent


def test_unrecorded_rows_of_each_kind_are_merged_from_the_queue(golden_daemon, monkeypatch) -> None:
    """(b) a row marked before the recorder covered it, (c) a refused row, (a) a lagging row.

    Since slice S2 the refused row is a bodiless placeholder of the record,
    shown with the body the queue still holds.
    """

    def redact(draft: Any) -> Any:
        return None if b"secret" in draft.body else draft

    booted = record_daemon(golden_daemon, monkeypatch, redact=redact)
    old = booted.send(ALPHA, BETA, CONV, "old")
    pages = {
        source: booted.app._inbox.interaction_rows_after(source, 0, limit=256)
        for source in ("inbox", "sender_copies")
    }
    for source, page in pages.items():
        for row in page.rows:
            booted.recorder._store.mark(
                source, row.position, row.payload_sha256,
                f"{SKIPPED_PREFIX}unthreaded", now_ms=0,
            )
    recorded = booted.send(ALPHA, BETA, CONV, "recorded")
    skipped = booted.app.handle(
        "message.send",
        {"from": ALPHA, "sessionRef": "session-alpha", "to": [BETA], "summary": "s",
         "message": "a secret", "conversationId": CONV, "idempotencyKey": "key-secret"},
    )["messageId"]
    booted.daemon.clock.advance(5)
    booted.settle()
    lagging = booted.send(ALPHA, BETA, CONV, "lagging")
    booted.forget(recorded)
    cell = cell_of(ALPHA, BETA, CONV)
    in_record = {line["messageId"] for line in record_lines(booted.fs.inner, booted.space(), cell)}
    assert in_record == {recorded, str(skipped)}
    page = booted.history(BETA, cell)
    assert page["recordAvailable"] is True
    assert ids(page) == [old, recorded, str(skipped), lagging]
    assert [entry["messageClass"] for entry in page["entries"]] == ["interaction"] * 4


def _write_foreign_segment(booted: RecordDaemon, cell: str, entries: list[RecordEntry]) -> None:
    store = SegmentStore(
        fs=lambda: booted.fs, space_for=lambda _authority: booted.space(), writer="node-2"
    )
    store.append("user:op", cell, entries, 0)


def test_two_writer_segments_interleave_by_time(golden_daemon, monkeypatch) -> None:
    booted = record_daemon(golden_daemon, monkeypatch)
    sent = _three_recorded(booted)
    cell = cell_of(ALPHA, BETA, CONV)
    page = booted.history(ALPHA, cell)
    template = booted.app._inbox.interaction_rows_after(SOURCE_INBOX, 0, limit=1).rows[0]
    first = page["entries"][0]
    foreign = []
    for index, entry in enumerate(page["entries"][:2]):
        created = int(entry["createdAtMs"]) + 1
        foreign.append(
            RecordEntry(
                key=f"inbox:{9000 + index}:{template.payload_sha256}",
                source=SOURCE_INBOX, position=9000 + index,
                message_id=f"remote-{index}", conversation_id=CONV,
                thread_id=direct_key(ALPHA, BETA).id, cell_id=cell,
                sender=BETA, recipient=ALPHA, intent="request",
                created_at_ms=created, recorded_at_ms=created,
                payload_sha256=template.payload_sha256,
                body=b'{"message": "from another device"}',
            )
        )
    duplicate = dataclasses.replace(
        foreign[0], key=f"inbox:9100:{template.payload_sha256}", message_id=first["messageId"],
        created_at_ms=int(first["createdAtMs"]), body=b'{"message": "other copy"}',
    )
    _write_foreign_segment(booted, cell, [*foreign, duplicate])
    booted.forget(*sent)
    merged = booted.history(ALPHA, cell)
    assert ids(merged) == [sent[0], "remote-0", sent[1], "remote-1", sent[2]]
    assert merged["entries"][1]["message"] == "from another device"
    assert len(ids(merged)) == len(set(ids(merged)))


def _walk_forward(booted: RecordDaemon, cell: str, limit: int) -> list[str]:
    seen: list[str] = []
    page = booted.history(BETA, cell, limit=limit)
    seen.extend(ids(page))
    while page["nextCursor"] is not None:
        page = booted.history(BETA, cell, limit=limit, cursor=page["nextCursor"])
        seen.extend(ids(page))
    return seen


def _walk_backward(booted: RecordDaemon, cell: str, limit: int) -> list[str]:
    page = booted.history(BETA, cell, limit=limit, mode="tail")
    seen = ids(page)
    while page["nextOlderCursor"] is not None:
        page = booted.history(BETA, cell, limit=limit, mode="older", cursor=page["nextOlderCursor"])
        seen[:0] = ids(page)
    return seen


def _walk_after(booted: RecordDaemon, cell: str, limit: int, first: str) -> list[str]:
    oldest = booted.history(BETA, cell, limit=200, mode="tail")
    assert ids(oldest)[0] == first
    page = booted.history(BETA, cell, limit=1, mode="tail")
    while page["nextOlderCursor"] is not None:
        page = booted.history(BETA, cell, limit=1, mode="older", cursor=page["nextOlderCursor"])
    assert ids(page) == [first]
    cursor = booted.history(BETA, cell, limit=1)["nextCursor"]
    seen = [first]
    while cursor is not None:
        page = booted.history(BETA, cell, limit=limit, mode="after", cursor=cursor)
        seen.extend(ids(page))
        cursor = page["nextCursor"]
    return seen


def test_paging_across_the_record_and_queue_boundary_has_no_gap_or_duplicate(
    golden_daemon, monkeypatch
) -> None:
    booted = record_daemon(golden_daemon, monkeypatch)
    record_only = [booted.send(ALPHA, BETA, CONV, f"r{index}") for index in range(3)]
    booted.settle()
    both = [booted.send(ALPHA, BETA, CONV, f"b{index}") for index in range(2)]
    booted.settle()
    queue_only = [booted.send(ALPHA, BETA, CONV, f"q{index}") for index in range(3)]
    booted.forget(*record_only)
    expected = [*record_only, *both, *queue_only]
    cell = cell_of(ALPHA, BETA, CONV)
    for limit in (1, 2, 3, 50):
        assert _walk_forward(booted, cell, limit) == expected, limit
        assert _walk_backward(booted, cell, limit) == expected, limit
        assert _walk_after(booted, cell, limit, expected[0]) == expected, limit
    head = booted.history(BETA, cell, limit=2, mode="tail")
    late = booted.send(ALPHA, BETA, CONV, "late")
    newer = booted.history(BETA, cell, limit=5, mode="after", cursor=head["headCursor"])
    assert ids(newer) == [late]


def test_message_class_is_on_every_entry_in_every_mode(golden_daemon, monkeypatch) -> None:
    booted = record_daemon(golden_daemon, monkeypatch)
    sent = _three_recorded(booted)
    booted.forget(sent[0])
    cell = cell_of(ALPHA, BETA, CONV)
    tail = booted.history(ALPHA, cell, limit=1, mode="tail")
    pages = [
        booted.history(ALPHA, cell),
        booted.history(ALPHA, cell, limit=1),
        tail,
        booted.history(ALPHA, cell, limit=1, mode="older", cursor=tail["nextOlderCursor"]),
        booted.history(ALPHA, cell, mode="after", cursor=booted.history(ALPHA, cell, limit=1, mode="older", cursor=tail["nextOlderCursor"])["headCursor"]),
    ]
    for page in pages:
        assert page["entries"] or page.get("mode") == "after"
        assert all(entry["messageClass"] == "interaction" for entry in page["entries"])


def test_an_unreadable_record_falls_back_to_the_queue(golden_daemon, monkeypatch) -> None:
    booted = record_daemon(golden_daemon, monkeypatch)
    sent = _three_recorded(booted)
    cell = cell_of(ALPHA, BETA, CONV)
    booted.fs.down = True
    page = booted.history(ALPHA, cell)
    assert page["recordAvailable"] is False
    assert ids(page) == sent
    assert page["legacyConversationId"] == CONV
    listed = booted.listed(ALPHA)
    assert listed["recordAvailable"] is False
    assert cell in {head["conversationId"] for head in listed["conversations"]}
    booted.fs.down = False
    assert booted.history(ALPHA, cell)["recordAvailable"] is True


def test_another_participant_cannot_read_the_record(golden_daemon, monkeypatch) -> None:
    booted = record_daemon(golden_daemon, monkeypatch)
    sent = _three_recorded(booted)
    booted.forget(*sent)
    cell = cell_of(ALPHA, BETA, CONV)
    assert ids(booted.history(ALPHA, cell)) == sent
    foreign = booted.history(GAMMA, cell)
    assert foreign["recordAvailable"] is True
    assert foreign["entries"] == [] and "threadId" not in foreign
    assert foreign["legacyConversationId"] is None
    assert cell not in {head["conversationId"] for head in booted.listed(GAMMA)["conversations"]}
