"""Custody registry records and immutable authority messages."""

from __future__ import annotations

from dataclasses import dataclass

from hyprial.kernel import ProcessLivenessState


@dataclass(slots=True)
class _OrphanEntry:
    orphan_id: str
    harness_id: str
    pid: int | None
    marker: str | None
    observed: bool
    first_seen_ms: int
    owner_id: str | None
    orphan_since_ms: int | None = None
    last_probe_ms: int | None = None
    gc_attempts: int = 0
    state: str = ProcessLivenessState.UNKNOWN.value
    detail: str | None = None
    unknown_since_ms: int | None = None
    last_probe_error: str | None = None
    probe_failures: int = 0
    next_probe_ms: int | None = None


@dataclass(frozen=True, slots=True)
class ProcessCustodyCommand:
    operation_id: str
    generation: int
    kind: str
    harness_id: str = ""
    pid: int | None = None
    marker: str | None = None


@dataclass(frozen=True, slots=True)
class ProcessCustodyCompleted:
    operation_id: str
    generation: int
    orphan_id: str | None
    retired: int
    rows: tuple[tuple[tuple[str, object], ...], ...]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ProcessCustodyProjection:
    generation: int
    version: int
    pending: int
    rejected: int
    failed: int
    rows: tuple[tuple[tuple[str, object], ...], ...]
