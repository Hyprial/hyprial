"""Shared harness for the conversation bypass characterization tests.

Plan slice P1 (conversation-implementation-plan-2026-11-17, group 0): these
tests pin what dev does today on every message path the discussion record
(section 13) lists as not reaching the conversation record.  They assert
current behaviour, not wanted behaviour; each test names the slice (U1, U2,
S1, H1, G1) that is expected to flip it.

Every test runs the golden ``DaemonApplication`` booted like
``wiring/services.py`` (``boot_like_services``): real ``message.send`` /
``message.reply``, the real custody inbox, the real thread authority and
the real conversation recorder over an isolated ``LocalOrgFs``.  Links are
enforced (``bypass=False``) and only ALPHA and BETA are linked; no path
characterized here was held by the link gate.

A path is observed at three layers, the same three section 13 uses:

- queue: whether a row exists in ``inbox`` / ``outbox`` / ``sender_copies``;
- feed: whether ``interaction_rows_after`` hands the row to the recorder;
- record: whether the recorder recorded the row or skipped it and why, read
  from the recorder's own index (``conversation-records.sqlite3``), which
  holds one status per row it was handed.

``boot`` sends one ALPHA to BETA control message and ``settle`` asserts it
was recorded from both sources, so "not recorded" never passes because the
recorder could not record anything at all.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from typing import Any

import pytest

from hyprial.daemon.impl.conversations import RECORDS_DATABASE, ConversationRecorder
from hyprial.daemon.impl.conversations.record.store import (
    SKIPPED_PREFIX,
    STATUS_GRANTED,
    STATUS_RECORDED,
)
from tests.daemon.acceptance.acceptance_support import (
    LogCapture,
    boot_like_services,
    capture_logs,
    send,
)
from tests.daemon.conversations.conversation_support import home_space, record_lines
from tests.support.golden.app import ALPHA, BETA, GoldenDaemon

QUEUE_TABLES = ("inbox", "outbox", "sender_copies")
FEED_SOURCES = ("inbox", "sender_copies")
_RECORDED = frozenset({STATUS_RECORDED, STATUS_GRANTED})

__all__ = [
    "FEED_SOURCES",
    "QUEUE_TABLES",
    "Booted",
    "boot",
    "fed",
    "origin_of",
    "queue_rows",
    "recorded",
    "settle",
    "stored",
]


@dataclass
class Booted:
    """One booted golden daemon with its recorder, log capture and control id."""

    daemon: GoldenDaemon
    recorder: ConversationRecorder
    logs: LogCapture
    control: str

    @property
    def app(self) -> Any:
        return self.daemon.app


def boot(
    golden_daemon: Any, monkeypatch: pytest.MonkeyPatch, *, user_agent: bool = False
) -> Booted:
    """Boot the daemon, link ALPHA and BETA and send the control message.

    ``user_agent`` gives the daemon the owner's user agent first (UA2).
    """

    logs = capture_logs(monkeypatch)
    daemon, recorder = boot_like_services(
        golden_daemon(bypass=False, links=False, user_agent=user_agent), monkeypatch
    )
    control = str(send(daemon.app, ALPHA, BETA, "control")["messageId"])
    return Booted(daemon, recorder, logs, control)


def queue_rows(app: Any, table: str) -> dict[str, dict[str, Any]]:
    """Every row of one queue table by message id."""

    with closing(sqlite3.connect(app.state_dir / "inbox.sqlite3")) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT message_id, conversation_id, sender, recipient, payload, intent,"
            f" lifecycle, idempotency_key, thread_id FROM {table}"
        ).fetchall()
    return {str(row["message_id"]): dict(row) for row in rows}


def stored(app: Any, message_id: str) -> dict[str, dict[str, Any]]:
    """The queue tables that hold *message_id*, each with its row."""

    found = {table: queue_rows(app, table).get(message_id) for table in QUEUE_TABLES}
    return {table: row for table, row in found.items() if row is not None}


def origin_of(row: dict[str, Any]) -> dict[str, Any]:
    """The ``origin`` object of one stored row's payload."""

    origin = json.loads(row["payload"]).get("origin")
    assert isinstance(origin, dict)
    return origin


def _feed(app: Any) -> dict[tuple[str, int, str], str]:
    rows: dict[tuple[str, int, str], str] = {}
    for source in FEED_SOURCES:
        page = app._inbox.interaction_rows_after(source, 0, limit=256)
        for row in page.rows:
            rows[(source, row.position, row.payload_sha256)] = row.message.message_id
    return rows


def fed(app: Any, message_id: str) -> set[str]:
    """The feed sources that hand *message_id* to the recorder."""

    return {source for (source, _, _), found in _feed(app).items() if found == message_id}


@dataclass
class Settled:
    """The recorder's own index after a full pass: one status per fed row."""

    statuses: dict[tuple[str, str], tuple[str, str | None]]

    def skip_reasons(self, message_id: str) -> dict[str, str]:
        """Skip reason per feed source for one message."""

        return {
            source: status.removeprefix(SKIPPED_PREFIX)
            for (source, found), (status, _) in self.statuses.items()
            if found == message_id and status.startswith(SKIPPED_PREFIX)
        }

    def sources(self, message_id: str) -> list[str]:
        """The feed sources whose row of one message was recorded."""

        return sorted(
            source
            for (source, found), (status, _) in self.statuses.items()
            if found == message_id and status in _RECORDED
        )

    def recorded_messages(self) -> set[str]:
        """Every message id with at least one recorded row."""

        return {
            found for (_, found), (status, _) in self.statuses.items() if status in _RECORDED
        }

    def cells(self, message_id: str) -> set[str | None]:
        """The conversation cells one message was recorded into."""

        return {
            cell_id
            for (source, found), (status, cell_id) in self.statuses.items()
            if found == message_id and status in _RECORDED
        }


def recorded(app: Any) -> list[dict[str, Any]]:
    """Every record line in every conversation cell of the owner-home space."""

    fs = app._require_orgfs_runtime().facade.inner
    space = home_space(fs)
    assert space is not None, "no owner-home space was created"
    return [
        line
        for cell in fs.listdir(space, "cells/conversation")
        if cell.kind == "dir"
        for line in record_lines(fs, space, cell.name)
    ]


def settle(booted: Booted) -> Settled:
    """Run the recorder to the end of both sources; the control must be recorded."""

    rows = _feed(booted.app)
    for _ in range(16):
        result = booted.recorder.run_once()
        assert result.failed is False and result.unavailable is False
        if not result.more:
            break
    else:
        raise AssertionError("the recorder did not reach the end of its feed")
    with closing(sqlite3.connect(booted.app.state_dir / RECORDS_DATABASE)) as db:
        index = db.execute(
            "SELECT source, position, payload_sha256, status, cell_id FROM records"
        ).fetchall()
    statuses = {
        (str(source), rows[(str(source), int(position), str(digest))]): (
            str(status),
            None if cell_id is None else str(cell_id),
        )
        for source, position, digest, status, cell_id in index
    }
    settled = Settled(statuses=statuses)
    assert settled.sources(booted.control) == ["inbox", "sender_copies"]
    return settled

