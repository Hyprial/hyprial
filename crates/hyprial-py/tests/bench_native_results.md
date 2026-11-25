# Native actor engine performance benchmark — `bench_native.py` results

- Date: 2026-11-03
- Branch: `infra-ops/rustify-native-engine` (no commits made; this file and
  `bench_native.py` are new files only)
- Subject: `hyprial._core.actors` PyO3 extension,
  `src/hyprial/_core/actors.abi3.so` (built 2026-11-03 07:28,
  `ENGINE_ABI_VERSION = 1`)
- Command (repo root):
  `uv run --python 3.12 python crates/hyprial-py/tests/bench_native.py`
- Verdict: **ALL PASS** (all five bench sections completed and self-checked;
  details below)

## Machine (this run)

| item | value |
|---|---|
| node | `bench-host` |
| uname | Linux 7.0.0-38-generic x86_64 |
| CPU | AMD Ryzen 9 5950X 16-Core Processor (32 logical cores) |
| Python | CPython 3.12.16 (`uv`-managed venv, `--python 3.12`) |
| OS threads at bench start | 1 (main) |

**This is NOT the machine of the spec §16 pykka baseline.** The baseline was
recorded 2026-11-01 on Darwin arm64 (CPython 3.12.13); every cross-machine
number below is directional reference only, per the spec's own
non-normativity note. Notably:

- handler busy loops are calibrated independently per machine, so "handler
  CPU" rows compare *our loop vs their loop*, not the same work;
- handler CPU here is measured with `time.thread_time()` deltas taken inside
  the handler (the Linux analogue of the Darwin `RuntimeCpuCounters`
  `threadCpuMs` used for the baseline);
- the "plain tell admission" figure follows the bench protocol prescribed for
  this run (20000 single-threaded tells that all return 0, event callbacks
  flowing concurrently), which is a cleaner scenario than the baseline's
  in-run spin-retry measurement (`21.8–22.0 µs`; their 1 ms row `127.7 µs`
  included producer/consumer GIL contention);
- the spin-retry protocol is preserved exactly (single producer, capacity
  128, N = 20000, producer spin-retries `OVERLOADED` without sleeping).

## Protocol (as run)

- One serial `state_authority` actor (capacity 128) per workload; fresh
  `Engine` per scenario; shipped policy table; `jitter_seed=1`.
- Workloads: no-op, ~100 µs busy loop (1550 iterations calibrated), ~1 ms
  busy loop (15445 iterations calibrated); N = 20000 each.
- wall/msg = time from producer start until all N `command_completed` events
  observed (snapshot also checked `queued == 0`, `in_flight == 0`).
- handler CPU = Σ `time.thread_time()` deltas inside the handler; engine
  overhead = wall − handler CPU; engine share = (wall − handler CPU)/wall.
- Plain tell admission: separate engine, capacity 20000, no-op handler,
  single-threaded loop of 20000 tells, every return checked == 0, mean wall
  time of the `tell` call itself (two `perf_counter()` calls included).
- LIV-02: threads + RSS after engine creation / after spawning 200 serial
  actors / after stopping all 200. `threading.active_count()` (prescribed by
  the bench protocol) plus OS-level `Threads:` from `/proc/self/status`,
  because engine threads are spawned from Rust and are invisible to the
  `threading` module; RSS is `VmRSS` from the same file.

## Results vs pykka baseline (non-same-machine, reference only)

| handler work | wall/msg (native) | wall/msg (pykka §16) | handler CPU (native) | handler CPU (pykka §16) | engine overhead (native) | engine overhead (pykka §16) | engine share (native) | engine share (pykka §16) |
|---|---|---|---|---|---|---|---|---|
| no-op | 65.4 µs | 26.4 µs | 0.93 µs¹ | ~0.2 µs | 64.5 µs | ~26 µs | 98.6 % | 99.4 % |
| ~100 µs | 160.0 µs | 103.2 µs | 102.9 µs | 79.0 µs | 57.2 µs | ~24 µs | 35.7 % | 23.4 % |
| ~1 ms | 1075.7 µs | 872.4 µs | 1010.4 µs | 811.5 µs | 65.3 µs | ~61 µs | 6.1 % | 7.0 % |

¹ dominated by the two `time.thread_time()` calls (~0.9 µs of measurement
overhead); the pykka 0.2 µs came from Darwin-side per-thread counters.

Plain `tell` admission (this protocol): **0.25 µs/call** (20000/20000
returned 0) vs baseline "≈ 22 µs" on Darwin. This is the single biggest
cross-machine-stable signal: admission into the native engine is ~two orders
of magnitude cheaper than into the shipped pykka engine.

