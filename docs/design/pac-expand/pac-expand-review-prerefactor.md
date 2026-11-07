# Review of the PAC-expand plan (wf-b590cd23#design): CHANGES

Reviewer: hyprial-cli-developer (PAC is in my domain; I maintain it afterwards). Date: 2026-09-30.
Input: /Users/bobadmin/h2b-work/pac-expand/docs/pac-expand-plan.md (feat/pac-expand @1419e944). Code checked against origin/dev (bbe80d44 line) and the open PR branches.

C01–C22 are accurate and well-grounded. Nested BEGIN IMMEDIATE, and physical task plus node_kind instead of a journal/nodes rebuild, are the right calls. So are no graph-writer blocking on git (C16), exact-replay receipts (C11) and the D cap (C07).

The four slices are file-disjoint as listed, and the frozen F1–F4 interfaces are the right shape. What blocks dispatch is interaction with work already in flight, plus one gate.

## P1 (blocking): open PR #987 already claims schema v17 and edits S2's files

`fix/workflow-user-alert-hot-loop` (#987, open) sets `SCHEMA_VERSION = 17` and modifies:
- `pac/migrations.py`
- `pac/reactor.py`
- `pac/store.py`
- `pac/workflow_runtime.py`
- `pac/lifecycle.py`

These are exactly S2's engine quartet plus the integrator's lifecycle, measured with `git diff origin/dev...origin/fix/workflow-user-alert-hot-loop -- src/hyprial/pac`.

R2's "reserve v16->v17, renumber if dev advances" is not enough, because the collision is known now, not hypothetical. Required:
- (a) Land or explicitly defer #987 BEFORE S2 starts, and base S2 on the resulting dev, so S2 is v17->v18.
- or (b) If #987 must wait, S2 takes v18 on top of #987's branch, and the integrator merges #987 first.

Either way, the S2 brief must name the exact base SHA and the migration number. S2's migration tests must include "upgrade from #987's v17". I also found no other open PAC branch touching migrations: #989 and #936 show no `src/hyprial/pac` diff against dev.

## P2 (blocking): register S3's git timeout as a budget, via the integrator

`prepare_worktree(plan, *, timeout_s: float = 30.0)` and `cleanup_worktree(..., timeout_s: float = 30.0)` are new self-built blocking-wait budgets in `src/`. `tests/test_supervision_boundary_gate.py` ("the blocking wait rule finds inlined budgets") and `tests/supervision_exemptions.json` will flag them at merge.

Change F4/S3 so that:
- `timeout_s` is a REQUIRED keyword with no default;
- the integrator passes a named budget (for example `PAC_WORKTREE_GIT_TIMEOUT_SECONDS`) registered in `src/hyprial/contracts/lifecycle_budgets.py` in a small first commit.

The same applies to I3's prepare/remove effect lanes and any new expansion validation or preflight bound. Add `tests/test_supervision_boundary_gate.py` to S3's and the integrator's focused checks.

## P3: add the repo-wide lint tests to every slice's isolation check

Each slice also runs:
- `tests/test_supervision_boundary_gate.py`
- `tests/test_ipc_error_code_registry.py`
- `tests/test_unreachable_surface_retirement.py`
- the lint-style files that glob `src/**/*.py` (grep for `rglob("*.py")`)

S4 adds `tests/test_cli_help_coverage.py`, because new `--expansion` / `--expansion-for` options without `help=` fail its undocumented-parameter budget. That is exactly what reddened #1050's unit run today. Targeted-only runs miss these; I measured it on 09-30.

## P4: the teardown gate if the integrator adds a close step

If I3's closed-worktree cleanup scheduling adds an `attempt()` step in `DaemonApplication._close`, it needs a `DAEMON_CLOSE_STEP_BUDGETS` entry in `contracts/daemon_teardown.py` (the AST gate in `tests/test_teardown_guarantee.py`). Better: keep the cleanup inside the existing PAC actor lifecycle so there is no new close step.

## P5 (clarification): C19 and the planner brief

C19 is right that predecessor `outputText` does not reach a downstream brief. That gap made this very review node unable to read its design input: `workflow inspect --node design` returned CALLER_NOT_AUTHORIZED for the reviewer, and user-exp-improver had to write the plan to a file. The bounded predecessor-output delivery in S2 fixes the general case.

Until it lands, every plan→review graph must put the artifact PATH in the reviewer's task text. The planner brief template in I8 should say so too.

## P6: operational note (not a design change)

Today (09-30, from 14:4x local) the production daemon logged about 30 "did not settle" DOMAIN_COMMAND_TIMEOUTs across agent, harness, lifecycle and pac-cleanup domains, against a baseline of 0–4 per day. At least one timed-out `agent.create` settled late and left a bare agent record (k3s-maintainer's wf-12a43416). Expansion adds child compilation and worktree effects on the same graph-writer and lifecycle paths. Keep C16's "no git under the writer" strictly, and make I5's e2e include an injected `DOMAIN_COMMAND_TIMEOUT` on child worker start, asserting no orphan launch receipt and a visible placeholder failure.

## Verdict

CHANGES. Resolve P1 (base and migration number relative to #987) and P2 (the timeout budget is required and registered by the integrator) in the dispatch briefs. Fold P3–P5 into the checks. The design itself stands.
