"""The one production builder of headless cell dispatch envelopes (M′ M3/M4).

Decision notes (cell model I5-2 parts B/C):

- ``build_headless_envelope`` is the single production construction point of
  a turn ``CellDispatchEnvelope``.  It accepts only the snapshot that was
  just read, the entry member that snapshot published and the already
  captured immutable delivery; it never accepts a caller- or member-supplied
  member.  The stamp's epoch/revision are the snapshot's diagnostics — they
  never authorise and never revoke.
- ``build_steer_envelope`` re-stamps an in-flight turn's stamp for the one
  steered delivery id.  It reads no snapshot: a steer keeps the original
  member even after the entry moved to another member (M′ M6).
- ``HeadlessCellDelivery`` implements ``CellDeliveryPort`` over two
  injected read callables: the worker's cell id (from the agent registry)
  and the cell actor registry's published snapshot.  Both are memory reads
  behind short locks; neither waits on a cell handler, the inbox or a
  harness.  When the cell world is not composed yet the port answers None
  on every call and the bridge leaves its rows pending.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from hyprial.daemon.impl.api import HarnessDelivery
from hyprial.daemon.impl.cells.actor.contracts import (
    CellDispatchEnvelope,
    CellDispatchStamp,
    DISPATCH_STEER,
    DISPATCH_TURN,
)
from hyprial.daemon.impl.cells.actor.routing import (
    CellTerminalAction,
    OUTCOME_DONE,
    OUTCOME_REFUSED,
    resolve_terminal_routing,
)
from hyprial.daemon.impl.cells.actor.snapshot import (
    CellActorSnapshot,
    headless_admission,
)
from hyprial.daemon.impl.composition.cell_delivery.port import (
    CellTerminalRoute,
    InteractiveAdmission,
)


def build_headless_envelope(
    snapshot: CellActorSnapshot,
    member: str,
    delivery: HarnessDelivery,
) -> CellDispatchEnvelope:
    """Mint the turn envelope of one admitted snapshot read.

    ``member`` must be exactly the entry member the snapshot published; the
    stamp pairs it with the effective revision of the structure that carries
    the entry.  The envelope's origin ids are the input's own settlement
    ids, so the stamp can never describe a different work item.
    """

    if member != snapshot.entry_member:
        raise ValueError("the stamped member must be the snapshot's entry member")
    members = tuple(delivery.message_ids) if delivery.message_ids else ()
    origin = members if members else (delivery.delivery_id,)
    stamp = CellDispatchStamp(
        cell_id=snapshot.cell_id,
        member=member,
        epoch=snapshot.epoch,
        revision=snapshot.effective_revision,
        origin_message_ids=origin,
    )
    return CellDispatchEnvelope(
        stamp=stamp, input=delivery, hop_index=0, dispatch_kind=DISPATCH_TURN
    )


def build_steer_envelope(
    stamp: CellDispatchStamp, delivery_id: str
) -> CellDispatchEnvelope:
    """Mint the steer envelope of one in-flight delivery (M′ M6)."""

    steered = replace(stamp, origin_message_ids=(delivery_id,))
    return CellDispatchEnvelope(
        stamp=steered, input=None, hop_index=0, dispatch_kind=DISPATCH_STEER
    )


def build_next_hop_envelope(
    snapshot: CellActorSnapshot,
    member: str,
    delivery: HarnessDelivery,
    *,
    hop_index: int,
) -> CellDispatchEnvelope:
    """Mint the turn envelope for the next hop inside an admitted cell."""

    routed_delivery = (
        delivery if delivery.recipient == member else replace(delivery, recipient=member)
    )
    members = tuple(routed_delivery.message_ids) if routed_delivery.message_ids else ()
    origin = members if members else (routed_delivery.delivery_id,)
    stamp = CellDispatchStamp(
        cell_id=snapshot.cell_id,
        member=member,
        epoch=snapshot.epoch,
        revision=snapshot.effective_revision,
        origin_message_ids=origin,
    )
    return CellDispatchEnvelope(
        stamp=stamp,
        input=routed_delivery,
        hop_index=hop_index,
        dispatch_kind=DISPATCH_TURN,
    )


class HeadlessCellDelivery:
    """The bridge-side port over the app's published cell snapshots."""

    def __init__(
        self,
        cell_of_worker: Callable[[str], str | None],
        snapshot_of_cell: Callable[[str], CellActorSnapshot | None],
    ) -> None:
        self._cell_of_worker = cell_of_worker
        self._snapshot_of_cell = snapshot_of_cell

    def headless_envelope(
        self, worker: str, delivery: HarnessDelivery
    ) -> CellDispatchEnvelope | None:
        cell_id = self._cell_of_worker(worker)
        if cell_id is None:
            return None
        try:
            snapshot = self._snapshot_of_cell(cell_id)
        except Exception:  # noqa: BLE001 - a failed read defers this delivery
            return None
        decision = headless_admission(snapshot)
        if not decision.admitted:
            return None
        assert snapshot is not None
        return build_headless_envelope(snapshot, decision.member, delivery)

    def steer_envelope(
        self, stamp: CellDispatchStamp | None, delivery_id: str
    ) -> CellDispatchEnvelope | None:
        if stamp is None:
            return None
        return build_steer_envelope(stamp, delivery_id)

    def interactive_admission(self, actor: str) -> InteractiveAdmission | None:
        """Admit one interactive actor when its cell publishes it as entry."""

        cell_id = self._cell_of_worker(actor)
        if cell_id is None:
            return None
        try:
            snapshot = self._snapshot_of_cell(cell_id)
        except Exception:  # noqa: BLE001
            return None
        decision = headless_admission(snapshot)
        if not decision.admitted or decision.member != actor:
            return None
        assert snapshot is not None
        return InteractiveAdmission(
            cell_id=snapshot.cell_id,
            member=decision.member,
            epoch=snapshot.epoch,
            revision=snapshot.effective_revision,
        )

    def next_hop_route(
        self,
        worker: str,
        *,
        delivery: HarnessDelivery,
        outcome: str,
        label: str | None = None,
        stamp: CellDispatchStamp | None = None,
        hop_index: int = 0,
    ) -> CellTerminalRoute:
        """Resolve one completed turn into a bridge-consumable cell action."""

        from_member = stamp.member if stamp is not None else worker
        if outcome in {OUTCOME_REFUSED, OUTCOME_DONE}:
            decision = resolve_terminal_routing(
                None,
                from_member=from_member,
                outcome=outcome,
                label=label,
                hop_index=hop_index,
            )
            return CellTerminalRoute(
                action=decision.action.value,
                reason=decision.reason,
            )
        cell_id = stamp.cell_id if stamp is not None else self._cell_of_worker(worker)
        snapshot: CellActorSnapshot | None = None
        if cell_id is not None:
            try:
                snapshot = self._snapshot_of_cell(cell_id)
            except Exception:  # noqa: BLE001 - a failed snapshot read keeps work pending
                return CellTerminalRoute(action=CellTerminalAction.PENDING.value)
        resolved_from = (
            stamp.member
            if stamp is not None
            else (
                snapshot.entry_member
                if snapshot is not None and snapshot.entry_member is not None
                else worker
            )
        )
        decision = resolve_terminal_routing(
            snapshot,
            from_member=resolved_from,
            outcome=outcome,
            label=label,
            hop_index=hop_index,
        )
        if decision.action is not CellTerminalAction.HOP:
            return CellTerminalRoute(
                action=decision.action.value,
                reason=decision.reason,
            )
        assert snapshot is not None and decision.member is not None
        envelope = build_next_hop_envelope(
            snapshot,
            decision.member,
            delivery,
            hop_index=decision.hop_index,
        )
        return CellTerminalRoute(
            action=decision.action.value,
            envelope=envelope,
            used_fail_open=decision.used_fail_open,
        )


__all__ = [
    "HeadlessCellDelivery",
    "build_headless_envelope",
    "build_next_hop_envelope",
    "build_steer_envelope",
]
