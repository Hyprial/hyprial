"""Acceptance (d): after ack + retention the local queue keeps no body.

Phase A promise (design §10.0, S5/S6): "after ack plus retention the body is
dropped and only a reference is kept"; history still reads it, now from the
conversation cell.  The daemon boots like ``wiring/services.py``
(``boot_like_services``) with enforced links; an agent request, its reply and an operator
message are delivered, read and acked; the recorder writes them into their
conversation cells in the owner-home OrgFS space; the retention sweep then
drops every body from ``inbox.sqlite3`` (inbox rows and sender copies).
The local rows keep only residue (no body text anywhere in the row), while
``message.read_body`` and ``message.history`` answer byte for byte as
before, hydrated from the cell records.  With the conversation resolver
unbound, the same read is ``MESSAGE_BODY_UNAVAILABLE``: the body really comes
from the cell, not from a local copy.

PIN: passed on first run.  Seeded red case (temporary product edit,
reverted): ``ConversationBodies.resolve`` never resolving a record ->
``message.read_body`` after the drop raises ``no message body is readable
for this id``.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from hyprial.daemon.impl.delivery import DEFAULT_BODY_RETENTION_MS
from hyprial.kernel import DaemonRequestError
from tests.daemon.acceptance.acceptance_support import (
    DAY_MS,
    body_columns,
    boot_like_services,
    pending_ids,
    send,
    session,
)
from tests.daemon.conversations.conversation_support import home_space, record_lines
from tests.support.golden.app import (
    ALPHA,
    BETA,
    OWNER_USER,
    golden_daemon as golden_daemon,
)

REQUEST_TEXT = "what is the answer? ☃"
REPLY_TEXT = "42 ☃"
OWNER_TEXT = "operator note ☃"


def _converse(app: Any) -> dict[str, str]:
    request = str(send(app, ALPHA, BETA, "e2e", text=REQUEST_TEXT)["messageId"])
    owner = str(send(app, OWNER_USER, BETA, "owner", text=OWNER_TEXT)["messageId"])
    app.handle("message.read_body", {**session(BETA), "messageId": request})
    reply = app.handle(
        "message.reply",
        {
            **session(BETA),
            "messageId": request,
            "summary": "answer",
            "message": REPLY_TEXT,
        },
    )
    reply_id = str(reply["replyMessageId"])
    for actor, message_id in ((ALPHA, reply_id), (BETA, request), (BETA, owner)):
        app.handle("message.read_body", {**session(actor), "messageId": message_id})
        app.handle("message.ack", {**session(actor), "messageId": message_id})
    return {"request": request, "reply": reply_id, "owner": owner}


def _reads(app: Any, ids: dict[str, str]) -> dict[str, Any]:
    return {
        "request": app.handle(
            "message.read_body", {**session(BETA), "messageId": ids["request"]}
        ),
        "reply": app.handle(
            "message.read_body", {**session(ALPHA), "messageId": ids["reply"]}
        ),
        "owner": app.handle(
            "message.read_body", {**session(BETA), "messageId": ids["owner"]}
        ),
        "history-alpha": app.handle(
            "message.history", {**session(ALPHA), "conversationId": "conv-e2e"}
        ),
        "history-beta": app.handle(
            "message.history", {**session(BETA), "conversationId": "conv-e2e"}
        ),
    }


def _sweep(daemon: Any) -> None:
    daemon.clock.advance(DEFAULT_BODY_RETENTION_MS + DAY_MS)
    daemon.app._inbox.prune_inbox(now_ms=daemon.clock.ms())


def _recorded_messages(fs: Any) -> set[str]:
    space = home_space(fs)
    assert space is not None, "no owner-home space was created"
    lines = [
        line
        for cell in fs.listdir(space, "cells/conversation")
        if cell.kind == "dir"
        for line in record_lines(fs, space, cell.name)
    ]
    return {
        str(json.loads(base64.b64decode(line["body"])).get("message"))
        for line in lines
        if isinstance(line.get("body"), str)
    }


def test_acked_bodies_leave_the_queue_and_history_reads_them_from_the_cell(
    golden_daemon, monkeypatch
) -> None:
    daemon, recorder = boot_like_services(
        golden_daemon(bypass=False, links=False), monkeypatch
    )
    app = daemon.app
    ids = _converse(app)
    assert pending_ids(app, ALPHA) == [] and pending_ids(app, BETA) == []
    before = _reads(app, ids)
    assert before["request"]["message"] == REQUEST_TEXT
    assert before["reply"]["message"] == REPLY_TEXT
    assert before["owner"]["message"] == OWNER_TEXT

    recorded = recorder.run_once()
    assert recorded.failed is False and recorded.granted >= 3
    recorded_messages = _recorded_messages(app._require_orgfs_runtime().facade.inner)
    assert {REQUEST_TEXT, REPLY_TEXT, OWNER_TEXT} <= recorded_messages

    _sweep(daemon)
    _sweep(daemon)

    for table in ("inbox", "sender_copies"):
        rows = body_columns(app, table)
        for message_id in ids.values():
            if message_id not in rows:
                continue
            row = rows[message_id]
            assert row["dropped"] is True, (table, message_id, row)
            for text in (REQUEST_TEXT, REPLY_TEXT, OWNER_TEXT):
                assert text.encode("utf-8") not in row["payload"], (table, message_id)
    inbox = body_columns(app)
    assert all(inbox[message_id]["dropped"] for message_id in ids.values())

    assert _reads(app, ids) == before

    app._inbox.bind_body_retention(resolver=None)
    with pytest.raises(DaemonRequestError) as raised:
        app.handle("message.read_body", {**session(BETA), "messageId": ids["request"]})
    assert raised.value.code == "MESSAGE_BODY_UNAVAILABLE"
