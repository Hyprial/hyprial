"""Test-world headless cell delivery for bridge compositions (I5-2 B/C).

Decision record: bridge-level wiring tests that drive a REAL
``ManagedHarnessRuntime``/``HarnessRuntimeActor`` need the headless cell
delivery port the production wiring injects.  ``AdmittingCellDelivery``
admits every offered worker — the stamp names that worker as the entry
member — so the envelope path, its origin ids and its steer reuse are
exercised without composing a cell world.  Tests that instead pass a bare
dispatch fake keep receiving the bare delivery (the bridge only builds
envelopes when a port is wired).
"""

from __future__ import annotations

from typing import Any

from hyprial.daemon.impl.cells.actor.contracts import (
    CellDispatchEnvelope,
    CellDispatchStamp,
)
from hyprial.daemon.impl.composition.cell_delivery.headless import (
    build_steer_envelope,
)
from hyprial.daemon.impl.composition.cell_delivery.port import InteractiveAdmission


class AdmittingCellDelivery:
    """Admit every worker; the scanned worker is the entry member."""

    def __init__(self, *, cell_id: str = "c.test") -> None:
        self.cell_id = cell_id
        self.offered: list[tuple[str, tuple[str, ...]]] = []

    def headless_envelope(self, worker: str, delivery: Any) -> CellDispatchEnvelope:
        members = tuple(delivery.message_ids) if delivery.message_ids else ()
        origin = members if members else (delivery.delivery_id,)
        self.offered.append((worker, origin))
        return CellDispatchEnvelope(
            stamp=CellDispatchStamp(self.cell_id, worker, "ce-1", 1, origin),
            input=delivery,
        )

    def steer_envelope(
        self, stamp: CellDispatchStamp | None, delivery_id: str
    ) -> CellDispatchEnvelope | None:
        if stamp is None:
            return None
        return build_steer_envelope(stamp, delivery_id)

    def interactive_admission(self, actor: str) -> InteractiveAdmission:
        return InteractiveAdmission(
            cell_id=self.cell_id, member=actor, epoch="ce-1", revision=1
        )


__all__ = ["AdmittingCellDelivery"]
