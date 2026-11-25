"""RM1: ``conversations.list`` lists the record's conversations merged with the queue's.

A conversation only the record holds (its queue rows gone) and one only the
queue holds (the recorder has not reached it) are both listed, once each;
a conversation in both is one head whose count is not doubled.  The list
enumerates cells from the recorder's index and a timed sweep, so a warm
list makes no OrgFS listing.
"""

from __future__ import annotations

from hyprial.daemon.impl.conversations import ConversationDocuments
from tests.daemon.conversations.conversation_support import OWNER_PRINCIPAL
from tests.daemon.conversations.document.document_support import (
    cell_of,
    record_daemon,
)
from tests.support.golden.app import ALPHA, BETA, GAMMA, golden_daemon as golden_daemon


def _heads(answer: dict) -> dict[str, dict]:
    rows = answer["conversations"]
    assert len({row["conversationId"] for row in rows}) == len(rows)
    return {row["conversationId"]: row for row in rows}


def test_record_backed_and_queue_only_conversations_are_listed_once_each(
    golden_daemon, monkeypatch
) -> None:
    booted = record_daemon(golden_daemon, monkeypatch)
    recorded = booted.send(ALPHA, BETA, "conv-r", "r")
    both = booted.send(ALPHA, GAMMA, "conv-b", "b")
    booted.settle()
    queued = booted.send(ALPHA, BETA, "conv-q", "q")
    booted.forget(recorded)
    cells = {
        "r": cell_of(ALPHA, BETA, "conv-r"),
        "b": cell_of(ALPHA, GAMMA, "conv-b"),
        "q": cell_of(ALPHA, BETA, "conv-q"),
    }
    answer = booted.listed(ALPHA)
    assert answer["recordAvailable"] is True
    heads = _heads(answer)
    assert list(heads) == [cells["q"], cells["b"], cells["r"]]
    only_record = heads[cells["r"]]
    assert only_record["legacyConversationId"] == "conv-r"
    assert only_record["lastMessageId"] == recorded
    assert (only_record["kind"], only_record["peer"], only_record["messages"]) == ("agent", BETA, 1)
    assert only_record["messageClass"] == "interaction"
    assert heads[cells["b"]]["lastMessageId"] == both
    assert heads[cells["q"]]["lastMessageId"] == queued
    beta = _heads(booted.listed(BETA))
    assert set(beta) == {cells["r"], cells["q"]}
    assert beta[cells["r"]]["peer"] == ALPHA
    assert set(_heads(booted.listed(GAMMA))) == {cells["b"]}


def test_a_conversation_in_record_and_queue_counts_each_message_once(
    golden_daemon, monkeypatch
) -> None:
    booted = record_daemon(golden_daemon, monkeypatch)
    for index in range(5):
        booted.send(ALPHA, BETA, "conv-2", f"two-{index}")
    booted.settle()
    cell = cell_of(ALPHA, BETA, "conv-2")
    for actor in (ALPHA, BETA):
        answer = booted.listed(actor)
        assert answer["recordAvailable"] is True
        assert _heads(answer)[cell]["messages"] == 5
    booted.send(ALPHA, BETA, "conv-2", "lagging")
    assert _heads(booted.listed(ALPHA))[cell]["messages"] == 6


def test_list_pages_over_record_heads(golden_daemon, monkeypatch) -> None:
    booted = record_daemon(golden_daemon, monkeypatch)
    sent = [booted.send(ALPHA, BETA, f"conv-{index}", f"p{index}") for index in range(3)]
    booted.settle()
    booted.forget(*sent)
    first = booted.listed(ALPHA, limit=2)
    second = booted.listed(ALPHA, limit=2, cursor=first["nextCursor"])
    walked = [row["lastMessageId"] for row in first["conversations"] + second["conversations"]]
    assert walked == list(reversed(sent))
    assert second["nextCursor"] is None


def test_a_warm_list_makes_no_orgfs_listing(golden_daemon, monkeypatch) -> None:
    booted = record_daemon(golden_daemon, monkeypatch)
    booted.send(ALPHA, BETA, "conv-c", "c")
    booted.settle()
    booted.listed(ALPHA)
    documents = booted.app._conversation_documents
    assert isinstance(documents, ConversationDocuments)
    listings, reads = documents.listings, documents.reads
    booted.listed(ALPHA)
    booted.listed(BETA)
    assert (documents.listings, documents.reads) == (listings, reads)
    booted.send(ALPHA, BETA, "conv-c", "d")
    booted.settle()
    heads = _heads(booted.listed(ALPHA))
    homes = booted.app._conversation_placement.read_spaces(booted.fs, OWNER_PRINCIPAL)
    assert documents.listings == listings + len(homes)
    assert documents.reads == reads + 1
    assert heads[cell_of(ALPHA, BETA, "conv-c")]["messages"] == 2


def test_the_sweep_finds_cells_another_writer_added(golden_daemon, monkeypatch) -> None:
    booted = record_daemon(golden_daemon, monkeypatch)
    booted.send(ALPHA, BETA, "conv-s", "s")
    booted.settle()
    now = [0.0]
    placement = booted.app._conversation_placement
    homes = len(placement.read_spaces(booted.fs, OWNER_PRINCIPAL))
    documents = ConversationDocuments(
        fs=lambda: booted.fs,
        spaces=lambda fs: placement.read_spaces(fs, OWNER_PRINCIPAL),
        index=None,
        clock=lambda: now[0],
        sweep_s=30.0,
    )
    cell = cell_of(ALPHA, BETA, "conv-s")
    assert set(documents.known_cells()) == {cell}
    assert documents.listings == homes
    other = booted.send(ALPHA, GAMMA, "conv-t", "t")
    booted.settle()
    assert set(documents.known_cells()) == {cell}
    now[0] = 31.0
    found = documents.known_cells()
    assert set(found) == {cell, cell_of(ALPHA, GAMMA, "conv-t")}
    assert {entry.message_id for entry in found[cell_of(ALPHA, GAMMA, "conv-t")]} == {other}
    assert all(entry.body == b"" for entries in found.values() for entry in entries)
    assert documents.listings == 2 * homes
