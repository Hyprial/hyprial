# Dev PR source rule and rollout

Allen, 2026-10-31: “是的，所有功能分支先进 release 集成分支，dev 只接受 release 和 hotfix”.

Feature PRs target `release/v<next>`. After integration, promote the release
branch to `dev`. `hotfix/<slug>` may also target `dev`; renaming a feature branch
as a hotfix is not an integration substitute.

Rule 5 in `scripts/ci/check_pr_hygiene.py` checks branch names without git or
network access:

```sh
uv run --no-project --python 3.12 python scripts/ci/check_pr_hygiene.py \
  source-branch --base-ref dev --head-ref release/v0.5.5.3
```

For `dev`, accepted names are `release/v` followed by three or four dot-separated
numeric components, or `hotfix/` followed by an ASCII alphanumeric character
and zero or more ASCII alphanumeric characters, hyphens or underscores.
The match covers the full branch name. Rejected sources receive the ruling and
`retarget to release/vX.Y.Z`. Other bases, including an empty base on push events,
return success with an explicit `not applicable` message. This rule does not
approve merges or replace tests and review.

## Two-stage rollout

The `pr-hygiene` job overwrites its gate script with the PR base's copy before
running it. Preserve this trust boundary: a PR cannot supply its own gate.
Adding a new subcommand and its workflow invocation in one PR would fail against
a base that lacks the subcommand, even when the new rule would be not applicable.

The dispatcher approved this rollout on 2026-10-31:

1. Land `ci/dev-source-branch-rule`: script, rule tests, skills and this note.
   The dispatcher will request a bootstrap PR into `dev` before the workflow
   invokes the new rule. No branch-protection bypass is authorized by this note.
2. Land `ci/dev-source-branch-rule-step`, based on stage 1: the workflow invocation
   and its wiring test. The intended PR base must already contain stage 1.
   For a normal feature PR through `release/v<next>`, integrate stage 1 into that
   release base first. Allen merges the workflow change.
3. Verify the real PR job and required-check configuration on `dev`. Local tests
   do not establish that branch protection requires the job.

The workflow passes base/head refs through step-level environment variables and
quoted shell arguments. Do not interpolate PR branch names into shell source,
fall back to the candidate's script, or silently ignore an unknown subcommand.

## Implementation checks (2026-10-31)

- Red first: the new 17-case CLI matrix and workflow wiring test failed before
  implementation (18 failures, missing subcommand/step).
- Stage 1 alone: the focused rules/git suite **PASS**, 72 tests; all
  `tests/tooling/ci` **FAIL**, 612 passed and the same 2 baseline failures below.
- Combined candidate: `uv run pytest -q
  tests/tooling/ci/hygiene/test_pr_hygiene_rules.py
  tests/tooling/ci/hygiene/test_pr_hygiene_git.py`: **PASS**, 73 tests.
- `uv run pytest -q tests/tooling/ci`: **FAIL**, 613 passed and 2 failed. Both
  failures reproduce in a separate detached checkout of baseline `df9d50502`:
  `test_real_tree_denominator_and_family_witnesses` and
  `test_every_runs_on_label_is_registered` in
  `tests/tooling/ci/acquisition/test_ci_dependency_caches.py`. The failures concern
  existing release-ci runner/image registration, not the source-branch rule.
- `uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py`:
  **PASS** before and after changes.
- `uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py`:
  **PASS** before and after changes.
- `uv run ruff check .`: **PASS**.
- Real Forgejo execution and required-check activation: **UNKNOWN**, not deployed.

PR declarations: `Skill-Impact: updated`.
`Contract-Impact: CI-only branch admission; no published product fields change.`
