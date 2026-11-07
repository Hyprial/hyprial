# Hyprial CI performance baseline

Each measured step sources `scripts/ci/ci-step.sh`. It prints a `CI_TIMING` line
with elapsed seconds and the original command exit status, including failures,
and appends a row to the Forgejo step summary. Before and after resource snapshots
include load average and available cgroup CPU, memory and I/O counters. CPU/I/O
counters are cumulative: compare the two snapshots, not their absolute values.
They describe the job container, not runner-wide concurrent job counts.
Checkout timing is available from the Forgejo step UI; the helper becomes
available after checkout. Hard termination cannot run an EXIT trap.

Python and all locked test dependencies are synchronized once per job. All
following commands use `uv run --no-sync`; do not change the lockfile mid-job.
The entire test suite and existing dev tag gate remain intact. Pytest
prints its 50 slowest setup/call/teardown phases even on assertion failures.
The durable evidence is the CI log and step summary; there is no claim that
files inside an ephemeral container are persisted as downloadable artifacts.

The two-worker configuration below keeps one unit job and one publication
decision. Further sharding must preserve the FD guard and require every shard
to pass; measure runner contention before increasing worker counts.

## Change selection

The current policy is specified in [CI integration routing](ci-integration-routing.md).
Each workflow's existing first job computes `scripts/ci/ci-plan.mjs` once, before
installing dependencies, and publishes boolean outputs to its downstream jobs.
GUI and sidecar component suites follow the `intg/gui*` and `intg/sidecar*`
integration branch names; since 2026-11-03 the same changes may also arrive
from any source branch. Dev-source route violations or unverifiable PR
comparisons fail. There are no exemptions for individual PRs.

The planner reads the complete merge-base/head range, including both paths of
renames. Prose-only ordinary PRs skip heavy work; integration branch names force
their component suite. Shared CI/dependency/unknown changes remain conservative.
Dev landings retain full Python but do not repeat the component suites. Main
and explicit full checks retain complete coverage. The old audited classifier
and Python consumer map are reused; a stale audit still means full validation.

GUI Node checks precede browser/Codex/package work and use file concurrency 2.
Sidecar build/live E2E follow its Go test gate. Python `--staged` runs audited
early files before the remaining complete selection, with two workers in each
stage, separate temporary roots, no duplicate files and no failure retries.
This separates first-failure latency from total successful-run duration.

## Sidecar module downloads

Both #2478 cross-build and official E2E stopped on the same module zip
(`github.com/klauspost/compress@v1.19.1`, unexpected EOF from proxy.golang.org).
Sidecar jobs now prefer `https://goproxy.cn|https://proxy.golang.org`; the pipe
separator allows fallback on transport failures as well as missing versions.
The domestic proxy bypasses the public outbound proxy. Default checksum database
verification and go.sum remain active; no dependency version changes.

Test, cross-build and official E2E explicitly download and verify modules before
compilation. Downloads get at most three 240-second attempts with short backoff;
completed modules remain in the job's Go cache. Verification, compilation and
tests are not retried or suppressed. `go-modules` timings separate transfer from
compilation. This is a network resilience change; persistent module/build caching
is a further optimization and has not been claimed here.

`sidecar/hyprial-tsnet/e2e/download-modules.sh` retains the CI entrypoint and
delegates to the adjacent standard-library-only Python 3 script. It does not
require GNU `timeout` (absent on a stock macOS installation). Each download
gets its own process group: after 240 seconds it receives SIGTERM, followed
by SIGKILL after a five-second grace period if necessary, including remaining
Git helper processes. Timeout exits are 124, or 137 after forced termination.
Only download is retried, at most three times with two/four-second backoff;
`go mod verify` runs once after a successful download and its failure is terminal.
This POSIX helper supports the existing Linux/macOS shell entrypoint, not native
Windows. Other Linux-only E2E scripts may still use GNU `timeout`.

References: https://goproxy.cn/ and https://go.dev/ref/mod#environment-variables .

## Per-test cleanup and phase profiling

#2495 reported 4201 passed /38 skipped in 1299.95s; the measured pytest step
was 1308s. Its 50 slowest phases total 331.43s (25.5%), so they alone cannot explain the full run. CPU usage
increased by about 916.95 CPU seconds and throttling by only 0.274s, so the
2-CPU quota is not an established bottleneck for that run.

With HYPRIAL_CI_PROFILE=1 (set by unit CI), the test harness emits
CI_TEST_PROGRESS every 500 completed tests and CI_TEST_PROFILE at completion,
including collection time, setup/call/teardown totals, cumulative GC time and
collection counts by generation, and the 15 slowest files by summed phases.
GC time is already included in phases/collection: do not add it again.
These are parent-pytest measurements; subprocess work contributes elapsed call
time, but its own GC callbacks are not aggregated into the parent GC total.
Failure status is preserved. Local runs remain uninstrumented unless opted in.

FD hygiene still runs after every test. It clears fixture/global pins, collects
young cycles, and checks descriptors. If descriptors remain, it performs the
original full collection and rechecks before issuing the same leak error.
Promoted file cycles are still collected and reachable leaks still fail.
Automatic GC is unchanged. The benefit is avoiding a full imported-heap scan
for every FD-clean test; no leak check is removed.

The earlier full-collection/20-selected-test local probe reduced teardown from
6.116s to 0.026s. That is not a whole-suite or CI speedup estimate. Full validation
now includes the separately fixed init-readiness handshake, which avoids
mistaking slow daemon startup for waiting on adapter readiness.

