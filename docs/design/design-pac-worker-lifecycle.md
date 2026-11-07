# PAC worker lifecycle, collection, log retention, and restart restore

Status: accepted by Allen (2026-09-27), with the worker state model added. No code has changed yet. The
implementation baseline is dev `5f09e08a` plus the agent-activity/keep-list work
from PR #879 (merged into dev on 2026-09-27; not yet in the running production daemon); the measurements below were
taken read-only on 2026-09-27.

## Current state

### A workflow describes all workers, but starts them lazily

`src/hyprial/pac/workflow_schema.py:load_workflow_text` validates at most 100
task nodes. A node with an explicit `owner` is borrowed; the schema forbids it
from also declaring `worker` or `launch`. Every other executable node refers to
a workflow-owned worker, implicitly using its node id or explicitly sharing a
named entry from `workers`.

`src/hyprial/pac/workflow_graph.py:compile_workflow` atomically creates the PAC
graph and all actor-node records before external effects. For each distinct
owned worker it creates `_actor.<worker-key>`, stores the immutable launch
artifact, and derives the local actor name as:

```
wf-<32-hex graph id>-<first 12 hex of SHA-256(worker key)>
```

The actor node is a predecessor of every executable node assigned to that
worker. This establishes the graph's complete node-to-actor map, but it does not
create an agent or start a process.

The process is lazy. `src/hyprial/pac/lifecycle.py:ActorCoordinator.reconcile`
creates an `actor_activations` row only if the graph is open and `_ready`
returns true. For managed workflows, `_ready` calls
`src/hyprial/pac/workflow_graph.py:actor_ready`, which returns true when any
assigned pending/requested step has all non-actor inputs satisfied and has not
expired. `ActorCoordinator._reconcile_up` then resolves the launch and calls the
daemon's ordinary lifecycle start port. A downstream B worker in A -> B is
therefore only created after A completes; B does not exist as an agent or
process at initial dispatch.

### What one started workflow worker leaves behind

The lifecycle start is not just a process. It spans these durable and runtime
objects:

| Object | Current location and owner | Current end-of-graph behavior |
| --- | --- | --- |
| PAC ownership and activation | `state/pac-graph.sqlite3`: `nodes(kind='actor')`, `actor_activations`, and actor lifecycle journal rows. Owned by `ActorCoordinator`. | Activation is driven to `down`; rows remain indefinitely. |
| Agent identity | `state/agents.sqlite3`: one `agents` row keyed by the actor name. `src/hyprial/daemon/application.py:_ensure_agent` creates or updates it after lifecycle start settles. | Kept. |
| Agent home | `<hyprial-home>/agents/<actor>/`, with `state/`, `config/`, `secrets/`, optional `workspace/`, and `state/home-receipt.json`. The matching `lifecycle_resources` row in `agents.sqlite3` is the authority; `src/hyprial/agents/home.py:AgentHomeProvisioner.provision` creates it and `validate` enforces its receipt/token fence. | Kept with an active home resource. |
| Agent-home P2 roots | `src/hyprial/agents/environment.py:compose_worker_child_launch` obtains the daemon-resolved runtime context and replaces `HOME`, XDG roots, and native harness roots with paths under the agent's home. Explicit secret grants are resolved into the child environment without logging values. | Content remains in the home; no workflow cleanup exists. |
| Harness desired state | The non-Lark `HarnessLaunchSpec` in `state/desired-state.json`, including status and the latest worker session reference when available. `src/hyprial/daemon/desired_state.py:confirm_harness_lifecycle` records a successful start. | Lifecycle deactivation removes/stops the active harness intent, but the agent identity and its historical session fields remain. |
| Session metadata | `agents.last_harness`, `agents.last_session_id`, plus the harness-specific session reference projected by the harness supervisor and synchronized into desired state. Native session material lives below the P2 harness roots in the agent home. | Agent and native session material remain. The measured workflow population currently has no `last_session_id` values. |
| Grants | `agent_capability_grants`, `agent_secret_grants`, and append-only `agent_grant_journal` in `agents.sqlite3`; private secret entries may live below the agent home. Foreign keys bind live grants to the exact agent incarnation. | Live grants cascade only when the agent row is destroyed; the grant journal intentionally survives normal destroy for audit. |
| Routes and liveness | A persona route is re-derived from every agent row by `src/hyprial/daemon/application.py:_restore_persona_routes`; lifecycle start also registers a process route through `LifecycleProcessManager`. | The process route is dropped on deactivation. The persona route remains because the agent row remains and is restored on daemon restart. |
| Mail | `state/inbox.sqlite3` can contain `inbox`, `outbox`, `custody`, delivery-status, and failure-settlement rows naming the full actor URI. | Normal worker stop does not delete these rows. Unconsumed rows expire by inbox policy; acknowledged inbox history is deliberately retained until the same message id is reused. |
| Worker JSONL | `state/logs/workers/<runtime>-<actor>.jsonl`, selected by `src/hyprial/log/__init__.py:route_path`. | Kept. Successive generations append to the same file. |

The existing irreversible cleanup surface is operator-oriented agent destroy,
not PAC collection. `src/hyprial/daemon/application.py:_destroy_agent` stops
connectors, drops the persona route, drains currently pending inbox messages,
and invokes `src/hyprial/agents/registry.py:AgentRegistry.destroy`.
`AgentRegistry.destroy` deletes the agent row and cascaded live grants, revokes
the home receipt, then `AgentHomeProvisioner.cleanup` deletes home content with
receipt-last crash recovery. It has no archive step, and its own contract says
there is no tombstone or revival path.

