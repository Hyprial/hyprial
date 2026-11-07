# PAC expand: a plan node decides the rest of the graph (v0.2, 2026-09-30, Allen approved)

Owner: user-exp-improver. Requested by Allen (09-30): «pac 动态开节点的功能请你开始设计». The use case: an astra design node finishes, and only then do we know that the work splits into, say, three parallel lines that later join for integration.

## 1. What exists today

Implementation paths now live under
`src/hyprial/daemon/impl/pac/`: workflow contracts in `contracts/`, atomic
publication in `workflows/expansion/`, writer dispatch in `graphs/authority/`,
and managed Git effects in `actors/worktrees/`. Older short paths below are
historical design references, not modules to recreate.

- Structure freezes at activation. `_require_version` raises `PAC_GRAPH_FROZEN` once `activated_at`/`closed_at` is set (graph.py:173-190). `workflow run` graphs are compiled already activated in one transaction (workflow_graph.py:155-305), so they are never editable.
- Three more things pin the node set, apart from the freeze:
  - **Roster digest.** It covers every node's id/role/owner/actor/location (workflow_roster.py:27-81) and is immutable on worker receipts (migrations.py:859-873). A changed node set raises `WORKFLOW_WORKER_RECEIPT_MISMATCH` (lifecycle.py:722-729).
  - **Spec digest.** Delivery reads each node's task and role from the spec file checked against `specification_digest` (workflow_graph.py:46-60).
  - **Input tokens.** A node's token hashes its predecessor set (workflow_graph.py:333-381), so a new edge into a live node withdraws its current request (workflow_runtime.py:751-823).
  - Subscriptions also assume frozen graphs (subscription.py:1-6).
- Graphs have no parent/child relation. Graph-to-graph links exist only in the overview, inferred from names (overview.py:219-245).
- **Completion.** Flagging a node stores `reasonRef` and `outputText` (64 KiB max). A graph closes on its `end` node or when everything settles. `on_failure` is terminate, hold or continue. Deadlines are per node (`timeout`); there is no graph deadline.

**Conclusion:** unfreezing a running graph would mean re-deriving rosters, specs and tokens for live workers, which touches the pinned places above. v0.1 therefore does not mutate the parent graph. The plan node's output becomes a **child graph** attached to a **placeholder node** in the parent. Each graph stays frozen and keeps its own roster and spec, and `compile_workflow` is reused as is.

## 2. How it looks in YAML

```yaml
version: 2
name: service-connect
nodes:
  - id: design
    role: plan
    launch: {harness: pi, provider: openai-codex, model: gpt-6-astra, cwd: /work/sc}
    expands: build                 # this node's completion must carry an expansion for 'build'
    task: "Design, then split the work into parallel slices; return the expansion."
  - id: build
    kind: expansion                # placeholder; PAC owns it
    after: [design]
    timeout: 8h                    # cap for the whole child graph
    limits:
      maxNodes: 12
      tiers: [fast, strong]        # worker tiers the planner may use
      owners: []                   # borrowed owners the planner may name (default: none)
      cwdUnder: [/Users/bobadmin/h2b-work]
      worktree: {repo: /Users/bobadmin/h2b-work/service-connect, base: feat/service-connect}
  - id: integrate
    after: [build]
    launch: {tier: strong, cwd: /work/sc}
```

`limits` is the human's authorization boundary. A model writes the expansion,
and it can never exceed what the person who ran the parent allowed. The daemon
loads the organization ceiling only from `<HYPRIAL_HOME>/pac-expansion.json`,
snapshots it into the frozen parent, and intersects it with any later
tightening before a new acceptance.

## 3. The planner's side

The plan node completes with an expansion: a normal format-2 workflow document containing only `nodes`/`edges`.

- CLI: `hyprial workflow complete <g> design --request-id … --reason-ref … --expansion build.yaml`.
- MCP: `workflow_complete` gains an `expansion` string (YAML/JSON), because sandboxed workers can't write files.
- Dry run: `hyprial workflow plan --expansion-for <graph>:build build.yaml` validates without completing.

**Validation happens before the flag is set.** An invalid expansion rejects the completion with a precise error (`PAC_EXPANSION_INVALID` plus the offending field), and the node stays `requested`, so the planner can fix and retry within its timeout. Checks:
- the schema is format 2;
- node count ≤ `maxNodes`;
- every `launch` uses an allowed tier, or an explicit model that resolves to an allowed tier (`matrix.tier_for_model`);
- borrowed `owner`s are in `owners`;
- every `cwd` is under `cwdUnder`;
- no node timeout exceeds the placeholder's remaining time;
- no `approval` or `end` nodes, and no nested `expansion` in v1;
- the DAG is acyclic.

## 4. The engine's side

- On a valid completion, PAC compiles the expansion as a **child workflow graph** in the same transaction that flags the plan node:
  - `operation_key = expand:<parent>:<placeholder>:<generation>`, so it is idempotent;
  - new columns `parent_graph_id`, `parent_node_id`;
  - creator and `escalate_to` inherited from the parent, `on_failure: terminate`;
  - the spec is stored under the state dir, digest-pinned like any spec.
