"""Fake ports and a fake link for the slice 5 source-side teleport tests.

Interface contract pinned by the tests in this package (decisions on the
details docs/design/agent-teleport-protocol.md leaves open are marked D):

- Public surface: ``hyprial.daemon.impl.teleport.source`` exports
  ``AgentTeleportService``, ``TeleportLink``, ``TeleportRunError``,
  ``SourceHost``, ``SourceRoutines``, ``SourceAgent``, ``ExportedStream``,
  ``RoutineRef`` and ``RoutineExport``.  Tests import it lazily through
  ``source()`` so a missing module fails each test on its own.
- ``SourceAgent`` and ``ExportedStream`` are frozen dataclasses built by
  keyword with exactly the §5 fields.  ``RoutineRef`` has ``name`` and
  ``enabled`` (D).
- ``AgentTeleportService(host, routines, link, *, workdir_root, new_operation_id)``
  (D: ``workdir_root`` is the directory under which each run creates and
  always deletes its private export directory; ``new_operation_id`` is a
  zero-argument callable).  ``run(agent, *, to, dry_run=False)`` returns the
  ``teleport.run`` result object as a plain dict, or raises
  ``TeleportRunError``.
- ``TeleportRunError(code, message, data=None)`` subclasses ``TeleportError``
  and carries ``data`` (a JSON object or ``None``) (D).
- ``SourceHost.describe`` raises ``LookupError`` for an unknown agent; the
  service answers ``AGENT_NOT_FOUND`` (the existing ipc code) (D).
  ``require_resumable`` raises ``TeleportError`` whose code propagates (D).
- Preflight refusals for no home, not headless, unsupported harness, a cwd
  other than ``<home>/workspace`` and pinned adapters are all
  ``TELEPORT_RUNTIME_UNSUPPORTED``; ``to`` equal to ``host.node_id`` is
  ``TELEPORT_WRONG_TARGET``.  Supported harnesses: claude, codex, pi.
- The link surface the service consumes (D):
  ``prepare(target_node, PrepareRequest) -> PrepareReply``,
  ``send(target_node, operation_id, path) -> SendResult``,
  ``receive(target_node, ReceiveRequest) -> ReceiveReply``,
  ``commit(target_node, CommitRequest) -> CommitReply`` and
  ``abort(target_node, AbortRequest) -> bool`` (never raises).
  A verdict (``ok:false``) is a ``TeleportError`` with the remote code.  For
  receive, no reply or an unreadable reply is ``TELEPORT_UNRESOLVED``; the
  service also treats any non-``TeleportError`` exception from receive as
  unresolved, because the outcome on the target is unknown.
- ``TELEPORT_UNRESOLVED`` data keys are exactly ``operationId``,
  ``targetNode``, ``agent``, ``sourceAgentUri``, ``bytesSent``,
  ``sourceState`` (``"stopped"``), ``pausedRoutines`` and ``manualCheck``
  (D: the name of the §4 manual-check hint).  ``TELEPORT_SOURCE_CLEANUP_FAILED``
  data keys are exactly ``operationId``, ``agent``, ``targetNode``,
  ``targetAgentUri`` and ``pausedRoutines`` (D).
- ``teleport.run`` declaration (D, chosen from existing destructive,
  non-retryable writes such as ``agent.rename``/``org.*`` removals):
  owner ``daemon.teleport``, ``mutates=True``, ``retry="never"``,
  ``confirmation="none"`` (``preview``/``execute`` require two methods and
  an operationId-only execute, which contradicts one method for dry-run and
  run), no preview, unary, audience ``("local-operator",)``.  D:
  ``http_exposed=False`` for now: the GUI backend is still an HTTP 501
  placeholder, so HTTP exposure, examples and the generated GUI client are
  added when the GUI is wired.  The spec, its handler ``teleport_run`` and
  ``TELEPORT_BINDINGS`` live in ``hyprial.daemon.impl.request_catalog.teleport``
  (that directory is full); the handler obtains the service through the
  module-level name ``source_service(app)`` imported from
  ``hyprial.daemon.impl.teleport.wiring.source``.  ``teleport.run`` is
  recorded in ``MIGRATED_METHODS``.
- Declared errors are exactly the 20 frozen ``TELEPORT_*`` codes plus
  ``INVALID_ARGUMENT`` and ``AGENT_NOT_FOUND``.
- CLI ``hyprial agent teleport AGENT --to HOST [--dry-run | --yes] [--json]``
  sends ``teleport.run`` with ``{"agent", "to", "dryRun"}`` (``dryRun``
  always present) and a request timeout of at least 335 s (D: the §2 fixed
  budgets 10 + 3*15 + (60+60) + 2*15 + 10 plus the 120 s routine drain).
  Without ``--yes`` and without ``--dry-run``: under ``--json`` or a
  non-terminal stdin it fails ``CONFIRMATION_REQUIRED`` without a request;
  interactively it asks on stderr and a refusal is ``CANCELLED`` (D, mirrors
  ``agent destroy``).  ``--dry-run`` with ``--yes`` is ``INVALID_ARGUMENT``.
  The command module is ``hyprial.shell.impl.cli.commands.agent.teleport``
  (D: the ``agent`` command directory already holds seven files).
- Outside ``hyprial.daemon.impl.teleport`` only
  ``hyprial/daemon/impl/request_catalog/teleport/`` may import the package
  (source side); shell and GUI code never do.
"""

