# PAC workflow delegation v1

The graph's origin daemon remains authoritative. A second daemon may execute a
workflow node for an explicitly borrowed local actor. The namespace is
`hyprial/v1/pac-workflow/<encoded-owner>/<encoded-machine>` with `request` and
`reply/<correlation-id>` leaves. Both sides must implement this protocol; an
older/unreachable peer fails remote admission before a graph is published.

This uses the repository's existing **trusted tailnet** model. It does not
establish independent cryptographic node identity or protect against a malicious
member of that mesh. The execution daemon verifies its caller using the normal
local actor/session binding. Origin-minted HMAC request capabilities bind the
exact origin, actor, graph, node, request, transitive inputs, fixed deadline,
message identity and task-body digest. They remain in daemon state and protocol
payloads; never include them in model text, public projections, logs or URLs.

## Operations

- `admit`: the actor's home daemon validates existence and dispatch role. No
  actor is started, adopted or stopped.
- `offer`: import a granted request, verify it is current with the origin, then
  submit its stable delivery identity through typed inbox IO. Replays retain
  one request and delivery. The receiver does not create a graph.
- `current`: the origin verifies the grant and checks graph openness, unflagged
  node, current request, inputs and deadline. This is a read, not completion.
- `inspect`: return the delegated node's authoritative projection. It is not a
  general remote graph reader or a progress-reader credential for other nodes.
- `outcome`: explicit `complete`/`fail` plus an evidence reference. The origin
  applies its existing PAC owner/request/input/deadline fence and records an
  atomic replay receipt. Changed grants or conflicting outcomes are refused.
- `expansion_preview`: a `pac-expansion-v1` grant may preview only the
  placeholder bound to that exact remote planner request. The origin loads its
  trusted policy and resolves the base commit; the execution daemon never
  creates the child graph or receives host filenames.
- `reset`: explicitly reset the delegated owner's own flag for rework. The
  reset is bound to the completed request and has its own atomic receipt; an
  old replay cannot reset a later round. Transport failure is reported to the
  caller for retry; resets are not queued as business completion.

Frames are capped at 256 KiB, incoming work at eight operations, and outstanding
calls at 32. Saturation and transport loss do not acknowledge an operation.
Business deadlines are never extended by transmission or retries.

Expansion admission declares `features: ["pac-expansion-v1"]`. A peer that
does not declare it is refused only for expansion-enabled planners; ordinary
remote tasks remain compatible. A remote completion durably binds
`expansion`, its canonical `expansionDigest`, and `attemptNo` to the outbox
row. The origin recomputes the digest and performs the same local preflight and
writer transaction. Only a definitive `PAC_EXPANSION_INVALID` rejection may
be corrected on the still-current request; that increments `attemptNo`.
Pending, accepted, stale, deadline, and other-error results are immutable, and
a late result from an earlier attempt cannot overwrite the current attempt.

## Public return contract

`hyprial workflow complete/fail` called on the execution machine authenticates
locally and resolves the exact imported graph/node/request. The result carries:

| Field | Meaning |
| --- | --- |
| `requestId` | The exact request whose outcome was submitted |
| `returnState: pending` | Durable local custody; the origin has not accepted it |
| `returnState: accepted` | The origin acknowledged its committed outcome |
| `returnState: rejected` | A persisted origin rejection, observable in inspect |

A synchronous rejected submission uses the normal IPC error boundary. Accepted flag writes retain the normal PAC `event` object. Human CLI output
labels pending writes as queued, rather than claiming a flag event. Exact
retries yield the original acceptance; a different action/evidence for the same
request raises `WORKFLOW_OUTCOME_CONFLICT`. Returning `pending` is not proof of
business completion. The durable queue retries **transmission only**, never
model execution. It survives restarting either daemon. The origin rejects an
expired or withdrawn request even if its outcome was queued while offline.

`workflow inspect GRAPH --node NODE` on the execution machine returns the origin
snapshot plus a `return` object containing this local state (`not-submitted`
before an outcome exists). When the origin is unreachable, `current` is null
and `authorityAvailable` is false. This is not interpreted as ready, withdrawn
or complete. Capabilities are never projected.

The origin snapshot also carries `roster` and `rosterDigest`. `roster` is the
same canonical JSON object embedded after `Workflow roster: ` in every task
brief for the graph: graph id, digest, and the stable node/role/owner/ownership
projection, with actor-node and actor URI only for graph-owned workers and
local/remote location when known. Roster membership grants no completion,
home-read, or lifecycle authority; the request, owner, deadline, and input
token remain the authorization fence.

