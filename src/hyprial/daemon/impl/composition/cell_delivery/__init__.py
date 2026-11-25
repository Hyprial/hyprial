"""Composition of the headless cell delivery port (cell model I5-2 B/C).

Decision notes:

- ``compose_headless_cell_delivery`` is the wiring seam the runtime bridge
  receives: it composes the app's cell world once, at bridge assembly, and
  returns a ``HeadlessCellDelivery`` over the agent registry's worker→cell
  mapping and the cell actor registry's published snapshots.  The
  production runtime bridge is always wired with this port; the
  architecture gate in ``tests/tooling/architecture/delivery`` pins that
  wiring.
- Second review round (2026-11-20): the dispatch effect worker never
  composes anything.  ``cell_of_worker`` reads only published in-memory
  state — the app's already-attached registry and agent projection — and
  answers None while the cell world is missing, which the bridge reads as
  "leave the rows pending".  Recovery from a failed boot-time composition
  is the composition side's duty, not a per-offer retry.
- The worker→cell resolution re-reads the in-memory agent projection on
  every call, so an agent created after the bridge composes is admitted by
  its next tick without any bridge-side cache.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hyprial.daemon.impl.composition.cell_delivery.headless import HeadlessCellDelivery
from hyprial.daemon.impl.composition.cell_delivery.port import (
    CellDeliveryPort,
    CellTerminalRoute,
    InteractiveAdmission,
)

if TYPE_CHECKING:
    from hyprial.daemon.impl.cells.actor.snapshot import CellActorSnapshot


def compose_headless_cell_delivery(app: Any) -> CellDeliveryPort:
    from hyprial.daemon.impl.composition.cells import cell_runtime

    cell_runtime(app)

    def cell_of_worker(worker: str) -> str | None:
        registry = getattr(app, "_cell_actor_registry", None)
        if registry is None:
            return None
        agents = getattr(app, "agents", None)
        listing = getattr(agents, "list", None)
        if not callable(listing):
            return None
        try:
            records = tuple(listing())
        except Exception:  # noqa: BLE001 - a broken directory defers this worker only
            return None
        for record in records:
            if (
                str(getattr(record, "uri", "")) == worker
                or str(getattr(record, "actor", "")) == worker
            ):
                cell_id = getattr(record, "cell_id", None)
                return str(cell_id) if cell_id else None
        return None

    def snapshot_of_cell(cell_id: str) -> CellActorSnapshot | None:
        registry = getattr(app, "_cell_actor_registry", None)
        if registry is None:
            return None
        reader = getattr(registry, "snapshot", None)
        if not callable(reader):
            return None
        try:
            return reader(cell_id)  # type: ignore[no-any-return]
        except Exception:  # noqa: BLE001 - a failed read defers this cell only
            return None

    return HeadlessCellDelivery(cell_of_worker, snapshot_of_cell)


__all__ = [
    "CellDeliveryPort",
    "CellTerminalRoute",
    "HeadlessCellDelivery",
    "InteractiveAdmission",
    "compose_headless_cell_delivery",
]