from __future__ import annotations

import importlib
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

from hyprial.daemon.impl.teleport import SendResult, TeleportError
from hyprial.daemon.impl.teleport.protocol import (
    AbortRequest,
    CommitReply,
    CommitRequest,
    PrepareReply,
    PrepareRequest,
    ReceiveReply,
    ReceiveRequest,
)

SOURCE = "hyprial.daemon.impl.teleport.source"
SOURCE_NODE = "node-a"
TARGET_NODE = "node-b"
OWNER = "owner-1"
AGENT = "w1"
OPERATION_ID = "op-5"
SOURCE_URI = "agent://owner-1/node-a/w1"
TARGET_URI = "agent://owner-1/node-b/w1"
PUBLIC_KEY = bytes(range(32))
MANIFEST_DIGEST = "ab" * 32
STREAM_SHA256 = "cd" * 32
STREAM_TOTAL = 4096
SESSION_REF = "sess-1"
ARGS = ("--model", "m1")
UNRESOLVED_KEYS = frozenset(
    {
        "operationId",
        "targetNode",
        "agent",
        "sourceAgentUri",
        "bytesSent",
        "sourceState",
        "pausedRoutines",
        "manualCheck",
    }
)
CLEANUP_KEYS = frozenset(
    {"operationId", "agent", "targetNode", "targetAgentUri", "pausedRoutines"}
)


def source() -> ModuleType:
    """Import the slice 5 source package on demand."""

    return importlib.import_module(SOURCE)


def make_agent(home: Path | None, **overrides: Any) -> Any:
    values: dict[str, Any] = {
        "name": AGENT,
        "uri": SOURCE_URI,
        "harness": "claude",
        "headless": True,
        "args": ARGS,
        "session_ref": SESSION_REF,
        "cwd": None if home is None else home / "workspace",
        "home": home,
        "adapter_bindings": (),
    }
    values.update(overrides)
    return source().SourceAgent(**values)


@dataclass
class FakeHost:
    events: list[tuple]
    agents: dict[str, Any]
    node_id: str = SOURCE_NODE
    owner: str = OWNER
    quiesced: bool = True
    failures: dict[str, BaseException] = field(default_factory=dict)
    workdirs: list[Path] = field(default_factory=list)

    def _maybe_fail(self, step: str) -> None:
        if step in self.failures:
            raise self.failures[step]

    def describe(self, agent: str) -> Any:
        self.events.append(("describe", agent))
        if agent not in self.agents:
            raise LookupError(agent)
        return self.agents[agent]

    def require_resumable(self, agent: Any) -> None:
        self.events.append(("require_resumable", agent.name))
        self._maybe_fail("require_resumable")

    def quiesce(self, agent: Any) -> bool:
        self.events.append(("quiesce", agent.name))
        self._maybe_fail("quiesce")
        return self.quiesced

    def restart(self, agent: Any) -> None:
        self.events.append(("restart", agent.name))
        self._maybe_fail("restart")

    def export_stream(
        self, agent: Any, *, recipient_public_key: bytes, workdir: Path
    ) -> Any:
        self.events.append(("export", agent.name, recipient_public_key, workdir))
        self.workdirs.append(Path(workdir))
        stream = Path(workdir) / "bundle.tar"
        stream.write_bytes(b"\0" * STREAM_TOTAL)
        self._maybe_fail("export")
        return source().ExportedStream(
            path=stream,
            total=STREAM_TOTAL,
            sha256=STREAM_SHA256,
            manifest_digest=MANIFEST_DIGEST,
        )

    def remove(self, agent: Any) -> None:
        self.events.append(("remove", agent.name))
        self._maybe_fail("remove")


