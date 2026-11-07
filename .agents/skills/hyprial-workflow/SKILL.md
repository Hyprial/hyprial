---
name: hyprial-workflow
description: Dispatch and supervise subagents through Harness Bridge (hyprial). Use when an agent can access the Harness Network and a task can be delegated or parallelized across independent workers.
---

# Use hyprial workers

Prefer Harness Network workers over the host's built-in subagent mechanism when
the current session is already connected to hyprial. Keep each dispatch bounded,
observable, and recoverable.

## Dispatch through a PAC graph

Use workflow format 2 and run plan before run. A workflow creates a temporary
worker for each executable node, starts it when dependencies permit, and
reclaims its workers when the graph closes. Declare a shared workers entry
and reference it explicitly to share context. Set a full owner URI only when
borrowing an existing actor; that actor is never adopted or reclaimed. A full
remote actor URI can borrow across machines in the trusted tailnet. Both daemons
must support PAC workflow delegation; an unreachable home daemon fails admission.
Run complete/fail and inspect on the actor's own machine with its bound session;
the daemon routes the imported request back to the graph origin. A returned
`returnState: pending` means the outcome is durably queued, not that the graph has
completed. Inspect until accepted/rejected; do not repeat the task. The graph
origin remains the place for graph-wide status, creation, cancellation and history.

CLI calls inside interactive sessions use the file-backed actor/session
binding. Workflow and work-item writes reject invalid or dead claims with
`PAC_PRINCIPAL_UNVERIFIED`; they never fall back to the machine owner. Pi,
Claude and Codex interactive carriers do not use the headless direct-ref marker.
See `docs/reference/pac-cli-principal.md` and `contract/wire/message-sender-auth.md`.

Completion is an explicit flag for the current request with an evidence
reference. Never use await.match, DONE-prefix matching, automatic retries or
implicit reports. Declare approval, report and end nodes and their dependencies.
For rework use explicit back edges and reset your own old flag before acting
on a fresh request. Never reuse an old request ID after an upstream change.

Keep role, first_output_eta and human_gates declarations where relevant. Role
execute requires a headless actor; interactive actors may plan, dispatch or
review. State the branch/worktree and the first verifiable output checkpoint.
Metadata is not an executable approval gate: an approval must be a graph node.

For repository work, declare the PR base in the task. Allen's 2026-10-31 ruling
requires feature PRs to target `release/v<next>`, not `dev`. Plan integration on
that release branch, then promote `release/v<version>` to `dev`. Only release
promotions and `hotfix/<slug>` PRs may target `dev`; do not relabel feature work
as a hotfix. The `pr-hygiene source-branch` gate rejects other sources for `dev`.
Changes under `.forgejo/workflows/` need an Allen merge gate.

Review an exact commit, not a moving tree. An execute node completes with the
full 40-hex commit it produced; its review node reads that SHA in its own
detached checkout or separate worktree (`git worktree add --detach <dir> <sha>`)
and cites the SHA in its verdict. Never point a review at the author's live
worktree: the author may keep committing additively on its branch while the
review runs, and the next round re-reviews the next exact SHA under a fresh
request. Frozen text artifacts that a review judges (contracts, plans, source
proposals) are committed to a separate local docs Git repository and read by
commit; once their node is settled they are never rewritten in place, and a
correction is a new additive commit under a fresh request.

```yaml
version: 2
name: implement-and-review
on_failure: terminate
defaults:
  launch: {tier: strong, cwd: /absolute/worktree}
  timeout: 2h
nodes:
  - {id: implement, task: Implement the approved specification and record test evidence.}
  - {id: review, task: Review the exact commit reported by implement in your own detached checkout., role: review, after: [implement], launch: {tier: strong, cwd: /absolute/review-checkout}}
  - {id: accept, kind: end, owner: 'user:owner', task: Confirm acceptance., after: [review]}
```

```bash
hyprial workflow plan task.yaml --json
hyprial workflow run task.yaml --operation-key <stable-business-key> --yes --json
hyprial workflow inspect <graph-id> --json
hyprial workflow complete <graph-id> <node-id> --request-id <current-request> --reason-ref <evidence>
hyprial workflow fail <graph-id> <node-id> --request-id <current-request> --reason-ref <failure-evidence>
```

A daemon-managed worker without a shell (for example a claude-tier Agent SDK
worker) uses its harness-bridge tools `workflow_complete` / `workflow_fail`
with the same graphId, nodeId, requestId and reasonRef. Either path completes
only the caller's own node.

