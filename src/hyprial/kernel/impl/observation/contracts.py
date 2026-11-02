"""Data-only lifecycle monitoring. No execution or business-custody authority.

Epochs are opaque diagnostic identities, not session credentials. A cursor is
local to one target and namespace. Observation uses zero wait in this version.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


LifecycleObservationState = Literal["unknown", "starting", "running", "stopping", "stopped", "down"]
ObservationStatus = Literal["ok", "overloaded", "closed"]


@dataclass(frozen=True, slots=True)
class LifecycleTarget:
    host_id: str
    group: str
    key: str

    def __post_init__(self) -> None:
        if not all(isinstance(part, str) and part.strip() for part in (
            self.host_id, self.group, self.key,
        )):
            raise ValueError("lifecycle target components must be nonempty strings")


@dataclass(frozen=True, slots=True)
class LifecycleCursor:
    epoch: str
    revision: int


@dataclass(frozen=True, slots=True)
class LifecycleSnapshot:
    target: LifecycleTarget
    epoch: str
    revision: int
    observed_at_ms: int
    state: LifecycleObservationState
    cause: str
    available: bool


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    target: LifecycleTarget
    epoch: str
    revision: int
    transition: LifecycleObservationState
    cause: str
    observed_at_ms: int


@dataclass(frozen=True, slots=True)
class ObservationBatch:
    events: tuple[LifecycleEvent, ...]
    cursor: LifecycleCursor
    resync_required: bool
    current: LifecycleSnapshot
    status: ObservationStatus = "ok"


@dataclass(frozen=True, slots=True)
class ObservationSubscription:
    status: ObservationStatus
    observer_id: str | None
    initial: ObservationBatch


class LifecycleObservationPort(Protocol):
    """Read-only lifecycle data; cancelling an observer never stops a resource.

    First observe (no cursor) returns current. Resync returns current and its
    cursor. For paginated replay the cursor points to the last returned event,
    which can precede current. Unsupported namespace/wait/limit raises ValueError.
    open is optional resource accounting for long-lived readers; stateless polls
    need no subscription. Unknown/cancelled observer IDs return status=closed.
    """

    def snapshot(self, target: LifecycleTarget, *, namespace: str = "lifecycle") -> LifecycleSnapshot: ...

    def observe(
        self, target: LifecycleTarget, *, namespace: str = "lifecycle",
        after_revision: int | None = None, epoch: str | None = None,
        limit: int = 128, wait_ms: int = 0, observer_id: str | None = None,
    ) -> ObservationBatch: ...

    def open(self, target: LifecycleTarget, *, namespace: str = "lifecycle") -> ObservationSubscription: ...

    def cancel(self, observer_id: str) -> None: ...
