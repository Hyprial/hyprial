# Decision record (file header; no inline comments by repository rule):
# - LIFE-01 runs a real interpreter exit in a subprocess (sys.executable,
#   cwd = the suite dir, the backend loaded through `backends.load`, the same
#   seam the adapter uses). The child leaves serial actors and pools with work
#   in flight, queued commands, pending timers and outstanding custody, then
#   returns from the main module. With the adapter's `atexit` hook (spec 12:
#   installing it is the adapter's job) `shutdown(2.0)` must return True and
#   the process must exit 0 quickly with no crash text on stderr. A second
#   child exits WITHOUT the hook: actor threads are daemon threads and the
#   engine must not take the GIL after finalization began (12), so that exit
#   is clean too. Churn actors keep many short commands re-entering Python so
#   that a thread touching the GIL during finalization is likely to be caught
#   (detection is probabilistic by nature; a single red run is a real defect).
# - LIFE-02 counts OS threads (Linux /proc/self/task; macOS `ps -M -p <pid>`),
#   not only threading.enumerate(), so native threads are covered. The count
#   is taken before the engine exists. A thread joined by shutdown can need a
#   moment to leave the OS task list after its join returns, so the count is
#   polled for a bounded time (WAIT); it may drop below the baseline (other cases'
#   threads finishing), never stay above it.
# - LIFE-03 asserts the literal 12 rule: a second shutdown returns the FIRST
#   result (also when that was False) without events or dead letters.
#   "At once" (< TOL) is measured through support.helpers.assert_timing.
# - LIFE-01's wall bound (EXIT_BOUND, 8 s for a whole child interpreter) is a
#   hang detector; it is measured through assert_timing too (best of 3 child
#   runs), so a loaded host's slow interpreter start-up cannot fail it while a
#   child whose exit really blocks fails every run.
# - LIFE-04 drives `backends.check_abi`, the suite's adapter seam (3.4), with
#   fake modules; the loaded backend must report exactly the int 1.
"""LIFE: process exit, thread hygiene, shutdown idempotence, ABI version gate (spec 12, 9.5, 3.4)."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

import backends
from support.helpers import (WAIT, Block, DeadLetters, Rec, Recorder, Sleep, assert_timing, case,
                             wait_until)

EXIT_BOUND = 8.0

SUITE = Path(__file__).resolve().parents[2]
CRASH_MARKERS = ("Traceback", "Fatal Python error", "Segmentation fault", "panicked", "Exception ignored",
                 "core dumped", "Aborted", "FATAL")

CHILD = r"""
import atexit, sys, threading, time
import backends

module = backends.load(BACKEND)


class Expected(Exception):
    pass


engine = module.Engine(policies={"fast": (3, 60.0, 0.02, 0.2, 0.0)}, jitter_seed=0,
                       event_sink=lambda event: None, expected_error_type=Expected)
if INSTALL_ATEXIT:
    def _shutdown():
        ok = engine.shutdown(2.0)
        sys.stdout.write(f"SHUTDOWN={ok}\n")
        sys.stdout.flush()
    atexit.register(_shutdown)

started = threading.Semaphore(0)


def busy(seconds):
    def run():
        started.release()
        time.sleep(seconds)
        return seconds
    return run


def trampoline():
    return lambda command: command() if callable(command) else command


owner_box = []


def owner_handler(message):
    if isinstance(message, module.EffectCompleted):
        pool_id, pool_name = owner_box[0]
        engine.acknowledge(pool_id, pool_name, message.operation_id, message.generation)
        return None
    return message() if callable(message) else message


dead = []
actors = []
for index in range(6):
    name = f"life-{index}"
    actor_id = engine.spawn(name, trampoline, 8, "fast", lambda m, r: dead.append(r))
    actors.append((actor_id, name))
    assert engine.tell(actor_id, name, busy(0.3)) == 0
    assert engine.tell(actor_id, name, busy(0.3)) == 0
    engine.send_after(actor_id, name, 30.0, busy(0.1))
