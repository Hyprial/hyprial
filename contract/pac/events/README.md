# PAC public events contract — v1 (slice-2 wave 1, §C; §B schema included)

This is the supported external boundary. Consumers use CLI JSON, not an internal
PAC import or SQLite layout. `schema.json` pins the JSON shapes; `client.py` is an
independent standard-library consumer. It neither imports hyprial nor opens a database.

## Bootstrap and stream

```sh
hyprial workflow events <graph> --snapshot --json
hyprial workflow events <graph> --after <cursor> --journal-id <journalId> --json
hyprial workflow events <graph> --after <cursor> --journal-id <journalId> --follow --json
# Alternatively, one process with a race-free handoff:
hyprial workflow events <graph> --snapshot --follow --json
```

`--snapshot` emits exactly one snapshot JSON line. `--after` emits zero or more
typed event JSON lines, strictly ordered by `seq > after`; it defaults to 0.
`--snapshot` and explicit `--after` are mutually exclusive. Each non-follow drain
is bounded by its initial high watermark, even if writers keep appending. Internal
pages contain at most 256 events. Follow drains, releases its read transaction,
then polls for the next page; no read lock is held during its wait. SIGINT/Ctrl-C
and SIGTERM exit cleanly (including inherited SIGINT=SIG_IGN from background/CI
parents). Closing the output pipe/socket also stops an idle follower without a
new event or heartbeat. An EPIPE during a write is quiet cancellation, not a
second stderr flush error. Stdin is not read: /dev/null stdin remains valid.
The caller's signal handlers are restored when the command returns.
stdout contains only the requested JSON lines; errors are one JSON object
on **stderr**, exit 1. Empty EOF means the drain is complete, not graph completion.

The former `workflow events` dump is removed: `workflow events` is the single public
subscription outlet. The internal flag table's `seq` is a **different legacy
domain**, not a public cursor; do not reuse old flag cursors, even if the numbers
coincide. The hidden projection audit is a diagnostic audit, not a second public
subscription protocol. Old `pac restate` / `pac clock` paths are removed; internal
clock setup uses resident workflow cadence, not a claimed resident v2 tick loop.

## Cursor domain and recovery

- `seq` is global to one PAC journal, not graph-local and not `flag_events.seq`.
  Events from other graphs cause legitimate gaps. Consumers must not infer a lost
  event from a numerical gap.
- Persist `journalId` alongside `cursor` and pass `--journal-id` on resume. A fresh
  database gets a fresh identity. A mismatched identity, negative cursor, cursor
  beyond the current high watermark, or cursor below the graph's `cursorFloor`
  returns `PAC_EVENTS_RESYNC` with `data.resync=true`. Bootstrap again.
- The journal is append-only and has **no retention/pruning in this slice**.
  Migration baselines are the current expiration boundary: an old pre-contract
  cursor cannot claim a structure/history that was never recorded.
- Replaying an event with `seq <=` the last consumed cursor is a no-op. Check the
  graph/journal identity first; a duplicate from a different journal is not valid.
  Persist the resulting projection and its cursor together on the consumer side.
- A snapshot's cursor may exceed its last graph-local event because another graph
  advanced the global journal. This changes no graph state. A stream consumer can
  keep its last graph-local seq safely; it need not manufacture unrelated events.
- `journalId` identifies a database history, not an external backup lineage
  service. This slice does not implement arbitrary SQLite backup/rollback tools.
  Out-of-band restore must not reuse an identity for divergent history; a full
  rollback preserving the identity and later reusing seq cannot be detected from
  an integer cursor alone. An ahead-of-restored-head cursor does fail closed.

## Snapshot

All fields come from **one explicit SQLite read transaction** (including the
cursor). Reads do not migrate, write, inspect an inbox, or dereference a brief or
result reference.

| Field | Meaning |
|---|---|
| `schemaVersion`, `type` | `1`, `snapshot` |
| `graphId`, `version` | Run/graph instance and frozen structure version |
| `journalId`, `cursor`, `cursorFloor` | History identity, inclusive snapshot watermark, oldest valid after-cursor |
| `identityFormat` | OPTIONAL additive field (schemaVersion stays 1): `principal-uri-v1` (every owner and the creator are full principal URIs) or `mixed-legacy` (at least one pre-URI short name remains, pre-rewrite database). Never infer the era from row content. Era changes do NOT ride journal events (the schema-8 rewrite is cursor-neutral by design); a consumer sees the new era on its next snapshot bootstrap |
| `structure` | Graph name/creator, nodes (id/owner/briefRef/kind/deadlineMs), forward/back edges |
| `flags[nodeId]` | flag, setBy, setAt, reasonRef, completedCount (number of true set facts) |
| `active` | `{at, by}` or null; explicit activation freezes the structure |
| `closed` | `{at, by}` or null; explicit owner close, not derived from flags |
| `actors` | Empty in wave 1; **no actor lifecycle implementation or evidence** |
| `assignments` | Current requested rounds folded from recorded notifications and completions |
| `notifications` | Recorded plan/outbox rows including current delivery metadata and overdue notices |

