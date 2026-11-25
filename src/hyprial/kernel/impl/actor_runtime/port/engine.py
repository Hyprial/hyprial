# Decision record (file header; no inline comments by repository rule):
# - P8 removed the shipped Python engine together with the ActorRuntime
#   facade. The port now has one adapter, AbiEngine, over a reference or
#   native ABI v1 module; TimerService remains the host view of the engine's
#   D3 timer capability and delegates every call to that engine.
# - Backend selection stays explicit and uncached: an unset variable means
#   native, ``reference-abi`` loads the packaged module, and ``native`` loads
#   the compiled extension and fails closed when it is absent or foreign.
#   The pykka backend and its ``pykka`` choice are gone; no value falls back.
from __future__ import annotations

from hyprial.kernel.impl.actor_runtime.contracts import ActorHandle
from hyprial.kernel.impl.actor_runtime.port.config import (
    ActorEngineConfig,
    selected_actor_backend,
)
from hyprial.kernel.impl.actor_runtime.port.contracts import ActorEngine


class TimerService:
    """Host-view of the engine's D3 timer capability."""

    __slots__ = ("_engine",)

    def __init__(self, engine: ActorEngine) -> None:
        self._engine = engine

    def send_after(
        self, handle: ActorHandle, delay: float, command: object
    ) -> str:
        return self._engine.send_after(handle, delay, command)

    def cancel_after(self, timer_id: str) -> bool:
        return self._engine.cancel_after(timer_id)


def create_engine(config: ActorEngineConfig | None = None) -> ActorEngine:
    """Construct the engine selected by HYPRIAL_ACTOR_BACKEND.

    The default is native; ``reference-abi`` loads the packaged pure-Python
    ABI module; ``native`` loads the compiled extension and fails closed when
    it is absent or foreign. Selection is never cached, so tests and callers
    can change the environment between constructions.
    """

    backend = selected_actor_backend()
    from hyprial.kernel.impl.actor_runtime.backends.abi import AbiEngine

    if backend == "reference-abi":
        from hyprial.kernel.impl.actor_runtime.backends import reference_abi

        return AbiEngine(reference_abi, config)
    from hyprial.kernel.impl.actor_runtime.backends.native import load_native

    return AbiEngine(load_native(), config)
