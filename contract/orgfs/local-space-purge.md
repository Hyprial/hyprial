# Local space purge contract

IPC method: `orgfs.space_purge`

Request:

```json
{"spaceId":"<space-id>"}
```

A successful response is one of:

```json
{"spaceId":"<space-id>","purged":true,"localOnly":true}
{"spaceId":"<space-id>","purged":false,"localOnly":true}
```

`purged:false` means the local registration and replica were already absent.
`localOnly:true` means no peer deletion was broadcast.

The method can reject with `space-purge-refused` and a `reason` of
`not-local-owner`, `other-members`, or `org-not-deleted`. It rejects an unsafe
target with `unsafe-space-path`. Once its durable intent exists, an interrupted
attempt reports `space-purge-incomplete` with `resumable:true`; daemon startup
or a later identical request resumes it.

A space still in the old journal layout (a `journal.sqlite3` directory without a
v3 `space.json`) is rejected with `ORGFS_LAYOUT_UNSUPPORTED`; its directory is
left untouched. Old-layout directories are removed by hand, as described in
`docs/guides/migration-orgfs-minimal.md`.

The CLI command is `hyprial org fs purge run <space-id> [--yes]`. Without `--yes`, it
requires interactive confirmation. `--json` without `--yes` returns the
standard `USER_ACTION_REQUIRED` failure object. JSON success uses the standard
single-object envelope and includes `ok:true` plus the fields above.
