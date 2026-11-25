from __future__ import annotations

import threading
import time

from hyprial.kernel import ActorCell, AdmissionResult
from hyprial.kernel.impl.actor_runtime.effects import EffectCompleted
from hyprial.daemon.impl.correlation.bounded_cadence import BoundedCadence


def test_blocked_effect_coalesces_ticks_and_keeps_actor_responsive(actor_host) -> None:
    entered = threading.Event()
    release = threading.Event()
    observed: list[int] = []

    def effect(at_ms: int) -> int:
        observed.append(at_ms)
        entered.set()
        if at_ms == 1:
            assert release.wait(2)
        return at_ms

    lane = BoundedCadence("test-cadence-coalesce", effect, actor_host=actor_host)
    assert lane.submit(1) is AdmissionResult.ACCEPTED
    assert entered.wait(1)
    assert lane.submit(2) is AdmissionResult.ACCEPTED
    assert lane.submit(3) is AdmissionResult.ACCEPTED
    started = time.monotonic()
    assert not lane.close(0.02)
    assert time.monotonic() - started < 0.3
    assert lane.projection().closing
    assert observed == [1]
    release.set()
    assert lane.close(2)
    assert observed == [1, 3]
    assert lane.projection().coalesced >= 1
    assert lane.projection().completed == 2
    assert lane.submit(4) is AdmissionResult.CLOSED


def test_effect_failure_is_reported_and_later_tick_runs(actor_host) -> None:
    results: list[int] = []

    def effect(at_ms: int) -> int:
        results.append(at_ms)
        if at_ms == 1:
            raise ValueError("injected probe failure")
        return at_ms

    lane = BoundedCadence("test-cadence-failure", effect, actor_host=actor_host)
    assert lane.submit(1) is AdmissionResult.ACCEPTED
    deadline = time.monotonic() + 1
    while lane.projection().completed != 1 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert lane.submit(2) is AdmissionResult.ACCEPTED
    assert lane.close(2)
    assert results == [1, 2]
    assert lane.projection().failed == 1


def test_lost_accepted_completion_is_redelivered_until_actor_ack(actor_host, monkeypatch) -> None:
    original_tell = ActorCell.tell
    lost = False

    def tell(cell, command):
        nonlocal lost
        if isinstance(command, EffectCompleted) and not lost:
            lost = True
            return AdmissionResult.ACCEPTED
        return original_tell(cell, command)

    monkeypatch.setattr(ActorCell, "tell", tell)
    effects: list[int] = []
    lane = BoundedCadence(
        "test-cadence-lost-completion",
        lambda at_ms: effects.append(at_ms) or at_ms,
        actor_host=actor_host,
    )
    assert lane.submit(17) is AdmissionResult.ACCEPTED
    assert lane.close(2)
    assert lost
    assert effects == [17]
    assert lane.projection().completed == 1
