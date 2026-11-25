"""T5: the retention-time golden (plan §1.2 step 8, §2.8, §4 "Allowed re-pins").

An INTERACTION request and reply between two agents is delivered and acked
on a golden daemon whose S6 conversation recorder writes into an isolated
``LocalOrgFs`` owner-home space.  The recorder grants both bodies, the clock
moves past ``DEFAULT_BODY_RETENTION_MS`` and the sweep runs: the local queue
then holds only residue (observed), and ``message.read_body``,
``message.history`` and ``message.pending.*`` answer exactly as they did
just before the drop.  The test asserts that equality step by step and then
pins the whole scenario as ``retention/interaction-after-drop``.

The pending message is sent after the clock moves, so it is live (inside
its hold TTL) for both read rounds and shows that a pending row is never a
drop candidate.  Sender copies settle on the sweep that runs at the
delivery time, so both tables are past retention at the recorded sweep.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from types import SimpleNamespace
from typing import Any

import pytest

from hyprial.daemon.impl.composition.conversations import compose_conversation_recorder
from hyprial.daemon.impl.delivery import DEFAULT_BODY_RETENTION_MS, RESIDUE_KEYS
from hyprial.daemon.impl.orgfs import LocalOrgFs
from tests.support.golden.app import (
    ALPHA,
    BETA,
    OWNER_USER,
    SESSIONS,
    GoldenDaemon,
    golden_daemon as golden_daemon,
)
from tests.support.golden.snapshot import Golden

GROUP = "retention"
SCENARIO = "interaction-after-drop"
DAY_MS = 86_400_000
CONVERSATION = "conv-retention"


def _session(name: str) -> dict[str, str]:
    actor = {"alpha": ALPHA, "beta": BETA}[name]
    return {"from": actor, "sessionRef": SESSIONS[name]}


def _queue(daemon: GoldenDaemon) -> dict[str, list[dict[str, Any]]]:
    database = daemon.app.state_dir / "inbox.sqlite3"
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as db:
        return {
            table: [
                {
                    "messageId": row[0],
                    "payload": bytes(row[1]).decode("utf-8"),
                    "dropped": row[2] is not None,
                    "granted": row[3] is not None,
                }
                for row in db.execute(
                    "SELECT message_id, payload, body_dropped_at_ms, body_payload_sha256"
                    f" FROM {table} WHERE conversation_id = ?"
                    " ORDER BY created_at_ms, message_id",
                    (CONVERSATION,),
                )
            ]
            for table in ("inbox", "sender_copies")
        }


def _converse(golden: Golden) -> tuple[str, str]:
    sent = golden.call(
        "message.send",
        {**_session("alpha"), "to": [BETA], "summary": "question",
         "message": "what is the answer? ☃", "conversationId": CONVERSATION,
         "idempotencyKey": "key-retention"},
    )
    request = str(sent["messageId"])
    golden.call("message.read_body", {**_session("beta"), "messageId": request})
    reply = golden.call(
        "message.reply",
        {**_session("beta"), "messageId": request, "summary": "answer",
         "message": "42 ☃"},
    )
    reply_id = str(reply["replyMessageId"])
    golden.call("message.read_body", {**_session("alpha"), "messageId": reply_id})
    golden.call("message.ack", {**_session("alpha"), "messageId": reply_id})
    golden.call("message.ack", {**_session("beta"), "messageId": request})
    return request, reply_id


READS: tuple[tuple[str, str, Callable[[str, str], dict[str, Any]]], ...] = (
    ("message.read_body", "request",
     lambda request, _reply: {**_session("beta"), "messageId": request}),
    ("message.read_body", "reply",
     lambda _request, reply: {**_session("alpha"), "messageId": reply}),
    ("message.history", "alpha",
     lambda _request, _reply: {**_session("alpha"), "conversationId": CONVERSATION}),
    ("message.history", "beta",
     lambda _request, _reply: {**_session("beta"), "conversationId": CONVERSATION}),
    ("message.pending.list", "beta", lambda _request, _reply: _session("beta")),
    ("message.pending.batch", "beta", lambda _request, _reply: _session("beta")),
    ("message.pending.wait", "beta",
     lambda _request, _reply: {**_session("beta"), "knownMessageIds": [], "holdMs": 0}),
    ("message.pending.list", "alpha", lambda _request, _reply: _session("alpha")),
)


def _read_round(golden: Golden, phase: str, request: str, reply: str) -> list[dict]:
    start = len(golden.steps)
    for method, who, params in READS:
        golden.call(method, params(request, reply), label=f"{phase}:{who}")
    return [
        {key: value for key, value in step.items() if key not in ("step", "label")}
        for step in golden.steps[start:]
    ]


def scenario_interaction_after_drop(
    daemon: GoldenDaemon, monkeypatch: pytest.MonkeyPatch
) -> Golden:
    app = daemon.app
    fs = LocalOrgFs(author=OWNER_USER)
    monkeypatch.setattr(
        app, "_require_orgfs_runtime", lambda: SimpleNamespace(facade=fs), raising=False
    )
    recorder = compose_conversation_recorder(app, start=False, clock_ms=daemon.clock.ms)
    assert recorder is not None
    golden = daemon.golden(GROUP, SCENARIO)
    request, reply = _converse(golden)
    recorded = recorder.run_once()
    golden.observe("recorder-pass", {"granted": recorded.granted, "failed": recorded.failed})
    app._inbox.prune_inbox(now_ms=daemon.clock.ms())

    daemon.clock.advance(DEFAULT_BODY_RETENTION_MS + DAY_MS)
    golden.call(
        "message.send",
        {**_session("alpha"), "to": [BETA], "summary": "later",
         "message": "still pending", "conversationId": CONVERSATION,
         "idempotencyKey": "key-retention-pending"},
        label="pending",
    )
    golden.observe("queue-before-sweep", _queue(daemon))
    before = _read_round(golden, "pre-drop", request, reply)

    app._inbox.prune_inbox(now_ms=daemon.clock.ms())
    queue = golden.observe("queue-after-sweep", _queue(daemon))
    after = _read_round(golden, "post-drop", request, reply)

    dropped = {
        (table, row["messageId"])
        for table, rows in queue.items()
        for row in rows
        if row["dropped"]
    }
    assert dropped == {
        (table, message_id)
        for table in ("inbox", "sender_copies")
        for message_id in (request, reply)
    }
    assert all(
        set(json.loads(row["payload"])) <= set(RESIDUE_KEYS)
        for rows in queue.values()
        for row in rows
        if row["dropped"]
    ), queue
    assert after == before
    return golden


def test_retention_golden(
    golden_daemon: Callable[..., GoldenDaemon], monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario_interaction_after_drop(golden_daemon(), monkeypatch).check()
