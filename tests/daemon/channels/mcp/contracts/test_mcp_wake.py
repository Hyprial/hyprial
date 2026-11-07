from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from hyprial.daemon.impl import DesiredState, InteractiveSession
from hyprial.kernel.impl.log import Logger
from hyprial.daemon.impl.mcp import WakeAttempt, WakeCoordinator, WakeDeliveryState, WakeStatus


@dataclass
class FakeStore:
    state: DesiredState

    def load(self) -> DesiredState:
        return self.state


class FakeWakeDriver:
    def __init__(self) -> None:
        self.calls = []
        self.busy = {"alice"}

    async def wake(self, command):  # type: ignore[no-untyped-def]
        self.calls.append(command)
        if command.actor in self.busy:
            return WakeAttempt(WakeStatus.BUSY, "harness session is busy")
        return WakeAttempt(WakeStatus.ACCEPTED)


def session(actor: str) -> InteractiveSession:
    return InteractiveSession(
        actor=actor,
        cwd=f"/work/{actor}",
        command=("claude", "--resume", actor),
        source="attach",
    )


@pytest.mark.anyio
async def test_wake_emits_correlated_trajectory_event_with_required_fields(
    tmp_path: Path,
) -> None:
    logger = Logger.worker(tmp_path, runtime="claude", name="alice")
    driver = FakeWakeDriver()
    driver.busy.clear()
    coordinator = WakeCoordinator(
        FakeStore(DesiredState(interactive_sessions=(session("alice"),))),
        driver,
        logger=logger,
    )
    coordinator.enqueue(
        "alice", "epoch-1:delivery-wake-1", message_id="msg-wake-1"
    )

    outcome = await coordinator.dispatch_due()

    assert outcome[0].status is WakeStatus.SIGNALLED
    record = json.loads(logger.path.read_text().splitlines()[-1])
    assert record["event"] == "wake.signalled"
    assert {
        "messageId": record["messageId"],
        "correlationId": record["correlationId"],
        "node": record["node"],
        "component": record["component"],
        "actorId": record["actorId"],
        "deliveryId": record["deliveryId"],
        "attempt": record["attempt"],
    } == {
        "messageId": "msg-wake-1",
        "correlationId": "msg-wake-1",
        "node": "wake",
        "component": "claude-worker",
        "actorId": "alice",
        "deliveryId": "epoch-1:delivery-wake-1",
        "attempt": 1,
    }
    assert isinstance(record["ts"], str)


def test_preview_is_explicitly_non_delivery_and_never_arms_a_wake() -> None:
    driver = FakeWakeDriver()
    coordinator = WakeCoordinator(
        FakeStore(DesiredState(interactive_sessions=(session("alice"),))),
        driver,
    )

    preview = coordinator.preview(
        "alice", [{"from": "agent:bob", "text": "please investigate"}]
    )

    assert "PREVIEW ONLY" in preview
    assert "harness_read" in preview
    assert "please investigate" in preview
    assert coordinator.pending_count == 0
    assert driver.calls == []


@pytest.mark.anyio
async def test_busy_session_retry_is_fair_and_does_not_starve_other_actors() -> None:
    now = 100.0
    driver = FakeWakeDriver()
    coordinator = WakeCoordinator(
        FakeStore(
            DesiredState(interactive_sessions=(session("alice"), session("bob")))
        ),
        driver,
        clock=lambda: now,
        retry_delay=lambda _attempt: 5.0,
    )
    assert coordinator.enqueue("alice", "delivery-a") is True
    assert coordinator.enqueue("alice", "delivery-a2") is True
    assert coordinator.enqueue("bob", "delivery-b") is True
    assert coordinator.enqueue("alice", "delivery-a") is False

    outcomes = await coordinator.dispatch_due()

    assert [(item.actor, item.status) for item in outcomes] == [
        ("alice", WakeStatus.BUSY),
        ("bob", WakeStatus.SIGNALLED),
    ]
    assert coordinator.pending_count == 3
    assert [call.delivery_id for call in driver.calls] == ["delivery-a", "delivery-b"]
    assert all(call.kind == "formal" for call in driver.calls)
    assert all("PREVIEW" not in call.prompt for call in driver.calls)
    coordinator.observe_read("bob", ("delivery-b",))

    now += 5.0
    driver.busy.clear()
    outcomes = await coordinator.dispatch_due()

    assert [(item.actor, item.status) for item in outcomes] == [
        ("alice", WakeStatus.SIGNALLED)
    ]
    assert coordinator.pending_count == 3
    assert (
        coordinator.delivery_state("alice", "delivery-a") == WakeDeliveryState.SIGNALLED
    )

    # A channel write is not a Claude turn or a durable delivery ACK.  The
    # second delivery for this session must not be signalled while one edge is
    # outstanding.
    outcomes = await coordinator.dispatch_due()

    assert outcomes == []
    assert [call.delivery_id for call in driver.calls] == [
        "delivery-a",
        "delivery-b",
        "delivery-a",
    ]

    coordinator.observe_read("alice", ("delivery-a", "delivery-a2"))
    assert (
        coordinator.delivery_state("alice", "delivery-a")
        == WakeDeliveryState.AWAITING_ACK
    )
    assert (
        coordinator.delivery_state("alice", "delivery-a2")
        == WakeDeliveryState.AWAITING_ACK
    )
    assert coordinator.complete("alice", "delivery-a") is True
    assert coordinator.pending_count == 2

    assert coordinator.complete("alice", "delivery-a2") is True
    assert coordinator.complete("bob", "delivery-b") is True
    assert coordinator.pending_count == 0


