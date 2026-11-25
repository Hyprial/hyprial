"""Direct (1:1) send, reply and history are unchanged by groups (slice GC3).

Two daemons run the same direct script: one has no group, the other has a
group and, in the ``traffic`` variant, group sends (one replying, one
mentioning) and group history reads before the script.  Every direct answer
must be the same bytes.  The ``created`` variant passed on the base commit
(GC2) before any GC3 code was written: it is the characterization the
``traffic`` variant is held to.

Group replies are not part of this comparison; slice GC4's
``group_delivery/test_group_rows_stay_out.py`` holds direct answers to the
same bytes with delivered group rows present.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.daemon.conversations.group_messages.group_message_support import (
    OWNER,
    GroupDaemon,
    group_daemon,
    handle,
    history,
    make_group,
    send,
)
from tests.support.golden.app import SESSIONS, golden_daemon as golden_daemon


def _group_traffic(booted: GroupDaemon, traffic: bool) -> None:
    group = make_group(booted, ("alpha", "beta"))
    if not traffic:
        return
    first = send(booted, group, "please cut the release", key="group-traffic-1")
    send(booted, group, "on it", "alpha", key="group-traffic-2",
         replyTo=first["messageId"], mentions=[OWNER])
    history(booted, group)
    history(booted, group, "alpha", mode="tail", limit=1)


def _direct_script(booted: GroupDaemon) -> list[dict[str, Any]]:
    answers: list[dict[str, Any]] = []
    sent = handle(booted, "message.send", to=[booted.address("alpha")],
                  message="hello alpha", summary="hello alpha", idempotencyKey="direct-1")
    answers.append(sent)
    booted.advance()
    answers.append(booted.app.handle(
        "message.reply",
        {"from": booted.address("alpha"), "sessionRef": SESSIONS["alpha"],
         "messageId": sent["messageId"], "message": "hello op", "summary": "hello op"},
        _trusted_message_origin="ipc",
    ))
    booted.advance()
    explicit = handle(booted, "message.send", "alpha", to=[booted.address("beta")],
                      message="explicit", summary="explicit", conversationId="conv-explicit",
                      idempotencyKey="direct-2")
    answers.append(explicit)
    booted.advance()
    listed = handle(booted, "conversations.list")
    answers.append(listed)
    for head in listed["conversations"]:
        answers.append(handle(booted, "conversations.history",
                              conversationId=head["conversationId"]))
        answers.append(handle(booted, "conversations.history",
                              conversationId=head["conversationId"], mode="tail", limit=1))
    answers.append(handle(booted, "message.history", conversationId=sent["conversationId"]))
    answers.append(handle(booted, "conversations.list", "alpha"))
    return answers


def _bytes(answers: list[dict[str, Any]]) -> str:
    return json.dumps(answers, sort_keys=False, separators=(",", ":"))


@pytest.mark.parametrize("traffic", [False, True], ids=["created", "traffic"])
def test_direct_answers_are_the_same_bytes_with_groups(
    golden_daemon, monkeypatch, traffic
) -> None:
    # One daemon after the other: each build pins the process clock to its
    # own frozen clock, so the second build starts the script at the same time.
    without = _direct_script(group_daemon(golden_daemon, monkeypatch))
    grouped = group_daemon(golden_daemon, monkeypatch)
    _group_traffic(grouped, traffic)
    with_groups = _direct_script(grouped)

    assert len(without[3]["conversations"]) == 1
    assert len(without[-1]["conversations"]) == 2
    assert without[1]["replied"] is True
    assert len(with_groups) == len(without)
    for step, (left, right) in enumerate(zip(with_groups, without, strict=True)):
        assert _bytes([left]) == _bytes([right]), step
