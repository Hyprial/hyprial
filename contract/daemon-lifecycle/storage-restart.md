# Storage and shutdown generation evidence

`ping.storageHealth` and top-level `ps.storageHealth` are cached diagnostics. They
never open SQLite on an IPC thread. `healthy: null` means no check is available;
`healthy: false` is not readiness. `init` fails with `DAEMON_START_FAILED` when a
reachable daemon reports a failed database probe. The cached object also carries
`observedAtMs`, `ageMs`, `stale`, and `lastError`; a missing observation is stale
and unknown, never inferred as down. Other daemon capabilities remain available
for recovery. The probe opens existing agents/inbox/PAC files
with `mode=rw`, reads SQLite schema, closes every connection, and never creates
missing databases or deletes WAL/SHM/lock files. It does not certify integrity,
write availability, cross-process lock freedom or a WAL root cause.

`ping`, `ps`, and `hosts` are projection-only status reads. They do not refresh
network state, expire routes, or clean sessions on the IPC owner. `ps` and
`hosts` accept `refresh: true` as an asynchronous request: the response still
contains the current projection and adds `refresh.accepted`, `refresh.queued`,
`refresh.operationId`, and `refresh.coalesced`. Requests for the same status
refresh coalesce onto one operation ID while it is outstanding. The endpoint
projection reports that ID through `refreshOperationId`; `refreshState` moves
from `queued` to `settled` or `failed`. A caller can pass `refreshOperationId`
to `ps` (CLI: `--refresh-operation-id`) to resolve that operation after a later
refresh has settled. Each target retains at most 16 settlements for 120 seconds,
whichever bound is reached first. A requested ID that is no longer retained, was
never admitted, or belongs to another target returns the requested
`refreshOperationId` with typed `refreshState: unknown-expired`; absence never
silently substitutes the latest operation. Lookup changes only the refresh
settlement fields: endpoint evidence remains the latest projection. The bounded
refresh owner and periodic maintenance perform the probes; periodic maintenance
also owns route expiry and dead-session cleanup.

Host rows carry `observedAtMs`, `ageMs`, `stale`, and `lastError`. A presence
sample is fresh for 30 seconds, stale through 60 seconds, and expired after 60
seconds. An expired row has `status: unknown`; a status read never renews its
observation time. `observedAtMs: null` and `stale: true` mean the daemon lacks
any evidence, also `unknown` and never a synthetic `online` or `down` verdict.
`lastError` describes the latest failed observation and does not itself prove
the remote endpoint is down.

The Zenoh endpoint projection exposes the same freshness and refresh-settlement
fields. This generation has no tailnet status producer, so `tailnet.supported`
is false with reason `TAILNET_STATUS_PRODUCER_UNAVAILABLE`; it does not return
an empty snapshot as if a probe had succeeded. The legacy
`refreshTailnetStatus: true` request returns `refresh.accepted: false`,
`refresh.queued: false`, a null operation ID, and the same reason.

The independent storage watchdog writes `daemon-health.json` every 30 seconds,
labelled with PID, daemon epoch and `checkedAtMs`. A stale timestamp is a stale
heartbeat, not proof of health. Failure/recovery transitions also reach stderr
with generated codes only, as well as the main log. The file has one writer and
does not race the startup metadata's `daemon.json` writer. Runtime fields show
restorePending and maintenance watchdog load/running/status separately.
`threadCountScope=python-managed` labels the Python thread count; it is not a
measurement of native runtime threads. Shutdown drains this metadata writer
before releasing the home lock, refusing teardown if that writer has not ended.

`shutdown` accepts optional `expectedEpoch`. A nonempty string different from
the serving generation returns `DAEMON_EPOCH_MISMATCH` without requesting stop.
The CLI forwards `daemon stop --expected-epoch <approved-epoch>` atomically to
that handler. Omission retains legacy direct-operator compatibility; it is not
a fenced maintenance plan. All delayed or dispatched maintenance must provide
the approved epoch and use a non-daemon carrier. Epochs are observations, not
authorization, and do not permit a daemon-owned agent to stop its parent.

Inbox SQLite WAL uses `synchronous=FULL` before accepting mutations; ack returns
after the existing transaction commits consumption and terminal status. The
same durable inbox and dedup tables remain authoritative. Startup logs
`inbox.recovery.pending` in batches of at most 64 unconsumed message IDs. These
events describe recovery eligibility, not proof of delivery to a harness.
Existing turn-start events describe actual managed dispatch. No message bodies
are logged. FULL synchronization may increase commit latency; it is intentional
for the durable acceptance boundary and must not be replaced with a synthetic
success before commit.

With the orphan journal counter API from PR #1490 installed, the independent
watchdog samples orphan-custody bytes (append + snapshot) and pages once when
the sampled rate stays above 50 MiB/s for more than 300 seconds. A normal sample
or counter reset clears the sustained window; recovery is logged. The heartbeat
labels scope `orphan-custody`; this is not an OS-wide disk-write measurement.
Without that counter API, `writeRate.available=false` is explicit. This code
does not install an external launchd watchdog; that remains operations-owned.

## Orphan custody upgrade bounds

Recovery replays the custody snapshot and journal before enforcing the same
entry cap as live observations (default 4096). Eviction prefers non-live rows,
then oldest first. Eviction deletes and normalized legacy-key upserts/deletes
must be durable before recovery returns; reopening with a larger cap must not
resurrect the discarded rows. The diagnostic `orphanId` for legacy
`<harness>:unidentified:<owner>:<process-key>` rows can change on upgrade to the
harness-scoped key. Unknown rows merge without erasing known identity or custody;
distinct known identities stay separate, subject to the same cap.

A capacity-rejected start/stop observation returns an empty internal custody ID
and retains no handle mappings. This is not a process-exit observation. Removed
harness tombstones retain only the most recent `max_entries` distinct IDs;
re-retirement refreshes recency and a new start clears its mark. Late observations
after tombstone eviction follow normal collection rules.

Regression coverage: `tests/daemon/processes/recovery/test_orphan_restore_bounds.py`.

Reproduce only against disposable homes:

```
HYPRIAL_BIN="$PWD/.venv/bin/hyprial" uv run python scripts/verify/check_storage_restart.py
```

The runner pins the executable, checks network isolation, creates synthetic
actors, acks before SIGKILL, preserves another pending message across two
restarts, refuses an old-epoch stop, checks the independent heartbeat, and waits
for cleanup of every test-owned daemon. It never invokes the production prune
script. This proves isolated recovery; incident causality and long-running
production health remain separate evidence.
