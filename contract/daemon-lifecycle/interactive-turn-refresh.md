# Interactive current-run refresh (slice E1)

`session.turn.refresh` is the session-fenced, data-only current-run report a
Claude Channel or Pi attach carrier sends on registration and whenever it
recovers the daemon generation. Slice C derives `runState` from validated
`session.turn.started` / `session.turn.ended` receipts; those receipts cannot
survive a disconnect, a lost end pulse or a daemon restart. This method is the
recovery edge. It changes no delivery, settlement, turn or supervision policy.

Executable vectors: `turn_refresh.py` (called by `run.py` in the isolated
lifecycle runner): 12 open-set/producer-fence cases and 4 contact-gap acknowledgement cases.
Owner-path regressions, including real application reconstruction, are in
`tests/daemon/harnesses/carrier/turns/test_fence_resets.py`.

## Declaration

The method is declared once as a `MethodSpec` and bound to its handler in
`src/hyprial/daemon/impl/request_catalog/session.py`. The registry validates the
params before and the result after the handler, so a request can never reach
the handler with an undeclared key. The name is a declared member of the
still-legacy `session` family on purpose: sibling `session.turn.*` boundary
reports stay on the legacy chain (they are in the frozen ledger), while a
declared name is served by the registry first and never falls back. The wire
request shape and wire name were preserved when the dispatch path moved.
The response now adds `rejectionReason` under `schemaVersion: 1`. `scripts/ci/check_request_contracts.py`
(`legacyMethods`) forbids adding a new name to the legacy router, which is why
no `_METHODS` table, allow-list entry or dispatch branch names it.

## Request (schema version 1)

```json
{
  "actor": "agent:owner:node:name",
  "sessionRef": "session-1",
  "schemaVersion": 1,
  "producerEpoch": "carrier-boot-id",
  "daemonEpoch": "confirmed-daemon-epoch",
  "seq": 4,
  "openRuns": [{"reportId": "turn-1", "phase": "executing", "elapsedMs": 12000}],
  "capabilities": {
    "wholeSessionTurns": true,
    "preparation": true,
    "permissionWait": false,
    "producerActivity": false
  },
  "channelLeaseToken": "…"
}
```

- `schemaVersion` must be exactly `1`; any other value or type is
  `INVALID_ARGUMENT`. Only declared keys are accepted. `daemonEpoch` is an
  additive v1 fence: older callers that omit it receive a typed rejection,
  never an admitted snapshot. Older daemons may reject the new field; carriers
  then cache telemetry as unsupported without affecting delivery.
- `actor` + `sessionRef` are fenced exactly like the boundary reports: no
  current session is `STALE_SESSION`, a superseded one is `SESSION_SUPERSEDED`.
  Counter and receipt resets do not own or delete the observation fence.
- `daemonEpoch` is the epoch confirmed by successful registration or session
  refresh, not merely an epoch seen on an inbox read. Missing/mismatched epochs
  or a session not confirmed in this daemon epoch return `stale_daemon_epoch`.
  A replay retains its original epoch; registration cannot re-stamp it.
- `producerEpoch` is the carrier process boot identity (opaque, ≤128 chars).
  `seq` is a non-negative integer that starts over in a new producer epoch.
- `openRuns` holds at most 64 entries. `reportId` is 1–128 characters and
  `phase` is `prepared` or `executing`; anything else is `INVALID_ARGUMENT`.
  `elapsedMs`, when present, is producer-local elapsed time (≤7 days).
- `capabilities` must declare exactly the four keys, all booleans. A partial
  object is malformed, not "the rest unsupported".
- A registration with a channel lease (`claude-channel`) must present the
  matching `channelLeaseToken`; a mismatch is `INVALID_CHANNEL_LEASE` and a
  lease-backed session without a persisted digest is
  `CHANNEL_LIVENESS_UNAVAILABLE`. Pi attach sessions hold no lease.