For a terminal origin graph, status and inspect also carry `workerCleanup`.
`pending` means at least one receipt-owned worker has not been confirmed absent
with a durable `down/done` activation; `complete` means every receipt-owned
worker has that confirmation; `attention` means the last cleanup evaluation
failed (unavailable lifecycle manager, stop timeout, or an observed identity
that is not provably this actor's). `attention` is not terminal: the daemon
re-evaluates it on the fallback sweep with a fresh lifecycle operation id
(`<operationId>:retry:<n>`) and moves it to `complete` once the worker is
absent. A process is provably this actor's when its strict PAC marker names
this graph and actor node, or when it carries no marker and its agent entity
token equals the receipt's; any other identity keeps failing closed (an
activation without a marker proves nothing, and a NULL receipt token matches
nothing). A failed intent is re-admitted by the actor sweep only after
`min(5 min * 2^(attempts-1), 6 h)` since its last update; every failed
evaluation counts, including a reconcile that raises before writing any
outcome (only a `worker_in_use` hold counts none). That sweep runs every
30 seconds (running tier) and every 300 seconds (fallback tier); each sweep
keeps at most two cleanup retries in flight, counting retry jobs queued in
the actor service or queued/running on the graph authority's four effect
threads, and admits the oldest due ones into the free slots. The cap bounds
retries only: first cleanup evaluations (attempts 0, not throttled),
worktree cleanup (up to 16 per sweep) and expansion preflight use the same
effect threads uncounted, so open graphs are not guaranteed two free
threads. At deployment the burst comes from the first evaluation of the
closed graphs whose intents are backfilled (about 186 in production).
Attention reason `worker_in_use` means the agent is pinned to an adapter, or
has a registered interactive session that is online or whose presence cannot
be determined (missing, raising: fail closed); the signals come from the
agent record and hold after the worker spec is gone. Known limit: presence
that is wired but not yet ready (after a daemon restart or presence rebuild,
before the carrier heartbeats again) reports the session offline, so such a
worker may be stopped in that window; it is not deleted, because GC
re-checks under the destroy reservation. Cleanup does not stop such an agent,
`lastObservation.inUse` lists the signals (`pinned`, `interactive_online`),
and it resumes once released. PAC GC skips it as `worker-in-use`, and the
destroy recheck refuses it with reason `worker-in-use`; the GC pass report's
`skipped` histogram carries the reason any destroy recheck returned
(`worker-in-use`, `online`, `pinned`, `agent-incarnation-mismatch`), with
`destroy-noop` only when none is given. A closed graph whose receipt has no cleanup
intent gets one on its next reconcile. This field never grants the execution
daemon authority over a borrowed actor.

Bound-agent `workflow complete --expected-request` uses the same completion path;
`workflow reset` uses the explicit reset path. Graph-wide operations stay on the
origin. `user:` principals with no target machine are not remotely routable
execution actors; use an explicit actor URI for cross-machine borrowing.

Before a queued model turn, the execution daemon checks current authority. A
network failure retains queued work, while a definite withdrawal or deadline
prevents it entering the model. Already running borrowed actors are not stopped.
Late outputs cannot write a flag without passing the origin fence.

## Persistence and validation

Schema 12 adds imported bindings, the origin secret, the durable return queue
and outcome receipts. Schema 15 adds graph-owned worker receipts and the roster
projection; derivable older managed graphs are backfilled without inferring
ownership from actor names. Schema 16 adds terminal cleanup intents and permits
a confirmed receipt to move to `down`; existing terminal graphs are not
retroactively granted cleanup authority. Remote routine
occurrences use the same mechanism. Removing a routine closes its graphs without
obtaining stop authority over a borrowed remote actor.

Schema 18 adds expansion text/digest/attempt custody to the return queue. These
fields are archival, while the two pre-existing `result_json` columns retain
their existing owner-rewrite treatment.

Unit contracts: `tests/daemon/pac/workflows/delivery/test_pac_remote_workflow.py`, `tests/daemon/pac/authority/test_pac_fence.py`.
Two-daemon protocol gate: `contract/e2e/run-pac-cross-node.py`.
The regular E2E-015 runner executes both the local and cross-node gates.
