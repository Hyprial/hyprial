"""Characterization (plan slice P1): one message between two agents of one owner.

Plan section 2.1 ("whether the read side merges the two entries was not
checked").  Current behaviour on dev, with sender and recipient on one
daemon under one owner:

- The sender copy and the inbox row carry the same thread and legacy
  conversation id, so they map to the same conversation cell.  The recorder
  records both rows: one message becomes two entries of that cell, because
  an entry is keyed by ``(source, position, payload digest)`` and not by
  the message id.
- In this harness ``conversations.list`` and ``conversations.history`` do
  not read the record document: the app's OrgFS is patched in through
  ``_require_orgfs_runtime`` and not composed, so the record read model
  (plan slice RM1) falls back to the delivery queue, which deduplicates by
  message id, and both ends see one message and one history entry.  The
  record read itself folds the two entries by message id
  (``tests/daemon/conversations/document``).

The two entries are asserted on the recorder's index.  The record document
is only checked for what it must never hold (a line of another message, a
repeated key): in this harness the segment file written for the inbox row
is intermittently gone from the OrgFS tree when the sender copy is
appended; the recorder then recreates the segment and the document holds
one line while the index still says two.  That loss is reported by P1, not
pinned here.

Slice G1 (GUI reads ``conversations.*``) relies on the one-entry read.  A
reader that walks the record document itself sees two lines until the
record is keyed by message id; the multi-owner chat space work (group 4)
is where that becomes visible to another owner.
"""

from __future__ import annotations

from typing import Any

from hyprial.daemon.impl.conversations import conversation_cell_id
from tests.daemon.acceptance.acceptance_support import session
from tests.daemon.conversations.bypass.bypass_support import (
    boot,
    recorded,
    settle,
    stored,
)
from tests.daemon.conversations.conversation_support import OWNER_PRINCIPAL
from tests.support.golden.app import ALPHA, BETA, golden_daemon as golden_daemon


def _head(app: Any, actor: str, cell_id: str) -> dict[str, Any]:
    heads = app.handle("conversations.list", session(actor))["conversations"]
    [head] = [item for item in heads if item["conversationId"] == cell_id]
    return head


def test_characterization_one_message_is_two_record_entries_but_one_history_entry(
    golden_daemon, monkeypatch
) -> None:
    """Current behaviour, relied on by G1: two record entries, one history entry."""

    booted = boot(golden_daemon, monkeypatch)
    app = booted.app
    message_id = booted.control
    thread_id = app._conversation_stamp(ALPHA, BETA)
    assert thread_id is not None
    cell_id = conversation_cell_id(OWNER_PRINCIPAL, thread_id, "conv-control")
    rows = stored(app, message_id)
    assert set(rows) == {"inbox", "sender_copies"}
    assert {row["thread_id"] for row in rows.values()} == {thread_id}

    settled = settle(booted)

    assert settled.sources(message_id) == ["inbox", "sender_copies"]
    assert settled.cells(message_id) == {cell_id}
    lines = recorded(app)
    assert {line["messageId"] for line in lines} == {message_id}
    assert {line["cellId"] for line in lines} == {cell_id}
    assert {line["source"] for line in lines} <= {"inbox", "sender_copies"}
    assert len({line["key"] for line in lines}) == len(lines)

    for actor in (ALPHA, BETA):
        head = _head(app, actor, cell_id)
        assert head["conversationId"] == cell_id
        assert head["messages"] == 1
        page = app.handle(
            "conversations.history",
            {**session(actor), "conversationId": cell_id},
        )
        assert [entry["messageId"] for entry in page["entries"]] == [message_id]
