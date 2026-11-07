"""A supplier that cannot be reached must stop being retried at tick rate.

Measured on a live node: one offline holder produced ~3.3 scheduled-sync
failures per second because every reconcile tick re-queued it, and each
known-doomed attempt was charged to the reconcile budget that delivery and
harness launch also draw from.  These tests pin both directions of the hold:
it must engage past the threshold, and it must not engage before it or after
a success.
"""

from __future__ import annotations

from threading import Lock
from types import SimpleNamespace


from hyprial.daemon.impl.orgfs.mesh import OrgFsMesh
from hyprial.daemon.impl.orgfs.network.supplier_health import SupplierSyncHealth
from hyprial.daemon.impl.orgfs.network.protocol import (
    ORGFS_SUPPLIER_SYNC_FAILURE_THRESHOLD,
    ORGFS_SUPPLIER_SYNC_HOLD_MAX_SECONDS,
)


def _bare_mesh() -> tuple[OrgFsMesh, list[tuple[str, dict[str, object]]], list[str]]:
    """A recovery surface with only the scheduled-sync lane wired up."""

    mesh = OrgFsMesh.__new__(OrgFsMesh)
    events: list[tuple[str, dict[str, object]]] = []
    submitted: list[str] = []
    mesh._closed = False
    mesh._worker_lock = Lock()
    mesh._sync_pending = set()
    mesh._sync_active = set()
    mesh._sync_rerun = set()
    mesh._sync_dropped = 0
    mesh._supplier_sync_health = SupplierSyncHealth()
    mesh._supplier_sync_local_health = SupplierSyncHealth()
    mesh.store = SimpleNamespace(space_id="test-space")
    mesh._supplier_sync_held_until_total = 0
    mesh._log = lambda _level, event, **fields: events.append((event, fields))
    mesh._submit_worker = lambda _op, key: submitted.append(key) or True
    return mesh, events, submitted


def test_a_known_unreachable_supplier_is_held_off_instead_of_retried() -> None:
    mesh, events, submitted = _bare_mesh()
    supplier = "autodl-container-dead"

    for _ in range(ORGFS_SUPPLIER_SYNC_FAILURE_THRESHOLD):
        mesh._record_supplier_sync_failure(supplier, "no-holder-online")

    assert mesh._supplier_sync_health.hold_remaining(supplier) > 0
    assert mesh.schedule_sync_from(supplier) is False
    assert submitted == []  # must-fire: no worker, the attempt cannot succeed
    # Deferred triggers are counted in memory and emitted once when this hold
    # window settles; the hot path must not serialize one event per trigger.
    assert not [f for e, f in events if e == "orgfs.sync.deferred"]
    held = [f for e, f in events if e == "orgfs.sync.held"]
    assert held and held[-1]["holdSeconds"] > 0


def test_below_the_threshold_a_failure_still_retries() -> None:
    """Counterexample: the hold must not swallow ordinary jitter."""

    mesh, events, submitted = _bare_mesh()
    supplier = "peer-with-momentary-jitter"

    mesh._record_supplier_sync_failure(supplier, "sync-failed")

    assert mesh._supplier_sync_health.hold_remaining(supplier) == 0
    assert mesh.schedule_sync_from(supplier) is True
    assert submitted == [supplier]
    assert not [e for e, _ in events if e == "orgfs.sync.held"]


def test_a_successful_sync_clears_the_hold_and_the_count() -> None:
    mesh, events, _submitted = _bare_mesh()
    supplier = "flapping-holder"

    for _ in range(ORGFS_SUPPLIER_SYNC_FAILURE_THRESHOLD + 2):
        mesh._record_supplier_sync_failure(supplier, "no-holder-online")
    assert mesh.schedule_sync_from(supplier) is False

    mesh._clear_supplier_sync_failure(supplier)

    assert (
        mesh._supplier_sync_health.failure_count(supplier)
        == (ORGFS_SUPPLIER_SYNC_FAILURE_THRESHOLD + 2) // 2
    )
    assert mesh._supplier_sync_health.hold_remaining(supplier) == 0
    assert mesh.schedule_sync_from(supplier) is True
    recovered = [f for e, f in events if e == "orgfs.sync.recovered"]
    assert recovered and recovered[-1]["afterFailures"] == (
        ORGFS_SUPPLIER_SYNC_FAILURE_THRESHOLD + 2
    )
    assert recovered[-1]["remainingFailures"] == (
        (ORGFS_SUPPLIER_SYNC_FAILURE_THRESHOLD + 2) // 2
    )


def test_the_hold_is_capped_so_a_flapping_holder_settles() -> None:
    mesh, _events, _submitted = _bare_mesh()
    supplier = "flapping-holder"

    for _ in range(ORGFS_SUPPLIER_SYNC_FAILURE_THRESHOLD):
        mesh._record_supplier_sync_failure(supplier, "no-holder-online")

    for _ in range(8):
        mesh._record_supplier_sync_failure(supplier, "no-holder-online")
        remaining = mesh._supplier_sync_health.hold_remaining(supplier)
        assert 0 < remaining <= ORGFS_SUPPLIER_SYNC_HOLD_MAX_SECONDS + 1.0