Response: `{"ok": true, "schemaVersion": 1, "refreshedAtMs": <daemon ms>,
"applied": <bool>, "resyncCleared": <bool>, "observationGapCleared": <bool>,
"rejectionReason": <null|string>}`.
The additive `rejectionReason` is null for admission, or one of
`stale_sequence` (duplicate/older sequence), `retired_producer` (previously
replaced boot), `producer_history_exhausted` (bounded history cannot prove a
safe transition), `mount_unavailable` (no current cell fence), or
`stale_daemon_epoch` (missing/old epoch or unconfirmed session). Rejection
keeps all three booleans false; `ok: true` means the observation request was
processed, not that the snapshot was admitted.
`refreshedAtMs` is daemon receipt time; it is not a turn start and not an
activity time. `observationGapCleared` is true only when this call cleared a
recovered contact gap (below).

## Daemon semantics

- The refresh replaces the current open set. A receipt start the carrier no
  longer lists is dropped without publishing `turn.*`: a stale observation is
  not a business turn transition. Closed (`end`) report ids are never reopened
  by a refresh.
- A receipt start and a refreshed run with the same `reportId` are one open
  run. The oldest known start wins, and a later confirmed `session.turn.started`
  still publishes its normal `turn.started` event (the refresh never occupies
  the report id for the business hook path).
- Elapsed time is provenance, not authority: `since = receipt − elapsedMs`.
  Without `elapsedMs` the start instant and turn age stay unknown (`null`), never
  zero and never the receipt time. A daemon restart therefore republishes
  `in-turn` with unknown age until the producer supplies elapsed time.
- Duplicate or older `seq` inside one `producerEpoch` is an idempotent no-op
  (`applied: false`). A previously unseen `producerEpoch` restarts the sequence
  and is applied while history has capacity. Replacing a producer retires its
  epoch for this mount; any later replay is rejected, even with a higher seq.
  Retired identities are never evicted to admit another producer. The ledger
  retains at most 1024 retired epochs plus the current epoch. A further
  transition latches unknown/resync-required and rejects all snapshots until
  the mount fence changes; heartbeats, receipts and current-producer refreshes
  cannot clear that latch. Retired epochs still return `retired_producer`.
  Thus bounded memory never turns a known old producer into an unseen boot.
  Boot identities have no intrinsic chronological order: an unseen boot is
  admitted on authenticated arrival, not by its string value or wall clock.
- An accepted refresh makes the open set authoritative: an evicted-receipt
  completeness gap is cleared (`resyncCleared: true`). It does not clear
  package staleness, a conflicting mount or a verified process exit — those
  still outrank it in the C precedence.
- **Recovered contact gap.** The session owner records `observation_gap` when
  the same session misses its lease TTL. Ordinary contact (heartbeat, session
  refresh, list reads) restores `alive` but must not clear that gap. An accepted
  current-run refresh from the owning session may acknowledge it
  (`observationGapCleared: true`), because it is the authenticated current-state
  statement that repairs the unknown period. Admission must have `applied: true`:
  rejected old or duplicate sequences cannot clear a gap, even with a valid mount.
  The handler captures the owner's gap revision before admitting the statement.
  The owner checks and clears under one lock: that exact revision must still
  exist (a concurrently recorded new gap is not acknowledged), the heartbeat must belong to this exact
  session, the session must be confirmed for this daemon generation, and that
  heartbeat must still be inside the lease TTL. A stale heartbeat, a superseded
  session, a different generation, a missing mount or a deterministically
  refused request keeps the gap. A replacement session starts without the old
  gap, and a verified process exit still outranks the cleared gap.
- Observation history has its own daemon-local store, separate from display
  counters and resettable business-hook receipts. Register, guarded register,
  re-register, unregister and counter resets cannot erase its retired producers,
  accepted sequence, saturation latch or completeness gap. The current binding
  is read under the existing receipt lock before admission. A changed
  identity-owned mount (rebind, replacement session, new entity) explicitly
  advances the observation with an `observation.mount.advanced` audit record;
  an identical mount never resets history. A read of a different mount remains
  unknown and does not mutate the store. Only the current mount is retained per
  actor; the identity owner, not caller data, establishes a new mount.
