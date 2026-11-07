# Read-only lifecycle observation (internal Python port, slice A)

This port adds diagnostics only. It does not change IPC methods, agent URIs,
process custody, lifecycle receipts, inbox cursors, delivery, grants, or policy.
Public `ps.agents[].runState` publication is specified separately in
[run-state.md](run-state.md) (slice C), not by this owner port.

## Values

The kernel public face exports frozen values `LifecycleTarget(host_id, group,
key)`, `LifecycleCursor(epoch, revision)`, `LifecycleSnapshot(target, epoch,
revision, observed_at_ms, state, cause, available)`, `LifecycleEvent(target,
epoch, revision, transition, cause, observed_at_ms)`, `ObservationBatch(events,
cursor, resync_required, current, status)`, and
`ObservationSubscription(status, observer_id, initial)`.

A harness target uses group `harness.lifecycle`. Its key must identify a
canonical agent incarnation, not a PID or display name. Composition supplies
that key and privately binds it to the source incarnation. Observation epochs
are random diagnostic IDs; they never expose session references or entity tokens.
The aggregate coordinator uses group `lifecycle.coordinator`, never a child key.

State vocabulary: `unknown`, `starting`, `running`, `stopping`, `stopped`, `down`.
The owner must know a transition before it publishes it. Operation completion
is not process readiness. `noproc` means no observed resource, not OS death.
Session contact/detach remains `unknown`: neither proves execution nor exit.

## Reads and observer accounting

- `snapshot(target, namespace="lifecycle")` returns one immutable current cut.
- `observe(target, namespace="lifecycle", after_revision=None, epoch=None,
  limit=128, wait_ms=0, observer_id=None)` returns current even on the first
  call to an absent/already-stopped target. No cursor means an initial read.
- A replay cursor is valid only in the target's retained epoch/revision range.
  Epoch mismatch, retention loss, negative/future/missing revision, or missing
  epoch requests resync. Resync returns no events and a current snapshot cursor.
- A normal page returns at most `limit` events. Its cursor identifies the last
  returned event, not a newer `current` snapshot. Continue from that cursor;
  otherwise a reader could skip transitions on a short page.
- Publication, initial subscription, and reads share one lock. A concurrent
  exit appears in the initial/current snapshot, replay, or explicit resync.
- Only namespace `lifecycle`, wait 0, and limits 1 through 128 are accepted.
  Unsupported parameters raise `ValueError`; no hidden waiting thread exists.
- Optional `open(target)` accounts a long-lived reader and returns initial state.
  At 64 observers per target it returns `status=overloaded`, without an ID.
  Stateless polls do not allocate subscriptions. `cancel(id)` is idempotent;
  cancelled/unknown/evicted IDs read as `status=closed`. It cannot stop a target.
- Shutdown closes readers and releases diagnostic allocations. It does not
  acquire, cancel, or settle the watched owner's work.

Default retention is 128 transitions per target, 64 observers per target, and
4096 targets/sources/bindings per owner. Oldest-entry eviction is diagnostic
loss, never work rejection. Eviction closes affected subscriptions and creates
fresh diagnostic epochs on republication. Removal/eviction also invalidates
absent cursors so a transient create/exit cannot disappear as empty success.
Readers cannot pin retained work or backpressure execution.

## Current owner adapters and coverage

`HarnessProjection` retains the existing status boolean unchanged. A false
`running` value alone is not exit evidence: reconnect failure and stop-in-progress
remain unknown/unavailable. A stopped observation requires the process owner's
existing `liveness()` result to be observed DEAD. No independent PID probe is
introduced. Failed owner liveness reads report unavailable, not stopped, and do
not change recovery policy. A retry/failure string is not a lifecycle transition. `SessionActor` publishes confirmed contact and
detach at its existing runtime projection boundary, without changing leases.
`LifecycleCoordinator` publishes its own dependency health and closes observers
on drain. Snapshot, observe, and open refresh actor quarantine directly through
the existing runtime read boundary; no unrelated health-property read is required.
Its failure does not mark independently running harnesses dead.

The adapter's `bind`/`unbind` association is internal and read-only with respect
to the source. Binding and source publication are atomic. A source incarnation
mismatch makes the old target unavailable, never adopts a new instance. Source
writers retain their existing event ordering and generation fences. The cell
mount package (slice B) supplies canonical target associations; this slice does
not create cell mounts or claim native ractor execution.

## Executable vectors

- `tests/daemon/lifecycle/observation/`: initial stopped/absent, exit race,
  retention/epoch resync, pagination, observer overload/cancel/close, eviction,
  transient-resource loss, current harness/session owner projections, and live
  coordinator recovery-thread failure and mailbox quarantine without child death,
  plus unknown/reconnecting/stopping/verified-stop process liveness.
- `tests/kernel/contracts/observation/`: frozen data values and AST guards
  against callable fields, new actor/mailbox/thread APIs, and callbacks.

No Rust native monitor, terminal carrier, permission prompt, or E1/E2 acceptance
is established by these vectors. Native `monitor`/`unmonitor` migration is later.