### Graph end stops processes but does not collect workers

`src/hyprial/pac/workflow_runtime.py:close_workflow` records the workflow's
terminal state, settles unfinished workflow-node projections as done or
cancelled, sets `graphs.closed_at`, and appends `graph_closed` plus
`workflow_changed` events. It is reached for explicit graph close, a settled
workflow, terminate-on-failure, deadline failure, or
`GraphWorkflowService.cancel`. The terminal workflow states are `completed`,
`failed`, and `cancelled`; an expired node becomes a failure and therefore ends
the graph when policy is `terminate`.

On its next reconciliation pass,
`src/hyprial/pac/lifecycle.py:ActorCoordinator.reconcile` notices a closed graph
and changes every existing activation from `up` to a durable `down/pending`
operation. `_reconcile_down` calls the ordinary runtime stop, and
`_complete_down` records `actor_down`, marks the activation `down/done`, and
resets the actor flag. This is the graph-end reclamation path for processes.
The daemon's coordinator repeatedly scans all graphs and actor nodes through
`ActorCoordinator.reconcile_all`, so an interrupted stop is retried.

There is no corresponding deletion of the PAC actor node/activation, agent
registry row, active home resource, agent-home directory, persona route,
session artifacts, worker JSONL, or historical mail. There is also no periodic
PAC worker collector.

### What PAC history keeps today

`src/hyprial/pac/store.py:PacGraphStore` owns
`state/pac-graph.sqlite3`. It retains graph structure and ownership
(`graphs`, `nodes`, `edges`), append-only flag and typed journal events
(`flag_events`, `journal`), notification plans and delivery ids
(`notifications`, `workflow_deliveries`), actor activations, workflow/node
projections, request ids and input fences, fixed deadlines, bounded node output,
remote return custody, and outcome receipts.

`src/hyprial/pac/workflow_graph.py:compile_workflow` stores the canonical
workflow specification and per-worker launch documents under
`state/workflow-artifacts/<specification-digest>/`. A node `brief_ref` is an
opaque reference such as `workflow:<graph>#<node>`; at dispatch,
`src/hyprial/pac/workflow_runtime.py:WorkflowSender.send` resolves the immutable
specification and constructs the task brief plus predecessor evidence. Evidence
references are retained as opaque strings in node flags, workflow-node
`reason_ref`, workflow terminal `reason_ref`, and outcome receipts. PAC does not
copy a referenced file into the database merely because it is used as an
evidence reference.

`state/workflows.sqlite3` is separate: it is the sealed, read-only legacy
workflow archive/write barrier created by
`src/hyprial/pac/legacy_workflows.py:cutover`. It is read by
workflow history commands, not by the PAC executor, and currently has no TTL.

No PAC graph-history prune exists. On the measured machine the PAC store has 83
graphs, 75 managed workflows (74 terminal and one open), 142 actor nodes, 127
activation rows (126 `down/done`, one current `up/done`), 1,590 journal rows,
201 notification rows, 179 workflow-delivery rows, and 168 outcome receipts.

### What "dispatch/execution logs" means today

There is no single log object or one retention policy. A readable PAC history
currently depends on four stores:

1. `pac-graph.sqlite3` and `workflow-artifacts/`: graph decisions, lifecycle
   events, request/delivery identities, briefs, flags, outputs, and evidence
   references. Retention is unbounded.
2. `state/logs/daemon.jsonl`: workflow delivery/lifecycle/daemon events emitted
   through `src/hyprial/daemon/application.py:_log`. The daemon constructs
   `Logger.daemon` without a rotation hook.
3. `state/logs/workers/<runtime>-<actor>.jsonl`: worker generation and turn
   events emitted by each harness's `Logger.worker`, also without a rotation
   hook. `src/hyprial/daemon/top.py:read_worker_turn_stats` deliberately reads
   only the latest generation for live counters, but earlier generations stay
   on disk.
4. `state/inbox.sqlite3`: request/reply payloads, custody, consumption,
   delivery-status, and failure settlement. Unconsumed inbox rows have a
   deadline and the delivery-status default retention is seven days
   (`src/hyprial/inbox/pull.py:HoldPolicy`), but acknowledged inbox rows are not
   swept (`src/hyprial/inbox/service.py:InboxService.prune_inbox`). This is not a
   coherent PAC-log TTL.

`src/hyprial/log/__init__.py:Logger._append` exposes a `rotation_hook`, but no
production daemon or worker logger supplies one. The only existing log archive
operation, `migrate_pre_trajectory_logs`, is a one-time format migration rather
than ongoing retention. The measured active files were about 241 MiB for
`daemon.jsonl`; 256 workflow-worker log files occupied about 65 MiB. Worker GC
cannot currently distinguish or preserve a graph history as one retained unit.

### Restart restoration has no idle threshold

`src/hyprial/daemon/application.py:_restore_harnesses` loads desired state and
passes every non-Lark `HarnessLaunchSpec` whose last recorded status is
`running` to the harness supervisor's restore operation. Age, last activity,
graph terminality, inbox state, and agent keep-list membership are not inputs.
A successfully running worker from before restart is therefore relaunched no
matter how long it has been idle.

