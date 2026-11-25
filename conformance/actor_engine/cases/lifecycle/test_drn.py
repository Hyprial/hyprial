# Decision record (file header; no inline comments by repository rule):
# - Drain cases of spec 5.5 (+ 9.4 for DRN-09, 9.5 for pool tokens, 11 for
#   events). Serial targets use the `h` trampoline (both handler kinds); pool
#   owners are Python actors because they must call `acknowledge`.
# - "Drain in progress" is observed through the ABI only: a drain running on
#   a background thread has started once an external `tell` to a target
#   returns 2. Probe tells use the data string "noop", which the trampoline
#   echoes, so an early accepted probe is harmless.
# - DRN-02 keeps every target busy for the timed-out attempt, so `remaining`
#   is unambiguous (whether an already-idle target of an unfinished drain
#   counts as remaining is not fixed by 5.5; this suite does not assert it).
#   Its timed-out elapsed_s goes through support.helpers.assert_timing; every
#   retried trial uses a fresh busy pair and all pairs get the same checks.
# - RuntimeError is asserted only where 5.5 / E-7 mandate it: during an
#   in-progress ENGINE-WIDE drain and after a completed engine-wide drain or
#   shutdown. Whether a timed-out engine-wide drain is still "in progress"
#   is not fixed by the spec and is not asserted.
# - drain validates every id first: an unknown id in actor_ids raises
#   KeyError BEFORE any state changes (5.5 bullet 1, 3.2), so known targets
#   stay open and keep their pending timers, wherever the unknown id sits.
# - A reserved-but-unsubmitted token is a live custody token (9.1), and
#   pool_cancel_reservation is not external admission, so it holds a pool
#   drain until it is cancelled during the drain (5.5 bullet 3, 9.5).
# - 5.5 does not make closure atomic across targets (each target enters the
#   draining condition "at start"), so DRN-06 awaits the pool's closure
#   separately from the serial target's.
# - DRN-05 checks the pending timer's cancellation after drain() returns;
#   the drain-start timing of that cancellation is covered by TMR-05.
"""DRN: cell and engine-wide drain (spec 5.5, 9.4, 9.5, E-7)."""

from __future__ import annotations

import threading

import pytest

from support.helpers import (TOL, WAIT, Block, DeadLetters, Rec, Recorder, Sleep, assert_timing, case,
                             wait_until)


def _drain_async(h, actors, timeout: float = WAIT) -> tuple[threading.Thread, list]:
    result: list = []
    thread = threading.Thread(target=lambda: result.append(h.drain(actors, timeout)), daemon=True)
    thread.start()
    return thread, result


def _closed(h, actor) -> bool:
    return wait_until(lambda: h.tell(actor, "noop") == 2)


def _join(thread: threading.Thread, result: list) -> tuple:
    thread.join(WAIT + 1.0)
    assert not thread.is_alive()
    return result[0]


class Owner:
    """Python owner: records completions and the result of acknowledging each one."""

    def __init__(self, harness, pool, ack: bool = True, dead_letters=None) -> None:
        self.log = Recorder()
        self.acks = Recorder()
        self.ack = ack
        completed = harness.module.EffectCompleted
        engine = harness.engine

        def handle(message: object) -> object:
            if isinstance(message, completed):
                self.log.append(message)
                if self.ack:
                    self.acks.append(engine.acknowledge(pool.actor_id, pool.name,
                                                        message.operation_id, message.generation))
                return None
            return message() if callable(message) else message

        self.actor = harness.spawn(dead_letters=dead_letters, factory=lambda: handle)


def _submit(h, pool, owner, op: str, payload: object) -> int:
    return h.engine.pool_submit(pool.actor_id, pool.name, owner.actor_id, owner.name, op, 1, payload)


@case("DRN-01")
def test_drn_01_drain_waits_for_in_flight_and_queued_then_stops(h) -> None:
    """Drain returns (True, elapsed, []) only after the running and queued commands finish; then stopped."""
    dead = DeadLetters()
    actor = h.spawn(dead_letters=dead)
    log = Recorder()
    assert h.tell(actor, Sleep(0.2, "slept")) == 0
    for value in range(3):
        assert h.tell(actor, Rec(log, value)) == 0
    completed, elapsed, remaining = h.drain([actor], timeout=WAIT)
    assert (completed, remaining) == (True, [])
    assert elapsed >= 0.2 - TOL
    assert log.items() == [0, 1, 2]
    assert h.state(actor) == "stopped"
    assert dead.items() == []


