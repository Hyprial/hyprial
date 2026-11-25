# Decision record (file header; no inline comments by repository rule):
# - ORD asserts only per-actor guarantees of 5.2: strict FIFO of accepted
#   tells, one handler execution at a time, per-producer order under
#   concurrency. Cross-actor and cross-producer interleaving is never
#   constrained (no cross-actor ordering is promised).
# - Serialization is probed with an "active" counter that every command
#   raises and lowers around a GIL-releasing sleep, so overlapping executions
#   would be observed instead of hidden by the GIL.
# - Producers that meet 1 (overloaded) retry the SAME message: the caller
#   retains a rejected message (5.2), so retries never reorder a producer.
# - ORD-03 keeps the soak cheap: plain Rec commands, a mailbox small enough
#   to hit the overload boundary repeatedly, a short sleep on overload so the
#   producer does not starve the actor thread of the GIL.
# - ORD-04 covers both readings of "does not deadlock": a self-tell returns 0
#   from inside the handler and runs after it, a chain of self-tells
#   completes, and a tell (from a handler or from outside) never waits for the
#   target's running handler (5.2 "MUST NOT block on the handler ... or on
#   other actors"). "Promptly" is bounded by TOL (TOL-1) through
#   support.helpers.assert_timing; each trial uses a fresh busy actor.
# - ORD-04 also covers the re-entry clause of section 12: tell, ask,
#   send_after, pool_reserve and stop on OTHER actors MUST work from inside a
#   handler, for both handler kinds (stop is exercised by STP-02).
"""ORD: ordering and serialization (spec 5.2, 12)."""

from __future__ import annotations

import threading
import time

from support.helpers import WAIT, Block, Call, Rec, Recorder, assert_timing, case

SOAK = 10_000
PRODUCERS = 4
PER_PRODUCER = 250


def _tell_retrying(h, actor, command: object, deadline: float) -> None:
    """Tell ``command`` until accepted; only 0 and 1 are legal outcomes here."""
    while True:
        outcome = h.tell(actor, command)
        if outcome == 0:
            return
        assert outcome == 1
        assert time.monotonic() < deadline
        time.sleep(0.0005)


@case("ORD-01")
def test_ord_01_fifo_and_single_handler_serialization(h) -> None:
    count = 200
    log = Recorder()
    active = [0]
    peak = [0]
    guard = threading.Lock()
    release = threading.Event()
    blocker = Block(release)

    def step(value: int) -> None:
        with guard:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.0002 if value % 10 == 0 else 0)
        log.append(value)
        with guard:
            active[0] -= 1

    actor = h.spawn(capacity=count + 1)
    try:
        assert h.tell(actor, blocker) == 0
        assert blocker.entered.wait(WAIT)
        outcomes = [h.tell(actor, Call(lambda value=value: step(value))) for value in range(count)]
        assert outcomes == [0] * count
        assert h.snapshot(actor)[2:4] == (count, 1)
    finally:
        release.set()
    assert log.wait_len(count, 10.0)
    assert log.items() == list(range(count))
    assert peak[0] == 1


@case("ORD-01")
def test_ord_01_fifo_mixed_tell_and_ask(h) -> None:
    log = Recorder()
    actor = h.spawn(capacity=32)
    for value in range(5):
        assert h.tell(actor, Rec(log, f"t{value}")) == 0
    assert h.ask(actor, Rec(log, "ask")) == "ask"
    assert log.items() == [f"t{value}" for value in range(5)] + ["ask"]


