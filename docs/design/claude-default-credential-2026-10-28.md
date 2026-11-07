# Default Claude credential: provider login, creation grant and launch guard

Status: revision contract, 2026-10-28; supersedes the contract at `a94104a3c`.
Owner ruling R1 folds default designation into provider login/default. R2 accepts
an agent's private native root without requiring a credential file.

## CLI and ownership

- `hyprial agent provider login claude --setup-token --actor OWNER [--default]`
  reads one long-lived token from non-TTY stdin. Obtain it with `claude setup-token`.
  `--source` is incompatible with `--setup-token`. The actor is the sole owner;
  the value is written once to that agent's SQLite `claude` entry, field
  `CLAUDE_CODE_OAUTH_TOKEN`. It is never placed in argv, logs, ordinary files or
  `secrets/`. Existing values require `--force`.
- `hyprial agent provider login claude --actor OWNER --default` keeps the native
  copy operation. The default references that owner's installed native login.
  Copying a refreshable OAuth login to several agents makes their refreshes
  interfere. `--setup-token` is recommended for defaults that many agents share.
- `hyprial agent provider default [--clear]` reads/clears the single default.
  Clearing affects future creation only; existing grants and native copies stay.
- Remove `agent secret default` and IPC `agent.secret.default`. There is no second
  designation entry point. `agent.provider.login` accepts `setupToken` and
  `makeDefault`; default changes require the host operator. Native login without
  default retains its existing IPC shape and behavior.
- Existing schema-1 provider-name defaults remain readable for CLI compatibility.
  Re-run login with an explicit owner to bind a source for PAC/lifecycle seeding;
  the daemon never discovers or reads ambient operator login files.

## Persistence and fences

The default is non-secret metadata: provider, kind (`setup-token` or `native`),
owner actor and entity token; setup-token also names entry and field. Registry
SQLite owns reference selection and creation grants. The setup-token value stays
in the owner's `secrets.sqlite`; consumers store only a grant. Owner and consumer
incarnations are checked; same-name recreation never inherits authorization.
Native default copies use only the named owner's verified home and the existing
native installer. Native refresh custody and native-to-SQLite migration remain
out of scope. No setup-token is converted into a native file.

New Claude records receive an ordinary `default-claude` consumer grant in their
creation transaction. Grant inspection/revocation uses the existing secret CLI.
Revocation is not repaired on restart; setting/changing/clearing defaults does
not alter existing consumers. Rotation is observed on next resolution.

## Creation decision table

The identity creation owner applies this policy to CLI create, PAC `_ensure_agent`
and lifecycle creation, not only the CLI wrapper.

| State | Result |
|---|---|
| Different harness, visitor or transfer record | Do not apply this default |
| Claude, no default | Create without credential; launch guard decides |
| Claude, setup-token default | Explicit shared grant in creation transaction |
| Claude, native-copy default | Seed own native root from named owner's login |
| Existing explicit auth grant or existing record | Do not override or repair |
| Stale default owner | Refuse; never choose another owner |

## Managed launch decision table

Round 2 scope: the SDK guard remains unchanged. PTY launches (including
command overrides) enforce this table for workflow-owned or launch-created
agents. Use the existing strict PAC desired-state ownership marker, never an
actor-name prefix. New `_ensure_agent` and lifecycle-created records also store
`capabilities.creationOrigin = "managed-launch"` in their creation transaction;
composition carries that fact to the connector. Existing operator-created
records are not relabeled by reuse or registration. Old workflow incarnations
with a matching strict PAC desired-state marker retain that provenance on reuse.
The shell interactive CLI checks marked legacy agents before subprocess/tmux
spawn using the existing daemon home receipt and shared source validator; it
adds no new grants transport. Operator interactive CLI is otherwise not tightened
in this round; that work remains part of 0.5.5.2 secrets.

Operator-created resident PTY sessions report `env-grant`, `agent-native`, or
`ambient` without refusing solely for missing credentials. `ambient` means the
launch lacks an admitted explicit/own-native source, not that a login was
inspected or found. Daemon-composed P1 resident launches use the same own
config/session roots in round 3; unsafe storage still refuses. Do not inspect
ambient files or the Keychain. Tightening resident credential admission is
deferred to the 0.5.5.2 secrets work.

