# Message trajectory contract (phase 1 + 2)

Status: approved phase-1/phase-2 contract, 2026-08-16.

## Authority and identity

A trajectory is a query-time projection of existing `hyprial.log.Logger` JSONL
events plus the durable `msg/status` result. It creates no trajectory table,
wire stream, or second logger. `correlationId` is the canonical Harness
`messageId`; both fields MUST be byte-identical on every persisted message-path
event. A native provider id is secondary metadata such as
`nativeMessageId`, never the correlation identity.

Every persisted message-path event MUST contain:

- `ts`: Logger-generated RFC 3339 UTC timestamp;
- `level`, `component`, `name`, and `event`: the shared Logger envelope;
- `messageId` and `correlationId`: the same canonical Harness message id;
- `node`: one stable path-node value from the table below.

Events additionally carry the identifiers named for their event class in the
table. Message bodies, prompts, replies, credentials, and provider URLs MUST
NOT be trajectory fields.

## Node and event vocabulary

| `node` | Persisted Logger event | Additional required fields | Meaning |
| --- | --- | --- | --- |
| `adapter-inbound` | `adapter.inbound` | `conversationId`, `actorId`, `sender`, `target`, `platform`, `nativeMessageId`, `nativeEventId` | Lark inbound was normalized, routed, and accepted by the daemon; the native ids remain secondary metadata. |
| `daemon-send` | `send.received` | `conversationId`, `sender`, `target` | The daemon received the send and assigned its canonical message id before delivery/custody work. This proves daemon acceptance only: it does not prove dispatch to or receipt by a worker; `worker.turn.started` is the first worker-dispatch evidence. |
| `daemon-send` | `send.target_unresolved` | `conversationId`, `sender`, `target` | The daemon retained the message but no local target currently claims it. |
| `wake` | `wake.signalled` | `actorId`, `deliveryId`, `attempt` | An interactive harness transport accepted the formal wake edge; this is not delivery completion. `deliveryId` may be epoch-scoped and MUST NOT replace the stable `messageId`. |
| `wake` | `wake.busy`, `wake.offline`, `wake.failed` | `actorId`, `deliveryId`, `attempt`; `detail` when supplied by the driver | The formal wake attempt did not signal; retry custody remains authoritative. |
| `worker-turn` | `worker.turn.started` | `actorId`, `deliveryId`, `conversationId`, `sender`, `recipient` | A managed streaming worker began this message's native harness turn. |
| `worker-turn` | `worker.turn.completed`, `worker.turn.failed`, `worker.turn.interrupted` | the worker-start fields; non-completed outcomes also carry `failure` | The managed turn closed with the named outcome. |
| `worker-turn` | `worker.turn.skipped` | the worker-start fields; `reason` | The managed worker did not prompt a turn because the durable inbox row was no longer pending. `reason` is `already-consumed` (acked), `expired` (past its TTL) or `terminally-failed` (a terminal failure settlement). System notices are never checked. If the inbox projection cannot be read, the turn runs and the worker log records the diagnostic `worker.delivery_projection_unreadable` (once per failure kind); that diagnostic is not a trajectory node. |
| `script-worker` | `script.ready` | `actorId`, `kind`, `pid`, `hyprialCommit`, `effectiveConfig`, `typesafeSdkVersion`, `scriptSha256`, `startupHash`, `credentialSource` | The packaged worker generation passed its validated ready frame. `credentialSource` is `environment` (injected `TYPESAFE_API_KEY`), `file` (read from `~/.config/typesafe/env`) or null. No credential value or path is persisted. |
| `script-worker` | `script.metric` | ordinary delivery fields when an id is known; `kind`, `segment`, `durationMs`, `ok`, `inFlight`, `maxInFlight`, `queueDepth` | One of the `decode`, `call`, or `send` observations for a Jev call. State, answers, and provider bodies are excluded. |
| `script-worker` | `script.error` | ordinary delivery fields when an id is known; `kind`, `stage`, `code`, `retryable`, `errorType` | Sanitized startup, protocol, decode, or call failure. Unknown delivery fields are omitted rather than fabricated. |
| `delivery-terminal` | `outbox.pruned` | `recipient`, `reason`, `attempts` | The held message left the outbox through the existing prune path. |
| `delivery-terminal` | `inbox.pruned` | `recipient`, `reason`, `createdAtMs`, `receivedAtMs` | An unconsumed inbox row was evicted by the TTL sweep (same node value as `outbox.pruned`). |
| `delivery-terminal` | query-only `message.status.fetched`, `.expired`, `.pending`, `.unknown` | `source=message-status`; known records retain their status fields under `details.record` | Projection of `msg/status`; holder response health remains under status diagnostics and never rewrites a terminal record. It is not appended to a second store. |

