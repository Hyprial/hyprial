from __future__ import annotations

import threading
import time
from pathlib import Path

from hyprial.daemon.impl.correlation.correlation import CorrelationEventRouter
from hyprial.daemon.impl.lifecycle.lifecycle_coordinator import LifecycleCoordinator
from hyprial.daemon.impl.lifecycle_manager import (
    LifecycleOperation,
    LifecyclePorts,
    LifecycleState,
)
from tests.support.network_owner_context import OwnerContextTracer

from ..support import _CompletionPorts, _spec


def test_reservation_timeout_reports_stable_receipt_and_recovers_after_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "bounded-reservation.sqlite3"
    router = CorrelationEventRouter()
    ports = _CompletionPorts(router)
    coordinator = LifecycleCoordinator(
        database,
        LifecyclePorts(ports, ports, ports, ports),
        router,
    )
    coordinator._reservation_timeout = 0.05
    operation = LifecycleOperation.create("stable-reservation", _spec())
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []
    original = coordinator._store.reserve

    def blocked_reserve(value: LifecycleOperation):
        calls.append(value.operation_id)
        entered.set()
        assert release.wait(3.0)
        return original(value)

    coordinator._store.reserve = blocked_reserve
    outcome: list[object] = []

    def submit() -> None:
        try:
            outcome.append(coordinator.submit(operation))
        except BaseException as error:  # noqa: BLE001 - the receipt is asserted below
            outcome.append(error)

    submitter = threading.Thread(target=submit)
    try:
        with OwnerContextTracer().installed():
            submitter.start()
            assert entered.wait(1.0)
            submitter.join(0.5)
        assert not submitter.is_alive(), "reservation admission waited without a bound"
        assert len(outcome) == 1
        error = outcome[0]
        assert isinstance(error, TimeoutError)
        assert getattr(error, "operation_id", None) == operation.operation_id
        assert calls == [operation.operation_id]

        release.set()
        terminal = {
            LifecycleState.COMPLETED,
            LifecycleState.COMPENSATED,
            LifecycleState.FAILED,
        }
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                if coordinator.result(operation.operation_id).state in terminal:
                    break
            except KeyError:
                pass
            time.sleep(0.01)
        assert coordinator.result(operation.operation_id).state in terminal
        assert coordinator.drain(3.0)

        recovered = LifecycleCoordinator(
            database,
            LifecyclePorts(ports, ports, ports, ports),
            router,
        )
        try:
            receipt = recovered.result(operation.operation_id)
            assert receipt.state in terminal
            assert calls == [operation.operation_id]
        finally:
            assert recovered.drain(3.0)
    finally:
        release.set()
        submitter.join(3.0)
        coordinator.drain(3.0)
