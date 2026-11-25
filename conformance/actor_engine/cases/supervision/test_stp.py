# Decision record (file header; no inline comments by repository rule):
# - STP cases follow the ordered stop definition of spec 5.4: acceptance
#   exactly once, current handler first, confirmation, dead letters, then
#   child_stopped; repeated/concurrent calls join with their own timeout.
# - "No re-acceptance" is observed through timers: acceptance cancels every
#   pending timer (5.4 step 2), so a timer scheduled after the first
#   acceptance must still be pending (cancel_after -> True) after a repeated
#   stop call.
# - Self-stop calls inside handlers pass a long timeout (WAIT); a blocking
#   implementation would deadlock until that timeout, so "non-blocking" is
#   asserted as returning True well before it (bounded at 1 s, far below
#   WAIT and far above scheduling noise).
# - 5.4 step 3 holds for both normal outcomes of the handler executing at
#   acceptance: STP-02 also stops while that handler raises the expected
#   error, so command_rejected precedes the dead letters and child_stopped,
#   with no restart or budget effect (6.2).
# - TOL clauses (stop on a stopped actor returns at once; a joining stop with
#   its own 0.1 s timeout) go through support.helpers.assert_timing; both are
#   repeatable on the same actor without side effects. Timers that must stay
#   pending use WAIT-long delays and restart_pending windows SLOW_DELAY, so a
#   loaded host cannot fire them early relative to the test thread.
# - "Endpoint gone" (5.2) has no further definition in the spec; STP-05 reads
#   it as the actor's engine having been shut down.
"""STP: stop ordering, self-stop, stop during restart, reset refusal (spec 5.2, 5.4, 6.4)."""

from __future__ import annotations

import threading
import time

import pytest

from support.helpers import (SLOW_DELAY, WAIT, Actor, Block, Call, DeadLetters, Expected, Raise, Rec,
                             Recorder, assert_timing, case, wait_until)

KIND, GEN = 0, 3
NON_BLOCKING = 1.0

def expect(h, actor, *prefix: object) -> None:
    """Wait (bounded) until the snapshot starts with ``prefix``; state and event order is not fixed."""
    assert wait_until(lambda: h.snapshot(actor)[:len(prefix)] == prefix), (h.snapshot(actor), prefix)



class Timeline(Recorder):
    """One ordered record of dead letters and per-actor events."""

    def dead(self, message: object, reason: str) -> None:
        self.append(("dead", message, reason))

    def event(self, event: tuple) -> None:
        self.append(("event", event[KIND], event))

    def kinds(self) -> list[str]:
        return [entry[1] for entry in self.items() if entry[0] == "event"]


def park(h, actor) -> threading.Event:
    release = threading.Event()
    block = Block(release)
    assert h.tell(actor, block) == 0
    assert block.entered.wait(WAIT)
    return release


def in_thread(fn) -> tuple[threading.Thread, list]:
    result: list = []
    thread = threading.Thread(target=lambda: result.append(fn()), daemon=True)
    thread.start()
    return thread, result


def spawn_timeline(h, name: str, profile: str = "fast") -> tuple[Actor, Timeline]:
    timeline = Timeline()
    actor_id = h.engine.spawn(name, h.factory(), 16, profile, timeline.dead, timeline.event)
    return Actor(actor_id, name), timeline


@case("STP-01")
def test_stp_01_stop_then_stop_on_stopped(h) -> None:
    actor = h.spawn()
    assert h.stop(actor, WAIT) is True

    def attempt() -> float:
        started = time.monotonic()
        assert h.stop(actor, WAIT) is True
        assert h.stop(actor, 0.0) is True
        return time.monotonic() - started

    assert_timing(attempt, 0.0, what="stop on a stopped actor")
    assert h.events.wait_kind(actor, "child_stopped")
    time.sleep(0.05)
    stopped = [e for e in h.events.of(actor) if e[KIND] == "child_stopped"]
    assert len(stopped) == 1 and stopped[0][GEN] == 1
    expect(h, actor, "stopped", 1, 0, 0)


@case("STP-01")
def test_stp_01_stop_keeps_generation_after_restart(h) -> None:
    actor = h.spawn(profile="fast")
    assert h.tell(actor, Raise(RuntimeError("boom"))) == 0
    assert h.events.wait_kind(actor, "child_restarted")
    assert wait_until(lambda: h.generation(actor) == 2)
    assert h.stop(actor, WAIT) is True
    assert h.events.wait_kind(actor, "child_stopped")
    stopped = [e for e in h.events.of(actor) if e[KIND] == "child_stopped"]
    assert [e[GEN] for e in stopped] == [2]
    expect(h, actor, "stopped", 2)


