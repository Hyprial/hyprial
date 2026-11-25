# Decision record (file header; no inline comments by repository rule):
# - CUS cases cover the only "return path" of the engine: dead letters
#   (spec 7) at restart (none), quarantine (`quarantined`), confirmed stop
#   (`stopped`), plus custody-token fencing for pools (5.3, 9.2).
# - Mailbox contents are made deterministic by parking the handler on a Block
#   command first; everything told afterwards is provably queued.
# - Dead-lettered messages are compared by identity (`is`), never by
#   equality: CUS-04 sends objects whose every interpretation hook raises, so
#   the test itself must not interpret them either.
# - CUS-03 "after confirmation" is asserted as a single timeline shared by the
#   dead-letter sink and the per-actor event sink: the handler running at
#   acceptance completes first, then dead letters in admission order, then
#   child_stopped (5.4 steps 3-5). Whether `stop` returns before or after the
#   sink calls is not fixed by the spec, so dead letters are awaited.
# - Admission order is required only for the confirmed-stop drain (5.4 step
#   5, section 7 "stop ... in admission order"). The quarantine drain (7,
#   6.3 step 2) fixes no order, so it is compared as an identity multiset
#   (`same_members`), which still never interprets the messages.
# - CUS-06 also fences acknowledge by the exact (operation_id, generation)
#   key while the real token is live and delivered (5.3, 9.2): keys that
#   differ only by generation or only by operation_id are False, and the live
#   token still pins its capacity slot.
# - CUS-06 uses a python owner (spawn_owner, ack disabled) so the test thread
#   decides exactly when a token is acknowledged.
# - CUS-05's stop timeout (TOL-1) goes through support.helpers.assert_timing;
#   a retried trial is a joining stop on the same stopping actor, which the
#   case already shows has no dead-letter side effect.
"""CUS: dead-letter custody and custody-token fencing (spec 5.3, 5.4, 6.3.5, 7, 9.2)."""

from __future__ import annotations

import threading
import time

from support.helpers import (WAIT, Actor, Block, Call, DeadLetters, Raise, Rec, Recorder, assert_timing,
                             case, wait_until)

KIND, GEN = 0, 3

def expect(h, actor, *prefix: object) -> None:
    """Wait (bounded) until the snapshot starts with ``prefix``; state and event order is not fixed."""
    assert wait_until(lambda: h.snapshot(actor)[:len(prefix)] == prefix), (h.snapshot(actor), prefix)



class Timeline(Recorder):
    """One ordered record of dead letters and per-actor events."""

    def dead(self, message: object, reason: str) -> None:
        self.append(("dead", message, reason))

    def event(self, event: tuple) -> None:
        self.append(("event", event[KIND], event))


