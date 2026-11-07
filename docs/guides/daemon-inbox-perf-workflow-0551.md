# daemon / inbox performance 0.5.5.1 development workflow

Status: WIP. This is the current user-approved development and release-avoidance process, superseding the initial branch/PR arrangement in `docs/notes/daemon-perf-0551-scope-2026-10-24.md`. This work targets 0.5.5.1 and must not alter the 0.5.5 flag-day sequence.

## Branch and PR authority

```text
dev @ 9e606260d or newer
  ├─ intg/daemon-inbox-perf ── WIP #1892 discussion/integration PR ──> dev
  │    └─ future fix branches ── reviewed work-item PRs ──> intg/daemon-inbox-perf
  └─ fix/daemon-perf-0551 ── WIP #1891 ──> intg/daemon-inbox-perf
       (created directly from dev; subsequently merge the integration head)

dev + pinned #1889 ── test/daemon-perf-0551-baseline-local (NEVER PUSH)
candidate + same pinned #1889 ── disposable local measurement branch (NEVER PUSH)
```

The integration worktree was created from current dev, not the old document tree. #1890's exact document head `f574b463d3649fec922de46c81c5dd35240e55bd` was then merged intact into integration. Its code line numbers refer to `ffb63ad01`; locate by function name on the new base. Section 9 takes precedence over earlier suggestions to remove read-like commands from the actor.

The current Forgejo API can retarget a PR's base but cannot change its head branch. WIP #1892 therefore replaces the #1890 discussion entry point. #1890 was closed with a successor link after its document was merged into integration; its old branch, document bytes and commit ancestry remain intact. #1891 is retargeted from dev to integration. No force push, rebase, cherry-pick or branch deletion is part of this setup.

All later work branches start at the then-current `intg/daemon-inbox-perf` head. Work-item PR completion means merging into integration, not closing without merge. Keep a separate conventional commit for each numbered fix and retain its RED/GREEN evidence. Ordinary protected-branch checks and review still apply; do not bypass them. The integration discussion PR remains WIP until the development plan is complete and release conditions below are satisfied.

## Worktrees

| Role | Path | Branch |
|---|---|---|
| Discussion and integration | `/private/tmp/hyprial-daemon-inbox-perf-intg` | `intg/daemon-inbox-perf` |
| Existing implementation work | `/private/tmp/hyprial-daemon-perf-0551` | `fix/daemon-perf-0551` |
| Pre-tag measurement baseline | `/private/tmp/hyprial-daemon-perf-0551-baseline` | `test/daemon-perf-0551-baseline-local` |
| Measurement candidate | Create a separate disposable worktree at the measured candidate | Local measurement branch only |

Use explicit cwd for each command. Do not change or clean other worktrees, including the original user workspace. Only integration and implementation branches may be pushed; never use `git push --all` or `--mirror`.

## 0.5.5 release avoidance

Before a final 0.5.5 tag exists:

- Keep this work's published PRs WIP and do not merge anything from this work into dev.
- Allowed product scope now: inbox no-change version/projection fix; `space_authority._ask` operation counters; the two requested document/composition waits; optional anti-entropy cadence.
- Defer changes to `application/actors/sessions/routes.py` until #1889 has actually merged into dev. Before that, session optimization may touch only the ps-call path in `mcp/channel/ownership.py`.
- Do not edit `session_actor/*`, `identity/impl/agents/actor/*`, `identity/impl/agents/state/liveness.py`, `pac/workflows/runtime/core.py`, `tests/integration/native_harnesses/test_native_resident_tripwires.py`, or `scripts/ci/registry-union.json`.
- Do not move `NATIVE_ORIGINS`; the registry-union gate reads its configuration from the base branch.
- Do not cherry-pick any 0.5.5 PR. The only pre-tag merge of #1889 is into local measurement branches, never the published implementation/integration branches.

After the final 0.5.5 tag:

1. Fetch and record the final tag's object/commit and current dev head; an rc tag alone does not release this constraint.
2. Merge current dev into `intg/daemon-inbox-perf`, then merge integration into outstanding work branches. Preserve history; resolve any conflicts explicitly and record their scope. Do not cherry-pick release PRs.
3. Verify #1889 is an ancestor of the new dev base before enabling the deferred sessions route change and accepting measurement evidence.
4. Repeat before/after measurement on the post-release dev baseline. Pre-tag results are provisional and cannot replace this run.
5. Remove WIP and merge the integration discussion PR into dev only after the agreed plan and its acceptance evidence are complete. Tag creation alone is not performance acceptance.

## Measurement baseline and isolation

Initial remote dev: `9e606260d3958c2bbfdb99491e5f9e0fc3bf7446`.

Initial pinned #1889 head: `8e088188538fa45d12dab4a12ef4568393e3af6b` (`fix/heartbeat-constant-cost`; includes the review evidence after product head `69564488e`). This was merged, not cherry-picked, into the local baseline to produce `df33154bdc59cb56a9d2a2e1818254deb6223d8c`.

