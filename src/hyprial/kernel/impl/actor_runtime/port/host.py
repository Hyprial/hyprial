# Decision record (file header; no inline comments by repository rule):
# - ActorHost is the composition-root object of prerequisites 2.2: it is the
#   business-facing ActorSpawner plus the engine-wide drain, shutdown, timer
#   view, and effect-lane factory. It owns no mailbox and no supervision
#   state; every call resolves through the one engine it was built with.
# - create_actor_host calls the module-level create_engine name (not a
#   captured reference) so P6 can replace the engine at this single seam and
#   tests can substitute it without touching host internals.
# - I2 keyed actors: spawn passes the spec's optional (group, key) through to
#   the engine, and find resolves a live pair to its cell or None. An occupied
#   pair is a spawn refusal. The host adds no key state of its own; the engine
#   owns the index.
from __future__ import annotations

from collections.abc import Callable

from hyprial.kernel.impl.actor_runtime.contracts import (
    ActorSpec,
    AdmissionResult,
    DrainReport,
)
from hyprial.kernel.impl.actor_runtime.effects import (
    EffectCompleted,
    EffectLane,
    OwnerGone,
)
from hyprial.kernel.impl.actor_runtime.port.cell import ActorCell
from hyprial.kernel.impl.actor_runtime.port.config import ActorEngineConfig
from hyprial.kernel.impl.actor_runtime.port.contracts import ActorPoolSpec
from hyprial.kernel.impl.actor_runtime.port.engine import (
    TimerService,
    create_engine,
)


class ActorHost:
    """Composition-root capability over one actor engine."""

    __slots__ = ("_engine", "_config")

    def __init__(self, engine: object, config: ActorEngineConfig) -> None:
        self._engine = engine
        self._config = config

    @property
    def engine(self) -> object:
        return self._engine

    def spawn(self, spec: ActorSpec) -> ActorCell:
        handle = self._engine.spawn(spec)
        return ActorCell(self._engine, handle, "serial")

    def find(self, group: str, key: str) -> ActorCell | None:
        """The live serial actor registered under (group, key), or None."""

        handle = self._engine.lookup(group, key)
        if handle is None:
            return None
        return ActorCell(self._engine, handle, "serial")

    def spawn_pool(self, spec: ActorPoolSpec) -> ActorCell:
        handle = self._engine.spawn_pool(spec)
        return ActorCell(self._engine, handle, "pool")

    def drain_all(self, timeout: float = 5.0) -> DrainReport:
        return self._engine.drain(None, timeout)

    def shutdown(self, timeout: float = 5.0) -> bool:
        return self._engine.shutdown(timeout)

    def timers(self) -> TimerService:
        return TimerService(self._engine)

    def effect_lane(
        self,
        *,
        name: str,
        execute: Callable[[object], object],
        complete: Callable[
            [EffectCompleted], AdmissionResult | OwnerGone
        ],
        capacity: int = 32,
        workers: int = 1,
        retry_seconds: float = 0.01,
        dead_letter_sink: Callable[[EffectCompleted, str], None] | None = None,
    ) -> EffectLane:
        return EffectLane(
            name=name,
            execute=execute,
            complete=complete,
            capacity=capacity,
            workers=workers,
            retry_seconds=retry_seconds,
            cpu_accounting=self._config.cpu_accounting,
            dead_letter_sink=dead_letter_sink,
        )


def create_actor_host(config: ActorEngineConfig | None = None) -> ActorHost:
    """Build one host over the engine factory; P5 supplies this to business."""

    selected = ActorEngineConfig() if config is None else config
    return ActorHost(create_engine(selected), selected)
