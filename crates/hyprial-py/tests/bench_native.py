"""Performance benchmark for the native actor engine (``hyprial._core.actors``).

Methodology mirrors the pykka baseline recorded in the frozen spec
(``docs/design/actor-engine-native-abi-spec-2026-11-01.md`` section 16,
``bench_engine_overhead.py`` on Darwin arm64):

* one producer thread, one serial ``state_authority`` actor, capacity 128,
  N = 20000 messages per handler workload, producer spin-retries on
  overloaded (``tell`` returns 1) without sleeping;
* handler workloads: no-op, ~100 us busy loop, ~1 ms busy loop (calibrated
  on this machine at start of run);
* wall clock per message = (time until all N completions observed) / N;
  handler-side CPU = sum of ``time.thread_time`` deltas taken inside the
  handler around the work (the Linux analogue of the Darwin
  ``RuntimeCpuCounters.threadCpuMs`` used for the pykka baseline);
  engine share = (wall - handler CPU) / wall;
* plain ``tell`` admission: a separate single-threaded loop of N tells that
  must all return 0 (huge capacity, no-op handler, completion events still
  flowing); mean wall time of the ``tell`` call itself;
* LIV-02 resource check: RSS and thread counts after engine creation, after
  spawning 200 serial actors, and after stopping all 200. Two thread
  counters are recorded because the engine's threads are spawned from Rust
  and are invisible to ``threading.active_count`` (which only sees threads
  created via the Python ``threading`` module): ``threading.active_count``
  as prescribed by the bench protocol, plus the OS-level ``Threads:`` line
  of ``/proc/self/status``. RSS is ``VmRSS`` from the same file.

Run from the repository root:

    uv run --python 3.12 python crates/hyprial-py/tests/bench_native.py

Every section prints ``PASS`` / ``FAIL``; the exit code is the verdict.
"""

from __future__ import annotations

import json
import os
import platform
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "src"))

from hyprial._core import actors as native  # noqa: E402

N = 20_000
CAPACITY = 128
PROFILES = {
    "state_authority": (3, 60, 0.1, 2.0, 0.1),
    "external_io": (5, 60, 0.25, 10.0, 0.2),
    "process_lifecycle": (2, 120, 0.5, 5.0, 0.1),
}


class BenchExpected(Exception):
    pass


