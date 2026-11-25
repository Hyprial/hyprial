# Authenticated message sender contract

Message IPC methods bind an `agent:` claim to the daemon session that owns that
exact canonical actor. `message.send`, `message.reply`, `message.read_body`,
`message.ack`, `message.settle_hint`, `message.pending.batch`,
`message.pending.list`, `message.pending.wait`, `progress.list`, and
`message.status` is catalog-bound and continues to use the same guard. The two
body-read methods (`message.read_body`, `message.settle_hint`)
add no separate identity rule: they follow `message.ack` exactly, including the
local-operator path and the `onBehalfOf` delivery rule. Both refusals happen
before a message is created, read, replied to, or
acknowledged:

- no `sessionRef` for an `agent:` claim: `SENDER_NOT_AUTHENTICATED` (under
  `enforce`; `observe` records it and lets the call through);
- a `sessionRef` that does not own a live claimed actor: the session fence's
  own code, unchanged in both modes: `SESSION_SUPERSEDED` (another session owns
  the actor, the session moved to another actor, or destroy durably retired
  that exact actor/ref pair) or `STALE_SESSION` (no live session owns it). A
  persisted registration after carrier liveness was released is stale. Channel
  children branch on these codes to retire or re-register.
  The error never names the session that does own the actor.

Workflow writes use the PAC caller resolver. Invalid actor/session claims
return `PAC_PRINCIPAL_UNVERIFIED`, including a user claim with a session;
they never fall back to the local operator. Dedicated work-item writes have
been retired. Work documents use ordinary OrgFS identity and permission
rules, not a work-specific caller or owner check; their `owner` field is
document content. See [the current work contract](../work-items/README.md).

The trusted local operator is the exact `user:<owner>` identity without a
session. It may provide `onBehalfOf`, but the delivered sender remains the user;
the claimed actor is visible only as `origin.onBehalfOf`. Adapter and channel
identities retain their gateway path. Daemon-owned sends use the dispatch
service actor through an in-process-only boundary.

