# PR pytest impact selection

PR feedback may use an audited test subset. Every push (including dev/main),
manual run and scheduled run still invokes the entire `tests/` suite. The
existing unit check, two-worker file grouping, crash/FD guards, lint, compile and
release-tag conditions are unchanged. GUI-only Python integration selection
remains a separate existing path.

`scripts/ci/pytest-impact-map.json` is the reviewable source-to-consumer-test map.
`scripts/ci/pytest_impact.py` reads the real PR event, checks out no code and makes
no network requests. It compares `merge-base(base, head)..head`, requires the
current checkout to equal the PR head, expands test patterns to existing regular
files, and executes pytest using an argv list. Pytest's exit status is preserved.
The log and step summary show the mode, groups, reason, compared commits and
complete file list. There is no `-k` filter and no retry of failing tests.

## Initial audited groups

- Routine template loader and the existing selfdrive YAML: all routine tests,
  CLI entry/help, real registration/restart, daemon coordinator/reconciliation,
  URI and actor-runtime boundary tests.
- Org document parser: org store/mesh and isolated Zenoh tests, real CLI org/init,
  daemon context, startup reconciliation, transport, and Lark's independent
  document consumer.

Common backend naming/terminology guards accompany either selection. Mapping is
by exact source path, not directory: new module files do not inherit permission
to skip full tests. Multiple groups form a sorted union. Matching consumer-test
modifications select the same group(s).

Stores, public schemas/ports/registry/package exports, root CLI/daemon,
installation, dependencies, CI policy, shared fixtures and every unrecognized
change require full pytest. Added, deleted, renamed and type-changed code/test
paths also require full pytest. Known prose may accompany an eligible change;
the earlier coarse gate still owns documentation-only skipping. Missing history,
invalid event/map, missing tests and unsafe/symlink test paths fail closed to full
pytest, never to an empty success.

## Preventing stale dependency maps

The first map pins `auditCommit`. If backend source outside the explicitly
reviewed paths has changed since that revision, selection expires and full tests
run. This includes a new consumer already merged into the PR's base: examining
only this PR's diff would miss that change. Missing audit history likewise
selects full tests. This intentionally conservative pilot may expire on unrelated
backend changes; refine it only with dependency evidence, not silent exceptions.

To add a group or refresh the audit, review direct imports, reexports, CLI and
subprocess calls, plugin/data consumers and relevant integration contracts.
Update the map and audit revision in a normal PR. Policy/script changes select
the full suite themselves. Do not refresh the revision solely to make CI faster.

## Validation boundary

Real-Git regressions cover complete PR ranges, base branches that advance,
multiple groups, file structure changes, missing history, unsafe maps and stale
consumer audits. A real subprocess regression proves that a selected failure
fails the gate and that a push executes a deliberately failing unselected test.
Run each group's actual consumer suite before enabling it; retain full CI for
the policy PR and all release commits. These are initial audited areas, not a
claim of a complete Python dependency graph or support for selecting every
business module.

## Local verification (2026-09-15)

