# Decision record (file header; no inline comments by repository rule):
# - TMR cases exercise spec 8: schedule-time validation, one-shot firing,
#   cancellation, the generation fence and the fire-time classification
#   table. `send_after` accepts in ANY state of a valid serial actor, so the
#   stopping/stopped/quarantined rows are reached by scheduling AFTER the
#   transition (pending timers would have been cancelled by it).
# - Cancellation by stop acceptance / quarantine / drain start / shutdown is
#   observed as `cancel_after(timer_id) -> False` for a timer whose delay has
#   not elapsed: only a cancellation can make such a timer non-pending. Stop
#   acceptance and drain start are checked while the handler is still parked,
#   which separates them from confirmation.
# - The cancellation-vs-fire race window is the interval between stop
#   acceptance and confirmation: a timer that fires there must be
#   dead-lettered `stopped`, never silently lost (TMR-05).
# - A timer firing into a DRAINING actor is not asserted: drain closes
#   external admission (5.5) but spec 8 names no outcome for "closed while
#   draining" (reported as an ambiguity).
# - TMR-01 bounds the fire time with TOL on both sides (TOL-1) through
#   support.helpers.assert_timing (strict lower bound, best of N timers).
# - Load robustness (HQ review): timers that must stay pending until a
#   cancellation use WAIT-long delays; TMR-02's fenced timer fires a full
#   SLOW_DELAY after the expected re-entry; TMR-03's "cancel of a pending
#   timer is True" is checked with until_conclusive: a trial in which the
#   test thread reached cancel_after only after the delay had (nearly)
#   elapsed proves nothing and is repeated with a fresh timer.
"""TMR: timers (spec 5.3, 5.4, 5.5, 8, E-8)."""

from __future__ import annotations

import threading
import time

import pytest

from support.helpers import (SLOW_DELAY, TOL, WAIT, Block, Call, DeadLetters, Raise, Rec, Recorder,
                             assert_timing, case, until_conclusive, wait_until)

KIND, GEN = 0, 3
FENCED = 2 * SLOW_DELAY
CANCEL_DELAY = 0.2

def expect(h, actor, *prefix: object) -> None:
    """Wait (bounded) until the snapshot starts with ``prefix``; state and event order is not fixed."""
    assert wait_until(lambda: h.snapshot(actor)[:len(prefix)] == prefix), (h.snapshot(actor), prefix)



def park(h, actor) -> threading.Event:
    release = threading.Event()
    block = Block(release)
    assert h.tell(actor, block) == 0
    assert block.entered.wait(WAIT)
    return release


@case("TMR-01")
def test_tmr_01_timer_fires_once_after_delay(h) -> None:
    actor = h.spawn()
    recorders: list[Recorder] = []

    def attempt() -> float:
        fired = Recorder()
        recorders.append(fired)
        started = time.monotonic()
        timer_id = h.send_after(actor, 0.2, Call(lambda: fired.append(time.monotonic())))
        assert isinstance(timer_id, str)
        assert fired.wait_len(1)
        return fired.items()[0] - started

    assert_timing(attempt, 0.2, what="send_after fire time")
    time.sleep(0.15)
    assert [len(fired) for fired in recorders] == [1] * len(recorders)


@case("TMR-01")
def test_tmr_01_zero_delay_and_distinct_ids(h) -> None:
    actor = h.spawn()
    log = Recorder()
    ids = {h.send_after(actor, 0.0, Rec(log, n)) for n in range(5)}
    assert len(ids) == 5
    assert log.wait_len(5)
    assert sorted(log.items()) == [0, 1, 2, 3, 4]


@case("TMR-02")
def test_tmr_02_generation_fence_drops_stale_timer(h) -> None:
    """A gen-1 timer firing after re-entry is dropped; one firing during restart_pending is kept."""
    dead = DeadLetters()
    log = Recorder()
    actor = h.spawn(profile="slow", dead_letters=dead)
    started = time.monotonic()
    h.send_after(actor, FENCED, Rec(log, "fenced"))
    assert h.tell(actor, Raise(RuntimeError("boom"))) == 0
    assert h.events.wait_kind(actor, "restart_scheduled")
    h.send_after(actor, 0.1, Rec(log, "kept"))
    assert h.events.wait_kind(actor, "child_restarted")
    assert wait_until(lambda: h.generation(actor) == 2)
    h.send_after(actor, 0.1, Rec(log, "current"))
    assert log.wait_len(2)
    time.sleep(max(0.0, started + FENCED + 0.15 - time.monotonic()))
    assert log.items() == ["kept", "current"]
    completed = [e for e in h.events.of(actor) if e[KIND] == "command_completed"]
    assert [e[GEN] for e in completed] == [2, 2]
    assert dead.items() == []


@case("TMR-03")
def test_tmr_03_cancel_after_reports_pending_state(h) -> None:
    actor = h.spawn()

    def trial() -> bool:
        log = Recorder()
        started = time.monotonic()
        pending = h.send_after(actor, CANCEL_DELAY, Rec(log, "cancelled"))
        first = h.engine.cancel_after(pending)
        if time.monotonic() - started >= CANCEL_DELAY - TOL:
            return False
        assert first is True
        assert h.engine.cancel_after(pending) is False
        assert h.engine.cancel_after("no-such-timer") is False
        fired = h.send_after(actor, 0.0, Rec(log, "fired"))
        assert log.wait_len(1)
        assert h.engine.cancel_after(fired) is False
        time.sleep(max(0.0, started + CANCEL_DELAY + 0.1 - time.monotonic()))
        assert log.items() == ["fired"]
        return True

    until_conclusive(trial, what="cancel_after on a pending timer")


