# Agent-owned bot delivery for user DMs

## Ruling and trigger

Allen, 2026-10-08 (verbatim): 「各 agent 直接用自己的机器人私信，user-proxy 是兜底方案」
("each agent DMs directly with its own bot; user-proxy is the fallback").

The trigger was `k3s-maintainer`: it had the pinned
`k3s-maintainer-feishu` adapter, but a report to
`user:bob.smith@example.com` went through `allen-proxy`.

## Decision

The sender's daemon decides inside `message.send`'s `user:` branch, before
user delivery, but only when the target is exactly `user:<daemon-owner>`.
Every other `user:<x>` target takes the existing receiver path unchanged. The
sender node owns the adapter configuration, secret, and outbound gateway.
There is no receiver-node lookup for the sender's bot.

| Check | Direct agent-bot DM | Existing user delivery |
|---|---|---|
| Authenticated session, or explicit in-process daemon origin, is the same registered local agent | continue | otherwise |
| Target is this daemon owner's `user:` URI | continue | otherwise |
| Agent registry row has exactly one Lark adapter among all pins | continue | zero or several |
| Adapter is configured, online, and has an outbound gateway | continue | otherwise |
| Target owner/user key resolves to an open-id for that exact adapter | continue | otherwise |
| Lark accepts | terminal success | not attempted |
| Lark proves refusal before send | no | fallback |
| Settlement is uncertain | terminal outcome unknown | never attempted |

The identity resolver exposes an owner/user-key plus adapter lookup that does
not require a local `UserProfile`. Resolution order is account override,
union-id binding accounts, then the grandfathered verified legacy observation
for that one adapter. Multiple candidates for one app are a conflict, not a
guess, and an open-id from another app is never used.

The outbound call uses the existing `GatewayIoAuthority.send_owner_dm` bounded
IPC wait with
`<target-key>:agent-bot:<adapter>` as its idempotency key. The text is sent
unchanged: there is no `转述自` prefix because the bot is the agent.

## Durable attempt record

Before native send admission, the sender daemon atomically writes an
`in-flight` row keyed by the `message.send` user-target idempotency key to
`state/agent-bot-attempts.json`. This ledger is separate from receiver-side
`user-deliveries.json`, uses a 30-day retention and 10,000-row target, and
persists the selected adapter, the stable Lark idempotency key, the sender that
began the attempt (audit only) and the time the first native send began. Count
pressure evicts only `definitely-not-sent` rows: an accepted row is what turns
a replay into a duplicate, and in-flight/uncertain rows block fallback, so
those are kept even past the 10,000 target; age retention still bounds the file.
A replay after the 30-day retention finds no row and is treated as a new
message, so it can be delivered again; callers must not reuse an
idempotencyKey after that long. Settlement
replaces the row with `accepted` (including the delivery entry), `uncertain`,
or `definitely-not-sent`.

An accepted replay returns the recorded entry with `duplicate: true` without
calling the gateway. An `in-flight` or `uncertain` replay remains locked to the
recorded adapter and Lark key: it retries only if that adapter is still the
sender's sole Lark pin and is available; otherwise it returns
`SUBMIT_OUTCOME_UNKNOWN`. A `definitely-not-sent` replay stays on the fallback
path, where the existing user-delivery ledger supplies its duplicate result.
A crash after the pre-send write therefore cannot switch the same caller key
to a different app.

The record decides a replay whatever the caller's current eligibility. The
user-target key does not carry the sender, and eligibility can change between
attempts (a replay over IPC without a session, re-registration, a delegated
retry). So an ineligible caller first reads the record: accepted replays as a
duplicate, in-flight/uncertain returns `SUBMIT_OUTCOME_UNKNOWN`, and only no
record or `definitely-not-sent` takes the receiver path. ("Unreadable" means
only that reading or parsing the ledger failed; a failed housekeeping save after
a successful read is ignored.)

When the ledger is unreadable, the subject (the identity the send is on behalf
of, not the transport sender) decides:

| Subject | Unreadable ledger |
|---|---|
| A system service (no agent identity), or this daemon's dispatch service itself | Receiver path. Neither can own a bot record, so a broken bot ledger never silences system alarms. |
| Any canonical agent URI, including an operator send `onBehalfOf` an agent | `USER_DELIVERY_UNAVAILABLE`: it may be replaying what that agent's bot already sent. |

This fails closed for one availability case: a dispatch relay carrying
`onBehalfOf` = an agent has that agent as its subject, so it is refused while
the ledger is unreadable. It can never double-send (its operation id is
`forward:<messageId>`, which shares no bot record), so it costs only
availability until the ledger is readable again. We keep it rather than exempt
relays by operation-id format (review 1182).

