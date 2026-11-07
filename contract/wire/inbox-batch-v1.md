# Coalesced inbox batch contract (`inbox-batch-v1`)

Status: additive. Existing `message.pending.list`, `message.pending.wait`,
`message.read_body`, `message.reply`, `message.ack`, `message.settle_hint` and
`message.status` request/response keys are unchanged. This document registers
one new IPC method, its request/response keys, the worker-channel exposure, the
`inbox-batch-v1` rendering format, and the notice-consumption change it
requires.

Design: `docs/design/inbox-batch-delivery-2026-10-22.md`.

## Method

`message.pending.batch` — one coalesced, full-body snapshot of the rows the
authenticated caller currently has pending.

- Exposed on the normal IPC allow-list and the bound worker-channel allow-list
  (`kernel.impl.contracts.channels.worker_channel`).
- Identity is the same boundary as `message.pending.list`: the caller's actor
  claim plus its session fence, or the local operator, or an adapter/channel
  identity. A cursor minted for another actor is `CALLER_NOT_AUTHORIZED`.
- It is a consuming **read**, not an acknowledgement: admitted rows receive the
  one-way fetched stamp and fetch receipts. `message.reply` and `message.ack`
  stay per `messageId`, and a transport failure after the call retains every
  message.

## Request keys

Exactly these optional keys in addition to the existing identity fields
(`actor`/`from`, `sessionRef`, `onBehalfOf` as admitted by the caller
boundary):

| key | meaning |
| --- | --- |
| `cursor` | replay one exact frozen snapshot (opaque token) |
| `afterCursor` | capture the successor of a previously consumed snapshot |

`cursor` and `afterCursor` are mutually exclusive; supplying both is
`INVALID_ARGUMENT`. A first capture supplies neither, and no exclusion list is
accepted: the signed predecessor cursor is the only successor boundary, so no
item-count exclusion cap can strand new work behind a larger unsettled
predecessor. A malformed, oversized, unsupported or
ancestry-inconsistent cursor is `INVALID_ARGUMENT`.

## Response keys

Schema version 1. The key set is fixed.

```json
{
  "ok": true,
  "schemaVersion": 1,
  "format": "inbox-batch-v1",
  "cursor": "<opaque>",
  "daemonEpoch": "<epoch>",
  "actor": "agent:owner:machine:name",
  "itemCount": 2,
  "totalBodyBytes": 1234,
  "inlineLimitBytes": 16384,
  "items": [
    {
      "messageId": "m1",
      "deliveryId": "m1",
      "conversationId": "c1",
      "from": "user:owner",
      "to": "agent:owner:machine:name",
      "intent": "request",
      "kind": "message",
      "createdAtMs": 1790000000000,
      "summary": "Please review",
      "body": "full body",
      "bodyBytes": 9,
      "origin": {},
      "class": "human",
      "expectedAction": "reply"
    }
  ],
  "delivery": {
    "mode": "inline",
    "text": "<the only connector injection text>",
    "spillPath": null,
    "spillBytes": null,
    "spillSha256": null
  }
}
```

- `messageId` and `deliveryId` remain identical; `deliveryId` is the per-id
  settlement identity, never the batch identity.
- `items[].class` is one of `human`, `request`, `workflow`, `system`.
  `items[].expectedAction` is `reply` only for an ordinary or human message
  whose intent is `request`; otherwise `ack`.
- `items[].origin` is the same provenance block `message.pending.list`
  returns. The item and its corresponding `delivery.text` block are projected
  from one origin-enriched immutable snapshot, so JSON and text cannot describe
  different provenance. Text marks absent, observed, ambiguous, and unresolved
  provenance as `NOT verified`; only verified provenance may name an owner.
  An unresolved origin sender with `lookupFailed: true` is rendered as
  `NOT verified: identity lookup failed`, in both inline text and spill files
  (and in scalar delivery prompts). Without the boolean flag it remains
  `NOT verified: sender unresolved`. This diagnostic exposes no underlying
  database error or credentials and grants no owner authority.
- Inline mode: `delivery.mode == "inline"`, `delivery.text` is the complete
  rendering, and every `items[].body` is the full body.
- Spill mode (`delivery.text` exceeds `inlineLimitBytes`): `delivery.mode ==
  "spill"`, every `items[].body` is `null`, and `spillPath`, `spillBytes`,
  `spillSha256` are populated. The injection contains per-item headers,
  summaries, body byte counts and the one absolute path; never a body prefix
  and never a truncation claim.
