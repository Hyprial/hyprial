"""Shared wiring for the phase A acceptance tests (design §10.0).

Every acceptance test runs a real golden ``DaemonApplication`` (custody
facade, link service, send/reply/ack paths) in an isolated tmp home, with
two isolated OrgFS doubles: the golden in-memory ``OrgFsWithMeta`` that the
link store reads, and a ``LocalOrgFs`` behind ``CellOrgFs`` that the cell
runtime and the conversation recorder reach as the app's own OrgFS runtime.
``Outage`` takes both down and up together.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace
from typing import Any

import pytest

import hyprial.daemon.impl.composition.cells.wiring as cell_wiring
from hyprial.daemon.impl.cells.codec import decode, encode
from hyprial.daemon.impl.cells.model import Cell, CellBinding, CellMember, CellScope
from hyprial.daemon.impl.composition.conversations import compose_conversation_recorder
from hyprial.daemon.impl.composition.delivery_params import bind_body_retention_params
from hyprial.daemon.impl.conversations import ConversationRecorder
from hyprial.daemon.impl.orgfs import LocalOrgFs
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.identity import org_space_name
from hyprial.kernel import subject_cell_id
from hyprial.kernel.impl.log import Logger
from tests.daemon.conversations.conversation_support import OWNER_PRINCIPAL, FlakyOrgFs
from tests.support.golden.app import ALPHA, BETA, GAMMA, ORG, SESSIONS, GoldenDaemon
from tests.support.orgfs_fakes.app import join_test_org
from tests.daemon.cells.runtime.cell_runtime_support import authority_for

DAY_MS = 86_400_000
HOUR_MS = 3_600_000
NAMES = {ALPHA: "alpha", BETA: "beta", GAMMA: "gamma"}
OUTAGE_MODES = ("fs-raises", "space-unreachable")
CROWD = tuple(f"agent:op:node-9:peer{index}" for index in range(60))

__all__ = [
    "CellOrgFs",
    "DAY_MS",
    "HOUR_MS",
    "LogCapture",
    "OUTAGE_MODES",
    "OrgCellDocs",
    "Outage",
    "attach_cell_orgfs",
    "body_columns",
    "boot_like_services",
    "capture_logs",
    "pending_ids",
    "send",
    "session",
    "skip_compose_backoff",
]


def session(actor: str) -> dict[str, str]:
    """The authenticated session params of one golden agent."""

    return {"from": actor, "sessionRef": SESSIONS[NAMES[actor]]}


class CellOrgFs(FlakyOrgFs):
    """The app's own OrgFS: raises while ``down``; an unreachable runtime while ``gone``."""

    def __init__(self, fs: LocalOrgFs) -> None:
        super().__init__(fs)
        self.gone = False
        self.unreachable_calls = 0

    def runtime(self) -> SimpleNamespace:
        """``app._require_orgfs_runtime()``: refuses while the runtime is unreachable."""

        if self.gone:
            self.unreachable_calls += 1
            raise OrgFsError("unavailable", {"message": "orgfs runtime unreachable"})
        return SimpleNamespace(facade=self)


def attach_cell_orgfs(app: Any, monkeypatch: pytest.MonkeyPatch) -> CellOrgFs:
    """Make an isolated ``LocalOrgFs`` (behind ``CellOrgFs``) the app's OrgFS runtime."""

    fs = CellOrgFs(LocalOrgFs(author=OWNER_PRINCIPAL))
    monkeypatch.setattr(app, "_require_orgfs_runtime", fs.runtime, raising=False)
    return fs


class Outage:
    """Takes the link OrgFS and the app's own OrgFS down and up together.

    ``fs-raises``: every link-space read and write and every guarded
    ``LocalOrgFs`` call raises ``OrgFsError('unavailable')``.
    ``space-unreachable``: the link spaces disappear from the listing and
    ``app._require_orgfs_runtime()`` itself refuses.
    """

    def __init__(self, daemon: GoldenDaemon, cells: CellOrgFs, mode: str) -> None:
        if mode not in OUTAGE_MODES:
            raise ValueError(f"unknown outage mode {mode!r}")
        self._links = daemon.fs
        self._cells = cells
        self._mode = mode
        self._spaces = dict(daemon.fs._spaces)

    def down(self) -> None:
        if self._mode == "fs-raises":
            self._cells.down = True
            self._links.fail_reads.update(self._spaces)
            self._links.fail_writes.update(self._spaces)
            return
        self._cells.gone = True
        for space_id in self._spaces:
            self._links.drop_space(space_id)

    def up(self) -> None:
        self._cells.down = False
        self._cells.gone = False
        self._links.fail_reads.difference_update(self._spaces)
        self._links.fail_writes.difference_update(self._spaces)
        self._links._spaces.update(self._spaces)

    def refusals(self) -> int:
        """How often the app's own OrgFS refused while down."""

        return len(self._cells.refused) + self._cells.unreachable_calls


def boot_like_services(
    daemon: GoldenDaemon, monkeypatch: pytest.MonkeyPatch, *, crowd: bool = False
) -> tuple[GoldenDaemon, ConversationRecorder]:
    """Compose a golden daemon's retention source and conversation recorder."""

    del crowd
    app = daemon.app
    attach_cell_orgfs(app, monkeypatch)
    join_test_org(app, ORG)
    bind_body_retention_params(app)
    recorder = compose_conversation_recorder(app, start=False, clock_ms=daemon.clock.ms)
    assert recorder is not None
    return daemon, recorder


def skip_compose_backoff(app: Any) -> None:
    """Age the failed cell-runtime composition past ``COMPOSE_RETRY_S``."""

    failed_at = app._cell_runtime_failed_at
    assert failed_at is not None, "no cell-runtime composition failed"
    app._cell_runtime_failed_at = failed_at - (cell_wiring.COMPOSE_RETRY_S + 1.0)


