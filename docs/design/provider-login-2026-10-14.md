# `hyprial agent provider login` — seeding an agent's harness credentials from the host CLI login

Status: design (2026-10-14). Owner: cli-developer. Scope approved by Allen
2026-10-14; the three open details below carry defaults that apply unless he
says otherwise.

## Problem

An agent runs in an isolated HOME, so it does not inherit the operator's
`~/.claude`, `~/.codex`, or pi configuration. A claude-backed agent therefore
fails at launch with `CLAUDE_CREDENTIAL_MISSING`
(`daemon/impl/harnesses/claude/runtime.py:198`) unless someone places a
credential in the agent's own native secrets tree.

Today half of that path exists: the API-key route
(`hyprial agent secret provider-write <provider> --field apiKey` plus
`agent secret grant <actor> --source user-provider`). What is missing is the
OAuth route — turning the host CLI's existing login into the agent's
credential — and a prompt at create time telling the operator that the agent
will need one.

This is the third instance of the same gap: autodl pi, nas-manager pi, and now
claude each failed because nothing seeds harness credentials at agent creation.

## Goal

`hyprial agent provider login <provider>` converts the host CLI's existing
login into the named agent's credential, once, with an explicit re-login path
when it expires or breaks.

## Decisions (Allen, 2026-10-14)

1. **Scope**: seeding is the default for **newly created** agents (`agent create`
   takes the provider from hyprial). The command also seeds a specific agent.
2. **OAuth**: a **one-shot seed**. When it breaks or expires, the operator is
   told to re-run the command. No refresh loop.
3. **Identity**: the credential belongs to **the user the daemon is logged in
   as**; whatever provider material that user supplies is accepted. The CLI
   help and output must say so, and must name the revoke path.
4. **Default posture**: API key stays the default (independent identity,
   measurable, revocable); seeding OAuth is an explicit choice.

## Surface

```
hyprial agent provider login <provider> [--actor <NAME>] [--default] [--force] [--source <PATH>]
```

- `<provider>` names the **harness** whose native login is copied: `claude`,
  `codex` or `pi` (revised 2026-10-19, review S4; see "Namespace and
  vocabulary" below). It is *not* the model-vendor vocabulary of
  `agent create --provider` (`deepseek`, `hyprial`, ...): the native files are
  laid out per harness, and an unknown name fails closed with
  `PROVIDER_UNSUPPORTED`.
- `--actor <NAME>`: seed this agent. Default target is the actor the CLI is
  already addressing.
- `--source <PATH>`: read the credential from this path instead of the
  documented location. Accepted only for single-file providers (`claude`,
  `codex`); `pi` reads two files, so one path cannot name its set and the flag
  is refused with `PROVIDER_SOURCE_AMBIGUOUS`.
- `--default`: record the provider as the **machine default** for **future**
  `agent create` runs. It does not retroactively seed existing agents.
  Recorded in the daemon's own configuration (`provider.default`), and
  settable only by a caller authorised for `agent.provider.login`; on a
  multi-actor daemon that means the operator who owns the machine's
  configuration, not every actor. Changing it changes what future
  `agent create` runs on that machine use, so the receipt names the previous
  value.
- `--force`: overwrite an existing non-empty credential.
- Not in v1: `--all`. A batch action across every agent is not auditable in one
  step; run the command per agent.

New daemon IPC method `agent.provider.login`, following the authorization and
audit shape of the existing `agent.secret.*` methods
(`daemon/impl/ipc/router.py`).

**Why a new method instead of reusing `agent.secret.provider-write`** (read from
the existing implementation, `shell/impl/cli/commands/agent/secret.py:16`): that
path refuses a TTY and accepts exactly one line of stdin, and it stores a value
in the secret store rather than a file. OAuth material is a multi-line JSON
document that the harness reads from a file, so neither transport nor target
shape matches. The new method carries file content and a filename.

**Where the host file is read**: in the **CLI process**, not in the daemon. The
CLI resolves the documented per-harness source path (or an explicit
`--source`), checks it is a regular non-empty file, and sends the bytes over
IPC. This keeps the daemon's file access unchanged (it never learns to read
arbitrary operator paths), and matches how `provider-write` already moves a
value from the operator's process into the daemon.

## What gets written

Seeding writes the **canonical native secrets tree**; the runtime already
projects it into the isolated HOME
(`identity/impl/agents/runtime/context.py:584-586`), so no second copy is
written by the CLI.

