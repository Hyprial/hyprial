# Adapter desired-state contract (0.5.5.1)

Adapters are delivery infrastructure, not worker harnesses. The desired-state
schema is 3. The JSON `providers` array contains only declared worker harnesses;
`adapters` contains `{provider, name}` records, with optional `status: failed`
and inert `legacyLaunch` metadata. No adapter has a `harness` field. Workers keep
the existing `provider` wire key; model-vendor fields are unchanged.

An adapter name and a worker name are independent storage identities. Admission
still refuses duplicate local names as a UX guard. Migration accepts historical
collisions. Agent list/top, worker restore, runtime delivery, transfer and inbox
recovery never derive agent identity from adapter intent. Adapter list/status
and daemon status retain their existing adapter `provider` shapes. The removal
receipt's historical `desiredStateProvider` boolean now reports adapter intent;
it is retained for compatibility, not a worker row.

## Lark quarantine recovery

Desired Lark adapters retry automatically after exhausting the unchanged fast
restart budget (two restarts, 120-second window). Quarantine is not terminal.
The slow retry delay is `min(600, 30 * 2^(attempt-1) * jitter)` seconds, with
independent jitter in [1, 1.2]. The exponent is capped before evaluation.
Repeated observations of the same failed process do not consume another attempt;
absence alone is not a new failure. Failed slow attempts remain on the slow path,
even after the fast failure window expires.

Adapter status/list rows add these optional fields (also in CLI human output):

- `nextRetryAt`: number, deadline in the daemon's monotonic-clock seconds, **not
  Unix time**. Present only while a desired quarantined adapter has a scheduled
  retry. It is absent during an in-flight retry and after recovery/stop. Do not
  persist or compare this value across daemon restarts or hosts.
- `retryAttempt`: positive integer, one-based slow retry ordinal (1 for the first
  scheduled/in-flight retry). Present while desired and quarantined; absent
  after recovery or stop. It does not count timer probes or fast restarts.

`status` stays `quarantined` while waiting and during a slow retry until a
running ONLINE observation with non-stale/non-checking stream health arrives.
That observation clears quarantine, failure history, failed-attempt deduplication
and retry schedule. Explicit start resets the budget immediately. Explicit stop
cancels retries on successful deactivation; it may supersede an in-flight
background retry (the late process is fenced and stopped). Existing mutual
exclusion between explicit lifecycle operations remains. Desired=false adapters
never respawn.
The schedule survives actor-generation recovery in daemon memory, not daemon
restart. Existing desired-state restore owns restart recovery.

The existing daemon health-event log emits `lark.adapter.quarantined` at error
level once per entry (fields: `adapter`, `nextRetryAt`, `retryAttempt`) and
`lark.adapter.recovered` at info level once on automatic recovery (fields:
`adapter`, `retryAttempt`). There are no hard-coded notification recipients.

Regression: `tests/daemon/adapters/lark/lifecycle/quarantine/test_self_heal.py`.

## Lark outage backfill

The adapter SQLite store retains a per-adapter, per-chat `observed_ms` high-water
mark in the additive `chat_observations` table. Successful live custody records
platform creation time (capped at observation time). A complete successful
history scan advances the mark to the scan's captured wall-clock time. Failed,
incomplete, or permission-blocked history does not advance it. Existing stores
without a mark use a 24-hour initial window; no credentials or message bodies
are stored in this table.

For each known chat, the history start is
`max(now - 24h, min(now - overlap, observed_ms))`. The default overlap remains
30 minutes. Known chats include durable observations, correlations, dead letters
and all active pending-submission ordering headers; retired chats remain excluded.
Header discovery is bounded by the existing 1024-row / 16-MiB frozen-custody cap.
It does not select bodies by native ID: every chat's oldest pending item and all
same-time parents are reachable, including items absent from platform history.
At most 16 frozen requests are attempted per sweep, within the existing five-second
replay budget. Native-ID circular paging remains only for unclassified/retired rows.
The pending replay budget limits frozen submissions, not the ordinary chat set.

Cold start requires history before the first new live message in a known chat.
Reconnect marks that requirement synchronously before scheduling its sweep.
History and live delivery share the bounded single inbound effect lane. History
is sorted by platform `create_time`; parent-before-reply breaks timestamp ties.
Frozen requests in a history batch retain their immutable submission and join
that order. Native message ID remains the idempotency key. Incomplete history
(including the existing 20-page REST cap), invalid causal order, transient API
failure or an unsettled submission prevents newer messages in that chat from
being forwarded. A live message held by this gate is preserved as a dead letter
with reason `backfill-unavailable`; no checkpoint advances. Local command-response
replay and already-delivered duplicates do not need another history sweep.

