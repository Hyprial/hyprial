"""RED custody contract: one accepted batch, not one prompt per unread count."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from hyprial.daemon.impl import DesiredState, InteractiveSession
from hyprial.daemon.impl.mcp import WakeAttempt, WakeCoordinator, WakeStatus
from hyprial.kernel.impl.log import Logger


@pytest.fixture(autouse=True)
def _isolated_daemon_environment(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HYPRIAL_HOME", str(home / ".hyprial"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "harness-state"))
    monkeypatch.setenv("HARNESS_SOCKET_PATH", str(tmp_path / "isolated.sock"))
    monkeypatch.setenv("HYPRIAL_NETWORK_ISOLATED", "1")


@dataclass
class _Store:
    state: DesiredState

    def load(self) -> DesiredState:
        return self.state


class _Driver:
    def __init__(self, *attempts: WakeAttempt) -> None:
        self.calls = []
        self.attempts = list(attempts)

    async def wake(self, command):  # type: ignore[no-untyped-def]
        self.calls.append(command)
        if self.attempts:
            return self.attempts.pop(0)
        return WakeAttempt(WakeStatus.ACCEPTED)


def _session() -> InteractiveSession:
    return InteractiveSession(
        actor="alice",
        cwd="/work/alice",
        command=("claude", "--resume", "alice"),
        source="claude-channel",
    )


@pytest.mark.anyio
async def test_unconsumed_signalled_batch_resignals_while_new_mail_waits(
    tmp_path: Path,
) -> None:
    now = 10.0
    driver = _Driver()
    logger = Logger.worker(tmp_path, runtime="claude", name="alice")
    coordinator = WakeCoordinator(
        _Store(DesiredState(interactive_sessions=(_session(),))),
        driver,
        logger=logger,
        clock=lambda: now,
        resignal_delay=lambda _attempt: 60.0,
    )
    first_text = "[Harness Network inbox batch cursor=c1 items=2]\nfirst"
    second_text = "[Harness Network inbox batch cursor=c2 items=1]\nlater"

    assert coordinator.enqueue_batch(
        "alice", cursor="c1", message_ids=("m1", "m2"), text=first_text
    )
    first = await coordinator.dispatch_due()
    assert first[0].status is WakeStatus.SIGNALLED
    assert driver.calls[0].prompt == first_text

    now += 1.0
    assert coordinator.enqueue_batch(
        "alice", cursor="c2", message_ids=("m3",), text=second_text
    )
    assert await coordinator.dispatch_due() == []
    assert [call.prompt for call in driver.calls] == [first_text]
    gated = json.loads(logger.path.read_text().splitlines()[-1])
    assert gated["event"] == "wake.gated"
    assert {
        "actorId": gated["actorId"],
        "cursor": gated["cursor"],
        "waitedSeconds": gated["waitedSeconds"],
    } == {
        "actorId": "alice",
        "cursor": "c1",
        "waitedSeconds": 1.0,
    }

    now += 59.0
    replay = await coordinator.dispatch_due()
    assert replay[0].status is WakeStatus.SIGNALLED
    assert [call.prompt for call in driver.calls] == [first_text, first_text]
    assert [call.delivery_id for call in driver.calls] == ["c1", "c1"]

    coordinator.observe_batch_consumed("alice", cursor="c1")
    second = await coordinator.dispatch_due()
    assert second[0].status is WakeStatus.SIGNALLED
    assert [call.prompt for call in driver.calls] == [
        first_text,
        first_text,
        second_text,
    ]


@pytest.mark.anyio
async def test_refused_edge_retries_the_same_cursor_and_text() -> None:
    now = 20.0
    driver = _Driver(
        WakeAttempt(WakeStatus.BUSY, "turn busy"),
        WakeAttempt(WakeStatus.ACCEPTED),
    )
    coordinator = WakeCoordinator(
        _Store(DesiredState(interactive_sessions=(_session(),))),
        driver,
        clock=lambda: now,
        retry_delay=lambda _attempt: 2.0,
    )
    text = "[Harness Network inbox batch cursor=retry items=1]\nbody"
    coordinator.enqueue_batch(
        "alice", cursor="retry", message_ids=("m1",), text=text
    )

    first = await coordinator.dispatch_due()
    assert first[0].status is WakeStatus.BUSY
    now += 2.0
    second = await coordinator.dispatch_due()
    assert second[0].status is WakeStatus.SIGNALLED
    assert [call.prompt for call in driver.calls] == [text, text]
    assert [call.delivery_id for call in driver.calls] == ["retry", "retry"]


@pytest.mark.anyio
async def test_awaiting_ack_recovery_replays_the_exact_consumed_batch() -> None:
    now = 30.0
    driver = _Driver()
    coordinator = WakeCoordinator(
        _Store(DesiredState(interactive_sessions=(_session(),))),
        driver,
        clock=lambda: now,
        read_retry_delay=120.0,
    )
    text = "[Harness Network inbox batch cursor=awaiting items=1]\nbody"
    coordinator.enqueue_batch(
        "alice", cursor="awaiting", message_ids=("m1",), text=text
    )
    await coordinator.dispatch_due()
    coordinator.observe_batch_consumed("alice", cursor="awaiting")

    now += 119.0
    assert await coordinator.dispatch_due() == []
    now += 1.0
    replay = await coordinator.dispatch_due()
    assert replay[0].status is WakeStatus.SIGNALLED
    assert [call.prompt for call in driver.calls] == [text, text]


@pytest.mark.anyio
async def test_older_awaiting_ack_timer_cannot_bypass_newer_unconsumed_edge() -> None:
    now = 35.0
    driver = _Driver()
    coordinator = WakeCoordinator(
        _Store(DesiredState(interactive_sessions=(_session(),))),
        driver,
        clock=lambda: now,
        read_retry_delay=120.0,
    )
    first_text = "[Harness Network inbox batch cursor=c1 items=1]\nfirst"
    second_text = "[Harness Network inbox batch cursor=c2 items=1]\nsecond"

    coordinator.enqueue_batch(
        "alice", cursor="c1", message_ids=("m1",), text=first_text
    )
    await coordinator.dispatch_due()
    coordinator.observe_batch_consumed("alice", cursor="c1")
    coordinator.enqueue_batch(
        "alice",
        cursor="c2",
        predecessor_cursor="c1",
        message_ids=("m2",),
        text=second_text,
    )
    await coordinator.dispatch_due()
    assert [call.prompt for call in driver.calls] == [first_text, second_text]

    now += 120.0
    replay = await coordinator.dispatch_due()
    assert replay[0].delivery_id == "c2"
    assert [call.prompt for call in driver.calls] == [
        first_text,
        second_text,
        second_text,
    ]

    coordinator.observe_batch_consumed("alice", cursor="c2")
    recovered = await coordinator.dispatch_due()
    assert recovered[0].status is WakeStatus.SIGNALLED
    assert [call.prompt for call in driver.calls] == [
        first_text,
        second_text,
        second_text,
        first_text,
    ]


@pytest.mark.anyio
async def test_generation_rearm_and_restart_recovery_each_queue_one_batch() -> None:
    now = 40.0
    text = "[Harness Network inbox batch cursor=recover items=2]\nbody"
    driver = _Driver()
    coordinator = WakeCoordinator(
        _Store(DesiredState(interactive_sessions=(_session(),))),
        driver,
        clock=lambda: now,
    )
    coordinator.enqueue_batch(
        "alice", cursor="recover", message_ids=("m1", "m2"), text=text
    )
    await coordinator.dispatch_due()

    assert coordinator.rearm_batch(
        "alice", cursor="recover", generation="generation-2"
    )
    await coordinator.dispatch_due()
    assert not coordinator.rearm_batch(
        "alice", cursor="recover", generation="generation-2"
    )
    assert [call.prompt for call in driver.calls] == [text, text]

    restarted_driver = _Driver()
    restarted = WakeCoordinator(
        _Store(DesiredState(interactive_sessions=(_session(),))),
        restarted_driver,
        clock=lambda: now,
    )
    assert restarted.recover_batch(
        "alice",
        cursor="recover",
        message_ids=("m1", "m2"),
        text=text,
        generation="generation-3",
    )
    recovered = await restarted.dispatch_due()
    assert recovered[0].status is WakeStatus.SIGNALLED
    assert [call.prompt for call in restarted_driver.calls] == [text]


def test_batch_members_settle_individually_without_retiring_the_cursor_early() -> None:
    coordinator = WakeCoordinator(
        _Store(DesiredState(interactive_sessions=(_session(),))), _Driver()
    )
    coordinator.enqueue_batch(
        "alice", cursor="c1", message_ids=("m1", "m2"), text="batch"
    )

    assert coordinator.complete("alice", "m1") is True
    assert coordinator.batch_members("alice", cursor="c1") == ("m2",)
    assert coordinator.complete("alice", "m2") is True
    assert coordinator.batch_members("alice", cursor="c1") == ()
