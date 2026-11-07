# Agent-private grant value authority

`SecretSource.AGENT_PRIVATE` resolves from the current agent home's
`secrets.sqlite`, owned by `AgentSecretStore`. The grant's `entryId` is the
store's `service`; `fieldName` is its `field`. Without a legacy source file,
a fieldless grant selects `VALUE`. Source migration binds fieldless grants to
an explicit field as described below. An agent-private grant never selects another
agent's database. Explicit agent-shared grants and default Claude credentials are
specified in [default-claude-credential.md](default-claude-credential.md).

- The agent entity token and home receipt token must match the grant before
  value access or migration. Existing home receipt validation remains required.
- The home and database must satisfy owner-only access checks (0700 and 0600
  on POSIX). A corrupt or unsafe database fails closed.
- `set` and `rotate` affect the next resolution. `remove` makes resolution fail;
  a grant does not retain an earlier value.
- Agent-private catalog paths identify `secrets.sqlite`, not a per-entry file.
- A missing value retains code `AGENT_SECRET_RESOLUTION_FAILED`, category
  `missing`, and the entry id. The message includes the `agent secret set`
  command with agent/service/field options and stdin instructions. Unknown
  services require `--no-validate`; validated services require their complete
  field set. Legacy field bindings that are not valid environment names also
  receive a grant-update command. No message includes a secret value.

## One-time legacy migration

After authorization, resolution transfers only the named direct file
`<agent-home>/secrets/<entryId>` into SQLite. It does not discover other files.
The resolver then reads SQLite; the file is never a value fallback.

The unit of migration is the source file, independent of grant resolution order.
The importer stores the exact UTF-8 text and every selectable JSON string field.
JSON keys must be safe grant field names to become fields; the exact text also
preserves non-string members. Raw text uses `VALUE` unless the JSON document
already contains that key. In that case it uses the first unused
`LEGACY_RAW_<positive integer>` key, starting at 1. This choice depends only on
the source and remains stable across retries.

Empty files, grants selecting invalid fields, unsafe types/permissions, files
over 1 MiB, and platforms without no-follow descriptor I/O fail closed.

Migration records `secret_events.action=migrate`, one fingerprint-only event
per imported field (`actor=system`, `via=daemon`, `result=ok`). These rows are
available to explicit grants. A migration event alone does not authorize the
separate implicit SQLite-to-environment materializer.

SQLite commit and verification precede grant rebinding and source deletion.
The importer checks the complete field set and values under a SQLite write
lock. It then checks every current-incarnation agent-private grant for that
source in a registry transaction. Structured grants retain their field names
and revisions. All fieldless grants receive the raw-text field name and a
revision increment, committed together before unlink. The resolver refreshes
its grant after migration, including migration by another resolver.

A crash after value import or grant rebinding leaves the source for a retry;
all grants remain resolvable after restart. A retry does not increment an
already rebound grant or duplicate migration events. Existing
SQLite rows or successful value history are never overwritten by an import.
A mismatch fails closed and preserves the source for operator reconciliation;
in particular, an interrupted import cannot restore a removed value. Migration
creates no plaintext backup. An empty legacy directory is removed.

New agent homes do not create an empty `secrets/` directory. The retired
`SecretResolver.write_agent_secret` file writer is removed. Agent destruction
also removes the private SQLite value store and its key/sidecars before deleting
the home receipt; unknown files still prevent automatic cleanup.

## Scope boundary

`secrets/native/{claude,codex,pi}/` remains unchanged. Third-party harnesses read
those native login files from disk; their redesign belongs to the separate
0.5.5.2 secrets cleanup (#2008). Only the `native` directory is excluded;
a private regular file literally named `native` migrates normally, and a
symlink is refused. Ungranted legacy files are not deleted. A
nonempty `secrets/` directory therefore remains. Home-level `secrets/login.json`,
`secrets/providers/*.json`, and `secrets/lark-*.json` are also unchanged.

## Regression coverage

- `tests/identity/agents/secret_storage/test_grant_resolution.py`
- `tests/identity/agents/secret_storage/test_legacy_migration.py`
- `tests/identity/agents/secret_storage/test_shared_legacy_source.py`
- `tests/shell/cli/agents/secret/test_grant_resolution.py`
- `tests/identity/agents/home/roots/lifecycle/test_destruction.py`
