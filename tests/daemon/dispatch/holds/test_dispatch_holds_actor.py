from __future__ import annotations

import threading
from dataclasses import dataclass

from hyprial.kernel import AdmissionResult
from hyprial.daemon.impl.dispatch.runtime.holds import HoldRefreshCoordinator
from hyprial.daemon.impl.dispatch.runtime.state import (
    DispatchOwnedMap,
    DispatchStateAuthority,
    DispatchTable,
)


@dataclass(frozen=True)
class Hold:
    worker: str
    at_ms: int


def test_late_hold_completion_cannot_resurrect_settled_delivery() -> None:
    entered = threading.Event()
    release = threading.Event()

    def refresh(_message_id: str) -> bool:
        entered.set()
        assert release.wait(2)
        return True

    state = DispatchStateAuthority()
    holds = DispatchOwnedMap[Hold](state, DispatchTable.QUEUED_HOLDS)
    original = Hold("worker", 1)
    holds["message-1"] = original
    owner = HoldRefreshCoordinator(refresh, state)
    assert owner.submit(
        "message-1", expected=original, replacement=Hold("worker", 2)
    ) is AdmissionResult.ACCEPTED
    assert entered.wait(1)
    holds.pop("message-1")  # terminal result settled while disk I/O was held
    assert not owner.close(0.02)
    release.set()
    assert owner.close(2)
    assert holds.get("message-1") is None
    assert state.close(2)


def test_successful_hold_refresh_publishes_only_matching_generation() -> None:
    state = DispatchStateAuthority()
    holds = DispatchOwnedMap[Hold](state, DispatchTable.QUEUED_HOLDS)
    owner = HoldRefreshCoordinator(lambda _message_id: True, state)
    assert owner.submit(
        "message-2", expected=None, replacement=Hold("worker", 3)
    ) is AdmissionResult.ACCEPTED
    assert owner.close(2)
    assert holds.get("message-2") == Hold("worker", 3)
    assert state.close(2)
