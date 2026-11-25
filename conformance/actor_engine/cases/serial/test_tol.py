# Decision record (file header; no inline comments by repository rule):
# - TOL-1 (3.2): timeouts and delays are relative seconds on a monotonic clock
#   and every timing clause is held to +-50 ms (support.helpers.TOL). Each
#   timed primitive is measured once against the same window: ask timeout,
#   stop timeout, drain timeout (wall clock AND the reported elapsed_s),
#   send_after delay and the restart backoff delay. Lower bounds matter as
#   much as upper bounds: a primitive must not give up or fire early.
# - The restart delay is measured from the instant the failing command raises
#   (recorded by the command) to the child_restarted event (recorded with a
#   timestamp by a spec-level sink); it uses a custom jitter-free profile.
# - TOL-2 (3.2, 13): a deadline <= 0 (zero AND negative) is checked exactly
#   once with no waiting: the call returns its "not yet" outcome in well under
#   TOL. Only states where the outcome is determined are probed (a blocked
#   handler), never a race against an idle actor. A zero delay timer is valid
#   (8 rejects only negative delays) and fires without waiting.
# - Every interval goes through support.helpers.assert_timing (strict lower
#   bound, control-overshoot upper bound, best of N fresh trials), so host
#   load cannot fail a correct engine while TOL itself stays +-50 ms. Each
#   trial builds its own blocked actor; the blocked TOL-2 actor gets room for
#   the asks every retried trial may leave queued.
"""TOL: timing tolerance and non-positive deadlines (spec 3.2, 13)."""

from __future__ import annotations

import threading
import time

import pytest

from support.helpers import POLICIES, WAIT, Block, Call, Raise, Recorder, assert_timing, case

KINDS = pytest.mark.parametrize("kind", ["python", "native"], ids=["py-handler", "native-handler"])
SPAN = 0.1


def _blocked(h, **spawn):
    release = threading.Event()
    blocker = Block(release)
    actor = h.spawn(**spawn)
    assert h.tell(actor, blocker) == 0
    assert blocker.entered.wait(WAIT)
    return actor, release


@case("TOL-1")
def test_tol_01_ask_timeout_within_tolerance(h) -> None:
    def attempt() -> float:
        actor, release = _blocked(h)
        try:
            started = time.monotonic()
            with pytest.raises(TimeoutError):
                h.ask(actor, "queued", SPAN)
            return time.monotonic() - started
        finally:
            release.set()

    assert_timing(attempt, SPAN, what="ask timeout")


@case("TOL-1")
def test_tol_01_stop_timeout_within_tolerance(h) -> None:
    actors = []

    def attempt() -> float:
        actor, release = _blocked(h)
        actors.append(actor)
        try:
            started = time.monotonic()
            assert h.stop(actor, SPAN) is False
            return time.monotonic() - started
        finally:
            release.set()

    assert_timing(attempt, SPAN, what="stop timeout")
    assert all(h.wait_state(actor, "stopped") for actor in actors)


@case("TOL-1")
def test_tol_01_drain_timeout_within_tolerance(h) -> None:
    def attempt() -> tuple[float, float]:
        actor, release = _blocked(h)
        try:
            started = time.monotonic()
            completed, elapsed, remaining = h.drain([actor], SPAN)
            wall = time.monotonic() - started
            assert (completed, remaining) == (False, [actor.name])
            assert elapsed <= wall
            return wall, elapsed
        finally:
            release.set()

    assert_timing(attempt, SPAN, what="drain timeout (wall, reported elapsed_s)")


@case("TOL-1")
def test_tol_01_send_after_delay_within_tolerance(h) -> None:
    actor = h.spawn()

    def attempt() -> float:
        fired = Recorder()
        started = time.monotonic()
        h.send_after(actor, SPAN, Call(lambda: fired.append(time.monotonic())))
        assert fired.wait_len(1)
        return fired.items()[0] - started

    assert_timing(attempt, SPAN, what="send_after delay")