Engine-overhead deltas (wall − handler CPU) are roughly comparable in
magnitude to the pykka baseline (~54–65 µs here vs ~24–61 µs there) even
across the machine gap; the 1 ms engine share (6.1 % vs 7.0 %) matches the
baseline's reading that the engine is noise for millisecond handlers.

### Honest reading of the no-op row

Under the spin-retry protocol the no-op wall (65.4 µs/msg) is **dominated by
the producer's own rejected-tell spin, not by accepted-message admission**:
the producer made 7,069,074 overloaded retries for 20000 accepted messages
(~353 retries per delivered message ≈ 45 µs/msg of pure spin at the measured
~0.18–0.25 µs per `tell`), and the producer/engine threads ping-pong GIL
slices. The pykka producer could not spin this fast because each pykka
`tell` costs ~22 µs, so its protocol never entered this regime. Comparing
no-op *wall* across the two engines under this protocol is therefore not
apples-to-apples; the meaningful native-side figures are admission cost
(0.25 µs) and engine share (98.6 % — the pipeline still dominates short
handlers, same qualitative conclusion as §16).

### Run-to-run variance (same machine, 3 full runs on 2026-11-03)

| workload | wall/msg observed | note |
|---|---|---|
| no-op | 57.5 / 70.5 / 65.4 µs | GIL-contention sensitive (spin rate varies) |
| ~100 µs | 169.6 / 157.0 / 160.0 µs | |
| ~1 ms | 1035.4 / 1133.4 / 1075.7 µs | |
| tell admission | 0.208 / 0.248 / 0.251 µs | |

The canonical numbers above are from the final complete run. Runs differ by
up to ~±15 % on the spin-retry rows; ordering conclusions are stable.

## LIV-02: 200 actors spawn/stop — threads and memory

| counter | after engine | after spawn 200 | after stop 200 | after shutdown |
|---|---|---|---|---|
| `threading.active_count()` (prescribed) | 1 | 1 | 1 | 1 |
| OS `Threads:` (`/proc/self/status`) | 18 | 18 | 18 | 1 |
| `VmRSS` (KiB) | 23700 | 25176 | 25180 | — |
| stops confirmed | — | — | 200/200 | — |

- `threading.active_count()` stays 1 throughout because engine threads are
  native Rust threads never registered with the Python `threading` module;
  the OS counter is the real signal.
- The engine does **not** use thread-per-actor: `Engine()` builds a tokio
  multi-thread runtime with `available_parallelism().clamp(4, 16)` workers
  (16 on this box) plus a timer thread, so OS threads stay at 18
  (16 workers + timer + main) across 200 spawned actors — consistent with
  LIV-01's "thread per actor or equivalent scheduling" and far below the
  shipped pykka engine's 200 actor threads.
- Memory: +1476 KiB for 200 actors = **7.4 KiB/actor**; RSS is not returned
  after stop (+1480 KiB retained after all 200 stopped — allocator
  retention), and drops to baseline-class footprint only at engine shutdown
  (OS threads 18 → 1).

## Complete output of the canonical run

