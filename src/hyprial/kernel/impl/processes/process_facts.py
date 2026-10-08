"""Harness-neutral observations and process ownership contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

@dataclass(frozen=True, slots=True)
class ProcessLiveness:
    state: ProcessLivenessState
    observed: bool
    pid: int | None = None
    marker: str | None = None
    detail: str | None = None

class ProcessLivenessState(StrEnum):
    ALIVE = "alive"
    DEAD = "dead"
    UNKNOWN = "unknown"


class ProcessLivenessProbeError(RuntimeError):
    """A liveness probe failed; callers must surface, never default, it."""


@runtime_checkable
class ManagedHarnessProcess(Protocol):
    """A managed child with explicit running, identity and cleanup observations."""

    @property
    def running(self) -> bool: ...

    @property
    def pid(self) -> int | None:
        """OS process id, or None for a stopped, unknown or childless session."""
        ...

    def liveness(self) -> ProcessLiveness: ...

    def stop(self) -> None: ...