@case("STP-01")
def test_stp_01_concurrent_stops_join_with_own_timeout(h) -> None:
    dead = DeadLetters()
    log = Recorder()
    actor = h.spawn(dead_letters=dead)
    release = park(h, actor)
    queued = Rec(log, "queued")
    assert h.tell(actor, queued) == 0
    first, first_result = in_thread(lambda: h.stop(actor, WAIT))
    assert h.wait_state(actor, "stopping")
    pending_timer = h.send_after(actor, WAIT, Rec(log, "timer"))

    def attempt() -> float:
        started = time.monotonic()
        assert h.stop(actor, 0.1) is False
        return time.monotonic() - started

    assert_timing(attempt, 0.1, what="joining stop with its own timeout")
    assert h.engine.cancel_after(pending_timer) is True
    assert first.is_alive()
    second, second_result = in_thread(lambda: h.stop(actor, WAIT))
    release.set()
    first.join(WAIT + 1)
    second.join(WAIT + 1)
    assert first_result == [True] and second_result == [True]
    assert dead.wait_len(1)
    time.sleep(0.05)
    assert len(dead) == 1 and dead.items()[0][0] is queued and dead.items()[0][1] == "stopped"
    assert h.events.kinds(actor).count("child_stopped") == 1
    expect(h, actor, "stopped", 1)
    assert log.items() == []


@case("STP-02")
def test_stp_02_self_stop_first_is_non_blocking(h) -> None:
    actor, timeline = spawn_timeline(h, "stp02-self-first")
    go = threading.Event()
    inner = Recorder()
    log = Recorder()

    def body() -> str:
        go.wait(WAIT)
        started = time.monotonic()
        accepted = h.engine.stop(actor.actor_id, actor.name, WAIT)
        inner.append(("stop", accepted, time.monotonic() - started))
        inner.append(("tell", h.engine.tell(actor.actor_id, actor.name, Rec(log, "after-self-stop"))))
        inner.append(("state", h.state(actor)))
        return "done"

    assert h.tell(actor, Call(body)) == 0
    queued = Rec(log, "queued")
    assert h.tell(actor, queued) == 0
    go.set()
    assert h.events.wait_kind(actor, "child_stopped")
    assert timeline.wait_len(3)
    (_, accepted, elapsed), tell_result, state = inner.items()
    assert accepted is True and elapsed < NON_BLOCKING
    assert tell_result == ("tell", 2)
    assert state == ("state", "stopping")
    entries = timeline.items()
    assert entries[0][:2] == ("event", "command_completed")
    assert entries[1][0] == "dead" and entries[1][1] is queued and entries[1][2] == "stopped"
    assert entries[2][:2] == ("event", "child_stopped")
    time.sleep(0.05)
    assert timeline.kinds().count("child_stopped") == 1
    assert len(timeline) == 3
    assert h.stop(actor, WAIT) is True
    assert log.items() == []


@case("STP-02")
def test_stp_02_external_stop_first_then_self_stop_joins(h) -> None:
    actor, timeline = spawn_timeline(h, "stp02-external-first")
    gate = threading.Event()
    entered = threading.Event()
    inner = Recorder()
    log = Recorder()

    def body() -> str:
        entered.set()
        gate.wait(WAIT)
        started = time.monotonic()
        joined = h.engine.stop(actor.actor_id, actor.name, WAIT)
        inner.append((joined, time.monotonic() - started))
        return "done"

    assert h.tell(actor, Call(body)) == 0
    assert entered.wait(WAIT)
    queued = Rec(log, "queued")
    assert h.tell(actor, queued) == 0
    external, external_result = in_thread(lambda: h.stop(actor, WAIT))
    assert h.wait_state(actor, "stopping")
    gate.set()
    external.join(WAIT + 1)
    assert not external.is_alive()
    assert external_result == [True]
    assert inner.wait_len(1)
    joined, elapsed = inner.items()[0]
    assert joined is True and elapsed < NON_BLOCKING
    assert timeline.wait_len(3)
    entries = timeline.items()
    assert entries[0][:2] == ("event", "command_completed")
    assert entries[1][0] == "dead" and entries[1][1] is queued and entries[1][2] == "stopped"
    assert entries[2][:2] == ("event", "child_stopped")
    time.sleep(0.05)
    assert timeline.kinds().count("child_stopped") == 1
    assert len(timeline) == 3
    assert log.items() == []


@case("STP-02")
def test_stp_02_handler_stopping_other_actor_waits_for_confirmation(h) -> None:
    caller = h.spawn()
    target = h.spawn()
    inner = Recorder()

    def body() -> None:
        inner.append(h.engine.stop(target.actor_id, target.name, WAIT))

    assert h.tell(caller, Call(body)) == 0
    assert inner.wait_len(1)
    assert inner.items() == [True]
    expect(h, target, "stopped")
    assert h.events.kinds(target) == ["child_stopped"]
    expect(h, caller, "running")