@case("TOL-1")
@KINDS
def test_tol_01_restart_delay_within_tolerance(make_harness, kind: str) -> None:
    h = make_harness(kind=kind, policies={**POLICIES, "timed": (3, 60.0, SPAN, SPAN, 0.0)})

    def attempt() -> float:
        raised = Recorder()
        restarted = Recorder()

        def record(event: tuple) -> None:
            if event[0] == "child_restarted":
                restarted.append(time.monotonic())

        def fail() -> None:
            raised.append(time.monotonic())
            raise RuntimeError("timed failure")

        actor = h.spawn(profile="timed", event_sink=record)
        assert h.tell(actor, Call(fail)) == 0
        assert restarted.wait_len(1)
        restart_delays = [e[7] for e in h.events.of(actor) if e[0] == "restart_scheduled"]
        assert restart_delays == [pytest.approx(SPAN, abs=1e-9)]
        return restarted.items()[0] - raised.items()[0]

    assert_timing(attempt, SPAN, what="restart backoff delay")


@case("TOL-2")
def test_tol_02_ask_non_positive_timeout_checks_once(h) -> None:
    actor, release = _blocked(h, capacity=64)
    try:
        for timeout in (0, 0.0, -1.0):
            def attempt(timeout: float = timeout) -> float:
                started = time.monotonic()
                with pytest.raises(TimeoutError):
                    h.ask(actor, "never-waited-for", timeout)
                return time.monotonic() - started

            assert_timing(attempt, 0.0, what=f"ask(timeout={timeout!r})")
    finally:
        release.set()
    assert h.ask(actor, "after") == "after"


@case("TOL-2")
def test_tol_02_stop_non_positive_timeout_checks_once(h) -> None:
    actor, release = _blocked(h)
    try:
        for timeout in (0, -1.0):
            def attempt(timeout: float = timeout) -> float:
                started = time.monotonic()
                assert h.stop(actor, timeout) is False
                elapsed = time.monotonic() - started
                assert h.state(actor) == "stopping"
                return elapsed

            assert_timing(attempt, 0.0, what=f"stop(timeout={timeout!r}) while stopping")
    finally:
        release.set()
    assert h.wait_state(actor, "stopped")
    for timeout in (0, -1.0):
        def confirmed(timeout: float = timeout) -> float:
            started = time.monotonic()
            assert h.stop(actor, timeout) is True
            return time.monotonic() - started

        assert_timing(confirmed, 0.0, what=f"stop(timeout={timeout!r}) on stopped")


@case("TOL-2")
def test_tol_02_drain_non_positive_timeout_checks_once(h) -> None:
    actor, release = _blocked(h)
    try:
        for timeout in (0, -1.0):
            def attempt(timeout: float = timeout) -> tuple[float, float]:
                started = time.monotonic()
                completed, elapsed, remaining = h.drain([actor], timeout)
                wall = time.monotonic() - started
                assert (completed, remaining) == (False, [actor.name])
                assert elapsed >= 0
                return wall, elapsed

            assert_timing(attempt, 0.0, what=f"drain(timeout={timeout!r}) (wall, elapsed_s)")
    finally:
        release.set()


@case("TOL-2")
def test_tol_02_zero_delay_timer_fires_without_waiting(h) -> None:
    actor = h.spawn()

    def attempt() -> tuple[float, ...]:
        fired = Recorder()
        started = time.monotonic()
        h.send_after(actor, 0, Call(lambda: fired.append(time.monotonic())))
        h.send_after(actor, 0.0, Call(lambda: fired.append(time.monotonic())))
        assert fired.wait_len(2)
        return tuple(stamp - started for stamp in fired.items())

    assert_timing(attempt, 0.0, what="zero-delay timers")


@case("TOL-2")
def test_tol_02_zero_timeout_ask_on_restart_pending_actor(h) -> None:
    actor = h.spawn(profile="slow")
    assert h.tell(actor, Raise(RuntimeError("fail"))) == 0
    assert h.wait_state(actor, "restart_pending")

    def attempt() -> float:
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            h.ask(actor, "pending", 0)
        return time.monotonic() - started

    assert_timing(attempt, 0.0, what="ask(timeout=0) on restart_pending")
