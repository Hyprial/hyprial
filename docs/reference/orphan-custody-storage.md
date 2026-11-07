# Process custody persistence

The daemon owns the process custody registry. `orphan-processes.json` is a v1
snapshot; `orphan-processes.json.jsonl` contains fsynced upsert/delete deltas.
Both files belong to one writer and must be retained together for backup or
recovery. Reading or pruning only the snapshot can miss accepted custody or
resurrect retired records. Do not use old snapshot-only prune scripts after
upgrading. Offline maintenance requires a non-daemon carrier, an explicitly
cancelled in-flight agent turn, and a matching daemon epoch. Restart operations
remain supervised through `hyprial init`.

Each changed observation appends only its delta; identical observations do no
disk I/O. Compaction is proportional to the snapshot size (minimum 256 KiB),
so adding new records does not repeatedly rewrite the growing registry.
Snapshot sequence numbers fence old deltas after an interrupted compaction.
Recovery discards an incomplete final append and rejects malformed committed
records. A failed append requires reopening the writer before more mutations.

`ps --json` exposes top-level `orphanPersistence`: `appendWrites`, `appendBytes`,
`snapshotWrites`, `snapshotBytes`, and `unchangedSaves`. Counters describe this
daemon generation, not lifetime totals. Snapshot byte counters measure actual
file bytes. Raw and ProcessOwner handles share one logical registration, whose
identifier remains stable through late PID backfill and stop. The owned handle
retains probe/stop authority. Unreachable entries without a usable identity
receive a 60-second first-observation grace period before retirement. Each GC
tick probes at most 64 entries, rotating across unknown survivors.

Processes exiting within 60 seconds spend the existing failure budget across
successful respawns. Production uses exponential retry backoff; reaching the
budget requires explicit start. A run stable for 60 seconds resets the early
exit counter. Explicit start resets it as well. The existing explicit budget=0
infinite-retry opt-in remains available.

Identity-probe failures on one custody entry back off exponentially (1s
doubling) up to a 15-minute cap; a successful probe resets the window, so a
permanently unreadable identity no longer probes at the collection tick.
The registry is capped (default 4096 entries), including after snapshot/journal
recovery: over capacity it evicts non-live entries first, then the oldest, and
logs `harness.orphan.capacity_evicted` rather than growing without bound. If
that policy rejects a new observation itself, `observe_start` / `observe_stop`
return an empty ID and retain no handle mappings for it. The caller still owns
the process; rejection is not proof of process exit or successful cleanup.

New observations without a complete identity share one harness-scoped
`unidentified` key. Recovery merges legacy per-process unidentified keys and
persists their deletions together with the normalized rows and capacity
removals. A known identity survives merging with unknown rows. Distinct known
identities retain separate custody rows; conflicting partial identities are
not combined into an invented PID/marker pair. These rows still count toward
the same capacity limit.

When a harness is confirmed removed, `retire_harness` drops its entries and
remembers it as gone, so a later stray entry for the same `harness_id` is pruned
by the next collection pass (`harness.orphan.harness_gone_retired`). Retirement
marks are bounded to the most recently retired `max_entries` harness IDs;
repeated retirement refreshes recency. Once a mark ages out, late observations
follow normal custody/collection rules. A fresh `observe_start` clears its mark
for a legitimate restart.
