# Default Claude credential and explicit shared grants

## Authority and creation

The token value exists once in the owning agent's `secrets.sqlite`. Registry
`agent_secret_defaults` stores only `harness=claude`, owner actor/incarnation,
entry, field and `kind` (`setup-token` or `native`). For setup-token the field is
`CLAUDE_CODE_OAUTH_TOKEN`. The schema upgrade gives existing references kind
`setup-token`. Native references name an explicitly selected owner's installed
login, never an ambient operator path.

Creating a local Claude agent adds `default-claude` revision 1 to
`agent_secret_grants` in the same transaction as its agent record. CLI, lifecycle
and PAC creation use this rule. Non-Claude, hosted visitor and transfer records
do not. A native default instead seeds the new agent's private native root using
the existing installer and the fenced owner's private login. It does not inject
an env grant. Copying refreshable logins can cause refresh interference; prefer
setup-token for shared defaults.
Existing agents, existing explicit auth grants and revoked grants are unchanged.
Default changes affect future creations only. Clearing the default does not
revoke outstanding grants. Setup-token creates no consumer value copy or secret file.

`source=agent-shared` adds `ownerActor` and `ownerEntityToken` to grant JSON.
These fields are null for other sources. Shared resolution checks consumer and
owner incarnation fences, the owner's home receipt and private SQLite access.
It reads only the named owner entry; it does not migrate consumer files.
Rotation affects the next resolution. Removal or owner replacement makes old
grants unusable. Consumer destruction cascades its grants. Running processes
must restart to observe rotation/revocation.

## CLI and IPC

- `agent provider login claude --setup-token --actor OWNER [--default] [--force]`:
  one token from non-TTY stdin, stored in owner's SQLite `claude` entry. No
  argv value or ordinary file is permitted. `--source`, non-Claude, blank,
  whitespace/NUL-containing or over-16384-character tokens are `INVALID_ARGUMENT`.
  Stdin is capped at 16385 characters (token plus one newline); excess is refused.
  An existing value requires `--force`. The owner receives `provider-claude`;
  future Claude agents receive `default-claude` when `--default` is set.
- `agent provider login claude --actor OWNER --default`: native copy plus fenced
  source designation. Claude defaults require an explicit owner. Native login
  without `--default` keeps its previous one-time behavior.
- `agent provider default [--clear]`: read/clear the source. Set/clear requires
  the host operator. `agent secret default` and `agent.secret.default` are removed.
- `agent secret grant CONSUMER --source agent-shared --owner-agent OWNER
  --entry-id ENTRY --field-name FIELD --environment-name ENV --grant-id ID
  --revision N`: explicit shared authorization; source/owner combination is
  validated. Existing grant revision rules apply.
- `agent secret ls [ACTOR] --json`: grant metadata. `list --agent ACTOR` retains
  local value metadata semantics. `revoke ACTOR ID` is unchanged.
- `agent.provider.login` accepts `setupToken` (sensitive, never returned) instead
  of `files`, and optional boolean `makeDefault`. Native `files` remain base64.
  The response includes `actor`, `provider`, and for setup-token
  `credentialSource:"setup-token"`. A default change also returns `default` with
  `provider`, `previous`, `credentialSource` and `defaultCredential` metadata.
- `agent.provider.default` returns `provider`/`previous`, adding
  `credentialSource` and `defaultCredential:{ownerActor,ownerEntityToken,entryId,
  fieldName,kind}` for a captured source. `agent.create.providerDefault` is this
  source object, the legacy provider string, or null. Clear removes the captured
  source and legacy name, but not grants or stored values. Legacy schema-1
  `provider-default.json` remains readable; CLI warns to re-run
  `login claude --default --actor OWNER` to enable PAC seeding. New captured
  Claude defaults use registry SQLite instead of that file.
  Agent-context default mutation is `CALLER_NOT_AUTHORIZED`. Provider login's
  `actor` is the target, not caller attribution; trusted worker origin,
  sessionRef and registered interactive peer fences still refuse mutation.
- `agent.secret.grant` accepts `ownerActor`; `agent.secret.list` and grant
  responses expose the two nullable owner fields. No response contains values.

## Launch and diagnostics

