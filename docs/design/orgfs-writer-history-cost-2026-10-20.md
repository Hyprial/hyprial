# OrgFS writer lane: history-proportional sections (2026-10-20)

## Symptom

The production group-internal org space (`a2b5fb64`, org directory plus
send-links files) has 45,330 tree commits, a 566 MB `journal.sqlite3` and
91k replica log files. Its writer thread `orgfs-space-a2b5fb64-…-writer-0`
held one core at 69-86% in pure Python. It held the GIL, so IPC and zenoh
threads starved and `hyprial org list` / `fs ls` on that space timed out at
15 s. The CPU use came back about 6 minutes after a restart with almost no
new commits. The samples showed `PyNumber_Lshift`, `binary_iop`, `len()`,
`PyUnicode_DecodeUTF8Stateful` and `tuple_dealloc`.

## Root cause

Every operation that `OrgSpaceAuthority` runs is one section of a single
worker lane. While a section runs, every other read or write on that space
waits behind it, including callers that hold the facade space lock. Three
read operations scanned the whole journal of a document on every call:

- `export_since`, the sync and replica-priming page source, ran
  `SELECT * … WHERE doc_id = ?` over every row. It decoded the row's version
  vector and the caller's vector for every row (`_read_var_uint`:
  `value |= (byte & 0x7F) << shift` inside `while offset < len(data)`). It
  then built the page by JSON-encoding the growing selection again after each
  added envelope. A peer that was already up to date still paid for the full
  scan. A peer paging from an empty vector paid for the full scan on every
  page, so a full pull cost O(n²).
- `document_authors` JSON-decoded every envelope of the document. The
  directory scan calls it for each device document. It runs from the ~1 s
  maintenance tick: `_reconcile_forwarding_endpoints` → forwarding
  `list_reachable_endpoints` → `directory_peers_for_app` → `dial_peers` →
  `service.directory_peers()` → `list_devices` → `_scan`. `snapshot()`
  repeated the same scan to count authors.
- `document_covers_member_add` decoded every envelope and state vector until
  it found a covering row. It ran the full scan again on every call.

None of these calls depends on new writes. Peer syncs (every ~60 s per peer,
plus every event-driven sync), replica priming and the directory tick keep
running them, so the load returns soon after each restart.

## Fix

The fix leaves the on-disk format unchanged and deletes no data. Every
in-memory index is rebuilt from the journal after an open. The fix also
clears every index after `delete_retired_objects`, because `VACUUM` can
renumber rowids.

- Normal commit and import histories are monotone: each row's `version` is
  the live document state immediately after that row was applied, so each
  version covers the preceding version. Replacement installation once allowed
  a pre-existing target document, however, and could append a disjoint reset
  state. It now rejects any target that already has journal rows before it
  changes in-memory or durable state. Existing journals are handled safely:
  the cached rowid index certifies the coverage relation as it extends, with
  O(1) amortised work per new row. On the first violation the document is
  marked non-monotone, one diagnostic is logged, and `export_since` uses the
  old correct full scan for that document. Certified histories use binary
  search for the first uncovered row, costing O(log n) version decodes; an
  up-to-date peer costs one decode after the index is warm. The page reads
  only its own envelopes, in chunks, and computes its encoded size
  arithmetically. Pages and cursors are byte-identical to the old algorithm
  for sequential, out-of-order multi-writer and legacy replacement-reset
  histories.
- `document_authors` and `snapshot()` share an index of per-document author
  counts and the latest origin. Each call reads only rows newer than the
  last indexed rowid.
- `document_covers_member_add` caches its last answer for each
  (document, user) pair under the required frontier. A `True` answer is
  final. A `False` answer resumes scanning after the last row it checked.
- No index is read or updated while the connection is in an open
  transaction, so a row that is later rolled back is never cached.
- `OrgSpaceAuthority` records the longest section and its operation kind
  (`max_section_seconds`, `max_section_kind`), so the space can expose it
  later.
- The commit path no longer copies the whole live document into a fresh
  working document and re-applies it on every commit. Each writer keeps a
  working document that is updated with each committed delta.

## Not changed

- `unbroadcast()` still scans the `commits` table in SQLite for
  `broadcast_at IS NULL`. That scan runs in C without the GIL, but it still
  holds the lane for its duration. A partial index would need a schema
  addition.
- The non-replica `_sync_page` path in `network/queries.py` still flattens
  every missing envelope for each response page. Each `export_since` call is
  now bounded, but a far-behind peer without a replica still costs the
  supplier O(missing) per page.
- `read_text` and `listdir` read the facade's in-memory projection. They
  never replay history. They wait only on the space lock, and a holder of
  that lock waits only on the now-bounded writer sections.
