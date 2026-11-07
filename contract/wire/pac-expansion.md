# PAC expansion v1 wire and persistence contract

This contract freezes the first PAC expansion protocol. A planner completion
may bind exactly one immutable child graph beneath one existing expansion
placeholder. Expansion does not permit live graph mutation, nesting,
generation-2 replanning, automatic merge/push/PR, or adoption of borrowed
workers.

## F1. Policy and grammar

The daemon alone loads trusted policy from
`<HYPRIAL_HOME>/pac-expansion.json`. The document is a string-keyed JSON object:

```json
{
  "version": 1,
  "workRoot": "/absolute/path",
  "limits": {
    "maxNodes": 12,
    "tiers": ["fast", "strong"],
    "owners": [],
    "cwdUnder": ["/absolute/path"]
  }
}
```

Only `version` and `workRoot` are required. Omitted individual limits default
to `12`, `['fast', 'strong']`, `[]`, and `[workRoot]`. `maxNodes` is a strict
integer from 1 through 100 and is never a boolean. Lists contain unique
strings. Every organization `cwdUnder` path is contained by `workRoot`.
Explicit empty lists mean none.

A parent may only tighten those four limits. It may add
`worktree: {repo: <absolute>, base: <git ref>}` but cannot replace `workRoot`.
Containment is canonical and symlink-safe, including the generated worktree
root under the narrowed `cwdUnder`. Policy is daemon-injected and never
planner-controlled.

`WorkflowNode` adds defaulted `expands: str | None` and
`limits: dict | None`; absent values are omitted from serialization. Expansion
placeholders serialize ownership as `system`. `WorkflowSpec` adds defaulted
`expansion_policy: dict | None`; `expansionPolicy` is serialized only on an
expansion-enabled parent to record the trusted organization/work-root snapshot
and is not accepted as an author override. Canonical bytes for ordinary
workflows remain unchanged.

The parent planner is a `task` node with role `plan` and `expands` naming the
placeholder. The placeholder has `{id, kind: expansion, after: [planner],
timeout, limits?}` and may have a generated or explicit task. It has no owner,
worker, launch, or expands field. There is exactly one source and target, one
incoming forward edge, and no back edge touching either endpoint.

The child root contains only `version: 2`, `nodes`, and optional `edges`.
A child node contains only `id`, `kind` (`task` or `report`), `task`, `role`,
exactly one of owner or launch, `after`, `timeout`, `deadline_ms`,
`first_output_eta`, and `human_gates`. Child documents have no shared worker,
defaults, workers, nested expansion fields, approval/end nodes, or back edges.
Normal role rules apply. Owned nodes default to tier `fast`; an authorized
explicit cwd is required except for a managed execute-node default. Borrowed
nodes have no launch. Nonempty native args are rejected. The daemon generates
a bounded name, inherits creator/escalation, and fixes child `on_failure` to
`terminate`.

The managed path is
`<workRoot>/.pac-worktrees/<childGraphId>/<nodeId>`, its branch is
`pac/<childGraphId>/<nodeId>`, and expansion generation is 1. An authorized
explicit cwd remains visible and carries an isolation-bypass warning.

## F2. Errors

The identity-domain error vocabulary, exported by `hyprial.identity`, is:

- `PAC_EXPANSION_INVALID`
- `PAC_EXPANSION_POLICY_UNAVAILABLE`
- `PAC_EXPANSION_ALREADY_BOUND`
- `PAC_EXPANSION_SYSTEM_NODE`
- `PAC_WORKTREE_PREPARE_FAILED`
- `PAC_WORKTREE_IDENTITY_MISMATCH`
- `PAC_WORKTREE_CLEANUP_FAILED`

`PAC_EXPANSION_INVALID` has data
`{field: <JSON-style path>, reason: <stable tag>}`. Stable reason tags are
`syntax`, `unknown-field`, `required`, `unexpected`, `kind`, `nested`, `cycle`,
`count`, `tier`, `owner`, `cwd`, `timeout`, `args`, `worker-sharing`, and
`transport-size`.

Existing `WORKFLOW_SCHEMA_ERROR`, `WORKFLOW_OUTCOME_CONFLICT`,
`WORKFLOW_REQUEST_STALE`, `WORKFLOW_DEADLINE_EXPIRED`, and
`WORKFLOW_OUTPUT_INVALID` retain their meanings. Generic daemon errors remain
registered through `hyprial.kernel.ipc_errors`; implementations do not define
duplicate IPC literals. Worktree error data is bounded to graphId, nodeId,
path, and stage, and contains no environment or secrets. An unsupported peer
returns existing `WORKFLOW_REMOTE_INVALID` with
`{field: 'expansion', reason: 'unsupported-peer'}`. Service, application, and
remote layers preserve error data.

