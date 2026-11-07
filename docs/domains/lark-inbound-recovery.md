# Lark inbound message recovery

The Lark long connection (`lark.ws.Client`) delivers `im.message.receive_v1`
events exactly once over the websocket: **events are never replayed**. Any
message sent while the socket is down — or whose delivery failed after the
event arrived — used to be unrecoverable: the daemon log kept only
`send.received` metadata, never the body. This note describes the three
layers that close that gap.

## Layer 0 — detect a silent inbound stream

Process liveness is not websocket liveness, and a quiet chat is not a failed
websocket.  The worker therefore keeps two independent timestamps:

- `lastEventAt`: the last business `im.message.receive_v1` callback;
- `lastTransportAt`: the last websocket control or data frame (including
  PONG), which continues to move when no user sends a message.

After 15 minutes without a business event the worker actively repeats the
official SDK's credentialed endpoint-discovery HTTP request and performs an
idempotent recent-history sweep. Endpoint discovery has a hard 10-second
deadline and the bounded multi-chat sweep has its own 60-second deadline: even
an HTTP black hole cannot wedge the health loop. The result is combined with
transport activity and reconciliation evidence:

Ready/reconnect callbacks enter a single-flight coordinator. While one history
sweep is active, any number of additional reconnect callbacks set one pending
bit instead of creating waiting threads; after the current sweep, exactly one
final sweep covers the latest reconnect. Every pass has the same hard history
deadline. Until that final pass succeeds, telemetry remains `checking`—health
ticks, PONGs, and business callbacks cannot overwrite it with a false healthy
baseline. A timeout or incomplete result transitions directly to `stale` and
exit 75 for supervised restart. That rebuild decision is terminal for the
current worker process: later reconnect requests, PONG/event activity, and a
successful in-flight sweep settlement cannot publish `checking` or `healthy`.
Telemetry snapshots enter a bounded, coalescing queue and one daemon dispatcher
publishes them in sequence after an epoch check. Callback re-entry cannot
overtake that consumer, and a terminal event clears queued non-terminal work.
Telemetry is best effort: rebuild/exit never waits for a slow or blocked sink,
and only the replacement worker may establish a new healthy baseline.

| Observation | Classification | Action |
|---|---|---|
| no business event, WS frames current, REST healthy, no missed history | normal idle | remain online |
| WS frames current but history contains unseen inbound messages | `stale` event path | redrive once, then rebuild |
| history sweep is partial, capped, or fails for any chat | `stale` history probe | fail closed and rebuild |
| REST/credential/network probe fails | `stale` | rebuild worker/websocket |
| REST healthy, but WS frames stopped | `stale` | rebuild worker/websocket |

Rebuilding exits only the adapter worker; daemon desired-state reconciliation
spawns a fresh SDK client, restores the subscription, and the first successful
connection runs the same history reconciliation described below.  The stale
transition is emitted to `state/logs/daemon.jsonl` as structured event
`lark.inbound.stale`; recovery is `lark.inbound.healthy`.

`hyprial daemon status --json` and `hyprial adapter status NAME --json` retain every existing
adapter key and add `processRunning`, `streamHealth`, `connectedAt`,
`lastEventAt`, `lastTransportAt`, `lastProbeAt`, and `reason`.  A stale stream
has `status=stale` and `online=false` even during the short interval where its
process is still alive. During deadline-bound reconciliation it has
`status=checking` and `online=false`; Doctor reports a warning rather than
healthy. `hyprial doctor --json` adds a `lark-inbound` failure for stale state.
This is an additive status contract: on-disk desired state,
`schemaVersion`, and the historical `provider=lark` key are unchanged, so an
older binary can still read all persisted state.

Status and Doctor also expose the recovery boundary as additive fields:
`knownChatsRecovery=supported`, `unknownFirstChatRecovery=unsupported`, and
`chatEnumeration=not-implemented`. A healthy stream means its transport and
the bounded history checks for **known chats** are healthy; it does not mean
that a first message from an unknown chat can be recovered. In particular,
chat enumeration is not implemented, and the platform does not provide a
reliable contract for discovering every previously unknown bot DM. Doctor
keeps stream liveness as an `ok` result when appropriate, but repeats this
coverage limitation in both `detail` and `metrics`.

The defaults can be adjusted for deterministic tests or unusual deployments
with `HYPRIAL_LARK_STALE_AFTER_SECONDS` and
`HYPRIAL_LARK_HEALTH_CHECK_INTERVAL_SECONDS`; the probe deadline uses
`HYPRIAL_LARK_REST_PROBE_TIMEOUT_SECONDS`, and the history deadline uses
`HYPRIAL_LARK_IDLE_RECONCILE_TIMEOUT_SECONDS`. Values must be finite and within
safe ranges: stale 60–86400s, tick 1–300s, REST deadline 0.1–120s, and history
deadline 1–300s. Invalid, non-finite, underflowing, or oversized values fall
back to 900, 30, 10, and 60 seconds. None changes persisted configuration. The
history window is computed from these effective settings plus a conservative
restart/retry margin, with a default floor of 1800 seconds, so the oldest
message that can precede stale detection remains inside recovery coverage.

## Layer 1 — failure paths preserve the body

Every inbound failure now records a **dead letter** in the adapter state
store (the shared `adapters.sqlite3` database, table `dead_letters`,
namespaced by the `adapter` column), including the full (truncated)
message text, routing metadata, a machine-readable `reason`, and an error
`detail`:

