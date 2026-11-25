"""Group rows stay out of the 1:1 surfaces (slice GC4, draft §4.6).

A delivered group row carries the group id as its conversation id.  It
must not appear as a direct conversation in ``conversations.list`` (nor
its threads), must not be recorded into a 1:1 conversation document, and
``message.history`` refuses a group id.  Direct traffic of a daemon that
delivers group rows answers the same bytes as one without groups.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.daemon.impl.composition.conversations import compose_conversation_recorder
from tests.daemon.conversations.conversation_support import cell_runtime_for
from tests.daemon.conversations.group_delivery.delivery_support import (
    OWNER,
    GroupDaemon,
    group_daemon,
    group_rows,
    handle,
    make_group,
    send,
)
from tests.daemon.conversations.group_messages.group_message_support import deliver_row
from tests.support.golden.app import SESSIONS, golden_daemon as golden_daemon


def _delivered(booted: GroupDaemon) -> str:
    """Two group rows shaped as delivery writes them, independent of the wake rule."""

    group = make_group(booted, ("alpha", "beta"))
    posted = send(booted, group, "status: tests pass", "alpha", key="out-1")["messageId"]
    deliver_row(booted, group, posted, "beta", row_id="out-row-beta",
                sender=booted.address("alpha"))
    deliver_row(booted, group, posted, "gamma", row_id="out-row-gamma",
                sender=booted.address("alpha"))
    assert len(group_rows(booted, "beta", group)) == 1
    return group


def test_group_rows_are_not_direct_conversations(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = _delivered(booted)

    for who in ("alpha", "beta", "gamma", OWNER):
        listed = handle(booted, "conversations.list", who)
        assert all(head["legacyConversationId"] != group
                   for head in listed["conversations"]), who
        assert listed["conversations"] == [], who
    grouped = handle(booted, "conversations.list", "beta", kinds=["direct", "group"])
    assert [head["conversationId"] for head in grouped["conversations"]] == [group]


def test_group_rows_are_not_recorded_into_a_direct_document(
    golden_daemon, monkeypatch, tmp_path
) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    app = booted.app
    app._cell_runtime = cell_runtime_for(tmp_path, booted.fs)
    group = _delivered(booted)
    recorder = compose_conversation_recorder(app, fs=booted.fs, start=False)
    assert recorder is not None

    result = recorder.run_once()

    assert result.recorded == 0
    assert result.skipped == 2
    assert handle(booted, "conversations.list", "beta")["conversations"] == []
    assert len(group_rows(booted, "beta", group)) == 1


def test_message_history_refuses_a_group_id(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = _delivered(booted)

    for who in (OWNER, "beta"):
        with pytest.raises(DaemonRequestError) as raised:
            handle(booted, "message.history", who, conversationId=group)
        assert raised.value.code == "INVALID_ARGUMENT", who
        assert "conversations.history" in str(raised.value)


def _direct_script(booted: GroupDaemon) -> list[dict[str, Any]]:
    answers: list[dict[str, Any]] = []
    sent = handle(booted, "message.send", to=[booted.address("alpha")],
                  message="hello alpha", summary="hello alpha", idempotencyKey="direct-1")
    answers.append(sent)
    booted.advance()
    for who in ("alpha", "beta"):
        answers.append(handle(booted, "conversations.list", who))
    answers.append(booted.app.handle(
        "message.reply",
        {"from": booted.address("alpha"), "sessionRef": SESSIONS["alpha"],
         "messageId": sent["messageId"], "message": "hello op", "summary": "hello op"},
        _trusted_message_origin="ipc",
    ))
    booted.advance()
    answers.append(handle(booted, "message.history", conversationId=sent["conversationId"]))
    answers.append(handle(booted, "conversations.list"))
    return answers


def test_direct_answers_are_the_same_bytes_with_group_rows(golden_daemon, monkeypatch) -> None:
    without = _direct_script(group_daemon(golden_daemon, monkeypatch))
    grouped = group_daemon(golden_daemon, monkeypatch)
    _delivered(grouped)
    with_rows = _direct_script(grouped)

    assert len(with_rows) == len(without)
    for step, (left, right) in enumerate(zip(with_rows, without, strict=True)):
        assert json.dumps(left) == json.dumps(right), step
