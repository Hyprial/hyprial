"""Plan slice S2: what ``conversations.*`` show for a record placeholder.

While the local queue holds the message the history shows the queue's body
at the placeholder's position (never less than before); once the queue no
longer holds it the entry has an empty ``message`` and ``bodyOmitted``
names the reason.  A device that reads only the document (another writer's
segment, no queue row) sees the placeholder the same way, and
``conversations.list`` counts it as a message.
"""

from __future__ import annotations

import hashlib
from typing import Any

from hyprial.daemon.impl.conversations import RecordEntry, SegmentStore, visible_messages
from hyprial.daemon.impl.delivery.ports import SOURCE_INBOX, SOURCE_SENDER_COPY
from hyprial.daemon.impl.conversations.keys import direct_key
from tests.daemon.conversations.conversation_support import record_lines
from tests.daemon.conversations.document.document_support import (
    RecordDaemon,
    cell_of,
    ids,
    record_daemon,
)
from tests.support.golden.app import ALPHA, BETA, golden_daemon as golden_daemon

CONV = "conv-placeholder"


def _redact(draft: Any) -> Any:
    return None if b"secret" in draft.body else draft


def _conversation(golden_daemon, monkeypatch) -> tuple[RecordDaemon, list[str], str]:
    booted = record_daemon(golden_daemon, monkeypatch, redact=_redact)
    sent = [
        booted.send(ALPHA, BETA, CONV, "before"),
        booted.send(ALPHA, BETA, CONV, "a secret"),
        booted.send(ALPHA, BETA, CONV, "after"),
    ]
    booted.settle()
    cell = cell_of(ALPHA, BETA, CONV)
    lines = record_lines(booted.fs.inner, booted.space(), cell)
    assert {line["messageId"] for line in lines if line.get("bodyOmitted")} == {sent[1]}
    return booted, sent, cell


def _head(booted: RecordDaemon, actor: str, cell: str) -> dict[str, Any]:
    listed = booted.listed(actor)
    return next(head for head in listed["conversations"] if head["conversationId"] == cell)


def test_the_history_shows_the_queue_body_while_the_queue_holds_it(
    golden_daemon, monkeypatch
) -> None:
    booted, sent, cell = _conversation(golden_daemon, monkeypatch)

    page = booted.history(BETA, cell)

    assert ids(page) == sent
    assert [entry["message"] for entry in page["entries"]] == [
        "body before", "body a secret", "body after",
    ]
    assert all("bodyOmitted" not in entry for entry in page["entries"])
    legacy = booted.app.handle(
        "message.history", {"from": BETA, "sessionRef": "session-beta",
                            "conversationId": CONV, "mode": "tail"},
    )
    assert all("bodyOmitted" not in entry for entry in legacy["entries"])


def test_the_marker_shows_once_the_queue_no_longer_holds_the_message(
    golden_daemon, monkeypatch
) -> None:
    booted, sent, cell = _conversation(golden_daemon, monkeypatch)
    booted.forget(*sent)

    for mode in ({}, {"mode": "tail"}):
        page = booted.history(BETA, cell, **mode)
        assert page["recordAvailable"] is True
        assert ids(page) == sent
        placeholder = page["entries"][1]
        assert placeholder["message"] == "" and placeholder["bodyOmitted"] == "redacted"
        assert (placeholder["from"], placeholder["to"]) == (ALPHA, BETA)
        assert placeholder["messageClass"] == "interaction"
        assert [entry["message"] for entry in page["entries"][::2]] == [
            "body before", "body after",
        ]
        assert all("bodyOmitted" not in entry for entry in page["entries"][::2])
    assert _head(booted, BETA, cell)["messages"] == 3


def test_a_device_reading_only_the_document_sees_the_placeholder(
    golden_daemon, monkeypatch
) -> None:
    booted = record_daemon(golden_daemon, monkeypatch)
    first = booted.send(ALPHA, BETA, CONV, "local")
    booted.settle()
    cell = cell_of(ALPHA, BETA, CONV)
    page = booted.history(ALPHA, cell)
    digest = hashlib.sha256(b"remote original").hexdigest()
    created = int(page["entries"][0]["createdAtMs"]) + 1
    remote = RecordEntry(
        key=f"{SOURCE_INBOX}:9000:{digest}", source=SOURCE_INBOX, position=9000,
        message_id="remote-1", conversation_id=CONV, thread_id=direct_key(ALPHA, BETA).id,
        cell_id=cell, sender=BETA, recipient=ALPHA, intent="request",
        created_at_ms=created, recorded_at_ms=created, payload_sha256=digest, body=b"",
    ).placeholder("too-large")
    SegmentStore(
        fs=lambda: booted.fs, space_for=lambda _authority: booted.space(), writer="node-2"
    ).append("user:op", cell, [remote], 0)

    merged = booted.history(ALPHA, cell)

    assert ids(merged) == [first, "remote-1"]
    assert merged["entries"][1]["message"] == ""
    assert merged["entries"][1]["bodyOmitted"] == "too-large"
    assert _head(booted, ALPHA, cell)["messages"] == 2


def _entry(source: str, key: str, **changes: Any) -> RecordEntry:
    digest = hashlib.sha256(b'{"message": "kept"}').hexdigest()
    entry = RecordEntry(
        key=key, source=source, position=1, message_id="m-1", conversation_id=CONV,
        thread_id="t", cell_id="c", sender=ALPHA, recipient=BETA, intent="request",
        created_at_ms=1, recorded_at_ms=1, payload_sha256=digest,
        body=b'{"message": "kept"}',
    )
    return entry.placeholder("redacted") if changes.get("omitted") else entry


def test_a_recorded_body_outranks_a_placeholder_of_the_same_message() -> None:
    inbox = _entry(SOURCE_INBOX, "inbox:1", omitted=True)
    copy = _entry(SOURCE_SENDER_COPY, "sender_copies:1")

    [message] = visible_messages([inbox, copy], {BETA})

    assert message.payload == b'{"message": "kept"}' and message.body_omitted is None
    [only] = visible_messages([inbox], {BETA})
    assert only.payload == b"" and only.body_omitted == "redacted"
