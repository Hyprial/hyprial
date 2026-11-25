"""RM1 acceptance: the owner and an agent read one conversation from one document.

The owner (``user:<owner>``) sends to an agent and the agent answers the
owner in the same conversation.  Both ends list the same ``c.`` document
and read the same ordered history from the record after the queue has
forgotten every row, so neither answer can come from the queue.

On this base a plain ``message.send`` from an agent to ``user:<owner>``
still bypasses the queue and the record (slice U1, characterized in
``bypass/test_external_targets.py``); the agent's side is therefore its
``message.reply``, which the recorder records from the sender copy.
"""

from __future__ import annotations

from tests.daemon.conversations.document.document_support import (
    cell_of,
    ids,
    record_daemon,
)
from tests.support.golden.app import BETA, OWNER_USER, golden_daemon as golden_daemon


def test_owner_and_agent_read_the_same_ordered_history_from_the_document(
    golden_daemon, monkeypatch
) -> None:
    booted = record_daemon(golden_daemon, monkeypatch)
    question = booted.send(OWNER_USER, BETA, "conv-owner", "question")
    answer = booted.reply(BETA, question, "answer")
    follow_up = booted.send(OWNER_USER, BETA, "conv-owner", "follow-up")
    booted.settle()
    booted.forget(question, answer, follow_up)
    cell = cell_of(OWNER_USER, BETA, "conv-owner")
    pages = {}
    for reader in (OWNER_USER, BETA):
        listed = booted.listed(reader)
        assert listed["recordAvailable"] is True
        [head] = [row for row in listed["conversations"] if row["conversationId"] == cell]
        assert head["legacyConversationId"] == "conv-owner"
        assert head["messages"] == 3
        page = booted.history(reader, cell)
        assert page["recordAvailable"] is True
        pages[reader] = page
    assert ids(pages[OWNER_USER]) == ids(pages[BETA]) == [question, answer, follow_up]
    assert pages[OWNER_USER]["entries"] == pages[BETA]["entries"]
    assert [entry["from"] for entry in pages[BETA]["entries"]] == [OWNER_USER, BETA, OWNER_USER]
    assert [entry["message"] for entry in pages[BETA]["entries"]] == [
        "body question", "reply answer", "body follow-up",
    ]