Absent flag attribution is explicit null. Clock `deadlineMs` belongs to that clock
node alone. Snapshot metadata never interprets clock→task as a task deadline.
Notifications are sorted by `(eventId, edge)`; assignments by `(nodeId, round)`;
requests within each assignment by `(eventId, edge)`. These are deterministic
serialization orders, not a replacement for the journal's causal order.

## Event envelope and payloads

Every event has `schemaVersion`, `seq`, `eventId`, `graphId`, `journalId`, `version`,
`type`, `at`, `data`. A journal `eventId` identifies that appended fact; a delivery
payload's `data.eventId` identifies the original triggering notification fact.
An event's `version` attributes its recorded decision: a late migrated receipt
can belong to v5 while current structure is v6. Keep the snapshot's structure
version until rebootstrap; never roll it back (or infer a new structure) from an
outcome's version. Structure changes already require a fresh snapshot.

- `flag_set` / `flag_reset`: `nodeId`, explicit `action`, `actor`, `reasonRef`.
  A set increments completedCount, sets attribution from actor/at/reasonRef, and
  releases requested rounds up to that completed count. Reset clears attribution,
  not the count. No other event type implicitly flips any flag.
- `notification_planned`: eventId/edge/kind/recipient/node/round (when applicable),
  text/sender/at/messageId/deliveredAt/plan. `plan` fixes graphId/version/nodeId and
  every forward predecessor with its flag and flagSetAt/By/ReasonRef. Turn plans
  also carry an opaque `activationId`. New plans start with null delivery fields.
- `delivery_changed`: eventId/edge/messageId/deliveredAt. Update that recorded
  notification; do not mark any task complete. A missing/rejected real send
  acknowledgement leaves the outbox pending; an idempotency key is **not** a
  fabricated messageId. Repeated identical acknowledgement creates no event.
- `graph_activated`: `{at, by}`. Does not start an actor in this wave.
- `structure_changed`: operation plus `resync=true`. Re-fetch a consistent
  snapshot; **historical structural replay is deliberately not provided**.
- `migration_baseline`: a labelled migration/import boundary, never a fabricated
  sequence of historical notification/delivery decisions. Re-bootstrap.
- `graph_closed`: `{at, by}`. Owner closes a task/clock run; clear current
  assignments, preserve flags and completion counts. No actor/end cleanup is
  implied, and a late delivery receipt does not reopen the run.
- `launch_failed`, `actor_up/down/restored/lost/unowned` reserve
  typed outcomes for wave 2. Their actual lifecycle semantics are **not tested or
  implemented here**; a consumer that cannot handle a type resynchronizes or
  reports unsupported, never guesses a reset. Producer decoding rejects an
  unknown tag with `PAC_EVENT_TYPE_UNKNOWN`; malformed metadata/payload JSON
  returns `PAC_EVENT_ENVELOPE_INVALID`. Stream errors stay on stderr.

## Assignment view (no additional authority table)

One requested `(graph, node, round)` has one opaque `activationId`. The round is
counted from set facts at planning time: completedCount + 1. Repeated unfinished
requests in the same round coalesce, rather than inventing additional activations.
An assignment stores its nodeId, owner, round and current request records.

A newer request on the same edge supersedes that edge's earlier request in the
same round. Different requesting edges can remain together. A withdrawal removes
only its edge's outstanding requests, never another edge's back activation. With
no requests remaining the assignment disappears. A later set completes requested
rounds up to completedCount. Reset of the target itself does not release its new
request: a back re-entry can be current while the old flag remains set, and must
survive the owner's reset before redoing work. Nodes with no recorded turn do not
get speculative/root assignments. Transport acknowledgement updates a request's
delivery metadata, not its completion.

## Structure freeze and legacy data

