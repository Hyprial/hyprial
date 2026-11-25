"""Pool, drain and lifecycle groups of the native engine behaviour self-check.

Run through ``engine_behavior.py``; see its docstring.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from behavior_support import (  # noqa: E402
    ENGINE_ABI_VERSION,
    EffectCompleted,
    Sink,
    check,
    make_engine,
    wait_until,
)


# ---------------------------------------------------------------- EFF


def eff_01_02() -> None:
    sink = Sink()
    eng = make_engine(sink)
    completions: list[EffectCompleted] = []
    comp_lock = threading.Lock()
    pool_slot: dict[str, str] = {}

    def make_owner(name: str, pool_key: str):
        def factory():
            def handler(cmd):
                if isinstance(cmd, EffectCompleted):
                    with comp_lock:
                        completions.append(cmd)
                    eng.acknowledge(pool_slot[pool_key], pool_key,
                                    cmd.operation_id, cmd.generation)
                return cmd

            return handler

        return factory

    owner = eng.spawn("own2", make_owner("own2", "pl2"), capacity=32,
                      profile="state_authority")
    err_owner = eng.spawn("own3", make_owner("own3", "pl3"), capacity=32,
                          profile="state_authority")

    # pool_size=1: strict submission order (EFF-01).
    order: list[str] = []
    order_lock = threading.Lock()

    def worker_factory():
        def handler(c):
            with order_lock:
                order.append(str(c))
            time.sleep(0.01)
            return c

        return handler

    pool_slot["pl2"] = eng.spawn_pool("pl2", worker_factory, pool_size=1,
                                      capacity=8, profile="state_authority")
    for i in range(5):
        assert eng.pool_submit(pool_slot["pl2"], "pl2", owner, "own2",
                               f"op{i}", 1, f"j{i}") == 0, f"submit j{i}"
    assert wait_until(lambda: len(completions) >= 5, timeout=5), len(completions)
    with order_lock:
        assert order == [f"j{i}" for i in range(5)], f"strict FIFO: {order}"

    # Worker error -> EffectCompleted(error=...); the worker continues
    # with the next queued payload (EFF-02).
    def bad_factory():
        def handler(c):
            raise ValueError("worker-error")

        return handler

    pool_slot["pl3"] = eng.spawn_pool("pl3", bad_factory, pool_size=1,
                                      capacity=4, profile="state_authority")
    assert eng.pool_submit(pool_slot["pl3"], "pl3", err_owner, "own3",
                           "err1", 1, "p") == 0, "submit err1"
    assert eng.pool_submit(pool_slot["pl3"], "pl3", err_owner, "own3",
                           "err2", 1, "p") == 0, "submit err2"
    assert wait_until(
        lambda: len({c.operation_id for c in completions if c.error}) >= 2,
        timeout=3,
    ), completions
    check("EFF-01/02 pool order + error-as-completion, worker continues")


def eff_03_04_05_10_11_13() -> None:
    sink = Sink()
    eng = make_engine(sink)
    owner = eng.spawn("o", lambda: lambda c: c, capacity=16, profile="state_authority")
    pool = eng.spawn_pool("pr", lambda: lambda c: c, pool_size=1, capacity=2,
                          profile="state_authority")
    # EFF-10: duplicate reserve idempotent, no capacity re-check.
    assert eng.pool_reserve(pool, "pr", "r1", 1) == 0
    assert eng.pool_reserve(pool, "pr", "r1", 1) == 0
    assert eng.pool_reserve(pool, "pr", "r2", 1) == 0
    assert eng.pool_reserve(pool, "pr", "r3", 1) == 1, "capacity=2 exhausted"
    assert eng.pool_cancel_reservation(pool, "pr", "r1", 1) is True
    assert eng.pool_reserve(pool, "pr", "r3", 1) == 0
    # EFF-04: submit_reserved succeeds regardless of backlog; binds owner.
    assert eng.pool_submit_reserved(pool, "pr", owner, "o", "r3", 1, "payload") == 0
    assert eng.pool_submit_reserved(pool, "pr", owner, "o", "r3", 1, "payload2") == 0, \
        "duplicate submit_reserved queues nothing"
    # EFF-05: cancel after submit -> False.
    assert eng.pool_cancel_reservation(pool, "pr", "r3", 1) is False, \
        "cancel after submit (EFF-05)"
    # EFF-11: wrong target kind.
    assert eng.tell(pool, "pr", "x") == 2
    try:
        eng.send_after(pool, "pr", 0.05, "x")
        raise AssertionError("expected ValueError for pool timer target")
    except ValueError:
        pass
    assert eng.pool_submit(owner, "o", owner, "o", "k", 1, "v") == 2, "pool_* on serial"
    assert eng.pool_reserve(owner, "o", "k", 1) == 2
    assert eng.pool_cancel_reservation(owner, "o", "k", 1) is False
    # EFF-13: forged owner.
    assert eng.pool_submit(pool, "pr", owner, "FORGED", "f1", 1, "v") == 2
    assert eng.pool_submit_reserved(pool, "pr", "act-nope", "o", "f2", 1, "v") == 2
    assert eng.acknowledge(pool, "pr", "r3", 1) is True, "final ack settles r3"
    assert eng.acknowledge(pool, "pr", "r3", 1) is False, "ack is idempotent"
    check("EFF-03/04/05/10/11/13 reservation protocol")


def eff_06_07_08() -> None:
    sink = Sink()
    eng = make_engine(sink)
    deliver_count = {"n": 0}
    dc_lock = threading.Lock()
    block_owner = threading.Event()
    pool_slot: list[str] = [""]

    def owner_factory():
        def handler(cmd):
            if isinstance(cmd, EffectCompleted):
                if cmd.operation_id == "slow-ack":
                    block_owner.wait(5)  # ack late: at-least-once duplicates
                with dc_lock:
                    deliver_count["n"] += 1
                eng.acknowledge(pool_slot[0], "pl", cmd.operation_id, cmd.generation)
            return cmd

        return handler

    owner = eng.spawn("ow", owner_factory, capacity=1,  # tiny mailbox -> overloaded retries
                      profile="state_authority")
    pool = eng.spawn_pool("pl", lambda: lambda c: c, pool_size=1, capacity=4,
                          profile="state_authority", dead_letter_sink=sink.dead_letter,
                          retry_interval=0.01)
    pool_slot[0] = pool
    assert eng.pool_submit(pool, "pl", owner, "ow", "slow-ack", 1, "p1") == 0
    time.sleep(0.25)  # retries pile up while the ack is blocked
    block_owner.set()
    assert wait_until(lambda: deliver_count["n"] >= 1, timeout=3), deliver_count
    assert wait_until(lambda: eng.pool_reserve(pool, "pl", "post-ack", 1) == 0, timeout=3), \
        "token settled by ack -> capacity freed"
    assert eng.pool_cancel_reservation(pool, "pl", "post-ack", 1) is True
    # EFF-07: owner death -> completion once to pool sink with owner reason.
    sink2 = Sink()
    eng2 = make_engine(sink2)
    owner2 = eng2.spawn("ow2", lambda: lambda c: c, capacity=4, profile="state_authority")
    pool2 = eng2.spawn_pool("pl2", lambda: lambda c: c, pool_size=1, capacity=4,
                            profile="state_authority", dead_letter_sink=sink2.dead_letter,
                            retry_interval=0.01)
    assert eng2.pool_submit(pool2, "pl2", owner2, "ow2", "dying", 1, "p") == 0
    assert eng2.stop(owner2, "ow2", 1.0) is True
    assert wait_until(lambda: sink2.dead_reasons() == ["stopped"], timeout=3), sink2.dead_reasons()
    with sink2.lock:
        dead = [m for m, _ in sink2.dead]
    assert any(isinstance(m, EffectCompleted) and m.operation_id == "dying" for m in dead), \
        "the EffectCompleted instance itself is dead-lettered"
    assert eng2.pool_reserve(pool2, "pl2", "after-death", 1) == 0, "token settled on owner death"
    assert eng2.pool_cancel_reservation(pool2, "pl2", "after-death", 1) is True
    check("EFF-06/07/08 completion custody")


def eff_09_eff_12() -> None:
    sink = Sink()
    eng = make_engine(sink)
    done: list[EffectCompleted] = []
    d_lock = threading.Lock()
    pool_slot: list[str] = [""]

    def owner_factory():
        def handler(cmd):
            if isinstance(cmd, EffectCompleted):
                with d_lock:
                    done.append(cmd)
                eng.acknowledge(pool_slot[0], "pl", cmd.operation_id, cmd.generation)
            return cmd

        return handler

    owner2 = eng.spawn("o2", owner_factory, capacity=16, profile="state_authority")
    pool = eng.spawn_pool("pl", lambda: lambda c: c, pool_size=2, capacity=8,
                          profile="state_authority")
    pool_slot[0] = pool
    assert eng.snapshot(pool, "pl")[0] == "running"
    assert eng.snapshot(pool, "pl")[1] == 1, "pools report generation 1"
    for i in range(4):
        assert eng.pool_submit(pool, "pl", owner2, "o2", f"e{i}", 1, f"v{i}") == 0
    assert wait_until(lambda: len(done) == 4, timeout=5), len(done)
    with d_lock:
        for c in done:
            assert c.error is None and c.generation == 1, c.__repr__()
            assert c.result == f"v{int(c.operation_id[1:])}", c.__repr__()
    # EFF-09: pool stop drains the queue and settles custody before returning.
    assert eng.stop(pool, "pl", 3.0) is True
    assert eng.snapshot(pool, "pl")[0] == "stopped"
    assert eng.pool_submit(pool, "pl", owner2, "o2", "late", 1, "v") == 2
    check("EFF-09/12 pool stop + EffectCompleted fields")


# ---------------------------------------------------------------- DRN/LIFE


def drn_01_02_03_06_07_08() -> None:
    eng = make_engine()
    gate = threading.Event()

    def factory():
        def handler(cmd):
            if cmd == "hold":
                gate.wait(5)
            return cmd

        return handler

    hold = eng.spawn("hold", factory, capacity=4, profile="state_authority")
    idle = eng.spawn("idle", lambda: lambda c: c, capacity=4, profile="state_authority")
    eng.tell(hold, "hold", "hold")
    assert wait_until(lambda: eng.snapshot(hold, "hold")[3] == 1, timeout=2)
    results: dict = {}

    def drainer() -> None:
        results["r"] = eng.drain([hold, idle], 0.3)

    t = threading.Thread(target=drainer)
    t.start()
    time.sleep(0.1)
    # DRN-06: tell during drain -> closed; DRN-03: non-targets unaffected
    # (the actor spawned below is NOT in the drain set).
    assert eng.tell(hold, "hold", "blocked") == 2
    new_actor = eng.spawn("fresh", lambda: lambda c: c, capacity=4,
                          profile="state_authority")
    assert eng.tell(new_actor, "fresh", "welcome") == 0, "cell drain must not touch others"
    t.join(timeout=3)
    completed, elapsed, remaining = results["r"]
    assert completed is False and "hold" in remaining and "idle" in remaining, results
    gate.set()
    # DRN-02: a later drain call on the same set is the retry.
    completed2, _, remaining2 = eng.drain([hold, idle], 3.0)
    assert completed2 is True and remaining2 == [], (completed2, remaining2)
    # DRN-04: engine-wide drain completed -> spawn refused forever.
    completed3, _, _ = eng.drain(None, 3.0)
    assert completed3 is True
    try:
        eng.spawn("post", lambda: lambda c: c, capacity=1, profile="state_authority")
        raise AssertionError("spawn after drained must raise RuntimeError")
    except RuntimeError:
        pass
    check("DRN-01/02/03/06/08 + DRN-04 drain semantics")


def drn_07() -> None:
    # DRN-07: spawn DURING an in-progress engine-wide drain raises.
    eng = make_engine()
    gate = threading.Event()

    def factory():
        def handler(cmd):
            if cmd == "hold":
                gate.wait(5)
            return cmd

        return handler

    hold = eng.spawn("hold", factory, capacity=4, profile="state_authority")
    eng.tell(hold, "hold", "hold")
    assert wait_until(lambda: eng.snapshot(hold, "hold")[3] == 1, timeout=2)
    results: dict = {}

    def drainer() -> None:
        results["r"] = eng.drain(None, 5.0)

    t = threading.Thread(target=drainer)
    t.start()
    time.sleep(0.1)
    try:
        eng.spawn("mid", lambda: lambda c: c, capacity=1, profile="state_authority")
        raise AssertionError("spawn during engine-wide drain must raise RuntimeError")
    except RuntimeError:
        pass
    gate.set()
    t.join(timeout=10)
    assert results["r"][0] is True
    check("DRN-07 spawn during in-progress engine-wide drain")


def drn_09() -> None:
    eng = make_engine()
    pool_slot: list[str] = [""]

    def owner_factory():
        def handler(cmd):
            if isinstance(cmd, EffectCompleted):
                eng.acknowledge(pool_slot[0], "pl", cmd.operation_id, cmd.generation)
            return cmd

        return handler

    owner = eng.spawn("o", owner_factory, capacity=4, profile="state_authority")
    pool = eng.spawn_pool("pl", lambda: lambda c: c * 2, pool_size=1, capacity=4,
                          profile="state_authority")
    pool_slot[0] = pool
    results: dict = {}

    def drainer() -> None:
        results["r"] = eng.drain(None, 3.0)

    t = threading.Thread(target=drainer)
    t.start()
    time.sleep(0.05)
    # Submit AFTER drain start: external admission is closed...
    assert eng.pool_submit(pool, "pl", owner, "o", "late", 1, 21) == 2, \
        "pool_submit during engine-wide drain is closed"
    t.join(timeout=10)
    completed, _, remaining = results["r"]
    assert completed is True and remaining == [], results
    check("DRN-09 owner+pool same drain set, no wait cycle")


def life_01_02_03() -> None:
    # Clean subprocess exit with live actors and pools (LIFE-01), no engine
    # thread surviving shutdown (LIFE-02), idempotent shutdown (LIFE-03).
    code = """