PR #879 adds `agents.last_active_at_ms`, the best-effort coalesced writer in
`src/hyprial/agents/registry.py:AgentRegistry.record_activity`, conservative
activity hints, inactive filtering in
`src/hyprial/daemon/application.py:_agent_is_inactive`, and the atomic
`state/agent-keep.json` managed by
`src/hyprial/agents/activity.py:AgentKeepList`. Those facts are available on
this design branch but are not yet consulted by `_restore_harnesses`. The live
daemon measured for this document is still version 0.4.6: its current
`agents.sqlite3` lacks the new `last_active_at_ms` column, its installed CLI has
no `agent keep` command, and no `agent-keep.json` exists. Rollout of #879 is
therefore a real dependency, not assumed production evidence.

### Read-only measurements on 2026-09-27

The production daemon was not changed or restarted. SQLite data was copied to a
scratch directory before queries; status commands were read-only.

| Measurement | Result |
| --- | ---: |
| Registered agents | 396 |
| `wf-*` agents | 127 |
| `wf-*` status | 126 offline, one current workflow worker online |
| Current `wf-*` connectors/processes | one, belonging to this design task |
| Historical offline `wf-*` processes | zero running |
| `wf-*` home directories / active home-resource rows | 127 / 127 |
| `wf-*` agents with registry `last_session_id` | 0 |
| `wf-*` live capability grants / secret grants / grant-journal rows | 0 / 0 / 0 |
| `wf-*` agents created in the preceding 24 hours | 84 |
| Oldest to newest `wf-*` creation span | about 67.7 hours |
| Terminal workflow actor nodes present in the agent registry | 126 |
| Terminal for at least six hours | 100 |
| Those 100 with pending inbox rows or a requested workflow node | 0 / 0 |
| Workflow-worker JSONL files / disk blocks | 256 / about 65 MiB |

The task's pre-dispatch observation of 126 offline workflow workers and no
workflow process is consistent with the live measurement: accepting this task
created the 127th registry/home record and the sole running workflow process.
The older estimate of roughly 40 new records per day is directionally correct
but already stale for this burst; the last-24-hour count was 84.

## Proposed contract and defaults

The five requirements need two separate clocks. Worker collection runs every
six hours and may retire an eligible worker once its graph has been terminal for
six hours. PAC history remains queryable for 30 days after graph closure. A
worker disappearing must never imply that its workflow history disappeared.

| Policy | Recommended default | Configuration boundary |
| --- | --- | --- |
| Eager workflow admission | Start every distinct graph-owned worker before issuing any task request; admit no more than 20 distinct owned workers per graph by default. | Daemon policy; reject a larger graph before worker effects unless the operator raises the cap. |
| Terminal grace before worker collection | 6 hours from `graphs.closed_at`. | Durable collector policy, minimum zero only in tests. |
| Collector cadence | Every 6 hours, plus an explicit dry-run command. | Daemon maintenance scheduler. |
| Worker disposition | Archive, then delete the live worker. | Per-installation policy; `delete` is available only as an explicit irreversible mode. |
| Worker archive retention | 7 days after successful collection. | Independent archive-pruner policy. |
| PAC dispatch/execution history | 30 days from `graphs.closed_at`. | PAC history-pruner policy; open graphs have no expiry. |
| Restart idle threshold | 12 hours. | Durable daemon setting; pending work or per-agent `always` overrides it. |

The mechanism belongs in the daemon: it already owns process lifecycle, the
agent registry, routes, inbox, PAC stores, and maintenance cadence. Policy is a
small durable document with schema version, cadence, terminal grace, archive
mode/retention, log TTL, restore threshold, and eager-worker cap. A missing or
malformed collection policy means **no collection**. A missing or malformed
restore policy means **restore as today**. Both failures are logged and exposed
by doctor; neither is silently replaced with a destructive default.

## 1. Eager creation and a concrete roster

### Admission mechanism

Keep `compile_workflow` as the atomic graph publication point and keep the
existing actor nodes as the ownership map. Add one immutable workflow-worker
receipt per actor node with:

- graph id, actor-node id, actor name and canonical actor URI;
- ownership `workflow` (borrowed principals never get a receipt);
- launch digest and the agent incarnation/entity token created by lifecycle;
- admission generation and state `planned`, `starting`, `ready`, `failed`, or
  `down`;
- the full roster digest.

After compile and before `GraphWorkflowService._project` can issue the first
request, an eager-admission coordinator creates an `up/pending` activation for
every workflow-owned actor node, irrespective of task readiness. It uses the
existing daemon lifecycle manager; there is no second launcher. The coordinator
waits until all receipts are `ready`. Only then does it set the workflow's
admission barrier and let request projection run.

This changes `ActorCoordinator._ready` for managed workflows from "an assigned
step is ready" to "the workflow is admitted and this graph-owned actor must be
up". Task readiness remains in `input_token` and request projection, not process
creation. A shared worker is started once. A graph with no owned workers (all
explicit borrowed owners) crosses the barrier without process effects.

Preflight counts distinct graph-owned workers, validates every immutable launch
document, confirms local capacity, and enforces the configured eager-worker cap
before publishing the graph. The schema already permits 100 task nodes; without
a separate worker cap a valid graph could start 100 model runtimes at once. The
recommended default of 20 is an admission bound, not partial eager creation:
the whole workflow is accepted or rejected. Operators can raise it deliberately
for a machine with measured capacity.

If any start fails or its receipt cannot be bound to the expected agent
incarnation, the coordinator records the exact failed actor, closes the graph as
failed before any task notification, and drives every already-started owned
worker down. Failure to stop one of those workers is an observable cleanup debt;
it does not make the failed graph look admitted. Recovery replays receipt state
and converges the same start-or-stop operations idempotently.

