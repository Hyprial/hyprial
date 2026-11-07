# P1 transport/inbox boundary

This slice deliberately has no dependency on `hyprial.proto` or `hyprial.providers`.
Protocol serialization ends before this boundary: callers supply an opaque
`bytes` payload plus the delivery metadata required to persist and route it.

## Public interfaces

- `hyprial.transport.TransportSession` is the daemon-facing byte transport:
  put/get, subscriptions, queryables and liveliness. `ZenohTransport` is its
  Zenoh 1.9 implementation.
- Durable inbox/outbox/custody state is owned by the local SQLite store. The
  daemon does not supervise a separate `zenohd` storage-manager process.
- `hyprial.transport.PresenceView` exposes materialized actor and mailbox
  liveliness without leaking Zenoh samples into inbox policy.
- `hyprial.inbox.DeliveryTransport` is the narrow network seam used by the durable
  queue. `ZenohDeliveryTransport` publishes delivery frames and waits for
  durable receipt query replies.
- `hyprial.inbox.InboxPort` is the daemon/proto-facing state-machine seam:
  submit, receive, FIFO next, durable ack and retry.

## Durability order

Direct delivery follows `sender SQLite outbox -> Zenoh put -> recipient SQLite
commit -> receipt query response -> sender outbox delete`. A timeout leaves the
sender item queued. Duplicate `message_id` or `idempotency_key` arrivals create
or retain a receipt tombstone but do not create another user-visible item.

Custody follows the same rule: a mailbox responds only after its local custody
row commits. The sender may then delete its outbox; the mailbox owns retry until
the real recipient receipt, hold expiry, or its one-GiB capacity boundary.
Custody is ownership transfer, not replication: exactly one holder exists at any
instant, so there are never two copies to reconcile.

## Terminal state

Every message ends in one persisted state, and both are readable long after the
fact — a sender is routinely offline at the moment its message ends, so the
verdict cannot be a one-shot notification.

- `fetched` — the recipient durably took it. Written at the recipient, inside
  the transaction that commits the message to its inbox. It is a local fact,
  so no network leg can lose it. The public reason is `ACK_RECEIVED` for
  interactive and headless recipients alike.
- `expired` — the holder TTL elapsed before it observed an ACK. The public
  reason is `TTL_EXPIRED`, written in the same transaction that removes the
  held row. Capacity and non-TTL prune/failure paths are the PR 4 cleanup and
  are not relabelled as TTL.

A correlated Lark reply is the local-control specialization of the same model.
The sender daemon retains the outbox row while its supervised adapter worker is
offline or fails a native send. A positive worker receipt is emitted only after
the Lark reply and durable reply-index commit; it removes the row and writes
`fetched` / `ACK_RECEIVED` with the sender daemon as holder. There is no
recipient Harness inbox on this leg that could truthfully write
first-hand inbox evidence. If no receipt arrives, ordinary holder TTL eviction
writes `expired` / `TTL_EXPIRED` instead.

Interactive Channel polling observes inbox state without claiming a fetch.
Only a real `harness_read` (or the stronger reply/ACK boundary) stamps
`fetched_at_ms`. That fetch immediately retires a matching same-daemon outbox
and pushes `fetch-receipt/<sender>/<message>` to a remote sender. The push is
the ACK edge: if the sender was offline and missed it, the same message ID is
redelivered after recipient-online events until a later ACK or TTL. There is no
separate terminal confirmation query.

While the recipient stays online, each no-ACK result follows the registered
external-I/O exponential curve, including its stable per-message jitter, up to
the policy's maximum backoff and never beyond the row TTL. The retry pump waits
for the earliest outbox or custody due key, capped by that same maximum; with no
due row it does not poll at the base-backoff cadence. An online transition still
advances that recipient's unexpired held rows immediately.

The explicit-fetch marker is defined in the shared interactive session
contract, not in the Claude transport. Every TUI carrier uses the same IPC
boundary; headless workers continue to complete through daemon-owned ACK
processing.

Holds default to 12 hours (`DEFAULT_HOLD_TTL_MS = 43200000`) and 100 items, per daemon configurable through
`HYPRIAL_HOLD_TTL_MS` and `HYPRIAL_HOLD_MAX_ITEMS`. Expiry is always measured on the
holder's own clock, from the instant it took the message; a sender's
`created_at_ms` never enters the arithmetic, because clocks across machines
disagree. FIFO order likewise stays on `arrival_id`, never on send time.

Records are served on `hyprial/v1/msg/status/<sender>` (all) and
`hyprial/v1/msg/status/<sender>/<message_id>` (one). Queried keys must be concrete:
a reply carries the query's own key expression, which a wildcard cannot be.
Every current endpoint answers a valid query with a frame naming its `holder`,
even when `records` is empty. That named empty reply proves the node answered and
had no better record.

Read them with `hyprial message status --from <actor> [--message-id <id>]`, which
calls the daemon's `message.status`. Persisted, wire, and query terminal states
are `fetched` / `expired`. A recipient machine that did not answer, returned an
undecodable reply, or cannot be identified from the recipient URI never rewrites
an `expired` record; those response-health facts remain under `diagnostics`
(`missingRecipientHolders`, `undecodableHolders`, and responder/error counts).
During a rolling upgrade an older querying daemon may still apply its own old
projection until that endpoint upgrades. A recipient on the querying node is
confirmed locally and does not depend on a mesh reply.

The pull counts how many separate replies arrived under each holder name.
Two daemons sharing one node identity (a copied home, a stale instance) each
answer under the same name, and the name-keyed diagnostics collapse them; the
count is what keeps them apart. When a name answers more than once, the
result's diagnostics carry `duplicateHolderRecords` (holder, reply and record
counts) and the daemon logs `message.status.duplicate_holder` -- the
status-query vantage on the condition the liveliness watch reports as
`daemon.identity.duplicate_instance`.

## Terminal alarms are a latency optimization

The same code paths that commit a fetched-after-processing failure or move an
expired item to the DLQ synchronously invoke `AlarmEmitter`. A Lark-originated
message receives one native reply in its original conversation. An agent
sender receives an `intent=system` notice containing the original `messageId`
and reason. A remote terminal holder publishes it directly to the sender's
daemon on `hyprial/v1/system-notice/<node>/<notice-id>`; that namespace has no
receipt queryable. The sender daemon persists the offered notice outside the
ordinary inbox FIFO and removes it when offered to the sender; it creates no
acknowledgement, result, receipt, or replay obligation. Alarm delivery failures
only write `alarm.failed`.

This does not change the correctness model above. Notifications can be
throttled or fail while the sender is offline, so `hyprial message status` over
the persisted `msg/status` records remains the only authoritative answer.

## Contract gate

Run the owned slice with an isolated Zenoh loopback transport:

```sh
UV_CACHE_DIR=/tmp/hyprial-p1-uv-cache uv run python contract/wire/golden/run-transport-inbox.py
```

The full 30-case runner remains an integration gate. Cases owned by proto,
daemon/providers and adapters are intentionally not synthesized in this slice.
