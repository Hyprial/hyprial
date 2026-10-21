"""Bind and unbind peers onto slots as the directory changes (design §5).

Three entry points, one contract:

* ``reconcile_slot_bindings(app)`` -- the idempotent whole-view pass.  It
  re-derives the set of peers THIS node dials (the one-dialer rule, PR
  #1283's ``dial_peers`` via ``directory_peers_for_app``), binds what is
  missing and unbinds what is gone.  The daemon's periodic reconcile tick
  calls it, which also keeps the dial-direction liveness fallback armed
  (that fallback is time-driven, so a purely event-driven caller is not
  enough on its own).
* ``bind_peer_slot`` / ``unbind_peer_slot`` -- the per-device hooks for the
  S2b peer-access actor (feat/peer-access-actor-s2b), which receives
  directory events and owns the ``allow`` set.  Order within one event is
  fixed by the design: the actor updates ``allow`` FIRST, then calls these.
  The granular hooks assume the caller already decided this node dials the
  peer; the whole-view pass applies the rule itself.

Slot lifecycle rules (§5):

* a device to dial gets a free slot assigned (reusing the persisted one),
  then ``map-peer``;
* a removed device is ``unmap-peer``-ed first, and only then is its slot
  released -- an unmap that fails while the sidecar is up keeps the slot
  assigned so the next pass retries, instead of handing a spliced slot to
  another peer;
* an exhausted pool grows by one base step and raises an alarm
  (``zenoh.slots.pool_exhausted``); the growth is what the one remaining
  ``reconfigure_connect`` caller then dials.
"""

from __future__ import annotations

from typing import Any

from .store import SlotAssignments


def slot_store_for_app(app: Any) -> SlotAssignments:
    """The daemon-lifetime assignment store, created on first use."""

    store = getattr(app, "_slot_assignments", None)
    if not isinstance(store, SlotAssignments):
        store = SlotAssignments(app.hyprial_home)
        app._slot_assignments = store
    return store


def _log(app: Any, level: str, event: str, **fields: object) -> None:
    sink = getattr(app, "_log", None)
    if sink is None:
        return
    try:
        sink(level, "zenoh", event, **fields)
    except Exception:  # noqa: BLE001 - reporting must never break binding
        pass


def _assign(app: Any, store: SlotAssignments, label: str) -> int:
    previous = store.pool_size
    slot, grown = store.assign(label)
    if grown:
        # The alarm the design requires: exhaustion is survivable (the
        # grown set reaches zenoh through the one remaining
        # ``reconfigure_connect`` call) but never silent.
        _log(
            app,
            "warn",
            "zenoh.slots.pool_exhausted",
            peer=label,
            slot=slot,
            previousPoolSize=previous,
            poolSize=store.pool_size,
            detail=(
                "slot pool exhausted; grew the fixed connect set, which "
                "costs one zenoh session rebuild on this node"
            ),
        )
    return slot


def bind_peer_slot(app: Any, device_label: str, address: str) -> int | None:
    """Assign (or reuse) the peer's slot and ``map-peer`` it; returns the slot.

    Returns the slot even while the sidecar is down -- the assignment is
    durable and the next pass re-sends the mapping; None only when the
    wire call failed outright (logged, retried by the next pass).
    """

    store = slot_store_for_app(app)
    slot = _assign(app, store, device_label)
    backend = app._forwarding_backend()
    if backend is None:
        return slot
    try:
        if backend.bind(device_label, address, slot):
            _log(
                app, "info", "zenoh.slots.bound",
                peer=device_label, slot=slot, address=address,
            )
    except Exception as error:  # noqa: BLE001 - one peer must not stop the rest
        _log(
            app, "warn", "zenoh.slots.bind_failed",
            peer=device_label, slot=slot, detail=str(error)[:300],
        )
        return None
    return slot


def unbind_peer_slot(app: Any, device_label: str) -> int | None:
    """``unmap-peer`` first, then free the slot; returns the freed slot.

    With the sidecar down there is no live mapping to unmap, so the slot
    is released directly.  A failed unmap on a LIVE sidecar keeps the
    assignment (and returns None) so the slot cannot be handed to another
    peer while the old splice might still exist; the next pass retries.
    """

    store = slot_store_for_app(app)
    backend = app._forwarding_backend()
    slot = store.slot_for(device_label)
    if backend is not None and slot is not None:
        try:
            backend.unbind(device_label)
        except Exception as error:  # noqa: BLE001 - retried next pass
            _log(
                app, "warn", "zenoh.slots.unbind_failed",
                peer=device_label, slot=slot, detail=str(error)[:300],
            )
            return None
    released = store.release(device_label)
    if released is not None:
        _log(app, "info", "zenoh.slots.unbound", peer=device_label, slot=released)
    return released


def reconcile_slot_bindings(app: Any, *, reason: str = "tick") -> dict[str, object]:
    """One idempotent pass: unbind the gone, bind the missing, report readings.

    Unbinds run first so a leave-and-join wave frees slots before new
    assignments can force a pool growth.  Never raises: a failure on one
    peer is logged and the pass continues, because this runs on the
    daemon's reconcile tick.
    """

    store = slot_store_for_app(app)
    pool_before = store.pool_size
    try:
        # The application's directory seam (it answers the org directory);
        # going through the method rather than the module function keeps
        # one override point for the whole forwarding half.
        directory = getattr(app, "_tailcat_directory_peers", None)
        if callable(directory):
            wanted = directory()
        else:
            from hyprial.daemon.impl.org.network import directory_peers_for_app

            wanted = directory_peers_for_app(app)
    except Exception:  # noqa: BLE001 - the tick must survive a directory fault
        wanted = {}
    unbound: list[str] = []
    bound: list[str] = []
    for label in sorted(set(store.assignments) - set(wanted)):
        if unbind_peer_slot(app, label) is not None:
            unbound.append(label)
    for label in sorted(wanted):
        if bind_peer_slot(app, label, wanted[label]) is not None:
            bound.append(label)
    return {
        "reason": reason,
        "bound": bound,
        "unbound": unbound,
        "grown": store.pool_size > pool_before,
        "poolSize": store.pool_size,
    }