### What "knows the others exist" means

The canonical roster is a stable PAC projection, not peer discovery. It contains
every executable node and:

- node id and role;
- canonical owner URI;
- actor-node id and canonical actor URI for graph-owned workers;
- `workflow` or `borrowed` ownership;
- local or remote location when known;
- roster digest and graph id.

`hyprial workflow inspect` exposes the roster. `WorkflowSender.send` includes the
same roster projection and digest in every task brief, so A and B see the same
node-to-actor map even if B has not received work. The daemon creates the normal
persona/process routes for every local graph-owned worker during eager lifecycle
start. At process binding, the worker channel also carries the graph id and
roster digest and exposes a bound read-only roster query; this is the
process-level knowledge before a model has taken a turn. The first actual task
brief carries the complete human/model-readable roster. A worker may address
another actor using the roster URI; no bootstrap turn, separate unfenced alias,
or all-to-all inbox message is created. Borrowed and remote actors appear in the
roster but remain owned by their existing daemon/principal.

The roster says **who exists and how to address them**; it does not grant a node
authority to complete another node or read another agent's home. PAC request id,
owner, fixed deadline, and transitive input-token fencing remain unchanged.

### Rework and loss

A back edge or stale-input withdrawal occurs while the graph is open. Eager
workers therefore stay up and a reset node can receive its new fenced request
without being recreated. If a graph-owned process is lost, the existing
incarnation/identity-marker rules apply: the actor flag is reset, the request is
withdrawn, and an explicit lifecycle wake starts a new incarnation before the
request is projected again. Completion with the old request id remains stale.

## 2. Reclaim every owned process when the graph ends

`close_workflow` remains the one place that declares terminal graph state. In
the same PAC transaction it must also write a cleanup intent for every
workflow-worker receipt. The lifecycle coordinator consumes those intents and
uses the existing `down` operation and identity marker. It never derives
ownership from the `wf-` prefix alone.

Graph status stays terminal immediately; process cleanup is a separate
projection:

- `workerCleanup: pending` while any owned actor has not reached `down/done`;
- `workerCleanup: complete` after every owned actor is absent and its durable
  activation is `down/done`;
- `workerCleanup: attention` after bounded retries or an unknown/mismatched
  process observation.

The daemon continues retrying pending stops across restart. A marker mismatch,
unknown process identity, unavailable lifecycle manager, or stop timeout fails
closed: the actor is not declared reclaimed and can never pass GC eligibility
while it stays in that state. It raises a structured event and doctor warning
naming graph, actor, operation, age, and last observation. Borrowed actors have
no workflow-worker receipt and are excluded from cleanup construction, so graph
close can neither stop nor adopt them.

Amended 2026-10-18 (Allen: PAC must reclaim workers automatically, no agent
left behind): `attention` is retried rather than parked. Each failed
evaluation counts one attempt and the next stop uses a fresh lifecycle
operation id (`<cleanup operation>:retry:<n>`), because the lifecycle manager
replays the terminal result of a reused id. A process whose strict PAC marker
names this graph and actor node (an earlier incarnation, or a start that
reported failure but came up) or that carries no marker while its agent entity
token equals the receipt's (a non-PAC restart of the same agent) is this
actor's own and is stopped with its observed fence; a marker-proven
incarnation whose receipt has no token adopts the observed token so GC can
later prove it. A foreign marker or foreign entity token still fails closed.
A closed graph whose receipts predate cleanup intents gets the intent
`close_workflow` would have written on its next reconcile.

Review follow-ups (2026-10-18): a marker-less activation proves nothing and a
NULL receipt token matches nothing; token adoption also requires the node's
journal to show the marker (incarnation and effect) was issued there. Failed
intents back off `min(5 min * 2^(attempts-1), 6 h)` from their last update;
a reconcile that raises before writing an outcome also counts an attempt and
moves the update time, so it cannot stay the oldest due retry.
The actor sweep that admits cleanup rows runs on the 30-second running tier as
well as the 300-second fallback tier, so the retry cap is per 30-second sweep:
at most two retries in flight or newly admitted, oldest due first. In flight
means queued in the actor service or queued/running on the graph authority's
effect pool (`actor_effect_keys()`), because production observe/stop work
runs there, not in the actor service. Two is half of the authority's four
effect threads, so retries alone can never hold more than two. The cap
bounds retries only; it does not reserve threads for open graphs. First
cleanup evaluations (attempts 0), worktree cleanup (up to 16 per sweep) and
expansion preflight share the pool uncounted. At deployment the burst is
the first evaluation of the backfilled closed graphs (about 186 in
production); most are offline and finish with one observe, but a present
worker's first stop can block a thread for the lifecycle budget.
Tracked, not changed in this round: throttling first evaluations too (for
example a separate first-evaluation cap per sweep) if the deployment burst
measurably delays open-graph reconciles. R&D default pending Allen's ruling: an
agent pinned to an adapter, or with a registered interactive session that is
online (or whose session registry/presence cannot answer), is `worker_in_use`.
Presence that is wired but not yet ready (restart or rebuild, before the
carrier heartbeats again) reads offline; that window can stop, never delete.
The signals are read from the agent record, so they hold after the worker spec
is gone. Cleanup does not stop such an agent, PAC GC skips it
(`worker-in-use`), and the destroy recheck under the reservation refuses it.
The policy lives in one function, `coordinator.types.worker_in_use`.

