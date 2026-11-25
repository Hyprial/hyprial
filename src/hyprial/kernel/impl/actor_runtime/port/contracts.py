# Decision record (file header; no inline comments by repository rule):
# - ActorEngine is the frozen eight-method port of prerequisites 2.3. Pool
#   custody and timers are D3 capabilities on the concrete engine; they do not
#   widen the business-facing ActorSpawner/ActorCell contract.
# - I2 adds lookup(group, key): the keyed-actor counterpart of spawn, resolving
#   the live actor under a stable key or None. It is the ninth method; the
#   other eight keep their frozen shapes.
# - ActorPoolSpec is the worker-pool construction form. It carries the same
#   event and dead-letter sinks as ActorSpec and a one-to-one handler factory:
#   the engine calls it once per worker, as the native ABI does.
from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from hyprial.kernel.impl.actor_runtime.contracts import (
    ActorEvent,
    ActorHandle,
    ActorSnapshot,
    ActorSpec,
    AdmissionResult,
    CommandHandler,
    DrainReport,
)

if TYPE_CHECKING:
    from hyprial.kernel.impl.actor_runtime.port.cell import ActorCell


class ActorSpawnRefused(RuntimeError):
    """Spawn was refused because the engine is draining, drained, or shut down."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = reason


@runtime_checkable
class ActorEngine(Protocol):
    """Engine port held only by the composition root and the engine region."""

    def spawn(self, spec: ActorSpec) -> ActorHandle: ...

    def lookup(self, group: str, key: str) -> ActorHandle | None: ...

    def tell(self, handle: ActorHandle, command: object) -> AdmissionResult: ...

    def ask(self, handle: ActorHandle, command: object, timeout: float) -> object: ...

    def snapshot(self, handle: ActorHandle) -> ActorSnapshot: ...

    def stop(self, handle: ActorHandle, timeout: float) -> bool: ...

    def reset(self, handle: ActorHandle) -> bool: ...

    def drain(
        self, handles: Sequence[ActorHandle] | None, timeout: float
    ) -> DrainReport: ...

    def shutdown(self, timeout: float) -> bool: ...


@dataclass(frozen=True, slots=True)
class ActorPoolSpec:
    """One logical actor executed by up to ``pool_size`` workers."""

    name: str
    handler_factory: Callable[[], CommandHandler]
    pool_size: int = 1
    capacity: int = 32
    supervision_profile: str = "state_authority"
    undelivered_sink: Callable[[object, str], None] | None = None
    event_sink: Callable[[ActorEvent], None] | None = None
    retry_interval: float = 0.01

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("actor name must not be blank")
        if (
            not isinstance(self.pool_size, int)
            or isinstance(self.pool_size, bool)
            or self.pool_size < 1
        ):
            raise ValueError("pool_size must be at least 1")
        if (
            not isinstance(self.capacity, int)
            or isinstance(self.capacity, bool)
            or self.capacity < 1
        ):
            raise ValueError("capacity must be at least 1")
        if (
            not isinstance(self.retry_interval, (int, float))
            or isinstance(self.retry_interval, bool)
            or not self.retry_interval > 0
            or math.isinf(float(self.retry_interval))
        ):
            raise ValueError("retry_interval must be positive and finite")


@runtime_checkable
class ActorSpawner(Protocol):
    """Business-facing construction seam; composition roots provide it."""

    def spawn(self, spec: ActorSpec) -> "ActorCell": ...
