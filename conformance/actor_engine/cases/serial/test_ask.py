# Decision record (file header; no inline comments by repository rule):
# - ASK asserts the outcome table of spec 10 and the exception mapping of 13
#   (E-3, E-4, E-5): success returns the handler's object itself (identity,
#   including None), TimeoutError on timeout, the ORIGINAL expected exception
#   instance, ActorFailedError for every other child failure (Exception,
#   custom BaseException, panic), ActorAdmissionError with args[0]
#   "overloaded" / "closed" for admission failures - never a bare KeyError.
# - "GIL released while waiting" is observed by a Python ticker thread that
#   must keep making progress while the main thread sits inside ask. The
#   handler is event-driven (not a fixed 0.2 s sleep): it returns only after
#   it saw the caller inside ask AND the ticker advanced 10 more ticks, so a
#   loaded host cannot starve the ticker inside a fixed window, while an ask
#   that held the GIL would never let the handler return (ask times out).
# - Promptness of tell (< TOL) goes through support.helpers.assert_timing.
# - ASK-02 never asserts whether the timed-out command ran (10: it MAY still
#   execute); it asserts only the timeout itself and that a later ask gets its
#   own reply, not the stale one.
# - Pool targets are closed for ask (3.2, 10).
"""ASK: request/response (spec 10, 3.2, 13)."""

from __future__ import annotations

import threading
import time

import pytest

from support.helpers import (WAIT, Block, Call, CustomBase, Expected, ExpectedSub, Raise, Rec, Recorder,
                             assert_timing, case, inside_call)


def _admission(h, actor, command: object, reason: str) -> None:
    with pytest.raises(h.module.ActorAdmissionError) as info:
        h.ask(actor, command, WAIT)
    assert info.value.args[0] == reason
    assert not isinstance(info.value, KeyError)


@case("ASK-01")
def test_ask_01_returns_handler_value(h) -> None:
    actor = h.spawn()
    sentinel = object()
    data = {"opaque": [1, 2]}
    assert h.ask(actor, Call(lambda: sentinel)) is sentinel
    assert h.ask(actor, data) is data
    assert h.ask(actor, Call(lambda: None)) is None
    assert h.ask(actor, Call(lambda: 0)) == 0
    assert h.events.wait_kind(actor, "command_completed", 4)


@case("ASK-01")
def test_ask_01_gil_released_while_waiting(h) -> None:
    actor = h.spawn()
    ticks = [0]
    stop = threading.Event()

    def tick() -> None:
        while not stop.is_set():
            ticks[0] += 1
            time.sleep(0.001)

    caller = threading.get_ident()

    def until_ticked() -> str:
        deadline = time.monotonic() + WAIT
        base = None
        while time.monotonic() < deadline:
            if base is None and inside_call(caller, "ask"):
                base = ticks[0]
            if base is not None and ticks[0] - base >= 10:
                return "ticked"
            time.sleep(0.001)
        return "ticker starved"

    ticker = threading.Thread(target=tick, daemon=True)
    ticker.start()
    try:
        before = ticks[0]
        assert h.ask(actor, Call(until_ticked)) == "ticked"
        during = ticks[0] - before
    finally:
        stop.set()
        ticker.join(WAIT)
    assert during >= 10


@case("ASK-01")
def test_ask_01_waiting_ask_does_not_block_other_callers(h) -> None:
    log = Recorder()
    replies = Recorder()
    release = threading.Event()
    blocker = Block(release, value="first")
    actor = h.spawn()

    def ask_in_thread(command: object) -> None:
        replies.append(h.ask(actor, command, WAIT))

    first = threading.Thread(target=ask_in_thread, args=(blocker,), daemon=True)
    second = threading.Thread(target=ask_in_thread, args=(Rec(log, "second"),), daemon=True)
    try:
        first.start()
        assert blocker.entered.wait(WAIT)
        told: list[int] = []

        def attempt() -> float:
            started = time.monotonic()
            assert h.tell(actor, Rec(log, "told")) == 0
            elapsed = time.monotonic() - started
            told.append(1)
            return elapsed

        assert_timing(attempt, 0.0, what="tell while an ask is waiting")
        second.start()
        assert h.snapshot(actor)[3] == 1
    finally:
        release.set()
    first.join(WAIT)
    second.join(WAIT)
    assert replies.wait_len(2)
    assert sorted(replies.items()) == ["first", "second"]
    assert log.items() == ["told"] * len(told) + ["second"]


@case("ASK-02")
def test_ask_02_timeout_raises_timeout_error(h) -> None:
    release = threading.Event()
    blocker = Block(release, value="late")
    actors = []

    def attempt() -> float:
        actor = h.spawn()
        actors.append(actor)
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            h.ask(actor, blocker, 0.1)
        return time.monotonic() - started

    try:
        assert_timing(attempt, 0.1, what="ask timeout on a blocking command")
    finally:
        release.set()
    for actor in actors:
        assert h.ask(actor, "fresh") == "fresh"
        assert h.snapshot(actor)[:2] == ("running", 1)


