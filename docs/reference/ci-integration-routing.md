# CI integration routes and staged execution

`scripts/ci/ci-plan.mjs` is the common workflow planner. It enforces the dev-source
route before dependency installation, then delegates Python subset eligibility
to the existing audited classifier/consumer map. A missing PR comparison is a
failure, never permission to skip checks. There are no PR-number exceptions.

## Branch flow

| Event | GUI/sidecar behavior | Python behavior |
|---|---|---|
| PAC workflow replacement via `intg/pac-v2-full-impl` | GUI workbench and Python checks run | Full or audited Python checks |
| Feature PR into `intg/gui*` | GUI checks run | Audited GUI integration subset or conservative full |
| Feature PR into `intg/sidecar*` | Sidecar checks run | Full when code changes; preserves Python consumers |
| Component PR into `release/*` | No source-branch restriction since 2026-11-03; suites follow integration branch names | Audited selection or full fallback |
| Unrelated backend PR into `release/*` | Only cheap route/plan jobs; no GUI/Go dependency installation | Python tests; stale/unmapped consumer map falls back to full |
| Promotion PR `release/*` or `hotfix/*` into `dev` | Suites of every promoted component still run | Audited selection or full fallback |
| Any other PR into `dev` | Refused: `CI_ROUTE: dev accepts only release/* and hotfix/* PRs` | — |
| Pure prose PR | Cheap route/plan checks; integration branch names still force their component suite | No empty pytest command |
| Direct push to `intg/gui*` / `intg/sidecar*` | Corresponding component suite | Same planning policy; no publication authority |
| Push to `dev` | Do not repeat GUI/sidecar suites after landing | Full Python, including Python GUI/sidecar integration tests |
| PR into `main`, push to `main`, manual full validation | Full component checks | Full Python |
| `tsnet-v*` tag | Full sidecar release workflow | Independent release namespace |

The PAC migration route is an exact branch name, covering the user-approved
daemon/CLI plus GUI replacement. It is not a PR-number or arbitrary `intg/pac*`
exception.

The prefixes are literal starts-with rules: `intg/gui-next` and
`intg/gui/topic` qualify; `feature/intg/gui-next` and legacy `intg/<member>-gui`
do not. Targeting an integration branch permits ordinary feature source names.
Since 2026-11-02 (Allen) `dev` accepts only `release/*` and `hotfix/*` PRs.
Since 2026-11-03 (Allen) the component-source restriction is removed: GUI and
sidecar product changes no longer have to come from `intg/*` sources; the
integration branch names only select component suites.

