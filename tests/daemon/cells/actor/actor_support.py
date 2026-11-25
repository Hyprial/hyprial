"""Shared fixtures for the cell actor tests (I5-1).

Every test here runs against its own actor host and its own cell documents;
nothing touches a production home, daemon or OrgFS tree.  The documents are
built directly as ``Cell`` values the codec would accept, so the interpreter
under test sees exactly what the runtime would tell it.
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.cells.model import Cell, CellBinding, CellMember, CellScope
from hyprial.kernel import ActorHost, create_actor_host, subject_cell_id
from tests.daemon.cells.runtime.cell_runtime_support import ALICE


class Clock:
    """A step clock: every read advances by one, so no-op means no bump."""

    def __init__(self) -> None:
        self.now = 1_000

    def __call__(self) -> int:
        self.now += 1
        return self.now


class Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, level: str, _domain: str, event: str, **fields: Any) -> None:
        self.events.append((level, event, fields))

    def names(self) -> list[str]:
        return [event for _, event, _ in self.events]


def host(config: Any | None = None) -> ActorHost:
    return create_actor_host(config)


def wait_until(predicate: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(0.005)
    raise AssertionError("condition was not reached before timeout")


def agent_id(name: str = "worker") -> str:
    return f"a.{uuid.uuid4().hex[:24]}{name[:1]}"


def agent_cell(
    binding_subject: str,
    *,
    harness: tuple[str, ...] = ("agent:alice:mac:a",),
    params: dict[str, Any] | None = None,
    revision: int = 1,
    retired: bool = False,
    updated_at_ms: int = 1_000,
) -> Cell:
    """One agent-kind ``Cell`` like the registrar would ensure it."""

    return Cell(
        cell_id=subject_cell_id("agent", binding_subject),
        kind="agent",
        authority=ALICE,
        scope=CellScope.of_org(),
        binding=CellBinding.subject(binding_subject),
        name=binding_subject,
        members=tuple(CellMember(ref, "harness") for ref in harness),
        params=params or {},
        revision=revision,
        retired=retired,
        updated_at_ms=updated_at_ms,
    )


def tmp_cache_path(tmp_path: Path) -> Path:
    return tmp_path / "cells.sqlite3"


class BlockCommand:
    """A controlled test command: its handler parks until released."""

    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        self.entered = entered
        self.release = release


class BlockingExtension:
    """The controlled test handler of plan §F: parks on ``BlockCommand``."""

    def __init__(self) -> None:
        self.commands: list[object] = []

    def handles(self, command: object) -> bool:
        return isinstance(command, (BlockCommand,))

    def handle(self, access: object, command: object) -> None:
        self.commands.append(command)
        assert isinstance(command, BlockCommand)
        command.entered.set()
        assert command.release.wait(10.0)


class PoisonExtension:
    """Raises on its command; the supervised restart is the tested effect."""

    def handles(self, command: object) -> bool:
        return isinstance(command, str) and command == "poison"

    def handle(self, access: object, command: object) -> None:
        raise RuntimeError("injected cell actor fault")


class RecordingExtension:
    """Records the access facts each call saw (the 后装 seam proof)."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, int, str]] = []

    def handles(self, command: object) -> bool:
        return isinstance(command, str) and command == "probe"

    def handle(self, access: Any, command: object) -> None:
        self.seen.append((access.epoch, access.effective_revision, str(access.state)))