Expired workflows follow the same path. Node deadline expiry first produces the
current fenced failure; a terminate policy closes the graph; terminal cleanup
then stops all owned workers. `hold` is not terminal and does not reclaim the
workers until an explicit cancel/terminal resolution.

## 3. Six-hour worker collection

### Exact eligibility predicate

At a frozen `observed_at_ms`, a worker is eligible only if **all** of these are
true:

1. It has a workflow-worker ownership receipt whose graph/actor-node/actor name,
   launch digest, and agent entity token still match PAC and the registry.
2. Its graph is a managed workflow in `completed`, `failed`, or `cancelled`, has
   non-null `closed_at`, and `closed_at <= observed_at_ms - terminal_grace_ms`
   (six hours by default).
3. Its activation is `desired='down'` and `op='done'`, runtime observation says
   absent, and no desired harness or live/interactive binding still speaks for
   its URI.
4. It is workflow-owned. An explicit owner/borrowed actor has no ownership
   receipt and can never become a candidate, even if its name begins with
   `wf-`.
5. No assigned `workflow_nodes` row is `requested`; there is no current remote
   workflow request or unsent return for it; no notification to it is still
   planned without a message id.
6. `inbox.sqlite3` has no unconsumed inbox row for either the canonical URI or
   legacy bare name, and no pending custody/outbox/failure-retry row would be
   orphaned by removal.
7. No migration, transfer, lifecycle receipt, or archive/delete operation for
   that agent is already in progress.

Any unavailable store, failed cross-store read, entity token mismatch, or
unknown liveness result makes the candidate ineligible for that run. The
collector emits a reason code instead of guessing. Eligibility is
rechecked under the mutation fences immediately before archive and immediately
before registry deletion; a dry-run result is not a lock.

The measured scratch snapshot had 100 terminal actor records beyond the
six-hour grace and no pending inbox or requested workflow-node rows. That is an
inventory observation, not approval to delete them.

### Archive mode

The default collector writes one archive at:

```
state/archives/workflow-workers/YYYY/MM/DD/<graph-id>/<actor>/<collection-id>/
```

The directory is `0700`; files are `0600`. It contains:

- `manifest.json`: schema version, collection id, graph/node/actor ids,
  ownership-receipt digest, terminal state/reason/time, archive time, source
  entity/resource tokens, file sizes/digests, collector version, and retention
  deadline;
- `agent.json`: the exact registry row, active capability-grant metadata,
  secret-grant metadata (source, entry id, environment names and revision, but
  never resolved secret values), agent grant-journal rows, home lifecycle
  resource/receipt rows, desired harness record if any, and last harness/session
  ids;
- `home.tar.zst`: a no-follow, path-validated archive of the complete agent home
  (`state`, `config`, `secrets`, and `workspace`), including P2 native session
  roots and any agent-private secret entries already present there;
- `inventory.json`: each archived path's type, mode, byte count, and digest, plus
  unreadable/skipped entries. Any skipped or changed entry aborts the archive.

Because `home.tar.zst` may contain credentials, it inherits the private state
root's trust boundary and must never be attached to PAC messages or ordinary
logs. Archive inspection redacts authority tokens and secret metadata by
default. The recommended seven-day archive retention is deliberately shorter
than PAC history retention: it provides a recovery window without keeping an
extra credential-bearing home for a month.

Archive is a two-phase operation. The collector writes a sibling
`.partial-<collection-id>` directory, fsyncs every file and directory, verifies
the manifest against a second no-follow inventory, and atomically renames it to
the final path. Only the final rename allows the PAC collection ledger to mark
`archive_ready`. Then the daemon uses the typed agent lifecycle/destroy path to
drop routes and bindings, revoke and remove the home, delete the registry row
and its cascaded live grants. A collector-only registry purge, fenced by the
workflow ownership receipt and completed home cleanup, then removes the old
agent's grant-journal and inactive lifecycle resource/receipt rows. The PAC
collection ledger becomes the durable tombstone; generic operator destroy keeps
its existing audit behavior. The collector finally marks `live_deleted`.

A crash before rename leaves an untrusted partial directory; the same collection
id resumes or removes it after verifying that no final archive exists. A crash
after rename but before live deletion reuses the verified archive and retries
deletion. A crash after deletion but before the final ledger update observes the
missing matching entity token and the present archive, then completes the
ledger. A second run returns the existing outcome. It never creates a second
archive or deletes a same-name replacement agent.

Archive restoration is an explicit future/operator action, not automatic PAC
rework. A valid archive has enough protected material to reconstruct the same
registry configuration, grants, P2 home, and native sessions after an authority
review. Routes are derived again rather than archived as live handles. PAC
graph/history rows are never restored because collection never removed them.

### Delete mode and irreversibility

Explicit `delete` mode skips `home.tar.zst` and retains only a collection ledger
entry with non-secret ids, counts, digests, terminal reason, and deletion time.
It removes the agent-home tree (workspace, native harness configs/sessions and
agent-private secret entries), agent row, active home resource, pins, live
capability/secret grants, grant-journal and lifecycle-receipt rows,
persona/process routes, and any now-empty desired runtime binding. These bytes
cannot be recovered.

