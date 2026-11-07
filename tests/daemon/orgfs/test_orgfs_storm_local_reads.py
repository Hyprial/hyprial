"""An unreachable foreign supplier cannot block daemon-local org facts."""

from __future__ import annotations

from threading import Event, Lock, Thread
import time
from types import SimpleNamespace

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.org.network.ipc import ipc_org_list
from hyprial.daemon.impl.orgfs.mesh import OrgFsMesh
from hyprial.daemon.impl.orgfs.network.protocol import (
    ORGFS_SUPPLIER_SYNC_FAILURE_THRESHOLD,
)
from hyprial.daemon.impl.orgfs.network.supplier_health import SupplierSyncHealth
from hyprial.shell.impl.daemon_wiring import (
    daemon_dependencies as _deep_module_daemon_dependencies,
)


class _FakeTransport:
    def close(self) -> None:
        return None


def _held_mesh(
    health: SupplierSyncHealth,
    space_id: str,
    events: list[tuple[str, dict[str, object]]],
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
    mesh.store = SimpleNamespace(space_id=space_id)
    mesh._log = lambda _level, event, **fields: events.append((event, fields))
    mesh._submit_worker = lambda _operation, _key: True
    return mesh


def test_local_reads_stay_bounded_during_a_twenty_space_hold_storm(tmp_path) -> None:
    app = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id="node-local",
        hyprial_home=tmp_path / "home",
        owner="owner@example.com",
        **_deep_module_daemon_dependencies(),
    )
    app._transport = _FakeTransport()  # type: ignore[assignment]
    try:
        space_ids = [
            app._handle_orgfs("orgfs.create", {"name": f"space-{index}"})[
                "space_id"
            ]
            for index in range(20)
        ]
        target = space_ids[0]
        app._handle_orgfs(
            "orgfs.write",
            {"spaceId": target, "path": "local.txt", "text": "local-value"},
        )
        app._handle_orgfs("orgfs.mkdir", {"spaceId": target, "path": "work"})

        health = SupplierSyncHealth()
        events: list[tuple[str, dict[str, object]]] = []
        meshes = [_held_mesh(health, space_id, events) for space_id in space_ids]
        supplier = "unreachable-foreign-supplier"
        for _ in range(ORGFS_SUPPLIER_SYNC_FAILURE_THRESHOLD):
            meshes[0]._record_supplier_sync_failure(supplier, "no-holder-online")

        started = Event()
        finished = Event()

        def storm() -> None:
            started.set()
            for index in range(200_000):
                meshes[index % len(meshes)].schedule_sync_from(supplier)
            finished.set()

        worker = Thread(target=storm, name="orgfs-held-supplier-storm")
        worker.start()
        assert started.wait(1.0)
        reads = (
            lambda: ipc_org_list(app, {}),
            lambda: app._handle_work("work.ls", {"spaceId": target}),
            lambda: app._handle_orgfs(
                "orgfs.ls", {"spaceId": target, "path": ""}
            ),
            lambda: app._handle_orgfs(
                "orgfs.read", {"spaceId": target, "path": "local.txt"}
            ),
        )
        measured: list[tuple[float, object]] = []
        for read in reads:
            before = time.monotonic()
            value = read()
            measured.append((time.monotonic() - before, value))

        worker.join(timeout=10.0)
        assert finished.is_set()
        assert all(elapsed < 1.0 for elapsed, _value in measured)
        assert all(value is not None for _elapsed, value in measured)
        # The producer hot path performs only the shared O(1) accounting; its
        # single aggregate is emitted when the hold window settles.
        assert not [event for event, _fields in events if event == "orgfs.sync.deferred"]
    finally:
        app._close()
        app._close_home_lease()
