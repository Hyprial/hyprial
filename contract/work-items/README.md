# Work documents in ordinary OrgFS

Current contract after the #2214 simplification. Work documents are ordinary
CRDT text in the selected organization's directory space, under `work/`.
There is no work-specific write API, admission policy, audit graph, repair
protocol, or approval ledger. The retired [journal contract](journal-history.md)
describes the old implementation only.

## Read and edit

The only `org work` commands are read shortcuts:

```sh
hyprial org work ls --org "<org>" --json
hyprial org work show "<filename>" --org "<org>" --wait 10 --json
```

`ls` resolves the organization and calls `orgfs.ls` on `work`. `show` calls
`orgfs.read` on `work/<filename>` and returns its ordinary result: text
documents include the original `text` and `version`; binary nodes retain the
ordinary binary read shape. The filename is used as supplied, including names
without an extension; `.md` is not added. A relative subdirectory is allowed. There are no task filters,
`--invalid`, or `--copy` options. These commands do not parse or rewrite the
document. Organization resolution keeps its explicit/current/unique selection
rules and may clear an unavailable local current selection; the document read
itself does not write OrgFS.

Use `hyprial org list --json` to identify the organization's actual directory
space ID; do not construct an ID from its display name. The generic commands
use that ID. Replace the angle-bracket placeholders with the actual values:

```sh
hyprial org fs ls "<space-id>" work --json
hyprial org fs read "<space-id>" "work/<filename>" --json
hyprial org fs write "<space-id>" "work/<filename>" "<complete updated text>" \
  --expect-version "<version returned by read>" --json
hyprial org fs read "<space-id>" "work/<filename>" --json
```

`write` takes the complete desired text as its third positional argument;
there is no `--file` option. Preserve unrelated text, unknown fields, and
line endings when constructing it. `--expect-version` is the strict compare
and set condition (`orgfs.write.expectVersion`). On a stale version, re-read
and reconcile the changes before a new write. Do not silently retry an old
document over a newer one. `--base-version` selects a merge baseline and is
not a substitute for this strict condition.

For a new file, ordinary `org fs write` creates a missing path without a
baseline, but can also update an existing path. It does not promise atomic
create-if-absent through the CLI. A client needing that guarantee uses the
existing ordinary `orgfs.write` request with `createOnly: true`, for example:

```json
{"spaceId":"<space-id>","path":"work/<new-filename>","text":"<complete text>","createOnly":true}
```

Do not pass an empty `expectVersion` as a create condition. No automatic task
ID allocation or global sequence guarantee is provided. Existing document
names, including names that do not match `M-*.md`, remain valid OrgFS names.

## Views and authority

A reader may interpret YAML metadata and Markdown as a task view (the PAC
overview reads the first fenced `yaml` block): for
example `id`, `status`, `owner`, `assignee`, `checklist`, `depends`,
`acceptance`, `opened`, `closed`, `keywords`, and `pacs`. This schema is a
read-only projection, not a storage or authorization constraint. Unparseable
metadata, unfamiliar states, and extension fields do not make the original
text unreadable or unwritable through OrgFS. A view may report diagnostics;
it must not repair the source on read. PAC overview may use the current text
for grouping, with its existing preview redaction and size limits.

The space's ordinary OrgFS permissions decide who may write. `owner`,
`ownerActions`, `verified`, a checklist tick, a log entry, or `status: done`
does not prove approval or grant permission. Old fields remain document
content. Neither a local ledger nor a complete journal is required to read or
interpret the current work text. Cross-node signed approvals are not supplied
by this model; workflows needing enforced approval must use a separately
specified authorization mechanism, such as an explicit PAC approval node.

There are no `work.*` IPC methods or dedicated task mutation, association,
collect/finish, or asked/declined state-machine commands. A deliberate change
to a `pacs` field is an ordinary conditional text edit. PAC execution and its
own graph history remain separate from the work document.

The packaged Squire template follows this contract. Existing customized agent
skills and routine definitions are not rewritten automatically. Before resuming
an old association routine, review its instructions against this contract and
remove its dependency on retired commands; do not treat old collect/finish
output as an input to the new read shortcuts. Updating repository templates is
not evidence that a deployed agent's private copy has been updated.

## Conflicts and recovery

Use the ordinary node identity interfaces. For same-name copies,
resolve first, inspect each `id:<nodeId>`, and obtain the intended resolution
before moving or deleting a copy:

```sh
hyprial org fs resolve "<space-id>" "work/<filename>" --json
hyprial org fs read "<space-id>" "id:<node-id>" --json
hyprial org fs mv "<space-id>" "id:<node-id>" "work/<distinct-filename>" --json
```

When deletion or restoration is intended, use the standard soft-delete/trash
commands. `restore` takes only the node: it brings back the node's last state
before deletion together with the descendants removed in the same batch. It
does not certify a task state or find a special last-valid work version:

```sh
hyprial org fs rm "<space-id>" "id:<node-id>" --json
hyprial org fs trash list "<space-id>" --json
hyprial org fs trash restore "<space-id>" "id:<node-id>" --json
```

OrgFS no longer offers a node history: `hyprial org fs history` and
`orgfs.history` were removed (orgfs-minimal-plan Q5), and text overwritten
before deletion cannot be restored. Soft delete, `trash list` and
`trash restore` remain. Removing the
extra work audit graph does not remove those interfaces. This change neither changes the
disk format nor migrates, rewrites, or deletes documents, journal data, unknown
CRDT roots, or old local approval-ledger files. Do not run the historical Yjs
state-only cutover or ledger cleanup as part of this simplification.
