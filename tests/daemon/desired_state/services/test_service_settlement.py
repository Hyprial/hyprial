from __future__ import annotations

from pathlib import Path
import threading

from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.desired_state_io import (
    DesiredStateIoPort,
    DesiredStateIoRequest,
    DesiredStateOperation,
)
from hyprial.daemon.impl.state_persistence import StatePersistenceAuthority
from hyprial.kernel.impl.actor_runtime import AdmissionResult


def test_authority_normal_and_settled_service_ports_share_one_state(tmp_path: Path) -> None:
    authority = StatePersistenceAuthority(
        DesiredStateStore(tmp_path / "desired.json"), call_timeout=1.0
    )
    try:
        normal = authority.desired.set_service_connection("api", 0)
        settled = authority.settled_desired.set_service_registry(
            {
                "schemaVersion": 1,
                "spaceId": "space",
                "owner": "owner",
                "cachedAtMs": 1,
                "entries": [],
            }
        )
        assert normal.service_connections[0].name == "api"
        assert settled.service_registry is not None
        assert authority.desired.load() == settled
    finally:
        assert authority.close()


def test_desired_state_io_uses_typed_service_operation_and_acknowledges(tmp_path: Path) -> None:
    store = DesiredStateStore(tmp_path / "desired.json")
    completed = []
    ready = threading.Event()
    port = DesiredStateIoPort(
        store,
        complete=lambda event: (completed.append(event), ready.set(), AdmissionResult.ACCEPTED)[2],
        capacity=1,
        retry_seconds=0.001,
    )
    request = DesiredStateIoRequest(
        "service-connection",
        1,
        0,
        DesiredStateOperation.SET_SERVICE_CONNECTION,
        args=("api", 0),
        context=("service",),
    )
    try:
        assert port.submit(request) is AdmissionResult.ACCEPTED
        assert ready.wait(1.0)
        assert completed[0].result.result.service_connections[0].name == "api"
        assert port.acknowledge(request)
        assert store.load() == completed[0].result.snapshot
    finally:
        assert port.close(1.0)
