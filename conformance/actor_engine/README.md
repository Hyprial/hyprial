# Actor engine conformance suite

Black-box conformance suite for the native actor engine ABI v1
(`hyprial._core.actors`, `ENGINE_ABI_VERSION = 1`). It is the executable form of
[`docs/design/actor-engine-native-abi-spec-2026-11-01.md`](../../docs/design/actor-engine-native-abi-spec-2026-11-01.md)
(§14 case table, §15 how to run) and of the binding clarifications in
[`actor-engine-native-abi-spec-clarifications-2026-11-03.md`](../../docs/design/actor-engine-native-abi-spec-clarifications-2026-11-03.md)
(CL-1, CL-2). When the suite and the spec disagree, the spec wins and the suite is
the bug.

The suite lives outside `tests/` on purpose: the main test tree's conftest loads
the daemon, and this suite must load no business package (see *Import closure*).

## Layout

| path | content |
|---|---|
| `pytest.ini`, `conftest.py` | backend option, fixtures, HOME/TMPDIR isolation, strict-xfail registry, hang watchdog |
| `backends/__init__.py` | the adapter seam: backend loader, ABI check (LIFE-04), fail-closed native import, trampoline |
| `backends/reference_abi.py` (+ `_reference_engine.py`, `_reference_pools.py`) | pure-Python reference implementation: the executable oracle; stdlib only |
| `support/catalog.py` | the 100 case IDs of spec §14 (checked against the spec text) |
| `support/helpers.py` | `Harness`, commands (`Rec`, `Block`, `Raise`, `Call`, `Sleep`), recorders, policies, the load-robust timing helper (`assert_timing`, `until_conclusive`, `inside_call`) |
| `support/verdict.py` | session verdict PASS / FAIL / BLOCKED, exit status, `--verdict-json` |
| `support/coverage.py` → `COVERAGE.md` | generated case ID ↔ test table |
| `cases/serial/` | ADM, ORD, EXP, ASK, SNP, EVT, TOL |
| `cases/supervision/` | SUP, CUS, STP, TMR |
| `cases/lifecycle/` | DRN, LIFE, LIV, FREE |
| `cases/pools/` | EFF |
| `cases/keyed/` | KEY: keyed actors (I2, spec §3.5) — mutex creation, lookup, failure cleanup, key release, restart stability |
| `cases/meta/` | suite integrity: catalog = spec, every ID covered, `COVERAGE.md` fresh, import closure, verdict logic, timing helper |

## Running

From the repository root, with the uv-managed interpreter:

```bash
uv run --python 3.12 pytest conformance/actor_engine --actor-backend=reference-abi -q
uv run --python 3.12 pytest conformance/actor_engine --actor-backend=native -q
```

`--actor-backend` defaults to `reference-abi`. For the native backend, build and
stage the extension with the conformance handlers first (see below):

```bash
scripts/build/build_native_engine.sh --conformance
```

For the native gate, also pass the free-threaded interpreter (see *FREE-03*);
without it the run ends `BLOCKED`, never `PASS`:

```bash
scripts/build/build_native_engine.sh --free-threaded --conformance
export HYPRIAL_CONFORMANCE_FT_PYTHON=$(uv python find 3.14t)
export HYPRIAL_CONFORMANCE_FT_PATH=$PWD/crates/target-ft/stage
uv run --python 3.12 pytest conformance/actor_engine --actor-backend=native -q \
    --verdict-json=native-verdict.json
```

Every run ends with a `CONFORMANCE VERDICT:` line and a distinct exit status
(see *Verdict and exit codes*). Read the verdict, not the dots.

Release gate (spec §14): three consecutive `CONFORMANCE VERDICT: PASS` (exit 0)
`--actor-backend=native` runs, while `reference-abi` stays `PASS`. A `BLOCKED`
native run is not a green gate.

Environment knobs (none of them changes a semantic bound):

| variable | default | effect |
|---|---|---|
| `HYPRIAL_CONFORMANCE_WAIT_SCALE` | `1` | multiplies `WAIT` (10 s), the bound of every wait that only stops a hang, and the watchdog |
| `HYPRIAL_CONFORMANCE_TIMING_ATTEMPTS` | `5` | N of the best-of-N timing trials (see *Timing under load*) |
| `HYPRIAL_CONFORMANCE_FT_PYTHON`, `HYPRIAL_CONFORMANCE_FT_PATH` | unset | FREE-03 interpreter and staged module |