For pre-tag comparisons, merge that exact same #1889 head into a disposable candidate measurement branch. A refreshed heartbeat head requires preparing and measuring both sides again. Never attribute #1889's improvement to this work.

Every workload must launch with an explicitly isolated home, HYPRIAL_HOME, state directory, socket, XDG directories, uv cache and ports, with inherited worker binding removed and network isolation selected. Do not start an unqualified product CLI or test collector that can resolve production defaults. Do not inspect, open SQLite from, signal, restart or query production daemon or `~/.hyprial`.

Use `hyprial daemon status --json` only against that isolated instance. Preserve raw start/end snapshots and workload results under `docs/notes`, along with:

- exact baseline/candidate and heartbeat SHAs, Python/runtime versions and load parameters;
- same concurrency, session/space/message inventory, warmup and measurement duration;
- per-component `ipcStats` threadCpuMs deltas, processCpuSeconds delta and metric coverage;
- inbox command counts, version changes, available queue observations, overload and timeout counts, and latency distribution;
- a clear UNKNOWN where a requested metric is not exposed or was not measured; zero must be observed, not assumed.

Thread CPU excludes blocked waiting. Its sum alone does not prove GIL saturation. Component evidence does not establish live or complete load acceptance.

## Ordered delivery and acceptance

1. Inbox: keep commands inside the actor; omit version bump/full projection refresh only for the specified commands without persistent changes. First reproduce concurrent empty message waits on the unmodified product. Afterward the version stays fixed and command count is bounded by wait duration and the existing scan cadence. Test real receive/ack/fetch mutation wakeups, no-change fetch/drain, and unchanged W-slot admission.
2. Sessions: obey the #1889 route-file deferral. Avoid repeated ps while preserving PID birth/reuse fencing, older marker formats and unreadable-as-UNKNOWN behavior.
3. OrgFS: count `_ask` operations by a bounded operation-type set; reproduce in isolation before choosing any merge/coalescing or frequency reduction. No optimization by inference alone.
4. Waits: bound the waits in `orgfs/document/space_state.py` and `orgfs/composition/lifecycle.py`. Test no reply, accepted operation, late completion and authority retention; caller timeout is not cancellation.
5. Optional cadence: separate anti-entropy only with its own evidence and commit. Do not expand into 0.5.6's admission lanes, step contracts, unified backoff, process split or PAC redesign.

For each behavioral change retain an exact unmodified-product failing run and candidate passing run. Run structural/module gates before product/test restructuring, focused tests first, affected-directory tests and ruff before delivery, and the repository-required full suite with its prerequisites. Use uv, never bare Python. Keep PASS, FAIL, BLOCKED, UNKNOWN, NOT_RUN and skips distinct.

After each item completes, report to R&D head `emergent-org-structure-refactor`: work-item PR, integration PR, head SHA, RED/GREEN figures, stress numbers, verified/inferred/untested claims. No product item is complete at workflow setup.

## Open-PR overlap inventory

The initial `fj pr search` exposed only 30 entries. Authenticated paginated API enumeration found 62 open PRs (50 + 12); the full inventory is retained in `docs/notes/daemon-perf-0551-open-prs-2026-10-24.json`.

| PR | Relation and disposition |
|---|---|
| #1890 | Original discussion document; preserved verbatim and in history within integration; closed with successor link to WIP #1892. |
| #1891 | Existing performance work item; retarget to integration, keep WIP. |
| #1889 | Independent 0.5.5 heartbeat sequence; local-only measurement prerequisite; do not redo or alter merge order. |
| #1830 | Independent 0.5.5 CLI sequence; respect protected files and release ordering. |
| #1879 | OrgFS queue-bound test correction; track as potential baseline-test context, do not import it or weaken tests. |
| #1742 | PAC host-load/daemon-health admission design; related overload discussion, separate from this 0.5.5.1 scope. |
| #1572 | System-notice ack/migration/capacity changes; potential inbox overlap, independent PR with its own freeze rule; do not absorb. |
| #1541 / #1542 | Daemon shutdown hardening / session-route retry and replay; separate failure paths, not this performance plan. |
| #1511 | Orphan probe/backoff/capacity work; adjacent periodic load, separate scope. |
| #1464 | Broader thread-per-harness/org structure discussion; not an additional performance dependency. |

Only #1890's analysis is a development-discussion dependency; the 0.5.5 PR sequence stays independent. No other enumerated PR has been merged, retargeted or closed by this workflow setup.

Contract-Impact: setup contains documentation and PR topology only; reassess diagnostics and timeout contracts at implementation.
Skill-Impact: not-needed: task-specific branch/release instructions only; no product command, identity protocol or shared skill behavior changes.