## F3. Public and wire protocol

`workflow.complete` adds optional `expansion`, a YAML/JSON string of at most
65,536 UTF-8 bytes and never an origin filename. CLI `--expansion` performs a
bounded local UTF-8 read. MCP `workflow_complete` accepts
`expansion: str | None = None`. `workflow.fail` rejects expansion rather than
discarding it.

`workflow.expansion.plan` accepts
`{graphId, nodeId, yaml, actor, sessionRef?}` for the placeholder. Only the
verified creator or corresponding planner may call it; a remote planner uses a
scoped origin grant rather than a general proxy. Its exact success response is
`{ok: true, graphId, nodeId, expansionDigest, deadlineMs, effectiveLimits,
plan, warnings}`. Before binding, `deadlineMs` is an estimate; acceptance fixes
the authoritative deadline. No reservation, artifact, lease, or attempt is
created by preview.

`workflow.plan` accepts `{yaml, actor, sessionRef?}` and is the authoritative
parent-policy preview. Ordinary offline plan remains supported; an
expansion-enabled plan or run uses daemon preview and start revalidates it.

`childGraphId` and `expansionDigest` occur only in the `workflow.complete` IPC
response and the outcome-receipt columns/result JSON used for replay. PAC
journal `flag_set` and `flag_reset` event data is unchanged; the event and
snapshot physical kind remains `task`. No new journal event data is added.
Expansion linkage is observed through `workflow_expansions` and status/inspect.
Ordinary keys and remote pending/accepted/rejected states are unchanged.

Remote admission/capability responses add the signed feature declaration
`features: ['pac-expansion-v1']`. Its absence refuses expansion-specific
forwarding while ordinary remote tasks stay compatible. Outcomes carry
`expansion`, `expansionDigest`, and `attemptNo`; the origin recomputes the
digest. The complete encoded frame remains at most 256 KiB. Attempt CAS allows
correction only after definitive `PAC_EXPANSION_INVALID`, with no receipt and
the request still current. Pending or accepted outcomes are immutable and a
late earlier reply cannot overwrite them. This mixed-version refusal was
scope-signed by Allen (Lin Smith) on 2026-10-06.

Child status/list adds `parentGraphId` and `parentNodeId`. A placeholder view
has `kind: 'expansion'`, `systemOwned: true`, `childGraphId`, `childState`,
`expansionDigest`, `deadlineMs`, bounded `childNodes` entries containing
nodeId/state/reasonRef, and optional `worktreeCleanup`. There is no system
delivery or human action. Existing stale/progress/current-node/delivery-failure
and worker-cleanup fields are preserved. The one-level summary is
snapshot-consistent and is a projection, not a second authority.

Overview adds `nodeLinks` entries shaped as
`{from: {graphId, nodeId}, to: {graphId, nodeId}, kind: 'expand'}` from planner
to each child and child sinks to placeholder successors. These links are
separate from graph links. Explicit parent task-line/mission wins over name
heuristics.

## F4. Internal signatures and values

The pure S1 contracts are:

```text
policy.load_expansion_policy(home: Path) -> dict
policy.effective_limits(overrides: dict | None, *, policy: dict) -> dict
document.expansion_digest(raw: str) -> str
validation.validate_parent(spec: WorkflowSpec, *, policy: dict) -> WorkflowSpec
validation.validate_expansion(raw: str, *, limits: dict,
  child_graph_id: str, deadline_ms: int, at: int, name: str,
  escalate_to: str | None) -> WorkflowSpec
```

The author digest is over the canonical parsed document and is independent of
resolution, cwd, time, and policy. Duplicate/non-string keys, non-JSON values,
and alias/depth abuse are bounded and rejected. Validators do no DB, network,
or git writes, reuse workflow launch normalization, and use the public tier
vocabulary. Only the trusted side snapshots effective policy.

S2 extracts immutable artifact preparation and a caller-transaction compiler:

```text
prepare_workflow_artifacts(store, spec) -> dict
compile_workflow_in_transaction(store, spec, *, sender, machine, local_owner,
  operation_key, at, prepared_artifacts, graph_id=None,
  parent_graph_id=None, parent_node_id=None, expansion_deadline_ms=None,
  routine_name=None, task_key=None) -> str
```