## Verdict and exit codes

`support/verdict.py` (registered by `conftest.py`) turns the session into one
verdict, printed as the last terminal-summary section and returned as the
process exit status:

| verdict | when | exit status |
|---|---|---|
| `FAIL` | any test failed, errored or XPASSed; a collection error; any skip **without** the `BLOCKED:` prefix (skips are forbidden in this suite); no test ran; pytest itself ended non-zero | pytest's own non-zero status (`1` for failures, or when pytest alone would have said `0`) |
| `BLOCKED` | otherwise, at least one case reported itself explicitly unavailable with `pytest.skip("BLOCKED: ...")` (`support.verdict.BLOCKED_PREFIX`); each one is listed with its reason | `7` |
| `PASS` | everything else: every test passed, or is a strict xfail of a known defect that failed as expected | `0` |

The last section reads, for example:

```text
============================= conformance verdict ==============================
BLOCKED cases/lifecycle/test_free.py::test_free_03_module_import_keeps_gil_disabled: FREE-03 needs a free-threaded interpreter ...
- 1 test(s) BLOCKED (explicitly unavailable on this host)
CONFORMANCE VERDICT: BLOCKED backend=native python=3.12.13 exit=7
```

`--verdict-json PATH` writes the same data: `verdict`, `exit_status`,
`backend`, `python`, `python_executable`, `pytest_exit_status`, `passed`,
`failed`, `errors`, `xfailed`, `xpassed`, `collection_errors`, `blocked`
(node ids), `blocked_reasons`, `unprefixed_skips`, `failures`, `reasons`.
Strict xfails stay as they are (an expected failure is not a failure, an
XPASS is). The verdict covers the tests that ran: a `-k` / path subset is a
verdict about that subset only.

## Timing under load

Spec timing clauses (TOL-1 ±50 ms: ask / stop / drain timeouts, `send_after`
delay, restart delay; TOL-2 "checks once") keep their bound; only the
*measurement* is made robust against a loaded host (HQ review: macOS, load
≈ 12). Every such assertion goes through `support.helpers.assert_timing`:

- the **lower bound is strict on every trial** and fails at once: load only
  lengthens a measured interval, so an early interval is an engine that gave up
  or fired early;
- an interval above `nominal + TOL` is re-checked against a **control**: the
  overshoot of an equivalent plain-Python wait (`threading.Event().wait(nominal)`)
  measured right after the trial, outside any engine call; the platform's own
  lateness in that moment is credited, nothing else;
- **best of N** (`HYPRIAL_CONFORMANCE_TIMING_ATTEMPTS`, default 5) fresh trials;
  one passing trial satisfies the clause.

A semantically wrong engine (doubled delay, a timeout that waits for the
handler, a blocking non-positive deadline) misses the bound in every trial while
the control stays near zero, so it still fails; `TOL` itself never grows and exact
values (restart delay at jitter 0) stay exact. `cases/meta/test_timing_helper.py`
pins this behaviour. Two companions follow the same rule:

- `until_conclusive`: an observation that is only meaningful inside a window
  (cancel a timer before its delay elapsed; two failures inside a 0.4 s
  restart window) checks that the test thread really acted inside the window
  and repeats the trial with fresh setup otherwise; the clause itself is
  asserted in every conclusive trial;
- `inside_call`: GIL-release cases (ASK-01, LIV-03) are event-driven: the
  handler keeps working until it has seen the caller inside `ask` / `stop` /
  `drain` and a counter thread advanced, instead of assuming that after a fixed
  sleep.

Waits that exist only to stop a hang are not semantic bounds: `WAIT` is 10 s
(scaled by `HYPRIAL_CONFORMANCE_WAIT_SCALE`), `Block` releases itself after
`3 * WAIT`, timers that must stay pending use `WAIT`-long delays, and the
`slow` restart profile holds `restart_pending` for `SLOW_DELAY` (1 s).

## Backend injection

`backends.load(name)` is the only way a case reaches an engine:

- `reference-abi` → `backends.reference_abi`;
- `native` → `hyprial._core.actors`. **Fail closed:** a missing module raises
  `BackendLoadError`, so every case errors; nothing is ever skipped.

Every backend must expose the full module surface of spec §3 and report
`ENGINE_ABI_VERSION == 1` (an `int`); anything else is refused (LIFE-04).

## Handler kinds and the native injection channel (CL-2)

Most cases run twice, once with a Python callable handler and once with a
Rust-native handler (fixture `h`, ids `py-handler` / `native-handler`). Both use
the same *trampoline* semantics: a callable command is called and its return
value returned; the string `"panic"` panics (a Rust `panic!` in the native
handler, `PanicException` — a `BaseException` — in the Python backends); any
other command is echoed back.

The native handler reaches Python through the **test-only** channel ruled in
CL-2: the extension built with the cargo feature `conformance-handlers` registers
the factory `conformance.trampoline` through the Rust-side
`register_native_factory` during module init, and the suite obtains it with the
public lookup `Engine.native_handler_factory("conformance.trampoline")`.

- The feature also registers `conformance.env_panic_factory`. It reads the
  environment variable `HYPRIAL_CONFORMANCE_BUILD_PANIC` on every `build()`
  call, panics in `build()` only while the value is exactly `"1"`, and
  otherwise builds the trampoline handler; `handle()` never reads the
  variable. The suite sets and clears the variable only through `monkeypatch`
  (SUP-06 restart-time factory panic, SUP-10 first-start factory panic). The
  Python backends provide the same name through their twin channel.
- The channel is not part of the public ABI; application code must not depend
  on it, and wheels are built without the feature.
- A native module built without the feature fails every native-handler case
  with `ValueError: unknown native handler factory` — an error, never a skip.
- The Python backends register the same trampoline through their own
  `register_native_factory` twin (`backends.install_python_trampoline`).

## Known defects (strict xfail)

Only defects recorded in spec §16 are marked, in `conftest.py`
(`KNOWN_DEFECTS`), as `xfail(strict=True)` with the defect reference; an
unexpected pass is red. The registry is empty: the P2b containment fix removed
the `SUP-09` entry, and P8 removed the `pykka` backend it described (a
`BaseException` silently killed the actor thread). A failure on the remaining
backends is a suite bug, a spec ambiguity or an engine gap and is reported, not
hidden.

## FREE-03 (free-threaded import)

FREE-03 imports the backend under a free-threaded interpreter and asserts the
GIL stays disabled. Pass the interpreter with `--free-threaded-python=<path>` (or
`HYPRIAL_CONFORMANCE_FT_PYTHON`) and, for the native backend, a directory that
contains a free-threaded build of `hyprial/_core/actors` via
`HYPRIAL_CONFORMANCE_FT_PATH`. Without them the native case is **BLOCKED**:
it reports `pytest.skip("BLOCKED: FREE-03 needs a free-threaded interpreter ...")`,
the session verdict becomes `BLOCKED` (exit 7) and lists it. That is an explicit
"not proven on this run", never a PASS and never a silent skip. The Python
backend (`reference-abi`) is unchanged: it runs the import probe plus an Engine
use check on the running interpreter and passes or fails on it. A
non-free-threaded interpreter passed as the option fails the case.

## Import closure

`cases/meta/test_import_closure.py` proves the suite imports no business
package: a static scan of every suite file, and a fresh interpreter that loads the
selected backend plus all support code and inspects `sys.modules`.
`hyprial.daemon`, `hyprial.identity`, `hyprial.biz` and `hyprial.shell` are
forbidden for every backend; `reference-abi` and `native` load no
`hyprial.kernel` module at all.

## Isolation and hangs

`conftest.py` points `HOME`, `TMPDIR` and the XDG directories at a private
temporary directory for the whole run; no case reads or writes user or production
state, and no daemon is involved. A `faulthandler` watchdog dumps all stacks and
exits the process if a single case hangs for `max(120 s, 12 * WAIT)`.

## Coverage table

`COVERAGE.md` maps every case ID to its tests. Regenerate after adding cases:

```bash
cd conformance/actor_engine && ../../.venv/bin/python -m support.coverage
```
