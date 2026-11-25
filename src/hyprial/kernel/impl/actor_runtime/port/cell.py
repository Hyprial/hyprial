# Decision record (file header; no inline comments by repository rule):
# - ActorCell is a thin identity plus the engine calls it needs. It adds no
#   queue and no thread: tell/ask/snapshot/stop/reset/drain all resolve through
#   the one engine that minted the handle, preserving the P2a/P2b mailbox.
# - A cell minted for a worker pool still exposes stop/snapshot/drain, while
#   tell returns CLOSED and ask raises ActorAdmissionError("closed"), matching
#   the native ABI's pool target rule (10).
from __future__ import annotations

from hyprial.kernel.impl.actor_runtime.contracts import (
    ActorHandle,
    ActorSnapshot,
    AdmissionResult,
    DrainReport,
)
from hyprial.kernel.impl.actor_runtime.port.contracts import ActorEngine


class ActorCell:
    """Business-facing handle for one logical actor on an engine."""

    __slots__ = ("_engine", "handle", "kind")

    def __init__(self, engine: ActorEngine, handle: ActorHandle, kind: str) -> None:
        self._engine = engine
        self.handle = handle
        self.kind = kind

    def tell(self, command: object) -> AdmissionResult:
        return self._engine.tell(self.handle, command)

    def ask(self, command: object, *, timeout: float = 5.0) -> object:
        return self._engine.ask(self.handle, command, timeout)

    def snapshot(self) -> ActorSnapshot:
        return self._engine.snapshot(self.handle)

    def stop(self, timeout: float = 1.0) -> bool:
        return self._engine.stop(self.handle, timeout)

    def reset(self) -> bool:
        return self._engine.reset(self.handle)

    def drain(self, timeout: float = 5.0) -> DrainReport:
        return self._engine.drain((self.handle,), timeout)