To hand back the result itself, not only a reference, add `--output-text
<result>` (tools: `outputText`). It is stored with the current request, shown
by `workflow inspect` / `inspect --node`, and carried in the notice to the graph
sender. A worker that cannot write files should put its verdict there, not
`harness_send` it to the graph's dispatcher, which nobody reads. The cap is
65,536 UTF-8 bytes; larger text is refused, never truncated. `reasonRef` stays
required and may point at it as `output:<node>`.

Every PAC notification (graph notices, routine tasks and remote offers) now
carries a `summary`; the notice's `summary` is what the worker sees first. Read
the full task text with `harness_read_body` (or `hyprial message read <messageId>`)
before acting.

The default failure policy terminates the whole graph and reclaims owned
workers. Explicit alternatives are continue (independent branches) and hold
(keep open for owner action). A relative timeout starts at the node's first
request and is not extended by activity, replay, or re-request; an explicit
deadline remains absolute. Budget a human node's timeout for its prescreen, up
to the declared number of asks, and one upstream rework. Borrowed actors are
never stopped by graph cleanup.

## Let a planner fan out work it cannot list in advance (expansion)

Use an expansion when the number or shape of the next steps is only known after
planning (for example one build node per slice the planner discovers). The graph
itself never changes: the planner's completion binds exactly one immutable child
graph under a placeholder that was declared up front.

```yaml
version: 2
name: plan-then-build
nodes:
  - {id: design, role: plan, expands: build, task: Plan the slices; complete with the child graph.}
  - {id: build, kind: expansion, after: [design], timeout: 6h}
  - {id: integrate, task: Integrate the built slices., after: [build]}
```

```bash
# Before completing: validate the child document against the trusted limits (no side effects).
hyprial workflow plan --expansion-for <graph-id>:build build.yaml --json
# Complete the planner node and bind the child in one step.
hyprial workflow complete <graph-id> design --request-id <current-request> --reason-ref <evidence> --expansion build.yaml
```

A worker without a shell passes the same document as the `expansion` string of
`workflow_complete`. The child document contains only `version: 2`, `nodes` and
optional `edges`; child nodes are `task`/`report` with the usual role rules, no
defaults, shared workers, approval/end nodes, back edges or nested expansion.
Limits come only from the machine owner's `<HYPRIAL_HOME>/pac-expansion.json`
(`workRoot`, `maxNodes`, allowed `tiers`, `owners`, `cwdUnder`); a parent may
only tighten them. An invalid expansion is rejected with
`PAC_EXPANSION_INVALID` (`data.field`, `data.reason`) and the planner stays
requested: fix the document and complete again. An accepted expansion is final
for v1: no re-planning; a failing child follows the parent's on_failure policy.
Managed execute nodes get their own git worktree and branch; nothing is merged,
pushed or opened as a PR automatically, so plan the integration as a normal node.

## Inspect built-in routine templates

`hyprial routine templates [--json]` lists packaged templates without contacting a
daemon; `hyprial routine templates show selfdrive` prints the YAML. The selfdrive
source defaults to PAC's own journal; taskwarrior remains an optional source.
Register a derived routine with an explicit coordinator and escalation owner:

```bash
hyprial routine add --template selfdrive --for agent:<owner>:<machine>:<actor> --escalate-to user:<owner> --json
# Optional: --name <name> --interval 15m --idle-threshold 30m
# Optional taskwarrior source: --source taskwarrior --filter "+selfdrive -blocked"
```

`--for` names the routine-owned coordinator and route:self destination.
The registration owner remains the verified caller; it is not impersonated. Template admission starts that headless actor
through hyprial's shared lifecycle manager; daemon restart restores the same desired
actor. `hyprial routine remove <name>` fences cadence first and retires the coordinator.
Same-name or same-coordinator registration fails without replacing state.
File-based registration is `hyprial routine add routine.yaml`; --from may
only echo the verified caller. Omit actor/produces to allocate a routine-owned
agent, or declare actor explicitly to borrow one; template-only flags must not be mixed with that form.

## Install(pi)

把本 skill 装进 pi 的 skill 目录(在 harness-bridge checkout 根目录执行):

```bash
mkdir -p ~/.pi/agent/skills && rm -rf ~/.pi/agent/skills/hyprial-workflow && cp -R .agents/skills/hyprial-workflow ~/.pi/agent/skills/
```

验证:`ls ~/.pi/agent/skills` 能看到 `hyprial-workflow`。

## Check capabilities first

Workflow GUI reads PAC graph/node state and request/evidence references.
Legacy workflows are read-only history. Inspect with workflow inspect
or PAC context/events; these reads do not ACK messages or change flags.
See docs/reference/workflow-node-observation.md for the current contract.