Managed P1 Claude SDK and workflow-owned PTY launch accept explicit auth
environment, or their own exact
verified private `secrets/native/claude` root. A credential file is not required
(Keychain-only native login); when present, file privacy/type/nonempty checks
still apply. No Keychain is queried. P1 Claude composition always sets
`CLAUDE_CONFIG_DIR=<agent home>/secrets/native/claude`, creating missing levels
at 0700; inherited host/foreign selectors never survive. This also applies to
env-grant launches (the former `state/claude-env` selector is retired).
`projects` links to `<agent home>/state/sessions/claude`, the P2 convention.
Unsafe roots and session-link collisions refuse without following or moving them.

A launch-provisioned root is atomically published with the fixed, empty,
non-secret `.hyprial-launch-provisioned` marker. Directory creation never grants
native auth: marked roots remain `missing` across retries without an env grant.
Successful explicit native installation/seed clears the marker; failed install
does not. Env grants permit launch without clearing it, so revocation fails
closed again. Pre-existing private roots retain file-less admission. Arbitrary
config or transcript files do not clear negative provenance. No new CLI/IPC
fields are introduced. The legacy no_config warning remains a warning.
Ambient daemon/default/another agent's native root cannot authorize these launches.
PTY includes configured command overrides. The strict PAC desired-state ownership
marker identifies existing workflow workers; actor names do not. New records
created by `_ensure_agent` or lifecycle launch store
`capabilities.creationOrigin:"managed-launch"` in the creation transaction.
Reuse of an operator-created record does not add this marker. An old workflow
incarnation with a matching strict PAC desired-state marker retains that fact
in creation metadata on reuse. The complete child launch carries this non-secret
provenance to the connector. Interactive CLI reuse also consumes the registry
metadata: a marked P1 agent uses the existing `agent.ensure-home` receipt to
validate its own native selector before subprocess or tmux spawn. No new secret
transport is added; absent auth in that launch is still refused. Operator
interactive CLI admission is otherwise not tightened in this round; it remains
part of the 0.5.5.2 secrets work.

Operator-created resident PTY launches are not refused this round. They report
`env-grant`, `agent-native`, or `ambient`; the last means no admitted explicit or
own-native source, not proof that an ambient login exists. Ambient files and
Keychain are not inspected. Resident PTY tightening is deferred to the 0.5.5.2
secrets work. Daemon-composed P1 resident PTY sessions now use the same own
config/session roots, even when reporting `ambient`; that class does not imply
an ambient selector is passed. Operator interactive CLI without a composed
agent-home launch retains round-2 admission compatibility.
P1 explicit auth retains precedence even when its own native login exists;
P2 native validation and explicit-auth conflict rules remain in force.
Codex `CODEX_HOME` and Pi `PI_CODING_AGENT_DIR`/HOME (auth.json and
models-store.json) retain the corresponding P1 ambient gap as separate follow-up;
this change does not alter their launch behavior.
Native root admission is not proof that a login exists or that vendor auth works.

Missing credentials fail before spawn with permanent
`CLAUDE_CREDENTIAL_MISSING`. No provider request occurred; this is not
`PROVIDER_AUTHENTICATION_FAILED`. PAC journal `launch_failed` includes nullable
`errorCode`; the workflow `reasonRef` for this error is
`pac:launch_failed:CLAUDE_CREDENTIAL_MISSING`.

`worker.claude.credential_source` reports closed `credentialSource` values:
`env-grant`, `agent-native`, `missing`, `ambient`. The logger permits only these values for
this field; arbitrary credential data remains redacted. No values or
fingerprints are logged. `worker.readiness.waiting` reports `stage` and
`elapsedSeconds`. SDK spawn/ready waits report initially and after cumulative
10/30/60 seconds, with no additional deadline or retry. Admission reports once
per waiting turn. Readiness does not prove provider authentication.

## Executable coverage

- `tests/identity/agents/secret_storage/default_credential/test_claude.py`
- `tests/daemon/harnesses/agent_sdk/driver/identity/test_legacy_credential.py`
- `tests/daemon/harnesses/claude_pty/test_credentials.py`
- `tests/shell/cli/agents/claude/test_profile.py`
- `tests/daemon/harnesses/providers/test_default_claude_credential.py`
- `tests/daemon/harnesses/providers/test_provider_setup_token.py`
- `tests/daemon/harnesses/agent_sdk/test_readiness.py`
- `tests/integration/claude_credential/test_default.py` (isolated daemon/PAC;
  disclosed fake Claude model protocol, not real provider authentication)
