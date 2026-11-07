from __future__ import annotations

import threading

import pytest

from hyprial.kernel.impl.actor_runtime import ActorRuntime, AdmissionResult
from hyprial.kernel.impl.actor_runtime.effects import EffectCompleted
from hyprial.daemon.impl.network.routing.session_route_coordinator import (
    SessionRouteCoordinator,
    SessionRouteKind,
)


def test_same_resource_stays_fifo_while_unrelated_session_progresses() -> None:
    entered = threading.Event()
    release = threading.Event()
    unrelated_done = threading.Event()
    same_resource_started = threading.Event()
    operations: list[tuple[SessionRouteKind, str]] = []

    def apply(command: object) -> str:
        kind = command.kind  # type: ignore[attr-defined]
        actor = command.actor  # type: ignore[attr-defined]
        operations.append((kind, actor))
        if kind is SessionRouteKind.HEARTBEAT and actor == "agent-a":
            entered.set()
            assert release.wait(2)
        elif actor == "agent-a":
            same_resource_started.set()
        elif actor == "agent-b":
            unrelated_done.set()
        return kind.value

    owner = SessionRouteCoordinator(apply, capacity=4)
    with pytest.raises(TimeoutError, match="accepted but still pending"):
        owner.call(
            SessionRouteKind.HEARTBEAT,
            actor="agent-a",
            session_ref="session-a",
            timeout=0.02,
        )
    assert entered.wait(1)
    admission, _id = owner.request(
        SessionRouteKind.REFRESH,
        actor="agent-a",
        session_ref="session-a",
    )
    assert admission is AdmissionResult.ACCEPTED
    admission, _id = owner.request(
        SessionRouteKind.HEARTBEAT,
        actor="agent-b",
        session_ref="session-b",
    )
    assert admission is AdmissionResult.ACCEPTED
    assert unrelated_done.wait(1)
    assert not same_resource_started.is_set()
    assert not owner.close(0.02)
    release.set()
    assert owner.close(2)
    assert operations == [
        (SessionRouteKind.HEARTBEAT, "agent-a"),
        (SessionRouteKind.HEARTBEAT, "agent-b"),
        (SessionRouteKind.REFRESH, "agent-a"),
    ]
    assert owner.projection().completed == 3


def test_effect_lane_overload_reports_failure_for_accepted_request() -> None:
    entered = threading.Event()
    release = threading.Event()

    def apply(command: object) -> str:
        entered.set()
        assert release.wait(2)
        return "ok"

    owner = SessionRouteCoordinator(apply, capacity=1)
    admission, _id = owner.request(SessionRouteKind.ENSURE, actor="agent-a")
    assert admission is AdmissionResult.ACCEPTED
    assert entered.wait(1)
    admission, _id = owner.request(SessionRouteKind.DROP, actor="agent-a")
    assert admission is AdmissionResult.OVERLOADED
    assert owner.projection().overloaded == 1
    release.set()
    assert owner.close(2)


def test_lost_accepted_completion_does_not_repeat_session_effect() -> None:
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
    operations: list[SessionRouteKind] = []
    owner = SessionRouteCoordinator(
        lambda command: operations.append(command.kind) or "settled",
        runtime=runtime,
    )
    assert owner.call(SessionRouteKind.HEARTBEAT) == "settled"
    assert owner.close(2)
    assert runtime.lost
    assert operations == [SessionRouteKind.HEARTBEAT]
    assert owner.projection().completed == 1