@case("TMR-04")
def test_tmr_04_full_mailbox_retries_until_admitted(h) -> None:
    dead = DeadLetters()
    log = Recorder()
    actor = h.spawn(capacity=1, dead_letters=dead)
    release = park(h, actor)
    assert h.tell(actor, Rec(log, "queued")) == 0
    assert h.tell(actor, Rec(log, "overflow")) == 1
    h.send_after(actor, 0.0, Rec(log, "timer"))
    cancelled = h.send_after(actor, 0.0, Rec(log, "cancelled"))
    time.sleep(0.1)
    assert h.snapshot(actor)[2] == 1
    assert h.engine.cancel_after(cancelled) is True
    release.set()
    assert log.wait_len(2)
    time.sleep(0.1)
    assert log.items() == ["queued", "timer"]
    assert dead.items() == []


@case("TMR-04")
def test_tmr_04_stopping_and_stopped_dead_letter_stopped(h) -> None:
    dead = DeadLetters()
    log = Recorder()
    actor = h.spawn(dead_letters=dead)
    release = park(h, actor)
    assert h.stop(actor, 0.05) is False
    while_stopping = Rec(log, "stopping")
    h.send_after(actor, 0.0, while_stopping)
    assert dead.wait_len(1)
    release.set()
    assert h.stop(actor, WAIT) is True
    while_stopped = Rec(log, "stopped")
    h.send_after(actor, 0.0, while_stopped)
    assert dead.wait_len(2)
    time.sleep(0.05)
    assert [(m is e, reason) for (m, reason), e in zip(dead.items(), (while_stopping, while_stopped))] == [
        (True, "stopped"), (True, "stopped")]
    assert len(dead) == 2
    assert log.items() == []


@case("TMR-04")
def test_tmr_04_quarantined_dead_letters_quarantined(h) -> None:
    dead = DeadLetters()
    log = Recorder()
    actor = h.spawn(profile="never", dead_letters=dead)
    assert h.tell(actor, Raise(RuntimeError("boom"))) == 0
    assert h.events.wait_kind(actor, "child_quarantined")
    expect(h, actor, "quarantined")
    message = Rec(log, "quarantined")
    h.send_after(actor, 0.0, message)
    assert dead.wait_len(1)
    assert dead.items()[0][0] is message and dead.items()[0][1] == "quarantined"
    assert log.items() == []


@case("TMR-05")
def test_tmr_05_stop_acceptance_cancels_pending_timers(h) -> None:
    log = Recorder()
    actor = h.spawn()
    release = park(h, actor)
    timer_id = h.send_after(actor, WAIT, Rec(log, "never"))
    assert h.stop(actor, 0.05) is False
    assert h.engine.cancel_after(timer_id) is False
    release.set()
    assert h.stop(actor, WAIT) is True
    assert log.items() == []


@case("TMR-05")
def test_tmr_05_quarantine_cancels_pending_timers(h) -> None:
    log = Recorder()
    actor = h.spawn(profile="never")
    timer_id = h.send_after(actor, WAIT, Rec(log, "never"))
    assert h.tell(actor, Raise(RuntimeError("boom"))) == 0
    assert h.events.wait_kind(actor, "child_quarantined")
    expect(h, actor, "quarantined")
    assert h.engine.cancel_after(timer_id) is False
    assert log.items() == []


@case("TMR-05")
def test_tmr_05_drain_start_cancels_pending_timers(h) -> None:
    log = Recorder()
    actor = h.spawn()
    release = park(h, actor)
    timer_id = h.send_after(actor, WAIT, Rec(log, "never"))
    completed, _, remaining = h.drain([actor], 0.05)
    assert completed is False and remaining == [actor.name]
    assert h.engine.cancel_after(timer_id) is False
    release.set()
    assert h.drain([actor], WAIT)[0] is True
    assert log.items() == []


@case("TMR-05")
def test_tmr_05_shutdown_cancels_pending_timers(make_harness) -> None:
    h = make_harness()
    log = Recorder()
    actor = h.spawn()
    timer_id = h.send_after(actor, WAIT, Rec(log, "never"))
    assert h.engine.shutdown(WAIT) is True
    assert h.engine.cancel_after(timer_id) is False
    assert log.items() == []


@case("TMR-05")
def test_tmr_05_fire_between_acceptance_and_confirmation_not_lost(h) -> None:
    dead = DeadLetters()
    log = Recorder()
    actor = h.spawn(dead_letters=dead)
    release = park(h, actor)
    assert h.stop(actor, 0.05) is False
    message = Rec(log, "race")
    h.send_after(actor, 0.05, message)
    assert dead.wait_len(1)
    release.set()
    assert h.stop(actor, WAIT) is True
    time.sleep(0.05)
    assert len(dead) == 1
    assert dead.items()[0][0] is message and dead.items()[0][1] == "stopped"
    assert log.items() == []


@case("TMR-06")
def test_tmr_06_schedule_time_validation(h) -> None:
    log = Recorder()
    actor = h.spawn()
    pool = h.spawn_pool()
    with pytest.raises(KeyError):
        h.engine.send_after("no-such-actor", actor.name, 0.0, Rec(log, "unknown"))
    with pytest.raises(KeyError):
        h.engine.send_after(actor.actor_id, actor.name + "-forged", 0.0, Rec(log, "forged"))
    with pytest.raises(ValueError):
        h.send_after(pool, 0.0, Rec(log, "pool"))
    with pytest.raises(ValueError):
        h.send_after(actor, -0.1, Rec(log, "negative"))
    time.sleep(0.1)
    assert log.items() == []