```
=== machine ===
node: bench-host
system: Linux 7.0.0-38-generic x86_64
cpu: AMD Ryzen 9 5950X 16-Core Processor (32 logical cores)
python: 3.12.16 (/home/hyprial/zcode/harness-bridge/.venv/bin/python)
engine abi: 1
n_messages: 20000  capacity: 128  profile: state_authority

=== calibration ===
~100us workload: 1550 busy-loop iterations
~1ms workload: 15445 busy-loop iterations

=== engine overhead vs handler work ===
[PASS] workload noop: {'workload': 'noop', 'wall_s': 1.308766336995177, 'wall_us_per_msg': 65.43831684975885, 'handler_cpu_us_per_msg': 0.9310837000000065, 'engine_overhead_us_per_msg': 64.50723314975885, 'engine_share_pct': 98.57715823874612, 'overloaded_retries': 7069074, 'completed': 20000, 'snapshot_end': ('running', 1, 0, 0, 0)}
[PASS] workload 100us: {'workload': '100us', 'wall_s': 3.2009531849762425, 'wall_us_per_msg': 160.04765924881212, 'handler_cpu_us_per_msg': 102.87868649999916, 'engine_overhead_us_per_msg': 57.16897274881296, 'engine_share_pct': 35.719968050227685, 'overloaded_retries': 6364326, 'completed': 20000, 'snapshot_end': ('running', 1, 0, 0, 0)}
[PASS] workload 1ms: {'workload': '1ms', 'wall_s': 21.514647122006863, 'wall_us_per_msg': 1075.7323561003432, 'handler_cpu_us_per_msg': 1010.3824282500038, 'engine_overhead_us_per_msg': 65.34992785033933, 'engine_share_pct': 6.074924443775278, 'overloaded_retries': 6551477, 'completed': 20000, 'snapshot_end': ('running', 1, 0, 0, 0)}

=== plain tell admission ===
[PASS] plain tell admission: {'tells': 20000, 'nonzero_returns': 0, 'mean_tell_us': 0.25094049924518913}

=== LIV-02 resources (200 actors) ===
[PASS] LIV-02 resources: {'actors': 200, 'threads_base': 1, 'threads_after_spawn': 1, 'threads_after_stop': 1, 'os_threads_base': 18, 'os_threads_after_spawn': 18, 'os_threads_after_stop': 18, 'os_threads_after_shutdown': 1, 'threads_active_after_shutdown': 1, 'rss_base_kib': 23700, 'rss_after_spawn_kib': 25176, 'rss_after_stop_kib': 25180, 'rss_delta_spawn_kib': 1476, 'rss_delta_spawn_per_actor_kib': 7.4, 'rss_delta_stop_kib': 1480, 'stops_confirmed': 200}

=== RESULTS (json) ===
{
  "machine": {
    "node": "bench-host",
    "system": "Linux 7.0.0-38-generic x86_64",
    "cpu": "AMD Ryzen 9 5950X 16-Core Processor",
    "logical_cores": 32,
    "python": "3.12.16",
    "engine_abi": 1
  },
  "calibration": {
    "busy_iters_100us": 1550,
    "busy_iters_1ms": 15445
  },
  "noop": {
    "workload": "noop",
    "wall_s": 1.308766336995177,
    "wall_us_per_msg": 65.43831684975885,
    "handler_cpu_us_per_msg": 0.9310837000000065,
    "engine_overhead_us_per_msg": 64.50723314975885,
    "engine_share_pct": 98.57715823874612,
    "overloaded_retries": 7069074,
    "completed": 20000,
    "snapshot_end": [
      "running",
      1,
      0,
      0,
      0
    ]
  },
  "us100": {
    "workload": "100us",
    "wall_s": 3.2009531849762425,
    "wall_us_per_msg": 160.04765924881212,
    "handler_cpu_us_per_msg": 102.87868649999916,
    "engine_overhead_us_per_msg": 57.16897274881296,
    "engine_share_pct": 35.719968050227685,
    "overloaded_retries": 6364326,
    "completed": 20000,
    "snapshot_end": [
      "running",
      1,
      0,
      0,
      0
    ]
  },
  "ms1": {
    "workload": "1ms",
    "wall_s": 21.514647122006863,
    "wall_us_per_msg": 1075.7323561003432,
    "handler_cpu_us_per_msg": 1010.3824282500038,
    "engine_overhead_us_per_msg": 65.34992785033933,
    "engine_share_pct": 6.074924443775278,
    "overloaded_retries": 6551477,
    "completed": 20000,
    "snapshot_end": [
      "running",
      1,
      0,
      0,
      0
    ]
  },
  "tell_admission": {
    "tells": 20000,
    "nonzero_returns": 0,
    "mean_tell_us": 0.25094049924518913
  },
  "liv02": {
    "actors": 200,
    "threads_base": 1,
    "threads_after_spawn": 1,
    "threads_after_stop": 1,
    "os_threads_base": 18,
    "os_threads_after_spawn": 18,
    "os_threads_after_stop": 18,
    "os_threads_after_shutdown": 1,
    "threads_active_after_shutdown": 1,
    "rss_base_kib": 23700,
    "rss_after_spawn_kib": 25176,
    "rss_after_stop_kib": 25180,
    "rss_delta_spawn_kib": 1476,
    "rss_delta_spawn_per_actor_kib": 7.4,
    "rss_delta_stop_kib": 1480,
    "stops_confirmed": 200
  }
}

ALL PASS
```

Verbatim stdout of the run (captured via `tee`), including the full JSON
block; nothing was edited.

## Verification status

| check | command/result | status |
|---|---|---|
| venv is 3.12 and module imports | `uv run --python 3.12 python -c "from hyprial._core import actors"` → `ENGINE_ABI_VERSION = 1`, `.so` at `src/hyprial/_core/actors.abi3.so` (2026-11-03 07:28) | PASS |
| no-op workload, N=20000, all delivered | 20000/20000 completed, snapshot drained | PASS |
| ~100 µs workload, N=20000, all delivered | 20000/20000 completed (handler CPU 102.9 µs ≈ target) | PASS |
| ~1 ms workload, N=20000, all delivered | 20000/20000 completed (handler CPU 1010.4 µs ≈ target) | PASS |
| plain tell admission, all returns == 0 | 20000/20000 returned 0 | PASS |
| LIV-02 spawn/stop 200 | 200/200 stops confirmed; threads/RSS recorded above | PASS |
| same-machine comparison with §16 | not applicable — baseline is Darwin arm64, this is Linux x86_64 | UNKNOWN / N-A by construction |

Numbers are as measured; nothing was adjusted to look favorable.