- Daemon restart is a new observation epoch, not persistent turn storage.
  Previous-epoch observations are rejected even after identical registration
  and even with a higher producer sequence. Freshly admitted snapshots must
  carry the newly confirmed epoch; before admission the new projection is
  unknown. No old monotonic timestamps or run facts survive restart.

## Coverage negotiation

`runState.scope` is now negotiated, never inferred from the carrier's name:

- `wholeSessionTurns=true` → `whole-session`; the daemon may report idle or
  in-turn for the whole agent.
- absent capability (carrier never refreshed) or `false` → `carrier-deliveries`
  and therefore `unknown` / `partial-turn-coverage`. A missing capability is
  not an empty active set and does not inherit whole-session coverage.
- `permissionWait` and `producerActivity` drive
  `coverage.permissionWait` / `coverage.producerActivity`. E1 carriers declare
  both `false`; explicit permission waits are E3.

## Carrier behaviour

- Claude Channel (`ClaudeChannelAdapter`): a best-effort refresh after a
  successful `session.register` and after the recovery `session.refresh`. It
  reports `openRuns` from the adapter's own open-turn state (`executing` once the
  start report was confirmed, otherwise `prepared`) with in-process elapsed time,
  and declares `wholeSessionTurns`/`preparation` true.
- Pi attach (`pi_harness_attach.ts`): the same two edges, using the extension's
  own open turn, `agent_start` phase and monotonic elapsed time.
- Same-epoch recovery uses the ordinary heartbeat cadence. `session.heartbeat`
  adds `observationGap: boolean`, reporting the session owner's outstanding gap
  after contact renewal. This hint does not clear the gap or imply any run state.
  Both carriers owe a current-state refresh when the hint is true. After the
  accepted refresh clears the gap, later heartbeats no longer request snapshots.
  No failed RPC or new native turn event is required. Older daemons that omit
  the hint remain compatible but cannot provide this recovery trigger.
- Both treat failure as telemetry-only: it never fails registration, polling,
  delivery or turn reporting. The two failure kinds are handled differently,
  because "not retried in a loop" must not mean "never recovered":
  - A transient failure (transport loss or an ambiguous daemon fault) leaves a
    bounded retry obligation. Each successful ordinary poll/heartbeat round-trip
    services at most one retry, so the existing cadence paces it: no new timer,
    thread or loop, no delivery dependency, and the obligation settles after the
    first success. Only one snapshot RPC may be in flight per carrier. A
    concurrent poll or heartbeat does not send another snapshot or wait for
    that diagnostic RPC. A new confirmed epoch keeps its own obligation: a
    success or refusal from the old in-flight epoch cannot settle or disable it.
    A typed `stale_daemon_epoch` response also leaves the obligation owed until
    the ordinary session recovery confirms the new epoch and sends a fresh
    snapshot; the carrier does not re-stamp and retry the old payload.
  - A deterministic refusal (`METHOD_NOT_FOUND`, `INVALID_REQUEST`,
    `INVALID_ARGUMENT`, a lease/generation mismatch) is cached as unsupported
    for this daemon generation and is not retried. A `SESSION_SUPERSEDED`
    verdict ends the carrier's obligation with the session. A successful
    registration clears the cache. A successful `session.refresh` that confirms
    a different nonempty `daemonEpoch` also clears it, without re-registering.
    Each edge attempts one snapshot; same-epoch refreshes and healthy polling
    retain a cached refusal. An epoch merely seen on a list does not bypass
    session confirmation.

## Non-goals and evidence boundary

The refresh publishes no `turn.*` event and never reads, fetches, settles, acks
or replies to an inbox row; it renews no lease; it starts no worker, process or
timer; it never kills, restarts, approves or denies anything. It is not a durable
turn ledger: neither receipts nor refreshed sets survive a daemon restart.

Unit and protocol tests prove the request/response fences, ordering,
idempotency, contact-gap recovery, bounded retry, unsupported caching and the
"no extra consumption" property with the real shipped Pi extension against a
fake daemon socket. They do not prove that a real
Claude Code or Pi TUI emits these pulses. Attended native runs are reported
separately as PASS/FAIL/BLOCKED; a skip is not a pass.
