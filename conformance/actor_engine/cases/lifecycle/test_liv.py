# Decision record (file header; no inline comments by repository rule):
# - Liveness cases of spec 12, 9.1 and 9.4 (LIV-01..05), all on the `h`
#   trampoline so both handler kinds are covered.
# - LIV-01 also blocks more actors than CPU cores (2 * cores + 1, at least 8,
#   at most 32): "thread per actor or equivalent scheduling" (12) forbids a
#   scheduler whose worker count caps how many handlers may block at once.
# - LIV-02 measures OS threads (Linux /proc/self/task; macOS `ps -M`) and open
#   file descriptors (/proc/self/fd or /dev/fd) after the harness engine
#   exists, so only per-actor resources are compared. Slack: 2 threads, 4 fds;
#   the post-stop count is polled for a bounded time because a stopped
#   thread leaves the OS task list a moment after its last Python bytecode.
# - LIV-03 proves GIL release by progress, not by timing: a handler doing
#   pure-Python CPU work (needs the GIL) must finish while the main thread is
#   blocked in ask / stop / drain, and a background pure-Python counter thread
#   must keep counting during the wait. A wait that held the GIL would starve
#   both (ask would time out; the counter would not move). For the completion
#   retry, the owner's CPU work and the counter must progress while the pool
#   keeps retrying into the owner's full mailbox.
# - The progress is event-driven, never a fixed window (HQ review under host
#   load: a 0.2 s burn could end before the counter thread was scheduled at
#   all). The handler burns until it has seen the caller INSIDE the blocking
#   call (support.helpers.inside_call) and the counter advanced PROGRESS more
#   iterations from then and itself executes more than 100 iterations; the
#   owner of the completion-retry cases burns until
#   the case releases it. A GIL-holding wait still fails: the handler can never
#   finish (stop/drain/ask time out) and the counter never moves.
# - Progress alone cannot catch a retry wait that holds the GIL during each
#   sleep and drops it only between attempts, so a second completion-retry
#   case uses a 0.5 s retry interval and a gap counter: the longest pause
#   between two iterations of a pure-Python thread must stay below 0.25 s
#   while the completion is retrying (12 GIL-02, 9.4 overloaded row). Each
#   1.0 s window contains a whole retry sleep, so a GIL-holding sleep shows a
#   >= 0.5 s gap in every window; best of N windows (assert_timing) absorbs a
#   loaded host's occasional descheduling. Its control is the same 1.0 s gap
#   window measured BEFORE the payload is submitted (owner already burning, so
#   the same GIL contention, but no retry): the platform's own pause in that
#   moment. On an idle host that baseline is a few ms, so the strict 0.25 s
#   bound applies.
# - LIV-01 "promptly" (0.5 s per ask) is measured through assert_timing.
# - LIV-04 also covers re-entry on OTHER actors from inside a handler (12):
#   ask, pool_reserve, tell and send_after work, and a nested ask to a busy
#   actor times out normally instead of deadlocking.
# - LIV-05 observes concurrency inside the payloads (active / peak counters),
#   not through pool snapshot numbers, which 9.3 does not define.
"""LIV: no stalls, resource return, GIL-releasing waits, self-tell, pool concurrency."""

from __future__ import annotations

import os
import subprocess
import threading
import time

import pytest

from support.helpers import (TOL, WAIT, Block, Call, Rec, Recorder, assert_timing, case, inside_call,
                             wait_until)

PROGRESS = 200
PROMPT = 0.5
GAP = 0.25


def os_thread_count() -> int:
    """Number of OS threads of this process (native engine threads included)."""
    if os.path.isdir("/proc/self/task"):
        return len(os.listdir("/proc/self/task"))
    output = subprocess.run(["ps", "-M", "-p", str(os.getpid())], capture_output=True, text=True,
                            check=True).stdout
    return max(0, len([line for line in output.splitlines() if line.strip()]) - 1)


def open_fd_count() -> int:
    """Number of open file descriptors of this process."""
    for path in ("/proc/self/fd", "/dev/fd"):
        if os.path.isdir(path):
            return len(os.listdir(path))
    raise AssertionError("no fd listing available on this platform")


class GapCounter:
    """Background pure-Python loop recording the longest pause between two iterations."""

    def __init__(self) -> None:
        self.gap = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        last = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            self.gap = max(self.gap, now - last)
            last = now

    def __enter__(self) -> "GapCounter":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(WAIT)


class Counter:
    """Background pure-Python counter thread; it only advances while it can take the GIL."""

    def __init__(self) -> None:
        self.value = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.value += 1

    def __enter__(self) -> "Counter":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(WAIT)


