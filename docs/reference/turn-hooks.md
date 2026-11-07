# Turn hooks

## Unified daemon bus

The daemon owns one neutral hook bus. Per-agent `hooks.json` selects observed
events and routes each matching event either to a shell-registered consumer or
to a canonical handler agent. Entries require exactly one of `consumer` or
`handler`:

```json
{"schemaVersion": 2, "hooks": [
  {"events": ["turn.completed"], "consumer": "example", "options": {}},
  {"events": ["turn.failed"], "handler": "agent:owner:machine:handler"}
]}
```

`turn.started`, `turn.completed`, `turn.failed`, `delivery.prepared`,
`delivery.dispatched`, and `delivery.settled` are observable. A handler
receives an online-only inbox request containing the event envelope and, for
`turn.*` events, this schema-version-2 payload:

```json
{
  "deliveryId": "...",
  "sender": "...",
  "conversation": "...",
  "startedAtMs": 1,
  "endedAtMs": 2,
  "status": "completed",
  "toolNames": [],
  "outputExcerpt": "...",
  "promptExcerpt": "...",
  "toolCalls": [],
  "collectionMethod": "daemon",
  "fidelity": "observed"
}
```

`turn.failed` also carries `failureCode` when the harness reported one. Prompt
and output excerpts reuse the turn-record secret/path redaction and are bounded
to 1000 characters. The daemon routes handler requests and does not interpret
their replies. Configuration is checked at most once per second and reloads
after the file mtime or size changes. Invalid entries are logged and disabled
individually without stopping the daemon. The stock H1 shell registers no
consumer; the `example` name above represents a consumer explicitly registered
by an embedding shell. The agent-handler lane is created lazily only after a
valid handler entry is loaded. An unknown consumer name is logged and that
entry is disabled. Observe-handler delivery is fire-and-forget on the shared
bounded handler lane; `queue` bounds a consumer lane.

## Delivery events and the intercept (H3)

`delivery.prepared` fires once per delivery when the pump first prepares it,
`delivery.dispatched` when a worker accepts it, and `delivery.settled` when its
turn settles terminally. Observe payloads carry `deliveryId`, `sender`,
`conversation`, provenance labels, a redacted bounded `messageExcerpt`
(prepared only) and `outcome` plus optional `failureCode` (settled only).
`sender` and `conversation` are empty strings when a settlement happens after
a daemon restart. Observers never delay a delivery: publication is a bounded,
non-blocking lane offer.

`mode: "intercept"` is accepted only for `delivery.prepared` and requires a
`handler` (a consumer has no reply channel). The intercept keeps the retired
`before-delivery` semantics: the daemon holds the actor's queue head, sends the
handler one online-only inbox request, and bounds the exchange by `timeoutMs`.
The request is the event envelope with `"mode": "intercept"`, the entry's
`options` verbatim, a `context` object, and a payload carrying the delivery
id, sender, conversation, and the full message verbatim (the handler composes
context for the real delivery). `context` is filled by a shell-registered
provider (`InterceptContextProvider`; the recap consumer serves
`recentRecaps` through it) and is `{}` when none is registered or the
provider fails; the request is built on the invocation thread, so a provider
may read disk without touching the delivery pump. A nonempty reply is inserted verbatim between
`<<<HYPRIAL-HOOK>>>` and `<<<END-HYPRIAL-HOOK>>>`. Timeout, error, empty text,
a reply landing after the deadline, a full intercept pool, and an invalid or
unavailable configuration all fail open: the message is delivered unchanged.
The handler must be a different actor whose own config does not point back.
The intercept requires a harness mechanism that can inject opaque text into
the model prompt; for one that cannot (python_worker mechanisms), or if the
capability probe raises, the daemon skips the intercept and logs a loud
warning once per agent. A marked hook request may bypass an older ordinary
row's per-actor hook hold; ordinary messages retain FIFO head-of-line
ordering. A prepared projection survives failure backoff so one delivery
never runs its intercept twice; terminal settlement releases it.

## Daemon-level consumers (H4)

Besides per-agent `hooks.json` routing, the shell can register daemon-level
consumer lanes (`DaemonHookService.register_consumer`). A daemon-level lane
hears its events for every actor -- no hooks.json gates it -- and the service
publishes those events even when the emitting agent has no configuration.
Events without an actor never match per-agent registrations; only daemon-level
lanes can hear them.

`usage.refreshed` is the daemon-level fact published at the usage-cache refresh
observation point. Its payload is a tick, not a data carrier:

```json
{"collectionMethod": "daemon", "fidelity": "observed"}
```