@pytest.mark.anyio
async def test_channel_write_without_read_or_ack_remains_outstanding_and_retries_same_id() -> (
    None
):
    now = 20.0
    driver = FakeWakeDriver()
    coordinator = WakeCoordinator(
        FakeStore(DesiredState(interactive_sessions=(session("bob"),))),
        driver,
        clock=lambda: now,
        resignal_delay=lambda _attempt: 3.0,
    )
    coordinator.enqueue("bob", "delivery-1")

    first = await coordinator.dispatch_due()

    assert first[0].status == WakeStatus.SIGNALLED
    assert coordinator.pending_count == 1
    assert (
        coordinator.delivery_state("bob", "delivery-1") == WakeDeliveryState.SIGNALLED
    )

    now += 3.0
    second = await coordinator.dispatch_due()

    assert second[0].status == WakeStatus.SIGNALLED
    assert [call.delivery_id for call in driver.calls] == ["delivery-1", "delivery-1"]
    assert coordinator.pending_count == 1


@pytest.mark.anyio
async def test_org_policy_silent_drop_never_reports_delivery_green() -> None:
    driver = FakeWakeDriver()
    coordinator = WakeCoordinator(
        FakeStore(DesiredState(interactive_sessions=(session("bob"),))), driver
    )
    coordinator.enqueue("bob", "delivery-policy-blocked")

    outcome = await coordinator.dispatch_due()

    # The provider gives no policy rejection signal. Transport acceptance is
    # therefore only SIGNALLED; durable completion remains daemon-owned.
    assert outcome[0].status == WakeStatus.SIGNALLED
    assert coordinator.pending_count == 1
    assert (
        coordinator.delivery_state("bob", "delivery-policy-blocked")
        == WakeDeliveryState.SIGNALLED
    )


@pytest.mark.anyio
async def test_read_without_terminal_ack_is_rate_limited_then_resignalled() -> None:
    now = 50.0
    driver = FakeWakeDriver()
    coordinator = WakeCoordinator(
        FakeStore(DesiredState(interactive_sessions=(session("bob"),))),
        driver,
        clock=lambda: now,
        read_retry_delay=20.0,
    )
    coordinator.enqueue("bob", "delivery-read-stalled")
    await coordinator.dispatch_due()
    coordinator.observe_read("bob", ("delivery-read-stalled",))

    now += 19.0
    assert await coordinator.dispatch_due() == []

    now += 1.0
    outcome = await coordinator.dispatch_due()
    assert outcome[0].status == WakeStatus.SIGNALLED
    assert [call.delivery_id for call in driver.calls] == [
        "delivery-read-stalled",
        "delivery-read-stalled",
    ]
    assert coordinator.pending_count == 1


@pytest.mark.anyio
async def test_channel_transport_crash_keeps_delivery_queued_for_retry() -> None:
    class CrashedDriver:
        async def wake(self, command):  # type: ignore[no-untyped-def]
            return WakeAttempt(WakeStatus.FAILED, "channel server crashed")

    coordinator = WakeCoordinator(
        FakeStore(DesiredState(interactive_sessions=(session("alice"),))),
        CrashedDriver(),
        retry_delay=lambda _attempt: 1.0,
    )
    coordinator.enqueue("alice", "delivery-crash")

    outcome = await coordinator.dispatch_due()

    assert outcome[0].status == WakeStatus.FAILED
    assert coordinator.pending_count == 1
    assert (
        coordinator.delivery_state("alice", "delivery-crash")
        == WakeDeliveryState.QUEUED
    )


@pytest.mark.anyio
async def test_unregistered_session_is_not_woken() -> None:
    driver = FakeWakeDriver()
    coordinator = WakeCoordinator(FakeStore(DesiredState()), driver)

    assert coordinator.enqueue("missing", "delivery-x") is True
    outcomes = await coordinator.dispatch_due()

    assert [(item.actor, item.status) for item in outcomes] == [
        ("missing", WakeStatus.OFFLINE)
    ]
    assert driver.calls == []
    assert coordinator.pending_count == 1
