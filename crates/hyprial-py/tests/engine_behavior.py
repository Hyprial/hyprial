"""Development-time behaviour checks for the native actor engine.

This is NOT the conformance suite (mainline batch P3 owns
``conformance/actor_engine/``); it is the Rust developer's black-box
self-check against the frozen spec, organised by conformance case ID. Run:

    uv run --python 3.12 python crates/hyprial-py/tests/engine_behavior.py

Every check prints ``PASS <description>`` or ``FAIL <group>: <error>``;
the exit code is the verdict. The pool, drain and lifecycle groups live in
``engine_behavior_pools.py`` and the shared helpers in ``behavior_support.py``
(split for the 1000-line rule); this file is the only entry point.
"""

from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from behavior_support import (  # noqa: E402
    POLICIES,
    Engine,
    Expected,
    ExpectedSub,
    Sink,
    blocker,
    calling_factory,
    check,
    make_engine,
    make_engine_with,
    native,
    wait_until,
)


# ---------------------------------------------------------------- ADM/ORD


def adm_01_02_04_05() -> None:
    sink = Sink()
    eng = make_engine(sink)
    aid = eng.spawn("a", calling_factory, capacity=2, profile="state_authority")
    release, entered = threading.Event(), threading.Event()
    assert eng.tell(aid, "a", blocker(release, entered)) == 0
    assert entered.wait(3.0), "the blocking command must start"
    try:
        assert eng.tell(aid, "a", "m1") == 0
        assert eng.tell(aid, "a", "m2") == 0
        assert eng.tell(aid, "a", "m3") == 1, "queued-full must return overloaded"
    finally:
        release.set()
    assert wait_until(lambda: sink.kinds().count("command_completed") == 3), sink.kinds()
    # ADM-04: unknown and forged handles classify closed.
    assert eng.tell("act-nope", "a", "x") == 2
    assert eng.tell(aid, "forged-name", "x") == 2
    # ADM-05: invalid spec -> ValueError.
    for kwargs in (
        dict(name="  ", handler_factory=lambda: lambda c: c, capacity=1, profile="state_authority"),
        dict(name="x", handler_factory=lambda: lambda c: c, capacity=0, profile="state_authority"),
        dict(name="x", handler_factory=lambda: lambda c: c, capacity=1, profile="nope"),
    ):
        try:
            eng.spawn(**kwargs)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass
    try:
        eng.spawn_pool("p", lambda: lambda c: c, pool_size=0, capacity=1, profile="state_authority")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    try:
        eng.spawn_pool("p", lambda: lambda c: c, pool_size=1, capacity=1, retry_interval=0.0,
                       profile="state_authority")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    check("ADM-01/02/04/05 admission values, forged handles, validation")