@case("DRN-01")
def test_drn_01_drain_waits_for_live_pool_tokens(h) -> None:
    """A pool target drains only when no custody token is live: the late ack ends the wait."""
    pool = h.spawn_pool(pool_size=1, capacity=2)
    owner = Owner(h, pool, ack=False)
    assert _submit(h, pool, owner.actor, "op", Rec(Recorder(), "v")) == 0
    assert wait_until(lambda: len(owner.log) >= 1)
    acked: list[bool] = []

    def late_ack() -> None:
        threading.Event().wait(0.2)
        acked.append(h.engine.acknowledge(pool.actor_id, pool.name, "op", 1))

    acker = threading.Thread(target=late_ack, daemon=True)
    acker.start()
    completed, elapsed, remaining = h.drain([pool], timeout=WAIT)
    acker.join(WAIT)
    assert (completed, remaining) == (True, [])
    assert elapsed >= 0.2 - TOL
    assert acked == [True]
    assert h.state(pool) == "stopped"


@case("DRN-01")
def test_drn_01_unsubmitted_reservation_holds_drain(h) -> None:
    """A reserved, never-submitted token is live: the drain waits until it is cancelled."""
    pool = h.spawn_pool(pool_size=1, capacity=2)
    engine = h.engine
    assert engine.pool_reserve(pool.actor_id, pool.name, "r", 1) == 0
    short = h.drain([pool], 0.05)
    assert (short[0], short[2]) == (False, [pool.name])
    cancelled: list[bool] = []
    timer = threading.Timer(0.2, lambda: cancelled.append(
        engine.pool_cancel_reservation(pool.actor_id, pool.name, "r", 1)))
    timer.start()
    completed, elapsed, remaining = h.drain([pool], WAIT)
    timer.join(WAIT)
    assert (completed, remaining) == (True, [])
    assert elapsed >= 0.2 - TOL
    assert cancelled == [True]
    assert h.state(pool) == "stopped"


@case("DRN-01")
@pytest.mark.parametrize("first", [True, False], ids=["unknown-first", "unknown-last"])
def test_drn_01_unknown_id_raises_key_error_before_state_change(h, first: bool) -> None:
    """An unknown id in actor_ids raises KeyError before any target changes state."""
    actor = h.spawn()
    log = Recorder()
    pending = h.send_after(actor, 5.0, Rec(log, "late"))
    ids = ["no-such-actor", actor.actor_id] if first else [actor.actor_id, "no-such-actor"]
    with pytest.raises(KeyError):
        h.engine.drain(ids, 1.0)
    assert h.tell(actor, Rec(log, "after")) == 0
    assert h.state(actor) == "running"
    assert h.engine.cancel_after(pending) is True
    assert log.wait_len(1)
    assert log.items() == ["after"]


@case("DRN-02")
def test_drn_02_timeout_reports_sorted_remaining_and_retry_completes(h) -> None:
    """Timeout -> (False, ~timeout, sorted names); targets stay draining; a later drain finishes the job."""
    dead = DeadLetters()
    log = Recorder()
    release = threading.Event()
    pairs: list[tuple] = []

    def attempt() -> float:
        tag = f"{id(dead)}-{len(pairs)}"
        zulu = h.spawn(name=f"zulu-{tag}", dead_letters=dead)
        alpha = h.spawn(name=f"alpha-{tag}", dead_letters=dead)
        pairs.append((zulu, alpha))
        gates = [threading.Event(), threading.Event()]
        for actor, gate in zip((zulu, alpha), gates):
            assert h.tell(actor, Block(release, gate)) == 0
            assert h.tell(actor, Rec(log, actor.name)) == 0
        assert all(gate.wait(WAIT) for gate in gates)
        completed, elapsed, remaining = h.drain([zulu, alpha], timeout=0.1)
        assert completed is False
        assert remaining == sorted([zulu.name, alpha.name])
        return elapsed

    try:
        assert_timing(attempt, 0.1, what="timed-out drain elapsed_s")
        for zulu, alpha in pairs:
            for actor in (zulu, alpha):
                assert h.tell(actor, "noop") == 2
                assert h.state(actor) == "running"
            again = h.drain([zulu, alpha], timeout=0.05)
            assert (again[0], again[2]) == (False, sorted([zulu.name, alpha.name]))
        assert dead.items() == []
    finally:
        release.set()
    for zulu, alpha in pairs:
        completed, _, remaining = h.drain([zulu, alpha], timeout=WAIT)
        assert (completed, remaining) == (True, [])
        assert h.state(zulu) == h.state(alpha) == "stopped"
    assert sorted(log.items()) == sorted(actor.name for pair in pairs for actor in pair)
    assert dead.items() == []


