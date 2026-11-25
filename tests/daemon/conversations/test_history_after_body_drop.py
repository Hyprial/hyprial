"""End to end: a recorded body is dropped locally and hydrates byte for byte.

A real golden daemon (custody facade, link service, send/reply/ack paths)
records each delivered row (INTERACTION and, since plan slice S1, SYSTEM)
into its conversation cell in an isolated LocalOrgFs owner-home space.  After ack and the retention window
the sweep drops the bodies from ``inbox.sqlite3``; ``message.read_body`` and
``message.history`` then answer byte-identically through
``ConversationBodies``.  With OrgFS down the messages still flow and the
records catch up once it returns.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from types import SimpleNamespace
from typing import Any

import pytest

from hyprial.daemon.impl.cells.params import DELIVERY_BODY_RETENTION_MS
from hyprial.daemon.impl.composition.cells import cell_runtime
from hyprial.daemon.impl.composition.conversations import (
    close_conversation_recorder,
    compose_conversation_recorder,
)
from hyprial.daemon.impl.conversations import RECORDS_DATABASE, ConversationBodies
from hyprial.daemon.impl.delivery import DEFAULT_BODY_RETENTION_MS
from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage
from hyprial.daemon.impl.orgfs import LocalOrgFs
from hyprial.kernel import DISPATCH_MESSAGE_PREFIX, DaemonRequestError
from tests.daemon.conversations.conversation_support import (
    OWNER_PRINCIPAL,
    FlakyOrgFs,
)
from tests.support.golden.app import (
    ALPHA,
    BETA,
    SESSIONS,
    golden_daemon as golden_daemon,
)

DAY_MS = 86_400_000
HOUR_MS = 3_600_000


def _session(actor: str) -> dict[str, str]:
    name = {ALPHA: "alpha", BETA: "beta"}[actor]
    return {"from": actor, "sessionRef": SESSIONS[name]}


def _attach_orgfs(app: Any, fs: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        app, "_require_orgfs_runtime", lambda: SimpleNamespace(facade=fs), raising=False
    )


def _converse(app: Any) -> tuple[str, str]:
    sent = app.handle(
        "message.send",
        {
            **_session(ALPHA),
            "to": [BETA],
            "summary": "question",
            "message": "what is the answer? ☃",
            "conversationId": "conv-e2e",
            "idempotencyKey": "key-e2e",
        },
    )
    request = str(sent["messageId"])
    app.handle("message.read_body", {**_session(BETA), "messageId": request})
    reply = app.handle(
        "message.reply",
        {**_session(BETA), "messageId": request, "summary": "answer", "message": "42 ☃"},
    )
    reply_id = str(reply["replyMessageId"])
    app.handle("message.read_body", {**_session(ALPHA), "messageId": reply_id})
    app.handle("message.ack", {**_session(ALPHA), "messageId": reply_id})
    app.handle("message.ack", {**_session(BETA), "messageId": request})
    return request, reply_id


def _reads(app: Any, request: str, reply_id: str) -> dict[str, Any]:
    return {
        "request": app.handle("message.read_body", {**_session(BETA), "messageId": request}),
        "reply": app.handle("message.read_body", {**_session(ALPHA), "messageId": reply_id}),
        "history-alpha": app.handle(
            "message.history", {**_session(ALPHA), "conversationId": "conv-e2e"}
        ),
        "history-beta": app.handle(
            "message.history", {**_session(BETA), "conversationId": "conv-e2e"}
        ),
    }


def _dropped(app: Any) -> dict[str, list[str]]:
    with closing(sqlite3.connect(app.state_dir / "inbox.sqlite3")) as db:
        return {
            table: [
                str(row[0])
                for row in db.execute(
                    f"SELECT message_id FROM {table} WHERE body_dropped_at_ms IS NOT NULL"
                    " ORDER BY message_id"
                )
            ]
            for table in ("inbox", "sender_copies")
        }


def _sweep_past_retention(daemon: Any) -> None:
    """Sender copies settle on the first sweep, so they drop one window later."""

    daemon.clock.advance(DEFAULT_BODY_RETENTION_MS + DAY_MS)
    daemon.app._inbox.prune_inbox(now_ms=daemon.clock.ms())


def test_read_body_and_history_are_byte_identical_after_the_drop(
    golden_daemon, monkeypatch
) -> None:
    daemon = golden_daemon()
    app = daemon.app
    fs = LocalOrgFs(author=OWNER_PRINCIPAL)
    _attach_orgfs(app, fs, monkeypatch)
    recorder = compose_conversation_recorder(app, start=False, clock_ms=daemon.clock.ms)
    assert recorder is not None
    assert isinstance(app._inbox.bodies.resolver, ConversationBodies)

    request, reply_id = _converse(app)
    before = _reads(app, request, reply_id)
    result = recorder.run_once()
    assert result.failed is False and result.granted >= 2

    _sweep_past_retention(daemon)

    dropped = _dropped(app)
    assert sorted(dropped["inbox"]) == sorted([request, reply_id])
    assert _reads(app, request, reply_id) == before

    _sweep_past_retention(daemon)

    assert sorted(_dropped(app)["sender_copies"]) == sorted([request, reply_id])
    assert _reads(app, request, reply_id) == before

    app._inbox.bind_body_retention(resolver=None)
    with pytest.raises(DaemonRequestError) as raised:
        app.handle("message.read_body", {**_session(BETA), "messageId": request})
    assert raised.value.code == "MESSAGE_BODY_UNAVAILABLE"


def test_with_orgfs_down_messages_flow_and_records_catch_up(
    golden_daemon, monkeypatch
) -> None:
    daemon = golden_daemon()
    app = daemon.app
    flaky = FlakyOrgFs(LocalOrgFs(author=OWNER_PRINCIPAL))
    _attach_orgfs(app, flaky, monkeypatch)
    recorder = compose_conversation_recorder(app, start=False, clock_ms=daemon.clock.ms)
    assert recorder is not None
    flaky.down = True

    request, reply_id = _converse(app)
    before = _reads(app, request, reply_id)
    assert recorder.run_once().failed
    _sweep_past_retention(daemon)
    assert _dropped(app) == {"inbox": [], "sender_copies": []}

    flaky.down = False
    caught_up = recorder.run_once()
    assert caught_up.failed is False and caught_up.granted >= 2
    daemon.app._inbox.prune_inbox(now_ms=daemon.clock.ms() + DEFAULT_BODY_RETENTION_MS + DAY_MS)

    assert sorted(_dropped(app)["inbox"]) == sorted([request, reply_id])
    assert _reads(app, request, reply_id) == before


def test_a_corrupt_records_database_still_hydrates_dropped_bodies(
    golden_daemon, monkeypatch
) -> None:
    daemon = golden_daemon()
    app = daemon.app
    _attach_orgfs(app, LocalOrgFs(author=OWNER_PRINCIPAL), monkeypatch)
    recorder = compose_conversation_recorder(app, start=False, clock_ms=daemon.clock.ms)
    assert recorder is not None
    request, reply_id = _converse(app)
    before = _reads(app, request, reply_id)
    assert recorder.run_once().granted >= 2
    _sweep_past_retention(daemon)
    assert sorted(_dropped(app)["inbox"]) == sorted([request, reply_id])

    close_conversation_recorder(app)
    app._conversation_recorder_closed = False
    app._inbox.bind_body_retention(resolver=None)
    (app.state_dir / RECORDS_DATABASE).write_bytes(b"this is not a sqlite database" * 64)

    assert compose_conversation_recorder(app, start=False) is None
    assert isinstance(app._inbox.bodies.resolver, ConversationBodies)
    assert _reads(app, request, reply_id) == before


def _agent_retention(app: Any, actor_name: str, retention_ms: int) -> None:
    runtime = cell_runtime(app)
    assert runtime is not None
    agent = app.agents.get(actor_name)
    assert agent is not None
    runtime.update(
        agent.cell_id,
        params={DELIVERY_BODY_RETENTION_MS.key: retention_ms},
    )


def _retention_by_table(app: Any) -> dict[str, dict[str, int | None]]:
    with closing(sqlite3.connect(app.state_dir / "inbox.sqlite3")) as db:
        return {
            table: {
                str(row[0]): row[1]
                for row in db.execute(f"SELECT message_id, body_retention_ms FROM {table}")
            }
            for table in ("inbox", "sender_copies")
        }


def test_each_grant_carries_its_local_agents_cell_retention(golden_daemon, monkeypatch) -> None:
    daemon = golden_daemon()
    app = daemon.app
    _attach_orgfs(app, LocalOrgFs(author=OWNER_PRINCIPAL), monkeypatch)
    recorder = compose_conversation_recorder(app, start=False, clock_ms=daemon.clock.ms)
    assert recorder is not None
    _agent_retention(app, "alpha", 2 * HOUR_MS)
    _agent_retention(app, "beta", 3 * HOUR_MS)

    request, reply_id = _converse(app)
    assert recorder.run_once().granted >= 4

    retention = _retention_by_table(app)
    assert retention["inbox"][request] == 3 * HOUR_MS
    assert retention["inbox"][reply_id] == 2 * HOUR_MS
    assert retention["sender_copies"][request] == 2 * HOUR_MS
    assert retention["sender_copies"][reply_id] == 3 * HOUR_MS


def test_a_recorded_system_row_drops_on_the_code_default_and_hydrates(
    golden_daemon, monkeypatch
) -> None:
    """Plan slice S1: a SYSTEM row is recorded, granted and restored from its record."""

    daemon = golden_daemon()
    app = daemon.app
    _attach_orgfs(app, LocalOrgFs(author=OWNER_PRINCIPAL), monkeypatch)
    recorder = compose_conversation_recorder(app, start=False, clock_ms=daemon.clock.ms)
    assert recorder is not None
    _agent_retention(app, "beta", 2 * HOUR_MS)
    dispatch = InboxMessage(
        message_id=f"{DISPATCH_MESSAGE_PREFIX}00000000-0000-4000-8000-000000000009",
        conversation_id="workflow",
        sender=app._dispatch_service_actor,
        recipient=BETA,
        payload=b'{"message":"run step one \\u2603"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=daemon.clock.ms(),
    )
    assert app._inbox.receive(dispatch).accepted
    read = {**_session(BETA), "messageId": dispatch.message_id}
    app.handle("message.read_body", read)
    app.handle("message.ack", read)
    before = app.handle("message.read_body", read)

    assert recorder.run_once().granted == 1
    assert _retention_by_table(app)["inbox"][dispatch.message_id] == DEFAULT_BODY_RETENTION_MS
    daemon.clock.advance(DEFAULT_BODY_RETENTION_MS - DAY_MS)
    app._inbox.prune_inbox(now_ms=daemon.clock.ms())
    assert _dropped(app)["inbox"] == []

    _sweep_past_retention(daemon)

    assert _dropped(app)["inbox"] == [dispatch.message_id]
    assert app.handle("message.read_body", read) == before