Consumers read the current readings from the usage cache themselves. The
quota watchdog (design §3, H4) is the first daemon-level consumer: the policy
lives under `biz/impl/hooks/quota/` (shell file `biz/hooks/quota/` in the
design), subscribes to `turn.failed` with
`failureCode == PROVIDER_USAGE_LIMIT` plus `usage.refreshed`, and replaced the
two deleted callbacks (usage-cache refresh observer, failed-turn settlement
observer). Its alert, hold and mute behaviour is unchanged, and the lane never
changes settlement, routing or any agent's harness/model/session.

### Failure codes and agent blocking

A settled failure carries one closed, stable `failureCode`
(`classify_harness_failure` in `daemon/impl/api.py`); vendor prose is never
persisted. Two provider codes must not be conflated:

- `PROVIDER_USAGE_LIMIT` -- the account's quota is exhausted (wording such as
  `usage limit`, `quota exceeded`, `insufficient_quota`). It is permanent for
  the delivery and it also blocks the agent (`provider-quota`) until an
  explicit `agent unblock`; it feeds the quota watchdog alert.
- `PROVIDER_RATE_LIMITED` -- a time-bounded throttle (wording such as `429`,
  `too many requests`, `rate limit`, `rate_limit`, `resource_exhausted`). It
  is permanent for the delivery: one attempt, one notice, no redelivery. It is
  never an agent block -- no block reason maps it, `worker_state._reason` and
  the inbox-surface mapping do not name it, and the quota watchdog ignores it --
  because the window resets on its own and the next message/routine must still
  be delivered. A registry block has no expiry; mapping a 429 onto it would
  silence the agent until a human intervened (2026-10-16 acceptance, resident
  pi provider, item 1).

Authentication, billing, permission, invalid-request and context-exhaustion
codes are likewise permanent for the delivery; only `PROVIDER_USAGE_LIMIT` and
`PROVIDER_AUTHENTICATION_FAILED` block the agent. Transient codes
(`PROVIDER_TRANSIENT_FAILURE`, `HARNESS_TRANSIENT_FAILURE`) keep the
delivery retry budget.

## Legacy recap path (retained until its migration phase)

Headless turn hooks are an opt-in daemon mechanism configured per agent at
`H/agents/<actor>/config/turn-hooks.json`. The daemon sends neutral JSON events
to the configured handler actor and treats every reply as opaque text.

```json
{
  "schemaVersion": 1,
  "events": ["after-turn"],
  "handler": "agent:owner:machine:handler",
  "timeoutMs": 3000,
  "recap": true
}
```

- `events` may contain `after-turn` or be empty. `before-delivery` was
  migrated to the hooks.json v2 intercept above and is rejected here.
- `handler` is a canonical agent URI and is required when `events` is nonempty.
  It must be a different actor, and its own valid turn-hook config must not
  point back to the hooked agent. Use a dedicated idle handler: a sequential
  handler that is busy on ordinary work may consume the whole hook timeout.
- `timeoutMs` is bounded to 1–30000 milliseconds; the default is 3000.
- `recap` controls the append-only log at `state/recaps.jsonl` independently
  from handler dispatch.

No config means fully off: delivery prompt bytes are unchanged and no recap
file is created. The recap log is also opt-in; it is not written merely because
an agent exists. Invalid or unavailable configuration fails open, logs the
mechanism error, and leaves delivery unchanged.

Configuration is cached per agent and refreshed from the config file's mtime
and size. When the agent-home path is derivable, the default-off tick path
stats that config path before consulting the authoritative home receipt. Thus
an absent config does no registry/home-validation or JSON work and produces no
config-error warning. Repeated genuine configuration errors are rate-limited.

Hook requests use the ordinary online-only actor exchange because their value
ends at the configured deadline. On timeout, a same-daemon request row is
retired and stale `turn-hook:*` callback replies are acknowledged; a dead
handler therefore does not create a durable retry stream or a growing callback
scan. A same-node handler without a live connector/worker is offline and is
rejected immediately. A marked hook request may bypass an older ordinary row's
per-actor hook hold; ordinary messages still retain FIFO head-of-line ordering.

For `after-turn`, the request contains the neutral turn record already appended
to the recap log. Dispatch is fire-and-forget. Recap appends and `fsync` run on
a bounded writer queue, never on the daemon tick thread. Overflow drops the
new record, increments a counter, and emits a rate-limited warning rather than
blocking delivery settlement. A nonempty reply is appended as a separate
opaque `annotation` entry keyed by delivery ID, preserving the append-only
file. Persisted excerpts and annotations receive only the neutral path/secret
portability redaction required for agent transfer; the daemon does not
otherwise interpret their content.
