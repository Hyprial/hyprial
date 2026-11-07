# Homepage changelog sync (release record candidate)

`scripts/release/homepage_changelog.py publish` creates the Homepage release
record candidate, dispatches the trusted origin guard, and never writes a
commit status. This runbook covers the two rules that decide whether a release
can merge.

## Candidate: exactly one commit

The guard reads Forgejo's **compare** endpoint, which lists a changed file once
per commit (PR #166: 2 files over 4 commits came back as 8 entries), and
rejects any candidate whose file list is not exactly one newly added record.

- First run: one contents write on the candidate branch -> one commit.
- Retry or update (branch exists, open PR bytes changed): the branch is
  rewritten to a new commit whose only parent is the current `draft` head.
  The update is `git push --force-with-lease=refs/heads/<branch>:<old head>`;
  a branch that moved after the state read fails loudly and is never
  overwritten. The sync never appends a second commit.
- A branch-only retry whose bytes already match only opens the missing PR.
- Closed (unmerged) and merged-with-different-bytes candidates stay terminal.

The REST API on this Forgejo has no ref-update endpoint (its swagger exposes
only GET for git refs/commits/trees/blobs), which is why the rewrite uses git
directly. The candidate token travels in the child's `http.extraHeader`, never
in a URL or argv.

## Dispatch: the guard must speak for the candidate

- `sync` dispatches `homepage-release-origin-guard` (`workflow_dispatch` with
  `pr_number`, `head_sha`, `base_sha`) and does **not** read the guard's
  status. The merge check (`enable_status_check`) is what consumes it.
- Status is bound to a commit: every run dispatches with the latest head, so a
  new candidate head always gets a new guard run.
- There are exactly two suppression conditions:
  1. the diff touches `.forgejo/workflows/**` (new or previous path of a
     rename). The PR gate owns that row and emits failure, so the guard stays
     silent: one commit, one emitter.
  2. the `/pulls/{n}/files` list cannot be read, times out, or is a full page
     (50), so the dispatcher cannot certify that no workflow path is present.
- Everything else is dispatched.

The dispatcher reads `/pulls/{n}/files` (deduplicated). The guard reads
`compare` (repeated per commit); the two views intentionally differ and the
file list is never used to predict the guard's file count.

## When the guard reports `file-count`

Check the candidate branch's commit count first:

```bash
git fetch origin draft automation/homepage-release/<version>
git rev-list --count draft..automation/homepage-release/<version>
```

More than one commit means a candidate was appended to; that is the cause, not
the record bytes.

## Transition: when to drop the single-commit constraint

The constraint exists only because the guard counts `compare` entries. When
the Homepage repo changes the guard to count distinct paths or to read
`/pulls/{n}/files` (a separate Homepage PR), the single-commit rule can be
removed from this sync. Until then, keep it.

## Cross-repo references

- Gate: `HyprialOS/homepage` `.forgejo/workflows/homepage-release-gate.yml`
  (PR #166), filter `startsWith(".forgejo/workflows/")`.
- Guard tree: `HyprialOS/homepage` `ci/release-origin-guard` at `cd3e9a1`
  (`scripts/changelog-release-origin-guard.mjs`,
  `src/lib/changelog-origin.js`; `evaluateReleaseOrigin`).