The remaining PAC graph can reconstruct the actor name, launch document, owner,
and task history. It cannot reconstruct the old entity/resource tokens,
workspace, credentials, native session state, or destroyed live grants. A later
manual recreation is a new agent incarnation. The CLI must print that exact
loss and require both an irreversible mode flag and an approval record; delete
must never be the fallback when archive fails.

### Cadence, dry-run, and first-run gate

The daemon maintenance scheduler invokes the collector at most once per six-hour
window with a durable window id; overlapping ticks coalesce. The public dry run:

```
hyprial workflow worker gc plan --observed-at <ms> --json
```

lists every evaluated workflow-owned actor, eligible or not, with graph state
and close age, ownership/entity-token result, keep result, process/activation
state, open-request and mailbox counts, home/log sizes, selected disposition,
archive path, and a stable plan digest. It makes no archive, route, registry,
home, PAC, inbox, or desired-state mutation.

Deployment starts in `dry-run` mode. The first mutation over the existing
legacy population requires Allen's explicit approval of the exact plan digest,
terminal grace, disposition, archive retention, and candidate count. The gate
sits in the collector **after the final eligibility recheck and before creation
of the first partial archive**. A one-use durable approval record binds Allen's
configured full user principal, approval evidence reference, plan digest,
policy digest, maximum candidate count, and expiry. Caller-supplied text is not
an approval. A changed plan/policy or an extra candidate invalidates it. Later
scheduled windows use the installed enabled policy; switching from archive to
delete or shortening retention requires a new approval.

The approved first inventory may execute in small deterministic batches, but it
is still the same approved plan; a batch must not admit newly eligible actors.

## 4. Thirty-day dispatch and execution history

### Retained history bundle

For 30 days after `graphs.closed_at`, `hyprial workflow status`, `inspect`, and
history must remain useful without the agent registry or home. The retained PAC
bundle is:

- graph, node, edge, actor activation, flag-event, journal, notification,
  workflow projection, delivery, remote-return, outcome/output, and collection
  ledger rows in `pac-graph.sqlite3`;
- immutable specification and launch artifacts referenced by that workflow;
- exact daemon structured events carrying graph id, request/message id,
  lifecycle operation, delivery outcome, and collection outcome;
- all JSONL worker generation/turn events for graph-owned actor names;
- inbox/custody/status/failure rows associated by exact workflow conversation or
  a durable graph-to-message ledger;
- opaque evidence references and their recorded reason/output text. A reference
  does not transfer ownership of the referenced file, so history pruning never
  deletes an arbitrary `EVIDENCE.md` or other target path.

Worker collection is prohibited from touching any item in this bundle. Current
worker JSONL already lives outside agent homes; that separation becomes an
explicit invariant and test.

### Rotation and pruning

Install a production rotation hook for daemon and worker loggers. It closes a
UTC daily segment atomically, writes a checksum-bearing compressed segment, and
reopens the canonical `.jsonl`. The log reader learns the segment manifest and
continues to provide one chronological query. A shared `daemon.jsonl` segment is
retained until every record in that segment is older than 30 days; this may keep
up to one extra day and avoids rewriting shared history by graph.

Worker files are graph-specific because graph-owned actor names contain the
graph id. Closed files may be compressed after graph terminality, but are not
removed before `closed_at + 30 days`. Open/held graphs and the active segment
never expire. Logs without a parseable timestamp or ownership mapping are
quarantined from pruning rather than guessed old.

A PAC history pruner runs as a separate maintenance component. At 30 days it:

1. verifies the graph is still terminal, all remote returns and notifications
   are settled, and no worker collection operation is incomplete;
2. writes a small terminal history manifest (graph id, terminal state/reason,
   close time, counts and digests) for audit of the prune itself;
3. asks the inbox service to remove exact workflow-associated consumed/history
   rows using the durable message/conversation ledger, never raw prefix SQL;
4. deletes the managed workflow's PAC rows in one ordered transaction through a
   dedicated store method. A schema migration gives the append-only journal
   trigger one narrow exception: it accepts deletes only while the same
   transaction holds a durable retention authorization for that exact terminal
   graph and plan digest. The method never drops triggers or disables foreign
   keys, and ordinary journal updates/deletes still fail;
5. deletes immutable artifacts only when no retained graph references their
   digest;
6. removes expired worker-log segments and finally records counts/bytes in the
   daemon log.

The pruner is fail-safe but fail-open for daemon availability: a failed prune
leaves extra history and retries later; it never blocks workflow execution or
daemon startup. `workflows.sqlite3`, the sealed legacy archive, remains outside
this new TTL until a separate legacy-retention decision is made.

## 5. Restore-on-restart threshold

### Decision and measurement

Use a 12-hour default. For a local managed agent, effective activity is:

1. `agents.last_active_at_ms` when present;
2. otherwise `agents.created_at_ms` for a newly migrated/unobserved agent.

Home mtime remains an informational `agent list` hint but is not a restore
clock: archive, configuration, migration, or antivirus activity can touch the
tree without agent work. A null activity value on a pre-#879 row is handled
conservatively: restore it once, emit `activityUnknown=true`, and begin using the
new activity writer. No existing running intent is silently suppressed merely
because the migration could not reconstruct past activity.

Before the restore batch, the daemon classifies each `status='running'`
non-Lark harness:

- restore if idle age is at most the threshold;
- restore regardless of age if the agent has per-agent restore policy
  `always`, has an unconsumed inbox/custody item, or owns a
  currently requested PAC node/remote return;
- suppress automatic restore if it is older than the threshold and none of the
  overrides applies;