def burn_while_waited(counter: "Counter", caller: int, *calls: str,
                      started: threading.Event | None = None) -> Call:
    """Pure-Python CPU work until ``caller`` is inside one of ``calls`` and ``counter`` advanced PROGRESS.

    Returns its iteration count after both witnesses; fails after WAIT.
    """

    def run() -> int:
        if started is not None:
            started.set()
        deadline = time.monotonic() + WAIT
        count = 0
        base = None
        while time.monotonic() < deadline:
            count += 1
            if base is None:
                if inside_call(caller, *calls):
                    base = counter.value
                    count = 0
            elif count > 100 and counter.value - base > PROGRESS:
                return count
        raise AssertionError("progress witness timed out")

    return Call(run)


@case("LIV-03")
def test_liv_03_witness_waits_for_handler_progress(monkeypatch) -> None:
    class FastCounter:
        reads = 0

        @property
        def value(self) -> int:
            self.reads += 1
            return 0 if self.reads == 1 else PROGRESS + 1

    monkeypatch.setitem(burn_while_waited.__globals__, "inside_call", lambda *_: True)
    iterations = burn_while_waited(FastCounter(), 1, "ask")()
    assert iterations > 100


@case("LIV-03")
def test_liv_03_witness_timeout_fails(monkeypatch) -> None:
    monkeypatch.setitem(burn_while_waited.__globals__, "WAIT", 0)
    with pytest.raises(AssertionError, match="progress witness"):
        burn_while_waited(Counter(), 1, "ask")()


@case("LIV-03")
@pytest.mark.parametrize("inside", [False, True])
def test_liv_03_witness_requires_call_and_counter(monkeypatch, inside: bool) -> None:
    class Clock:
        ticks = 0

        def monotonic(self) -> float:
            self.ticks += 1
            return self.ticks / 1000

    class CounterWitness:
        reads = 0

        @property
        def value(self) -> int:
            self.reads += 1
            return 0 if inside else self.reads * (PROGRESS + 1)

    monkeypatch.setitem(burn_while_waited.__globals__, "time", Clock())
    monkeypatch.setitem(burn_while_waited.__globals__, "WAIT", 0.2)
    monkeypatch.setitem(burn_while_waited.__globals__, "inside_call", lambda *_: inside)
    with pytest.raises(AssertionError, match="progress witness"):
        burn_while_waited(CounterWitness(), 1, "ask")()


def burn_until(done: threading.Event, started: threading.Event) -> Call:
    """Pure-Python CPU work until ``done`` is set (bounded by 3 * WAIT); returns its iteration count."""

    def run() -> int:
        started.set()
        deadline = time.monotonic() + 3 * WAIT
        count = 0
        while not done.is_set() and time.monotonic() < deadline:
            count += 1
        return count

    return Call(run)


@case("LIV-01")
def test_liv_01_blocked_handler_does_not_stall_other_actors(h) -> None:
    """With one actor's handler and one pool worker blocked, other actors keep answering promptly."""
    blocked = h.spawn()
    others = [h.spawn() for _ in range(4)]
    pool = h.spawn_pool(pool_size=1, capacity=2)
    owner = h.spawn_owner(pool, Recorder())
    release, entered, worker = threading.Event(), threading.Event(), threading.Event()
    log = Recorder()
    try:
        assert h.tell(blocked, Block(release, entered)) == 0
        assert h.engine.pool_submit(pool.actor_id, pool.name, owner.actor_id, owner.name, "op", 1,
                                    Block(release, worker)) == 0
        assert entered.wait(WAIT) and worker.wait(WAIT)
        rounds: list[int] = []

        def attempt() -> tuple[float, ...]:
            latencies = []
            for actor in others:
                started = time.monotonic()
                assert h.ask(actor, Rec(log, actor.name), timeout=WAIT) == actor.name
                latencies.append(time.monotonic() - started)
            rounds.append(1)
            return tuple(latencies)

        assert_timing(attempt, 0.0, tol=PROMPT, what="ask to idle actors while others are blocked")
        assert not release.is_set()
        assert h.snapshot(blocked)[2:4] == (0, 1)
    finally:
        release.set()
    assert log.items() == [actor.name for actor in others] * len(rounds)


