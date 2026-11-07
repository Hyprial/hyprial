# PAC node context — v1 (§B, first wave)

```sh
hyprial workflow inspect <graph> --node <node> --context --json
```

This is a **node** context, not the old graph-wide `--for` view. Graph-wide
consumers use `workflow events --snapshot --json`. The success JSON is pinned by
[`../events/schema.json`](../events/schema.json), `$defs/context`.
Only public CLI JSON is the consumer boundary; no PAC imports or SQLite access
are required. Non-JSON output is a human summary, not a parsing contract.

## Fields

| Field | Meaning |
|---|---|
| `ok`, `schemaVersion`, `type` | `true`, `1`, `context` |
| `graphId`, `nodeId`, `version` | Run, addressed node, structure version |
| `journalId`, `cursor` | Same journal identity/watermark as the underlying public snapshot |
| `owner`, `kind`, `briefRef` | Node owner, task/clock kind, work-description reference |
| `flag`, `setBy`, `setAt`, `reasonRef` | Current node's flag and attribution; unset attribution is explicit null |
| `completedCount` | Count of accepted **set facts** for this node; reset does not decrement it |
| `currentActivation` | Current recorded task request: `activationId`, `nodeId`, `owner`, `round`, `requests`; otherwise null |
| `predecessors` | Forward predecessors, sorted by nodeId; each has nodeId/owner/briefRef/flag/setBy/setAt/reasonRef |
| `active`, `closed` | Graph activation/closure `{at, by}` or null; closure does not set any node flag |
| `deadlineMs` | **Present only for the addressed clock node**, not task successors |
| `actor` | Reserved but **ABSENT** in this wave; not null, `{}`, or an invented actor |

`setAt` and `deadlineMs` are epoch milliseconds. Missing result references are
null; they are not synthesized from notification text. Only forward inputs are
predecessors: a back edge is a re-entry request, not a prerequisite.

The whole result is assembled from **one snapshot at one cursor**. Predecessor
flag/reasonRef/setBy/setAt, own flag/count, structure/brief refs, activation and
closed metadata cannot come from separately advancing reads. The context reader
is read-only, does not contact the daemon/inbox, and never dereferences a brief
or result reference. A caller may separately resolve a reference through its
own authorized document tooling; PAC does not read or interpret that body.

## Delivery carries a summary (send-links WP5-D)

PAC notices, routine tasks and remote offers are delivered to workers through
`InboxDeliveryIoAdapter.deliver`, which requires a `summary`: the notification
row and the zenoh frame carry it, and the worker context sees only the summary
plus `bodyBytes`.  A local `workflow-request` summary is derived from the stored
event text before the task is rebuilt; a remote node derives it from the
rebuilt full text, so the two are not guaranteed to agree (still pending an
Allen ruling).  See `../wire/send-links.md` §11.

## Completed count is not current activation

After `demo` is set, `pm-confirm-2` can have `completedCount=0` and a requested
`currentActivation.round=1`; its predecessor carries demo's result reference.
Transport acknowledgement is not a completion.

After the first completion of `pm-helper`, a back edge can request round 2 while
its old flag remains true. Context then has `completedCount=1`, `flag=true`, and
`currentActivation.round=2`. The owner's reset clears attribution/flag, **not**
the new request or completed count. Its next accepted set increments the count
to 2 and completes that requested round.

Repeated requests for an unfinished `(graph,node,round)` coalesce under one
opaque activation identity. Withdrawal removes the withdrawing edge's request;
with no requests remaining, currentActivation is null. Roots without a recorded
request do not get invented activations. This task-request identity is **not an
actor incarnation**, nor proof that a worker exists or is doing work.

## Clock and actor scope

A task with zero, one, or multiple incoming clocks has **no deadlineMs field**.
Reading the clock node itself returns its own deadline. Overdue is a notification
fact, not task completion and not a general task-deadline binding model.

Registered agent short names are not actor-node declarations. No actor nodes
exist in this wave, so `actor` must be absent even when the owner is a known
agent. The schema rejects a present actor field. Positive actor_name/launch_ref/
assignment information waits for the real second-wave actor implementation;
there is no stub-based acceptance for it.

## Errors and verification

Unknown graph/node returns `ok=false` with `PAC_GRAPH_NOT_FOUND` or
`PAC_NODE_NOT_FOUND`, exit 1. The graph-only/old `--for` form is rejected by the
CLI parser. Context is an observation: a closed graph is readable, with null
currentActivation and preserved completed facts.

`tests/daemon/pac/authority/test_pac_context.py` covers the Allen demo→pm-confirm-2 case, back round 2,
repeat/withdraw, two-connection same-watermark reads, actor absence, clock
ownership and no reference/body/daemon reads. `tests/daemon/pac/graphs/test_pac_migrations.py`
checks this contract on a database generated by the **actual slice-1 code**.

The first-wave E2E-020 runner (B1 references + B2 actor-absence contract + C public
subscription) is authorized after CHECKPOINT 3 and now has a combined-runner
pytest guard as well as the isolated CI scenario. Exact full-suite/runner results
are in the PR receipt. B2 is the explicitly authorized absence check, **not**
positive actor lifecycle coverage.
