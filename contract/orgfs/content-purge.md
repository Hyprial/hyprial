# Owner content purge contract

IPC methods: `orgfs.purge_plan`, `orgfs.purge`, `orgfs.purge_status`

```json
{"spaceId":"<space-id>","targets":[{"kind":"doc-range","node":"id:<node>","from":"all","to":"all"}]}
{"spaceId":"<space-id>","planId":"<plan-id>"}
```

`orgfs.purge_plan` returns the frozen plan (`plan_id`, `docs`, `blobs`,
`snapshots`, `writers_affected`, `authors_before`, `expires_at`).
`orgfs.purge` executes it; `orgfs.purge_status` reports participants.

Every one of these methods is owner-only and refuses any other author with
`not-owner`, member or not. The `orgfs.*` bridge publishes result fields in
`snake_case`; the dataclass `to_dict()` shape additionally names the same
fields in camelCase and is not the wire form.

## `orgfs.purge` response

```json
{
  "ok": true,
  "plan_id": "<plan-id>",
  "executed_locally": true,
  "acknowledged": ["<node-id>"],
  "pending": [],
  "replacements": [
    {
      "doc_id": "<retired-doc-id>",
      "replacement_doc_id": "<installed-doc-id>",
      "snapshot_id": "<sha256-hex>",
      "snapshot_sha256": "<sha256-hex>"
    }
  ]
}
```

`replacements` is the purge's safe receipt: one entry per committed
replacement, sorted by `doc_id`. It carries identifiers and digests only —
never document content, a path, a credential or a raw error.

- `doc_id` — the retired physical document.
- `replacement_doc_id` — the rebuilt document installed in its place.
- `snapshot_id` — the identifier the committed retirement record and the
  published snapshot point both carry.
- `snapshot_sha256` — the sha256 of the installed replacement snapshot bytes.
  It equals `snapshot_id` by construction: the store refuses to install bytes
  whose digest differs from the committed record.

Every value is derived from committed authoritative records (the retirement
record plus the published snapshot point, `planId` and `snapshotId` agreeing).
A plan that is unknown, stale, expired or not committed never reports a
replacement: it fails with `stale-plan` and the standard failure object, which
has no `replacements` key.

Replaying a plan whose purge is durably complete (this node acknowledged it)
returns the same response, with an identical `replacements` list, instead of
re-executing it or expiring it. The daemon restarting between calls does not
change that list: it is re-derived from the same persisted records.
