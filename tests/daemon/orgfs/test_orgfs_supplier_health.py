"""Supplier sync health is per supplier node, not per orgfs space.

The prod follow-up to #1599: each space had its own failure counter and hold,
so an offline supplier needed three failures *per space* before holding, and
one success zeroed the count so a flapping supplier never reached the cap.
These tests pin the shared health and the gradual decay.
"""

from __future__ import annotations

from types import SimpleNamespace
from threading import Lock

from hyprial.daemon.impl.orgfs.mesh import OrgFsMesh
from hyprial.daemon.impl.orgfs.network import supplier_health as supplier_health_module
from hyprial.daemon.impl.orgfs.network.protocol import (
    ORGFS_SUPPLIER_SYNC_FAILURE_THRESHOLD,
    ORGFS_SUPPLIER_SYNC_HOLD_MAX_SECONDS,
)
from hyprial.daemon.impl.orgfs.network.supplier_health import SupplierSyncHealth

SUPPLIER = "autodl-container-dead"


def _bare_mesh(
    health: SupplierSyncHealth,
    *,
    space_id: str = "s1",
    events: list[tuple[str, dict[str, object]]] | None = None,
    submitted: list[str] | None = None,
) -> OrgFsMesh:
    mesh = OrgFsMesh.__new__(OrgFsMesh)
    mesh._closed = False
    mesh._worker_lock = Lock()
    mesh._sync_pending = set()
    mesh._sync_active = set()
    mesh._sync_rerun = set()
    mesh._sync_dropped = 0
    mesh._supplier_sync_health = health
    mesh._supplier_sync_local_health = SupplierSyncHealth()
    mesh._supplier_sync_held_until_total = 0
    mesh._supplier_online = lambda _supplier: True
    mesh.store = SimpleNamespace(space_id=space_id)
    mesh._log = lambda _level, event, **fields: (
        events.append((event, fields)) if events is not None else None
    )
    mesh._submit_worker = lambda _op, key: (
        (submitted.append(key) if submitted is not None else None) or True
    )
    return mesh


def test_three_failures_across_spaces_hold_the_supplier_everywhere() -> None:
    health = SupplierSyncHealth()
    events_a: list[tuple[str, dict[str, object]]] = []
    events_b: list[tuple[str, dict[str, object]]] = []
    mesh_a = _bare_mesh(health, space_id="s1", events=events_a)
    mesh_b = _bare_mesh(health, space_id="s2", events=events_b)

    mesh_a._record_supplier_sync_failure(SUPPLIER, "no-holder-online")
    mesh_a._record_supplier_sync_failure(SUPPLIER, "no-holder-online")
    assert health.hold_remaining(SUPPLIER) == 0
    mesh_b._record_supplier_sync_failure(SUPPLIER, "no-holder-online")

    assert health.failure_count(SUPPLIER) == ORGFS_SUPPLIER_SYNC_FAILURE_THRESHOLD
    assert health.hold_remaining(SUPPLIER) > 0
    assert mesh_a.schedule_sync_from(SUPPLIER) is False
    assert mesh_b.schedule_sync_from(SUPPLIER) is False
    assert not [f for e, f in events_b if e == "orgfs.sync.deferred"]
    assert mesh_a._supplier_sync_held_until_total == 1
    assert mesh_b._supplier_sync_held_until_total == 1


def test_hold_triggers_are_aggregated_across_spaces(
    monkeypatch,
) -> None:
    now = [100.0]
    monkeypatch.setattr(supplier_health_module.time, "monotonic", lambda: now[0])
    health = SupplierSyncHealth()
    events: list[tuple[str, dict[str, object]]] = []
    meshes = [
        _bare_mesh(health, space_id=f"s{index}", events=events)
        for index in range(27)
    ]
    for _ in range(ORGFS_SUPPLIER_SYNC_FAILURE_THRESHOLD):
        meshes[0]._record_supplier_sync_failure(SUPPLIER, "no-holder-online")

    trigger_count = 15_020
    for index in range(trigger_count):
        assert meshes[index % len(meshes)].schedule_sync_from(SUPPLIER) is False

    # The final count is not knowable until the hold closes, so the one
    # aggregate is emitted by the probe outcome rather than by every trigger.
    assert [fields for event, fields in events if event == "orgfs.sync.deferred"] == []
    now[0] += 5.001
    assert meshes[0].schedule_sync_from(SUPPLIER) is True
    meshes[0]._clear_supplier_sync_failure(SUPPLIER)

    deferred = [fields for event, fields in events if event == "orgfs.sync.deferred"]
    assert len(deferred) == 1
    assert deferred[0]["supplier"] == SUPPLIER
    assert deferred[0]["suppressedCount"] == trigger_count


