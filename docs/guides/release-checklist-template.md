# hyprial release gate template

Use a candidate-specific copy of this checklist in the release-preparation PR.
Do not check off this source template in place. Every result must name the exact
candidate commit; evidence from another SHA is not release evidence.

This checklist records evidence. It does not authorize a merge, tag, release,
shared-machine install, daemon restart, production credential use, or backup
removal. Obtain the approvals in section 7 immediately before those actions.

## Candidate record

- Release version: `<PEP 440 version>`
- Release tag: `v<PEP 440 version>`
- Previous supported tag: `<tag>`
- Candidate commit: `<full SHA>`
- Release class: `<development / prerelease / final>`
- Coordinator: `<owner>`
- Evidence index: `<durable log or PR comment>`
- Rollback point: `<tag, commit, state backup>`

## 1. Freeze the release scope

- [ ] Record the exact candidate SHA and confirm it is an ancestor of the
  protected `dev` branch.
- [ ] Derive the included change set from Git between the previous supported
  tag and the candidate. Do not reconstruct scope from memory, WIP branches,
  or issue labels.
- [ ] Close or explicitly defer every release-blocking defect. A design note,
  local fix, narrow passing test, or queued workflow is not completion.
- [ ] Classify every identity, keyspace, wire, persisted-state, migration,
  upgrade, and daemon-lifecycle change. Record whether mixed-version nodes and
  the supported rollback version remain compatible.
- [ ] Update `CHANGELOG.md` with a new version section (Keep a Changelog:
  newest-first, dated, Added/Changed/Fixed/Removed) covering the exact included
  changes, compatibility boundary, migrations, known limitations, and gate
  evidence. Write the long-form single-version release notes to
  `docs/notes/changelog/CHANGELOG-v<version>.md` and link to that file from the
  `CHANGELOG.md` version section — the per-version file carries the narrative,
  `CHANGELOG.md` stays the summary index.
- [ ] Run `node scripts/ci/check-changelog.mjs --tag v<version>` at the candidate.
  The tag must equal `v` + `VERSION`; the exact `## [<version>]` section must
  contain a non-empty bullet. Empty headings, examples in code blocks, and
  prefix matches do not count. `release-tag` checks the target SHA before tag
  creation; `github-publish` checks before building/publishing a formal release.
  The track-only wheel lane still rejects `v*` and has a defensive pre-build
  check. Movable `internal`/`nightly`/`stable` tracks are unchanged.
- [ ] Set the PEP 440 version once with `uv version <version> --no-sync`.
  The declared form is exactly `X.Y.Z` — development `.devN` suffixes are
  retired and version bumps are developer-manual only.  The `+g<short7>`
  local segment exists only on built wheels (stamped by
  `scripts/build/build-wheel.sh`), never in `pyproject.toml`.
  Confirm `pyproject.toml`, the regenerated `uv.lock`, installed distribution
  metadata, and runtime `hyprial.__version__` agree; do not add a second handwritten
  version constant.
- [ ] Confirm the proposed tag is a valid tag-only upgrade candidate and does
  not already exist. Existing release tags are immutable and must not move.
  The only movable tag refs are `internal`, `nightly`, and `stable`.
  Do not move them by hand. `internal` follows dev without a separate version
  bump. The formal v* path in `github-publish.yml::publish` is the sole stable
  writer, after CHANGELOG/VERSION and publication gates. It also applies the
  nightly stable floor; nightly-promote retains its same-SHA, fresh double-green
  checks and applies the same floor even without a green candidate.
  Neither path may roll a newer nightly back. See Allen's 2026-10-26 ruling in
  `docs/guides/upgrade-tracks.md`.
- [ ] Confirm the diff contains no unrelated release-time cleanup or
  opportunistic feature work.

## 2. Prove the exact candidate locally

Run from a clean detached checkout of the candidate. Keep `HYPRIAL_HOME`,
`HARNESS_STATE_DIR`, uv tool directories, sockets, and daemon processes in a
candidate-owned temporary root. Do not let ambient `HYPRIAL_BIN`, `HYPRIAL_PYTHON`, or
`VIRTUAL_ENV` select a different installation.