Graphs begin as draft. `workflow run <graph>` is a graph-owner operation at
the trusted local principal boundary. `HYPRIAL_OWNER` takes precedence, otherwise the
owner comes from the same unified home selection as the CLI: explicit `HYPRIAL_HOME`
or default `HOME/.hyprial` settings.json. State and owner must not select different
homes (including an explicitly empty HYPRIAL_HOME, which selects cwd). PAC delegates
to the same `resolve_node_owner(hyprial_home=selected_home)` used for node identity;
it does not parse/normalize owner independently. Acting commands require identity:
invalid or missing identity keeps the resolver's diagnostic in `PAC_OWNER_UNKNOWN`,
not an empty actor and unrelated graph-owner denial. Edit-time naming instead uses
canonical `node_owner_or_none`: a genuinely missing file/key contributes no local
principal; malformed/invalid identity still errors. Both canonical entry points
share environment precedence, strip/fallback, settings parsing and colon validation;
they differ only in how absence is handled. An env-only principal is retained.
Until activation flag mutations are refused
and clock ticks do nothing. Activation is durable/idempotent, does not bump the
structure version, and freezes subsequent node/edge edits; changes need a new
graph. Validation and activation serialize against edits under `BEGIN IMMEDIATE`.

`workflow cancel <graph>` uses the trusted local graph-owner identity, never a
caller-supplied `--by`. Closure is atomic with its journal fact, monotonic and
idempotent, including for drafts. It blocks reactivation, edits, flag mutations,
clock planning and undelivered-notification resends. It does not reset flags,
invent completion, or erase historical outbox rows. A send already in flight may
still be acknowledged after close; that real receipt is retained, remaining sends
stop, and assignments remain empty. This is not actor termination or end-node
automatic close; those belong to wave 2.

Schema v3 explicitly migrates old graph metadata and adds journal identity. Legacy
graphs with flag/clock facts become frozen, attributed to `migration` at migration
time (not falsely presented as an old human activation). A pre-contract graph gets
a migration baseline; the public snapshot derives assignments from its recorded
notification history, **never from its latest structure**. Old missing plan inputs
remain null. The legacy P1 sequence (set a, later add a→b, no notification) thus has
zero public assignments, not a fictional turn. projection audit reports legacy
inconsistency with `ok=false`/exit 1; it cannot certify missing historical inputs.

The diagnostic restatement compares all decision fields (kind, recipient, node,
round, text, sender, at, predecessor plan/version), not just (eventId, edge) keys,
in one read transaction. Delivery metadata is not derivable from flag facts and
is carried by the separate journal/outbox view.

## Verification boundary

`contract/e2e/run-pac-public-events.py` runs **E2E-020 §C only** using an
isolated real daemons, inert inbox identities, and `client.py`. It tests every
suffix cursor, duplicate consumption, cross-graph gaps, draft invalidation,
freeze, back/withdraw/overdue, daemon-down plan→real resend receipt, and follow
bootstrap/SIGINT/SIGTERM/output-EOF, including a deliberately ignored inherited
SIGINT. Neither consumer queries PAC SQLite or imports PAC code. Extra legacy
setup copies an [opaque original-code image](fixtures/README.md), without querying
it, then observes only public CLI JSON: a real positive cursor below the migration
floor resyncs, and old-version real receipts do not lower snapshot version. This
is migration-floor coverage, not pruning/retention coverage. Its pytest wrapper
is `tests/integration/pac/events/test_pac_events_blackbox.py`, which also gates the combined first-wave
B1/B2-ABSENT/C runner after the final-gate authorization.

[§B node context](../context/README.md) and task/clock owner close are added at
CHECKPOINT 3. Positive actor information, actor/start/stop reconciliation,
end-node automatic close and §D real work dispatch remain **not done**. The final
gate is authorized after CHECKPOINT 3; exact full-suite and combined first-wave
B1/B2-ABSENT/C results are recorded in the PR receipt, not inferred from focused
results. C-only passes do not certify that the combined runner reached C.

## Managed workflow request assignments

Snapshots of high-level workflow graphs add `assignmentFormat: workflow-request-v2`.
Their assignments come from current durable workflow requests, with exact full
principal owners and `activationId` equal to the request ID. Superseded inputs,
completed/failed nodes and closed graphs do not remain active assignments. This
lets the PAC-journal routine source wake and deduplicate the actual request.

These snapshots are rebootstrap projections: after a new journal event, clients
reload a snapshot instead of applying the legacy flag-round assignment fold.
The independent sample client enforces this marker. Generic low-level graphs
retain their existing incremental fold contract. Reply bodies and private remote
delegation capabilities are not included in either projection.