def test_only_one_space_probes_when_a_shared_hold_expires(monkeypatch) -> None:
    now = [200.0]
    monkeypatch.setattr(supplier_health_module.time, "monotonic", lambda: now[0])
    health = SupplierSyncHealth()
    submitted: list[str] = []
    meshes = [
        _bare_mesh(health, space_id=f"s{index}", submitted=submitted)
        for index in range(27)
    ]
    for _ in range(ORGFS_SUPPLIER_SYNC_FAILURE_THRESHOLD):
        meshes[0]._record_supplier_sync_failure(SUPPLIER, "no-holder-online")

    now[0] += 5.001
    admitted = [mesh.schedule_sync_from(SUPPLIER) for mesh in meshes]

    assert admitted.count(True) == 1
    assert submitted == [SUPPLIER]


def test_an_offline_expiry_probe_reholds_instead_of_spinning(monkeypatch) -> None:
    now = [300.0]
    monkeypatch.setattr(supplier_health_module.time, "monotonic", lambda: now[0])
    health = SupplierSyncHealth()
    events: list[tuple[str, dict[str, object]]] = []
    mesh = _bare_mesh(health, events=events)
    mesh._supplier_online = lambda _supplier: False
    mesh._submit_worker = lambda operation, key: operation(key) is None
    for _ in range(ORGFS_SUPPLIER_SYNC_FAILURE_THRESHOLD):
        mesh._record_supplier_sync_failure(SUPPLIER, "no-holder-online")
    for _ in range(100):
        assert mesh.schedule_sync_from(SUPPLIER) is False

    now[0] += 5.001
    assert mesh.schedule_sync_from(SUPPLIER) is True

    assert health.hold_remaining(SUPPLIER) > 0
    deferred = [fields for event, fields in events if event == "orgfs.sync.deferred"]
    assert len(deferred) == 1
    assert deferred[0]["suppressedCount"] == 100


