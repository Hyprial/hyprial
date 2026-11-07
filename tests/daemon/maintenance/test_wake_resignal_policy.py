"""State-dependent renotify policy: read custody silences the wake edge.

Production observed one message pushed five times because the SIGNALLED and
failure-retry cadences shared a 1s-start curve, and because AWAITING_ACK kept
resignalling after ``harness_read`` had already proven the session saw the
delivery. These tests pin the replacement policy with the production-default
delays and an injected clock.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from hyprial.daemon.impl import DesiredState, InteractiveSession
from hyprial.daemon.impl.mcp import WakeAttempt, WakeCoordinator, WakeDeliveryState, WakeStatus

LONG_FALLBACK = 600.0


@dataclass
class FakeStore:
    state: DesiredState

    def load(self) -> DesiredState:
        return self.state


class FakeWakeDriver:
    def __init__(self) -> None:
        self.calls = []

    async def wake(self, command):  # type: ignore[no-untyped-def]
        self.calls.append(command)
        return WakeAttempt(WakeStatus.ACCEPTED)


def session(actor: str) -> InteractiveSession:
    return InteractiveSession(
        actor=actor,
        cwd=f"/work/{actor}",
        command=("claude", "--resume", actor),
        source="attach",
    )


class Clock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def coordinator_with_defaults(driver: FakeWakeDriver, clock: Clock) -> WakeCoordinator:
    """Production-default delays; only the clock is injected."""

    return WakeCoordinator(
        FakeStore(DesiredState(interactive_sessions=(session("bob"),))),
        driver,
        clock=clock,
    )


@pytest.mark.anyio
async def test_read_before_ack_suppresses_every_resignal_inside_long_window() -> None:
    """Failure path 1: after harness_read, no renotify until the long timeout.

    The session demonstrably saw the delivery; any resignal before the
    10-minute fallback window elapses is pure noise. Sweep the whole window in
    1s steps so no intermediate cadence (the old 30s read_retry_delay, the
    signalled curve) can fire without failing this test.
    """

    clock = Clock(1000.0)
    driver = FakeWakeDriver()
    coordinator = coordinator_with_defaults(driver, clock)
    coordinator.enqueue("bob", "delivery-quiet")

    first = await coordinator.dispatch_due()
    assert first[0].status == WakeStatus.SIGNALLED
    assert len(driver.calls) == 1

    coordinator.observe_read("bob", ("delivery-quiet",))
    read_at = clock.now

    while clock.now < read_at + LONG_FALLBACK - 1.0:
        clock.now += 1.0
        assert await coordinator.dispatch_due() == []

    assert len(driver.calls) == 1
    assert (
        coordinator.delivery_state("bob", "delivery-quiet")
        == WakeDeliveryState.AWAITING_ACK
    )
    assert coordinator.pending_count == 1


@pytest.mark.anyio
async def test_read_owned_head_does_not_block_a_newer_delivery() -> None:
    """A read-owned head stays quiet while newer mail wakes promptly."""

    clock = Clock(1000.0)
    driver = FakeWakeDriver()
    coordinator = coordinator_with_defaults(driver, clock)
    coordinator.enqueue("bob", "delivery-a")

    first = await coordinator.dispatch_due()
    assert [item.delivery_id for item in first] == ["delivery-a"]
    coordinator.observe_read("bob", ("delivery-a",))
    read_at = clock.now

    clock.now += 120.0
    coordinator.enqueue("bob", "delivery-b")
    newer = await coordinator.dispatch_due()

    assert [item.delivery_id for item in newer] == ["delivery-b"]
    assert newer[0].status == WakeStatus.SIGNALLED
    assert "1 pending delivery" in driver.calls[-1].prompt
    coordinator.observe_read("bob", ("delivery-b",))

    clock.now = read_at + LONG_FALLBACK - 1.0
    assert await coordinator.dispatch_due() == []
    clock.now += 1.0
    fallback = await coordinator.dispatch_due()

    assert [item.delivery_id for item in fallback] == ["delivery-a"]
    assert [call.delivery_id for call in driver.calls] == [
        "delivery-a",
        "delivery-b",
        "delivery-a",
    ]


@pytest.mark.anyio
async def test_dead_session_after_read_is_renotified_by_long_timeout_fallback() -> None:
    """Failure path 2: session dies between read and ack — the fallback fires.

    This is the reason the long timeout exists at all: read custody without a
    terminal reply/ack must eventually resignal the same delivery_id, or a
    crashed session strands the message forever. The delivery must also stay
    on the long window afterwards instead of rejoining the aggressive
    signalled cadence.
    """

    clock = Clock(50.0)
    driver = FakeWakeDriver()
    coordinator = coordinator_with_defaults(driver, clock)
    coordinator.enqueue("bob", "delivery-stranded")
    await coordinator.dispatch_due()
    coordinator.observe_read("bob", ("delivery-stranded",))

    # Session dies here; nobody replies or acks. The long timeout elapses.
    clock.now += LONG_FALLBACK
    fallback = await coordinator.dispatch_due()

    assert [item.delivery_id for item in fallback] == ["delivery-stranded"]
    assert fallback[0].status == WakeStatus.SIGNALLED
    assert [call.delivery_id for call in driver.calls] == [
        "delivery-stranded",
        "delivery-stranded",
    ]

    # The fallback nudge does not demote the delivery to the signalled
    # cadence: it stays AWAITING_ACK and the next nudge is another long
    # window away, not 30s.
    assert (
        coordinator.delivery_state("bob", "delivery-stranded")
        == WakeDeliveryState.AWAITING_ACK
    )
    clock.now += LONG_FALLBACK / 2
    assert await coordinator.dispatch_due() == []
    clock.now += LONG_FALLBACK / 2
    third = await coordinator.dispatch_due()
    assert [item.delivery_id for item in third] == ["delivery-stranded"]
    assert len(driver.calls) == 3

    assert coordinator.complete("bob", "delivery-stranded") is True
    assert coordinator.pending_count == 0
    clock.now += LONG_FALLBACK
    assert await coordinator.dispatch_due() == []
    assert len(driver.calls) == 3


@pytest.mark.anyio
async def test_signalled_backoff_starts_at_30s_doubles_and_caps() -> None:
    """SIGNALLED without read renotifies at 30/60/120/240 then a 300s cap.

    Sweeping second-by-second pins the exact production cadence; under the old
    1s-start shared curve the same sweep would show five pushes inside 16s,
    which is the observed production regression.
    """

    clock = Clock(0.0)
    driver = FakeWakeDriver()
    coordinator = coordinator_with_defaults(driver, clock)
    coordinator.enqueue("bob", "delivery-unread")

    signal_times: list[float] = []
    horizon = 1100.0
    while clock.now <= horizon:
        for outcome in await coordinator.dispatch_due():
            assert outcome.status == WakeStatus.SIGNALLED
            signal_times.append(clock.now)
        clock.now += 1.0

    assert signal_times == [0.0, 30.0, 90.0, 210.0, 450.0, 750.0, 1050.0]
    assert coordinator.pending_count == 1