@case("LIV-01")
def test_liv_01_more_blocked_handlers_than_cores_do_not_stall_others(h) -> None:
    """Blocked handlers on more actors than CPU cores: a further actor still runs (thread per actor or equivalent)."""
    count = min(32, max(8, 2 * (os.cpu_count() or 1) + 1))
    blocked = [h.spawn() for _ in range(count)]
    other = h.spawn()
    release = threading.Event()
    gates = [threading.Event() for _ in blocked]
    try:
        for actor, gate in zip(blocked, gates):
            assert h.tell(actor, Block(release, gate)) == 0
        assert wait_until(lambda: all(gate.is_set() for gate in gates)), sum(g.is_set() for g in gates)
        assert h.ask(other, "ping", timeout=WAIT) == "ping"
        assert not release.is_set()
    finally:
        release.set()

@case("LIV-02")
def test_liv_02_two_hundred_stopped_actors_return_resources(h) -> None:
    """Spawn, use and stop 200 actors: OS threads and open fds return to the baseline (small slack)."""
    threads, fds = os_thread_count(), open_fd_count()
    log = Recorder()
    actors = [h.spawn(capacity=4) for _ in range(200)]
    for actor in actors:
        assert h.tell(actor, Rec(log, 1)) == 0
    for actor in actors:
        assert h.ask(actor, "ping") == "ping"
    for actor in actors:
        assert h.stop(actor) is True
    assert len(log) == 200
    assert all(h.state(actor) == "stopped" for actor in actors)
    assert wait_until(lambda: os_thread_count() <= threads + 2, timeout=WAIT), (os_thread_count(), threads)
    assert wait_until(lambda: open_fd_count() <= fds + 4, timeout=WAIT), (open_fd_count(), fds)


@case("LIV-03")
def test_liv_03_ask_wait_releases_gil(h) -> None:
    """A pure-Python handler and a Python counter both progress while the caller waits in ask."""
    actor = h.spawn()
    caller = threading.get_ident()
    with Counter() as counter:
        before = counter.value
        iterations = h.ask(actor, burn_while_waited(counter, caller, "ask"), timeout=2 * WAIT)
        progressed = counter.value - before
    assert iterations > 100
    assert progressed > PROGRESS


@case("LIV-03")
@pytest.mark.parametrize("wait", ["stop", "drain"])
def test_liv_03_stop_and_drain_waits_release_gil(h, wait: str) -> None:
    """stop / drain wait for a pure-Python CPU-bound handler, which must finish during the wait."""
    actor = h.spawn()
    started = threading.Event()
    log = Recorder()
    counter = Counter()
    work = burn_while_waited(counter, threading.get_ident(), wait, started=started)
    assert h.tell(actor, Call(lambda: log.append(work()))) == 0
    assert started.wait(WAIT)
    with counter:
        before = counter.value
        if wait == "stop":
            assert h.stop(actor, timeout=2 * WAIT) is True
        else:
            assert h.drain([actor], timeout=2 * WAIT)[0] is True
        progressed = counter.value - before
    assert len(log) == 1 and log.items()[0] > 100
    assert progressed > PROGRESS
    assert h.state(actor) == "stopped"


@case("LIV-03")
def test_liv_03_completion_retry_wait_releases_gil(h) -> None:
    """While a completion keeps retrying into a full owner mailbox, Python threads keep running."""
    pool = h.spawn_pool(pool_size=1, capacity=2, retry_interval=0.01)
    completions = Recorder()
    owner = h.spawn_owner(pool, completions, capacity=1)
    started, done = threading.Event(), threading.Event()
    burned = Recorder()
    executed = Recorder()
    work = burn_until(done, started)
    assert h.tell(owner, Call(lambda: burned.append(work()))) == 0
    assert started.wait(WAIT)
    try:
        assert h.tell(owner, Rec(executed, "filler")) == 0
        assert h.engine.pool_submit(pool.actor_id, pool.name, owner.actor_id, owner.name, "op", 1,
                                    Rec(executed, "payload")) == 0
        assert wait_until(lambda: "payload" in executed.items())
        with Counter() as counter:
            before = counter.value
            assert wait_until(lambda: counter.value - before > PROGRESS)
            assert completions.items() == []
            assert not burned.items()
    finally:
        done.set()
    assert wait_until(lambda: len(burned) == 1)
    assert burned.items()[0] > 100
    assert wait_until(lambda: len(completions) >= 1)
    assert completions.items()[0].operation_id == "op"