class Poison:
    """A message whose every interpretation hook raises; calling it is recorded."""

    def __init__(self, calls: Recorder) -> None:
        object.__setattr__(self, "_calls", calls)

    def __call__(self) -> None:
        object.__getattribute__(self, "_calls").append(1)

    def _refuse(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("the engine interpreted a dead-lettered message")

    __eq__ = __ne__ = __lt__ = __hash__ = __bool__ = __len__ = __iter__ = _refuse
    __repr__ = __str__ = __reduce__ = __reduce_ex__ = __copy__ = __deepcopy__ = _refuse

    def __getattr__(self, name: str) -> None:
        raise AssertionError(f"the engine looked up {name!r} on a dead-lettered message")


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


def same(items: list, expected: list) -> bool:
    return len(items) == len(expected) and all(a is b for a, b in zip(items, expected))


def same_members(items: list, expected: list) -> bool:
    return len(items) == len(expected) and sorted(map(id, items)) == sorted(map(id, expected))


@case("CUS-01")
def test_cus_01_restart_keeps_mailbox_drops_failed_command(h) -> None:
    dead = DeadLetters()
    log = Recorder()
    failed_runs = Recorder()

    def explode() -> None:
        failed_runs.append(1)
        raise RuntimeError("boom")

    actor = h.spawn(profile="slow", dead_letters=dead)
    release = park(h, actor)
    assert h.tell(actor, Call(explode)) == 0
    assert h.tell(actor, Rec(log, "a")) == 0
    assert h.tell(actor, Rec(log, "b")) == 0
    release.set()
    assert h.events.wait_kind(actor, "restart_scheduled")
    expect(h, actor, "restart_pending", 1, 2, 0)
    assert h.tell(actor, Rec(log, "c")) == 0
    assert log.wait_len(3)
    assert log.items() == ["a", "b", "c"]
    assert len(failed_runs) == 1
    completed = [e for e in h.events.of(actor) if e[KIND] == "command_completed"]
    assert [e[GEN] for e in completed] == [1, 2, 2, 2]
    time.sleep(0.05)
    assert dead.items() == []


@case("CUS-02")
def test_cus_02_quarantine_drains_mailbox_then_stop_sends_none(h) -> None:
    dead = DeadLetters()
    log = Recorder()
    actor = h.spawn(profile="never", dead_letters=dead)
    release = park(h, actor)
    assert h.tell(actor, Raise(RuntimeError("boom"))) == 0
    queued = [Rec(log, value) for value in ("a", "b", "c")]
    for command in queued:
        assert h.tell(actor, command) == 0
    release.set()
    assert h.events.wait_kind(actor, "child_quarantined")
    assert dead.wait_len(3)
    assert len(dead) == 3
    assert same_members([m for m, _ in dead.items()], queued)
    assert [reason for _, reason in dead.items()] == ["quarantined"] * 3
    expect(h, actor, "quarantined", 1, 0, 0)
    assert log.items() == []
    assert h.stop(actor) is True
    assert h.events.wait_kind(actor, "child_stopped")
    time.sleep(0.05)
    assert len(dead) == 3


@case("CUS-03")
def test_cus_03_confirmed_stop_dead_letters_in_order_after_handler(h) -> None:
    timeline = Timeline()
    log = Recorder()
    actor = Actor(h.engine.spawn("cus03", h.factory(), 16, "fast", timeline.dead, timeline.event), "cus03")
    release = park(h, actor)
    queued = [Rec(log, value) for value in ("a", "b", "c")]
    for command in queued:
        assert h.tell(actor, command) == 0
    thread, result = in_thread(lambda: h.stop(actor, WAIT))
    assert h.wait_state(actor, "stopping")
    release.set()
    thread.join(WAIT + 1)
    assert result == [True]
    assert timeline.wait_len(5)
    entries = timeline.items()
    assert entries[0][:2] == ("event", "command_completed")
    assert same([entry[1] for entry in entries[1:4]], queued)
    assert [entry[2] for entry in entries[1:4]] == ["stopped"] * 3
    assert entries[4][:2] == ("event", "child_stopped")
    assert len(entries) == 5
    assert log.items() == []
    expect(h, actor, "stopped", 1, 0, 0)


@case("CUS-04")
def test_cus_04_engine_never_interprets_dead_letters(h) -> None:
    calls = Recorder()
    log = Recorder()
    stopped_dead = DeadLetters()
    stopping = h.spawn(dead_letters=stopped_dead)
    release = park(h, stopping)
    stop_messages = [Poison(calls), object(), {"k": "v"}, None, 42, "panic", Rec(log, "never")]
    for message in stop_messages:
        assert h.tell(stopping, message) == 0
    thread, result = in_thread(lambda: h.stop(stopping, WAIT))
    assert h.wait_state(stopping, "stopping")
    release.set()
    thread.join(WAIT + 1)
    assert result == [True]
    assert stopped_dead.wait_len(len(stop_messages))
    assert same([m for m, _ in stopped_dead.items()], stop_messages)
    assert {reason for _, reason in stopped_dead.items()} == {"stopped"}

    quarantine_dead = DeadLetters()
    quarantined = h.spawn(profile="never", dead_letters=quarantine_dead)
    release = park(h, quarantined)
    assert h.tell(quarantined, Raise(RuntimeError("boom"))) == 0
    quarantine_messages = [Poison(calls), "panic", Rec(log, "never")]
    for message in quarantine_messages:
        assert h.tell(quarantined, message) == 0
    release.set()
    assert quarantine_dead.wait_len(len(quarantine_messages))
    assert len(quarantine_dead) == len(quarantine_messages)
    assert same_members([m for m, _ in quarantine_dead.items()], quarantine_messages)
    assert {reason for _, reason in quarantine_dead.items()} == {"quarantined"}

    timer_poison = Poison(calls)
    h.send_after(stopping, 0.0, timer_poison)
    assert stopped_dead.wait_len(len(stop_messages) + 1)
    last_message, last_reason = stopped_dead.items()[-1]
    assert last_message is timer_poison and last_reason == "stopped"
    assert len(calls) == 0
    assert log.items() == []


@case("CUS-05")
def test_cus_05_stop_timeout_sends_no_dead_letters_then_once(h) -> None:
    dead = DeadLetters()
    actor = h.spawn(dead_letters=dead)
    release = park(h, actor)
    queued = [Rec(Recorder(), "a"), Rec(Recorder(), "b")]
    for command in queued:
        assert h.tell(actor, command) == 0
    def attempt() -> float:
        started = time.monotonic()
        assert h.stop(actor, 0.1) is False
        return time.monotonic() - started

    assert_timing(attempt, 0.1, what="stop timeout with a parked handler")
    assert h.state(actor) == "stopping"
    assert h.tell(actor, Rec(Recorder(), "late")) == 2
    time.sleep(0.05)
    assert dead.items() == []
    assert h.stop(actor, 0.05) is False
    assert dead.items() == []
    assert h.state(actor) == "stopping"
    release.set()
    assert h.stop(actor, WAIT) is True
    assert dead.wait_len(2)
    time.sleep(0.1)
    assert same([m for m, _ in dead.items()], queued)
    assert [reason for _, reason in dead.items()] == ["stopped", "stopped"]
    assert h.events.kinds(actor).count("child_stopped") == 1


@case("CUS-06")
def test_cus_06_custody_keys_fenced_never_recreated(h) -> None:
    pool = h.spawn_pool(capacity=1)
    completions = Recorder()
    owner = h.spawn_owner(pool, completions, ack=False)
    engine = h.engine
    p = (pool.actor_id, pool.name)
    o = (owner.actor_id, owner.name)
    work = Recorder()
    assert engine.pool_submit_reserved(*p, *o, "op-x", 1, Rec(work, "x")) == 2
    assert engine.pool_cancel_reservation(*p, "op-x", 1) is False
    assert engine.acknowledge(*p, "op-x", 1) is False
    assert engine.pool_reserve(*p, "op-1", 1) == 0
    assert engine.pool_submit_reserved(*p, *o, "op-1", 2, Rec(work, "wrong-gen")) == 2
    assert engine.pool_cancel_reservation(*p, "op-1", 2) is False
    assert engine.pool_reserve(*p, "op-1", 2) == 1
    assert engine.pool_cancel_reservation(*p, "op-1", 1) is True
    assert engine.pool_submit_reserved(*p, *o, "op-1", 1, Rec(work, "cancelled")) == 2
    assert engine.pool_cancel_reservation(*p, "op-1", 1) is False
    assert engine.pool_submit(*p, *o, "op-2", 1, Rec(work, "w")) == 0
    assert completions.wait_len(1)
    assert completions.items()[0].operation_id == "op-2"
    assert engine.acknowledge(*p, "op-2", 2) is False
    assert engine.acknowledge(*p, "OP-2", 1) is False
    assert engine.pool_reserve(*p, "op-9", 1) == 1
    assert engine.acknowledge(*p, "op-2", 1) is True
    assert engine.acknowledge(*p, "op-2", 1) is False
    assert engine.pool_submit_reserved(*p, *o, "op-2", 1, Rec(work, "settled")) == 2
    assert engine.pool_cancel_reservation(*p, "op-2", 1) is False
    assert engine.pool_reserve(*p, "op-3", 1) == 0
    assert engine.pool_cancel_reservation(*p, "op-3", 1) is True
    time.sleep(0.05)
    assert work.items() == ["w"]


@case("CUS-07")
def test_cus_07_repeated_stop_one_drain_one_confirmation(h) -> None:
    dead = DeadLetters()
    actor = h.spawn(dead_letters=dead)
    release = park(h, actor)
    queued = [Rec(Recorder(), "a"), Rec(Recorder(), "b")]
    for command in queued:
        assert h.tell(actor, command) == 0
    stoppers = [in_thread(lambda: h.stop(actor, WAIT)) for _ in range(4)]
    assert h.wait_state(actor, "stopping")
    assert h.stop(actor, 0.05) is False
    release.set()
    for thread, result in stoppers:
        thread.join(WAIT + 1)
        assert result == [True]
    assert dead.wait_len(2)
    assert h.stop(actor, WAIT) is True
    time.sleep(0.1)
    assert same([m for m, _ in dead.items()], queued)
    assert [reason for _, reason in dead.items()] == ["stopped", "stopped"]
    assert h.events.kinds(actor).count("child_stopped") == 1
