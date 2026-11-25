"""S2: a caller cannot make its own message SYSTEM through ``message.send``.

``message.send`` accepts a caller-chosen ``from`` of the daemon's notice shape
``system:hyprial:<node>``.  The stored row must still classify INTERACTION,
because only daemon producers stamp ``intent == "system"``.  The daemon's own
notices keep classifying SYSTEM.
"""

from __future__ import annotations

from tests.daemon.delivery.message import test_message_history as history
from tests.daemon.delivery.message.test_message_history import (
    CONVERSATION,
    NODE,
    _HistoryHarness,
)

from hyprial.daemon.impl.inbox.envelope import message_class_of
from hyprial.kernel import MessageClass

history_harness = history.harness


def test_a_caller_chosen_system_sender_stays_interaction(
    history_harness: _HistoryHarness,
) -> None:
    harness = history_harness
    forged = f"system:hyprial:{NODE}"
    harness.app.handle(
        "message.send",
        {
            "from": forged,
            "to": [harness.reader],
            "message": "not a daemon notice",
            "summary": "not a daemon notice",
            "conversationId": CONVERSATION,
        },
    )
    rows = [
        row for row in harness.inbox.pending_messages(harness.reader) if row.sender == forged
    ]
    assert rows
    assert {row.intent for row in rows} == {"request"}
    assert {message_class_of(row) for row in rows} == {MessageClass.INTERACTION}