| harness | host source | agent target | constraints (read from source) |
|---|---|---|---|
| claude | `~/.claude/.credentials.json` | `<agent_home>/secrets/native/claude/.credentials.json` | must be a **regular file**, mode **0600**, non-empty; symlinks rejected; must not coexist with explicit auth environment (`claude/runtime.py:150-186`) |
| codex | `~/.codex/auth.json` | `<agent_home>/secrets/native/codex/auth.json` | mode 0600 |
| pi | `~/.pi/agent/auth.json` and `~/.pi/agent/models-store.json` (`$PI_CODING_AGENT_DIR` when set) | `<agent_home>/secrets/native/pi/{auth.json,models-store.json}` | **both files** — the 2026-10-13 autodl incident was two empty files, so a single-file seed is not a seed |

Verification step before implementation: confirm that for each harness the
isolated-HOME copy is generated **only** by that projection. If a harness needs
a second write, this design writes both and says so; the check is a task, not an
assumption.

## Failure semantics

The command reports exactly one of three states, and nothing else:

| state | when | what the operator sees |
|---|---|---|
| **seeded** | the files were written | path, byte count, whether an existing file was replaced — never a claim that the material is valid |
| **refused** | a rule above failed (unknown provider, unsafe path, exists without `--force`, ambiguous `--source`, missing or empty source) | the registered code and a message naming the fix |
| **not attempted** | the source could not be read at all | a refusal, ⛔ never a silent skip: no partial seed, no "assumed" success |

- Missing or expired material at *launch* is the harness's own permanent
  failure (`CLAUDE_CREDENTIAL_MISSING` is already
  `permanent_start_failure=True`). The error text and this command's help name
  the fix: `hyprial agent provider login <provider>`, or an API key.
- No silent fallback: the daemon never substitutes one provider's material for
  another's.
- Seeding claims "seeded", never "valid". Expiry cannot be judged at seed time,
  so the command does not judge it (`docs/design` tri-state honesty: unknown
  stays unknown).

## Validation rules

- Source must exist, be a regular file, and be non-empty; the source mode is not
  trusted as a substitute for setting the target mode.
- Target is written mode 0600, and re-checked after write.
- An existing non-empty target is not overwritten without `--force`.
- Repeat runs are idempotent: same source, same target bytes, no new revision.
- A provider whose material shape this build does not know is **refused** with
  an explicit code, rather than written speculatively.

## Tests (RED first)

1. Seeding writes a 0600 regular file at the canonical path; a symlinked or
   non-empty-but-wrong-mode target is repaired or refused per the rules above.
2. Seeding refuses to overwrite an existing credential without `--force`.
3. `--default` affects **only** agents created afterwards: one created before
   the flag has no credential and its failure names this command; one created
   after has the file.
4. A claude agent seeded this way starts; with explicit auth environment also
   set, the existing coexistence refusal still fires (unchanged rule).
5. Provider-unknown is refused with a code, and nothing is written.

## Non-goals

- Refreshing OAuth material.
- Seeding every agent in one command.
- Changing the API-key path, or the claude runtime's coexistence rule.
- Reading or writing any credential of the operator's own harness installs
  beyond the documented source files.

## Implementation state (2026-10-14)

Landed on `feat/agent-provider-login`:

1. This design document.
2. `identity/impl/agents/home/native_credential.py` —
   `install_native_credential()` plus its public-face exports: the shape rules
   above are enforced (0600 regular file, symlink refusal, per-harness file-name
   allowlist, no replace without `force`, empty refused, receipt reports
   "written" and never "valid"). Five tests, plus a mutation check that turns
   two of them red when the create mode becomes 0644.

Remaining, in order:

3. Daemon method `agent.provider.login` in
   `daemon/impl/application/actors/agents_admin/registry_ops.py`, alongside the
   existing `agent.secret.*` handlers. It must resolve the agent's canonical
   home for the named actor, carry the document as base64 (JSON-safe bytes),
   call `install_native_credential`, and map `NativeCredentialError.code`
   through to the CLI unchanged. Register the name in
   `daemon/impl/ipc/router.py`.
4. CLI `shell/impl/cli/commands/agent/provider.py`: resolve the documented
   source path per provider (or `--source`), refuse a non-regular, empty, or
   symlinked source, then call the IPC method. Register the sub-command in
   `shell/impl/cli/commands/agent/__init__.py` and the runtime entry.
5. `--default`: record the provider for future `agent create` runs, and have
   `create` seed from it. This is the one piece that touches the create flow;
   keep it last so the first two API surfaces can land independently.