- Empty snapshot: `itemCount: 0`, `items: []`, `delivery.text: ""`. Connectors
  do not inject an empty batch.
- `inlineLimitBytes` is the daemon's configured aggregate budget
  (`HYPRIAL_INBOX_BATCH_INLINE_BYTES`, 1024..1048576, default 16384; an invalid
  value is a startup configuration error). `daemonEpoch` is diagnostics only
  and is not part of cursor identity.
- If the rendering needs a spill file and no private actor state root is
  available, the call is refused with `INBOX_BATCH_SPILL_UNAVAILABLE`; it never
  falls back to a shared directory or an unbounded inline payload.

## Snapshot and cursor rules

The cursor is opaque: canonical JSON encoded as URL-safe base64 and MACed with
the installation's persistent, domain-separated inbox identity
(`hyprial/inbox-batch/v1`). It carries the actor digest, both source
high-waters, the shared capture arrival ordinal, the exact ordered admitted
members, the exact deferred (link-held) members, and — for a successor — the
predecessor's digest. It is self-contained, is not a second durable store, and
creates no batch ledger.

- Replay (`cursor`) reads only that member vector, in cursor order, and may
  only subtract members that became terminal (`replied`, `acked`, expired,
  withdrawn, otherwise settled). A held/nonmember row can never join an old
  cursor on replay.
- A successor (`afterCursor`) admits the predecessor's still-deferred rows plus
  rows above the predecessor's capture ordinal, and never repeats a predecessor
  member, regardless of how many members the predecessor had.
- Before any member settles, a retry returns byte-identical ordered items,
  rendering text, cursor and spill path.
- Neither rendering, spilling, retrying nor replaying acknowledges anything.

## Ordering

Class first (`human`, ordinary request, workflow, system), then the durable
arrival ordinal allocated by one durable authority sequence shared by inbox
rows and real system notices; FIFO within a class. An ordinal is never reused
after settlement, deletion, or daemon restart. Cursor replay matches the full
`(source, messageId, arrivalOrdinal)` identity, so a newer row that reuses a
message ID cannot replace an old cursor member. Sender-controlled `createdAtMs`
is displayed metadata and never used for selection or ordering.

## Notice consumption change (breaking for readers of `drain`)

`message.pending.list` and `message.pending.wait` no longer drain
`system_notices`. Both are doorbells: repeated calls return the same notice
until its explicit per-id acknowledgement, and `message.pending.batch` stamps a
notice fetched without deleting it. This is required so a doorbell poll cannot
delete a notice before the batch snapshot includes it.

A notice is acknowledged by `message.ack` with its `messageId`: after ordinary
inbox aliases fail, the daemon removes that one notice from the caller's own
notice rows (`dismiss_system_notice`, recipient-scoped). One id, one recipient,
no drain, no other message moved. The old drain helper is not called by any
connector, CLI observation or batch path.

## Connectors

Every connector consumes `delivery.text` byte for byte at its own injection
edge and never re-renders: Claude Code MCP channel wake and `harness_read`,
DSH, pi interactive attach, pi harness bridge, the managed worker MCP
`harness_read`, the Codex interactive app-server carrier, and the daemon-managed
model turn dispatch. `hyprial message batch` emits the same text (human output) or
exactly one response object (`--json`). Mechanism/Python-worker hook frames stay
scalar by contract and are not concatenated with model text.

### Codex interactive queued-batch guards and quiet-turn reconciliation (0.5.5.3)

Before enqueueing a recovered or live `FETCHED` batch, the external carrier
I/O driver asks the fenced daemon `message.settle_hint` for each member. Only
successful answers with literal `pending:false` for every member permit
hint-based local cleanup. The endpoint does not include system notices, so cleanup also requires
a successful, well-formed, non-consuming `message.pending.list` with no member
present. The list is a notice veto, not independent settlement evidence. An
unavailable or malformed answer emits a warning and retains the batch unless
an independent valid cursor replay resolves its membership. No
carrier DB flag, fetched stamp, delivery status, or list omission alone proves
settlement. An on-list sweep applies the same check to live `FETCHED` rows.

The carrier actor deletes only the matching generation/attempt while both its
projection and durable row remain `FETCHED`. A durable turn-start mapping wins
over delayed cleanup. For batch deliveries, both enqueue and the native-query
boundary replay
`message.pending.batch(cursor=...)`. Only the daemon renders the remaining
members; settled members are not displayed again. A valid empty replay also
proves that the batch is terminal. Invalid, unavailable, or expanded membership
answers preserve the existing delivery and log uncertainty; no fresh capture
is substituted. The streaming client may implement optional `query_delivery`
to validate a queued delivery immediately before native injection; clients
without it still receive `query(model_prompt())` unchanged.