class OrgCellDocs:
    """Cell documents written by hand into the org directory space on OrgFS.

    This is the owner's data path in phase A: a ``hyprial-cell/v1`` document
    at ``cells/<kind>/<c.id>.json`` in the ``group-<org>`` space, authored as
    the local OrgFS principal.  Nothing goes through ``CellRuntime``; the
    runtime only sees what its OrgFS scan reads back.
    """

    def __init__(self, app: Any, org: str = ORG) -> None:
        self.fs: LocalOrgFs = app._require_orgfs_runtime().facade.inner
        name = org_space_name(org)
        [space] = [info for info in self.fs.spaces() if info.name == name]
        self.space_id = str(space.space_id)
        self._cells: dict[str, Cell] = {}
        self._versions: dict[str, str] = {}
        self._now_ms = 1_000

    @staticmethod
    def path(cell: Cell) -> str:
        return f"cells/{cell.kind}/{cell.cell_id}.json"

    def write_agent(self, agent: Any, params: dict[str, Any], *, revision: int = 2) -> Cell:
        """Write an ``agent`` cell document directly to OrgFS before runtime composition."""

        authority = authority_for(self.fs.author)
        return self._write(
            Cell(
                cell_id=subject_cell_id("agent", str(agent.agent_id)),
                kind="agent",
                authority=authority,
                scope=CellScope.of_org(),
                binding=CellBinding.subject(str(agent.agent_id)),
                name=str(agent.actor),
                members=(CellMember(str(agent.uri), "harness"),),
                params=params,
                revision=revision,
                updated_at_ms=self._tick(),
            )
        )

    def agent(self, agent: Any, params: dict[str, Any]) -> Cell:
        """Edit the ``agent`` cell document the runtime registered for *agent*.

        The cell runtime registers every local agent's subject cell when it
        composes, so the owner's path is an edit of that stored document:
        the next revision with *params*, written over the version read.
        """

        path = f"cells/agent/{agent.cell_id}.json"
        content, version = self.fs.read_text(self.space_id, path)
        held = decode(content)
        assert held.binding == CellBinding.subject(str(agent.agent_id))
        assert held.members == (CellMember(str(agent.uri), "harness"),)
        self._cells[held.cell_id] = held
        self._versions[held.cell_id] = str(version)
        return self.set_params(held, params)

    def set_params(self, cell: Cell, params: dict[str, Any]) -> Cell:
        """Edit the stored document: the next revision with *params*."""

        held = self._cells[cell.cell_id]
        return self._write(held.revised(now_ms=self._tick(), params=params))

    def stored_params(self, cell: Cell) -> Any:
        """The ``params`` object of the document as it is stored on OrgFS."""

        text = self.fs.read_text_snapshot(self.space_id, self.path(cell)).content
        return json.loads(text)["params"]

    def _tick(self) -> int:
        self._now_ms += 1_000
        return self._now_ms

    def _write(self, cell: Cell) -> Cell:
        for directory in ("cells", f"cells/{cell.kind}"):
            try:
                self.fs.mkdir(self.space_id, directory)
            except OrgFsError:
                pass
        expect = self._versions.get(cell.cell_id)
        options: dict[str, Any] = (
            {"create_only": True} if expect is None else {"expect_version": expect}
        )
        node = self.fs.write_text(
            self.space_id, self.path(cell), encode(cell), **options
        )
        self._versions[cell.cell_id] = str(node.version)
        self._cells[cell.cell_id] = cell
        return cell


def send(
    app: Any, sender: str, to: str, key: str, *, text: str | None = None
) -> dict[str, Any]:
    """One ``message.send`` from *sender* (a golden agent or an operator) to *to*."""

    params: dict[str, Any] = session(sender) if sender in NAMES else {"from": sender}
    return app.handle(
        "message.send",
        {
            **params,
            "to": [to],
            "summary": f"summary {key}",
            "message": text if text is not None else f"body {key} ☃",
            "conversationId": f"conv-{key}",
            "idempotencyKey": f"key-{key}",
        },
    )


def pending_ids(app: Any, actor: str) -> list[str]:
    """The message ids ``message.pending.list`` shows for one golden agent."""

    listed = app.handle("message.pending.list", session(actor))
    return [str(item["messageId"]) for item in listed["messages"]]


def body_columns(app: Any, table: str = "inbox") -> dict[str, dict[str, Any]]:
    """Every row of *table* by message id: payload, drop and grant columns."""

    with closing(sqlite3.connect(app.state_dir / "inbox.sqlite3")) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT message_id, payload, body_dropped_at_ms, body_payload_sha256,"
            f" body_retention_ms FROM {table}"
        ).fetchall()
    return {
        str(row["message_id"]): {
            "payload": bytes(row["payload"]),
            "dropped": row["body_dropped_at_ms"] is not None,
            "granted": row["body_payload_sha256"] is not None,
            "retentionMs": row["body_retention_ms"],
        }
        for row in rows
    }


class LogCapture:
    """Every ``Logger.log`` record written while installed."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def named(self, event: str) -> list[dict[str, Any]]:
        return [record for record in self.records if record["event"] == event]


def capture_logs(monkeypatch: pytest.MonkeyPatch) -> LogCapture:
    """Record every daemon log line; install before the daemon is built."""

    capture = LogCapture()
    original = Logger.log

    def log(self: Logger, level: Any, event: str, **fields: Any) -> None:
        capture.records.append(
            {"level": level, "component": self.component, "event": event, **fields}
        )
        original(self, level, event, **fields)

    monkeypatch.setattr(Logger, "log", log)
    return capture