- never use this policy to restore a worker from a terminal workflow; terminal
  PAC cleanup takes precedence.

Skipping only `_restore_harnesses` is insufficient because the harness
reconciler would later start the same desired row. The classification therefore
writes a durable `idle-suppressed` restart disposition keyed by agent entity
token and desired-harness generation. Both startup restore and later reconcile
must honor it. New inbox work, an explicit start/wake, or an `always` override
clears the disposition through the normal lifecycle path.
A different same-name entity token cannot inherit the old suppression.

Global threshold and per-agent policy (`active`, `always`, or `never`) are
durable settings. `active` is the default threshold behavior. `always` is a
restore override but does not by itself exempt the worker from PAC collection.
It is the supported operator choice for an important persistent agent. `never`
still allows an explicit start or pending-work wake.

### User-visible reporting

`hyprial agent list --json` retains suppressed desired connectors with:

```
restoreStatus: idle-suppressed
lastActiveAtMs: <value or null>
idleAgeMs: <value or null>
restoreThresholdMs: 43200000
restoreOverride: none | pending-work | per-agent
activityUnknown: true | false
```

The row's `status` reads `dormant (idle 17h; auto-restore threshold 12h)` rather than
`offline`. Doctor reports the suppressed count, oldest idle age, unknown-activity
count, malformed-policy failures, and exact `agent restore <name> always`
or explicit start remediation. Restore-policy read failure is reported as
`restore-policy-degraded` and restores the agent, preserving today's availability
behavior.

## Contract interactions and safety invariants

| Existing contract | Required interaction |
| --- | --- |
| PAC request fencing | Eager admission is an operational barrier only. Node completion still requires the current request id, exact owner, unexpired deadline, and current input token. Roster membership grants no flag authority. |
| Withdraw/rework | Open graphs keep workers. A reset/withdraw creates a fresh request only after the worker's current incarnation is up; old request ids remain stale. GC requires terminal graph state, so it cannot race legitimate rework. |
| Borrowed actors | Explicit-owner nodes have no workflow-worker receipt. They appear in the roster and history but are never started, stopped, archived, deleted, or restore-suppressed by graph policy. This remains true for a misleading `wf-` name. |
| Legacy keep-list migration | Startup converts every legacy entry to per-agent `always`, then removes the old file. A migration failure preserves the old file and aborts startup visibly. |
| Agent-home P2 | All root resolution continues through `AgentRuntimeContext` and the registry home receipt. Archive walks only the receipt-fenced home without following links. Delete uses registry revoke/cleanup; neither path derives roots from cwd, ambient HOME, or actor URI. |
| Agent incarnation | Every mutate/delete/suppress decision binds the registry entity token and home resource token captured by the workflow-worker receipt. Same-name replacement means skip, alert, and human review. |
| Mail authority | Eligibility reads through inbox-owned queries. Collection never edits inbox tables directly; history pruning calls a typed inbox purge API after the 30-day gate. |

## Crash safety, observability, and operations

Every collection plan and outcome produces structured events with policy/plan
digest, graph id, actor, entity token digest, eligibility reason, disposition,
phase, duration, bytes, and error category. Authority tokens, secret values,
archive contents, and task bodies never enter logs. Minimum metrics are:

- candidates evaluated/eligible/deferred by reason;
- archives started/completed/failed, bytes and duration;
- live deletions completed/failed and cleanup debt age;
- terminal graphs with process cleanup pending/attention;
- live/archive/history bytes and oldest age;
- restore attempted/restored/failed/suppressed/overridden/unknown-activity;
- history and archive prune counts/bytes/failures.

The daemon exposes collector mode, next window, last plan digest, last outcome,
approval state, and debt counts in `ps` and doctor. An operator can re-run the
same collection id; idempotence is a supported recovery path, not an accident.

Dry-run must be a positive control in tests and rollout: with a seeded eligible
worker it lists exactly that actor and leaves byte-identical PAC, agents, inbox,
desired-state, home, route, and log inputs. Changing one predicate at a time
(pending mail, open request, process present, borrowed ownership, entity
mismatch) must remove it from candidates with the expected reason.

## Phased PR plan