- The placeholder node becomes `requested`, owned by the PAC system principal, and its deadline is `timeout`.
  It is physically stored as a task with
  `workflow_nodes.node_kind='expansion'`; public workflow projection renders
  it as expansion/system-owned. It has no actor, inbox turn, or human
  completion authority.
  - Child closes `completed` → the placeholder flags itself. `reasonRef` is the child graph id; `outputText` is a digest of each child node's reasonRef and output head, so `integrate` sees what each slice delivered.
  - Child closes `failed` → the placeholder fails with `pac:expansion-failed:<child>/<node>`, and the parent's `on_failure` applies as for any node.
  - Placeholder deadline expires → the child is cancelled, and the placeholder fails with `pac:deadline-expired`.
  - Parent cancelled or terminated → the child is cancelled (cascade), and its workers are cleaned up through the existing cleanup intents.
- A `worktree:` in `limits` makes PAC prepare one git worktree per child execute node before launch (`git worktree add <managed dir>/<child>/<node> -b pac/<child>/<node> <base>`). Its path becomes that node's `cwd` unless the planner set one. PAC removes clean worktrees at close and keeps dirty ones with a warning.
  - This is what made the service-connect fan-out manual today: a sandboxed planner can't create worktrees, and workers can commit but not push.
  - Integration reads the slice branches through the shared object store (`git merge pac/<child>/s1 …`), which works inside a same-repo sandbox.
- Remote owners work unchanged: a child node owned remotely is dispatched over `hyprial/v1/pac-workflow` like any other.

## 5. What people see

- `workflow status <parent>` shows the placeholder with its child graph id and the child's node states inline. `workflow list` marks children with `↳ parent`.
- Overview: the child's PACs sit on the parent's task line (no name guessing), with an `expand` link from the plan node to each child node and from the child's sinks to the placeholder's successors.
- Alerts: the creator gets one alert when an expansion is accepted (child id, node count, tiers used), and failures bubble as usual.

## 6. Not in v1 (on purpose)

- Mutating a running graph in place, or adding nodes to a graph that isn't an expansion placeholder.
- Nested expansions (a child plan node that expands again) and re-planning loops. Integration failing is a normal node failure. v2 could allow `generation` > 1 by letting the placeholder accept a second expansion after a failed child, under the same limits.
- Automatic pushing and PR creation; those stay with the owner.

The fixed expansion deadline is
`min(authored placeholder deadline, acceptance + placeholder timeout)` and
caps every pending request and start. Parent `hold` keeps the failed result for
operator action; it does not authorize reset or generation-2 replanning.
Managed worktrees retain branches and commits for explicit integration; dirty,
locked, replaced, or ambiguous worktrees remain as visible cleanup debt.

## 7. Implementation outline (for hyprial-cli-developer)

1. Schema: `expands` and `kind: expansion` + `limits` (workflow_schema.py); the expansion document validator; `--expansion` on CLI complete and MCP `workflow_complete`; `plan --expansion-for`.
2. Store: a migration adding `parent_graph_id`/`parent_node_id` to `workflow_graphs`, and a placeholder state machine in `workflow_runtime._project` (requested ↔ child state), cascade cancel in `close_workflow`.
3. Reactor: validation plus child compile inside the flag transaction for `expands` nodes.
4. Worktree preparation and cleanup in `ActorCoordinator.reconcile` (lifecycle.py) for nodes whose placeholder has `limits.worktree`.
5. Status, list, overview and alerts.
6. Tests: an invalid expansion rejects the completion, and the node stays requested; each limit (tier, owner, cwd, count, timeout) is enforced; the child closes completed → the placeholder flags with a digest → the join runs; the child fails → the parent terminates; the placeholder deadline cancels the child; the parent cancel cascades; the expansion is idempotent under a retried completion; worktrees are created per node and dirty ones are kept.

## 8. Decisions (Allen 09-30: «同意，按你检验的方案开始做»)

1. **Org-default limits**, which a graph may tighten but never loosen: tiers fast+strong, no borrowed owners, maxNodes 12, cwdUnder the configured work root (hq: /Users/bobadmin/h2b-work). A placeholder without `limits` gets the defaults, and a wider value than the default is a schema error.
2. **Per-node git worktree preparation is in v1** (only when `limits.worktree` is set).
3. **A failed child follows the parent's `on_failure`** (use `hold` on the parent to force a human re-plan).

## 9. Former open questions

1. Should `limits` be required on every placeholder, or should there be org defaults (e.g. fast/strong only, no borrowed owners, 12 nodes)?
2. Is per-node worktree preparation part of v1? It's what makes parallel coding practical, but it ties PAC to git.
3. When a child fails, should the parent's `on_failure` apply as usual (proposed), or should a failed child always hold the parent so a human can re-plan?