Before using this workflow:

1. Call `harness_whoami`. If that tool is unavailable, run `hyprial agent list`.
2. Treat a successful authenticated identity or a reachable hyprial daemon as
   evidence that this session can use the Harness Network.
3. Check that the host permits `Bash(hyprial *)` and the Harness MCP tools in
   `.claude/settings.local.json`. The MCP tool prefix follows the server name
   the host registered for Harness Bridge; match the wildcard permission rule
   to that prefix, and require `Bash(hyprial *)` alongside it.

If either permission is missing, do not edit the permission file. Ask the user
to approve adding this exact allow-list content:

```json
{
  "permissions": {
    "allow": [
      "Bash(hyprial *)",
      "mcp__harness-bridge__*"
    ]
  }
}
```

If hyprial is unavailable, continue without this workflow. Do not install, start,
or reconfigure infrastructure merely to gain subagents.

## Select execution resources

For a manual Codex or Claude launch, set reasoning with the public
`hyprial agent start ... --effort <level>` option before `--`; it also works with
`--headless`, `--resume`, and a Codex/Claude tier. Omission preserves provider
defaults (DeepSeek/hyprial: Codex `high`, Claude `max`). Codex accepts
`none|minimal|low|medium|high|xhigh|max|ultra`; Claude accepts
`low|medium|high|xhigh|max`, subject to the selected model/client. For DeepSeek
or hyprial, omission injects those defaults; Codex `high` overrides `config.toml`,
so pass `--effort max` when required. Native OpenAI/Anthropic inject nothing.
For declarative launches, use native `args` (`-c model_reasoning_effort="max"`
for Codex; `--effort low` for Claude). Do not combine native effort args with
the public flag. See `docs/reference/harness-model-providers.md` for persistence
and resume semantics; no workflow `launch.effort` field is introduced.

Declare launch: {tier: fast|strong|super} or an explicit harness/provider/model
selection. The provider field is a model vendor. Selection is static: no
availability probe, profile mark or silent fallback changes it. Plan shows the
selected launch. Give concurrent writers separate worktrees and explicit cwd
values; process isolation does not isolate a shared filesystem.