@case("DRN-03")
def test_drn_03_cell_drain_leaves_non_targets_open(h) -> None:
    """While and after a cell drain runs, non-targeted serial actors and pools keep open admission."""
    target, bystander = h.spawn(), h.spawn()
    pool = h.spawn_pool(pool_size=1, capacity=2)
    log = Recorder()
    release, entered = threading.Event(), threading.Event()
    try:
        assert h.tell(target, Block(release, entered)) == 0
        assert entered.wait(WAIT)
        thread, result = _drain_async(h, [target])
        assert _closed(h, target)
        assert h.tell(bystander, Rec(log, "during")) == 0
        assert h.ask(bystander, Rec(log, "ask")) == "ask"
        assert h.engine.pool_reserve(pool.actor_id, pool.name, "r", 1) == 0
        assert h.engine.pool_cancel_reservation(pool.actor_id, pool.name, "r", 1) is True
    finally:
        release.set()
    assert _join(thread, result)[0] is True
    assert h.tell(bystander, Rec(log, "after")) == 0
    assert log.wait_len(3)
    assert log.items() == ["during", "ask", "after"]
    assert h.state(bystander) == "running"
    assert h.state(pool) == "running"


@case("DRN-04")
def test_drn_04_spawn_after_engine_wide_drain_raises_runtime_error(make_harness) -> None:
    """After an engine-wide drain completes, spawn and spawn_pool raise RuntimeError, repeatedly."""
    harness = make_harness()
    actor = harness.spawn()
    pool = harness.spawn_pool()
    assert harness.drain(None, timeout=WAIT)[0] is True
    assert harness.state(actor) == "stopped"
    assert harness.state(pool) == "stopped"
    for _ in range(2):
        with pytest.raises(RuntimeError):
            harness.spawn()
        with pytest.raises(RuntimeError):
            harness.spawn_pool()
    assert harness.tell(actor, "noop") == 2


@case("DRN-04")
def test_drn_04_spawn_after_shutdown_raises_runtime_error(make_harness) -> None:
    """shutdown has the spawn effect of a completed engine-wide drain."""
    harness = make_harness()
    harness.spawn()
    assert harness.close(WAIT) is True
    for _ in range(2):
        with pytest.raises(RuntimeError):
            harness.spawn()
        with pytest.raises(RuntimeError):
            harness.spawn_pool()


@case("DRN-05")
def test_drn_05_drained_actor_stops_normally_and_fences_apply(h) -> None:
    """Accepted commands and an already-admitted timer still run; a pending timer is cancelled; one child_stopped."""
    dead = DeadLetters()
    actor = h.spawn(dead_letters=dead)
    log = Recorder()
    release, entered = threading.Event(), threading.Event()
    try:
        assert h.tell(actor, Block(release, entered)) == 0
        assert entered.wait(WAIT)
        assert h.tell(actor, Rec(log, "queued")) == 0
        h.send_after(actor, 0.0, Rec(log, "timer"))
        assert wait_until(lambda: h.snapshot(actor)[2] == 2)
        pending = h.send_after(actor, 5.0, Rec(log, "late"))
        thread, result = _drain_async(h, [actor])
        assert _closed(h, actor)
    finally:
        release.set()
    completed, _, remaining = _join(thread, result)
    assert h.engine.cancel_after(pending) is False
    assert (completed, remaining) == (True, [])
    assert log.items() == ["queued", "timer"]
    assert h.snapshot(actor)[:2] == ("stopped", 1)
    stopped = [event for event in h.events.of(actor) if event[0] == "child_stopped"]
    assert len(stopped) == 1
    assert stopped[0][3] == 1
    assert dead.items() == []