Routing is sticky in both directions. Once an eligible sender makes any
pre-send fallback decision (including no/multiple pins, adapter/status/gateway
unavailable, unresolved open-id, or identity conflict), the daemon durably
records `definitely-not-sent` with the reason before calling the proxy; replay
stays on the proxy even if bot eligibility later improves. Conversely, once a
bot attempt is in-flight/uncertain, a later Lark or gateway refusal cannot
downgrade it and cannot cross to the proxy.

The repository's `docs/reference/p4-squire.md` documents that the stable Lark
provider UUID suppresses duplicates only inside the provider's finite
idempotency window, but does not state its duration. This path therefore uses
the review's conservative one-hour window, measured from when the first native
send began (not from the uncertain settle, which can trail the POST by the call
timeout). An in-flight/uncertain attempt at or beyond one hour is not re-sent
and returns `SUBMIT_OUTCOME_UNKNOWN`.

## Eligibility

Eligible senders are only authenticated `session` callers, plus an explicit
in-process `daemon` origin that IPC parameters cannot manufacture, whose
`sender == subject` resolves to a registry row owned by this daemon's owner and
node. In default observe mode, an IPC caller that claims an agent URI without
`sessionRef` remains ineligible. Direct delivery additionally requires exactly
one Lark pin; unrelated non-Lark pins are ignored. The following remain ineligible: operator
`user:<owner>`, inbox/quota watchdogs, daemon and routine alarms,
`dispatch-coordinator`, adapter/channel callers, remote or transfer-hosted
agents, unregistered agents, and local agents without a pin.

## Outcomes and telemetry

| Outcome | Delivery result | Fallback |
|---|---|---|
| accepted | accepted entry with `nativeMessageId`, `via: agent-bot`, and `adapter`; replay adds `duplicate: true` | no |
| definitely not sent | no direct entry; existing user delivery produces the result | live proxy, then Squire |
| uncertain | refused entry with `SUBMIT_OUTCOME_UNKNOWN` and `outcomeKnown: false` | no |

Only an explicit Lark pre-acceptance allowlist is definite: documented invalid
`receive_id` (230001 with matching detail), app visibility/user reachability
(230013), bot/scope permission refusal (230006, or 230027/99991679 with parsed
missing scopes), and pre-acceptance rate limits (230020/99991400). The existing
`map_lark_send_error` classification supplies permission reasons. Every other
coded response, a timeout, an unclassified transport exception, or a Lark
failure without a response code is uncertain.

`GatewayIoAuthority` raises `GatewayAdmissionRefused` only when its mailbox
proves native I/O was never admitted (closed, capacity refusal, or typed actor
admission refusal). Only that exception permits fallback. A generic
`RuntimeError`, including one whose text says “gateway closed”, is uncertain.

Only an eligible registered local-agent sender enters evaluation and emits a
`user-delivery.agent-bot` event with only
`messageId`, `conversationId`, `sender`, `owner`, `adapter`, `outcome`, and
`reason`. System services, operator, dispatch, remote, and delegated senders
emit no such event. Telemetry never records message text, open-id, secrets, or
credentials.

## Unchanged behavior

There is no new CLI command, flag, config key, inbox, conversation authority,
or durable-message authority. The feature is on by default. The existing
user-proxy/Squire request, attribution, receiver-node policy, receiver ledger,
and idempotency key are byte-for-byte unchanged for every non-owner target and
whenever owner-direct delivery is ineligible or definitely not sent.
`profile_open_id` keeps the base receiver resolution order: first override,
first binding account, then `profile.owner_open_id`; strict conflict and
verified-legacy-row handling exists only in the new sender-side
`owner_open_id`. Agent-to-agent and route delivery are unchanged.

## Tests

- pinned local agent: exact open-id, raw text, deterministic key, no fallback,
  and direct-entry metadata;
- no app-qualified open-id, stopped adapter, or known Lark refusal: fallback;
- accepted replay: recorded duplicate entry and no second gateway call;
- timeout followed by an offline replay: no fallback and outcome unknown;
- crash after `in-flight`: durable replay is uncertain and cannot cross apps;
- definite refusal replay: remains on the idempotent fallback path;
- documented pre-send codes versus other coded errors: fallback versus unknown;
- typed gateway admission refusal versus untrusted RuntimeError text;
- zero, one, and multiple Lark pins while ignoring unrelated pins;
- two app accounts: only the pinned adapter's account is used;
- watchdog/service sender: old path and no agent-bot telemetry;
- existing sender-attribution, identity, routing, inbox, alarm, and Squire
  focused suites remain green.

## Deviations

None. The direct SDK gateway is built from the pinned adapter's existing local
configuration and secret only after the adapter runtime reports that adapter
online; this is the existing bounded outbound `GatewayIoAuthority`, not a new
transport or credential path.