def ord_01_02_03() -> None:
    eng = make_engine()
    got: list[int] = []
    lock = threading.Lock()

    def factory():
        def handler(cmd):
            with lock:
                got.append(cmd)
            return cmd

        return handler

    aid = eng.spawn("fifo", factory, capacity=20_000, profile="state_authority")
    n = 10_000 if os.environ.get("SOAK") else 500

    def produce(base: int) -> None:
        for i in range(n // 2):
            assert eng.tell(aid, "fifo", base + i) == 0

    t1 = threading.Thread(target=produce, args=(0,))
    t2 = threading.Thread(target=produce, args=(10_000_000,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert wait_until(lambda: len(got) == n, timeout=20)
    with lock:
        seq = list(got)
    a = [x for x in seq if x < 10_000_000]
    b = [x for x in seq if x >= 10_000_000]
    assert a == sorted(a) and b == sorted(b), "per-actor FIFO violated"
    check("ORD-01/02/03 per-actor FIFO, concurrent producers, soak order")


def ord_04_liv_04() -> None:
    sink = Sink()
    eng = make_engine(sink)
    holder: list[str] = [""]

    def factory():
        def handler(cmd):
            if cmd == "first":
                eng.tell(holder[0], "self", "again")
            return cmd

        return handler

    aid = eng.spawn("self", factory, capacity=8, profile="state_authority")
    holder[0] = aid
    eng.tell(aid, "self", "first")
    assert wait_until(lambda: sink.kinds().count("command_completed") == 2, timeout=3), sink.kinds()
    check("ORD-04/LIV-04 self-tell enqueues behind, no deadlock")


# ---------------------------------------------------------------- EVT


def evt_01_02_03() -> None:
    engine_level = Sink()
    seen: list[tuple[str, int]] = []

    def spec_sink(event: tuple) -> None:
        seen.append(("spec", id(event)))
        raise RuntimeError("sink exception must be swallowed (EVT-03)")

    def engine_sink(event: tuple) -> None:
        seen.append(("engine", id(event)))
        engine_level.event(event)

    eng = Engine(policies=POLICIES, jitter_seed=1, event_sink=engine_sink,
                 expected_error_type=Expected)
    aid = eng.spawn("evt", lambda: lambda c: c, capacity=4,
                    profile="state_authority", event_sink=spec_sink)
    eng.tell(aid, "evt", "hello")
    assert wait_until(lambda: len(seen) >= 2, timeout=2), seen
    assert seen[0][0] == "spec" and seen[1][0] == "engine", "spec sink first, then engine"
    assert seen[0][1] == seen[1][1], "both sinks receive the same tuple"
    # EVT-01: field layout of the completion event
    eng.tell(aid, "evt", "world")
    assert wait_until(lambda: len(engine_level.events) >= 1, timeout=2)
    event = engine_level.events[-1]
    assert len(event) == 8, event
    kind, actor_id, name, generation, command_type, code, detail, restart_delay = event
    assert kind == "command_completed" and actor_id == aid and name == "evt"
    assert generation == 1 and command_type == "str"
    assert code is None and detail is None and restart_delay is None
    check("EVT-01/02/03 event fields, sink routing, sink isolation")


# ---------------------------------------------------------------- EXP/SUP


def exp_01_02() -> None:
    sink = Sink()
    eng = make_engine(sink)
    aid = eng.spawn(
        "e",
        lambda: lambda cmd: (_ for _ in ()).throw(ExpectedSub()) if cmd == "boom" else cmd,
        capacity=4,
        profile="state_authority",
    )
    eng.tell(aid, "e", "boom")
    assert wait_until(lambda: "command_rejected" in sink.kinds(), timeout=2), sink.kinds()
    snap = eng.snapshot(aid, "e")
    assert snap == ("running", 1, 0, 0, 0), f"expected error must not restart: {snap}"
    eng.tell(aid, "e", "ok")
    assert wait_until(lambda: sink.kinds().count("command_completed") == 1), sink.kinds()
    check("EXP-01/02 expected error rejected, actor survives, subclass same")


def sup_01_08() -> None:
    sink = Sink()
    eng = make_engine(sink)
    aid = eng.spawn(
        "s",
        lambda: lambda cmd: (_ for _ in ()).throw(RuntimeError("kaboom")) if cmd == "die" else cmd,
        capacity=4,
        profile="state_authority",
        dead_letter_sink=sink.dead_letter,
    )
    eng.tell(aid, "s", "keep-1")
    eng.tell(aid, "s", "die")
    eng.tell(aid, "s", "keep-2")
    assert wait_until(lambda: "child_restarted" in sink.kinds(), timeout=5), sink.kinds()
    # SUP-08: exactly one child_failed for the one failure (no stale reports).
    fails = [e for e in sink.events if e[0] == "child_failed"]
    assert len(fails) == 1, sink.kinds()
    assert fails[0][5] == "RuntimeError", fails
    snap = eng.snapshot(aid, "s")
    assert snap[1] == 2, f"generation must increment on re-entry: {snap}"
    assert wait_until(lambda: sink.kinds().count("command_completed") >= 2, timeout=5), sink.kinds()
    assert sink.dead_reasons() == [], "restart must NOT dead-letter (CUS-01)"
    check("SUP-01/08 failure->restart, generation+1, stale reports fenced")


def sup_02() -> None:
    sink = Sink()
    eng = make_engine(sink)
    aid_a = eng.spawn("a", lambda: lambda c: (_ for _ in ()).throw(RuntimeError()) if c == "die" else c,
                      capacity=4, profile="state_authority")
    aid_b = eng.spawn("b", lambda: lambda c: c, capacity=4, profile="state_authority")
    eng.tell(aid_a, "a", "die")
    assert wait_until(lambda: "child_restarted" in sink.kinds(), timeout=5), sink.kinds()
    failed = [e for e in sink.events if e[0] == "child_failed"]
    assert all(e[1] == aid_a for e in failed), "only the failed child restarts"
    eng.tell(aid_b, "b", "ping")
    assert wait_until(lambda: sink.kinds().count("command_completed") >= 1), sink.kinds()
    check("SUP-02 single-actor isolation")


def sup_03_04() -> None:
    sink = Sink()
    eng = make_engine(sink)
    aid = eng.spawn(
        "q",
        lambda: lambda cmd: (_ for _ in ()).throw(RuntimeError("x")),
        capacity=4,
        profile="state_authority",
        dead_letter_sink=sink.dead_letter,
    )
    for i in range(3):
        eng.tell(aid, "q", f"d{i}")
        assert wait_until(
            lambda: sink.kinds().count("child_failed") >= i + 1, timeout=5
        ), sink.kinds()
        assert wait_until(
            lambda: sink.kinds().count("restart_scheduled") >= i + 1, timeout=5
        ), sink.kinds()
    eng.tell(aid, "q", "d3")
    assert wait_until(lambda: "child_quarantined" in sink.kinds(), timeout=5), sink.kinds()
    assert sink.kinds().count("child_failed") == 4, ("CL-1", sink.kinds())
    assert sink.kinds()[-2:] == ["child_failed", "child_quarantined"], ("CL-1", sink.kinds())
    assert sink.kinds().count("restart_scheduled") == 3, ("CL-1", sink.kinds())
    snap = eng.snapshot(aid, "q")
    assert snap[0] == "quarantined", f"quarantine state: {snap}"
    assert snap[1] == 4, f"quarantine generation unchanged: {snap}"
    assert snap[4] == 4, f"budget deque frozen: {snap}"
    assert eng.tell(aid, "q", "late") == 2
    assert sink.dead_reasons() == [], "no dead letters at restart"
    # SUP-04: window prunes old failures.
    sink2 = Sink()
    eng2 = make_engine_with({"win": (1, 0.3, 0.01, 0.02, 0.0)}, sink2)
    aid2 = eng2.spawn("w", lambda: lambda c: (_ for _ in ()).throw(RuntimeError()),
                      capacity=2, profile="win")
    eng2.tell(aid2, "w", "1")
    assert wait_until(lambda: sink2.kinds().count("child_failed") >= 1, timeout=3), sink2.kinds()
    time.sleep(0.5)
    eng2.tell(aid2, "w", "2")
    assert wait_until(lambda: sink2.kinds().count("child_failed") >= 2, timeout=3), sink2.kinds()
    assert "child_quarantined" not in sink2.kinds(), "old failure must prune out of window"
    check("SUP-03 quarantine + SUP-04 window pruning")


def sup_05() -> None:
    # Backoff formula exactness at jitter_ratio = 0 (SUP-05) via events.
    sink = Sink()
    eng = make_engine_with({"exact": (5, 60, 0.2, 1.6, 0.0)}, sink)
    aid = eng.spawn("b", lambda: lambda c: (_ for _ in ()).throw(RuntimeError()),
                    capacity=4, profile="exact", dead_letter_sink=sink.dead_letter)
    for i in range(4):
        eng.tell(aid, "b", f"d{i}")
        assert wait_until(
            lambda: sink.kinds().count("restart_scheduled") >= i + 1, timeout=5
        ), sink.kinds()
    delays = [e[7] for e in sink.events if e[0] == "restart_scheduled"]
    assert delays == [0.2, 0.4, 0.8, 1.6], f"backoff must be exact: {delays}"
    check("SUP-05 backoff formula exact at jitter_ratio=0")


def sup_06() -> None:
    # Deterministic restart-factory-failure flow (row 4b): the factory
    # succeeds at first start, then fails on every restart.
    sink = Sink()
    eng = make_engine_with({"fast": (3, 60, 0.01, 0.02, 0.0)}, sink)
    state = {"fail_factory": False}

    def factory():
        if state["fail_factory"]:
            raise RuntimeError("factory boom")
        return lambda cmd: (_ for _ in ()).throw(RuntimeError("handler dies"))

    aid = eng.spawn("rf", factory, capacity=4, profile="fast",
                    dead_letter_sink=sink.dead_letter)
    state["fail_factory"] = True
    # One handler failure plus three restart-factory failures exhaust the
    # budget (max_restarts=3): the fourth record quarantines.
    eng.tell(aid, "rf", "die")
    assert wait_until(lambda: "child_quarantined" in sink.kinds(), timeout=10), sink.kinds()
    fails = [e for e in sink.events if e[0] == "child_failed"]
    assert len(fails) == 4, ("CL-1", [e[0] for e in sink.events])
    assert [e[0] for e in sink.events][-2:] == ["child_failed", "child_quarantined"], "CL-1"
    restarts = [e for e in sink.events if e[0] == "restart_scheduled"]
    assert len(restarts) == 3
    assert fails[0][4] == "str", "handler failure reports the command type"
    assert all(e[4] is None for e in fails[1:]), "factory failures have command_type None"
    snap = eng.snapshot(aid, "rf")
    assert snap[1] == 1, f"factory-failure restarts never bump generation: {snap}"
    assert snap[0] == "quarantined" and snap[4] == 4, snap
    assert sink.dead_reasons() == [], "queue was empty: no dead letters"
    check("SUP-06 restart factory failure consumes budget, no running actor without handler")


def sup_07_stp_04() -> None:
    sink = Sink()
    eng = make_engine_with({"p": (0, 60, 0.01, 0.02, 0.0)}, sink, jitter_seed=3)
    recover = {"ok": False}

    def rz_factory():
        def handler(cmd):
            if not recover["ok"]:
                raise RuntimeError("not yet")
            return cmd

        return handler

    aid = eng.spawn("rz", rz_factory, capacity=2, profile="p",
                    dead_letter_sink=sink.dead_letter)
    eng.tell(aid, "rz", "die")
    assert wait_until(lambda: "child_quarantined" in sink.kinds(), timeout=3), sink.kinds()
    assert eng.reset(aid, "rz") is True
    assert eng.reset(aid, "rz") is False, "reset only from quarantined"
    assert eng.reset("act-nope", "x") is False
    recover["ok"] = True
    assert wait_until(lambda: "child_restarted" in sink.kinds(), timeout=3), sink.kinds()
    assert wait_until(lambda: eng.snapshot(aid, "rz")[0] == "running", timeout=3), \
        eng.snapshot(aid, "rz")
    snap = eng.snapshot(aid, "rz")
    assert snap[0] == "running" and snap[1] == 2, f"reset re-entry bumps generation: {snap}"
    eng.tell(aid, "rz", "alive")
    assert wait_until(lambda: sink.kinds().count("command_completed") >= 1, timeout=3), sink.kinds()
    check("SUP-07/STP-04 reset from quarantine at delay 0")


def sup_09() -> None:
    sink = Sink()
    eng = make_engine(sink)

    class CustomBase(BaseException):
        pass

    def factory():
        def handler(cmd):
            if cmd == "exit":
                raise SystemExit(7)
            if cmd == "intr":
                raise KeyboardInterrupt()
            if cmd == "custom":
                raise CustomBase("cb")
            return cmd

        return handler

    aid = eng.spawn("base", factory, capacity=4, profile="state_authority")
    eng.tell(aid, "base", "exit")
    assert wait_until(lambda: "child_failed" in sink.kinds(), timeout=3), sink.kinds()
    assert wait_until(lambda: "child_restarted" in sink.kinds(), timeout=5), sink.kinds()
    gen1 = eng.snapshot(aid, "base")[1]
    eng.tell(aid, "base", "custom")
    assert wait_until(lambda: eng.snapshot(aid, "base")[1] == gen1 + 1, timeout=5), sink.kinds()
    codes = [e[5] for e in sink.events if e[0] == "child_failed"]
    assert "SystemExit" in codes and "CustomBase" in codes, codes
    check("SUP-09 BaseException family -> supervised child failure")


def sup_10() -> None:
    eng = make_engine()

    def bad_factory():
        raise RuntimeError("no start")

    try:
        eng.spawn("never", bad_factory, capacity=1, profile="state_authority")
        raise AssertionError("expected ActorSpawnError")
    except native.ActorSpawnError as err:
        assert err.__cause__ is not None and isinstance(err.__cause__, RuntimeError)
    try:
        eng.snapshot("act-99999999", "never")
        raise AssertionError("expected KeyError: no actor record may exist")
    except KeyError:
        pass
    assert eng.tell("act-99999999", "never", "x") == 2
    check("SUP-10 first-start factory failure leaves no record")


# ---------------------------------------------------------------- STP/CUS


def stp_01_03_cus_03_05_07() -> None:
    release = threading.Event()

    def factory():
        def handler(cmd):
            if cmd == "block":
                release.wait(5)
            return cmd

        return handler

    sink = Sink()
    eng = make_engine(sink)
    aid = eng.spawn("st", factory, capacity=4, profile="state_authority",
                    dead_letter_sink=sink.dead_letter)
    eng.tell(aid, "st", "block")
    assert wait_until(lambda: eng.snapshot(aid, "st")[3] == 1, timeout=2), eng.snapshot(aid, "st")
    # CUS-05: stop timeout -> False, no dead letters yet.
    assert eng.stop(aid, "st", 0.05) is False
    assert sink.dead_reasons() == []
    # Concurrent stoppers join the same confirmation (STP-01).
    results: list[bool] = []
    stop_lock = threading.Lock()

    def stopper(t: float) -> None:
        ok = eng.stop(aid, "st", t)
        with stop_lock:
            results.append(ok)

    threads = [threading.Thread(target=stopper, args=(3.0,)) for _ in range(3)]
    for t in threads:
        t.start()
    time.sleep(0.1)
    release.set()
    for t in threads:
        t.join()
    assert results == [True, True, True], results
    assert wait_until(lambda: sink.kinds().count("child_stopped") == 1, timeout=2), sink.kinds()
    assert eng.snapshot(aid, "st")[0] == "stopped"
    # CUS-07: repeated stop on stopped -> True, no second drain.
    assert eng.stop(aid, "st", 0.1) is True
    assert sink.kinds().count("child_stopped") == 1
    assert sink.dead_reasons() == []  # queue was empty
    # CUS-03 with queued messages.
    sink2 = Sink()
    eng2 = make_engine(sink2)
    gate2 = threading.Event()

    def gated_factory():
        def handler(cmd):
            if cmd == "gate":
                gate2.wait(5)
            return cmd

        return handler

    aid2 = eng2.spawn("st2", gated_factory, capacity=4, profile="state_authority",
                      dead_letter_sink=sink2.dead_letter)
    eng2.tell(aid2, "st2", "gate")       # in flight: completes at stop
    eng2.tell(aid2, "st2", "q1")         # queued
    eng2.tell(aid2, "st2", "q2")         # queued
    assert wait_until(lambda: eng2.snapshot(aid2, "st2")[2] == 2, timeout=2), \
        eng2.snapshot(aid2, "st2")
    stop_result2: dict = {}

    def stopper2() -> None:
        stop_result2["r"] = eng2.stop(aid2, "st2", 2.0)

    stopper2_t = threading.Thread(target=stopper2)
    stopper2_t.start()
    time.sleep(0.1)
    gate2.set()
    stopper2_t.join(timeout=4)
    assert stop_result2["r"] is True, stop_result2
    assert wait_until(
        lambda: sink2.dead_reasons() == ["stopped", "stopped"], timeout=2
    ), sink2.dead_reasons()
    with sink2.lock:
        messages = [m for m, _ in sink2.dead]
    assert messages == ["q1", "q2"], "admission order"
    # STP-03: stop during restart_pending cancels the restart.
    sink3 = Sink()
    eng3 = make_engine(sink3)
    aid3 = eng3.spawn("st3", lambda: lambda c: (_ for _ in ()).throw(RuntimeError()),
                      capacity=2, profile="state_authority", dead_letter_sink=sink3.dead_letter)
    eng3.tell(aid3, "st3", "die")
    assert wait_until(lambda: "restart_scheduled" in sink3.kinds(), timeout=3), sink3.kinds()
    assert eng3.stop(aid3, "st3", 2.0) is True
    time.sleep(0.3)
    assert "child_restarted" not in sink3.kinds(), "restart must be cancelled"
    check("STP-01/03 + CUS-03/05/07 stop semantics")


def stp_02() -> None:
    # Self-stop: the handler stops its own actor; external stoppers join.
    sink = Sink()
    eng = make_engine(sink)
    holder: list[str] = [""]

    def factory():
        def handler(cmd):
            if cmd == "self":
                eng.stop(holder[0], "selfstop", 5.0)
            return cmd

        return handler

    aid = eng.spawn("selfstop", factory, capacity=4, profile="state_authority",
                    dead_letter_sink=sink.dead_letter)
    holder[0] = aid
    eng.tell(aid, "selfstop", "self")
    # Self-stop returns immediately (non-blocking); the external join path
    # must not deadlock either.
    assert eng.stop(aid, "selfstop", 0.05) in (True, False)
    assert wait_until(lambda: sink.kinds().count("child_stopped") == 1, timeout=3), sink.kinds()
    assert sink.kinds().count("child_stopped") == 1, "exactly one confirmation"
    check("STP-02 self-stop non-blocking, exactly one confirmation")


def cus_02() -> None:
    sink = Sink()
    eng = make_engine_with({"p": (0, 60, 0.01, 0.02, 0.0)}, sink, jitter_seed=3)
    aid = eng.spawn("cq", lambda: lambda c: (_ for _ in ()).throw(RuntimeError()),
                    capacity=4, profile="p", dead_letter_sink=sink.dead_letter)
    eng.tell(aid, "cq", "die")
    eng.tell(aid, "cq", "queued-1")
    eng.tell(aid, "cq", "queued-2")
    assert wait_until(lambda: "child_quarantined" in sink.kinds(), timeout=3), sink.kinds()
    assert sink.dead_reasons() == ["quarantined", "quarantined"], sink.dead_reasons()
    with sink.lock:
        messages = [m for m, _ in sink.dead]
    assert messages == ["queued-1", "queued-2"]
    assert eng.stop(aid, "cq", 1.0) is True
    assert sink.kinds().count("child_stopped") == 1
    assert sink.dead_reasons().count("stopped") == 0, "quarantine already drained"
    check("CUS-02 quarantine drains mailbox; later stop sends none")


def cus_04() -> None:
    sink = Sink()
    eng = make_engine(sink)
    secret = {"token": "opaque-payload"}
    aid = eng.spawn("opaque", calling_factory, capacity=2, profile="state_authority",
                    dead_letter_sink=sink.dead_letter)
    release, entered = threading.Event(), threading.Event()
    assert eng.tell(aid, "opaque", blocker(release, entered)) == 0
    assert entered.wait(3.0), "the blocking command must start"
    assert eng.tell(aid, "opaque", secret) == 0
    threading.Timer(0.05, release.set).start()
    assert eng.stop(aid, "opaque", 3.0) is True
    with sink.lock:
        assert [r for m, r in sink.dead if m is secret] == ["stopped"], \
            "the exact object must be delivered"
    check("CUS-04 dead letters carry the object untouched")


def cus_06() -> None:
    sink = Sink()
    eng = make_engine(sink)
    owner = eng.spawn("owner", lambda: lambda c: c, capacity=16, profile="state_authority")
    pool = eng.spawn_pool("p", lambda: lambda c: c, pool_size=1, capacity=4,
                          profile="state_authority", dead_letter_sink=sink.dead_letter)
    assert eng.pool_reserve(pool, "p", "res1", 1) == 0
    assert eng.pool_cancel_reservation(pool, "p", "res1", 1) is True, "cancel reserved"
    assert eng.pool_cancel_reservation(pool, "p", "res1", 1) is False
    assert eng.acknowledge(pool, "p", "res1", 1) is False, "token gone"
    assert eng.pool_submit(pool, "p", owner, "owner", "sub1", 1, "x") == 0
    assert eng.pool_cancel_reservation(pool, "p", "sub1", 1) is False, \
        "cancel after submit is False (EFF-05)"
    assert eng.pool_submit_reserved(pool, "p", owner, "owner", "ghost", 1, "y") == 2
    assert eng.acknowledge(pool, "p", "ghost", 1) is False
    # A different generation is a different key (no silent re-creation).
    assert eng.pool_reserve(pool, "p", "op2", 1) == 0
    assert eng.acknowledge(pool, "p", "op2", 2) is False
    assert eng.pool_cancel_reservation(pool, "p", "op2", 2) is False
    assert eng.pool_cancel_reservation(pool, "p", "op2", 1) is True
    check("CUS-06 custody token fencing")


# ---------------------------------------------------------------- TMR


def tmr_01_02_03_06() -> None:
    sink = Sink()
    eng = make_engine(sink)
    aid = eng.spawn("t", lambda: lambda c: c, capacity=4, profile="state_authority",
                    dead_letter_sink=sink.dead_letter)
    tid = eng.send_after(aid, "t", 0.05, "tick")
    t0 = time.monotonic()
    assert wait_until(lambda: sink.kinds().count("command_completed") == 1, timeout=1), sink.kinds()
    fired = time.monotonic() - t0
    assert 0.0 <= fired <= 0.3, f"timer fired after {fired:.3f}s"
    assert eng.cancel_after(tid) is False, "already fired -> False"
    assert eng.cancel_after("timer-nope") is False
    try:
        eng.send_after("act-nope", "t", 0.05, "x")
        raise AssertionError("expected KeyError")
    except KeyError:
        pass
    try:
        eng.send_after(aid, "forged", 0.05, "x")
        raise AssertionError("expected KeyError (forged)")
    except KeyError:
        pass
    try:
        eng.send_after(aid, "t", -1.0, "x")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    # TMR-02: generation fence drops timers scheduled before a restart.
    sink2 = Sink()
    eng2 = make_engine(sink2)
    aid2 = eng2.spawn(
        "t2",
        lambda: lambda c: (_ for _ in ()).throw(RuntimeError()) if c == "die" else c,
        capacity=4, profile="state_authority", dead_letter_sink=sink2.dead_letter,
    )
    eng2.send_after(aid2, "t2", 0.4, "stale")
    eng2.tell(aid2, "t2", "die")
    assert wait_until(lambda: "child_restarted" in sink2.kinds(), timeout=5), sink2.kinds()
    time.sleep(0.6)
    with sink2.lock:
        stale = [e for e in sink2.events
                 if e[0] == "command_completed" and e[4] == "str"]
    assert not stale, f"stale-generation timer must drop silently: {stale}"
    assert sink2.dead_reasons() == [], sink2.dead_reasons()
    check("TMR-01/02/03/06 timers, fences, validation")


def tmr_04() -> None:
    sink = Sink()
    eng = make_engine(sink)
    release = threading.Event()

    def factory():
        def handler(cmd):
            if cmd == "hold":
                release.wait(10)
            return cmd

        return handler

    aid = eng.spawn("tf", factory, capacity=1, profile="state_authority",
                    dead_letter_sink=sink.dead_letter)
    # Fire into a FULL mailbox while running: the payload retries at the
    # custody cadence and stays pending (TMR-04 first row).
    eng.tell(aid, "tf", "hold")          # in flight; queue empty -> room
    assert wait_until(lambda: eng.snapshot(aid, "tf")[3] == 1, timeout=2)
    eng.tell(aid, "tf", "occupy")        # queue full now
    assert wait_until(lambda: eng.snapshot(aid, "tf")[2] == 1, timeout=2)
    eng.send_after(aid, "tf", 0.05, "after-stop")
    time.sleep(0.2)                      # timer fired into the full mailbox
    assert wait_until(lambda: eng.snapshot(aid, "tf")[2] == 1, timeout=2), \
        "payload must keep retrying, not occupy a queue slot"
    # Stop acceptance fences the due entry; the next retry pass classifies
    # against `stopping` -> dead letter with reason stopped (TMR-04/TMR-05).
    stop_result: dict = {}

    def stopper() -> None:
        stop_result["r"] = eng.stop(aid, "tf", 5.0)

    stopper_t = threading.Thread(target=stopper)
    stopper_t.start()
    assert wait_until(lambda: sink.dead_reasons() == ["stopped"], timeout=3), \
        (sink.dead_reasons(), sink.kinds())
    with sink.lock:
        assert any(m == "after-stop" for m, _ in sink.dead)
    release.set()
    stopper_t.join(timeout=8)
    assert stop_result["r"] is True, stop_result
    check("TMR-04 fire-time classification")


# ---------------------------------------------------------------- ASK/SNP


def ask_01_02_03_04() -> None:
    sink = Sink()
    eng = make_engine(sink)
    def fast_factory():
        def handler(cmd):
            if cmd == "slow":
                time.sleep(1.0)
                return "late"
            if cmd == "boom":
                raise Expected()
            return cmd * 2

        return handler

    aid = eng.spawn("a", fast_factory, capacity=4, profile="state_authority")
    assert eng.ask(aid, "a", 21, 2.0) == 42
    try:
        eng.ask(aid, "a", "boom", 2.0)
        raise AssertionError("expected the original exception instance")
    except Expected:
        pass
    t0 = time.monotonic()
    try:
        eng.ask(aid, "a", "slow", 0.1)
        raise AssertionError("expected TimeoutError")
    except TimeoutError:
        pass
    assert 0.09 <= time.monotonic() - t0 <= 0.5
    try:
        eng.ask("act-nope", "a", "x", 0.1)
        raise AssertionError("expected ActorAdmissionError")
    except native.ActorAdmissionError as err:
        assert "closed" in str(err)
    pool = eng.spawn_pool("p", lambda: lambda c: c, pool_size=1, capacity=2,
                          profile="state_authority")
    try:
        eng.ask(pool, "p", "x", 0.1)
        raise AssertionError("pool target must classify closed")
    except native.ActorAdmissionError:
        pass
    # Other child failure -> ActorFailedError for the failed ask only.
    aid2 = eng.spawn(
        "f",
        lambda: lambda c: (_ for _ in ()).throw(RuntimeError()) if c == "die" else c,
        capacity=4, profile="state_authority",
    )
    done: dict = {}

    def asker():
        try:
            eng.ask(aid2, "f", "die", 5.0)
        except native.ActorFailedError:
            done["failed"] = True
        except Exception as err:  # noqa: BLE001
            done["failed"] = False
            done["err"] = err

    th = threading.Thread(target=asker)
    th.start()
    time.sleep(0.05)
    eng.tell(aid2, "f", "die")  # failure while the ask is pending
    th.join(timeout=6)
    assert done.get("failed") is True, done
    check("ASK-01..04 ask semantics")


def snp_01_02() -> None:
    eng = make_engine()
    aid = eng.spawn("s", lambda: lambda c: c, capacity=3, profile="state_authority")
    assert eng.snapshot(aid, "s") == ("running", 1, 0, 0, 0)
    eng.tell(aid, "s", "x")
    assert eng.snapshot(aid, "s")[2] >= 0
    for handle in (("act-nope", "s"), (aid, "forged")):
        try:
            eng.snapshot(*handle)
            raise AssertionError(f"expected KeyError for {handle}")
        except KeyError:
            pass
    check("SNP-01/02 snapshot")


def main() -> None:
    from engine_behavior_pools import (
        drn_01_02_03_06_07_08,
        drn_07,
        drn_09,
        eff_01_02,
        eff_03_04_05_10_11_13,
        eff_06_07_08,
        eff_09_eff_12,
        eff_completed_type,
        life_01_02_03,
        life_version,
    )

    tests = [
        adm_01_02_04_05,
        ord_01_02_03,
        ord_04_liv_04,
        evt_01_02_03,
        exp_01_02,
        sup_01_08,
        sup_02,
        sup_03_04,
        sup_05,
        sup_06,
        sup_07_stp_04,
        sup_09,
        sup_10,
        stp_01_03_cus_03_05_07,
        stp_02,
        cus_02,
        cus_04,
        cus_06,
        tmr_01_02_03_06,
        tmr_04,
        ask_01_02_03_04,
        snp_01_02,
        eff_01_02,
        eff_03_04_05_10_11_13,
        eff_06_07_08,
        eff_09_eff_12,
        drn_01_02_03_06_07_08,
        drn_07,
        drn_09,
        life_01_02_03,
        life_version,
        eff_completed_type,
    ]
    failures = 0
    for test in tests:
        try:
            test()
        except Exception as err:  # noqa: BLE001
            failures += 1
            print(f"FAIL {test.__name__}: {err!r}", flush=True)
    print(f"\n{len(tests) - failures}/{len(tests)} groups passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