def test_retry_ladder_stops_as_soon_as_the_shared_hold_starts(monkeypatch) -> None:
    from hyprial.daemon.impl.orgfs.storage.store import StoreError

    health = SupplierSyncHealth()
    mesh = _bare_mesh(health)
    for _ in range(ORGFS_SUPPLIER_SYNC_FAILURE_THRESHOLD - 1):
        mesh._record_supplier_sync_failure(SUPPLIER, "no-holder-online")
    attempts = 0

    def fail(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise StoreError("no-holder-online", "no holder")

    monkeypatch.setattr(supplier_health_module.time, "sleep", lambda _delay: None)
    mesh.sync_from = fail
    mesh._sync_pending = {SUPPLIER}
    mesh._run_scheduled_sync(SUPPLIER)

    assert attempts == 1
    assert health.hold_remaining(SUPPLIER) > 0


def test_a_success_halves_the_failure_count_instead_of_clearing_it() -> None:
    health = SupplierSyncHealth()
    for _ in range(ORGFS_SUPPLIER_SYNC_FAILURE_THRESHOLD + 2):
        health.record_failure(SUPPLIER, "no-holder-online")

    after = health.record_success(SUPPLIER)

    assert after == (ORGFS_SUPPLIER_SYNC_FAILURE_THRESHOLD + 2) // 2
    assert health.failure_count(SUPPLIER) == after


def test_a_flapping_supplier_converges_to_the_hold_cap() -> None:
    health = SupplierSyncHealth()
    for _ in range(20):
        for _ in range(8):
            health.record_failure(SUPPLIER, "no-holder-online")
        health.record_success(SUPPLIER)
    for _ in range(8):
        health.record_failure(SUPPLIER, "no-holder-online")

    remaining = health.hold_remaining(SUPPLIER)
    assert remaining > 0
    assert remaining <= ORGFS_SUPPLIER_SYNC_HOLD_MAX_SECONDS
    assert remaining >= ORGFS_SUPPLIER_SYNC_HOLD_MAX_SECONDS - 1


def test_events_carry_the_space_id() -> None:
    health = SupplierSyncHealth()
    events: list[tuple[str, dict[str, object]]] = []
    mesh = _bare_mesh(health, space_id="space-42", events=events)

    for _ in range(ORGFS_SUPPLIER_SYNC_FAILURE_THRESHOLD):
        mesh._record_supplier_sync_failure(SUPPLIER, "no-holder-online")
    mesh.schedule_sync_from(SUPPLIER)

    held = [fields for event, fields in events if event == "orgfs.sync.held"]
    assert held and held[-1]["spaceId"] == "space-42"
    assert not [fields for event, fields in events if event == "orgfs.sync.deferred"]


def test_space_local_refusal_never_poisons_another_space() -> None:
    from hyprial.daemon.impl.orgfs.storage.store import StoreError

    health = SupplierSyncHealth()
    mesh_a = _bare_mesh(health, space_id="s1")
    mesh_b = _bare_mesh(health, space_id="s2")
    refusal = StoreError(
        "not-a-member",
        "supplier's device owner is not a current member",
        reason="supplier-not-member",
    )
    assert mesh_a._supplier_failure_is_global(refusal) is False
    for _ in range(ORGFS_SUPPLIER_SYNC_FAILURE_THRESHOLD):
        mesh_a._record_supplier_sync_failure(
            SUPPLIER, "not-a-member", shared=False
        )

    assert health.failure_count(SUPPLIER) == 0
    assert mesh_a._supplier_sync_local_health.failure_count(SUPPLIER) == 3
    assert mesh_a.schedule_sync_from(SUPPLIER) is False
    assert mesh_b.schedule_sync_from(SUPPLIER) is True


def test_a_held_bad_supplier_does_not_starve_a_healthy_one() -> None:
    health = SupplierSyncHealth()
    events: list[tuple[str, dict[str, object]]] = []
    submitted: list[str] = []
    mesh = _bare_mesh(health, events=events, submitted=submitted)
    for _ in range(ORGFS_SUPPLIER_SYNC_FAILURE_THRESHOLD):
        mesh._record_supplier_sync_failure(SUPPLIER, "no-holder-online")

    assert mesh.schedule_sync_from(SUPPLIER) is False
    assert mesh.schedule_sync_from("healthy-supplier") is True
    assert submitted == ["healthy-supplier"]


def test_failed_and_recovered_events_carry_the_space_id() -> None:
    from hyprial.daemon.impl.orgfs.storage.store import StoreError

    health = SupplierSyncHealth()
    events: list[tuple[str, dict[str, object]]] = []
    mesh = _bare_mesh(health, space_id="space-7", events=events)
    mesh._supplier_allowed = lambda _supplier: True
    mesh._sync_pending = {SUPPLIER}
    mesh._sync_active = set()
    mesh._sync_rerun = set()
    mesh._sync_dropped = 0
    mesh._worker_executor = None

    def fail(*_args, **_kwargs):
        raise StoreError("no-holder-online", "no holder")

    mesh.sync_from = fail
    mesh._submit_worker = lambda _op, _key: True
    mesh._run_scheduled_sync(SUPPLIER)
    failed = [f for e, f in events if e == "orgfs.sync.failed"]
    assert failed and failed[0]["spaceId"] == "space-7"

    mesh._clear_supplier_sync_failure(SUPPLIER)
    recovered = [f for e, f in events if e == "orgfs.sync.recovered"]
    assert recovered and recovered[-1]["spaceId"] == "space-7"


def test_lifecycle_injects_the_daemon_wide_health() -> None:
    """Must-fire: removing the composition kwarg would restore per-mesh state."""

    import inspect

    from hyprial.daemon.impl.orgfs.composition.lifecycle import RuntimeLifecycle
    from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime

    source = inspect.getsource(RuntimeLifecycle._create_mesh_sync)
    assert "supplier_sync_health=self._supplier_sync_health" in source
    runtime_source = inspect.getsource(OrgFsRuntime.__init__)
    assert "self._supplier_sync_health = SupplierSyncHealth()" in runtime_source


def test_unknown_space_refusals_are_local_not_global() -> None:
    """A space-local unknown-space refusal must not poison daemon-wide health."""

    from hyprial.daemon.impl.orgfs.storage.store import StoreError

    health = SupplierSyncHealth()
    mesh_a = _bare_mesh(health, space_id="s1")
    mesh_b = _bare_mesh(health, space_id="s2")
    refusal = StoreError("unknown-space", "sync requested another space")
    assert mesh_a._supplier_failure_is_global(refusal) is False
    for _ in range(ORGFS_SUPPLIER_SYNC_FAILURE_THRESHOLD):
        mesh_a._record_supplier_sync_failure(
            SUPPLIER,
            "unknown-space",
            shared=mesh_a._supplier_failure_is_global(refusal),
        )

    assert health.failure_count(SUPPLIER) == 0
    assert mesh_a._supplier_sync_local_health.failure_count(SUPPLIER) == 3
    assert mesh_b.schedule_sync_from(SUPPLIER) is True


def test_an_unrecognized_error_code_is_local() -> None:
    from hyprial.daemon.impl.orgfs.storage.store import StoreError

    health = SupplierSyncHealth()
    mesh = _bare_mesh(health)
    error = StoreError("mystery-code", "not a proven reachability failure")
    assert mesh._supplier_failure_is_global(error) is False
    assert isinstance(error, TimeoutError) is False


def test_three_scheduled_unknown_space_refusals_stay_local() -> None:
    from hyprial.daemon.impl.orgfs.storage.store import StoreError

    health = SupplierSyncHealth()
    mesh = _bare_mesh(health, space_id="s1")
    other = _bare_mesh(health, space_id="s2")
    mesh._supplier_allowed = lambda _supplier: True
    mesh._sync_pending = {SUPPLIER}
    mesh._sync_active = set()
    mesh._sync_rerun = set()
    mesh._sync_dropped = 0
    mesh._worker_executor = None

    def refuse(*_args, **_kwargs):
        raise StoreError("unknown-space", "sync requested another space")

    mesh.sync_from = refuse
    for _ in range(3):
        with mesh._worker_lock:
            mesh._sync_pending = {SUPPLIER}
        mesh._run_scheduled_sync(SUPPLIER)

    assert health.failure_count(SUPPLIER) == 0
    assert mesh._supplier_sync_local_health.failure_count(SUPPLIER) >= 3
    assert other.schedule_sync_from(SUPPLIER) is True