Based on dev `93f7877530c6bf732591925f8a2cf111aa72bf6c` (merged #544):
selector and workflow regressions: 52 passed; routine consumer group: 134 passed
across 17 files; org consumer group: 143 passed across 12 files. Each consumer
group ran independently with two workers and file grouping. Counts overlap
between groups. Ruff and diff whitespace checks passed.

Independent selection exposed a collection-order dependency in CLI init tests:
the first MCP import evaluated `Popen[bytes]` after the test replaced Popen. The
fixture now imports that dependency before stubbing and avoids probing its fake
PID. Startup failure assertions remain intact; the CLI init module passes alone
both serially and with two workers. No production startup behavior changed.

## Dependency re-audit after shared onboarding (#508, 2026-09-15)

This section is a historical record of the audit performed on that date; its
revision was superseded by the 2026-09-17 re-audit below. The refreshed audit was
pinned to merged #548, dev
`1af990bc071988a61444ecb66401a1d323c598d4`. The previous audit at
`93f7877530c6bf732591925f8a2cf111aa72bf6c` expired correctly when #508 changed
`src/hyprial/cli.py`. The complete backend source delta between these revisions
contains only that CLI refactor and a `home.py` docstring explaining login's
new use of home initialization. No other backend module changed.

The review followed direct imports, relative imports and package reexports,
then the CLI entry points and their startup/transport consumers:

- Routine's `templates`/`templates show` still read `BUILTIN_TEMPLATES` and
  `template_text` in `routine/cli.py`; root CLI `routine add` still renders
  the template and registers the resulting routine through the daemon. None
  of those functions changed in #508. Existing routine, CLI help, daemon and
  actor boundary coverage remains applicable; this group's scope is unchanged.
- `org.document` still flows through `OrgContextStore` and `OrgContextMesh`,
  their `org` package reexports, daemon context, and CLI `org context` show/import/status.
  Existing org, daemon, transport and independent Lark coverage is retained.
- Login now initializes a missing home and calls `_initialize_org_context`,
  whose `OrgContextStore.load_accepted()` reads/parses accepted context (also
  relevant when a legacy home is migrated). Both init and first-time login
  now finish through `_complete_initialization`, including org warnings and
  fetching candidates. The identity transaction remains held across daemon
  startup. An existing owner's login retains the account-switch path.

The org group therefore adds whole files, without test-name filtering:

| Added test file | Consumer or boundary covered |
| --- | --- |
| `test_cli_onboarding.py` | Shared init/login flow, routing by owner, identity before transport, real isolated daemon startup, failure recovery |
| `test_init_caller_identity.py` | Init's caller/owner boundary during shared initialization |
| `test_home_guard.py` | Missing-home creation and guarded commands |
| `test_login_cli.py` | CLI login result/error and existing-owner contracts |
| `test_login_orchestrate_daemon.py` | Existing-owner account switch, identity lock and daemon handoff |
| `test_tsnet_sidecar.py` | Missing-home login initialization before sidecar transport, including environment-owner routing |
| `test_login_u3c_e2e.py` | Local regression checks for the login end-to-end script and its phase/error budgets |

The last file checks the external acceptance harness locally; selecting it
**does not run or claim live Headscale/OIDC acceptance**. Low-level OIDC/login
implementation, sidecar implementation and their dependencies did not change;
this refresh does not map those source files. Root CLI and home changes still
require full pytest, as do audit-policy changes themselves. Future unrelated
backend changes can still expire this audit: the conservative safeguard is
unchanged, and a refresh is evidence for this exact baseline, not permission to
keep advancing the revision automatically.

Verification used a disposable detached worktree and real commits based on the
new audit revision. A template-only change selected 17 files, a document-parser
change selected 19 files, and a root CLI change selected full pytest. The same
events on `push` all selected full pytest. Substituting the old audit revision
made all three PR cases fall back to full pytest, proving the refresh resolves
the observed expiration without relaxing its guard. Temporary commits were not
pushed, and the disposable worktree was removed.

Independent local runs with `-n 2 --dist=loadfile --max-worker-restart=0`:
Routine **134 passed / 17 files** (pytest 32.63 s, process wall time 52.05 s);
Org **290 passed / 19 files** (pytest 63.85 s, process wall time 74.33 s).
Selector/workflow regressions: **52 passed**. Ruff across the checkout and Git
whitespace validation passed. Timings are local measurements, include overlapping
tests between groups, and do not estimate remote CI queue/setup time.

## Consumer re-audit and widened groups (#549, 2026-09-17)

The sections above record the audits that produced `93f78775` and the `1af990bc`
revision; they are historical. The authoritative selector baseline is always the
`auditCommit` field, which this re-audit sets to
**`45356e59a57ca012af9711e8c9b0f925cdfdd408`** (the dev head merged into this
branch). The `1af990bc` audit had expired: 35 `src/hyprial/` paths changed
between it and that head, including `cli.py`, `daemon/application.py`,
`daemon/home_guard.py`, `home_migration.py` and the `org`/`pac` packages, so
selection fell back to full pytest.

What was audited, and how:

- Object of the audit: the `src/hyprial` and `tests` trees at `45356e59…`. The
  post-merge tree was checked to carry unchanged blobs for every mapped source
  (`org/document.py` `53c61044`, `routine/templates/__init__.py` `6d84a44a`,
  `routine/templates/selfdrive.yaml` `9d088fd8`), so the audit applies to this
  head exactly.
- Method: static import reachability only, no test execution and no network. A
  throwaway `ast` script (not committed, because this PR stays inside its two
  files) parsed every `tests/**/*.py` and every `src/hyprial/**/*.py` and
  computed the complete forward closure: absolute and relative imports, package
  `__init__` re-export chains (importing `a.b.c` executes `a/__init__.py` and
  `a/b/__init__.py`), `from pkg.mod import Name` where `Name` is a module, and
  deferred `"hyprial.…"` string imports. The script imported nothing, started no
  process and wrote nothing; its output is the consumer list in the map.
- Result: `hyprial.org.document` is reachable from 308 tracked test files and
  `hyprial.routine.templates` from 305. Their declared groups now contain those
  closed sets (311 and 306 files after retaining every path the earlier maps
  listed). Removal is absent at the expansion level: the union each group
  expands to is a strict superset of the fork-base map, of the `1af990bc` map
  and of the map this branch carried, and re-adding the two globs below leaves
  every expanded path unchanged. The entry lists themselves are not claimed to
  be unchanged, because the two original glob entries were re-added to them.
  Three retained org-document paths (`test_init_caller_identity.py`,
  `test_login_u3c_e2e.py`, `test_transport_contract.py`) and one routine path
  (`test_routine_pac_journal.py`) are not in their group's static closure; they
  are kept deliberately, because dropping a selected test can only narrow the
  gate and the reviewable direction here is to widen.
- Lookahead compatibility: both groups keep their original glob entries,
  `tests/test_org*.py` and `tests/test_routine*.py`, alongside the audited
  explicit lists. The explicit lists pin today's coverage; the globs keep a
  future `tests/test_org*.py` or `tests/test_routine*.py` file selected
  automatically, which is the behaviour the earlier maps relied on. On this head
  the globs match 3 and 6 regular, non-symlink files, all of which the explicit
  lists already contain, so adding them changes nothing today and only prevents a
  forward-looking narrowing.
- Reliability limit, stated plainly: static reachability is a sound
  over-approximation of the *import* relation, not the *execution* relation. A
  test can reach a module without exercising it, and dynamic access the parser
  cannot see (a subprocess, plugin registry or environment-driven import) is
  absent from the closure. That is acceptable only in the widening direction.
  The closure does not license removing tests from these groups.
- Unmapped backend paths are unchanged: `cli.py`, `daemon/application.py`,
  `daemon/home_guard.py`, `home_migration.py`, `tsnet_join`/`tsnet_sidecar`, the
  rest of `org`, the `pac` package and every other source path stay outside the
  map, so a change to any of them still fails closed to full pytest. `pac` in
  particular was left unmapped on purpose: it carries a large 2026-09 refactor
  and no audited consumer list.
- Boundary this establishes: an eligible change is exactly a modification of the
  two mapped source paths (or of a test path already in a group). Everything else
  is "full pytest". A dev head that later changes any unmapped backend path
  invalidates the `45356e59…` audit by the drift check, as before.

Verification performed on this head (`f0cd68bf…`, whose parents are the merge
`a8bf7186…` with ordered parents `cad64921…` + `45356e59…`):

- `scripts/ci/pytest-impact-map.json` parses; both groups expand to existing,
  regular, non-symlink files under `tests/`; all 617 selected paths exist.
- Re-running the `ast` closure on this head reproduces both closures exactly,
  and each map list is a superset of its closure.
- `scripts/ci/pytest_impact.py --plan-only` dry runs against the head's map, with
  synthetic one-file PR commits parented on the head: a change to
  `org/document.py` or `routine/templates/selfdrive.yaml` selects that group's
  files, and a change to the unmapped `src/hyprial/uri.py` returns full pytest.
  No test was executed for these checks; they only exercise Git and the map.
- No dynamic suite was run for this re-audit, and no CI run is claimed. The
  counts above are static file counts, not pass counts.
