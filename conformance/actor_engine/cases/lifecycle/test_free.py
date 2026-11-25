# Decision record (file header; no inline comments by repository rule):
# - FREE-01 is the stress half of "engine state correct without GIL reliance"
#   (the review half is out of scope for an executable suite). Many producer
#   threads tell across actors while other threads snapshot, stop some
#   targets mid-stream and run pool submit / reserve / cancel / ack traffic.
#   Invariants: every ACCEPTED tell (result 0) is executed or dead-lettered
#   exactly once (no loss, no duplication), per-producer FIFO holds over
#   executed-then-dead-lettered order (admission order, 5.2/7), snapshots stay
#   within bounds, every accepted pool payload completes once (distinct ids).
# - FREE-02 proves callbacks are not serialized across actors: handler A
#   blocks on an Event that only handler B (another actor) sets, and a ring
#   of handlers meets at a Barrier that needs all of them in flight at once
#   (12; also LIV-01: a blocked handler must not stall other actors).
# - FREE-03 (fail closed, never skipped):
#   * `--free-threaded-python PATH` (or env HYPRIAL_CONFORMANCE_FT_PYTHON)
#     names a free-threaded interpreter. A subprocess of it puts env
#     HYPRIAL_CONFORMANCE_FT_PATH (optional) FIRST on sys.path, then the suite
#     dir (never the repo `src/`, whose abi3 module cannot load there),
#     imports the backend module (native: hyprial._core.actors; reference:
#     backends.reference_abi) and asserts sys._is_gil_enabled() is False
#     before and after the import. The gating report covers ONLY the import
#     (12: import must not crash nor re-enable the GIL); behaviour on
#     free-threaded builds is not required to pass in v1 (12, D12), so this
#     probe never builds an Engine. A non-free-threaded interpreter given as
#     the option fails the case.
#   * Without the option, reference-abi runs the import probe plus an
#     Engine / ask / shutdown check on the current interpreter (a GIL build,
#     where behaviour is required anyway) and asserts the import leaves the
#     GIL state unchanged.
#   * The subject of the free-threaded probe is the native module; P8 removed
#     the pykka backend, so only reference-abi takes the current-interpreter
#     probe.
#   * Without the option, native is BLOCKED (HQ review): the native module's
#     free-threaded claim cannot be proven on a GIL build, so the case is
#     reported explicitly unavailable: `pytest.skip("BLOCKED: ...")` with
#     support.verdict.BLOCKED_PREFIX. The session verdict (conftest) turns
#     that into CONFORMANCE VERDICT: BLOCKED with exit status 7 - never PASS,
#     never a silent skip (any other skip makes the verdict FAIL).
#   The current-interpreter probe never receives HYPRIAL_CONFORMANCE_FT_PATH:
#   that stage directory holds a bare `hyprial` package (only `_core`) and
#   would shadow the real one.
#   PYTHON_GIL is removed from the child environment so it cannot mask the
#   module's own declaration.
"""FREE: no GIL reliance under stress, no cross-actor serialization, free-threaded import."""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import backends
from support.helpers import WAIT, Call, DeadLetters, Rec, Recorder, case, wait_until
from support.verdict import BLOCKED_PREFIX

SUITE = Path(__file__).resolve().parents[2]
STATES = {"running", "restart_pending", "quarantined", "stopping", "stopped"}


def _retrying_tell(h, actor, command: object, attempts: int = 5000) -> int:
    for _ in range(attempts):
        outcome = h.tell(actor, command)
        if outcome != 1:
            return outcome
        time.sleep(0.0005)
    return 1


