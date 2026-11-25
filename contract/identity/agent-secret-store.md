# Agent-private grant value authority

`SecretSource.AGENT_PRIVATE` resolves from the current agent home's
`secrets.sqlite`, owned by `AgentSecretStore`. The grant's `entryId` is the
store's `service`; `fieldName` is its `field`. A fieldless grant selects
`VALUE`. An agent-private grant never selects another agent's database.
Explicit agent-shared grants and default Claude credentials are specified in
[default-claude-credential.md](default-claude-credential.md).

- The agent entity token and home receipt token must match the grant before
  value access. Existing home receipt validation remains required.
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

## Legacy plaintext file window closed

Automatic migration from `<agent-home>/secrets/<entryId>` into `secrets.sqlite`
has been retired. Resolution reads only `secrets.sqlite`; any unmigrated
plaintext file under `<agent-home>/secrets/` is ignored and never acts as a
value fallback. New agent homes do not create an empty `secrets/` directory,
and agent destruction removes `secrets.sqlite` and its key/sidecars before
deleting the home receipt while unknown files still prevent automatic cleanup.

## Scope boundary

`secrets/native/{claude,codex,pi}/` remains unchanged. Third-party harnesses read
those native login files from disk; their redesign belongs to the separate
0.5.5.2 secrets cleanup (#2008). Home-level `secrets/login.json`,
and `secrets/lark-*.json` are also unchanged. User model Secrets have the explicit
opt-in [model service routing](model-service-routing.md) extension; ordinary
`secrets/providers/*.json` records are not automatically migrated.

## Saved user entries and HTTP grant revocation

`agent.secret.provider-list` is an operator-only, read-only catalog of existing
`H/secrets/providers/<entryId>.json` files. It accepts `{}` and returns
`{ok: true, entries: [{entryId, fieldNames, routes?}]}` in entry/field name order.
Optional non-secret routes appear only for explicitly configured model services.
Ungranted entries are included: absence of an Agent grant does not hide a saved
entry. Values, paths, login/adapter files, Agent-private SQLite and native Auth
are never returned or enumerated. The catalog is not an authorization record
or a model authentication check. Empty storage returns an empty array without
creating directories.

Only direct `.json` names with the existing safe entry syntax are considered;
temporary or unrelated names are ignored. Directories and symlinks masquerading
as entries, hardlinked files, unsafe permissions, invalid JSON/string-field
maps, files over 1 MiB and inventories over 1000 entries refuse the complete
observation with sanitized `AGENT_SECRET_RESOLUTION_FAILED`. These observation
limits do not change existing write or launch semantics. Reads use the kernel
private-file descriptor checks and never repair permissions. Enumeration is an
observation, not an atomic snapshot or a lease against later local file changes.

The method uses the existing local-operator IPC admission and authority gate.
An `owner-keys`, damaged or injected alternate authority refuses this local
file catalog; it cannot fall back to files behind an active alternate authority.
The writer remains `agent.secret.provider-write`, and authorization remains
`agent.secret.grant`. This adds no U2, OrgFS or identity requirement to their
existing local path.

`agent.secret.revoke` now has an HTTP declaration for the existing IPC handler:
`{actor, grantId}` returns `{ok: true, revoked: boolean}`. It is an operator
write with `retry=never`; an unknown reply must be resolved by reading grants,
not automatically replayed. HTTP refuses `expectedAgent`, because the legacy
handler does not implement that supplied incarnation fence. Revocation does not
delete the saved entry, stop an Agent or erase credentials already delivered to
a running process. It prevents subsequent grant-based resolution; restart is
required to discard the old process environment. Existing IPC compatibility
and alternate-authority routing are preserved.

## Regression coverage

- `tests/daemon/ipc/contracts/setup/test_secret_catalog.py`
- `tests/integration/request_http/setup/test_setup_secret_http.py`
- `tests/identity/agents/secret_storage/test_grant_resolution.py`
- `tests/shell/cli/agents/secret/test_grant_resolution.py`
- `tests/identity/agents/home/roots/lifecycle/test_destruction.py`
