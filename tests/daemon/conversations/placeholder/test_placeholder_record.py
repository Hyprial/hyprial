"""Plan slice S2: the recorder writes a bodiless placeholder for a refused row.

A row the redaction hook refuses (None, or a non-bytes body) or whose entry
is too large is recorded with its identity and metadata, no body and the
reason; the refused content never reaches the segment, the queue keeps the
body and no body grant is issued.  Cell faults write nothing and are logged
at error level; an unthreaded row is still skipped as before (D5).
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from hyprial.daemon.impl.composition.conversations import compose_conversation_recorder
from hyprial.daemon.impl.conversations import (
    ConversationBodies,
    RecordDraft,
    RecordEntry,
    RecordRef,
    conversation_cell_id,
)
from hyprial.daemon.impl.delivery.ports import BodyRef
from hyprial.daemon.impl.conversations.record import recorder as recorder_module
from hyprial.daemon.impl.inbox import InboxMessage
from hyprial.daemon.impl.orgfs import LocalOrgFs
from hyprial.daemon.impl.conversations.keys import direct_key
from tests.daemon.conversations.conversation_support import (
    OWNER_PRINCIPAL,
    Log,
    cell_runtime_for,
    home_space,
    inbox_bodies,
    record_lines,
)
from tests.daemon.delivery.message.test_message_pending_wait import (
    READER,
    _message,
    app as app,
    owned_applications as owned_applications,
)

DRIVER = "agent:op:node-1:driver"
CELL = conversation_cell_id(OWNER_PRINCIPAL, direct_key(DRIVER, READER).id, "conversation-1")
SECRET = "the-refused-secret-0123456789"


def _secret_message(message_id: str, *, size: int = 0) -> InboxMessage:
    text = SECRET + "x" * size
    return replace(_message(message_id), payload=json.dumps({"message": text}).encode())


def _recorder(app: Any, tmp_path: Any, **options: Any) -> tuple[Any, LocalOrgFs, Log]:
    fs = LocalOrgFs(author=OWNER_PRINCIPAL)
    app._cell_runtime = cell_runtime_for(tmp_path, fs)
    log = Log()
    recorder = compose_conversation_recorder(
        app, fs=fs, start=False, clock_ms=lambda: 1_700_000_000_000, logger=log, **options
    )
    assert recorder is not None
    return recorder, fs, log


def _raw_segments(fs: LocalOrgFs, cell_id: str) -> str:
    space = home_space(fs)
    assert space is not None
    base = f"cells/conversation/{cell_id}"
    content = ""
    for writer in fs.listdir(space, base):
        if writer.kind != "dir":
            continue
        for segment in fs.listdir(space, f"{base}/{writer.name}"):
            content += fs.read_text(space, f"{base}/{writer.name}/{segment.name}")[0]
    return content


def _assert_no_content(raw: str, payload: bytes) -> None:
    assert SECRET not in raw
    assert base64.b64encode(payload).decode("ascii") not in raw
    for offset in range(3):
        probe = base64.b64encode(payload[offset:offset + 24]).decode("ascii")[4:-4]
        assert probe not in raw


def _refuse(draft: RecordDraft) -> RecordDraft | None:
    return None


def _invalid(draft: RecordDraft) -> RecordDraft | None:
    return replace(draft, body=SECRET)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("reason", "options", "size"),
    [
        ("redacted", {"redact": _refuse}, 0),
        ("redaction-invalid", {"redact": _invalid}, 0),
        ("too-large", {}, 4096),
    ],
)
def test_a_refused_row_is_recorded_as_a_placeholder_without_its_content(
    app, tmp_path, monkeypatch, reason, options, size
) -> None:
    if reason == "too-large":
        monkeypatch.setattr(recorder_module, "ENTRY_MAX_BYTES", 2048)
    message = _secret_message("m-1", size=size)
    app._inbox.receive(message)
    recorder, fs, log = _recorder(app, tmp_path, **options)

    result = recorder.run_once()

    assert (result.recorded, result.omitted, result.granted, result.skipped) == (1, 1, 0, 0)
    [line] = record_lines(fs, home_space(fs), CELL)
    assert "body" not in line
    assert line["bodyOmitted"] == reason and line["redacted"] is True
    assert line["payloadSha256"] == hashlib.sha256(message.payload).hexdigest()
    assert (line["messageId"], line["sender"], line["recipient"], line["intent"]) == (
        "m-1", DRIVER, READER, "request",
    )
    assert line["createdAtMs"] == 1_000 and line["messageClass"] == "interaction"
    assert line["conversationId"] == "conversation-1" and line["cellId"] == CELL
    raw = _raw_segments(fs, CELL)
    _assert_no_content(raw, message.payload)
    assert len(raw.encode("utf-8")) < 1024
    bodies = inbox_bodies(app._inbox._database)
    assert bodies["m-1"]["payload"] == message.payload
    assert not bodies["m-1"]["granted"] and not bodies["m-1"]["dropped"]
    assert ("info", "conversations", "conversation.body_omitted") in [
        event[:3] for event in log.events
    ]
    again = recorder.run_once()
    assert (again.recorded, again.granted) == (0, 0)
    assert len(record_lines(fs, home_space(fs), CELL)) == 1


def test_a_placeholder_line_round_trips_and_never_resolves_a_body() -> None:
    entry = RecordEntry(
        key=f"inbox:1:{hashlib.sha256(b'').hexdigest()}", source="inbox", position=1,
        message_id="m-1", conversation_id="c", thread_id="t", cell_id=CELL,
        sender=DRIVER, recipient=READER, intent="request", created_at_ms=1,
        recorded_at_ms=2, payload_sha256=hashlib.sha256(b"").hexdigest(), body=b"",
    ).placeholder("some-future-reason")

    assert RecordEntry.from_line(entry.to_line()) == entry
    document = json.loads(entry.to_line())
    assert document["bodyOmitted"] == "some-future-reason" and "body" not in document
    for broken in (
        {**document, "body": ""},
        {**document, "redacted": False},
        {**document, "bodyOmitted": ""},
        {**document, "bodyOmitted": 7},
    ):
        assert RecordEntry.from_line(json.dumps(broken)) is None
    ref = RecordRef("space-1", "cells/conversation/x/rec-n/000000.jsonl", entry.key)
    bodies = ConversationBodies(SimpleNamespace(read=lambda _ref: entry))  # type: ignore[arg-type]
    assert bodies.resolve(
        BodyRef("inbox", 1, "m-1", entry.payload_sha256, ref.encode())
    ) is None


def _v1_reader(line: str) -> dict[str, Any] | None:
    """The RM1-era ``RecordEntry.from_line`` acceptance rule for body fields."""

    document = json.loads(line)
    if document.get("format") != "hyprial-conversation-record/v1":
        return None
    if not isinstance(document.get("body"), str) or not isinstance(
        document.get("redacted"), bool
    ):
        return None
    base64.b64decode(document["body"].encode("ascii"), validate=True)
    return document


def test_a_v1_reader_drops_a_placeholder_line_without_failing(app, tmp_path) -> None:
    app._inbox.receive(_secret_message("m-1"))
    app._inbox.receive(_message("m-2"))
    recorder, fs, _ = _recorder(
        app, tmp_path, redact=lambda draft: None if draft.message.message_id == "m-1" else draft
    )
    recorder.run_once()

    lines = _raw_segments(fs, CELL).splitlines()
    read = [_v1_reader(line) for line in lines]
    assert [document["messageId"] for document in read if document is not None] == ["m-2"]


def test_an_unthreaded_row_is_still_skipped_without_a_placeholder(app, tmp_path) -> None:
    app._inbox.receive(replace(_secret_message("m-1"), sender="system:hyprial:node-1"))
    recorder, fs, log = _recorder(app, tmp_path, redact=_refuse)

    result = recorder.run_once()

    assert (result.recorded, result.omitted, result.skipped) == (0, 0, 1)
    assert home_space(fs) is None
    skipped = [event for event in log.events if event[2] == "conversation.row_skipped"]
    assert [(level, fields["reason"]) for level, _, _, fields in skipped] == [
        ("info", "unthreaded"),
    ]
    assert not inbox_bodies(app._inbox._database)["m-1"]["granted"]