@case("DRN-06")
@pytest.mark.parametrize("scope", ["cell", "engine"])
def test_drn_06_tell_ask_and_pool_admission_closed_during_drain(h, scope: str) -> None:
    """During a drain, tell -> 2, ask -> ActorAdmissionError(closed), pool_submit / pool_reserve -> 2."""
    actor = h.spawn()
    pool = h.spawn_pool(pool_size=1, capacity=4)
    owner = Owner(h, pool)
    log = Recorder()
    release, entered = threading.Event(), threading.Event()

    def _pool_closed() -> bool:
        reserved = h.engine.pool_reserve(pool.actor_id, pool.name, "probe", 1)
        if reserved == 0:
            h.engine.pool_cancel_reservation(pool.actor_id, pool.name, "probe", 1)
        return reserved == 2

    try:
        assert h.tell(actor, Block(release, entered)) == 0
        assert entered.wait(WAIT)
        targets = None if scope == "engine" else [actor, pool]
        thread, result = _drain_async(h, targets)
        assert _closed(h, actor)
        assert wait_until(_pool_closed)
        assert h.tell(actor, Rec(log, "tell")) == 2
        with pytest.raises(h.module.ActorAdmissionError) as raised:
            h.ask(actor, Rec(log, "ask"), timeout=WAIT)
        assert raised.value.args[0] == "closed"
        assert _submit(h, pool, owner.actor, "op", Rec(log, "submit")) == 2
        assert h.engine.pool_reserve(pool.actor_id, pool.name, "r", 1) == 2
        assert h.engine.pool_cancel_reservation(pool.actor_id, pool.name, "op", 1) is False
    finally:
        release.set()
    assert _join(thread, result)[0] is True
    assert log.items() == []


@case("DRN-07")
def test_drn_07_spawn_during_engine_wide_drain_raises_runtime_error(make_harness) -> None:
    """An in-progress engine-wide drain rejects spawn and spawn_pool with RuntimeError."""
    harness = make_harness()
    actor = harness.spawn()
    release, entered = threading.Event(), threading.Event()
    try:
        assert harness.tell(actor, Block(release, entered)) == 0
        assert entered.wait(WAIT)
        thread, result = _drain_async(harness, None)
        assert _closed(harness, actor)
        with pytest.raises(RuntimeError):
            harness.spawn()
        with pytest.raises(RuntimeError):
            harness.spawn_pool()
        assert thread.is_alive()
    finally:
        release.set()
    assert _join(thread, result)[0] is True
    with pytest.raises(RuntimeError):
        harness.spawn()


@case("DRN-08")
def test_drn_08_spawn_during_and_after_cell_drain_is_allowed(h) -> None:
    """Cell drain never blocks spawning; actors spawned during it are open and not stopped by it."""
    target = h.spawn()
    log = Recorder()
    release, entered = threading.Event(), threading.Event()
    try:
        assert h.tell(target, Block(release, entered)) == 0
        assert entered.wait(WAIT)
        thread, result = _drain_async(h, [target])
        assert _closed(h, target)
        during = h.spawn()
        during_pool = h.spawn_pool(pool_size=1, capacity=2)
        assert h.tell(during, Rec(log, "during")) == 0
        assert h.engine.pool_reserve(during_pool.actor_id, during_pool.name, "r", 1) == 0
        assert h.engine.pool_cancel_reservation(during_pool.actor_id, during_pool.name, "r", 1) is True
    finally:
        release.set()
    assert _join(thread, result)[0] is True
    after = h.spawn()
    assert h.ask(after, Rec(log, "after")) == "after"
    assert h.tell(during, Rec(log, "during-again")) == 0
    assert log.wait_len(3)
    assert sorted(log.items()) == sorted(["during", "after", "during-again"])
    assert h.state(during) == "running"
    assert h.state(during_pool) == "running"
    assert h.state(target) == "stopped"


@case("DRN-09")
def test_drn_09_owner_and_pool_in_same_drain_set(h) -> None:
    """A completion produced after drain start reaches the draining owner, is acked, and both drain."""
    pool_dead, owner_dead = DeadLetters(), DeadLetters()
    pool = h.spawn_pool(pool_size=1, capacity=2, dead_letters=pool_dead)
    owner = Owner(h, pool, dead_letters=owner_dead)
    release, entered = threading.Event(), threading.Event()
    try:
        assert _submit(h, pool, owner.actor, "op", Block(release, entered, "late")) == 0
        assert entered.wait(WAIT)
        thread, result = _drain_async(h, [owner.actor, pool])
        assert _closed(h, owner.actor)
        assert owner.log.items() == []
    finally:
        release.set()
    completed, elapsed, remaining = _join(thread, result)
    assert (completed, remaining) == (True, [])
    assert elapsed < WAIT
    assert [(m.operation_id, m.result) for m in owner.log.items()][:1] == [("op", "late")]
    assert owner.acks.items()[:1] == [True]
    assert pool_dead.items() == []
    assert owner_dead.items() == []
    assert h.state(owner.actor) == "stopped"
    assert h.state(pool) == "stopped"
    assert h.engine.acknowledge(pool.actor_id, pool.name, "op", 1) is False