@case("FREE-01")
def test_free_01_concurrent_engine_operations_keep_invariants(h) -> None:
    """Concurrent tell / snapshot / stop / pool traffic: no lost or duplicated message, per-producer FIFO."""
    capacity = 8
    live = [h.spawn(capacity=capacity) for _ in range(4)]
    letters = [DeadLetters() for _ in range(2)]
    doomed = [h.spawn(capacity=capacity, dead_letters=sink) for sink in letters]
    actors = live + doomed
    logs = {actor.actor_id: Recorder() for actor in actors}
    accepted = {actor.actor_id: Recorder() for actor in actors}
    pool = h.spawn_pool(pool_size=3, capacity=16)
    completions = Recorder()
    owner = h.spawn_owner(pool, completions)
    errors = Recorder()
    done = threading.Event()
    producers, per_producer = 8, 400
    submitted = Recorder()

    def produce(producer: int) -> None:
        rng = random.Random(producer)
        try:
            for seq in range(per_producer):
                actor = rng.choice(actors)
                item = (producer, seq)
                if _retrying_tell(h, actor, Rec(logs[actor.actor_id], item)) == 0:
                    accepted[actor.actor_id].append(item)
        except BaseException as exc:
            errors.append(("produce", repr(exc)))

    def snapshots() -> None:
        try:
            while not done.is_set():
                for actor in actors:
                    state, generation, queued, in_flight, failures = h.snapshot(actor)
                    if (state not in STATES or generation != 1 or not 0 <= queued <= capacity
                            or in_flight not in (0, 1) or failures != 0):
                        errors.append(("snapshot", actor.name, state, generation, queued, in_flight))
                time.sleep(0.001)
        except BaseException as exc:
            errors.append(("snapshot", repr(exc)))

    def stopper() -> None:
        try:
            wait_until(lambda: sum(len(r) for r in accepted.values()) > 600, timeout=WAIT)
            for actor in doomed:
                if h.stop(actor, timeout=WAIT) is not True:
                    errors.append(("stop", actor.name))
        except BaseException as exc:
            errors.append(("stop", repr(exc)))

    def pool_traffic() -> None:
        engine = h.engine
        try:
            for index in range(300):
                op = f"op{index}"
                for _ in range(5000):
                    outcome = engine.pool_submit(pool.actor_id, pool.name, owner.actor_id, owner.name,
                                                 op, 1, Call(lambda: None))
                    if outcome != 1:
                        break
                    time.sleep(0.0005)
                if outcome == 0:
                    submitted.append(op)
                reservation = f"res{index}"
                if engine.pool_reserve(pool.actor_id, pool.name, reservation, 1) == 0:
                    if engine.pool_cancel_reservation(pool.actor_id, pool.name, reservation, 1) is not True:
                        errors.append(("cancel", reservation))
        except BaseException as exc:
            errors.append(("pool", repr(exc)))

    threads = [threading.Thread(target=produce, args=(p,), daemon=True) for p in range(producers)]
    threads += [threading.Thread(target=stopper, daemon=True), threading.Thread(target=pool_traffic, daemon=True)]
    observer = threading.Thread(target=snapshots, daemon=True)
    observer.start()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30.0)
    done.set()
    observer.join(WAIT)
    assert not any(thread.is_alive() for thread in threads + [observer])
    assert errors.items() == []

    def delivered(actor) -> list:
        executed = logs[actor.actor_id].items()
        if actor in doomed:
            executed = executed + [message.value for message, _ in letters[doomed.index(actor)].items()]
        return executed

    for actor in actors:
        expected = sorted(accepted[actor.actor_id].items())
        assert wait_until(lambda: len(delivered(actor)) >= len(expected), timeout=10.0), actor.name
        got = delivered(actor)
        assert sorted(got) == expected, actor.name
        for producer in range(producers):
            seqs = [seq for p, seq in got if p == producer]
            assert seqs == sorted(seqs), (actor.name, producer)
    for sink in letters:
        assert {reason for _, reason in sink.items()} <= {"stopped"}
    assert sum(len(r) for r in accepted.values()) > 0
    expected_ops = set(submitted.items())
    assert len(expected_ops) == len(submitted)
    assert wait_until(lambda: {m.operation_id for m in completions.items()} >= expected_ops, timeout=10.0)
    assert {m.operation_id for m in completions.items()} == expected_ops


@case("FREE-02")
def test_free_02_handler_waits_on_event_set_by_other_actor(h) -> None:
    """Handler A blocks on an Event that only handler B on another actor sets: no deadlock."""
    first, second = h.spawn(), h.spawn()
    signal, a_started = threading.Event(), threading.Event()
    outcome = Recorder()

    def wait_for_b() -> None:
        a_started.set()
        outcome.append(signal.wait(WAIT))

    assert h.tell(first, Call(wait_for_b)) == 0
    assert a_started.wait(WAIT)
    assert h.tell(second, Call(signal.set)) == 0
    assert outcome.wait_len(1)
    assert outcome.items() == [True]