| `reason` | Trigger |
|---|---|
| `forward-error` | harness IPC / state / provider failure while forwarding |
| `no-target` | DM with no pin, no mention, no resolvable reply route |
| `route-unavailable` | reply route points at an actor that is gone |
| `normalize-error` | event could not be parsed (non-text content, missing fields) — best-effort references plus a safe placeholder |

Properties:

- keyed by **native message id** (re-processing the same lost message
  upserts, it never grows the file, and only the first insert counts as a newly
  missed message for health/rebuild decisions);
- text truncated to 2000 chars, detail to 500, at most 500 letters retained
  (oldest evicted);
- holds message content and routing metadata only — never app credentials;
- group messages that never addressed the bot (`ignored`) are *not*
  dead-lettered — that is policy, not loss;
- a successful forward clears the letter for that native id atomically
  inside `record_inbound`.

Every newly created row also produces a body-free structured log event on the
shared Lark adapter JSONL route (`component=adapter`, `event=dead-letter`). The
event contains the reason and native message/chat/conversation references, but
never the original text, error detail, or sender. Repeated upserts remain one
log event. `hyprial log --component adapter` therefore exposes failures alongside
all other component logs without a dead-letter-specific command.

The production worker watches the current fixed one-hour UTC window. When its
unique dead-letter count reaches `HYPRIAL_LARK_DEAD_LETTER_ALERT_THRESHOLD` (10 by
default, bounded to 1–500), it sends the default operator route a single
count-and-reason-distribution notification. The per-adapter/window claim is
stored in SQLite, so process restarts and concurrent writers do not create an
alert storm. Notification failure releases the claim for a later write to
retry. This mechanism deliberately does not replay messages or create a
dashboard.

## Layer 2 — recovery by native id

`LarkAdapter.recover_message(message_id)` pulls the original body back from
the server with `im.v1.message.get` — the same endpoint the adapter already
used for quoted context — and re-drives the normal inbound pipeline
(routing, harness custody, OnIt ack). Outcomes: `forwarded`, `duplicate`,
`not-found`, `error` (provider errors propagate).

Two recovery-specific behaviours:

- the REST `Message` model carries **no `chat_type`**, so recovered messages
  reuse the chat type observed from live traffic (`chatTypes` map in the
  state store). If the type is unknown the message is *not* guessed to be
  p2p (that would misroute group traffic to a pin); it is dead-lettered as
  `no-target` and becomes recoverable once the chat type is known;
- recovery never sends routing guidance replies — no spam on replay.

## Layer 3 — reconnect reconciliation

On every websocket reconnect (first connect = `on_ready`, later connects =
`on_reconnect` in `LarkEventStream`), the single-flight worker coordinator
sweeps recent history off the SDK callback thread
(`LarkAdapter.reconcile_recent`):

- scan set: chats with prior inbound activity (request correlations + dead
  letters) — not every chat the app can see; neither unknown DM discovery nor
  group enumeration is implemented;
- window: `lookback_seconds` (default 1800s, dynamically enlarged when health
  timing overrides require it) of history per chat via
  `im.v1.message.list`, newest-first `ByCreateTimeDesc`, seconds-level
  `start_time`, pagination bounded at 20 pages × 50. This covers windows over
  200 messages while prioritizing the newest outage edge;
- only user text messages are replayed (bot messages, images, cards are
  filtered at the SDK seam);
- per-chat failures (missing scope, rate limit) are isolated so other chats
  still redrive, but any partial/failed/capped sweep is surfaced as a generic,
  credential-free `ReconcileReport.errors` entry and fails health closed;
- guidance replies are suppressed during sweeps; unaddressed messages are
  dead-lettered instead.

### API evaluation (`im.v1.message.list`)

- Available in the vendored SDK (`lark_oapi.api.im.v1.ListMessageRequest`,
  `client.im.v1.message.list`), tenant access token supported.
- Scopes: p2p history needs `im:message` / `im:message:readonly` — the same
  scope family the adapter already requires for `message.get`/`reply`.
  **Group** history additionally needs `im:message.group_msg`; if an app
  lacks it the sweep reports a per-chat error and continues (DM recovery is
  unaffected).
- Rate limits (1000 req/min, 50 req/s) are far above this adapter's volume;
  the lookback window and 20-page cap bound each sweep regardless. Reaching the
  cap still returns and processes the newest messages, but marks the sweep
  incomplete so the worker cannot report healthy without full coverage.

## Idempotency (the hard requirement)

Duplicate delivery is worse than loss, so dedup is keyed on **both**:

- `event_id` (`seenEvents`) — platform redelivery of the same event;
- native `message_id` (`seenMessages`) — the same message arriving via any
  mix of live event, `recover_message`, and reconcile sweeps.

`seenMessages` is written only when Harness accepts custody (inside
`record_inbound`), so a message that failed to deliver stays recoverable.
The adapter's inbound lock serializes check-forward-record per process, and
the state store's file lock + merge-on-write keeps multi-process access
crash-safe.

The worker also sends `message.send` with a stable idempotency key derived from
the gateway actor plus native `message_id`. If the daemon durably accepts a
message but its Unix-socket response is lost, a restarted worker's history
replay resolves to the same daemon message instead of enqueueing a duplicate.
