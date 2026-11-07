# OrgFS sync wire contract

This contract covers queries on `hyprial/v1/org/fs/<space>/sync/<supplier>`.
All messages retain `schemaVersion: 1`; peers must ignore unknown object fields.

## Bounded stream exchange

A current requester starts or resumes with:

```json
{
  "schemaVersion": 1,
  "type": "orgfs-sync-stream",
  "spaceId": "<space>",
  "requester": {"node": "<node>", "author": "<principal>"},
  "cursor": null,
  "maxBytes": 1048576
}
```

The supplier answers with `orgfs-sync-stream-page`, its node ID, a base64
`envelopes` array, and nullable opaque `next`. The encoded response must not
exceed `maxBytes`. One handler invocation scans no more than the product's
fixed document limit. It can include envelopes from multiple documents.

After page one, the requester sends only the opaque `cursor` plus the stable
query envelope (`schemaVersion`, `type`, `spaceId`, `requester`, and
`maxBytes`). It does not repeat `docIds` or `versionVectors`. The requester
persists an acknowledged non-null cursor per `(space, supplier)` only after it
has durably admitted that page. A timeout retains the cursor, so a later
scheduled pass or requester restart resumes. Applying a page before persisting
its cursor is safe because journal envelopes are idempotent.

When the supplier returns `next: null`, that scan is complete and the requester
removes the durable cursor. A clipped final query that returns no reply is
reported as `deadline-expired-mid-sync`, not `no-reply`.

## Compatibility

Suppliers continue to accept the older `orgfs-sync` request and return
`orgfs-sync-page` or `orgfs-sync-snapshot`; this is the expand-first side of a
rolling upgrade. A current requester detects an old supplier from an
`invalid-argument` response to `orgfs-sync-stream`.

Fallback never sends the old unbounded full-space request. It sends only the
bounded set of documents already known locally and refuses local spaces above
`ORGFS_LEGACY_SYNC_DOCUMENT_LIMIT` with reason `legacy-peer-large-space`.
Requests that exceed `ORGFS_PAGE_BYTES` are refused with
`legacy-request-too-large`. This can delay discovery of documents known only
to an old supplier; it cannot recreate unbounded supplier work.