@case("FREE-02")
def test_free_02_ring_of_handlers_runs_concurrently(h) -> None:
    """Eight handlers on eight actors must all be in flight at once to pass a Barrier, five rounds."""
    size = 8
    actors = [h.spawn() for _ in range(size)]
    for round_index in range(5):
        barrier = threading.Barrier(size, timeout=WAIT)
        results = Recorder()

        def meet() -> None:
            try:
                barrier.wait()
                results.append("met")
            except threading.BrokenBarrierError:
                results.append("broken")

        for actor in actors:
            assert h.tell(actor, Call(meet)) == 0
        assert results.wait_len(size, timeout=WAIT + 1.0), round_index
        assert results.items() == ["met"] * size, round_index


IMPORT_PROBE = r"""
import importlib, json, os, sys, sysconfig
extra = os.environ.get("HYPRIAL_CONFORMANCE_FT_PATH", "")
paths = [p for p in (extra, SUITE) if p]
sys.path[0:0] = paths
gil = getattr(sys, "_is_gil_enabled", lambda: True)
ft_build = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))
before = gil()
module = importlib.import_module(TARGET)
after_import = gil()
print(json.dumps({"ft_build": ft_build, "before": before, "after_import": after_import,
                  "abi": module.ENGINE_ABI_VERSION, "module": module.__name__,
                  "python": sys.version}), flush=True)
"""

USE_PROBE = IMPORT_PROBE + r"""
engine = module.Engine(policies={"fast": (3, 60.0, 0.02, 0.2, 0.0)}, jitter_seed=0,
                       event_sink=lambda event: None, expected_error_type=LookupError)
actor_id = engine.spawn("free-03", lambda: (lambda command: command() if callable(command) else command),
                        4, "fast")
value = engine.ask(actor_id, "free-03", lambda: 41 + 1, 5.0)
stopped = engine.shutdown(5.0)
print(json.dumps({"after": gil(), "value": value, "shutdown": stopped}), flush=True)
"""

TARGETS = {"native": backends.NATIVE_MODULE, "reference-abi": "backends.reference_abi"}


def _probe(interpreter: str, backend_name: str, use: bool) -> dict:
    """Run the import probe (plus the Engine use check when ``use``) in a fresh interpreter."""
    probe = USE_PROBE if use else IMPORT_PROBE
    code = probe.replace("SUITE", repr(str(SUITE))).replace("TARGET", repr(TARGETS[backend_name]))
    env = {key: value for key, value in os.environ.items() if key not in ("PYTHON_GIL", "PYTHONPATH")}
    if use:
        env.pop("HYPRIAL_CONFORMANCE_FT_PATH", None)
    completed = subprocess.run([interpreter, "-c", code], cwd=str(SUITE), env=env, capture_output=True,
                               text=True, timeout=60)
    assert completed.returncode == 0, (f"FREE-03 probe failed under {interpreter} (rc={completed.returncode})\n"
                                       f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}")
    lines = completed.stdout.strip().splitlines()
    report = json.loads(lines[0])
    assert report["abi"] == 1
    if use:
        used = json.loads(lines[-1])
        assert used["value"] == 42
        assert used["shutdown"] is True
        report.update(used)
    return report


@case("FREE-03")
def test_free_03_module_import_keeps_gil_disabled(pytestconfig: pytest.Config, backend_name: str) -> None:
    """Free-threaded interpreter: importing the module leaves the GIL disabled (fail closed)."""
    interpreter = pytestconfig.getoption("--free-threaded-python")
    if not interpreter:
        if backend_name == "native":
            pytest.skip(f"{BLOCKED_PREFIX} FREE-03 needs a free-threaded interpreter for the native "
                        "backend and none was given: pass --free-threaded-python=PATH (or set "
                        "HYPRIAL_CONFORMANCE_FT_PYTHON, plus HYPRIAL_CONFORMANCE_FT_PATH for a staged "
                        "cp3xxt module); the free-threaded import claim is UNPROVEN on this run, not passed")
        report = _probe(sys.executable, backend_name, use=True)
        assert report["after_import"] == report["before"]
        assert report["after"] == report["before"]
        return
    report = _probe(interpreter, backend_name, use=False)
    assert report["ft_build"] is True, f"{interpreter} is not a free-threaded build: {report['python']}"
    assert report["before"] is False, report
    assert report["after_import"] is False, report
