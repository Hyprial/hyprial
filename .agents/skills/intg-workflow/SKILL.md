---
name: intg-workflow
description: Integration-branch development flow (intg/** → dev). Use when a plan is agreed and implementation must be split across several PRs or several agents/workers. Covers opening the WIP integration PR, landing implementation PRs into intg/**, removing WIP as the owner, and handing to 质检部 for the merge. Not for single-file fixes that can go straight to dev with one PR.
---

# Integration-branch workflow (`intg/**` → `dev`)

A feature is one **container**, not a set of unrelated PRs. The container is a branch plus the
WIP pull request that points it at `dev`. Everyone working on the feature targets the container;
only the container's owner talks to `dev`.

## The five rules

1. **Open the container when the plan is agreed, not when the code is written.**
2. **`intg/<topic>` owns the feature.** Its owner is the agent that opened the WIP PR. Only that
   owner removes WIP.
3. **Implementation PRs go into `intg/<topic>`, never into `dev`.**
4. **Remove WIP only when the criteria are green on the `intg` head**, then hand to 质检部. The
   owner never merges their own work.
5. **A container that dies is closed with a reason.** Never silently.

Rule 4 says *on the `intg` head*, not *on each work branch*. That distinction is the whole point of
the container — see "Gates belong to the container" below.

## 1. Open the container

```bash
git fetch origin dev
git checkout -b intg/<topic> origin/dev
# land at least one commit first: a PR needs a diff to exist
git commit --allow-empty -m "wip(intg): open intg/<topic> for <feature>"
git push -u origin intg/<topic>

tea pr create --repo <owner>/<repo> --base dev --head intg/<topic> --draft \
  --title "WIP: <feature> — integration branch (do not merge until every WP is done)" \
  --description-file <description-file>
```

`--draft` prepends `WIP: ` to the title in Forgejo, which is what the flow keys on.

**Why a commit before the PR:** a branch identical to `dev` has no commits between base and head,
so the server refuses to create the PR. The first commit is usually the plan or the spec.

**Why open it before the code:** the container is what answers "is this feature finished?". Opening
it late means nobody can see the feature as a whole, and the spec PR starts impersonating the
container.

## 2. Land implementation PRs into the container

```bash
tea pr create --repo <owner>/<repo> --base intg/<topic> --head <work-branch> \
  --title "<type>(<area>): <what>" --description-file <description-file>
```

- One PR per work package, split by **kind of change** (identity / storage / CLI / transfer …),
  not by who found the problem.
- Targeting `intg/<topic>` is also what makes the CI routing work: PRs into `intg/*` run that
  component's suite (see `docs/reference/ci-integration-routing.md`).
- **Write the red evidence into the WP's PR body**: which job, which test name, and the failing
  reading. A red that only exists in a chat message is a memory, and the next reader cannot check it.

## 3. Gates belong to the container

Each work package can be green on its own branch while the container is red. Three shapes of that,
all observed:

| Shape | Example | Consequence |
|---|---|---|
| Gate data lives in a file the diff doesn't touch | an identity gate reads a hostname allowlist in another file; the WP's own tree is still consistent | branch green, `intg` head red the moment it lands |
| Two green PRs sum past a limit | two PRs each keep a file under 1000 lines; merged, it is 1050 | neither baseline could see it |
| Gate runner is an aggregator, not a test | `pytest-shard (N)` / `unit` jobs carry different tests under the same name | "same job is red on dev" is not a baseline |

So: **run the gates once on the `intg` head after the last merge**, and judge CI **by test name**,
never by job name.

## 4. Remove WIP and hand over

Conditions — all of them:

- every work package's acceptance items are green **on the `intg` head**, and each was red before
  the fix, with the red evidence in that WP's PR body;
- the spec's own criteria (not the tests) are satisfied;
- no work package is still marked pending in its work item;
- for a WP that fixes a red already on `dev`, the criterion is **the named red turning green**, not
  "CI has no red" — a red baseline makes "all green" unreachable for that WP;
- the merged artifact is the verified artifact: `merge-base(dev, intg) == dev` so the merge is a
  fast-forward and `merge_commit_sha == intg head`. Otherwise merge-forward first and verify the
  merged head, never the old one.

```bash
tea pulls edit <wip-pr> --repo <owner>/<repo> --title "<feature> — integration branch (ready for review)"
# then: hand to 质检部 (e2e-verifier) for the merge
```

The owner does not merge. Executor and acceptor must be different parties; a self-verified merge is
a self-certified one. If the container's owner *is* a QA member, do not add an exception —
**the verdict is an artifact, and a third party executes the merge**. Who may merge is a permission
question and can be given away; who has looked is an epistemic question and cannot.

## 5. Merge-time friction (not a routing conflict)

Parallel PRs into one `intg` branch do **not** cancel each other (the `pull_request` concurrency
group includes the PR number), but three other things happen:

1. **No base-freshness gate (removed 2026-11-04).** The `base-freshness` rule used to require
   `origin/<base>` to be an ancestor of an `intg/*` PR, so merging one WP turned every other open WP
   red (N parallel WPs gave N−1 "unexplained" reds). It was removed on Allen's instruction.
   A sibling that is not up to date is no longer red for that reason; merge-forward only when the
   merge itself conflicts or the merge-tree gate says so.
2. **`intg` branch push runs cancel each other.** The `push` concurrency group is keyed on the ref,
   so two pushes to `intg/<topic>` share a group and `cancel-in-progress` kills the first. The
   newest green on the container head may belong to another commit. → **After the last WP, run once
   more on the container head** (the same action as rule 3).
3. **Real file collisions between sibling WPs.** Two WPs touching one subsystem conflict for real
   (and a conflict resolved by hand can silently loosen an admission — the seam is often a fencing
   boundary). → **Order the merges by the size of the file intersection, largest first.**

## 6. Failure path

If the container is abandoned:

```bash
tea pr close <wip-pr> --repo <owner>/<repo>
# and state, in the close comment or the PR body: which work packages landed, which did not, why
```

A closed PR without a reason gets redone by the next agent, from scratch.

## Pitfalls that cost real time

| Pitfall | What happens | Do instead |
|---|---|---|
| Trying to change a PR's base after the fact | `tea` has no `--base` on `edit`; you have to close and reopen, which changes the number | decide base before opening; if you must retarget, close the old one and say so in the new one |
| Opening the container only when the branch has work | server refuses: no commits between base and head | land the plan as the first commit |
| Reporting "green" from a branch | CI is a function of `(sha, event)`: the same head can be green on `pull_request` and red on `push`, and a baseline that was red an hour ago can be green now | report **branch + sha + event**, and verify with `git show <sha>:<path>` |
| Reporting "done" from the working tree | a shared checkout is rewritten by whoever checks out next; the reader greps two different contents | report **branch + sha** |
| Merging your own green branch | nobody independent checked it | hand to 质检部 |
| Big-bang integration | the container accumulates unrelated work and can never be reviewed | one work package per PR, merged as each finishes |

## What the owner owes the reader

Every claim about the container carries a coordinate:

```
branch   intg/<topic>
sha      <full sha of the commit you looked at>
event    push | pull_request          (CI results are not comparable across events)
file     git show <sha>:<path> | <the check that prints the expected line>
```

"I updated the spec" without branch and sha is not a report; it is a memory.

## Work items

Each work package gets one work item in the org space; keep the checklist decidable:

```
hyprial org work add "<feature>: <work package>" --org <org> \
  --owner <agent-uri> --assignee <agent-uri> \
  --item "<one acceptance item that can be true or false>" --acceptance "<the feature-level gate>"
hyprial org work check <item-id> <n> --org <org>     # tick only a decidable item
hyprial org work note  <item-id> "<state + reading + epoch>" --org <org>
```

- A checklist item must be decidable true/false; if it can be done halfway, split it when it is
  created, not afterwards in a log.
- A conjunction ("reads the store **and** writes the event") is one item with two tests, not two
  items — splitting it yields two green ticks while the requirement goes unverified.

## Provenance

Rules 3–5 and the friction list were reviewed by `e2e-verifier` at `epoch 1792734600`; every
condition in them cites an observed instance from 2026-10-23 (see PR #1871). The
"executor ≠ acceptor" rule is not a discipline but a description of how I fail: four attribution
errors that night, four caught by the other party, none by me.
