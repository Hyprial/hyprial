from __future__ import annotations

import threading
import time

import pytest

from hyprial.kernel.impl.actor_runtime import AdmissionResult
from hyprial.daemon.impl.desired_state import DesiredState
from hyprial.daemon.impl.desired_state_io import (
    DesiredStateIoCompleted,
    DesiredStateIoPort,
    DesiredStateIoRequest,
    DesiredStateOperation,
)
from hyprial.daemon.impl.state_persistence import (
    StateCommandCompleted,
    StateCostOrigin,
    StatePersistenceTimeout,
)


def test_request_refuses_an_untyped_cost_origin() -> None:
    with pytest.raises(TypeError, match="cost_origin must be a StateCostOrigin"):
        DesiredStateIoRequest(
            "invalid-origin",
            1,
            0,
            DesiredStateOperation.RECORD_SESSION_AGENT_EFFECTS,
            cost_origin="ipc",  # type: ignore[arg-type]
        )


def test_completion_has_one_accepted_handoff_until_owner_releases_it() -> None:
    class Store:
        supports_state_cost_origin = True

        def record_session_agent_effects(  # type: ignore[no-untyped-def]
            self, effects, *, _cost_origin=StateCostOrigin.BACKGROUND
        ):
            assert effects == ()
            assert _cost_origin is StateCostOrigin.BACKGROUND
            return "stored"

        def load(self) -> DesiredState:
            return DesiredState()

    completions = []
    first = threading.Event()
    second = threading.Event()

    def complete(event):  # type: ignore[no-untyped-def]
        completions.append(event)
        (first if len(completions) == 1 else second).set()
        return AdmissionResult.ACCEPTED

    port = DesiredStateIoPort(
        Store(), complete=complete, capacity=1, retry_seconds=0.001
    )
    request = DesiredStateIoRequest(
        "one-handoff",
        1,
        0,
        DesiredStateOperation.RECORD_SESSION_AGENT_EFFECTS,
        args=((),),
        context=("test",),
    )
    try:
        assert port.submit(request) is AdmissionResult.ACCEPTED
        assert first.wait(1.0)
        time.sleep(0.03)
        assert len(completions) == 1

        port.redeliver(request)
        assert second.wait(1.0)
        assert len(completions) == 2
        assert completions[0] == completions[1]
        assert port.acknowledge(request)
    finally:
        assert port.close(1.0)


def test_late_state_completion_is_joined_without_readmission() -> None:
    class Store:
        supports_state_cost_origin = True
        calls = 0

        def record_session_agent_effects(  # type: ignore[no-untyped-def]
            self, effects, *, _cost_origin=StateCostOrigin.BACKGROUND
        ):
            assert effects == ()
            assert _cost_origin is StateCostOrigin.IPC
            self.calls += 1
            raise StatePersistenceTimeout("state-correlation")

        def load(self) -> DesiredState:
            return DesiredState()

    store = Store()
    completions = []
    ready = threading.Event()
    polls = 0

    def late_result(correlation_id: str):  # type: ignore[no-untyped-def]
        nonlocal polls
        assert correlation_id == "state-correlation"
        polls += 1
        if polls == 1:
            return None
        return StateCommandCompleted(correlation_id, 1, "settled", None)

    def complete(event):  # type: ignore[no-untyped-def]
        completions.append(event)
        ready.set()
        return AdmissionResult.ACCEPTED

    port = DesiredStateIoPort(
        store,
        complete=complete,
        late_result=late_result,
        capacity=1,
        retry_seconds=0.001,
    )
    request = DesiredStateIoRequest(
        "operation",
        1,
        0,
        DesiredStateOperation.RECORD_SESSION_AGENT_EFFECTS,
        args=((),),
        context=("test",),
        cost_origin=StateCostOrigin.IPC,
    )
    try:
        assert port.submit(request) is AdmissionResult.ACCEPTED
        assert ready.wait(1.0)
        event = completions[-1]
        assert isinstance(event.result, DesiredStateIoCompleted)
        assert event.result.result == "settled"
        assert event.result.error_code is None
        assert store.calls == 1
        assert polls >= 2
        assert port.command_costs.snapshot()[
            "record_session_agent_effects/ipc"
        ]["calls"] == 1
        assert port.acknowledge(request)
    finally:
        assert port.close(1.0)
