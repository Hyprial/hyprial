from __future__ import annotations

import time

from hyprial.kernel.impl.actor_runtime import ActorRuntime, AdmissionResult
from hyprial.kernel.impl.actor_runtime.effects import EffectCompleted
from hyprial.daemon.impl.ipc.ipc_request_owner import IpcRequestOwner


def test_request_remains_in_custody_after_lost_accepted_completion() -> None:
    class LostOnce(ActorRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.lost = False

        def tell(self, handle, command):  # type: ignore[no-untyped-def]
            if isinstance(command, EffectCompleted) and not self.lost:
                self.lost = True
                return AdmissionResult.ACCEPTED
            return super().tell(handle, command)

    runtime = LostOnce()
    owner = IpcRequestOwner(capacity=2, runtime=runtime)
    request = owner.start("session.heartbeat")
    assert request is not None
    assert owner.complete(request, None) is AdmissionResult.ACCEPTED
    assert owner.close(2)
    assert runtime.lost
    assert owner.projection().accepted == 1
    assert owner.projection().completed == 1
    assert owner.projection().active == 0


def test_request_high_water_survives_after_requests_complete() -> None:
    owner = IpcRequestOwner(capacity=2)
    first = owner.start("message.send")
    second = owner.start("message.status")
    assert first is not None and second is not None
    deadline = time.monotonic() + 2
    while owner.projection().active < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert owner.projection().high_water == 2
    assert owner.complete(first, None) is AdmissionResult.ACCEPTED
    assert owner.complete(second, None) is AdmissionResult.ACCEPTED
    assert owner.close(2)
    assert owner.projection().active == 0
    assert owner.projection().high_water == 2


def test_unsettled_request_prevents_domain_teardown() -> None:
    owner = IpcRequestOwner(capacity=1)
    request = owner.start("message.send")
    assert request is not None
    assert not owner.close(0.02)
    assert owner.complete(request, "INJECTED_FAILURE") is AdmissionResult.ACCEPTED
    assert owner.close(2)
    assert owner.projection().failed == 1
