"""Frozen message dataclasses for the prepare / receive / commit / abort ops.

Decisions:

- Attributes are snake_case and sequences are tuples, so a decoded message
  compares equal to the one that was encoded.
- Defaults here mirror the wire defaults (``dryRun`` false, ``args`` empty,
  ``strictTimeoutSeconds`` 60) so callers and the decoder agree.
"""

from __future__ import annotations

from dataclasses import dataclass

from hyprial.kernel import TELEPORT_BUDGET

DEFAULT_STRICT_TIMEOUT_SECONDS = TELEPORT_BUDGET.default_strict_seconds


@dataclass(frozen=True, slots=True)
class RoutinePayload:
    name: str
    yaml: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class RoutineRestore:
    name: str
    restore_enabled: bool


@dataclass(frozen=True, slots=True)
class RoutineOutcome:
    name: str
    enabled: bool
    resumed: bool


@dataclass(frozen=True, slots=True)
class PrepareRequest:
    operation_id: str
    source_node: str
    target_node: str
    owner: str
    agent: str
    source_agent_uri: str
    harness: str
    routines: tuple[str, ...]
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class PrepareReply:
    operation_id: str
    target_node: str
    recipient_public_key: bytes
    dry_run: bool


@dataclass(frozen=True, slots=True)
class ReceiveRequest:
    operation_id: str
    manifest_digest: str
    stream_sha256: str
    harness: str
    session_ref: str | None
    routines: tuple[RoutinePayload, ...]
    args: tuple[str, ...] = ()
    strict_timeout_seconds: float = DEFAULT_STRICT_TIMEOUT_SECONDS


@dataclass(frozen=True, slots=True)
class ReceiveReply:
    operation_id: str
    agent_uri: str
    session_ref: str | None
    routines: tuple[RoutineRestore, ...]


@dataclass(frozen=True, slots=True)
class CommitRequest:
    operation_id: str


@dataclass(frozen=True, slots=True)
class CommitReply:
    operation_id: str
    routines: tuple[RoutineOutcome, ...]


@dataclass(frozen=True, slots=True)
class AbortRequest:
    operation_id: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class AbortReply:
    operation_id: str