For a `user:<owner>` target, an authenticated `session` or daemon-owned caller
whose sender is the same machine-local registered agent may use the Lark
adapter bound to that agent's cell document. The target owner's open-id is
resolved only in
that adapter's app namespace. An accepted direct delivery adds `via:
"agent-bot"` and `adapter` to its delivery entry. A definitely-not-sent direct
attempt falls through to the user-delivery path; an uncertain attempt
returns `SUBMIT_OUTCOME_UNKNOWN` with `outcomeKnown: false` and never falls
back. Operator, service, dispatch, remote-agent, unregistered-agent, and
unpinned-agent callers always retain the existing user-delivery path.

When the target is the daemon's own owner and the owner's user agent lives
on this node (UA2, 2026-11-18), the message is first queued and recorded as
an ordinary message; the agent-bot attempt and the user-delivery path above
then run only as the owner notification, under the same idempotency key and
message id.  An accepted push adds `pushed: true` and the facts above; a
push with nothing configured to push to (`TARGET_UNRESOLVED`,
`USER_DELIVERY_UNAVAILABLE` from a wired daemon) adds `pushed: false` and
`pushCode` and the delivery stays `accepted`, and so does a push the
daemon skips because it confirmed the owner has no push destination at all
(`TARGET_UNRESOLVED`, no receipt wait; see `daemon-request/README.md`), and
so does a push nobody claimed (`USER_DELIVERY_TIMEOUT`) while the daemon has
no evidence of any destination for the owner (no personal org on that node
or no readable record, and no local profile); otherwise a
transient or unknown outcome
(`USER_DELIVERY_TIMEOUT`, `USER_DELIVERY_FAILED`, `SUBMIT_OUTCOME_UNKNOWN`,
any other refusal) is raised as before, with the message already queued, so
the same-key retry pushes again.  I7a (2026-11-24) replaced the receiver's
user-proxy hand-off: the owner's copy is posted through this node's user
agent cell, so the text is recorded once as an interaction (the queued
message).  A user agent address never authors a
stored message: naming this machine's user agent (its address or its bare
name) as `from` is refused with `SENDER_NOT_AUTHENTICATED`, because the user
agent never has a session and the owner's form is `user:<owner>` (another
machine's instance stays `SENDER_UNRESOLVED`); an `onBehalfOf` naming any
machine's name-form user agent address, or this machine's user agent in any
spelling, is stored in `origin` as `user:<owner>`.  Each machine's user agent
has its own id, so another machine's id-form address is an ordinary agent
address here.

Since I3 (2026-11-23) a sender whose cell has an adapter-role member is
not handed to this path at all: the daemon posts the owner's copy to the
chats that cell names, once, and folds the per-chat outcomes into
`pushed`, `via: "cell"` and `pushCode` without ever failing the queued
send (`daemon-request/README.md`, the `message.send` section).

Every newly delivered message has an `origin.via` value:

- `session`: a matching interactive or managed-worker session;
- `operator`: the local `user:<owner>` control path;
- `adapter`: an adapter or channel ingress;
- `daemon`: an in-process daemon-owned send.

`origin`, including `onBehalfOf` when present, is returned by
`message.pending.batch`, `message.pending.list`, and `message.query`.
`harness_read` consumes `message.pending.batch`; pending-list remains an
observation doorbell. Claude Channel wake notifications repeat the same origin
block under `params.meta.origin`, encoded as a compact JSON string (Claude Code
accepts only string meta values); the wake is still only a signal and does not
replace `harness_read`.

Adapter ingress stores a second, adapter-reported origin block alongside
`via`, the same shape for every adapter kind: `chatKind` (`dm` or `group`),
`sender` (`kind`, `platformId`, `displayName`: the platform account and the
display name the adapter itself observed, possibly `null`), `provider` and
`platformMessageId` appear only when the reporting adapter supplied them. A
sender block without a `platformId` and any other chat kind are not carried.
No origin field judges who the person is: an IM sender is whoever owns that
platform account, and the daemon does not mark anyone as the owner. Adapter
ingress adds `origin.platformCreatedAt`, the
platform's own creation time for the message, normalised to ISO-8601 in UTC
(`YYYY-MM-DDTHH:MM:SS.mmmZ`, projected from a millisecond epoch such as Lark's
`createTime`). The key is omitted whenever the platform reported nothing or an
unreadable value: a missing field, a decimal string that is not pure ASCII
digits or exceeds 16 digits, and a non-positive or datetime-unrepresentable
epoch all omit the key, never fail delivery, and never raise. An unreadable
value is additionally counted per process and logged throttled as
`origin.platform_created_at.unparsable`. A missing key means "the platform
said nothing": a reader must not reconstruct or guess a timestamp for it.

The single rollout setting is `HYPRIAL_SENDER_AUTH_MODE`. Its default is
`observe`; explicit `enforce` enables rejection. Both modes emit one
bounded `send.sender_unauthenticated` record per rejected or observed claim,
with the method, claimed actor, count, and caller PID when Unix peer credentials
make it available.

Managed CLI subprocesses resolve one actor/session binding. Headless workers
receive `HYPRIAL_WORKER_ACTOR` and `HYPRIAL_WORKER_SESSION_REF` directly.
Interactive Pi, Claude, and Codex sessions receive `HYPRIAL_WORKER_ACTOR`,
`HYPRIAL_AGENT_SESSION=1`, and an owner-only absolute
`HYPRIAL_WORKER_SESSION_REF_FILE`; the CLI reads the opaque ref from that file.
They do not receive `HYPRIAL_MANAGED_WORKER`, which remains a headless-worker
marker and is not an interactive-session authority.
The CLI forwards the ref only when the actor is the same actor as `--from` (or
when interactive `hyprial message send` defaults an omitted `--from` to that actor).
Operator sends use `--from user:<owner> --on-behalf-of <actor>`. No managed
session reference is placed in process arguments. Every agent claim remains
subject to the daemon's live actor/session fence.

Successful daemon responses for `message.send` and `workflow.start/complete/fail`
include `actingPrincipal`, the canonical user or agent principal accepted by
its live session fence. The CLI copies that field unchanged into successful
JSON from `hyprial message send` and `hyprial workflow run/complete/fail`; it never
reconstructs the value from `--from` or its environment claim. The returned
value therefore reflects bare-alias canonicalization and owner-only relocation.
This field reports attribution; it never substitutes for the live session fence.

`message.send` and `message.reply` take `summary` (the only text a carrier
injects, at most 512 UTF-8 bytes) and `message.send` takes `replyTo`
(a non-empty string when the send is a reply). The daemon-internal actor names
`dispatch-coordinator` and `_turn-hooks` cannot be registered as agents:
registration returns the existing `INVALID_AGENT_NAME` with the text
`is reserved for a daemon internal actor`.

## System-notice acknowledgement and retention

`message.ack` settles one system notice through the same recipient-scoped inbox
writer used for ordinary messages. It deletes that notice and atomically records
a durable acknowledgement tombstone. A repeated ack, including after an internal
notice drain or dismiss, returns `ok:true`, `acknowledged:true`, and
`code:MESSAGE_ALREADY_SETTLED`. Unknown ids and another recipient's ids still
return `acknowledged:false` and `MESSAGE_ACK_UNAVAILABLE`.

Only an authenticated local operator (`via:operator`) may use the local node id
as an additional notice key. An agent session or an unfenced node-name claim
cannot acknowledge the operator's notices, including in observe mode.

Tombstones suppress same-id notice delivery across restart. Admission resolves
confirmed local bare-name recipients before checking tombstones, including the
transactional failure-notice path. Migration discards a legacy replay that matches
a live canonical-recipient tombstone before updating remaining recipient keys;
it does not renew the tombstone. Expired or other-recipient tombstones do not
suppress that migration. The internal reply reminder producer may explicitly allow replay: it owns a bounded in-memory dedup
set (256 ids per actor, 1024 actors). Eviction or process restart can remind again;
reading or acknowledging the notice does not clear that consumer's dedup set.
No IPC, CLI, or MCP parameter exposes this internal exception.

Pending notices and ack tombstones each retain at most 10,000 rows and have a
90-day retention window. Cleanup runs at construction, notice insertion (including
transactional failure notices), acknowledgement, drain, dismiss, and explicit
prune. Notice retention uses node-local `received_at_ms`, not the sender's
`created_at_ms`. Legacy rows keep NULL receive times and are bounded by the cap.
Pruned notices do not become acknowledged and acquire no tombstone. Tombstone
expiry or cap eviction ends both repeat-ack success and replay protection.

At construction, bare-name recipients migrate only when the identity resolver
confirms a canonical agent on this node. The current node key stays unchanged.
Unknown, ambiguous, remote, or otherwise unconfirmed mappings remain quarantined
under their original keys; migration does not report them as acknowledged. Source
failures roll back the whole migration pass, including replay removal, and retry
on the next construction. An admission-time resolver failure preserves the offer
under its original key for later reconciliation; it does not guess an identity.
`hyprial daemon status --json` exposes `daemon.systemNotices.pending` and
`daemon.systemNotices.quarantined` as read-only row counts. `quarantined` counts
retained bare recipients other than the current node key; `pending` counts the
remaining addressed notices. A pending count is not proof of recipient liveness.
Tombstones are excluded. Counts use one SQLite snapshot, do not fetch or ack any
row, and change as notices are migrated, acknowledged, or pruned. An unavailable
inbox projection reports null for `systemNotices`, not fabricated zero counts.
Quarantined rows stay in `system_notices` under their original keys; they are not
hidden deletions or acknowledgements.

`system_notices.recipients_migrated` logs recipient counts `migrated` and
`quarantined`; `system_notices.recipients_migrate_failed` logs `errorType` at warn
level. `system_notices.recipient_resolve_failed` logs `errorType` at warn level
when admission cannot resolve a legacy recipient. `system_notices.pruned` logs
`expired`, `overflow`, `tombstoneExpired`,
`tombstoneOverflow`, `retentionMs`, and `maxRows` without message bodies.