@case("ORD-02")
def test_ord_02_concurrent_producers_keep_per_producer_order(h) -> None:
    log = Recorder()
    actor = h.spawn(capacity=64)
    barrier = threading.Barrier(PRODUCERS)
    deadline = time.monotonic() + 20.0
    errors = Recorder()

    def produce(producer: int) -> None:
        try:
            barrier.wait(5.0)
            for seq in range(PER_PRODUCER):
                _tell_retrying(h, actor, Rec(log, (producer, seq)), deadline)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=produce, args=(p,), daemon=True) for p in range(PRODUCERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(25.0)
    assert errors.items() == []
    assert not any(thread.is_alive() for thread in threads)
    assert log.wait_len(PRODUCERS * PER_PRODUCER, 10.0)
    items = log.items()
    assert len(items) == PRODUCERS * PER_PRODUCER
    for producer in range(PRODUCERS):
        assert [seq for p, seq in items if p == producer] == list(range(PER_PRODUCER))


@case("ORD-03")
def test_ord_03_soak_keeps_order(h) -> None:
    log = Recorder()
    actor = h.spawn(capacity=256)
    deadline = time.monotonic() + 60.0
    for value in range(SOAK):
        _tell_retrying(h, actor, Rec(log, value), deadline)
    assert log.wait_len(SOAK, 60.0)
    assert log.items() == list(range(SOAK))
    assert h.wait_idle(actor)
    assert h.snapshot(actor) == ("running", 1, 0, 0, 0)


@case("ORD-04")
def test_ord_04_self_tell_from_handler_does_not_deadlock(h) -> None:
    log = Recorder()
    outcomes = Recorder()
    actor = h.spawn()

    def outer() -> str:
        log.append("outer-start")
        outcomes.append(h.tell(actor, Rec(log, "inner")))
        log.append("outer-end")
        return "outer"

    assert h.tell(actor, Call(outer)) == 0
    assert log.wait_len(3)
    assert outcomes.items() == [0]
    assert log.items() == ["outer-start", "outer-end", "inner"]
    assert h.ask(actor, "echo") == "echo"


@case("ORD-04")
def test_ord_04_self_tell_chain_completes(h) -> None:
    links = 100
    log = Recorder()
    actor = h.spawn(capacity=4)

    def link(index: int) -> None:
        log.append(index)
        if index + 1 < links:
            assert h.tell(actor, Call(lambda: link(index + 1))) == 0

    assert h.tell(actor, Call(lambda: link(0))) == 0
    assert log.wait_len(links, 10.0)
    assert log.items() == list(range(links))
    assert h.wait_idle(actor)


@case("ORD-04")
def test_ord_04_tell_never_waits_for_running_handler(h) -> None:
    caller = h.spawn()

    def attempt() -> tuple[float, float]:
        log = Recorder()
        timings = Recorder()
        release = threading.Event()
        blocker = Block(release)
        busy = h.spawn()

        def tell_busy_from_handler() -> None:
            started = time.monotonic()
            outcome = h.tell(busy, Rec(log, "from-handler"))
            timings.append((outcome, time.monotonic() - started))

        try:
            assert h.tell(busy, blocker) == 0
            assert blocker.entered.wait(WAIT)
            started = time.monotonic()
            assert h.tell(busy, Rec(log, "from-outside")) == 0
            outside = time.monotonic() - started
            assert h.tell(caller, Call(tell_busy_from_handler)) == 0
            assert timings.wait_len(1)
            outcome, inside = timings.items()[0]
            assert outcome == 0
            assert log.items() == []
        finally:
            release.set()
        assert log.wait_len(2)
        assert log.items() == ["from-outside", "from-handler"]
        return outside, inside

    assert_timing(attempt, 0.0, what="tell to a busy actor (from outside, from a handler)")


@case("ORD-04")
def test_ord_04_handler_reenters_engine_on_other_actors(h) -> None:
    log = Recorder()
    results: list[object] = []
    a = h.spawn()
    b = h.spawn()
    pool = h.spawn_pool()

    def reenter() -> str:
        results.append(h.ask(b, "x", WAIT))
        results.append(h.send_after(b, 0.0, Rec(log, "t")))
        results.append(h.engine.pool_reserve(pool.actor_id, pool.name, "r", 1))
        return "done"

    assert h.ask(a, Call(reenter), WAIT) == "done"
    assert results[0] == "x"
    assert isinstance(results[1], str)
    assert results[2] == 0
    assert log.wait_len(1)
    assert log.items() == ["t"]
    assert h.engine.pool_cancel_reservation(pool.actor_id, pool.name, "r", 1) is True
