# OrgFS: Yjs-default state model (0.5.5.3 design)

Status: design approved (see Decisions, 2026-10-29); **not implemented**.
Target release: TBD (originally drafted for 0.5.5.3, which shipped without it).

> Archive note (0.5.5.3.4): shipped 0.5.5.3.x still uses the v2 journal store
> (`commits` journal + `_replay_journal`, no `space.json` layoutVersion 3), so the
> "0.5.5.2 cannot read a 0.5.5.3 space" break below describes this design's
> planned release, not any shipped version. Version numbers in the body are the
> author's original targets.
Related: #2069, `docs/design/orgfs-baseline-snapshot-2026-10-29.md`,
`docs/notes/orgfs-journal-compaction-2026-10-29/ANALYSIS.md`.

## Decision and scope

OrgFS will follow Yjs defaults: a document is its current CRDT state plus the
updates needed to reach that state. The server does **not** keep an authoritative
per-commit journal, version history, or a replayable sequence. Deleted CRDT
content is eligible for Yjs GC; there is no `read_at`/`restore` of old versions.

Incompatibility is accepted: 0.5.5.2 and older nodes cannot read a 0.5.5.3
space. The migration is one-way. We will keep a narrow, read-only N-1 window
(see below), not bidirectional compatibility.

## Target shape

A space keeps:

- the current Yjs document state for `meta`, `tree`, and each `doc-*`/`tree-*`;
- the current blob set referenced by that state;
- transport-only pending/outbox rows (versioned update blobs, bounded TTL);
- a small `space.json`: `layoutVersion: 3`, `protocolVersion: 3`,
  `spaceId`, owner, migratedAt/from and a state hash.

A write applies the Yjs update to the in-memory document and appends the
incremental update to that document's private delta file before acknowledging.
When the delta file reaches about 500 updates or 1 MiB, it is merged with the
current state into a new state file and the delta file is cleared atomically.
Reads open the current state and replay only the unmerged tail; the delta file
is not a public API and is never a queryable history. There is no
`commits(writer,seq,doc_id,envelope_bytes,version,...)` authority and no
`_replay_journal`.

The private state/delta layout is an implementation detail. It must be
crash-safe: update append, state replace and delta truncation each use atomic
rename/fsync, and a torn delta is ignored/rebuilt from the last state.

Sync is state-vector based: a peer sends its per-doc state vector; the holder
replies with `encodeStateAsUpdate(doc, requesterVector)` (or a full state update
when the vector is unknown). A Yjs update is idempotent and mergeable; there is
no `snapshot-barrier`, retirement record, or baseline-snapshot special case.
An empty/new peer can always take a full state update.

## Deletion list and dependency handling

Delete as the authority/hot path:

1. `storage/store/journal.py` commit/replay path and the `commits` table.
   Replace with state persistence. Keep the envelope only as an in-flight
   transport frame for the outbox.
2. `doc_checkpoints`, checkpoint replay, `scripts/orgfs_compact_journal.py`
   (`--dry-run`/`--apply`) and the #2069 compaction maintenance path. They
   become dead once there is no history.
3. `snapshots`, `snapshotPoints`, `retirements`, `replacement_doc_id` and the
   baseline-snapshot protocol. Purge can delete current nodes and GC blobs
   directly; it no longer needs a replacement snapshot for old peers.
4. `fs history`, `read_at`, `stat_at` and the IPC methods `history`,
   `read_at`, `stat_at`. They cannot work once Yjs GC removes prior structs.
   Remove the CLI surface and return a coded `ORGFS_HISTORY_UNAVAILABLE` only
   during a deprecation window, then remove it. **Keep `trash`**: `fs rm`
   remains a move-to-trash operation, `trash list` and `trash restore` operate
   on the deleted node's last current state (not historical versions), and the
   new `fs rm --permanent` deletes without entering trash. Permanent delete may
   let Yjs GC collect the content; trash retention is a current-state artifact,
   not history.
5. `document_authors` as a history query. Directory validation currently checks
   the set of durable authors per device document; replace it with a current
   author/signature field on the current document (or a current-author sidecar
   index), not a scan of old commits.
6. `export_since` cursors, `stream` history cursors, sync snapshot manifests,
   `snapshot-barrier`, and `unconfirmed_commits`/its status field. Status should
   report current sync state (holders/vector), not undelivered history.
7. `retired_residue`/retirement cleanup can shrink to blob/current-doc cleanup;
   keep only the tombstone/redirect map needed for current tree semantics.

Keep, but narrow:

- `pending`: out-of-order Yjs updates waiting for a predecessor; bounded TTL and
  bytes, not an audit trail.
- `outbox`: transport delivery/retry frames; delete on acknowledgement.
- meta/membership/ACL current state; never replay old membership to authorize a
  current write.
- Yjs delete tombstones plus the private trash record until `fs rm
  --permanent`; trash never exposes historical versions. The trash record is a
  current-state copy kept only while the user has not permanently deleted it.