Integration pushes use the complete branch difference against an independent
dev merge base, rather than the push's before/after interval. Merging dev back
into a sidecar branch can legitimately import GUI fixes already accepted on dev
(run #6565); those imported changes are not new GUI work on the sidecar branch.
Foreign-component edits in the merge resolution, earlier branch-only commits,
and renamed/deleted files still appear in the comparison diff that selects suites.

The planner fetches dev and logs `comparisonBase`, `comparisonHead` and
`comparisonKind`. If a historical push is checked after its head has landed,
it walks back dev's first-parent history past merges containing that head, so
the branch cannot become its own comparison base and hide an illegal diff.
This search/fetch is bounded to 128 commits. Missing history or a fast-forward
history without a provable independent boundary refuses the integration push;
it never emits an unverified empty plan. Ordinary dev landing and main/tag/manual
policies are unchanged.

Ownership includes `src/gui/`, `src/hyprial/gui*.py`, Python GUI tests and the
GUI release builder; sidecar ownership includes `sidecar/`, `tsnet*.py`,
sidecar/tsnet/forwarding-sidecar tests, `tests/test_ci_go_modules.py` and their
named fake-sidecar fixture.
Known prose files are exempt. Exact rules live in `component()`.

CI governance is shared infrastructure, not GUI/sidecar product ownership:
`.forgejo/`, `scripts/ci-*`, `scripts/pytest*`, `tests/test_ci_*` (except the
sidecar-only `test_ci_go_modules.py`),
`tests/test_pytest_*`, `tests/test_gui_ci_migration.py`, and GUI `scripts/ci-*`.
Changing these inputs runs all relevant suites. Adding product changes beside
them no longer requires a specific source branch (2026-11-03). Dependencies/build inputs
and unknown paths are conservative full checks, not new implicit exemptions.

## Order and evidence

Each workflow computes the common plan once, in its existing first job:

- Python: `cli-help-inventory` publishes the plan; `unit` checks upstream
  success and boolean output completeness, then runs static checks and pytest.
  `unit` runs on failed prerequisites to report an explicit failure rather than
  allowing a skipped required check to look successful. Internal publication
  remains in unit, fenced to a successful full Python run on refs/heads/dev.
- GUI: `gui-unit` publishes the plan and runs generated-client/Node/migration
  checks at explicit file concurrency 2. Browser, Codex and package jobs need
  that success and their selected output.
- Sidecar: `test` publishes the plan before any Go installation. Build and live
  E2E jobs require both test success and the selected sidecar output. A rejected
  route fails the first job rather than starting expensive downstream jobs.

Python `--grouped` keeps the four audited early files (URI import boundaries,
workflow schema, CI origin policy and the consumer selector), then runs CI
policy, storage, runtime, harness/MCP, transport and CLI/GUI Python groups.
The last catch-all traverses `tests/` with explicit ignores for files already
assigned: unknown/new files and custom collectors are not dropped. Groups
own whole files and remain sequential within the same required unit job, using
at most two xdist workers in total. The first nonzero result stops execution;
later groups are not claimed as tested. There are no automatic test retries or
worker restarts. Each group has a distinct basetemp, JUnit artifact and logged
duration/exit code. Tests verify full collection union/disjointness, including
parameterized cases, partial selections and failure propagation. The old
`--staged` two-wave mode remains available locally.

New pushes supersede older Python runs of the same integration branch, just
as new PR revisions supersede their older checks. Dev/main publication runs
keep unique concurrency groups and are never automatically cancelled here.
Push and PR are still distinct validation contexts; no unaudited same-SHA
cross-workflow result reuse is introduced.

The Python consumer map is not blindly re-dated. If new consumers have appeared
since its audit, it still falls back to full. Tests whose names contain "unit"
can run real processes/databases; this ordering does not claim the remainder is
exclusively integration tests. Every release/full run still covers the full set.

## Deployment boundaries

Cold Python bootstrap has its own bounded stages: uv installation 6 minutes,
Python/locked dependencies 12, CLI inventory 2, with checkout/planning/tail
headroom inside a 25-minute preflight job. Task 22414 spent 227 seconds installing
uv and at least 353 downloading Python before the old 10-minute job cap; no tests
ran. Unit's 45-minute ceiling and test deadlines are unchanged. Setup failures
still fail the required unit prerequisite check.

A same-run Python environment artifact was tested locally, including a network-
disabled consumer, but was withdrawn before merge: the 124 MiB transfer hit
repeated HTTP 502s in task 22450 and acknowledged only 16 MiB before its 2-minute
upload limit. It is not a required or optional path in this workflow. Runner
cache was also reported unavailable by task 22418. Preinstalled, version-pinned
CI images or an operator-configured cache remain follow-up infrastructure work;
this change does not mutate global runner configuration or hide download errors.

PR checks report a route violation; enforcing merge refusal requires the repo's
required status-check policy. The observed dev protection has status checks
disabled. This change does not mutate server policy or prevent an administrator
from overriding CI. Direct pushes to dev cannot prove their former source branch;
the intended enforcement boundary is the PR plus protected-branch permissions.

Skipping repeated component suites on dev is the agreed branch workflow, not
cryptographic reuse of prior test evidence. Conflicts or bypassed checks can
invalidate that assumption. Main/full validation remains the backstop. Internal
track publication still requires full Python; stable-pointer's existing rule
(follow main independently of CI) is unchanged.

Internal publication additionally re-reads the remote dev and internal refs and
fetches ancestry (unshallowing the CI checkout). The tested commit must belong
to current dev history. Already-published/newer internal results cause no write;
an older internal can advance to the tested commit even if dev has newer,
unvalidated work. This prevents a busy dev branch starving publication. Unrelated
history fails closed. The push uses an explicit lease on the observed tag,
including the absent-tag case: a concurrent publisher invalidates the stale
push rather than being overwritten. The mover timestamp remains after successful
push. Dev is not atomically frozen by this snapshot, and no unconditional force
retry or manual tag promotion is added.

Current #693 product fixture/download fixes are split into `intg/gui-ci-portability`
and `intg/sidecar-ci-portability`. #693 itself carries CI policy/test infrastructure
and a routine-test race fix. No bootstrap exemption or branch alias is added.

Measure queued time, first failure feedback, green critical path and summed job
seconds separately. Dependency ordering can increase a green run's wall time
while avoiding expensive work after an early failure. No runner quota increase,
global concurrency change, cache deployment or cross-workflow API polling is
part of this change.