A frozen request that fails three recovery attempts becomes terminal with reason
`submission-retry-exhausted`. The failure counter survives restarts. Closing or
an expired deadline before an attempt does not consume this budget. Terminal
custody and the bounded dead-letter audit row commit together. Acceptance remains
unconfirmed: this is not proof of non-delivery and must not emit a false terminal
failure notice to the sender. The immutable request is retained, counts against
the custody capacity, and is excluded from automatic replay and active pending
counts. Remaining messages keep their order and the chat can ungate. Live/history
redelivery of a terminal request returns `submission-dead-lettered`, without IPC.
Explicit `recover_message` re-arms its budget and reuses the exact frozen request;
only a validated receipt settles custody. Downgrading to a build without these
columns can resume automatic replay of terminal rows and is not equivalent behavior.

A gated sweep either advances delivery/terminal custody or reports a typed cause:
`pending-recovery-budget`, `inbound-recovery-incomplete`, history failure/incompleteness,
invalid order, or a blocked-history category. It must not silently return healthy
with active frozen requests remaining. A timeout still returns `recovery-deadline`.

Permission-blocked/unsupported history retains the existing degraded live-stream
behavior for mention-only apps: live traffic continues, with no history-order
claim and no checkpoint advancement. Recovery cannot cover chats that were never
observed, messages older than the 24-hour cap, unavailable platform history, or
messages omitted by an eventually consistent platform response. Manual recovery
of an explicitly selected message is not a chronological batch replay.

Adapter log event registry (info level; standard logger envelope is unchanged):

| Event | Additional fields | Meaning |
| --- | --- | --- |
| `lark.backfill.truncated` | `windowStartMs`: integer; `observedMs`: integer or null; `maxLookbackSeconds`: integer (86400) | Emitted for each chat whose requested start predates the cap. The older interval is not scanned. |
| `lark.backfill.sweep` | `chatsScanned`, `forwarded`, `duplicates`, `truncated`, `retryableErrors`, `blockedChats`: non-negative integers; `windowStartMs`, `windowEndMs`: integer Unix milliseconds | One summary per completed sweep, including empty and failed-history sweeps. Start is the earliest chat window (end for an empty sweep); truncated counts capped chats. Forwarded/duplicate counts include attempted frozen submissions. |
| `lark.backfill.deferred` | `reason`: `submission-recovery-failed` or `pending-recovery-budget`; `attempts`: non-negative integer | A frozen recovery failed or its per-sweep budget prevented an attempt. Failure attempts are per request; budget attempts are per sweep. |
| `lark.backfill.dead-lettered` | `reason`: `submission-retry-exhausted`; `attempts`: integer (3); `acceptance`: `unconfirmed` | Automatic retries stopped; exact frozen custody is preserved for explicit recovery. |

These events contain no chat IDs, sender IDs, native message IDs, message content,
credentials, URLs or exception text. Existing phase/failure events still report
sweeps that raise or time out; a killed worker cannot promise a summary event.
`ReconcileReport` keeps its existing fields. SQLite schema compatibility is
additive: older builds ignore the observation table but do not provide gap
recovery or ordering guarantees.

Regressions: `tests/daemon/adapters/lark/inbound/recovery/reconciliation/test_outage_backfill.py`
and `test_pending_progress.py` in the same directory.

## Migration and rollback

Both legacy `.v1` files and SQLite schemas 1/2 migrate atomically to schema 3.
Legacy adapter rows are removed from the worker table and stored in `adapters`.
Unknown kinds/versions, malformed arrays and duplicate adapter names fail loudly.
Pins in the agents database remain unchanged. Unresolved `channelPins` remain
staged; migration does not resolve or drop them.

The first pre-migration document, original schema version and SHA256 are stored
in `adapter_model_backup` in the same transaction before schema promotion.
Repeated migration never replaces that backup. Older binaries reject schema 3
with `unsupported desired-state schema version 3`; they cannot silently load
only workers and lose adapters.

`hyprial adapter downgrade-state --json` is offline and takes the daemon ownership
fence. It verifies the backup digest and refuses if desired intent has changed
since migration. Success restores schema 2 and returns `ok`, `schemaVersion: 2`,
`backupSha256`, adapter count and `nextStep`. Expected refusals return
`ADAPTER_DOWNGRADE_REFUSED`. It does not edit the agents database, credentials,
inbox or lifecycle operation journal. Use it with the new binary after stopping
the daemon, then install/start the older binary. Do not start the new binary
between restoration and downgrade: that migrates again.

A new home without a legacy backup cannot use this command. If restoration is
refused, retain the current binary, state and backup and ask an operator to
reconcile the changed intent. Never edit schemaVersion or delete state to force
a downgrade. Runtime-only downgrade refuses schema 3 with the stable code
`RUNTIME_DOWNGRADE_REFUSED` and the two-step remedy; to reach a pre-schema2
binary, first restore/install a schema2-capable binary, then use its existing
runtime downgrade command after retiring runtime resources.

## Evidence boundaries

Tests exercise real isolated SQLite migration, base-version SQLite/document
codecs, ownership fencing, JSON surfaces and scripted adapter/worker lifecycles.
They do not establish live Lark connectivity or real model-provider acceptance.
