"""The two ports the teleport target service drives, and what a landing returns.

Decisions:

- ``target/`` depends on these ``typing.Protocol`` types only; the registry,
  activation and lifecycle adapters live in ``teleport/wiring`` so the state
  machine can be tested with plain fakes.
- ``Landed.cwd`` is ``home / "workspace"`` for every real landing: the target
  never honours a cwd chosen by the source (protocol section 0).
- ``start`` performs the launch and strict resume together; it raises
  ``TELEPORT_STRICT_RESUME_FAILED`` when the session does not hold.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Landed:
    name: str
    uri: str
    entity_token: str
    home: Path
    cwd: Path
    harness: str


class TargetHost(Protocol):
    node_id: str
    owner: str

    def agent_exists(self, name: str) -> bool: ...

    def runtime_supported(self, harness: str) -> bool: ...

    def recipient_public_key(self) -> bytes: ...

    def land(self, bundle_dir: Path, *, name: str, harness: str) -> Landed: ...

    def undo_landing(self, landed: Landed) -> None: ...

    def activate(self, landed: Landed) -> None: ...

    def deactivate(self, landed: Landed) -> None: ...

    def start(
        self,
        landed: Landed,
        *,
        args: Sequence[str],
        session_ref: str | None,
        timeout: float,
    ) -> str | None: ...

    def stop(self, landed: Landed) -> None: ...


class TargetRoutines(Protocol):
    def existing(self, names: Sequence[str]) -> list[str]: ...

    def create_paused(self, *, yaml_text: str, source_uri: str, target_uri: str) -> str: ...

    def resume(self, name: str) -> None: ...

    def remove(self, name: str) -> None: ...
