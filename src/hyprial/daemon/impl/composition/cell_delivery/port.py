"""The read-only headless cell delivery port the bridge consumes (M′ M2/M4).

Decision notes (cell model I5-2 parts B/C):

- The bridge never imports the cell actor registry.  It asks this port for
  one admitted envelope per captured delivery; the implementation resolves
  the worker's cell, reads the published snapshot and applies
  ``headless_admission``.  A ``None`` answer means "no offer this round":
  the row stays pending, nothing is fetched, refused or acknowledged.
- The snapshot read inside ``headless_envelope`` is the linearization point
  of that work decision: later document or health changes belong to the
  next call, never to an envelope already returned.
- ``steer_envelope`` never reads a snapshot.  It reuses the stamp of the
  turn that is already in flight, so a steer keeps the original member even
  after a revision changed the entry (M′ M6).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from hyprial.daemon.impl.api import HarnessDelivery

if TYPE_CHECKING:
    from hyprial.daemon.impl.cells.actor.contracts import (
        CellDispatchEnvelope,
        CellDispatchStamp,
    )


@dataclass(frozen=True, slots=True)
class InteractiveAdmission:
    """Admitted interactive entry snapshot binding one actor to a cell."""

    cell_id: str
    member: str
    epoch: str
    revision: int


@dataclass(frozen=True, slots=True)
class CellTerminalRoute:
    """Bridge-facing cell terminal decision and optional next-hop envelope."""

    action: str
    envelope: CellDispatchEnvelope | None = None
    reason: str | None = None
    used_fail_open: bool = False


@runtime_checkable
class CellDeliveryPort(Protocol):
    """One admitted envelope per captured delivery, or None to defer."""

    def headless_envelope(
        self, worker: str, delivery: HarnessDelivery
    ) -> CellDispatchEnvelope | None: ...

    def steer_envelope(
        self, stamp: CellDispatchStamp | None, delivery_id: str
    ) -> CellDispatchEnvelope | None: ...

    def interactive_admission(
        self, actor: str
    ) -> InteractiveAdmission | None: ...

    def next_hop_route(
        self,
        worker: str,
        *,
        delivery: HarnessDelivery,
        outcome: str,
        label: str | None = None,
        stamp: CellDispatchStamp | None = None,
        hop_index: int = 0,
    ) -> CellTerminalRoute: ...


__all__ = ["CellDeliveryPort", "CellTerminalRoute", "InteractiveAdmission"]