- [ ] `uv lock --check` passes before any command that could update the lock.
- [ ] `uv sync --locked --python 3.12 --extra test` succeeds from the locked
  dependency set.
- [ ] `uv run --locked --python 3.12 --extra test pytest tests/ -q` passes with no
  exclusions that differ from `.forgejo/workflows/tests.yml`.
- [ ] The CI-equivalent Linux container command documented in
  `.forgejo/workflows/README.md` passes.
- [ ] `env -u HYPRIAL_BIN -u HYPRIAL_PYTHON -u VIRTUAL_ENV ./contract/run-all.sh --isolated-only`
  passes.
- [ ] `uv run --locked --python 3.12 python contract/e2e/validate.py` reports
  every documented scenario and standalone runner.
- [ ] `uv run --locked --python 3.12 python -m compileall -q src tests contract` and
  `git diff --check` pass.
- [ ] `uv build` produces both the wheel and sdist.
- [ ] Install the built wheel into a fresh isolated environment. Verify imports,
  the `hyprial` entry point, installed/sealed CLI contracts, and `hyprial version --json`.
- [ ] The installed package, runtime `__version__`, requested tag, resolved tag,
  resolved commit, and candidate SHA all identify this release. A correct tag
  with a stale runtime-reported version fails the gate.
- [ ] The checkout remains clean and teardown leaves no candidate-owned daemon,
  harness child, socket, tool environment, or temporary state behind.

For every command, record the SHA, command, result counts, duration, platform,
tool versions, and evidence-log location. Separate measured results from
inference and unverified environmental explanations.

## 3. Require real Forgejo CI evidence

- [ ] An authorized `ubuntu-latest` runner and, when required, a `macos-host`
  runner are online with the labels used by the repository workflows.
- [ ] `.forgejo/workflows/tests.yml` checks out and passes the exact candidate
  SHA. `waiting`, `running`, skipped, cancelled, or a run from another SHA is
  not a pass.
- [ ] The manually triggered or scheduled `.forgejo/workflows/e2e.yml` run
  checks out the exact candidate SHA and passes its full isolated macOS set.
- [ ] Job logs retain the candidate SHA, environment diagnostics, commands,
  result counts, and failure output. This repository does not treat a missing
  artifact as success; the workflow job log is the unit-test record.
- [ ] Any platform-specific failure is fixed or remains an explicit failed
  gate. Do not relabel it as environmental without a reproducer and a recorded
  release decision.

## 4. Run the release E2E boundary

The scenario inventory in `contract/e2e/scenarios.json` is the source
of truth. Run every isolated gate plus every real-environment scenario touched
by the release. A final release requires attended evidence for all supported
real-environment paths; `BLOCKED` is a disclosed release risk, not `PASS`.

- [ ] The isolated macOS set passes: daemon restart, pin survival, tag install /
  upgrade, adapter restore, and running-deployment upgrade continuity.
- [ ] Upgrade continuity starts at the previous supported tag and reaches the
  exact candidate, proving automatic daemon restart, fresh PID/daemon epoch,
  owner fence, session re-registration, FIFO, no loss, and no replay.
- [ ] Network, addressing, identity, registration, or transport changes run the
  attended two-machine roundtrip and their dedicated reconnect/regression
  runners, including daemon restart and post-reconnect delivery.
- [ ] Lark-facing changes run the real Feishu roundtrip and the applicable App
  onboarding, scope, media, rich-message, adapter-restore, and history-recovery
  paths.
- [ ] Harness, Agent, model, or lifecycle changes run the applicable resident
  Claude/Pi/Codex, Claude TUI, Codex app-server, Squire setup, and all-harness
  lifecycle scenarios.
- [ ] Every production-defect fix includes a red-to-green regression and a
  release-boundary reproduction of the original failure mode where practical.
- [ ] Long-running liveness/resource defects run past the observed failure
  window with relevant FD, thread, process, socket, registration, and log
  telemetry retained.
- [ ] Test identities, real credentials, production Apps, cleanup, and restart
  scope were approved before the attended run; no unattended gate uses
  production credentials.