The compiler requires the caller transaction and never begins, commits, rolls
back, or runs effects. The existing wrapper prepares artifacts first. Child ID
is `wf-` plus UUID5 of `pac-expand:<parent>:<placeholder>:1`; operation key is
`expand:<parent>:<placeholder>:1` while the existing workflow storage prefix
is retained.

`PacReactor.set_flag` adds keyword-only `expansion: str | None = None` without
changing existing arguments or `FlagEventOutcome`; its constructor adds
`expansion_context: PreparedExpansion | None = None`. That context is trusted
per-command preflight data, never IPC input. Production and injected-test paths
share it. Accepted replay is resolved before preflight.

`PreparedExpansion` is a frozen data-only record with parent graph, planner
node/request, placeholder, child graph, author digest, captured input token,
effective limits, policy snapshot, derived `WorkflowSpec`, prepared artifacts,
and worktree plans. The writer computes acceptance time/deadline and rechecks
request, token, binding, current policy, validity, and remaining time. Preflight
does not mint acceptance time. Stored expansion deadline is authoritative.

A worktree plan contains graphId, nodeId, actorNode, repo, baseRef, baseOid,
branch, path, effectiveCwd, and operationId. The operation ID is
`pac-worktree:<child>:<node>`. Plans are immutable and exist only for local,
workflow-owned execute nodes. Durable ownership proof, not a directory name,
authorizes crash recovery, adoption, or removal.

The S2 internal flag helper writes nodes, flag events, journal, and projection
inside the caller transaction with the captured token. It never calls public
`set_flag` or generic request delivery.

Git effects have required keyword-only aggregate timeouts and no defaults:

```text
resolve_base(repo: Path, base_ref: str, *, timeout_s: float) -> str
prepare_worktree(plan: dict, *, timeout_s: float) -> dict
cleanup_worktree(plan: dict, *, timeout_s: float) -> dict
```

Each result is `{state: prepared|removed|retained|attention, path, branch,
baseOid, reason}`. `PAC_WORKTREE_GIT_TIMEOUT_SECONDS` is 30.0 seconds; callers
cap it by remaining operation/placeholder time and each helper uses one
monotonic deadline across its commands.

## F5. Storage schema owned by S2

Schema 18 adds nullable `parent_graph_id`, `parent_node_id`, and
`expansion_deadline_ms` to `workflow_graphs`, with paired-null,
parent-reference, immutable-identity, lookup, uniqueness, and authoritative
binding agreement checks. The binding has a composite parent-node foreign key.
`workflow_nodes.node_kind` is non-null text defaulting to `task`; the compiler
stores expansion/end while preserving legacy end projection.

`workflow_expansions` has primary key `(parent_graph_id, parent_node_id)`,
unique `child_graph_id`, planner node/request, generation constrained to 1,
expansion digest, limits JSON, accepted time, and deadline. Composite node/child
foreign keys and immutable identity/policy/time checks apply.

`workflow_outcome_receipts` adds nullable graph ID, node ID, and expansion
digest; new records bind identity and retain original event/child in result
JSON. `remote_workflow_outbox` adds nullable expansion text/digest and
non-null integer attempt number defaulting to 0. `workflow_worktrees` is keyed
by graph/node and stores actor node, immutable plan JSON, state
(`planned`, `prepared`, `cleanup_pending`, `removed`, `retained`, `attention`),
optional reason, and update time. Managed path and operation identity are
unique; node/actor references apply; borrowed nodes have no plan. Every insert
names its columns.

Owner migration archives byte-identical values for:

- `workflow_graphs.parent_graph_id,parent_node_id`
- `workflow_nodes.node_kind`
- `workflow_expansions.parent_graph_id,parent_node_id,child_graph_id,planner_node_id,planner_request_id,expansion_digest,limits_json`
- `workflow_outcome_receipts.graph_id,node_id,expansion_digest`
- `remote_workflow_outbox.expansion_text,expansion_digest`
- `workflow_worktrees.graph_id,node_id,actor_node,plan_json,state,reason`

The two pre-existing `result_json` columns remain on their existing rewrite
path. Replay compares stored digest/child columns, not result JSON bytes. IDs,
digests, vocabulary, accepted limit history, authored payload, immutable plan,
and worktree state are archival; live principal/routing columns retain their
existing rewrite behavior. Snapshots grant no routing or authorization after a
rename. New attempts reauthorize against live identity/policy and fail closed
on mismatch. Every additional materialized text path/operation column requires
an exact classification before DDL freeze.