import sys, os
sys.path.insert(0, {src!r})
from hyprial._core import actors as native
events = []
eng = native.Engine(policies={{"p": (3, 60, 0.05, 0.2, 0.1)}}, jitter_seed=1,
                    event_sink=events.append, expected_error_type=Exception)
aid = eng.spawn("a", lambda: lambda c: c, capacity=4, profile="p")
eng.tell(aid, "a", "x")
pool = eng.spawn_pool("p", lambda: lambda c: c, pool_size=1, capacity=2, profile="p")
own = eng.spawn("o", lambda: lambda c: c, capacity=4, profile="p")
eng.pool_submit(pool, "p", own, "o", "op", 1, "v")
assert eng.acknowledge(pool, "p", "op", 1) is True
assert eng.shutdown(5.0) is True, "shutdown must confirm"
assert eng.shutdown(5.0) is True, "idempotent"
print("SHUTDOWN-OK")
"""
    src = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))
    proc = subprocess.run(
        [sys.executable, "-c", code.format(src=src)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"subprocess exit {proc.returncode}: {proc.stderr}"
    assert "SHUTDOWN-OK" in proc.stdout, proc.stdout + proc.stderr
    check("LIFE-01/02/03 clean subprocess exit with live actors and pools")


def life_version() -> None:
    assert ENGINE_ABI_VERSION == 1
    check("LIFE-04 ENGINE_ABI_VERSION == 1 (adapter refuses other values)")


def eff_completed_type() -> None:
    assert hasattr(EffectCompleted, "operation_id")
    assert hasattr(EffectCompleted, "generation")
    assert hasattr(EffectCompleted, "result")
    assert hasattr(EffectCompleted, "error")
    check("EFF-12 EffectCompleted type exported with exact fields")
