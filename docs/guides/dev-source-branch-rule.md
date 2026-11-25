# Dev-first PR targeting

Allen, 2026-11-08: completed changes enter `dev` first; releases select a tested
commit from dev history. This replaces the 2026-10-31 release-first source rule.

The machine-readable policy is [`.forgejo/git-flow.json`](../../.forgejo/git-flow.json).
It defines the integration branch, unrestricted PR source names, Draft WIP,
release preparation, candidate identity and track roles. `ci-plan.mjs` reads and
validates this file from the checked-out tree before producing a plan; missing
or unsupported policy fails planning rather than falling back to release-first.

Feature, fix, docs, ci, hotfix and integration branches may all target `dev`.
WIP PRs remain Draft after retargeting. Review, merge-tree composition, registry
preservation, declarations and required checks still decide whether a PR can
merge; a branch name is not evidence of readiness.

`gate-python.yml` and `gate-gui.yml` use that common planner. Every PR's complete
base-to-head diff selects its GUI and sidecar suites, including ordinary
`feat/*` and `fix/*` sources and changes earlier than the latest commit. A mixed
PR selects both component suites. Dev pushes retain full Python validation and
also select changed component suites against the merged tree. Existing exact
checkout, comparison-history, rename/deletion and unknown-input protections
remain. Optional integration lanes may request extra suites; their names cannot
suppress a suite selected by a changed component path.

Release-preparation sources also receive the same path-based Rust audit; the
retired release-sync exemption is removed. The legacy release-target changelog
job remains compatible with old PRs. New dev PRs share its not-applicable result
regardless of source name, while formal release-tag validation requires the
selected candidate's matching VERSION and CHANGELOG section.

The old `source-branch` command remains as an explicit no-op compatibility shim
because existing PR workflow revisions may still invoke the base branch's gate
script. It reports that release-first admission is retired. Active workflows
must not invoke it, and the tests reject reintroducing that step. The hygiene
script still comes from the base ref; this trust boundary is unchanged.

During one-time convergence, merge existing release and main history into dev
without dropping their features or fixes. Keep WIP integration lines outside the
candidate. Formal release selection, temporary publication holds and track
writers are defined only in [release rules](release-rules.md).

`release/v<version>` may collect a preparation WIP targeting dev: release notes,
upgrade instructions and validation fixes enter dev through review. It does not
become a parallel feature lane. The version tag selects a verified full SHA
from dev history, which need not be the latest dev head. Changing the flow
config does not itself enable publication; the current three workflow holds
remain frozen and require a separate reviewed change.
