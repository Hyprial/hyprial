"""A registered (id-form) sender is keyed by its ``a.`` id, not its address.

Plan slice C1, coordinator adjustment: the default conversation must not
move when the owner or machine segment of an agent's address changes.  The
application here registers its agent the way production does, so the stored
sender is ``agent:<owner>:<machine>:a.<id>``.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from hyprial.daemon.impl.conversations.ids import default_conversation_id
from hyprial.kernel import parse_agent_uri
from tests.daemon.application.messaging.test_open_requests_from import (
    A_NAME,
    Y_URI,
    _app,
    _inbox,
    _official,
)
from tests.support.runtime._owned_applications import (
    owned_applications as owned_applications,
)


def test_the_default_conversation_of_a_registered_sender_uses_its_agent_id(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path, _inbox(tmp_path))
    official = _official(app)
    parsed = parse_agent_uri(official)
    assert parsed is not None
    agent_id = parsed[2]
    assert agent_id == app.agents.require(A_NAME).agent_id

    answer = app.handle(
        "message.send",
        {
            "actor": A_NAME,
            "to": [Y_URI],
            "message": "hello",
            "summary": "hello",
            "idempotencyKey": uuid4().hex,
        },
    )

    expected = default_conversation_id(agent_id, Y_URI)
    assert answer["conversationId"] == expected
    assert answer["conversationId"] != default_conversation_id(official, Y_URI)
    with closing(sqlite3.connect(tmp_path / "inbox.sqlite3")) as db:
        rows = db.execute(
            "SELECT sender, conversation_id FROM sender_copies WHERE message_id = ?",
            (answer["messageId"],),
        ).fetchall()
    assert rows == [(official, expected)]
