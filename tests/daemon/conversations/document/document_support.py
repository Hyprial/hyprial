"""Harness for the record read model tests (plan slice RM1).

A golden daemon with the conversation recorder composed over an isolated
``LocalOrgFs`` injected into the placement (so the read side reads it, as
it reads the composed OrgFS runtime of a started daemon).  The recorder
thread is not started: a test decides when the record catches up, which is
how a row the recorder has not reached yet is produced.

``forget`` deletes a message's rows from every queue table: the queue can
then no longer supply it, so a test that still sees it proves the answer
came from the record.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from hyprial.daemon.impl.composition.conversations import compose_conversation_recorder
from hyprial.daemon.impl.conversations import ConversationRecorder, conversation_cell_id
from hyprial.daemon.impl.orgfs import LocalOrgFs
from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
from hyprial.daemon.impl.conversations.keys import direct_key
from tests.daemon.conversations.conversation_support import (
    OWNER_PRINCIPAL,
    FlakyOrgFs,
    home_space,
)
from tests.support.golden.app import ALPHA, BETA, GAMMA, SESSIONS, GoldenDaemon

NAMES = {ALPHA: "alpha", BETA: "beta", GAMMA: "gamma"}
QUEUE_TABLES = ("inbox", "outbox", "sender_copies")


def caller(actor: str) -> dict[str, str]:
    """The read params of *actor*: a golden agent's session, else ``from``."""

    if actor in NAMES:
        return {"from": actor, "sessionRef": SESSIONS[NAMES[actor]]}
    return {"from": actor}


@dataclass
class RecordDaemon:
    """A golden daemon whose conversation record the read side can read."""

    daemon: GoldenDaemon
    recorder: ConversationRecorder
    fs: FlakyOrgFs

    @property
    def app(self) -> Any:
        return self.daemon.app

    def send(self, sender: str, to: str, conversation: str, key: str) -> str:
        answer = self.app.handle(
            "message.send",
            {
                **caller(sender),
                "to": [to],
                "summary": f"summary {key}",
                "message": f"body {key}",
                "conversationId": conversation,
                "idempotencyKey": f"key-{key}",
            },
        )
        self.daemon.clock.advance(5)
        return str(answer["messageId"])

    def reply(self, sender: str, message_id: str, key: str) -> str:
        answer = self.app.handle(
            "message.reply",
            {**caller(sender), "messageId": message_id, "summary": key,
             "message": f"reply {key}"},
        )
        self.daemon.clock.advance(5)
        return str(answer["replyMessageId"])

    def settle(self) -> None:
        for _ in range(16):
            result = self.recorder.run_once()
            assert result.failed is False and result.unavailable is False
            if not result.more:
                return
        raise AssertionError("the recorder did not reach the end of its feed")

    def forget(self, *message_ids: str) -> None:
        with closing(sqlite3.connect(self.app.state_dir / "inbox.sqlite3")) as db, db:
            for table in QUEUE_TABLES:
                db.executemany(
                    f"DELETE FROM {table} WHERE message_id = ?",
                    [(message_id,) for message_id in message_ids],
                )

    def space(self) -> str:
        found = home_space(self.fs.inner)
        assert found is not None
        return found

    def history(self, actor: str, cell_id: str, **params: Any) -> dict[str, Any]:
        return METHOD_REGISTRY.invoke(
            self.app, "conversations.history",
            {**caller(actor), "conversationId": cell_id, **params},
        )

    def listed(self, actor: str, **params: Any) -> dict[str, Any]:
        return METHOD_REGISTRY.invoke(
            self.app, "conversations.list", {**caller(actor), **params}
        )


def cell_of(a: str, b: str, legacy: str) -> str:
    key = direct_key(a, b)
    thread = None if key is None else key.id
    assert thread is not None
    return conversation_cell_id(OWNER_PRINCIPAL, thread, legacy)


def record_daemon(
    golden_daemon: Callable[..., GoldenDaemon],
    monkeypatch: pytest.MonkeyPatch,
    **recorder_options: Any,
) -> RecordDaemon:
    daemon = golden_daemon()
    app = daemon.app
    fs = FlakyOrgFs(LocalOrgFs(author=OWNER_PRINCIPAL))
    monkeypatch.setattr(
        app, "_require_orgfs_runtime", lambda: SimpleNamespace(facade=fs), raising=False
    )
    recorder = compose_conversation_recorder(
        app, fs=fs, start=False, clock_ms=daemon.clock.ms, **recorder_options
    )
    assert recorder is not None
    return RecordDaemon(daemon, recorder, fs)


def ids(page: dict[str, Any]) -> list[str]:
    return [entry["messageId"] for entry in page["entries"]]


__all__ = [
    "NAMES",
    "RecordDaemon",
    "caller",
    "cell_of",
    "ids",
    "record_daemon",
]
