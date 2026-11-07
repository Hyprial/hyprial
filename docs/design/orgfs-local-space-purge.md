# OrgFS local space purge rule

`hyprial fs purge <space_id>` permanently removes one replica from the current
node. It removes that space's live registrations, checkout, replica data,
store directory, `journal.sqlite3`, and blob references. It does **not** create
or broadcast a replicated deletion, so peers keep their copies. Repeating the
command after completion succeeds with `purged:false`.

The daemon applies this conservative rule before writing the durable purge
intent:

1. The current local principal must equal the space's immutable owner.
2. The space must have no member other than that owner.
3. A space whose reserved name binds it to an organization (the directory
   `group-*` space or its `-acl` space) may be purged only when
   `directory/org.json` says `state: "deleted"`, or when a durable local
   deleted-org marker preserved from an earlier purge says so. Missing,
   malformed, active, or unknown org state is refused. A space without an org
   binding may be purged.

Directory and ACL spaces are independent purge targets. Purging either never
selects or removes its paired space; the operator must name and confirm each
space separately.

Before detaching state, the daemon opens every ancestry component with
`O_NOFOLLOW|O_DIRECTORY` from a filesystem-root descriptor and records each
`st_dev`/`st_ino`. Deletion walks that same descriptor-anchored chain again,
refuses any changed identity, and removes descendants only with `dir_fd`
operations. It never reopens the mutable absolute ancestry after validation.

A per-space intent under `orgfs/purge-intents` is fsynced before deregistration.
Before releasing blob references, the detached phase and complete digest cleanup
set are replaced and fsynced in that intent. Startup excludes intended spaces
from registration and resumes both tree and blob-byte cleanup. The intent is
removed only after cleanup and readback confirm that registrations and both
trees are absent.