| Phase | Change | Red-first tests and acceptance |
| --- | --- | --- |
| 0. Deploy #879 foundation | Ship registry `last_active_at_ms`, activity recording, and inactive reporting before behavioral changes. The temporary keep-list shipped here is retired by CLI refactor F3. | Upgrade an old registry; prove null activity is preserved and real turn/delivery activity records and coalesces. No collection/restore behavior changes. |
| 1. Ownership receipt and roster | Add immutable workflow-worker receipts, roster projection/digest, and inspect/brief output. | Borrowed-prefix adversary gets no receipt; entity/launch mismatch fails; A and B receive byte-equivalent complete rosters; request fences remain unchanged. Depends on no later phase. |
| 2. Eager admission barrier | Preflight capacity, start every owned worker through lifecycle, and block task projection until all are ready. | A -> B proves both processes/routes exist before A request; shared workers start once; 21-worker default rejection has zero effects; nth-start failure produces zero task deliveries and converges prior starts down; restart resumes partial admission. |
| 3. Terminal cleanup projection | Persist cleanup intents at close and expose pending/complete/attention. | Completed, failed, cancelled, and expired graphs stop all owned workers; hold does not; borrowed actors survive; marker mismatch/stop timeout remains pending and ineligible; restart converges exactly once. |
| 4. Dry-run collector and archive engine | Add exact eligibility evaluator, six-hour scheduler, plan digest, archive transaction and collection ledger. Mode remains dry-run. | Frozen-clock boundary at exactly six hours; each exclusion predicate; P2 no-follow/symlink and receipt fences; partial archive crash at every phase; same-name new incarnation survives; repeated run is byte-stable/idempotent. |
| 5. Thirty-day history retention | Add daily rotation, graph/message association, query support, and fail-safe history/archive pruners. | Worker GC leaves status/inspect/log history readable; day 29 retained/day 30 eligible; open graph never pruned; shared daemon segment waits for its newest record; evidence targets are untouched; prune failure leaves history; legacy archive unchanged. |
| 6. Restore threshold | Add durable restart dispositions, 12-hour policy, overrides, wake clearing, and ps/doctor reporting. | 11h59m restores, 12h boundary is defined and pinned, 17h suppresses, pending/open request restores, null migrated activity restores once, terminal workflow never restores, malformed policy fails open, later reconcile cannot bypass suppression. Depends on #879 activity. |
| 7. Approved live collection | Produce the exact legacy plan, obtain Allen's one-use approval, execute deterministic batches, and retain evidence. | Mutation is impossible without matching unexpired approval; plan drift refuses; archive inventory verifies before delete; final report reconciles every approved actor to archived/deleted/deferred with reason. First live execution is an operational gate, not merely a merged test. |

Each PR stays independently default-off where it can delete or suppress. The
first three phases can land without enabling collection. Phase 5 should land
before Phase 7 so the first live worker deletion already has the promised
history separation.

## Allen's decisions (2026-09-27, Feishu)

Allen accepted the recommended defaults: **archive before delete**, **7-day
archive retention**, **12-hour restart idle threshold**, and **20 eagerly
started owned workers per graph**. The 30-day PAC history TTL and the six-hour
cadence/grace stand as required.

He then raised a gap: a worker stalled because its model quota is exhausted
waits for a human and easily passes 12 hours, so it must not be treated like an
idle worker. The design therefore adds an explicit worker state model instead
of one "stalled/paused" bucket (confirmed: «确认，写进#885后开始派pac实施»).

### Worker state model

| State | Meaning | Restart restore | Collection (GC) | Notes |
| --- | --- | --- | --- | --- |
| `running` | Working normally. | Restored. | Never. | |
| `idle` | Online, no work. | Restored while idle age <= 12 h; beyond that becomes `dormant`. | Only via its graph's terminal state. | |
| `dormant` | Not auto-restored because idle > 12 h. | Not auto-restored; new inbox work, an explicit start/wake, or policy `always` wakes it. | Only via its graph's terminal state. | Not a fault; `ps` shows `dormant (idle 17h; threshold 12h)`. |
| `blocked` | Needs a human to continue; carries a reason (`provider-quota`, `credential-invalid`, ...). | Always restored, but does not retry its turn automatically; it resumes after the block is cleared (human confirmation or a periodic probe). | **Never collected while blocked**, even if its graph failed. | Entering `blocked` notifies the agent's owner immediately. Blocked time does not count as idle time. |
| `stopped` | Its graph ended, or a human stopped it. | Not restored. | Candidate after the 6-hour grace, subject to the full eligibility predicate. | |
| `archived` | Collected. | n/a | Done. | Record and home kept in the archive for 7 days. |

**Source of `blocked`.** The existing fail-loud path already classifies an
exhausted quota as `PROVIDER_USAGE_LIMIT` (`src/hyprial/availability_loud.py`),
and a quota watchdog exists. `blocked(provider-quota)` is set durably from that
signal, keyed by agent entity token, and cleared by an explicit human action or
a successful probe. Credential failures map to `blocked(credential-invalid)`
the same way. An unrecognised failure never becomes `blocked`; it stays a normal
failure.

**Interaction with collection.** A graph whose failure reason is a
`blocked(...)` cause (for example a node deadline that expired while its worker
was blocked on quota) is excluded from automatic collection until a human
releases it. Eligibility predicate item 8 therefore gains: "the actor is not
`blocked`, and its graph did not fail because of a `blocked` cause unless a
human release is recorded."

**Deferred decision.** Pausing a PAC node's deadline while its worker is
`blocked` (so quota alone never fails a PAC) is offered to Allen as an option
and is **not** part of the first phases. It aligns with his earlier point that
node deadlines should count from activation, and will be designed separately.

## Open questions for Allen

Only four product choices remain; the mechanisms above do not depend on hidden
answers.

1. **Archive or immediate delete?** Recommended: archive the complete private
   home and metadata for 7 days, then purge it. Immediate delete saves more disk
   but makes workspace, native sessions, and agent-private secrets unrecoverable
   as soon as the six-hour collector runs.
2. **Archive retention:** recommended 7 days. This is long enough to detect a
   mistaken eligibility decision and shorter than the 30-day non-secret PAC
   history window. Choose a different fixed duration if recovery practice needs
   longer.
3. **Restart idle threshold:** recommended 12 hours, so the observed 17-hour-idle
   agent would be reported dormant instead of automatically relaunched. Pending
   work or per-agent policy `always` still restores it.
4. **Maximum eagerly started owned workers per graph:** recommended 20. All
   workers within an admitted graph still start up front; graphs above the cap
   are rejected before effects unless an operator raises the configured limit.

The 30-day PAC history TTL and six-hour cadence/grace are treated as accepted
requirements from the task rather than open questions.
