# Decision record (file header; no inline comments by repository rule):
# - ``AgentTeleportService`` talks to its host only through the two Protocols
#   below, so the §4 state machine is tested with fakes and the daemon wiring
#   (``teleport/wiring/source.py``) is the only place that knows the registry,
#   the lifecycle primitives and the routine runtime.
# - ``SourceHost.describe`` raises ``LookupError`` for an unknown agent and
#   ``TeleportError`` for an agent it cannot describe; ``require_resumable``
#   raises ``TeleportError`` whose code the service reports unchanged.
# - ``quiesce`` returns the quiescence proof as a bool; ``restart`` is the
#   non-strict rollback that puts the stopped agent back; ``remove`` retires
#   the agent identity and its home on this node.
# - ``SourceRoutines`` is complete for slice 6 (export, pause, drain, remove);
#   slice 5 only reads ``executed_by`` and migrates nothing.
"""Ports and value types of the source side of an agent teleport."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class SourceAgent:
    """What the source node knows about the agent it is asked to move."""

    name: str
    uri: str
    harness: str
    headless: bool
    args: tuple[str, ...]
    session_ref: str | None
    cwd: Path | None
    home: Path | None
    adapter_bindings: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class ExportedStream:
    """The packed bundle: the tar to send and the digests the target checks."""

    path: Path
    total: int
    sha256: str
    manifest_digest: str


@dataclass(frozen=True, slots=True)
class RoutineRef:
    name: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class RoutineExport:
    name: str
    yaml: str
    enabled: bool


class SourceHost(Protocol):
    node_id: str
    owner: str

    def describe(self, agent: str) -> SourceAgent: ...

    def require_resumable(self, agent: SourceAgent) -> None: ...

    def quiesce(self, agent: SourceAgent) -> bool: ...

    def restart(self, agent: SourceAgent) -> None: ...

    def export_stream(
        self, agent: SourceAgent, *, recipient_public_key: bytes, workdir: Path
    ) -> ExportedStream: ...

    def remove(self, agent: SourceAgent) -> None: ...


class SourceRoutines(Protocol):
    def executed_by(self, agent_uri: str) -> list[RoutineRef]: ...

    def export(self, name: str) -> RoutineExport: ...

    def pause(self, name: str) -> None: ...

    def resume(self, name: str) -> None: ...

    def wait_drained(self, names: Sequence[str], timeout: float) -> list[str]: ...

    def remove(self, name: str) -> None: ...