6. Help text: state that the credential belongs to the daemon's logged-in user
   and name the revoke/re-login path (Allen's identity rule).

## Completion (2026-10-19, cli-developer)

Assessment: `docs/evidence/agent-provider-login-2026-10-19/ASSESSMENT.md`. The
remaining steps 3-6 above are completed with the following concrete shapes.

### One IPC call per seed, all files or none

`agent.provider.login` takes `files: [{filename, contentBase64}, ...]` (one entry
for claude/codex, two for pi) instead of one file per call. The daemon checks
every file (allowlist, symlink, exists-without-`--force`, empty) **before**
writing the first one, so a refusal of pi's second file can no longer leave
`auth.json` written alone. pi must send both of its files; a pi seed with one
file is refused with `INVALID_ARGUMENT`.

### Revoke: `agent provider logout`

`hyprial agent provider logout <provider> [--actor]` (IPC `agent.provider.logout`)
removes exactly the allowlisted files of that harness from the agent's native
tree, and reports which existed. It is the revoke path named in the help and in
every `login` receipt (`revoke` field). A running agent keeps whatever its
harness already loaded until it restarts; the receipt says so. Revoking the
material at the vendor (the vendor CLI's own logout) is still the only way to
invalidate an already-copied OAuth token; the help says so too.

### `--default` and create-time seeding

- Store: `<daemon state_dir>/provider-default.json` (`schemaVersion: 1`,
  `provider`), written atomically by the daemon. IPC `agent.provider.default`:
  no params reads, `provider` sets, `clear: true` clears; every answer carries
  `provider` and `previous`.
- `login --default` records the default (receipt carries `default.previous`)
  and seeds `--actor`/the addressed actor when there is one; with no actor it
  only records the default. `hyprial agent provider default [--clear]` reads or
  clears it.
- `agent create`: after the agent is created, the CLI reads the default. When
  set, it resolves that provider's host files and seeds the new agent through
  the same `agent.provider.login` call. The result carries
  `providerSeed.state` = `seeded` | `refused` (with `code`) | `not-configured`.
  Creation itself never fails because of seeding: the agent exists, and the
  seed state plus a stderr hint name both fixes
  (`hyprial agent provider login <provider> --actor <name>` or the API-key
  grant). Existing agents are never touched by `--default`.

### Batch API-key grant: `agent provider grant-key`

Production on 2026-10-18 granted one shared `user-provider` entry to 15 pi
agents by running `agent secret grant` 15 times. The convenience is:

```
hyprial agent provider grant-key --entry-id <E> --field-name <F> \
  --environment-name <N> --grant-id <G> --revision <R> --actor A --actor B ...
```

Rules, so the convenience does not weaken the three principles:

- **Owned by the logged-in user**: the source is fixed to `user-provider` (the
  daemon login user's store); `agent-private` is not offered because it is per
  agent by definition.
- **Revocable per agent**: it is a CLI loop over the existing
  `agent.secret.grant`; each actor gets its own grant record and journal entry,
  revoked with `hyprial agent secret revoke <actor> <grant-id>`. No new daemon
  authority, no new storage.
- **No echo**: only the entry id, field name and environment names cross the
  wire or appear in output; the value is never read by this command.
- **Explicit list, no `--all`**: actors are named one by one (the non-goal
  above stands). Execution stops at the first refusal; the result lists
  `granted`, `failed` (with code) and `notAttempted`, exits 1, and the granted
  ones stay granted (each is already a complete, revocable grant).

## Implementation record (2026-10-19, branch `feat/agent-provider-login-v2`)

Merged `origin/dev` (`c5e0598ea`) into the 2026-10-14 branch (merge, not
rebase; two test-registry conflicts resolved with dev semantics, see
`docs/evidence/agent-provider-login-2026-10-19/ASSESSMENT.md`). Then, each
behaviour as a red-test commit followed by its implementation:

| behaviour | red | green |
|---|---|---|
| route `agent.provider.*` via its own mixin (registry_ops back to its 1005-line baseline) | refactor, existing tests | `caceef90f` |
| one all-or-nothing seed per provider (`files[]`, whole-set preflight, identical bytes `unchanged`) | `e43c74728` | `bc0982337` |
| `agent provider logout` + `revoke` / `note` in every login receipt, owner and revoke path in help | `6ca1f61d6` | `cd052a77f` |
| machine default (`agent.provider.default`, `login --default`, `agent provider default [--clear]`) | `bf7b39f88` | `e926b11e3` |
| `agent create` seeds from the default (`providerDefault` on `agent.create`, `providerSeed` in the result, stderr hint) | `474ef516e` | `057b2366d` |
| `agent provider grant-key` (batch API-key grant) | `d09c131b2` | `de351e5d7` |
| CLI -> real `DaemonApplication` round trip (login --default, create, logout) | n/a | `1889e38b0` |

Mutation readings (each restored byte-for-byte, sha256 in the `.tree`):
`raw/mutation-{1..8}-*.tree`. Verification state and the remaining gaps are in
the evidence README.

Still open, deliberately: the design's "verification step" (isolated-HOME copy
produced only by the projection) is read for claude/codex (`runtime.py` reads
`native_root` directly) but not exercised against a real pi launch; real vendor
login files and a socket-attached daemon were not used (UNKNOWN by rule).


## Review follow-ups (2026-10-19, review of `95c1b8b45`)

### Path safety of the writer (B2, B3, S3)

`identity/impl/agents/home/native_credential.py` walks `secrets/native/<harness>`
from the agent home by descriptor (`O_DIRECTORY|O_NOFOLLOW` per level). Each
level must be a 0700 directory of this uid; a missing level is created and
`fchmod`ed to 0700, so the umask cannot produce the 0755 `secrets/native` that
the runtime's `_ensure_private_directory` rejects on first start. Every
create/rename/read/unlink goes through the leaf descriptor, so a symlinked
`secrets` or `secrets/native` (writable by the agent) can no longer send login
or logout into another agent's home. Documents are staged as `<name>.incoming`
(registered for cleanup before the first byte), the files being replaced are
moved aside to `<name>.previous`, and only then are the staged files renamed in.
Any I/O error removes what the run installed and staged and renames the
`.previous` copies back, so the directory holds the whole new set or the whole
old one; the error states which, and names any old file it could not restore
(kept as `.previous`). Only a process crash mid-run is not rolled back; stale
`.incoming`/`.previous` files (they may hold plaintext) are removed by the next
seed and by logout (review r2, C1).

The agent home itself must be a 0700 directory of this uid, opened without
following a link (review r2, C3). The provisioner creates it that way
(`validate-agent-root`); the runtime's `_ensure_private_directory` checks only
the levels below the home, so this writer is stricter than the reader there.
Logout refuses a host-invited or foreign-owned agent exactly as login does
(review r2, C4).
Refusal messages never carry document bytes.

### Who may change the default (S1)

`agent.provider.default` writes (`provider`, `clear`) require the caller the
router judges the machine operator (`_identity_binding_operator`: not a daemon
worker, no `actor`/`sessionRef` in the request, not a registered interactive
session); otherwise `CALLER_NOT_AUTHORIZED`. Reads are open. The CLI refuses
`login --default` and `default --clear` when `HYPRIAL_WORKER_ACTOR` is set. This
is L0 bookkeeping on the local socket (the same posture as `agent.grant`), not an
authentication boundary. `login`, `logout` and `grant-key` keep the posture of
`agent.secret.*` (local socket = operator).

### Whose agents may be seeded (S2)

Decision 3 makes the credential the daemon user's. `agent.provider.login`
therefore refuses (`TARGET_NOT_AUTHORIZED`) an agent whose owner is not the
daemon's owner or that was host-invited: the operator's own OAuth never goes to
a visitor's agent. `agent create` never produces such an agent.

### Create and the preferred harness (S4)

When `agent create --preferred-harness H` names a harness other than the
default, the seed is `skipped` (with the `login H --actor <name>` hint) instead
of copying another harness's login the agent would not use.

### grant-key on an existing grant id (S5)

The daemon upserts grants by id. `grant-key` therefore reads each actor's
grants first: an identical grant under the id is `alreadyGranted` (skipped, so
re-running the same command resumes after a partial failure); a different one is
refused with `PROVIDER_CREDENTIAL_EXISTS` unless `--replace` (which still needs
a higher `--revision`) and is then reported under `replaced`.

### Namespace and vocabulary of `agent provider`

- **Ownership.** This feature owns exactly `agent.provider.login`,
  `agent.provider.logout` and `agent.provider.default` (and the CLI group
  `hyprial agent provider` with `login`, `logout`, `default`, `grant-key`). The
  router routes only those three names to `_AgentProviderMixin`; every other
  `agent.provider.*` name falls through to the general agent handler, so a later
  feature can add methods in the same namespace without this mixin answering
  them with `METHOD_NOT_FOUND`.
- **Vocabulary.** Commands that act on native login files take a **harness**
  name (`claude|codex|pi`). Commands that choose a model take the **model vendor**
  of `agent create --provider` and squire (`deepseek`, `hyprial`, ...). A new verb
  must say which of the two it takes in its help, and must not accept both.
- **`feat/agent-provider-config-20261010`** (not merged; provider profiles with
  `agent.provider.{inspect,resolve,add,switch,set}` and `agent.config.import-native`)
  registers `hyprial agent provider` as a single command. If it is revived, its
  verbs become sub-commands of this group (`agent provider inspect|add|switch`),
  its IPC names stay as they are (none collide with the three above, and the
  router no longer swallows them), and its vendor-vocabulary arguments follow the
  rule above.