### Round 3: P1 Claude state belongs to the agent home

Let `A` be the verified agent home. P1 Claude composition replaces any inherited
`CLAUDE_CONFIG_DIR` with `A/secrets/native/claude`, creating missing directories
at 0700. The Claude adapter links `projects` to `A/state/sessions/claude` (the
P2 convention). It rejects foreign links/collisions; it does not overwrite or
move existing transcripts. The cell is not a storage authority. HOME itself
retains P1 semantics; Claude config and transcripts do not use host `~/.claude`.
The `agent.home.legacy_path reason=no_config` warning remains a warning.

| Source | Result | CLAUDE_CONFIG_DIR | projects |
|---|---|---|---|
| One explicit OAuth/API/auth env grant | `env-grant`; no daemon login fallback | Own native root | Own session root |
| Pre-existing verified private own native root | `agent-native`, including no `.credentials.json` (Keychain-only admission) | Own native root | Own session root |
| Fresh launch-provisioned empty native root, no grant | `missing`; refuse before spawn, including retries | Own native root | No host fallback |
| Own native file exists, no env grant | Keep existing regular-file/privacy/nonempty checks | Own native root | Own session root |
| Ambient/default/another agent's root | Never use or inspect it | Replace with own root | Own session root |
| Conflicting auth methods | Refuse | Own native root | No spawn |
| P2 | Existing policy remains in force | Existing P2 mapping | Existing P2 mapping |

P1 explicit auth retains precedence over an existing own native login; the P2
native/env coexistence refusal is not added to P1.

Directory creation is not credential authorization. Provisioning must distinguish
new empty roots from the pre-existing roots admitted by round 1, without reading
Keychain or requiring a credential file for those existing roots. Source admission
never asserts successful provider authentication.

R&D ruling (`4709fa2b-61c3-53b2-b2ad-edbf384251aa`): keep round-1 admission;
record negative provision provenance across retries. This implementation uses
the approved fixed marker alternative, `.hyprial-launch-provisioned`, an empty
non-secret file in the native root. The identity native installer has no registry
parameter; keeping the marker beside its installation target lets all callers
clear it through that one authority. New roots are staged with the marker before
publication. Successful explicit native install/seed removes it; failed install
does not. An env grant bypasses the negative provenance but does not erase it:
revocation must not turn an env-only root into a native login. Do not manually
remove the marker or infer authorization from arbitrary config/session files.

Codex/Pi follow-up (no implementation in this change): P1 still preserves ambient
`CODEX_HOME` and `PI_CODING_AGENT_DIR`/HOME. Codex auth.json and Pi auth.json plus
models-store.json therefore have the corresponding legacy ownership gap; their
config, credential and session isolation require separate scoped work.

Do not query the Keychain. Source admission is not proof of provider validity.
`CLAUDE_CREDENTIAL_MISSING` stays a permanent start failure and is preserved in
PAC `reasonRef`, not replaced by `PROVIDER_AUTHENTICATION_FAILED`.

## Diagnostics, compatibility and verification

Keep `worker.claude.credential_source` (`env-grant`, `agent-native`, `missing`,
`ambient`) and
bounded `worker.readiness.waiting` stage diagnostics. No values or fingerprints
are logged; timeout and retry behavior do not change. SDK and workflow-owned
PTY ambient auth fail closed; resident PTY remains the documented exception.
Consumers restart to observe rotation/revocation.

Contract-Impact: provider login/default metadata and removal of secret-default
IPC/CLI require synchronized `contract/` and CLI snapshot.
Skill-Impact: guide and packaged skill must use provider login/default, explain
stdin and ownership, and warn against refreshable native copies.

Commit order: contract, RED tests, GREEN, evidence. Run focused tests, ruff,
structural/module gates, CLI snapshot test, and isolated fake-token daemon/PAC
E2E in `/tmp/hr-claudecred-*` with `NO_PROXY=127.0.0.1,localhost,::1`.
Cover token default/start, missing default/PAC reason, own native root without a
file, clear preserving old grants while new workers fail fast, workflow-owned
PTY refusal before spawn, and resident PTY ambient compatibility. Do not run
the full suite, real provider auth or Keychain. Never push. Append evidence to
`docs/notes/claude-default-credential-2026-10-28/evidence.md`.
