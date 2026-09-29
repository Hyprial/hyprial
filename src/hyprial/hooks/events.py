"""The neutral hook event envelope and its canonical names.

The daemon emits facts here; it never encodes policy.  Sources stamp their own
``seq`` (per-source FIFO); the bus neither re-stamps nor orders across sources.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

HOOK_EVENT_SCHEMA_VERSION = 1

#: The stable name contract other lines align to (design §3.2).
EVENT_NAMES: frozenset[str] = frozenset(
    {
        "actor.up",
        "actor.down",
        "actor.lost",
        "actor.restored",
        "actor.unowned",
        "turn.started",
        "turn.completed",
        "turn.failed",
        "delivery.prepared",
        "delivery.dispatched",
        "delivery.settled",
        "delivery.held",
        "graph.finished",
        "graph.closed",
        "worker.cleanup.complete",
    }
)

#: The only event an intercept hook may bind this release (design §4.2).
INTERCEPT_EVENT_NAMES: frozenset[str] = frozenset({"delivery.prepared"})

HookKind = Literal["observe", "intercept"]


@dataclass(frozen=True, slots=True)
class HookEvent:
    event: str
    kind: HookKind
    emitted_at_ms: int
    seq: int
    source: str
    actor: str | None = None
    graph_id: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = HOOK_EVENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != HOOK_EVENT_SCHEMA_VERSION:
            raise ValueError(f"unsupported hook event schema {self.schema_version!r}")
        if self.event not in EVENT_NAMES:
            raise ValueError(f"unknown hook event {self.event!r}")
        if self.kind not in ("observe", "intercept"):
            raise ValueError(f"unknown hook kind {self.kind!r}")
        if self.kind == "intercept" and self.event not in INTERCEPT_EVENT_NAMES:
            raise ValueError(f"{self.event!r} cannot be intercepted")
        if not self.source.strip():
            raise ValueError("hook event source must not be blank")
        if self.seq < 0 or self.emitted_at_ms < 0:
            raise ValueError("hook event seq and emitted_at_ms must not be negative")
        # Observers on other threads read the payload; freeze a private copy.
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


__all__ = [
    "EVENT_NAMES",
    "HOOK_EVENT_SCHEMA_VERSION",
    "INTERCEPT_EVENT_NAMES",
    "HookEvent",
    "HookKind",
]
