"""Safe access to the process-wide cell actor snapshot store."""

from __future__ import annotations

from typing import Any

class CellActorMount:
    """A read-only adapter that isolates one cell actor read from its peers."""

    def __init__(self, registry: Any) -> None:
        self._registry = registry

    def snapshot(self, cell_id: str) -> Any | None:
        try:
            snapshot = self._registry.snapshot(cell_id)
        except Exception:
            return None
        return snapshot


CellStateMount = CellActorMount


def __getattr__(name: str) -> Any:
    if name == "CellObservationView":
        from .wiring import CellObservationView

        return CellObservationView
    raise AttributeError(name)


__all__ = ["CellActorMount", "CellStateMount"]