Existing phase-0 `terminal.failure` and `alarm.*` records remain feedback
events produced by `AlarmEmitter`, not phase-1 persisted path-node events.
They remain available through ordinary log inspection but are not projected
by `hyprial message trajectory`; the query has no pre-contract identity or node exception.

`agent.harness.handover`, `agent.binding.superseded`, worker lifecycle, and
session lifecycle events are agent-timeline annotations, not message-path
nodes. `hyprial message trajectory` joins matching actor/name annotations within the
message time window into `agentTimeline`; it never assigns them a message id.

## Interactive carrier turn events (send-links, WP5-A)

Interactive carriers (pi attach, codex, claude) report turn boundaries over IPC
and publish the same `turn.started`, `turn.completed` and `turn.failed` names as
the managed headless path, with these differences:

- `source` is `daemon.turns` (the daemon stamps it, never the carrier's own
  `source`);
- `fidelity` is `observed`;
- `sender`, `conversation`, `prompt` and `output` are empty strings: an
  interactive turn is not bound to one message;
- `actor` and `endedAtMs` are daemon-stamped; the carrier's own seq/timestamps
  are ignored;
- `deliveryId` is optional and may be empty;
- every `turn.*` event takes its `seq` inside one shared critical section, so an
  interactive and a managed turn never interleave sequence numbers.

The carrier never replies on its own; at turn end it asks `message.settle_hint`
(`send-links.md` §8) and then acks only what the daemon answers `ack`.

## Query and state projection

`hyprial message trajectory <message-id>` scans active local `logs/**/*.jsonl*`, excluding
`logs/archive-pre-trajectory/`, selects exact contract-identical `messageId`
and `correlationId`, infers the sender from those local facts, and pulls
`message.status` for the same id. The JSON response conforms to
`trajectory.schema.json`.

Every returned node uses exactly one provenance state:

- `completed`: an instantaneous event was measured, a started turn has a
  terminal pair, or status is `fetched`/confirmed `expired`;
- `running`: a `worker.turn.started` has no terminal pair, or status is
  `pending`;
- `unknown`: status is `unknown`, or a required fact cannot be
  established. Unknown never means the action did not happen.

Known timestamps are sorted ascending. A node without a measured timestamp is
placed after measured nodes and keeps `ts: null`; the command MUST NOT invent a
duration or timestamp. Cross-node clocks have no global sequence, so this sort
is display order only (`ordering.authoritative` is always `false`).

When no local event has the exact correlation id, human output MUST contain
`本机日志无此消息`; JSON returns `found: false`, the same message, and empty
`nodes`/`agentTimeline` rather than an empty successful-looking list.

## Retention and rotation policy

This section contracts the implementation as it exists on 2026-08-16; it does
not imply stronger deletion guarantees.

- Undelivered outbox and custody message bodies default to a 12-hour hold
  (`HYPRIAL_HOLD_TTL_MS`) and 100 items per holder (`HYPRIAL_HOLD_MAX_ITEMS`). Expiry
  or capacity eviction records a terminal status and moves the body to DLQ.
- Recipient inbox bodies use the same 12-hour hold deadline
  (`HYPRIAL_HOLD_TTL_MS`): each received-but-unconsumed row is stamped
  `expires_at_ms = received_at_ms + ttl` on this node's own clock, and the
  daemon's periodic reconcile evicts rows past the deadline. Eviction is
  observable via the `inbox.pruned` trajectory event; acknowledged rows are
  not swept and remain until the same message id is reused. Pre-existing rows
  are backfilled once from `received_at_ms + ttl` at schema migration. The
  daemon inbox DLQ still has no automatic purge.
- Lark adapter dead letters are a distinct bounded store: each adapter retains
  at most 500 newest rows, truncates stored message text to 2,000 characters
  and failure detail to 500 characters, and capacity-evicts older rows. It has
  no time-based expiry. Request-correlation rows contain identifiers and route
  metadata, not message bodies.
- Terminal `msg/status` facts default to seven days
  (`HYPRIAL_STATUS_RETENTION_MS`) and are purged by the inbox maintenance tick.
- Structured JSONL logs, including trajectory events, currently have no
  configured rotation or expiry and are retained indefinitely. Logger exposes
  a rotation hook, but production does not install one.
- On the first daemon start after the trajectory-contract upgrade, existing
  `*.jsonl` files are moved once into `logs/archive-pre-trajectory/` and listed
  in its `.migrated` marker. The archive is retained for operator inspection
  but excluded from active log and trajectory queries. An interrupted move is
  resumed from its persisted file plan; migration failures emit a stderr
  warning and `logs.migration_failed` when logging remains writable, but do not
  block daemon startup.

TODO (later phase): define operator-configurable size/time rotation for JSONL
and explicit DLQ body retention with atomic deletion and documented
trajectory degradation. Until that ships, operators MUST treat local logs
and DLQ rows as indefinitely retained sensitive data. Phase 1+2 does not add
an unreviewed deletion policy.