Pick the tier per node by its role (the owner's model principle):
execution nodes (implement, test, fix) on `fast` (kimi / glm / deepseek);
planning and design nodes on `strong` (gpt / claude). `workflow plan` and
`workflow run` repeat this as a `reminders` entry; nothing enforces it, and
the choice stays yours.

```yaml
nodes:
  - id: design
    task: "Write the design for X."
    launch: {tier: strong, cwd: /absolute/worktree}
  - id: implement
    task: "Implement the design."
    launch: {tier: fast, cwd: /absolute/worktree}
    after: [design]
```

The reminder text is operator data (`hyprial config set dispatch.reminder
"..."`, shown by `hyprial agent tiers`); an empty string turns it off.

Workflow normally owns worker creation and cleanup. Only manually start an
actor when it is intentionally external to a graph, then borrow its full URI.
Do not manually stop a borrowed actor as graph cleanup.

Routine mode must distinguish scheduled from source. A scheduled routine
creates a graph per occurrence, runs serially and records skipped overlapping
or missed periods without catch-up. Source mode deduplicates task UUIDs.
Its persistent actor normally coordinates and dispatches child workflows;
it can also execute tasks. Removing the routine retires only its owned actor.
Pause preserves the actor and stops new cadence.

Use ordinary messaging for discussion, status questions and necessary replies.
Dispatch new tasks through workflow/routine, not by sending a second copy of
an accepted task as a chat message. Resume/replay uses the original operation
key and graph facts; a slow reply is not evidence of failed acceptance.

## End conversations without acknowledgement loops

Reading a pending message does not consume it. Use `harness_reply` only when
the peer needs an actual correlated response, because every reply publishes a
new delivery to that peer. For a terminal `reply` or `event` that needs no
response, call `harness_ack` with its `messageId`; from a trusted local shell,
use `hyprial message ack --from <local-agent> <message-id>`. Ack-only advances the durable
consumer position without publishing another Harness message.

Never send a courtesy reply merely to dequeue a message. In particular, when
two agents have completed a goodbye or shutdown handshake, the recipient
acknowledges the terminal reply and stops. `harness_read` is safe for
inspection but must be followed by either a required reply or ack-only before
the delivery leaves the queue.

Only `harness_reply` / `hyprial message reply` closes a request. Sending a new message
to the requester is not a reply: it leaves that request open and the new message
counts against your window, so the request keeps occupying the requester's
window too. A carrier never replies for the worker at end of turn; an injected
request left unanswered comes back as a system reminder in the next turn.

## Keep an interactive session's inbox drained

Mail to an interactive (MCP pull) session counts as taken only when the
session calls `harness_read`. By default it expires 12 hours after it reaches the
holder (`DEFAULT_HOLD_TTL_MS`, overridable with `HYPRIAL_HOLD_TTL_MS`), and
the inbox watchdog alerts the owner at 6 hours with the default policy. Unlike a headless worker,
whose accepted deliveries renew their deadline while it works, an
interactive session has no renewal while it is busy: a long turn that never
reads loses its mail. Therefore every interactive session:

- schedules a periodic drain every 5–10 minutes (a cron or loop that calls
  `harness_read` and replies to or acks every pending item);
- reads the inbox between long steps, not only at the end of a turn;
- runs any command that can take minutes (a test gate, a CI wait, an
  `until` loop) in the background rather than as one foreground tool call,
  so channel notifications can arrive while it runs.

A channel notification is a wake-up, not the message. On every notification call
`harness_read` once and handle the whole batch it returns, in displayed order:
`harness_read` / `hyprial message batch` answer with one `inbox-batch-v1`
response (`message.pending.batch`) whose `delivery.text` is the complete batch:
every item's metadata, priority class and **full body**. Inject that text as-is
— do not reformat it and do not call `harness_read_body` for an item whose body
is already inline. When `delivery.mode == "spill"`, read the one
`delivery.spillPath` file with your normal file tool; the notice itself carries
no body prefix. Then reply or ack every listed `messageId` separately
(`reply` for `expectedAction: reply`, otherwise `ack`): 每个 ID 必须显式
`harness_reply` 或 `harness_ack`, and one batch read never settles anything by
itself. A workflow item still needs its explicit `workflow complete` / `fail`
command/tool; the inbox ack for that item is a separate step and an ordinary
Harness reply does not complete the graph.

One accepted notification carries the batch; count changes and timers do not
produce more copies while a turn is consuming it.

## Inspect system-notice health without consuming notices

`hyprial daemon status --json` exposes `daemon.systemNotices.pending` (addressed
notice rows) and `daemon.systemNotices.quarantined` (bare non-node recipient rows).
Quarantined notices remain durable under their original keys; do not treat them
as acknowledged when pending reaches zero. These counts exclude ack tombstones
and do not fetch or acknowledge messages. `systemNotices: null` means the inbox
projection is unavailable, not empty. A pending count does not prove that its
recipients are live. Use the same isolated profile as the test; never redirect a
test health check to a production daemon.

## Declare the communication contract

Every dispatch message must state:

- the task, working directory, branch or worktree, constraints, and completion
  evidence;
- which product or scope decisions the worker may send directly to an explicit
  developer or channel target;
- which progress, technical questions, and final results must return to the
  delegating agent for forwarding.

Default to sending everything back to the delegating agent. Direct developer
messages require an explicit target and an explicit category of message. A
worker is automatically network-visible and may proactively message an agreed
target when that avoids an unnecessary relay; it must not infer new recipients.
A worker's first send to a non-owner agent mints a link and is briefly held
(`ok`, `queued`, `holdReason`); that hold is not a failure. Wait for the daemon
to release it rather than resending or routing around it.

## Observe and handle timeouts

Use `hyprial agent list` (its runtime column) to verify worker liveness and identity. To observe an accepted
dispatch without publishing another message, query the durable local history:

```bash
hyprial log --correlation-id <correlation-id> --json
```

`hyprial log` reads local JSONL history; it is not a second Harness dispatch and
does not replace the graph flags and evidence references.

Give every task a stated checkpoint or deadline. If no reply arrives:

1. Check `hyprial agent list` and, when applicable, `hyprial log`.
2. If the dispatch was accepted and the worker is live, send at most one status
   request instead of duplicating the task.
3. If the worker exited, preserve the receipt and available diagnostics, report
   the interruption, and decide whether retrying is safe.
4. If the deadline expires, report the timeout and reclaim the worker once it
   is confirmed no longer needed.

## Run a parallel worker pool

Create only as many workers as there are independent tasks. Give each worker a
separate Git worktree and a disjoint ownership boundary (files, package, or
test seam). Never let two workers edit the same worktree.

The delegating agent owns integration: collect correlated results, inspect each
diff and test result, resolve ordering explicitly, and stop every worker after
its contribution is accepted or rejected. Parallelism changes scheduling, not
review, verification, or cleanup requirements.