## 5. Audit compatibility, migration, and rollback

- [ ] Test a copy of representative deployed state, including desired state,
  `agents.sqlite3`, `adapters.sqlite3`, secrets references, legacy settings,
  logs, and any release-specific migration input.
- [ ] Every migration is deterministic, idempotent, interruption-safe, and
  fail-loud on ambiguous or unsupported input. Backups are created before the
  first irreversible write and exclude secret material from reports.
- [ ] The supported rollback binary either reads candidate-written state safely
  or refuses via an explicit guard before modifying it. Rollback must not
  silently erase required state or downgrade canonical identity.
- [ ] Mixed-version hosts preserve addressing, registration, discovery,
  delivery, and terminal-state semantics. If they cannot, use a coordinated
  all-node window with owner notification and per-node verification; do not
  describe the release as rolling-upgrade compatible.
- [ ] Exercise upgrade failure after package replacement and after automatic
  restart. Confirm `upgradeCompleted`, restart failure, resolved tag/commit,
  running PID/version, and recovery instructions remain observable.
- [ ] Test rollback and a second upgrade from the rollback point on disposable
  state. Record what is restored, retained, archived, or intentionally lost.
- [ ] `hyprial version --json` on the fresh install and running daemon reports the
  intended package/local version, source, tag, and commit with no retired
  update-track behavior affecting selection.

## 6. Prepare publication and rollout

- [ ] The reviewed release-preparation commit is immutable and all evidence
  refers to it.
- [ ] Release notes include upgrade commands, automatic-restart behavior,
  compatibility/migration boundaries, rollback instructions, known risks, and
  any coordinated-window requirement.
- [ ] The rollout order, canary node, owner notifications, maintenance window,
  health checks, stop conditions, and rollback owner are recorded.
- [ ] Backups and rollback commands were tested on disposable paths and have
  explicit, validated targets. No cleanup command relies on an unresolved glob
  or broad home/workspace path.
- [ ] The proposed annotated tag and Forgejo release object will point to the
  same reviewed commit. With tag-only upgrades, creating that tag is the
  publication decision.

## 7. Obtain approval for irreversible or shared actions

Immediately before each applicable action, report the candidate SHA, evidence
matrix, remaining risks, automatic side effects, and rollback point, then obtain
explicit approval for that action:

- [ ] merge the release-preparation PR;
- [ ] create the release tag;
- [ ] create the Forgejo release object;
- [ ] run `hyprial daemon upgrade` on a shared or production machine (it may automatically
  restart a running daemon unless `--no-restart` is explicitly selected);
- [ ] restart a shared daemon, adapter, or harness outside the approved upgrade;
- [ ] use real credentials or production Apps in an attended gate;
- [ ] remove backups, archived state, or the rollback installation.

Approval for one action does not authorize the later actions in this list.

## 8. Verify publication and rollout

- [ ] Verify the remote tag and its peeled commit independently after creation.
- [ ] Verify the Forgejo release object points to the same tag and commit.
- [ ] Run `uv run python scripts/ci/check-release-tracks.py <version>` from the
  source checkout. Retain all three remote track commits and the exit code.
  Require stable == the v* commit and nightly == that commit or a descendant.
  This check also runs in the release job and must fail if either track is stale.
  In particular, v0.5.5.1 must reach both stable and nightly; the 10-26 manual
  v0.5.5 catch-up is not evidence for v0.5.5.1.
- [ ] Verify the dispatched stable/nightly GitHub publications completed and
  contain the intended version. Public snapshot commits differ from source
  commits; source track readback alone is not public publication acceptance.
- [ ] Install from the canonical repository in a fresh environment and confirm
  `hyprial version --json` resolves the published tag and commit.
- [ ] Upgrade the canary, verify the new daemon PID/version, registrations,
  adapters, Agent directory, mesh links, message roundtrip, and logs, then
  continue only if the recorded stop conditions remain clear.
- [ ] Verify every upgraded node and the required cross-node paths; do not infer
  fleet health from one node.
- [ ] Retain the rollback point and evidence for the announced observation
  window, then publish the final outcome and any deferred risks.