@case("LIV-03")
def test_liv_03_completion_retry_sleep_releases_gil(h) -> None:
    """A slow completion retry never holds the GIL across its sleep: no Python pause reaches 0.25 s."""
    pool = h.spawn_pool(pool_size=1, capacity=2, retry_interval=0.5)
    completions = Recorder()
    owner = h.spawn_owner(pool, completions, capacity=1)
    started, done = threading.Event(), threading.Event()
    burned = Recorder()
    executed = Recorder()
    work = burn_until(done, started)
    assert h.tell(owner, Call(lambda: burned.append(work()))) == 0
    assert started.wait(WAIT)

    def window() -> float:
        with GapCounter() as counter:
            time.sleep(1.0)
            assert completions.items() == []
            assert not burned.items()
        return counter.gap

    try:
        baseline = window()
        assert h.tell(owner, Rec(executed, "filler")) == 0
        assert h.engine.pool_submit(pool.actor_id, pool.name, owner.actor_id, owner.name, "op", 1,
                                    Rec(executed, "payload")) == 0
        assert wait_until(lambda: "payload" in executed.items())
        assert_timing(window, 0.0, tol=GAP, control=lambda: baseline,
                      what="longest Python pause while a completion retries")
    finally:
        done.set()
    assert wait_until(lambda: len(burned) == 1)
    assert burned.items()[0] > 100
    assert wait_until(lambda: len(completions) >= 1)
    assert completions.items()[0].operation_id == "op"


@case("LIV-04")
def test_liv_04_self_tell_enqueues_behind_current_command(h) -> None:
    """A handler telling itself gets 0 at once; the message runs after the current and earlier commands."""
    log = Recorder()
    results = Recorder()
    told = threading.Event()
    box: list = []

    def current() -> str:
        log.append("x-start")
        told.wait(WAIT)
        results.append(h.tell(box[0], Rec(log, "self")))
        log.append("x-end")
        return "x"

    actor = h.spawn()
    box.append(actor)
    assert h.tell(actor, Call(current)) == 0
    assert wait_until(lambda: "x-start" in log.items())
    assert h.tell(actor, Rec(log, "earlier")) == 0
    told.set()
    assert log.wait_len(4)
    assert log.items() == ["x-start", "x-end", "earlier", "self"]
    assert results.items() == [0]


@case("LIV-04")
def test_liv_04_handler_reenters_engine_on_other_actors(h) -> None:
    """From inside a handler, ask / pool_reserve / tell / send_after on other actors work."""
    log = Recorder()
    a = h.spawn()
    b = h.spawn()
    pool = h.spawn_pool(pool_size=1, capacity=2)

    def body() -> tuple:
        h.send_after(b, 0.0, Rec(log, "timer"))
        return (h.ask(b, "ping", WAIT), h.engine.pool_reserve(pool.actor_id, pool.name, "op", 1),
                h.tell(b, Rec(log, "tell")))

    assert h.ask(a, Call(body), WAIT) == ("ping", 0, 0)
    assert log.wait_len(2)
    assert sorted(log.items()) == ["tell", "timer"]
    assert h.engine.pool_cancel_reservation(pool.actor_id, pool.name, "op", 1) is True

    release, entered = threading.Event(), threading.Event()

    def timed() -> str:
        try:
            h.ask(b, "late", 0.1)
        except TimeoutError:
            return "timeout"
        return "answered"

    try:
        assert h.tell(b, Block(release, entered)) == 0
        assert entered.wait(WAIT)
        assert h.ask(a, Call(timed), WAIT) == "timeout"
    finally:
        release.set()


class Tracker:
    """Payload that records concurrent executions and blocks until released."""

    def __init__(self, release: threading.Event) -> None:
        self.release = release
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.started = 0
        self.finished = 0

    def __call__(self) -> object:
        with self.lock:
            self.active += 1
            self.started += 1
            self.peak = max(self.peak, self.active)
        self.release.wait(3 * WAIT)
        with self.lock:
            self.active -= 1
            self.finished += 1
        return None


@case("LIV-05")
@pytest.mark.parametrize("pool_size", [1, 3])
def test_liv_05_pool_concurrency_honored(h, pool_size: int) -> None:
    """Exactly pool_size payloads execute concurrently; the rest wait and run afterwards."""
    pool = h.spawn_pool(pool_size=pool_size, capacity=16)
    owner = h.spawn_owner(pool, Recorder())
    release = threading.Event()
    tracker = Tracker(release)
    total = pool_size * 2
    try:
        for op in range(total):
            assert h.engine.pool_submit(pool.actor_id, pool.name, owner.actor_id, owner.name,
                                        f"op{op}", 1, tracker) == 0
        assert wait_until(lambda: tracker.active == pool_size)
        time.sleep(0.1 + TOL)
        assert tracker.active == pool_size
        assert tracker.started == pool_size
    finally:
        release.set()
    assert wait_until(lambda: tracker.finished == total)
    assert tracker.peak == pool_size