@case("ASK-02")
def test_ask_02_timeout_when_queued_behind_busy_handler(h) -> None:
    log = Recorder()
    release = threading.Event()
    blocker = Block(release)
    actor = h.spawn()
    try:
        assert h.tell(actor, blocker) == 0
        assert blocker.entered.wait(WAIT)
        with pytest.raises(TimeoutError):
            h.ask(actor, Rec(log, "maybe"), 0.05)
    finally:
        release.set()
    assert h.ask(actor, "after") == "after"
    assert log.items() in ([], ["maybe"])


@case("ASK-03")
def test_ask_03_expected_error_reraised_as_is(h) -> None:
    actor = h.spawn(profile="never")
    for exc in (Expected("E_ASK", "ask rejected"), ExpectedSub("E_ASK_SUB", "sub rejected")):
        with pytest.raises(Expected) as info:
            h.ask(actor, Raise(exc))
        assert info.value is exc
    assert h.wait_idle(actor)
    assert h.snapshot(actor) == ("running", 1, 0, 0, 0)
    assert h.ask(actor, "alive") == "alive"


@case("ASK-03")
def test_ask_03_other_failure_raises_actor_failed_error(h) -> None:
    actor = h.spawn(profile="fast")
    failures = (Raise(RuntimeError("boom")), Raise(CustomBase("custom base")), "panic")
    for command in failures:
        with pytest.raises(h.module.ActorFailedError) as info:
            h.ask(actor, command)
        assert not isinstance(info.value, Expected)
    assert h.events.wait_kind(actor, "child_restarted", len(failures))
    assert h.ask(actor, "alive") == "alive"
    assert h.generation(actor) == 1 + len(failures)


@case("ASK-04")
def test_ask_04_overloaded(h) -> None:
    log = Recorder()
    release = threading.Event()
    blocker = Block(release)
    actor = h.spawn(capacity=1)
    try:
        assert h.tell(actor, blocker) == 0
        assert blocker.entered.wait(WAIT)
        assert h.tell(actor, Rec(log, "queued")) == 0
        _admission(h, actor, Rec(log, "overloaded"), "overloaded")
    finally:
        release.set()
    assert log.wait_len(1)
    assert h.ask(actor, "after") == "after"
    assert log.items() == ["queued"]


@case("ASK-04")
def test_ask_04_closed_stopped_and_stopping(h) -> None:
    log = Recorder()
    release = threading.Event()
    blocker = Block(release)
    actor = h.spawn()
    try:
        assert h.tell(actor, blocker) == 0
        assert blocker.entered.wait(WAIT)
        assert h.stop(actor, 0) is False
        _admission(h, actor, Rec(log, "stopping"), "closed")
    finally:
        release.set()
    assert h.wait_state(actor, "stopped")
    _admission(h, actor, Rec(log, "stopped"), "closed")
    assert log.items() == []


@case("ASK-04")
def test_ask_04_closed_quarantined(h) -> None:
    log = Recorder()
    actor = h.spawn(profile="never")
    assert h.tell(actor, Raise(RuntimeError("fail"))) == 0
    assert h.wait_state(actor, "quarantined")
    _admission(h, actor, Rec(log, "quarantined"), "closed")
    assert log.items() == []


@case("ASK-04")
def test_ask_04_closed_draining(h) -> None:
    log = Recorder()
    release = threading.Event()
    blocker = Block(release)
    actor = h.spawn()
    try:
        assert h.tell(actor, blocker) == 0
        assert blocker.entered.wait(WAIT)
        assert h.drain([actor], 0.02)[0] is False
        _admission(h, actor, Rec(log, "draining"), "closed")
    finally:
        release.set()
    assert log.items() == []


@case("ASK-04")
def test_ask_04_closed_unknown_and_forged_handles(h) -> None:
    log = Recorder()
    actor = h.spawn()
    other = h.spawn()
    for handle in (actor._replace(actor_id="no-such-actor-id"), actor._replace(name=actor.name + "-forged"),
                   actor._replace(name=other.name)):
        _admission(h, handle, Rec(log, "forged"), "closed")
    assert h.ask(actor, "real") == "real"
    assert log.items() == []


@case("ASK-04")
def test_ask_04_pool_target_closed(h) -> None:
    log = Recorder()
    pool = h.spawn_pool()
    _admission(h, pool, Rec(log, "pool"), "closed")
    assert log.items() == []
    assert h.snapshot(pool)[:2] == ("running", 1)