@dataclass
class FakeRoutines:
    calls: list[tuple] = field(default_factory=list)

    def executed_by(self, agent_uri: str) -> list:
        self.calls.append(("executed_by", agent_uri))
        return []

    def export(self, name: str) -> Any:
        self.calls.append(("export", name))
        raise AssertionError("slice 5 migrates no routine")

    def pause(self, name: str) -> None:
        self.calls.append(("pause", name))

    def resume(self, name: str) -> None:
        self.calls.append(("resume", name))

    def wait_drained(self, names, timeout: float) -> list[str]:
        self.calls.append(("wait_drained", tuple(names), timeout))
        return []

    def remove(self, name: str) -> None:
        self.calls.append(("remove", name))


@dataclass
class FakeLink:
    events: list[tuple]
    failures: dict[str, BaseException] = field(default_factory=dict)
    abort_ok: bool = True
    gate: Callable[[PrepareRequest], None] | None = None

    def _maybe_fail(self, step: str) -> None:
        if step in self.failures:
            raise self.failures[step]

    def prepare(self, target_node: str, request: PrepareRequest) -> PrepareReply:
        self.events.append(("prepare", target_node, request))
        if self.gate is not None:
            self.gate(request)
        self._maybe_fail("prepare")
        return PrepareReply(
            operation_id=request.operation_id,
            target_node=target_node,
            recipient_public_key=PUBLIC_KEY,
            dry_run=request.dry_run,
        )

    def send(self, target_node: str, operation_id: str, path) -> SendResult:
        self.events.append(("send", target_node, operation_id, Path(path)))
        self._maybe_fail("send")
        return SendResult(
            operation_id=operation_id,
            total=STREAM_TOTAL,
            stream_sha256=STREAM_SHA256,
            persisted=STREAM_TOTAL,
        )

    def receive(self, target_node: str, request: ReceiveRequest) -> ReceiveReply:
        self.events.append(("receive", target_node, request))
        self._maybe_fail("receive")
        return ReceiveReply(
            operation_id=request.operation_id,
            agent_uri=TARGET_URI,
            session_ref=request.session_ref,
            routines=(),
        )

    def commit(self, target_node: str, request: CommitRequest) -> CommitReply:
        self.events.append(("commit", target_node, request))
        self._maybe_fail("commit")
        return CommitReply(operation_id=request.operation_id, routines=())

    def abort(self, target_node: str, request: AbortRequest) -> bool:
        self.events.append(("abort", target_node, request))
        return self.abort_ok


@dataclass
class Rig:
    """One service wired to fakes that share a single event log."""

    events: list[tuple]
    host: FakeHost
    routines: FakeRoutines
    link: FakeLink
    workdir_root: Path
    service: Any

    def names(self) -> list[str]:
        return [event[0] for event in self.events]

    def link_ops(self) -> list[str]:
        ops = {"prepare", "send", "receive", "commit", "abort"}
        return [event[0] for event in self.events if event[0] in ops]

    def run(self, agent: str = AGENT, *, to: str = TARGET_NODE, dry_run: bool = False):
        return self.service.run(agent, to=to, dry_run=dry_run)

    def leftovers(self) -> list[Path]:
        if not self.workdir_root.exists():
            return []
        return sorted(self.workdir_root.iterdir())


def rig(tmp_path: Path, *, agent: Any = None, ids: Callable[[], str] | None = None) -> Rig:
    events: list[tuple] = []
    home = tmp_path / "agents" / AGENT
    (home / "workspace").mkdir(parents=True, exist_ok=True)
    described = make_agent(home) if agent is None else agent
    host = FakeHost(events=events, agents={described.name: described})
    routines = FakeRoutines()
    link = FakeLink(events=events)
    workdir_root = tmp_path / "teleport-work"
    workdir_root.mkdir()
    service = source().AgentTeleportService(
        host,
        routines,
        link,
        workdir_root=workdir_root,
        new_operation_id=ids or (lambda: OPERATION_ID),
    )
    return Rig(events, host, routines, link, workdir_root, service)


def expect_failure(call: Callable[[], Any]) -> Any:
    """Run ``call`` and return the ``TeleportRunError`` it must raise."""

    module = source()
    try:
        call()
    except module.TeleportRunError as error:
        assert isinstance(error, TeleportError)
        return error
    raise AssertionError("expected TeleportRunError")


class Gate:
    """Blocks the first prepare of one agent until released."""

    def __init__(self, agent: str) -> None:
        self.agent = agent
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, request: PrepareRequest) -> None:
        if request.agent == self.agent and not self.entered.is_set():
            self.entered.set()
            assert self.release.wait(10)
