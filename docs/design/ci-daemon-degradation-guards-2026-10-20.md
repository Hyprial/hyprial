# CI guards for daemon degradation (2026-10-20)

## Decision

Add cheap, deterministic guards for the three 2026-10-19/20 failure classes:
unbounded settlement held all IPC slots, OrgFS work grew with history or ran
under an authority lock, and actor threads accumulated without useful CPU
attribution. These guards change no production behavior.

## Guard map

| Incident mechanism | Guard | Failure predicate | CI cost in this worktree |
| --- | --- | --- | --- |
| Event, queue, future, join, or lock waits had no deadline | `scripts/ci/check_unbounded_waits.py` | A new `.wait()`, zero-argument `.get()`, `.result()`, `.join()`, or `.acquire()` has no finite timeout; `block=True` without a timeout is also red | 1.38 s wall time for 741 files, including `uv` startup |
| All 64 IPC client slots stayed attached to domain settlement | `test_64_stuck_handlers_do_not_keep_the_65th_client_slot` | After 64 clients disconnect while their handlers remain stuck, a light request is not served below 2 s during GIL-burning bursts | 0.23 s |
| OrgFS writer work grew with document history | #1647's scaling rows, including `test_steady_state_commit_does_not_reapply_document_history` and `test_export_since_does_not_rescan_the_document_history` | A commit or sync-page read replays history-proportional bytes/decodes | part of the normal scaling suite; not duplicated here |
| Repeated directory/link/device publication created commits without a content change | `test_identical_directory_documents_create_no_tree_commits` | Rewriting the same three directory documents changes the commit recorder | below 0.01 s |
| Slow work ran while an OrgFS space authority lock was held | `test_space_authority_lock_hold_stays_below_250ms_during_list_write_mix` | Any measured hold exceeds 250 ms during a 25-write/list mix; the wrapper also proves `holder_alive` and waiter/holder counts return to zero | 0.01 s |
| Thread-per-actor ownership leaked after stop | `test_stopping_200_agent_actors_returns_threads_to_baseline` | After 200 explicit stops, more than two test-owned/background-race threads remain above baseline | 0.02 s |
| A hot owner was invisible in process CPU | Existing `test_runtime_cpu_accounting.py` coverage | Actor/effect thread CPU is not attributed, bounded in cardinality, and exposed in `ps.daemon.ipcStats.runtime` | already in the normal suite |

The newly added focused performance rows take less than one second of pytest
execution together (IPC plus actor rows: 0.28 s; no-op plus lock rows: 0.04 s).
The no-op and lock rows run in 0.88 s when grouped with #1647's reused
steady-state history row. They use the `perf_bound` marker but stay in the
normal suite because each new row is short. The older write-cost module
also contains an approximately 9 s O(space) test; that pre-existing cost is not
added by this design.

The static network-reachability check belongs to #1646 and is intentionally not
reimplemented here. The history-proportional writer fix and its larger scale
matrix belong to #1647. This branch keeps only the complementary wait, IPC-slot,
no-op publication, lock-hold, and actor-lifecycle guards.

## Static-gate policy

The scanner is standard-library AST and runs with the structural and module
boundary gates before dependency installation. PR merge-tree hygiene invokes
the same script, so a head/base composition cannot bypass it.

The initial baseline contains 68 exact `(path, line, method)` rows from `dev`.
It is an only-out ratchet:

- a new or moved finding is red;
- a repaired finding leaves a stale baseline row, which is red until deleted;
- malformed source or policy JSON fails closed;
- `mapping.get(key)` and explicit nonblocking calls are not classified as
  blocking queue reads.

A deliberate exception is allowed only in the first 20 lines of its source
file:

```python
# hyprial-unbounded-wait-allow: join - shutdown closed every producer first
```

The reason is mandatory. Inline suppression is ignored. An unused header entry
is stale and red. The baseline records migration debt; the header is only for a
reviewed invariant that intentionally remains unbounded. Current headers cover
the process-lifetime Org-network worker queue and the locally bounded
`BoundedProjection.result` wrapper introduced by #1659.

## Red evidence and strict-xfail transition

The static-gate fixture suite contains pre-fix calls for every recognized
method and proves that bounded/nonblocking forms stay green. The 64-slot test
is the regression test introduced before the fix in commits `c589b866e` and
`b8df0d78f`; it is the pytest adaptation of the e2e-verifier's composed
peer-hangup/65th-client probe at SHA-256
`4befd3265fc6ed4cc9b1e9944718ad8c2c4a130bfe0ead1b2b8b78d78d818ef0`.
It keeps all 64 handlers blocked, closes all peers, proves every transport slot
is released before the request deadline, and serves the 65th request.

Strict xfail is a temporary red ledger, not a pass. A red row for an unmerged
fix carries `xfail(strict=True, reason="<workflow>: ...")`; once the fix makes
the assertion green, pytest reports `XPASS(strict)` as a failure, so the fixing
PR must remove the xfail and leave a normal hard assertion. #1647 landed before
this branch's dev merge, so its former `wf-75cc1d53` history row is now a hard
assertion owned by the #1647 scaling files rather than a duplicate xfail here.

`wf-ce00902c` and `wf-557571b4` do not need xfail rows on this baseline because
their focused assertions are green. The no-op, lock-hold, IPC, and
actor-lifecycle rows are hard assertions; a skip is never accepted as evidence.

## Deployment acceptance

CI bounds isolated mechanisms; it does not prove a deployed daemon is healthy.
The `hyprial-e2e` skill therefore adds a read-only saturation check. It requires
all of these observations after deploy:

1. `ps --json` reports `daemon.ipcClients.active < limit` and
   `saturated=false`.
2. top-level `storageHealth.threadCount` has no monotonic growth across repeated
   samples; its scope is `python-managed`.
3. Five consecutive `org list --json` calls each complete below two seconds.

An active count equal to the limit, a rising thread trend, any two-second
sample, a timeout, or an unavailable field is `FAIL`/`BLOCKED`, never PASS.
