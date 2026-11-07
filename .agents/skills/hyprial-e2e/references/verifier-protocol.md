# E2E verifier protocol

The `e2e-verifier` Forgejo identity is the release-gate reviewer for Harness
Bridge. It is not an implementation author or a general automation account.

## Responsibilities

For each candidate pull request, the verifier:

1. runs the required E2E gate against the exact reviewed commit;
2. posts a sanitized evidence matrix on the pull request, including the tested
   SHA, preflight status, command, duration, final `RESULT` line,
   classification, visible side effects, cleanup status, and evidence
   reference for every required row;
3. submits `APPROVED` only when every required row has current passing
   evidence and all skips or accepted `BLOCKED` rows are explicitly accepted
   by the release owner; or
4. submits `REQUEST_CHANGES` with the failing or blocked matrix rows and the
   smallest reproducible evidence.

Connectivity checks and partial runs may use `COMMENT`, but they must never be
reported as release approval.

## Attended real-environment watch procedure

Treat the required `[REAL ENV]` scenario rows (E2E-001 attended two-host form,
E2E-004, E2E-006, E2E-012, E2E-013, and E2E-014) as attended release-gate
rows, not background smoke tests. E2E-011's automated seam stops at the
verification URL; if the release owner requires real App creation, its human
approval boundary must be attended and reported explicitly. E2E-008's
controlled `--attended --smoke` row is explicitly non-gating; its full attended
flow is manual diagnostic/forensic evidence and must not be presented as a
controlled release-gate pass.
The verifier must check out the exact PR head, complete the tooling, isolated
home, and account preflight in the parent skill, pin `HYPRIAL_BIN` to the
checkout's own build, warn the dedicated Feishu test chat before E2E-004, and
stay with each runner from its preflight lines through its final `RESULT`
line.

During a watched run, do not restart a provider manually, inject input into a
test-owned TUI session, or repair the runner's disposable state; those actions
invalidate the evidence and the row must be rerun. A runner that reports
`BLOCKED` preconditions is recorded as `BLOCKED` with the missing
prerequisite; it is never upgraded to a pass or downgraded to a warning.

The evidence comment must record the reviewed SHA, each scenario ID with its
command and final `RESULT` line, per-assertion classifications where the
runner emits them, the visible Feishu side effects and their markers, and the
cleanup result including the Docker leak self-check for E2E-001. Any leaked
daemon, adapter, container, tmux session, or route is `REQUEST_CHANGES`, not
a partial pass.

## API authentication

Use only the verifier's repository-local token file. It must be ignored by
Git, owned by the local user, and mode `600`:

```bash
set +x
REPO_ROOT="$(git rev-parse --show-toplevel)"
VERIFIER_TOKEN_FILE="$REPO_ROOT/.agents/skills/forgejo-ops/.e2e-verifier-token"
test -f "$VERIFIER_TOKEN_FILE"
test "$(stat -f '%Lp' "$VERIFIER_TOKEN_FILE")" = "600"
VERIFIER_TOKEN="$(cat "$VERIFIER_TOKEN_FILE")"
test -n "$VERIFIER_TOKEN"
FORGEJO_API="https://code.hyprial.com/api/v1"
FORGEJO_OWNER="HyprialOS"
FORGEJO_REPO="$(basename -s .git "$(git remote get-url origin)")"
```

Deriving `FORGEJO_REPO` from the checkout's `origin` keeps the calls pointed
at the repository actually under review. Never print the token, enable shell
tracing, use verbose HTTP output, or commit the token file. Unset
`VERIFIER_TOKEN` as soon as the calls are complete.

## Pull request and review templates

Read the pull request and record its exact head SHA before running the gate:

```bash
PR_INDEX="18"
curl --fail-with-body --silent --show-error \
  --header "Authorization: token ${VERIFIER_TOKEN}" \
  "$FORGEJO_API/repos/$FORGEJO_OWNER/$FORGEJO_REPO/pulls/$PR_INDEX"
```

Post a non-verdict evidence comment while a run is incomplete:

```bash
REVIEW_BODY="E2E evidence matrix: <sanitized matrix and evidence references>"
jq -n --arg body "$REVIEW_BODY" \
  '{body: $body, event: "COMMENT"}' | \
curl --fail-with-body --silent --show-error \
  --request POST \
  --header "Authorization: token ${VERIFIER_TOKEN}" \
  --header "Content-Type: application/json" \
  --data-binary @- \
  "$FORGEJO_API/repos/$FORGEJO_OWNER/$FORGEJO_REPO/pulls/$PR_INDEX/reviews"
```

After the evidence matrix is complete, use the same endpoint with event
`APPROVED` for a passing gate or `REQUEST_CHANGES` for a failing or blocked
gate. Include the tested head SHA and evidence matrix in `body`. Inspect the
returned review state and URL before reporting success, then run:

```bash
unset VERIFIER_TOKEN
```

## Author and reviewer separation

The implementation author must not authenticate as `e2e-verifier`, run the
verifier decision on their own behalf, or approve their own pull request. The
verifier must independently read the PR head, execute or validate the gate,
and publish its own evidence. If Forgejo identifies the verifier as the PR
author, or the evidence was produced only by the author without independent
verifier execution, the verifier must not approve; report the separation
failure as a blocked release gate.