class CompletionSink:
    """Counts command_completed events (thread-safe)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.completed = 0

    def event(self, event: tuple) -> None:
        if event[0] == "command_completed":
            with self.lock:
                self.completed += 1

    def count(self) -> int:
        with self.lock:
            return self.completed


_SINK = 0


def _busy(n: int) -> int:
    """Pure-Python CPU busy loop, ~60-100 ns per iteration."""
    x = 1
    for i in range(n):
        x = (x * 33 + i) & 0xFFFFFFFFFFFF
    global _SINK
    _SINK = x
    return x


def calibrate(target_s: float) -> int:
    """Pick an iteration count so one _busy call takes ~target_s of wall."""
    probe = 1_000
    _busy(probe)  # warm-up
    reps = 200
    t0 = time.perf_counter()
    for _ in range(reps):
        _busy(probe)
    per_iter = (time.perf_counter() - t0) / (reps * probe)
    return max(1, int(round(target_s / per_iter)))


def make_engine(sink: CompletionSink) -> native.Engine:
    return native.Engine(
        policies=PROFILES,
        jitter_seed=1,
        event_sink=sink.event,
        expected_error_type=BenchExpected,
    )


def wait_completed(sink: CompletionSink, expected: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sink.count() >= expected:
            return True
        time.sleep(0.0005)
    return sink.count() >= expected


def proc_status() -> tuple[int, int]:
    """(VmRSS KiB, OS thread count) from /proc/self/status."""
    rss: int | None = None
    threads: int | None = None
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1])
            elif line.startswith("Threads:"):
                threads = int(line.split()[1])
    if rss is None or threads is None:
        raise RuntimeError("VmRSS/Threads not found in /proc/self/status")
    return rss, threads


def run_workload(label: str, busy_iters: int, completion_timeout_s: float) -> dict:
    """Single producer, one state_authority actor, spin-retry on overloaded."""
    sink = CompletionSink()
    eng = make_engine(sink)
    cpu_total_s = 0.0

    def factory():
        def handler(cmd):
            nonlocal cpu_total_s
            t0 = time.thread_time()
            if busy_iters:
                _busy(busy_iters)
            cpu_total_s += time.thread_time() - t0
            return cmd

        return handler

    name = f"bench-{label}"
    aid = eng.spawn(name, factory, capacity=CAPACITY, profile="state_authority")

    stats = {"accepted": 0, "overloaded": 0, "closed": 0}

    def produce():
        for i in range(N):
            while True:
                r = eng.tell(aid, name, i)
                if r == 0:
                    stats["accepted"] += 1
                    break
                if r == 1:
                    stats["overloaded"] += 1
                    continue
                stats["closed"] += 1
                return

    try:
        wall0 = time.perf_counter()
        producer = threading.Thread(target=produce, name="bench-producer")
        producer.start()
        producer.join()
        if stats["closed"]:
            raise RuntimeError(f"producer saw closed admission: {stats}")
        if not wait_completed(sink, N, completion_timeout_s):
            raise RuntimeError(
                f"timeout waiting for completions: {sink.count()}/{N}"
            )
        wall_s = time.perf_counter() - wall0
        snap = eng.snapshot(aid, name)
        if snap[2] != 0 or snap[3] != 0:
            raise RuntimeError(f"queue not drained at end: snapshot={snap}")
        completed = sink.count()
    finally:
        eng.shutdown(10.0)

    wall_per_msg_us = wall_s / N * 1e6
    cpu_per_msg_us = cpu_total_s / N * 1e6
    overhead_us = wall_per_msg_us - cpu_per_msg_us
    share_pct = (wall_s - cpu_total_s) / wall_s * 100.0
    result = {
        "workload": label,
        "wall_s": wall_s,
        "wall_us_per_msg": wall_per_msg_us,
        "handler_cpu_us_per_msg": cpu_per_msg_us,
        "engine_overhead_us_per_msg": overhead_us,
        "engine_share_pct": share_pct,
        "overloaded_retries": stats["overloaded"],
        "completed": completed,
        "snapshot_end": snap,
    }
    ok = completed == N and stats["accepted"] == N
    print(f"[{'PASS' if ok else 'FAIL'}] workload {label}: {result}", flush=True)
    if not ok:
        raise RuntimeError(f"workload {label} did not deliver all messages: {result}")
    return result


def run_tell_admission(n: int = N) -> dict:
    """Single-threaded loop: mean wall time of tell calls that return 0.

    Huge capacity so admission never rejects; the no-op handler drains the
    queue and the completion event callbacks run concurrently on the engine
    thread, so their cost is included, per the bench protocol.
    """
    sink = CompletionSink()
    eng = make_engine(sink)

    def factory():
        def handler(cmd):
            return cmd

        return handler

    aid = eng.spawn("bench-admission", factory, capacity=n, profile="state_authority")
    nonzero = 0
    try:
        acc = 0.0
        for i in range(n):
            t0 = time.perf_counter()
            r = eng.tell(aid, "bench-admission", i)
            acc += time.perf_counter() - t0
            if r != 0:
                nonzero += 1
        if not wait_completed(sink, n, 60.0):
            raise RuntimeError(f"timeout waiting for completions: {sink.count()}/{n}")
    finally:
        eng.shutdown(10.0)

    mean_us = acc / n * 1e6
    ok = nonzero == 0
    result = {
        "tells": n,
        "nonzero_returns": nonzero,
        "mean_tell_us": mean_us,
    }
    print(f"[{'PASS' if ok else 'FAIL'}] plain tell admission: {result}", flush=True)
    if not ok:
        raise RuntimeError(f"tell admission saw nonzero returns: {result}")
    return result


def run_liv02_resources(count: int = 200) -> dict:
    """Threads and RSS after engine creation / spawn 200 / stop 200 (LIV-02)."""
    sink = CompletionSink()
    eng = make_engine(sink)
    names = [f"liv02-{i:03d}" for i in range(count)]

    def factory():
        def handler(cmd):
            return cmd

        return handler

    try:
        base_threads = threading.active_count()
        base_os_threads = proc_status()[1]
        base_rss = proc_status()[0]

        aids = [
            eng.spawn(nm, factory, capacity=8, profile="state_authority")
            for nm in names
        ]
        spawned_threads = threading.active_count()
        spawned_os_threads = proc_status()[1]
        spawned_rss = proc_status()[0]

        stop_ok = 0
        for aid, nm in zip(aids, names):
            if eng.stop(aid, nm, 5.0):
                stop_ok += 1
        # give exited threads a moment to be reaped before sampling
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if threading.active_count() <= base_threads + 1:
                break
            time.sleep(0.001)
        stopped_threads = threading.active_count()
        stopped_rss, stopped_os_threads = proc_status()
    finally:
        eng.shutdown(10.0)
        time.sleep(0.2)
        after_shutdown_threads = threading.active_count()
        _, after_shutdown_os_threads = proc_status()

    result = {
        "actors": count,
        "threads_base": base_threads,
        "threads_after_spawn": spawned_threads,
        "threads_after_stop": stopped_threads,
        "os_threads_base": base_os_threads,
        "os_threads_after_spawn": spawned_os_threads,
        "os_threads_after_stop": stopped_os_threads,
        "os_threads_after_shutdown": after_shutdown_os_threads,
        "threads_active_after_shutdown": after_shutdown_threads,
        "rss_base_kib": base_rss,
        "rss_after_spawn_kib": spawned_rss,
        "rss_after_stop_kib": stopped_rss,
        "rss_delta_spawn_kib": spawned_rss - base_rss,
        "rss_delta_spawn_per_actor_kib": round((spawned_rss - base_rss) / count, 1),
        "rss_delta_stop_kib": stopped_rss - base_rss,
        "stops_confirmed": stop_ok,
    }
    ok = stop_ok == count
    print(f"[{'PASS' if ok else 'FAIL'}] LIV-02 resources: {result}", flush=True)
    if not ok:
        raise RuntimeError(f"LIV-02: only {stop_ok}/{count} stops confirmed")
    return result


def main() -> int:
    print("=== machine ===")
    u = platform.uname()
    cpu_model = "<unknown>"
    with open("/proc/cpuinfo") as f:
        for line in f:
            if line.startswith("model name"):
                cpu_model = line.split(":", 1)[1].strip()
                break
    print(f"node: {u.node}")
    print(f"system: {u.system} {u.release} {u.machine}")
    print(f"cpu: {cpu_model} ({os.cpu_count()} logical cores)")
    print(f"python: {platform.python_version()} ({sys.executable})")
    print(f"engine abi: {native.ENGINE_ABI_VERSION}")
    print(f"n_messages: {N}  capacity: {CAPACITY}  profile: state_authority")

    print("\n=== calibration ===")
    iters_100us = calibrate(100e-6)
    iters_1ms = calibrate(1e-3)
    print(f"~100us workload: {iters_100us} busy-loop iterations")
    print(f"~1ms workload: {iters_1ms} busy-loop iterations")

    results: dict = {
        "machine": {
            "node": u.node,
            "system": f"{u.system} {u.release} {u.machine}",
            "cpu": cpu_model,
            "logical_cores": os.cpu_count(),
            "python": platform.python_version(),
            "engine_abi": native.ENGINE_ABI_VERSION,
        },
        "calibration": {
            "busy_iters_100us": iters_100us,
            "busy_iters_1ms": iters_1ms,
        },
    }

    print("\n=== engine overhead vs handler work ===")
    results["noop"] = run_workload("noop", 0, 60.0)
    results["us100"] = run_workload("100us", iters_100us, 120.0)
    results["ms1"] = run_workload("1ms", iters_1ms, 600.0)

    print("\n=== plain tell admission ===")
    results["tell_admission"] = run_tell_admission()

    print("\n=== LIV-02 resources (200 actors) ===")
    results["liv02"] = run_liv02_resources()

    print("\n=== RESULTS (json) ===")
    print(json.dumps(results, indent=2))

    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
