"""Versioned contracts for one read-only agent context snapshot."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

DEFAULT_SESSION_READ_LIMIT_BYTES = 1024 * 1024
DEFAULT_INJECTION_TOKEN_LIMIT = 4096
HANDOFF_SCHEMA_VERSION = 1

Coverage = Literal["complete-within-cutoff", "partial", "unavailable"]
_SAFE_SWITCH_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ContextSnapshotError(RuntimeError):
    """A snapshot source or publication violated a fail-closed contract."""


def _required(value: str, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class ContextSnapshotSource:
    harness: str
    generation: str
    session_ref: str
    cwd: str

    def __post_init__(self) -> None:
        if self.harness not in {"claude", "pi", "codex"}:
            raise ValueError("snapshot source harness must be claude, pi, or codex")
        _required(self.generation, "source generation")
        _required(self.session_ref, "source session_ref")
        _required(self.cwd, "source cwd")


@dataclass(frozen=True, slots=True)
class ContextSnapshotTarget:
    harness: str
    provider: str | None = None
    model: str | None = None

    def __post_init__(self) -> None:
        _required(self.harness, "target harness")
        for value, label in ((self.provider, "target provider"), (self.model, "target model")):
            if value is not None:
                _required(value, label)


@dataclass(frozen=True, slots=True)
class RecapCaptureState:
    """Facts supplied by the bounded recap writer at the snapshot cutoff."""

    enabled: bool
    dropped_records: int = 0

    def __post_init__(self) -> None:
        if self.dropped_records < 0:
            raise ValueError("dropped recap count must not be negative")


@dataclass(frozen=True, slots=True)
class ContextSnapshotBody:
    objective: str
    verified_facts: tuple[str, ...] = ()
    model_inferences: tuple[str, ...] = ()
    incomplete: tuple[str, ...] = ()
    next_steps: tuple[str, ...] = ()
    files_and_cwd: tuple[str, ...] = ()
    pending_handles: tuple[str, ...] = ()
    unknown_or_irreversible: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _required(self.objective, "handoff objective")
        for field_name in (
            "verified_facts",
            "model_inferences",
            "incomplete",
            "next_steps",
            "files_and_cwd",
            "pending_handles",
            "unknown_or_irreversible",
        ):
            values = getattr(self, field_name)
            if any(not isinstance(value, str) or not value for value in values):
                raise ValueError(f"{field_name} must contain non-empty strings")


@dataclass(frozen=True, slots=True)
class ContextSnapshotRequest:
    """Frozen source locator and evidence cutoff captured before source stop."""

    switch_id: str
    actor: str
    expected_entity_token: str
    source: ContextSnapshotSource
    target: ContextSnapshotTarget
    last_settled_delivery_id: str | None
    cutoff_at_ms: int
    recap: RecapCaptureState
    body: ContextSnapshotBody
    session_read_limit_bytes: int = DEFAULT_SESSION_READ_LIMIT_BYTES
    injection_token_limit: int = DEFAULT_INJECTION_TOKEN_LIMIT

    def __post_init__(self) -> None:
        if not _SAFE_SWITCH_ID.fullmatch(self.switch_id):
            raise ValueError("switch_id must be a safe single path component")
        _required(self.actor, "actor")
        _required(self.expected_entity_token, "expected entity token")
        if self.last_settled_delivery_id is not None:
            _required(self.last_settled_delivery_id, "last settled delivery id")
        if self.cutoff_at_ms < 0:
            raise ValueError("cutoff timestamp must not be negative")
        if self.session_read_limit_bytes < 1:
            raise ValueError("session read byte limit must be positive")
        if self.injection_token_limit < 1:
            raise ValueError("injection token limit must be positive")

    def with_switch_id(self, switch_id: str) -> "ContextSnapshotRequest":
        return replace(self, switch_id=switch_id)


@dataclass(frozen=True, slots=True)
class ContextSnapshotResult:
    path: Path
    coverage: Coverage
    manifest_digest: str


@dataclass(frozen=True, slots=True)
class SnapshotGap:
    code: str
    detail: str
    ref: str | None = None

    def as_json(self) -> dict[str, str]:
        value = {"code": self.code, "detail": self.detail}
        if self.ref is not None:
            value["ref"] = self.ref
        return value


@dataclass(frozen=True, slots=True)
class SnapshotSourceEvidence:
    kind: str
    ref: str
    digest: str
    captured_bytes: int
    source_bytes: int

    def as_json(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "ref": self.ref,
            "digest": self.digest,
            "capturedBytes": self.captured_bytes,
            "sourceBytes": self.source_bytes,
        }


@dataclass(frozen=True, slots=True)
class SourceAnalysis:
    coverage: Coverage
    sources: tuple[SnapshotSourceEvidence, ...] = ()
    gaps: tuple[SnapshotGap, ...] = ()
    excerpts: tuple[str, ...] = ()


__all__ = [
    "DEFAULT_INJECTION_TOKEN_LIMIT",
    "DEFAULT_SESSION_READ_LIMIT_BYTES",
    "HANDOFF_SCHEMA_VERSION",
    "ContextSnapshotBody",
    "ContextSnapshotError",
    "ContextSnapshotRequest",
    "ContextSnapshotResult",
    "ContextSnapshotSource",
    "ContextSnapshotTarget",
    "Coverage",
    "RecapCaptureState",
    "SnapshotGap",
    "SnapshotSourceEvidence",
    "SourceAnalysis",
]