@case("STP-02")
def test_stp_02_stop_while_current_handler_rejects_expected_error(h) -> None:
    actor, timeline = spawn_timeline(h, "stp02-rejecting")
    entered = threading.Event()
    release = threading.Event()
    log = Recorder()

    def body() -> None:
        entered.set()
        release.wait(WAIT)
        raise Expected("E_X", "no")

    assert h.tell(actor, Call(body)) == 0
    assert entered.wait(WAIT)
    queued = Rec(log, "queued")
    assert h.tell(actor, queued) == 0
    thread, result = in_thread(lambda: h.stop(actor, WAIT))
    try:
        assert h.wait_state(actor, "stopping")
    finally:
        release.set()
    thread.join(WAIT + 1)
    assert result == [True]
    assert timeline.wait_len(3)
    time.sleep(0.05)
    entries = timeline.items()
    assert len(entries) == 3
    assert entries[0][:2] == ("event", "command_rejected") and entries[0][2][GEN] == 1
    assert entries[1][0] == "dead" and entries[1][1] is queued and entries[1][2] == "stopped"
    assert entries[2][:2] == ("event", "child_stopped") and entries[2][2][GEN] == 1
    assert "child_failed" not in timeline.kinds() and "restart_scheduled" not in timeline.kinds()
    expect(h, actor, "stopped", 1, 0, 0, 0)
    assert log.items() == []


@case("STP-03")
def test_stp_03_stop_during_restart_pending_cancels_restart(h) -> None:
    dead = DeadLetters()
    log = Recorder()
    actor = h.spawn(profile="slow", dead_letters=dead)
    assert h.tell(actor, Raise(RuntimeError("boom"))) == 0
    assert h.events.wait_kind(actor, "restart_scheduled")
    expect(h, actor, "restart_pending")
    kept = Rec(log, "kept")
    assert h.tell(actor, kept) == 0
    assert h.stop(actor, WAIT) is True
    assert h.events.wait_kind(actor, "child_stopped")
    time.sleep(SLOW_DELAY + 0.15)
    assert h.events.kinds(actor) == ["child_failed", "restart_scheduled", "child_stopped"]
    expect(h, actor, "stopped", 1)
    assert [e[GEN] for e in h.events.of(actor)] == [1, 1, 1]
    assert len(dead) == 1 and dead.items()[0][0] is kept and dead.items()[0][1] == "stopped"
    assert log.items() == []


@case("STP-03")
def test_stp_03_cancelled_restart_never_calls_factory(hp) -> None:
    builds = Recorder()
    actor = hp.spawn(profile="slow", factory=hp.factory(builds))
    assert hp.tell(actor, Raise(RuntimeError("boom"))) == 0
    assert hp.events.wait_kind(actor, "restart_scheduled")
    assert hp.stop(actor, WAIT) is True
    time.sleep(SLOW_DELAY + 0.15)
    assert len(builds) == 1
    assert "child_restarted" not in hp.events.kinds(actor)


@case("STP-04")
def test_stp_04_reset_rejects_running_restart_pending_and_forged(h) -> None:
    running = h.spawn()
    assert h.reset(running) is False
    expect(h, running, "running", 1)
    pending = h.spawn(profile="slow")
    assert h.tell(pending, Raise(RuntimeError("boom"))) == 0
    assert h.events.wait_kind(pending, "restart_scheduled")
    assert h.reset(pending) is False
    expect(h, pending, "restart_pending")
    assert h.events.wait_kind(pending, "child_restarted")
    assert h.events.kinds(pending) == ["child_failed", "restart_scheduled", "child_restarted"]
    assert h.engine.reset("no-such-actor", running.name) is False
    assert h.engine.reset(running.actor_id, running.name + "-forged") is False
    expect(h, running, "running", 1)
    assert h.events.kinds(running) == []


@case("STP-04")
def test_stp_04_reset_rejects_stopping_and_stopped(h) -> None:
    actor = h.spawn()
    release = park(h, actor)
    assert h.stop(actor, 0.05) is False
    assert h.reset(actor) is False
    assert h.state(actor) == "stopping"
    release.set()
    assert h.stop(actor, WAIT) is True
    assert h.reset(actor) is False
    expect(h, actor, "stopped", 1)
    assert h.events.kinds(actor) == ["command_completed", "child_stopped"]


def assert_closed(h, actor) -> None:
    assert h.tell(actor, Rec(Recorder(), "x")) == 2
    with pytest.raises(h.module.ActorAdmissionError) as info:
        h.ask(actor, Rec(Recorder(), "x"), WAIT)
    assert "closed" in str(info.value)


@case("STP-05")
def test_stp_05_tell_and_ask_closed_while_stopping_and_stopped(h) -> None:
    actor = h.spawn()
    release = park(h, actor)
    assert h.stop(actor, 0.05) is False
    assert h.state(actor) == "stopping"
    assert_closed(h, actor)
    release.set()
    assert h.stop(actor, WAIT) is True
    expect(h, actor, "stopped")
    assert_closed(h, actor)


@case("STP-05")
def test_stp_05_tell_and_ask_closed_when_endpoint_gone(make_harness) -> None:
    h = make_harness()
    actor = h.spawn()
    assert h.engine.shutdown(WAIT) is True
    assert_closed(h, actor)