## PAC work repair and work-item validation

`org work repair` and `work_items._work_inspect_history` currently use
`fs.history`/`read_at` to find the last valid work item. History deletion must
not silently break them. Before the storage cutover:

- Validate a work item against the **current** tree/meta snapshot at write
  time. Reject an invalid candidate with `ORGFS_WORK_ITEM_INVALID` before any
  Yjs update is persisted; the writer must fix and retry.
- Make `org work repair` operate only on the current invalid item: quarantine
  it to trash (or an owner-visible repair record), never claim a historical
  last-valid version. Re-creating a valid item is an explicit new write.
- Keep the current item/snapshot validation path; do not keep a hidden history
  index just for repair. If a later audit sidecar is introduced, it is not
  consulted by write admission or repair.

This is a prerequisite, not an optional cleanup: the state-only cutover is not
allowed to land while work-item admission still depends on old versions.

## Journal folding migration (one-way)

Precondition: every daemon for the space is stopped. There is no online
migration with old writers.

1. Back up the entire `state/orgfs/spaces/<spaceId>/` directory outside
   `spaces/` (same rule as #2069: `state/orgfs/compaction-backups/`), including
   journal/WAL/SHM, `.loro`, blobs and pending.
2. Open the old store once with the v2 materializer; verify each document's
   current state against its `.loro` and record a state hash. Fail and restore
   the backup if any doc cannot be materialized.
3. Write the v3 layout: current `meta.loro`, `tree.loro`, `docs/*.loro`, blobs
   and `space.json`. Drain or explicitly discard unacknowledged pending/outbox
   rows according to a printed report; never silently claim they were sent.
4. Move the old `journal.sqlite3` and its auxiliaries to the backup. Put a
   non-SQLite placeholder at the old journal path so a stray 0.5.5.2 daemon
   fails closed (`file is not a database`) instead of creating an empty space.
   The v3 opener removes the placeholder only after validating `space.json`.
5. Reopen v3, run a full state hash check, then start the daemon. A failure
   restores the entire backup; after the first v3 write, rollback is only via
   the backup and the v2 binary.

`space.json.layoutVersion` is checked before opening. A v3 daemon refuses an
older layout with a coded error and points at this migration; a v2 daemon cannot
be made to understand v3, hence the placeholder and the maintenance window.

## Upgrade order and compatibility

1. Stop all daemons that can write the affected spaces; take backups.
2. Upgrade the binaries; run the v3 migration on each node that owns local
   state (replicas migrate their own current state independently).
3. Start 0.5.5.3 nodes; let them exchange full state/state-vector updates.
4. Only then reopen user traffic. Do not mix a live v2 writer with a migrated
   space.

Protocol handshake advertises `layoutVersion/protocolVersion`. A v2 peer is
rejected with `ORGFS_LAYOUT_UNSUPPORTED` (new frozen code) before any update is
applied; there is no partial v2/v3 merge.

## N-1 compatibility (decided)

0.5.5.3 does **not** export to 0.5.5.2; the break is direct. It only reserves a
protocol version and a capability-negotiation field in `space.json`/hello so a
future release can implement the export without a layout rewrite.

From 0.5.5.4 onward, each release supports a **read-only export to exactly its
immediate predecessor (N-1)**, and only one N-1 step is retained. The export
emits the current full Yjs state through an explicit compatibility
`stateSnapshot`/`baseline` marker to clients that advertise the capability; it
is not a v2 journal replay and never permits v2 writes. The capability is
advertised as false in 0.5.5.3, and a v2 peer is rejected with
`ORGFS_LAYOUT_UNSUPPORTED` before any update is applied. The N-1 export window
expires when the next release ships.

## Future history, if reintroduced

History is a product feature, not the sync authority. Re-add it as an optional
append-only audit sidecar (per space, outside the hot document path) written
asynchronously from accepted current-state changes; retention is explicit and
can be off. `fs history`/`restore` read that audit, and `restore` produces a new
current update. It must not reintroduce `commits` as the source of truth, and it
must never be required for a peer to catch up. If snapshots are needed for
audit, they are retained audit artifacts with a declared retention, not sync
baseline machinery.

## Decisions (2026-10-29, Allen/#2104)

1. **Write persistence:** private per-document delta file plus periodic merge
   (about 500 updates or 1 MiB), state + unmerged delta on read. Delta files are
   internal, not exposed, and never become history.
2. **Trash:** keep trash; add `fs rm --permanent` to delete without trash.
   Trash/restore only touches the deleted node's last current state.
3. **N-1:** no export in 0.5.5.3 (direct break); reserve protocol version and
   capability negotiation now. Starting 0.5.5.4, each release supports a
   read-only export to exactly N-1.
4. **Audit history:** not implemented now. Keep the state model and an optional
   future append-only audit sidecar possible; do not make audit a sync
   dependency.