Full local candidate validation passed: 4239 passed, 4 skipped in 1231.48s.
Parent measurements: collection 11.141s, setup 39.565s, call 1146.949s,
teardown 32.122s, GC 30.140s, 124 generation-2 collections. This includes the
four new guard probes. Local availability differs from ARM CI (which skipped 38
on the preceding baseline); the local total must not be compared directly with
CI’s 1299.95s as a speedup estimate. A candidate CI run is required.

The first ARM candidate (#2502) reduced the pytest step from 1308s to 943s,
but failed four lifecycle tests; it is not an accepted performance result.
Its parent teardown cost was 30.681s and GC cost 21.035s. The failures were
short waits for rollback completion and an assumed crash after 200 ms.
On the pre-GC-change baseline, injecting 350 ms per fake port submission
reproduced both failure classes (four failures across seven selected cases).
The tests now wait for the actual bind commit and worker exit before restart,
and allow ten seconds for the rollback result, returning immediately on
completion. The same delayed probe passes all seven cases; ordering,
durability and terminal-state assertions are unchanged. This does not change
product deadlines or retry failed assertions. A passing full CI remains required.

## Two-worker pytest experiment

The unit job uses pytest-xdist with two workers and `--dist=loadfile`: a file's
tests and module fixtures stay in one process. The worker count matches the
observed two-CPU job quota; it never expands to the runner host's CPU count.
`--max-worker-restart=0` makes worker crashes fail the job without automatic
restart. The existing unit job, scope selection, full test selection, timeout,
and successful-push-only dev tag gate remain in place. Each worker runs the
same FD hygiene fixture. xdist gives workers separate pytest temporary roots;
test-owned Hyprial homes and state directories remain per-test temporary directories.

Local invocations remain serial unless `-n` is supplied. Reproduce either mode
with the same installed test extra and checkout:

```sh
HYPRIAL_CI_PROFILE=1 uv run --locked --extra test pytest tests/ -q -n 0
HYPRIAL_CI_PROFILE=1 uv run --locked --extra test pytest tests/ -q -n 2 --dist=loadfile --max-worker-restart=0
```

The final profile includes the selected test count and a SHA-256 digest of the
sorted node IDs. Compare these between serial and parallel runs of the same
tree. xdist also rejects inconsistent collections between workers.
`wall_seconds` is elapsed time. In the parallel controller report,
`phases_seconds` and file durations are summed work across workers, so their
sum can exceed elapsed time. `gc_seconds` describes the controller itself;
`workers` contains each worker's profile and `worker_gc_seconds` sums their GC
time (which already overlaps their phases). Missing worker profiles after a
crash must not be interpreted as zero-cost successful work.

Validate on one PR before enabling the change on dev. Existing branches keep
their current workflow until they incorporate this change. An experimental PR
still consumes a runner slot and may add queue time, so do not launch several
full benchmark jobs simultaneously. No runner deployment, cache enablement,
sidecar build, or external E2E environment changes are part of this experiment.

The empty-install CLI assertion imports the CLI in a fresh process after setting
its temporary home. CLI mounts are created at import time, so changing the home
after importing the module can retain applications installed on a developer's
machine. The original failure also reproduces in an isolated serial invocation;
the test keeps both the command-inventory and rejected-invocation assertions.
The parallel guard probe deliberately leaks a file and crashes a worker in
separate subprocess runs, requiring failure without a profiler internal error.

## September 15 refresh

The two-worker change originates in PR #451, commit
`8c0351558f9495250bc1bd79cfd7279e86f7267a`, by gregpaws. It was refreshed
against dev `2a192f44`; the original author attribution is retained. The refresh
keeps the current CLI installation tests and Hyprial 0.4.5 lock metadata, adding
only pytest-xdist and execnet to the locked test environment. No production
dependency, sidecar E2E gate, or worker-crash retry policy changes.

Focused refresh validation ran the CLI installation and FD hygiene regression
files both serially and with two workers: each completed 49 tests successfully,
with identical collection SHA-256
`9404620ec5e6b2256d70f33d20fae1c36c63d42113d3fa8aa3105bdbec1467d6`.
The probes still reject intentional descriptor leaks and worker crashes without
a profiler internal error. Ruff and locked dependency synchronization passed.
These small file-grouped probes are correctness evidence, not a throughput
benchmark; the full normal CI result remains required before merging.


## Legacy PAC receipt baseline race

Full local two-worker validation exposed a pre-existing E2E-020-C setup race:
its old golden image has structure v6 and an already overdue timer, but only v5
notification decisions. The resident daemon can create and deliver the new v6
clock after the scenario's bootstrap snapshot, so a suffix expected to contain
only resent v5 receipts also contains legitimate v6 clock rows. Both public CLI
wrappers reproduced the same failure serially on dev `2a192f44` with the original
#451 refresh and no GUI migration (2 failures, 147.52 seconds). A direct serial
invocation of the legacy boundary also reproduced it. This is not an xdist-only
failure or a reason to omit those tests.

The scenario now issues public `debug clock` and waits for the exact v6 timer's
real delivery before capturing the receipt baseline. It does not change the
opaque image, pause production clocks, resend the v5 backlog early, or discard
any event after the baseline. The existing assertion that every suffix event is
an older-version delivery receipt, duplicate replay, migration-floor rejection,
and final snapshot equality remain intact. Deterministic probes cover either
writer winning insertion and reject an undelivered current clock or a delivered
old-version clock as proof of settlement.