owner_id = engine.spawn("life-owner", lambda: owner_handler, 64, "fast")
lazy_owner_id = engine.spawn("life-lazy-owner", trampoline, 64, "fast")
for index in range(2):
    pool_name = f"life-pool-{index}"
    pool_id = engine.spawn_pool(pool_name, trampoline, 2, 8, "fast")
    owner_box.append((pool_id, pool_name))
    for op in range(3):
        assert engine.pool_submit(pool_id, pool_name, owner_id, "life-owner", f"op{op}", 1, busy(0.2)) == 0
    assert engine.pool_submit(pool_id, pool_name, lazy_owner_id, "life-lazy-owner", "unacked", 1,
                              busy(0.0)) == 0
for index in range(4):
    name = f"life-churn-{index}"
    actor_id = engine.spawn(name, trampoline, 64, "fast")
    for _ in range(60):
        engine.tell(actor_id, name, lambda: time.sleep(0.002))
for _ in range(6 + 4):
    assert started.acquire(timeout=5.0)
sys.stdout.write("READY\n")
sys.stdout.flush()
"""


def _run_child(backend_name: str, install_atexit: bool) -> tuple[subprocess.CompletedProcess, float]:
    code = CHILD.replace("BACKEND", repr(backend_name)).replace("INSTALL_ATEXIT", repr(install_atexit))
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    started = time.monotonic()
    completed = subprocess.run([sys.executable, "-c", code], cwd=str(SUITE), env=env,
                               capture_output=True, text=True, timeout=60)
    return completed, time.monotonic() - started


def _assert_clean(completed: subprocess.CompletedProcess) -> None:
    detail = f"rc={completed.returncode}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    assert completed.returncode == 0, detail
    assert "READY" in completed.stdout, detail
    for marker in CRASH_MARKERS:
        assert marker not in completed.stderr, detail


@case("LIFE-01")
def test_life_01_subprocess_exits_cleanly_through_atexit_shutdown(backend_name: str) -> None:
    """Live actors and pools with work in flight: the atexit shutdown(2.0) returns True and exit is clean."""
    def attempt() -> float:
        completed, wall = _run_child(backend_name, install_atexit=True)
        _assert_clean(completed)
        assert "SHUTDOWN=True" in completed.stdout, completed.stdout + completed.stderr
        return wall

    assert_timing(attempt, 0.0, tol=EXIT_BOUND, attempts=3, what="child exit with atexit shutdown")


@case("LIFE-01")
def test_life_01_subprocess_exits_cleanly_without_shutdown(backend_name: str) -> None:
    """Without any shutdown call the daemon engine threads never block or crash interpreter exit."""
    def attempt() -> float:
        completed, wall = _run_child(backend_name, install_atexit=False)
        _assert_clean(completed)
        return wall

    assert_timing(attempt, 0.0, tol=EXIT_BOUND, attempts=3, what="child exit without shutdown")


def os_thread_count() -> int:
    """Number of OS threads of this process (native engine threads included)."""
    task_dir = "/proc/self/task"
    if os.path.isdir(task_dir):
        return len(os.listdir(task_dir))
    output = subprocess.run(["ps", "-M", "-p", str(os.getpid())], capture_output=True, text=True,
                            check=True).stdout
    return max(0, len([line for line in output.splitlines() if line.strip()]) - 1)


@case("LIFE-02")
@pytest.mark.parametrize("kind", ["python", "native"], ids=["py-handler", "native-handler"])
def test_life_02_no_engine_thread_survives_shutdown(make_harness, kind: str) -> None:
    """After shutdown(timeout) -> True, the OS thread count and the Python thread set are back to baseline."""
    baseline = os_thread_count()
    python_baseline = set(threading.enumerate())
    harness = make_harness(kind=kind)
    log = Recorder()
    actors = [harness.spawn() for _ in range(8)]
    for actor in actors:
        assert harness.ask(actor, Rec(log, actor.name)) == actor.name
        assert harness.tell(actor, Sleep(0.05)) == 0
        harness.send_after(actor, 30.0, Rec(log, "never"))
    pool = harness.spawn_pool(pool_size=3, capacity=8)
    owner = harness.spawn_owner(pool, Recorder())
    for op in range(5):
        assert harness.engine.pool_submit(pool.actor_id, pool.name, owner.actor_id, owner.name,
                                          f"op{op}", 1, Sleep(0.05)) == 0
    assert os_thread_count() > baseline
    assert harness.close(WAIT) is True
    assert wait_until(lambda: os_thread_count() <= baseline, timeout=WAIT), (os_thread_count(), baseline)
    assert wait_until(lambda: set(threading.enumerate()) <= python_baseline, timeout=WAIT)
    assert "never" not in log.items()


@case("LIFE-03")
def test_life_03_second_shutdown_returns_first_result_without_side_effects(h) -> None:
    """A second (and third) shutdown returns the first True at once, with no new events or dead letters."""
    dead = DeadLetters()
    actor = h.spawn(dead_letters=dead)
    release, entered = threading.Event(), threading.Event()
    assert h.tell(actor, Block(release, entered)) == 0
    assert entered.wait(WAIT)
    assert h.tell(actor, Rec(Recorder(), "queued")) == 0
    h.send_after(actor, 30.0, "timer")
    pool = h.spawn_pool(pool_size=1, capacity=2)
    threading.Timer(0.05, release.set).start()
    assert h.close(WAIT) is True
    events, letters = h.events.items(), dead.items()
    def again() -> float:
        started = time.monotonic()
        assert h.close(WAIT) is True
        return time.monotonic() - started

    for _ in range(2):
        assert_timing(again, 0.0, what="repeated shutdown")
    time.sleep(0.05)
    assert h.events.items() == events
    assert dead.items() == letters
    assert h.state(actor) == "stopped"
    assert h.state(pool) == "stopped"


@case("LIFE-03")
def test_life_03_failed_shutdown_result_is_repeated(make_harness) -> None:
    """A shutdown that timed out returns False; a later call returns that first result (False) unchanged."""
    harness = make_harness()
    actor = harness.spawn()
    release, entered = threading.Event(), threading.Event()
    try:
        assert harness.tell(actor, Block(release, entered)) == 0
        assert entered.wait(WAIT)
        assert harness.close(0.05) is False
    finally:
        release.set()
    harness.wait_state(actor, "stopped", timeout=WAIT)
    events = harness.events.items()
    assert harness.close(WAIT) is False
    assert harness.events.items() == events


def _fake(source: types.ModuleType, **overrides: object) -> types.ModuleType:
    fake = types.ModuleType("fake_engine_backend")
    for name in backends.REQUIRED_EXPORTS:
        setattr(fake, name, getattr(source, name))
    for name, value in overrides.items():
        if value is _MISSING:
            delattr(fake, name)
        else:
            setattr(fake, name, value)
    return fake


_MISSING = object()


@case("LIFE-04")
@pytest.mark.parametrize("version", [0, 2, -1, "1", 1.0, True, None], ids=repr)
def test_life_04_adapter_refuses_other_abi_versions(backend, version: object) -> None:
    """check_abi refuses any ENGINE_ABI_VERSION that is not exactly the int 1."""
    with pytest.raises(backends.BackendLoadError):
        backends.check_abi(_fake(backend, ENGINE_ABI_VERSION=version))


@case("LIFE-04")
@pytest.mark.parametrize("export", backends.REQUIRED_EXPORTS)
def test_life_04_adapter_refuses_missing_exports(backend, export: str) -> None:
    """check_abi refuses a module lacking any ABI v1 export."""
    with pytest.raises(backends.BackendLoadError):
        backends.check_abi(_fake(backend, **{export: _MISSING}))


@case("LIFE-04")
def test_life_04_loaded_backend_reports_exactly_int_one(backend) -> None:
    """The backend under test exposes ENGINE_ABI_VERSION as exactly the int 1 and passes the gate."""
    assert type(backend.ENGINE_ABI_VERSION) is int
    assert backend.ENGINE_ABI_VERSION == 1
    assert backends.check_abi(backend) is backend
    assert backends.check_abi(_fake(backend)) is not None


@case("LIFE-04")
def test_life_04_absent_native_module_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A native module that cannot be imported is a load error (fail closed), never a skip."""
    monkeypatch.setattr(backends, "NATIVE_MODULE", "hyprial._core.no_such_engine_module")
    with pytest.raises(backends.BackendLoadError):
        backends.load("native")
