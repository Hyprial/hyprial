"""Immutable cell attachment diagnostics, never agent or process authority."""

from dataclasses import dataclass
from typing import Literal

from hyprial.kernel import LifecycleSnapshot, LifecycleTarget

BaseRunState = Literal["unknown", "starting", "idle", "in-turn", "stopping", "not-running"]
RunState = Literal[BaseRunState, "blocked", "suspected-stuck"]
RunCertainty = Literal["observed", "inferred", "unknown"]
RunScope = Literal["whole-session", "carrier-deliveries"]
ObservationHealth = Literal["current", "stale", "unavailable", "partial", "resync-required"]


@dataclass(frozen=True, slots=True)
class CellSnapshot:
    actor: str
    diagnostic_incarnation: str
    binding_revision: int | None
    target: LifecycleTarget
    custody: Literal["managed", "external", "none"]
    lifecycle: LifecycleSnapshot


@dataclass(frozen=True, slots=True)
class RunFacts:
    """Data-only, ephemeral evidence from an existing serialized owner.

    Monotonic values belong to this daemon epoch; they are never persisted or
    restored. No message text, native handle, credential or authority crosses
    this diagnostic port. Missing coverage does not mean an empty run set.
    """

    source_epoch: str = "unavailable"
    known: bool = False
    running: bool = False
    contact_current: bool = True
    conflict: bool = False
    resync_required: bool = False
    scope: RunScope = "whole-session"
    open_count: int = 0
    since_ms: int | None = None
    since_monotonic_ms: int | None = None
    exit_verified: bool = False
    process_exit_supported: bool = False
    operation: Literal["starting", "stopping"] | None = None
    provider_block: bool = False
    permission_supported: bool = False
    waits: tuple[tuple[str, int | None], ...] = ()
    activity_supported: bool = False
    quiet: bool = False
    quiet_after_ms: int | None = None
    last_activity_at_ms: int | None = None
