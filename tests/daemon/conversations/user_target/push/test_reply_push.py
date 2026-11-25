"""UA2b item 8: ``message.reply`` to the owner is recorded but never pushed.

A recorded decision, not an accident: before UA2 a reply to ``user:<owner>``
never reached Lark either (its row sat in the outbox), and this slice keeps
that.  ``message.send`` with ``replyTo`` to the owner is a send, so it is
pushed like any other send to the owner: through the user agent cell (I7a).
"""

from __future__ import annotations

from tests.daemon.acceptance.acceptance_support import session
from tests.daemon.conversations.bypass.bypass_support import settle
from tests.daemon.conversations.user_target.user_target_support import (
    boot_with_user_agent,
    operator_send,
    record_ends,
)
from tests.support.golden.app import ALPHA, OWNER_USER, golden_daemon as golden_daemon


def test_message_reply_to_the_owner_is_recorded_and_not_pushed(golden_daemon, monkeypatch) -> None:
    booted = boot_with_user_agent(golden_daemon, monkeypatch)
    app = booted.app
    question = operator_send(app, ALPHA, "reply-unpushed")["messageId"]

    replied = app.handle("message.reply", {
        **session(ALPHA), "messageId": question, "message": "answer", "summary": "answer",
    })
    settle(booted)

    assert booted.daemon.user_delivery.requests == []
    assert record_ends(app, replied["replyMessageId"]) == {
        ("inbox", ALPHA, OWNER_USER), ("sender_copies", ALPHA, OWNER_USER),
    }
    assert app._inbox.pending_messages(OWNER_USER) == ()


def test_message_send_with_reply_to_the_owner_is_pushed(golden_daemon, monkeypatch) -> None:
    from tests.support.golden.app import ADAPTER
    from tests.support.identity.cell_push import bind_user_agent

    booted = boot_with_user_agent(golden_daemon, monkeypatch)
    app = booted.app
    bind_user_agent(app, ADAPTER)
    question = operator_send(app, ALPHA, "reply-pushed")["messageId"]

    answer = app.handle("message.send", {
        **session(ALPHA), "to": [OWNER_USER], "summary": "answer", "message": "answer",
        "replyTo": question, "idempotencyKey": "key-reply-pushed-answer",
    })

    [delivery] = answer["deliveries"]
    assert (delivery["pushed"], delivery["via"]) == (True, "user-agent")
    assert [call[0] for call in booted.daemon.gateway.calls] == ["oc_dm_chat"]
    assert booted.daemon.user_delivery.requests == []