The original `delivery_id` and `message_ids` remain internal turn/settlement
correlation: `HarnessDelivery.__post_init__` requires the key to be a member,
and the store/runtime/pump use that key. The daemon-rendered `message` is the
only model input (`model_prompt` passes batch text verbatim), so retaining these
internal IDs does not re-show settled members. This also permits filtering out
a settled head without renaming an existing FIFO reservation. Individual
settlement remains daemon-owned. Legacy reoffer and `KEPT` handling are unchanged.

A `TURN_STARTED` with no observable progress for **900 seconds**, inclusive,
becomes `STATUS_UNKNOWN`. Progress means a changed `thread/read` status/items
projection or a new native notification correlated with that thread and turn.
Repeated `inProgress` level notifications, other turns, and failed reads do
not renew the deadline. Each carrier process starts a fresh observation grace;
this is not a maximum native turn age. Native notification tracking retains
sequence numbers for at most 128 recently active thread/turn pairs, not bodies.

`STATUS_UNKNOWN` releases local receive observation and allows the pump to
advance. It does not interrupt the native turn, send an ended report, or
acknowledge a message. The existing tombstone prevents a second injection of
that turn's batch; its messages remain available through the authoritative
inbox. A final observed at the deadline wins; a final arriving after the
terminal transition is ignored without settlement or duplicate enqueue.

Structured worker diagnostics (no message text):

- `worker.carrier.recovery_summary`: `rowCount`, `stateCounts`, `activeRowCount`,
  `generation` describe local recovery before the external inbox check.
- `worker.carrier.fetched_skipped`: `source` (`recovery`, `drain`, `sweep`, `native-query`),
  `generation`, `attempt`, `stage=fetched-settlement`,
  `recoveredState=ALL_MEMBERS_SETTLED` describe a filtered enqueue/sweep.
- `worker.carrier.fetched_filtered`: removed/remaining member counts and `source`.
- `worker.carrier.dequeue.skipped` / `worker.carrier.dequeue.rerendered`:
  `messageId`, `batchMessageIds`, `remainingMessageIds`, `reason`,
  `enqueuedAtMs`, `dequeuedAtMs`, `settledAtMs`, `ackSource`.
  Enqueue/dequeue times are wall-clock epoch milliseconds for this process's
  FIFO admission (not the historical fetched time). `settledAtMs` is the
  carrier's observation time, not the daemon's actual ack time.
  `ackSource={method,observedAtMs}` identifies the settlement evidence:
  `message.settle_hint+message.pending.list` or `message.pending.batch`.
  If a sweep already removed the queued row, its observation is retained until
  dequeue. Missing/non-FETCHED correlation without that evidence uses
  `reason=carrier-not-fetched`, `settledAtMs=null`, `ackSource=null`.
  This clean skip releases the local pump as INTERRUPTED, not FAILED or a
  synthetic completed native turn; it does not retry or report a carrier error.
- `worker.carrier.fetched_removed`: `reason=all-members-settled`, `generation`,
  `attempt` confirm the actor's state-fenced local deletion.
- `worker.carrier.turn_progress`: `observation` (`initial`, `progress`) and
  `deadlineSeconds` describe the idle-clock boundary, never native text.
- `worker.carrier.turn_status_unknown`: `stage=turn-reconcile-deadline`,
  `idleSeconds`, `deadlineSeconds`, `error` describe local observation expiry.
  The deadline is re-derived on every reconcile while the row remains
  TURN_STARTED, so this record repeats when the synthesized status-unknown fact
  cannot be admitted (actor overloaded with a full durable fact relay) and is
  regenerated once pressure clears; the runtime's expected_stage/attempt
  fencing makes a duplicate a no-op.

The launcher uses `$HYPRIAL_HOME/state/logs/workers/codex-<safe(actor)>.jsonl`,
not `state/logs/daemon.jsonl`. `HARNESS_STATE_DIR` overrides the state root.
`safe` replaces each `[^A-Za-z0-9._-]` character with `_`, strips leading and
trailing `.`, and uses `unnamed` for an empty result. Existing recovery records
are `worker.carrier.recovered` (`messageId`, `recoveredState`); transitions are
`worker.carrier.transition` (`fromState`, `toState`).
