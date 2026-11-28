---
name: hyprial-e2e
description: Run and assess release-grade Harness Bridge (hyprial) end-to-end scenarios across the pytest suite, the cross-implementation contract corpus, isolated daemon runners, the uv distribution flow, real resident providers, and live Feishu/Lark behavior. Use when preparing a hyprial release, reproducing an E2E failure, completing the E2E-001…E2E-013 acceptance matrix, or deciding whether E2E evidence is sufficient to pass a release gate.
---

# Run Harness Bridge E2E

Treat E2E as an evidence-producing release gate. Never infer a pass from a
command exit code when a required scenario was blocked or skipped, and never
count a gated or skipped run as E2E evidence.

## Keep implementation changes and scenarios together

Before changing CLI, daemon, provider, PAC, OrgFS, routing or protocol behavior,
run the implementation impact plan on the full branch range:

```sh
uv run --no-project --python 3.12 python scripts/ci/check_e2e_impact.py \
  --base origin/dev --head HEAD --plan-only
```

Address every reported scenario in a visible PR-body `E2E-Impact:` line as
`updated`, `unchanged` with a concrete contract judgment and original-scenario
validation record/plan, or `not-applicable` with a reason and `requires-review`.
Unknown product/protocol paths need exact-path `E2E-Unknown:` review reasons.
Keep `covers` in `contract/e2e/scenarios.json` current; shared and dedicated
runner/helper changes also affect scenarios. Never mechanically change a test
when its contract remains valid, or claim a declaration proves an E2E PASS.
Real execution evidence must identify its fixed source SHA independently.
The format, local body check and explicit first-adoption bootstrap boundary
are in [the impact guide](../../../docs/guides/e2e-impact.md).

## Preflight environment and accounts first

Do not run any scenario before this preflight is complete. Record each item as
`PASS`, `BLOCKED`, or `SKIP: <reason>` without recording identifiers or
credentials that are not needed in the report.

1. Install the checkout into an isolated environment and pin the exact
   executable under test:

   ```bash
   uv sync --extra test
   export HYPRIAL_BIN="$(pwd)/.venv/bin/hyprial"
   "$HYPRIAL_BIN" version --json
   ```

   The scenario runners resolve `hyprial` through `HYPRIAL_BIN`, then
   `.venv/bin/hyprial`, then `PATH`. Export `HYPRIAL_BIN` explicitly whenever more
   than one build is installed so the tested SHA and the executed binary
   cannot diverge.

2. Establish an isolated home for every manual hyprial command:

   ```bash
   export HARNESS_E2E_HOME="${HARNESS_E2E_HOME:?set a dedicated E2E home outside ~/.hyprial}"
   export HYPRIAL_HOME="$HARNESS_E2E_HOME"
   export HARNESS_STATE_DIR="$HARNESS_E2E_HOME/state"
   test "$HYPRIAL_HOME" != "$HOME/.hyprial"
   ```

   Abort if the last check fails. The runners allocate their own disposable
   `HYPRIAL_HOME`/`HARNESS_STATE_DIR` roots; the isolated home above is for
   commands run outside a runner, including diagnosis and cleanup. Do not
   copy production `~/.hyprial` into the test home, and never point any scenario
   at the production daemon socket.

   One network profile is one `HYPRIAL_HOME`: an E2E isolated home *is* a
   profile, and with no `profile.json` in it, it is the built-in `hyprial`
   profile — nothing extra to set up (see `hyprial profile list|use|create`).

3. For the live Feishu scenario (E2E-004), verify the user identity against
   the server:

   ```bash
   LARKSUITE_CLI_NO_UPDATE_NOTIFIER=1 LARKSUITE_CLI_NO_SKILLS_NOTIFIER=1 \
     lark-cli auth status --json --verify
   ```

   Require `.identities.user.verified == true`. If user authentication is
   absent or expired, use the split flow; request only the domains or scopes
   the live test needs:

   ```bash
   lark-cli auth login --domain im --domain contact --no-wait --json
   lark-cli auth qrcode '<verification_url>' --output lark-e2e-auth.png
   # In a later turn, after the user confirms authorization:
   lark-cli auth login --device-code '<device_code>'
   ```

   Relay the emitted URL unchanged with its QR code, then return control to
   the user. The device code and link expire after 600 seconds; regenerate
   them instead of reusing expired values. The agent, not the user, completes
   the later `--device-code` command. Do not block on it in the same turn
   that must deliver the URL.

   **`lark-cli` only impersonates the user.** In E2E-004 its sole job is to
   send/read messages as the real user (`--as user`). Which App the lark-cli
   auth profile is bound to is irrelevant to the App under test — the user
   token is just an OAuth client credential for the CLI; the message is sent
   by the user account, and nothing flows through the profile App's bot,
   adapter, or daemon. A lark-cli profile bound to a production App is NOT a
   blocker and must not be reported as one. (A `needs_refresh` token that
   recovers on the next API call is also not a blocker; an actually expired
   token is — record the exact error text and stop.)

4. Confirm the dedicated E2E App, bot, user account, and test chat named in
   the preconditions of `contract/e2e/04-feishu-roundtrip.md` exist
   and are approved. The dedicated test App is `alicesmith-hyprial-e2e`
   (`cli_<e2e-test-app>`; the id is in `contract/e2e/feishu/settings.py`). **The shared test target is the App's multi-
   member group chat selected by the gateway's `py-test` route**: any
   developer can join it, so E2E runs are
   reproducible by the whole team. A p2p DM with the bot is a per-developer
   private conversation others cannot access — **DM chat ids must never be
   committed to the repo** (docs, skills, runners, evidence); a developer
   running against their own DM pins it locally via `HYPRIAL_E2E_LARK_CHAT_ID`.
   Runners must assert `chat_type` against the resolved chat's actual mode
   and fail loudly when the configured/resolved chat is inaccessible —
   group and DM semantics (pins, @mentions, routing, event delivery) differ
   and must not be substituted for each other. In group mode, user messages
   must @mention the bot or the platform does not deliver them.
   **The three production Apps — `hq-adjutant-feishu`, `hyprial-kanban`,
   `mac-studio-feishu` — are a red line: never point any test, adapter, or
   daemon at them under any circumstances.** A missing account, credential,
   scope, or chat is a blocked scenario, not a skipped green one.

   Media refs are directed pulls. A direct label carries
   `ref:<message-id>/<key>`; a merge-forward child carries
   `ref:<top-level-shell-id>/<child-id>/<key>`. Fetch only in the approved
   isolated E2E environment with
   `hyprial adapter media get <test-adapter> <ref>`. The shell-aware form must
   call the resource endpoint with the top-level shell ID. Never substitute a
   production adapter to make a blocked media check pass.

5. Confirm per-scenario tooling before each run: Docker for the full E2E-001
   container mesh (`--local` is a two-phase loopback diagnostic, not a gate
   substitute),
   `tmux` plus a logged-in Claude Code CLI for E2E-008, and authenticated
   `pi`, `codex`, and `claude` executables with the models listed in
   `contract/e2e/06-resident-providers.md` for E2E-006. **When
   running E2E-006 from a git worktree, always export `HYPRIAL_RESIDENT_SKILL`
   pointing at the real skill directory** (for example
   `HYPRIAL_RESIDENT_SKILL=/path/to/workspace/kanban/.claude/skills/kanban`):
   the runner's default locates the skill via `ROOT.parents[2]`, which
   silently resolves to the wrong place under a worktree and surfaces as a
   misleading `BLOCKED preflight.pi-skill`. The scenario documents own the
   authoritative preconditions; the runners re-verify them and classify
   missing prerequisites as `BLOCKED`. For an attended Codex TUI that must
   receive Harness Network messages, run foreground `hyprial agent start codex`
   inside a tmux window you own.

Start testing only when the tooling, isolated home, and — for `[REAL ENV]`
rows — account checks all pass.

## Enforce isolation and secret discipline

For storage restart incidents, run
`HYPRIAL_BIN="$PWD/.venv/bin/hyprial" uv run python scripts/verify/check_storage_restart.py`
only with disposable scenario state. `daemon-health.json` is epoch-labelled
evidence with a timestamp, not an authorization. Dispatch any destructive
maintenance through a non-daemon carrier with the approved `--expected-epoch`;
never replay an incident's hardcoded production script. See
`contract/daemon-lifecycle/storage-restart.md` for probe and evidence limits.

Process custody backups/recovery must include both `orphan-processes.json`
and its `.jsonl` delta journal. Never prune or interpret the snapshot alone;
see `docs/reference/orphan-custody-storage.md`. Incident tests use isolated
homes and must distinguish crash/restart proof from long-running growth/CPU
acceptance.

- Keep `HOME`, all `XDG_*` directories, `HYPRIAL_HOME`, and `HARNESS_STATE_DIR`
  inside the disposable test root. Remove inherited `DBUS_*` addresses. Pytest
  subprocess tests use `tests.support.runtime.environment.isolated_environment`.
  Use `init --no-service`; changing HOME does not isolate the launchd UID
  domain or systemd user manager, but non-default homes now get derived unit
  names (the default home keeps the fixed label) and real supervisor commands
  still reject a HOME that differs from the OS account home; doctor reports
  that refusal separately from the daemon/zenoh checks.
  Never call launchctl/systemctl to clean up a test: stop its isolated daemon and
  remove only its isolated unit files. The pytest session tripwire lists real
  user unit names and mtimes only; any change fails the session. Never inspect,
  mutate, stop, or reuse processes, sockets, or state from `~/.hyprial`.
- Every test-owned home, state directory, socket, database, correlation
  store, and Zenoh endpoint is disposable. Use unique actor, adapter, and
  marker names so evidence cannot be confused with production traffic.
- E2E-001 Docker resources use the fixed `hyprial-e2e001-*` prefix and the runner
  performs pre-clean, cleanup on every exit path, and a post-run leak
  self-check. After a crash, remove leftovers with
  `docker rm -f $(docker ps -aq --filter name=hyprial-e2e001)` before rerunning.
- Live Feishu runs create visible messages, reactions, and replies in the
  dedicated test chat. Warn participants before an attended run. Evidence
  markers are the runner's unique non-secret `CONTRACT_LARK_*_<nonce>` texts.
- Never print a secret, enable shell tracing, dump the environment, or place
  an App secret or token in argv, shell history, logs, evidence, `.env`
  files, or repository files. The Feishu runner consumes only the approved
  read-only local credential files; if a flow would require printing or
  persisting plaintext, stop and report the block.
- Leave no test process behind. After each scenario, verify the runner's
  daemons, adapters, providers, tmux sessions, and containers are gone.
  Report leaked test state instead of adopting or reusing it.

## Login invitation confirmation (O2c)

Login queries pending invitations after daemon verification, including first-time
initialization. TTY users confirm each invite; non-TTY and `--json` runs report
without joining. Bulk acceptance requires `hyprial org pending --accept-all`.
Declining leaves the account invite unchanged. Pickup failures do not undo a
committed login. Never print payloads or login credentials in evidence.
A partial join stays pending with `status=partial` and `missingStep`; an org
listing is not completion proof. Retry the same confirmed invite to resume its
durable checkpoints. Do not delete `org-invites/` state to force re-redemption.
Cover directory/ACL failure, binding/policy read-back, device publication,
concurrent IPC accepts, and crashes between checkpoints in `test_recovery.py`.
Also run `test_access.py`: real OrgFS invitation grants are directory `rw` and
ACL `ro`. Read-back and resume must accept those grants and higher access,
without promoting ACL access or accepting a missing/revoked member or a
read-only directory grant. Transport and device stand-ins are not live E2E.

Run `uv run pytest tests/daemon/org_membership/invites tests/shell/cli/org_membership`
plus `tests/shell/invites`, `tests/shell/login/test_login_cli.py`, and
`tests/shell/cli/installation/test_cli_onboarding.py` with disposable homes and
`NO_PROXY=127.0.0.1,localhost,::1`. These prove local composition and contracts;
real Casdoor/Tailcat and cross-device acceptance remain separate gates.

## Org membership authority (O1a)

For membership-storage changes, run
`uv run pytest tests/daemon/org_membership tests/identity/org_membership`
and the touched squire/startup tests with disposable homes.
`tests/daemon/org_membership/rework` covers real protected OrgFS trees,
durable replica reload, IPC dispatcher wire keys, unavailable projections,
and fake-Casdoor concurrent binding. These tests do not prove live Casdoor
or Lark operation.

Personal ensure must probe `group-space`. Without that capability it reports
pending and creates no competing space. With it, bind the reserved UUID before
creating the local space. On conflict retain both IDs and never overwrite the
local space. A reserved UUID must survive a restart.

There is no receivers routing record any more (S3a): `org.receivers.*` and
`receivers.json` are deleted, and `state/users.json` is not read at all (S3c;
runtime capabilities live in `state/runtime-capabilities.json`). Owner-bound
delivery goes through the user agent cell; do not repair routing by editing
`state/users.json`. Administrative membership approval remains BLOCKED until the
Casdoor role capability is separately integrated and tested.

### Org-membership upgrade (O1b)

`hyprial doctor --fix` and daemon upgrade request `upgrade.steps.run`; only the
live daemon executes `org-membership-v1`. A missing daemon is pending, never a
local migration fallback. Read `upgrade.steps.status` for per-step results.
Each run writes a private per-entity report under
`<target HYPRIAL_HOME>/state/org-membership-v1/`; the step's `data.reportPath`
identifies it. Keep the journal and source files: do not delete checkpoints to
force success after a source or membership conflict. Reconcile drift explicitly.

Run `tests/daemon/org_membership/migration` plus upgrade/doctor tests in isolated
homes. Inject interruption after every checkpoint and durable write, reopen the
same replica, and preserve removed memberships. Use independent FileSpaceStore
replicas in both upgrade/merge orders, not only a shared facade. Doctor shows
`conflict` as a warning with an action hint; it is not completion. Other
pending/failure conditions must still warn. The readback must use OrgFS, not a
profile fallback. Context targets come
from explicit containing-space org metadata; residents match unique name+node,
not publishers or human members. Foreign owner writes remain pending until the
authorized record syncs. Fake Casdoor and in-process runtime composition do not
prove live multi-device or installer-upgrade acceptance; report those UNKNOWN.

## Current organization defaults (O2a, 0.5.5.3)

Run `tests/daemon/org_membership/context`, `tests/shell/cli/work`, and
`tests/shell/cli/org_membership/test_defaults.py`.
Use isolated homes. Set `org use group-b`; work defaults must target B and
explicit `--org group-a` must target A. After `org use --clear`, two active
orgs must return `ORG_REQUIRED` with candidates. Left/deleted/partial current
orgs clear durably on the next context/work command and emit a stderr notice,
including with `--json` and when fallback fails. Personal orgs may host work.
The two work shortcuts resolve an org and call ordinary `orgfs.ls/read`; there
are no legacy `work.*` requests or work-specific owner/session checks. Verify
home isolation, unchanged OrgFS metadata, and the normal OrgFS permissions.
These tests do not establish O2b member defaults or live release acceptance.

## Work documents after #2214 simplification

Use `contract/work-items/README.md` as the current behavior contract. In an
isolated space, verify `org work ls` reads `work/` and `org work show <filename>`
returns the same raw text/version as `org fs read` without adding `.md`, parsing
tasks, or requiring journal replay or a local approval ledger. Include arbitrary
filenames, malformed YAML, unknown frontmatter fields, and old `ownerActions`;
the source text must remain readable and unchanged. No `work.*` IPC methods,
specialized work writes, collect/finish commands, or asked/declined state machine
should remain registered.

Exercise a normal `org fs write --expect-version` edit and stale-version refusal,
then re-read and reconcile. Same-name copies must be selectable by node ID and
handled with ordinary resolve/move/trash operations. Check trash recovery
without a special last-valid-task rule: `org fs trash restore SPACE NODE` takes
no version, restores the node's last state before deletion, and brings back the
descendants removed in the same batch; overwritten text cannot be recovered.
OrgFS has no node history (`org fs history` was removed). Parser and
overview diagnostics are read-only projections; task status, owner fields and
legacy verification markers must not become approval evidence. Keep PAC's own
execution/approval/history tests separate. Verify that this simplification does
not migrate or delete existing text, history, unknown CRDT roots, or ledger files;
it does not execute the historical Yjs state-only cutover. A local fixture does
not establish live multi-device convergence or signed cross-node approval.

## Lark quarantine recovery (0.5.5.1)

For daemon-side adapter lifecycle changes, run
`uv run pytest tests/daemon/adapters/lark/lifecycle/quarantine tests/shell/cli/adapters`.
The fake-worker/fake-clock tests cover budget exhaustion, bounded slow retries,
stop cancellation, simultaneous recovery, ONLINE reset and status rendering.
They do not prove live Lark connectivity; E2E-004 remains a separate live gate.

Desired quarantined adapters now retry automatically (about 30 seconds initially,
doubling up to 600 seconds, with jitter). `adapter status` exposes `nextRetryAt`
in daemon-monotonic seconds, not Unix time, and the one-based `retryAttempt`.
No manual start is required after a transient outage. Explicit start bypasses
the delay; successful stop cancels retries. Check `lark.adapter.quarantined`
and `lark.adapter.recovered` in the existing daemon log. Do not restart production
or modify desired-state files to validate this behavior. Contract:
`contract/daemon-lifecycle/adapter-model.md`.

## User-key local verification (U2 prerequisite only)

Run `uv run pytest tests/identity/provider_keys/user_key tests/identity/user_key
 tests/daemon/user_key tests/daemon/identity/proofs
 tests/integration/native_harnesses/test_native_resident_tripwires.py` as one command
(that file merges the `resident_metadata/native_origins_*.py` parts into `NATIVE_ORIGINS`).
These tests use synthetic issuers and isolated state. They prove local JWT/JWKS,
lookup/cache/freshness, custody, unavailable lifecycle and reserved-partition
behavior, not real Casdoor, provider access or cross-device acceptance.

`hyprial user key renew|rotate|revoke` remains unavailable (`USER_KEY_UNBOUND`);
workers cannot invoke these operator commands. Do not try renewal as a login
repair or inject an issuer/activation setting to bypass the refusal. U2 adds no
production issuance, revocation, publication, re-signing or signer activation.
Use `contract/identity/user-key.md` for exact local verification limits, including
in-process-only sequence memory. UK-CAS and actual cross-device behavior stay
BLOCKED/UNKNOWN until their approved prerequisites exist. Never use production
login credentials or an existing organization to make this local slice pass.

## Provider-key integration and refusal checks

Run `uv run pytest tests/daemon/provider_keys tests/identity/provider_keys
 tests/integration/provider_keys tests/shell/cli/users/provider
 tests/daemon/ipc/contracts/setup/test_internal_secret_fence.py` as one command.
Keep the default provider authority `legacy`, HTTP exposure false and production
signer disabled. A revoked grant refuses the whole owner-key launch. A corrupt
legacy inventory returns `AGENT_ERROR`, never an empty successful list. Recheck
trusted internal and public incarnation fences through grant and inventory reads,
including foreign agents and an empty inventory after same-name replacement.
Explicit allow/grant repair must verify the exact CAS snapshot and use a sequence
above durable high-water; renew/revoke never repair implicitly. Test competing
store/service instances, unavailable anchors, and a concurrently restored active
slot with `replace=False`. Default daemon state-directory exclusivity limits the
multi-instance trigger; local probes do not prove a default production exploit.

The provider gate does not gate login's local UserKey initialization: login may
create an unbound key in its selected isolated home/state after real device binding.
Do not add a fabricated device identity, issuer, owner-home resolver or global gate
just to satisfy these tests. Production issuance and cross-node signature/freshness
acceptance remain unimplemented prerequisites, not established E2E results.

## Lark outage backfill

For inbound history/reconnect changes, run
`uv run pytest tests/daemon/adapters/lark/inbound/recovery/reconciliation/test_outage_backfill.py`
and the Lark adapter suite. The fake-clock/history tests cover a 59-minute cold
start gap, reconnect admission fencing, concurrent live delivery, native-ID
idempotency, incomplete-history holds, timestamp ties and the 24-hour cap.
Inspect `lark.backfill.sweep` and `lark.backfill.truncated` in isolated logs; the
latter reports an intentionally uncovered older interval, not full recovery.
Missing history permission preserves degraded live behavior and cannot establish
history ordering. These tests do not prove real Lark history completeness or
live E2E acceptance. Do not access production state to create a test checkpoint.
Contract: `contract/daemon-lifecycle/adapter-model.md`.

Also run `test_pending_progress.py` in the same reconciliation directory for
17/41-row frozen backlogs, native IDs unrelated to timestamps, absent history,
equal-time parents and poisoned-request restart recovery. Three failed recovery
attempts stop automatic replay of that request; inspect
`lark.backfill.dead-lettered` (`acceptance=unconfirmed`). The exact frozen request
remains in custody and consumes capacity, but no longer gates the chat. Explicit
message recovery can re-arm it; do not treat this event as proof of non-delivery
or ask a sender to submit a duplicate. `lark.backfill.deferred` names submission
failure or a bounded replay budget; zero progress is not a healthy recovery.

## Post-deploy daemon saturation check

Run this read-only scenario after a daemon deploy and before declaring the
daemon healthy. Use the intended deployment profile; do not restart it or
substitute an isolated mock. Capture sanitized outputs and five timing rows.

1. Run `hyprial daemon status --json`. Require
   `daemon.ipcClients.active < daemon.ipcClients.limit` and
   `daemon.ipcClients.saturated == false`. `active == limit` is saturation and
   is `FAIL`, even if a later request happens to succeed.
2. The retired full `hyprial ps` projection exposed
   `storageHealth.threadCount` and `storageHealth.threadCountScope`; the current
   grouped CLI does not expose those fields. Record this predicate as `BLOCKED:
   storageHealth is not exposed by the public grouped CLI` instead of reaching
   through private IPC or claiming a thread-trend PASS. A future public grouped
   command must restore the three samples (start, after the latency loop, and
   after 60 seconds) before this predicate can pass.
3. Run the following five times and retain each `/usr/bin/time` `real` value:

   ```bash
   /usr/bin/time -p hyprial org list --json >/tmp/hyprial-org-list-check.json
   ```

   Every call must exit 0, emit `ok:true`, and have `real < 2.0` seconds. Use a
   dedicated sanitized output path if `/tmp/hyprial-org-list-check.json` is not
   appropriate, and delete only that exact file after evidence extraction.
4. Run `hyprial daemon status --json` once more. The IPC slot predicate must
   still pass; compare `daemon.ipcStats.runtime` samples for a single hot
   actor/effect owner. Do not claim the unavailable thread-count predicate.

When fresh host-presence evidence is required, run
`hyprial host list --refresh --json`; it queues the asynchronous refresh and
returns `refresh.operationId` plus observational host rows. The current public
CLI does not expose operation-id settlement lookup. Do not use the retired top-level `ps` refresh-operation spelling or poll an
unqualified latest operation; record exact settlement as `BLOCKED: refresh operation lookup is not
exposed by the public grouped CLI`. Host presence is not proof that a peer or
application is reachable. Use `hyprial agent list --json` for actor status and
`hyprial daemon status --json` for local daemon status.

Report the three predicates separately: IPC slots, thread-count trend, and all
five Org-list latencies. A missing field, command timeout, inaccessible daemon,
or absent deployment baseline is `BLOCKED` with the exact error. It is never a
skip or inferred PASS.

## Tailcat login and device provisioning

On this tree, `hyprial login [--no-open] [--json]` handles OIDC identity and
device-key provisioning,
not a Headscale/Tailscale control-plane join. Inspect the current CLI help;
do not use retired `--switch-account`, `--dry-run`, `--no-daemon`,
`--skip-join`, `--preauthkey-file`, `--install-sidecar`,
`--only-install-sidecar` or `--join-timeout` instructions from old evidence.

Each `HYPRIAL_HOME` binds permanently to one `(identityIssuer, userId)` pair
stored in `settings.homeAccount` (`{issuer, userId, owner}`). First-time identity
commit writes `settings.owner`, `settings.username`, `settings.userId`,
`settings.identityIssuer`, `settings.homeAccount`, and the refresh token in the
isolated home's `secrets/login.json` at 0600. A same-uid re-login with a renamed
`preferred_username` updates `settings.username` and `homeAccount` while
preserving `settings.owner`. A home that already has an
owner starts the serving daemon if needed and runs one daemon-driven same-uid
reauthentication (`auth.relogin({operationKey, until: "serving"})`) until
serving, verifying `issuer`, `userId`, and bound `owner` while driving
device/user-key/initialize recovery. Logging into another uid (or an unbound
home with legacy business data) is refused before any write with
`IDENTITY_CONFLICT`; switch accounts by creating/selecting a separate
`HYPRIAL_HOME` (or following `docs/guides/migration-account-binding.md`).
Device provisioning invokes the wheel's `hyprial-tailcat` genkey seam and
writes the public device record. Identity success is not proof of device
readiness: a missing/failing sidecar may leave login successful with
`device.ready=false` and a next step. Require the device outcome separately;
organization membership/traffic is tested by `org` scenarios, not inferred from
login or a fake issuer.

A first login on a home with no account is one daemon operation: `login` starts
the bootstrap daemon when none runs, shows the URL and code, then makes one
`auth.login.begin` (`until: serving`) and one `auth.operation.get` with
`waitSeconds`; the daemon authorises, registers the device and serves in the
same process, and a device that cannot be registered is exit 0 with
`device.ready=false`. A real run needs a controlled issuer: `hyprial login` on an
ownerless home reaches the configured issuer, so use the controlled daemon entry
(`tests/integration/daemon_lifecycle/child_entry.py`), never a production one.

`hyprial logout [--yes] [--json]` is scoped to the selected home. It is one
daemon `auth.logout` (the command starts the daemon when none runs): the daemon
removes only local session state, `secrets/login.json` and login-owned Casdoor
settings fields (preserving `settings.homeAccount`), and a serving daemon then
returns to bootstrap in the same PID.
The command takes no lock, stops no daemon and does not wait for an exit; its
JSON reports `daemon.stopRequested`. It preserves agent credentials, Tailcat
device keys, and unrelated settings. It does not revoke already-issued access tokens at the issuer; those
tokens can remain valid until expiry. It requires confirmation before the
request (`--yes` for noninteractive/JSON use) and is an idempotent
`not-logged-in` success when state is absent (a missing home starts nothing).
With `HYPRIAL_OWNER` set and no login left, a repeated logout answers
`DAEMON_NOT_READY` (known-problems KP-120). Test these behaviors only with
synthetic sentinels in a disposable home. Do not apply any login or logout flow
to a production home during testing.

Sidecars ship in platform wheels; separate tsnet release acquisition is retired.
The `hyprial socialware install` command reports COMING_SOON (the old top-level `hyprial install` is retired: unknown command). Builtin GUI
lifecycle uses the wheel's `_gui` bundle, not catalog/application installation.

Evidence rules for any scenario that touches login:

- Automated verification uses the in-repo fake OIDC
  (`tests/fixtures/network/fake_oidc.py`), whose response shapes are pinned to
  Casdoor v3.144.0 source. It is a unit-test fixture, not an environment:
  passing against it is never "verified against auth.hyprial.com".
- Real-issuer device flow is `[REAL ENV]` and gated behind U4 (app-hyprial
  runbook executed by its owner) plus a dedicated test account; before
  that, classify it `BLOCKED`, never run it, and never send write traffic
  to the production issuer from a scenario runner.
- Secret discipline extends to the flow's own values: the device code and
  access/refresh tokens must not appear in any report or log; the
  verification URI and user code are public by design. `--json` stdout
  carries exactly the final result object; progress events go to stderr.
- A daemon must come up offline from `HYPRIAL_OWNER`/`settings.owner` alone —
  any scenario that needs the credential file to *start* a daemon is a
  product regression, not an environment gap.  Its cell runtime (and with
  it every headless delivery) additionally needs the uid: pair
  `HYPRIAL_OWNER` with `HYPRIAL_USER_ID` (a Casdoor-shaped `sub`, so that
  `u.<value>` is a user-id segment), or seed `settings.userId`.
  `contract/e2e/_daemon_runner.py` derives one from the owner when the
  runner sets none; a scenario that sets only `HYPRIAL_OWNER` gets
  `cell.runtime_unavailable reason=authority-uid-missing` and pending
  headless rows, which is the product behaviour, not an environment gap.

The tsnet-era join harnesses retired with the sidecar (Tailcat cutover):
the `tag:hyprial-ci` pre-auth key CI job, the self-hosted Headscale login
path, and E2E-022 (public install + Headscale join) are gone. Org
networking (`org create/invite/join`) is the replacement surface; drive it
from scenario runners with disposable homes and never against production
orgs.
Retiring these runners is not evidence of passing published Tailcat artifacts or
Windows native acceptance. Those remain UNKNOWN until independently executed;
real issuer/browser approval still needs dedicated accounts and attended authority.

## Tailcat HTTP proxy acceptance (0.5.5.3)

For proxy-only hosts, provide `HTTPS_PROXY`/`https_proxy` or `HTTP_PROXY`/`http_proxy`
in the daemon launch environment. The sidecar receives a frozen snapshot, including
`NO_PROXY`/`no_proxy`; uppercase wins and HTTPS falls back to HTTP. `ALL_PROXY` and
ambient `TS_*` are not supported. Do not print proxy URLs or credentials.

Run `go test ./internal/proxytest ./internal/outbound` and
`go test ./internal/membershiptest -run 'Test(Membership.*Proxy|ReviewProxyFallback)$' -count=1 -timeout=4m` from
`sidecar/hyprial-tailcat`. These use local CONNECT proxies, disabled direct DERP IP
families, no STUN service, isolated child HOME, and synthetic credentials. They
prove hostname CONNECT, TLS/DERP, map retrieval, invite redemption and safe 407
failure diagnostics. The pinned region client has no custom-dialer seam; direct
DERP is disabled by the map, while the HTTPS map test uses a rejecting dialer.
For OS-enforced UDP denial on Darwin, use the `sandbox-exec` recipe in
`docs/evidence/tailcat-http-proxy-2026-10-31.md`; it passed full redemption and
roster retrieval three times with outbound UDP and non-loopback TCP denied.
Do not inject `TS_DEBUG_ALWAYS_USE_DERP` into these subprocesses: the pinned
upstream's debug-only rebind replaces blocking dummy UDP sockets without closing
the old readers and can hang client shutdown. The production launcher strips it.

A passed local test is not cloud acceptance. The proxy-only cloud join was measured
on the Claude Code cloud PC on 2026-11-03
(`docs/evidence/tailcat-http-proxy-nofork-cloud-2026-11-03.md`): the invite redemption
passed over the proxy, and the OrgFS leg still failed separately. Repeat that measurement
for a new sidecar build; report BLOCKED where no proxy-only host is available. Error `data.proxy=configured`
means configuration exists, not that a particular connection used it; `stage=sidecar`
and `cause=response-timeout` do not prove a network cause. Safe DERP classifications
refer only to observations during the current control dial, including optional
netcheck activity. They never cancel transport. A failed node must not prevent
fallback to another node or region. Test mixed-node success, blocked-first-region
success, all-node rejection, and a hanging node under the same UDP-denying sandbox.
Initial regional handshakes divide the remaining thirty-second startup/caller
budget among remaining regions, reserving up to ten seconds (one third of initial
time) for healthy handshake/control work. The last region retains the remaining
context. Upstream's ten-second Ping cap still applies. On the unpatched checked-in
`tailscale.com` there is no typed exhaustion notification: rejected or hanging
candidates consume their whole share (about fifteen seconds for three rejections).
Proxied nodes of a multi-node region become single-node candidates (2026-11-03);
direct nodes (no effective proxy, or matched by `NO_PROXY`) keep the original grouped
candidate and upstream ten-second regional Ping. A hanging first proxied node only consumes its own share and
`TestReviewProxyFallback/mixed-node-hung` must pass (about 26 s). Treat a failure
there as a regression. Do not adapt the budget assertions to make the suite green;
report the per-case counts honestly. The 1500 ms per-node proxy bound and the
typed notification exist only in the unactivated candidate patch; follow
`sidecar/hyprial-tailcat/patches/README.md` to reproduce the candidate on a
disposable patched dependency. This is not
a target-TLS or complete membership deadline. Repeat all six `TestReviewProxyFallback`
cases and the separate
`TestReviewProxyBudget` 20 times under the same sandbox. Also repeat
`TestReviewProxyRegionHint`: three sequential engine exchanges with three rejected
regions must increase rejection counters only in the first exchange. The hint is
engine-local, authority/map-scoped, ten-minute TTL, at most 128 entries, and never
persisted. Do not retain zero-lease transports to make this test pass. These cases
include hanging-first/healthy-second and healthy-fourth-region. The budget test uses real
membership exchanges with an instrumented Dial seam, asserting five seconds
remain in each thirty-second exchange when the response completes. Redemption
performs two exchanges (redeem, then roster fetch); its summed duration is not a
single thirty-second budget. A complete repeat can exceed three minutes; allow
100 minutes for 20. The real CLI/daemon runner is
`scripts/verify/tailcat_proxy_join.py`; run it with the Python operation-budget
change for integrated acceptance. Its original-chain `MEMBERSHIP_SIDECAR_TIMEOUT`
after about ten seconds is a red reproduction, not a successful join.
A control response is complete only after newline. Test partial EOF/deadline
responses as `AUTHORITY_UNREACHABLE`, not `BAD_REQUEST`; complete malformed frames
remain `BAD_REQUEST`. Optional `data.bytesReceived` is only a count, not payload:
integer `0..1048577`, with `stage=control` and cause `deadline-exceeded` or
`transport-failed`. Integrate the Python diagnostic allowlist before claiming
cross-language acceptance. Preserve failed repeat logs and trace lengths/context
before changing a new failure; do not let the hint hide the original parser repro.
`data.attempts` lists
safe node outcomes; `source=diagnostic-probe` marks terminal-only TLS probes, not
proof of the original transport's cause. Probes never run on successful controls.

## Tailcat cycle-break acceptance (0.5.5.1)

Use matched daemon/sidecar stdio v7 and control v1. A v6 fixture or old invitation
is a negative test, not working mixed-version networking. Upgrade the pinned
fixed authority first; no election or automatic re-pin is permitted.

Use disposable short `/tmp/hr-*` homes and local labrelay only. Observe sidecar,
control, member, Zenoh and OrgFS readiness separately. Start control with no
Zenoh socket; then bind the private Unix backend. Never substitute TCP inbound.
Candidates must come from the sidecar leased roster, with greater
`(masterPublic,deviceId)` initiating. Directory writes, join-time and liveliness
must not select candidates or admission keys.

Required real cases: two-node one-time migration; three-node staged upgrade;
InviteV2 redeem before any OrgFS holder; message/OrgFS round-trip; confirmed
OrgFS-first revoke, connection closure and 300-second lease expiry; authority
outage/recovery; per-server DERP identity regression. Migration receipt plus
missing store must refuse reimport. Lost revoke replies must replay fixed device
IDs, expected revision and operation ID without repeating committed upper work.
Casdoor discovery remains pending until confirmation and authority execution.
Removal confirmations must send the preview's `operationId` and exact `deviceIds`
through CLI/IPC. Test interactive and `--yes` interleavings: another operator
completes removal, then the owner enrolls a new device. The old confirmation must
return only its original receipt or `REMOVAL_CONFIRMATION_STALE`, never revoke the
new device. Inject an ACL write failure after directory removal: retry must
finish ACL removal before recording the upper receipt or sending tailnet revoke.
Cover operator and Casdoor-origin journals with real local OrgFS state.

The default `forwarding.tailnet.trustMembersForUpperLayers=true` bypasses only
supplier/requester directory-member gates on the private sidecar path. Keep
format, author, path, commit/hash and CRDT checks. Record Zenoh ACL as
`not-implemented-0.5.5.1`; shared-session inter-org isolation is incomplete.
E2E-025 uses private Unix backend/slots and real invitation redemption. Test both
lexical dialing directions: a new roster must include the authority's derived
key for the new server before returning redemption, without a refresh delay.

Run `uv run python contract/e2e/run-tailcat-cycle-upgrade.py --nodes 2` and then
`--nodes 3 --lease`, with explicit `HYPRIAL_BIN`, `HYPRIAL_TAILCAT_BINARY` and
`HYPRIAL_TAILCAT_LABRELAY`. This seeds synthetic 0.5.5-shaped local directory
copies without derived maps, then uses real daemons/sidecars. Since 0.5.5.1.1 the
fixture pins the authority on member nodes offline (`org pin-authority`) before they upgrade,
because authority is a local fact and a member never elects a remote owner row. It checks one-time
master-only migration, staged startup, directory views, request/reply, OrgFS,
restart, authority outage/recovery and confirmed removal with a 0600 backup.
It does not prove an installer upgrade or mixed-version interoperability.

`uv run python contract/e2e/run-tailcat-membership-hardening.py` (same binaries) replays the
2026-10-28 production shape on real sidecars: a multi-device owner (two real alice devices plus
a renamed-host stale row and a corrupted-`updatedAt` duplicate), a deleted org and a zero-row
probe org. It checks the fast refusal (`daemon.start.failed` with `code`/`attempt`, no receipt,
`state/daemon-start-failures.json` written), offline `org migration-status` and
`org pin-authority` on both nodes, then the authority migration (stale rows dropped, deleted/probe
skipped with `membership.migration.skipped`, counter cleared), the member pin-only import and a
message round-trip. BLOCKED without the two binaries; never a skip.

`go test ./internal/membershiptest -run TestRealAuthorityOutageLeaseExpiry -count=1 -v -timeout=7m`
(from `sidecar/hyprial-tailcat`) takes five real minutes: traffic between two
members survives loss of the authority, then an existing stream closes at lease
expiry. Control remains ready; member restart cannot revive a persisted lease.
No shortened clock or skipped test counts as this acceptance.

Offline re-pin has a separate real-sidecar local smoke:
`uv run python tests/daemon/forwarding/membership/repin_smoke.py --binary <v7-binary> --relay <labrelay>`.
It creates disposable homes and checks live-daemon refusal, explicit confirmation,
0600 backup, pin update and member retention. It does not prove cross-machine
full-table/receipt transfer. Normal sidecar `up` still opens control networking
and starts refresh/announce; all other writers must be stopped for maintenance.
A local daemon fence is not a global write lock. A revision conflict requires
new preview/confirmation and a new operation ID, not silent parameter changes.

### V7 admission and redemption-recovery transport scenarios

Run `uv run python contract/e2e/run-tailcat-org-admission-deny.py` (E2E-029)
and `uv run python contract/e2e/run-tailcat-org-join-resume.py` (E2E-030).
They compile the current Go engine/membership code and use real Tailcat streams,
an in-process loopback labrelay and disposable `/tmp/hr-*` keys/stores. No external
sidecar binary or account is used. Missing Go is BLOCKED; zero tests or skips fail.
E2E-029 checks wrong-token and forged-peer refusals, a raw unjoined client's
inbound business denial, positive echo traffic, revocation and non-resurrection.
E2E-030 waits 65 seconds before issuing the invite, discards a committed redemption
reply, reopens both stores, replays the same operation and checks single consume,
fresh lease, bidirectional echo and another-device refusal.
These are **transport equivalents**, not full daemon/CLI/Casdoor/OrgFS acceptance.
The backend is echo, not Zenoh; E2E-025 remains the full-daemon positive control.
Do not restore the old Python join host, temporary allow grants or directory-row
continuation window to satisfy the retired v4 scenario wording.

## Cell run-state projection (0.5.5.3 slice C)

For owner/run-state/status changes, run
`NO_PROXY=127.0.0.1,localhost,::1 uv run pytest tests/daemon/cells/observability`
and the isolated contract runner. Include touched turn/session/process/status
and shell-diagnostics tests. The publication tests include a disposable real
PID/birth exit and unresolved native start/stop; require those source joins,
not only reducer vectors. Status reads must not add process probes or actions.
`contract/daemon-lifecycle/run-state.md` specifies
`runState`, unknown/partial coverage, and optional `cell-observation.json`.
Warnings use 30-minute run age, existing 15-minute Codex producer quiet, and
5-minute explicit permission wait defaults. They never kill or approve a turn.
Use only disposable homes when changing thresholds; never tune production for a
test. Explicit permission telemetry remains E3, not proven by projection tests
or fake-provider execution.

Slice D adds shared human rendering on `hyprial agent list` and `hyprial agent top`;
root `ps` remains retired and `daemon status` remains daemon-only. Run
`uv run pytest tests/shell/cli/lifecycle/cells tests/shell/cli/lifecycle/test_cli_top.py`
for display goldens, old-daemon unsupported behavior, JSON passthrough and
unavailable-daemon errors. `contract/daemon-lifecycle/run-state-display-vectors.json`
also runs in the isolated contract corpus. Require explicit partial scope and
permission coverage; a legacy top heuristic must not supply run state. Preserve
JSON `state/stateDetail` meanings and distinguish the human LEGACY column from
RUN STATE. These are fixture/CLI checks only. Codex whole-TUI
foreground coverage is E2, and each real permission adapter needs E3
evidence; no D test may close those gates or native ractor acceptance.

Slice E1 adds the interactive current-run refresh (`session.turn.refresh`, see
`contract/daemon-lifecycle/interactive-turn-refresh.md`). Run
`NO_PROXY=127.0.0.1,localhost,::1 uv run pytest tests/daemon/harnesses/carrier/turns
tests/daemon/channels/adapters tests/daemon/harnesses/pi/extension` plus the
isolated lifecycle contract. Required properties: a turn lost during a
disconnect is replaced by the reconnect refresh without new inbox traffic or an
extra model turn; initial idle, failed start, queued preparation, duplicate and
stale producer sequences, daemon restart, old channel lease and an old daemon
without the method are covered; a refresh publishes no `turn.*` event and reads,
settles or acks nothing. Two recovery properties are mandatory and were
regressions in the first E1 review: an accepted refresh acknowledges a contact
gap recorded after a missed lease TTL (heartbeat alone must not), and a
transient refresh failure leaves a bounded retry obligation that ordinary
polling services while a deterministic `METHOD_NOT_FOUND` is cached instead of
retried. Also require: a rejected/duplicate producer sequence cannot clear a
new gap; boot-A → boot-B → late boot-A is rejected with `retired_producer`
even when its sequence is higher, both before and after a TTL gap; bounded
producer-history exhaustion stays unknown until a new mount fence rather than
evicting retired identities; identical registration, counter reset and unregister
must not erase observation fences. Include `test_fence_resets.py` for both Pi and
Claude. Do not persist the run ledger across daemon restart: refreshes must carry
the confirmed `daemonEpoch`; missing/old epochs or unconfirmed sessions return
`stale_daemon_epoch` without clearing either gap. A typed stale-epoch rejection
keeps the carrier's bounded retry obligation. Legacy start/end receipts cannot
clear either gap. A gap created during snapshot admission survives the older statement;
a successful session refresh to a new daemon epoch invalidates an unsupported
cache without registering again; and same-epoch TTL recovery closes the gap
through ordinary heartbeats, once per recovery, without a new turn or consuming
read. Hold a heartbeat snapshot in flight while the poll lane runs: there must
be no second snapshot or diagnostic wait in the poll lane. Change the confirmed
epoch during that flight; the old success/refusal must not settle or disable the
new epoch's obligation. The additive heartbeat `observationGap` hint requests the
snapshot; the heartbeat itself never clears the gap. Coverage is negotiated: a carrier without
`capabilities.wholeSessionTurns=true` stays `carrier-deliveries`
(unknown/partial), never whole-session idle. The shipped Pi extension runs
against a fake daemon socket here — that is protocol evidence only. An attended
real Claude Code / Pi TUI run in an isolated home and socket is still required
before claiming native refresh coverage; report it PASS, FAIL or BLOCKED, and do
not treat a skip as a pass.

## Message body retention (cell model phase A)

Operator envs and defaults for local message bodies in `inbox.sqlite3`:

- `HYPRIAL_CONVERSATION_RECORDER` (default ON). Unset, or any value other
  than `0`/`off`/`false`/`no`, runs the conversation recorder: each
  delivered INTERACTION message (agent to agent request or reply) is written
  to its conversation cell in the owner-home OrgFS space, then its local
  body is released. `0`/`off`/`false`/`no` keeps the body resolver bound but
  records and releases no new INTERACTION body; bodies released by an
  earlier run still read back. It does NOT stop SYSTEM residue (below):
  that runs on every inbox prune with no runtime switch, and the only
  rollback is reverting the slice T change. Set it in the daemon's
  environment before start; it is read once at composition.
- INTERACTION bodies are released only after a record exists, the row is
  acked (inbox) or settled (sender copy), and `delivery.bodyRetentionMs`
  has passed. That value comes from owner-authored agent cells
  (min-combined, clamped 1 h..365 d), and defaults to 7 days.
  `message.read_body` and `message.history` answer byte-identically after
  the release, because they read the body back from the record.
  `message.send.lookup` never reads the body: it compares the stored
  `body_text_sha256`, which the release keeps, so it answers the same too.
- SYSTEM bodies (daemon notices and alarms delivered as mail, `workflow-*`
  dispatches, PAC worker replies) are never recorded. Their body becomes
  residue (only `origin`/`replyTo` are kept) 7 days after ack or settle (the
  code default; cell params do not apply). After that, `message.read_body`
  answers `MESSAGE_BODY_UNAVAILABLE` and history shows an empty text. Pending
  rows and `system_notices` (own 90 d / 10 k prune) are never touched. A row
  the sweep cannot decode (for example a lifecycle written by a newer build)
  is marked `body_class = 'undecodable'` and keeps its body; the rest of the
  prune carries on.
- E2E on these paths uses disposable homes and an isolated OrgFS. The
  retention-time golden is
  `uv run pytest tests/daemon/golden/test_retention_golden.py`, and the SYSTEM
  residue tests are `tests/daemon/delivery/bodies/test_system_residue.py`.
  Both run on a controlled clock. Never shorten retention on a production
  home to observe a drop.

## Codex interactive settled-batch recovery (0.5.5.3)

Run `uv run pytest tests/daemon/harnesses/interactive/test_codex_carrier_replay.py`
and the Codex/carrier focus with isolated HOME and daemon state. Cover settled,
partial, unavailable and malformed inbox answers; pending system notices;
reopened inbox/carrier stores; the real local turn pump; the 900-second idle
boundary; continuous native notification and thread/read progress; and late
finals. A fake server or reopened SQLite owner is not attended Codex acceptance.

Recovery, enqueue, and the native query after FIFO waiting use daemon evidence.
An on-list sweep self-heals settled FETCHED rows; do not edit a carrier database
manually. Partial batches replay their frozen cursor through the daemon so
only pending members appear in the new daemon-rendered inline/spill text.
STATUS_UNKNOWN releases only the local observer, never interrupts the native
turn or acknowledges inbox work. A restart grants a new 900-second observation
grace. Live native progress must renew that grace even on a long-running turn.

Inspect the actor's Codex worker file under `state/logs/workers/`, not only
`daemon.jsonl`. Require `worker.carrier.recovery_summary`, `fetched_skipped`,
`fetched_removed`, `turn_progress` and (when applicable) `turn_status_unknown`.
For the actual FIFO-late-injection incident, inspect
`worker.carrier.dequeue.skipped` / `worker.carrier.dequeue.rerendered`:
compare `enqueuedAtMs`, `dequeuedAtMs`, `settledAtMs` and `ackSource`.
The last timestamp is carrier observation, not the daemon's actual ack time.
Test external settlement before dequeue, including a sweep that removes the
mapping first: no native prompt, no retry, no turn-result error.
Do not log message bodies. Contract: `contract/wire/inbox-batch-v1.md`.

For Codex E2 recovery, run
`uv run pytest tests/daemon/harnesses/codex/whole_terminal` with isolated HOME
and `HYPRIAL_NETWORK_ISOLATED=1`. Disconnect an established observation socket,
not only the initial subscription; finish a turn during the gap and type another
after reconnect without inbox traffic. Require coverage withdrawal, a new
initialized connection, snapshot reconciliation, and retained listener delivery.
Fail the durable report sink during initial adoption and live start/end reports;
require worker survival, withdrawn coverage, and retry before dedup advances.
While snapshot adoption of A fails, queue completion of A and start of B;
require durable start(A), end(A), start(B) in that order before recovery.
Hold the sink unavailable with a live-event backlog: no later submission may
replace pending A, memory must remain bounded, and overflow must withdraw
coverage rather than imply lossless retention. Run these cross-path checks in
`test_pending_order.py`, not only separate snapshot/live failure tests.
The `codex.whole-terminal.coverage-gap` recovery diagnostic is a coverage
interval, not proof that every short turn during disconnection was retained.

## Run the gate matrix

Run from the repository root and preserve the exact command, commit SHA,
timestamp, duration, final `RESULT` line, per-assertion classifications, and
a sanitized log reference for every row.

Baseline gates, in order:

| Gate | Command | Pass threshold |
| --- | --- | --- |
| Unit and contract tests | `uv sync --extra test && uv run --with libcst==1.9.0 pytest tests/` | Every collected test passes. Tests skipped for missing optional environments are not E2E evidence; list each skip explicitly. |
| Scenario inventory | `uv run python contract/e2e/validate.py` | Prints `RESULT PASS` for every entry in the current scenarios.json; inventory is not scenario execution. |
| Contract corpus (hermetic) | `./contract/run-all.sh --isolated-only` | CLI black-box, wire-golden, scenario inventory, daemon lifecycle, and MCP corpora pass using only isolated state. |
| Contract corpus (installed CLI) | `./contract/run-all.sh --hyprial "$(uv run --project . which hyprial)" --doctor-schema sealed` | The full gate passes against the exact executable under test. |

Scenario runners. Each prints exactly one final `RESULT` summary classified as
`PASS`, `FAIL`, or `BLOCKED`; runners with real-environment preconditions
classify missing prerequisites as `BLOCKED` instead of failing or silently
passing.

| Scenario | Command | Environment and pass threshold |
| --- | --- | --- |
| E2E-001 two-machine send/reply/ack | `uv run python contract/e2e/run-two-machine-roundtrip.py` | Docker two-container mesh: endpoint discovery, durable acceptance, correlated reply, ack, offline queueing, restart self-heal, and a clean leak self-check. `--local` covers only two loopback phases and does not exercise cross-network semantics; report it as diagnostic evidence, never as E2E-001 gate completion. The attended two-Tailnet-host run is the `[REAL ENV]` release form; require `hyprial doctor --json` reachability from both hosts first. |
| E2E-002 pending survives daemon restart | `uv run python contract/e2e/run-daemon-restart-survival.py` | Isolated. Stable `messageId`, fresh `deliveryId`, FIFO preserved, acknowledged history not replayed. |
| E2E-003 pin survives daemon restart | `uv run python contract/e2e/run-pin-restart-survival.py` | Isolated. Pin persisted per conversation, unchanged across restart, historical reply root does not change the DM key, stale pin surfaced without silent reroute. |
| E2E-004 Feishu user/adapter roundtrip | `uv run python contract/e2e/run-feishu-roundtrip.py` | `[REAL ENV]` verified user, dedicated approved App, dedicated chat. Eight-step live flow plus the deterministic negative provider case: custody before `OnIt`, native reply with routing headers, `OnIt` cleared only after acceptance, follow-up and gateway-restart routing through the durable reply index, silent plaintext-free replay, provider failure retains `OnIt`, no secret emitted. |
| E2E-005 uv install/upgrade/uninstall | `uv run python contract/e2e/run-uv-install-flow.py` | Isolated sources/tools/home and local bare remote. Requires explicit HYPRIAL_TAILCAT_BINARY prebuilt input; missing is BLOCKED. Prove installed wheel-side sidecar location, executable/version and no identity writes, then source upgrade/track/no-fallback/dependency-lock and uninstall. Never touches ambient tools or real dev; fixture packaging is not all-platform native acceptance. |
| E2E-006 resident providers | `uv run python contract/e2e/run-resident-providers.py` | `[REAL ENV]` authenticated `pi`, `codex`, and `claude` with the documented models. Provider passthrough persisted, roundtrip and correlated reply per resident, desired-state recovery after each daemon restart, same established Claude session resumed. A missing target must fail worker startup. |
| E2E-007 four Lark adapters | `uv run python contract/e2e/run-four-lark-adapters.py` | Isolated. Secret-safe migration dry-run/apply and desired-state recovery. The worker emits only a fake WebSocket-ready frame: the runner's daemon status projection proves migration, handshake, and supervisor recovery, not real Lark credentials, event delivery, stream health, or adapter usability. |
| E2E-008 CC TUI worker | `uv run python contract/e2e/run-cc-tui-worker.py --attended --smoke` | `[REAL ENV]` controlled layer-2 smoke: a logged-in Claude Code CLI starts, attaches, registers with `channelConfirmed=true`, exits, and releases session visibility. **This row is gating** (promoted 2026-08-19 after the CC >=2.1.227 channel-contract break proved it catches real regressions; its CI form does not depend on reply delivery). If it ever goes red, a matching production signature means an incident gets filed — never waive it back to non-gating. The full `--attended` flow exercises wake/read/reply/ack/FIFO behavior for manual diagnostics and forensics; it is not the controlled release gate. |
| E2E-009 clean first run creates no agent | `uv run python contract/e2e/run-first-run-no-agent.py` | Isolated: init, a repeated init and a daemon restart create no agent, adapter, route or pin, and every retired `init onboarding` command and old step id exits 2. No App credential, model reply or human action is involved. CI gate: `run-isolated-gate.py` in the credential-free `e2e-macos-isolated.yml`; it is not part of the credentialed gate's verdict. |
| E2E-010 same-interpreter upgrade continuity | `uv run python contract/e2e/run-upgrade-continuity.py` | Isolated deployment from `v0.5.1`, the oldest supported build with upgrade-safe channel logging; fetch read-only (`HYPRIAL_E2E_UPGRADE_FROM` overrides). Explicitly pin `UV_PYTHON` to the initial interpreter, assert its minor version is unchanged and the held-open old channel survives, then prove restart, adapter/session recovery, FIFO, fresh delivery IDs and no replay. Separate named pre-v0.5.1 and cross-minor known-limitation runners assert their expected failures and are not continuity passes. Follow `10-upgrade-continuity.md` for the forward-case/promotion triggers after the interpreter fix ships. |
| E2E-011 Lark App onboarding | `uv run python contract/e2e/run-lark-app-onboarding.py` | Executable through the PTY, scope/event declaration, verification-URL, persistence, daemon-load, negative-name, and secret-safety seams. Real App creation stops at the human device-authorization boundary and is `BLOCKED` without attended approval. |
| E2E-012 Codex app-server worker | `uv run python contract/e2e/run-codex-worker.py` | `[REAL ENV]` authenticated Codex. Public CLI launch, exact desired state, OS process visibility, correlated reply/ack, daemon recovery with a fresh child PID, post-restart roundtrip, and fresh app-server thread. |
| E2E-013 all-harness lifecycle | `uv run python contract/e2e/run-all-harness-lifecycle.py` | `[REAL ENV]` authenticated Claude, Pi, and Codex plus the dedicated test App for the Lark leg. Cross-harness launch/roundtrip, effective config, busy FIFO, identity-preserving harness/model switch, crash recovery, native attachment/reply delivery, and down/no-resurrection lifecycle. |
| E2E-021 orgfs slot-pool-growth redial | `uv run python contract/e2e/run-orgfs-forwarding-redial.py` | Disposable closed-loopback topology; protocol-v7 leased-roster fixture reports daemon-selected private Unix slots but builds no tunnel. A one-slot pool binds the first leased fixture device without redial, then a second leased device forces legitimate pool growth and `reconfigure_connect`; owner-path fixtures, exact golden checks, 64-token replay and orgfs/request/reply/maintenance/PID checks must remain healthy. |
| E2E-024 peer re-learn after pool growth | `uv run python contract/e2e/run-peer-restart-relearn.py` | Positive control paired with E2E-028: four lossy-loopback modes first bind inside a one-slot pool with zero redials, then exhaust the pool and require `zenoh.forwarding.redialed`, an in-process zid change, bounded peer re-learning, bidirectional delivery, and owned cleanup. |
| E2E-031 group conversations (isolated) | `uv run python contract/e2e/run-group-conversation.py` | One disposable daemon, no provider. Group create/show/list, an unmentioned owner message lands in history and wakes nobody, an owner message mentioning agents wakes only those mentioned agents with exactly one row each, an agent reply via `message reply` lands in the group and wakes nobody, an agent send with `--mention` wakes only the named agent, a reply with `--reply-to` without `--mention` wakes nobody while `--reply-to` with `--mention` wakes only the mentioned agent, removal stops delivery, a graceful restart between send and turn creates no second row, the group stays out of 1:1 lists, history is ordered. `--harness pi` adds a real headless Pi turn through the mock model (GC-11, owner mentions P1 and P2); without it that row is not run. Multi-machine, GUI and IM are out of scope. CI gates: `run-isolated-gate.py` (without the flag) and the `hosted` job of `mock-model-harness-e2e.yml` (`--harness pi`). |
| E2E-028 remote drop without rebuild | `uv run python contract/e2e/run-remote-drop-no-rebuild.py` | Builds real Tailcat and labrelay executables from the current checkout. Two disposable nodes converge an OrgFS directory, then the dialer's owned sidecar is terminated to drop its remote slot. Require the same directory address and fixed endpoint set, zero forwarding changed/redialed events, zero transport reconfigure diagnostics, post-return delivery, and owned cleanup. |
| E2E-025 Tailcat org invite/join | `uv run python contract/e2e/run-tailcat-org-invite-join.py` | Disposable homes, explicit pinned HYPRIAL_TAILCAT_BINARY and HYPRIAL_TAILCAT_LABRELAY. Real sidecars/DERP remain loopback; no production issuer, browser or infrastructure. Explicit forwarding opt-in must pass isolation assertions. Require bootstrap, both directory views, bidirectional OrgFS traffic, mapped-path proof and owned cleanup; missing binaries are BLOCKED. Old-head or dirty-source receipts do not certify the final source. |

Membership timeout changes also require
`uv run pytest tests/daemon/forwarding/membership/budgets/` before E2E-025.
These virtual-clock tests cover 20s proxy exchanges, actor→wire budget
propagation, expired queued commands, and a shared join deadline with a stalled
OrgFS lifecycle. Transport probes cover a valid 100s result and a structured
105s domain timeout for both join IPC methods: the server waits 110s and the
explicit/pending/login clients wait 120s. Also check that the generic 15s IPC
client default does not cap an explicit timeout. They do not prove real proxy
connectivity. Join stderr is a
stage plan, not live sidecar telemetry; use timeout error `operation`, `leg`,
and `budgetSeconds` for diagnosis. Optional validated `bytesReceived` measures
an incomplete control response (including at most one frame-overflow sentinel
byte); absence is not zero and no payload is exposed. Never shorten a wrapper's join timeout below
the 120s CLI budget. See `contract/tailcat-admission/README.md`.

`run-dual-instance-roundtrip.py` is a supporting isolated harness, not a
numbered scenario; running it does not substitute for E2E-001.

### Pi P2 frozen projection settings (2026-10-26)

For changes to the Pi controlled loader or P2 projection validation, run the
real-SDK regressions with isolated pytest state:

```bash
HYPRIAL_REQUIRE_PI_SDK=1 uv run pytest \
  tests/daemon/harnesses/pi/configuration/test_pi_controlled_loader.py \
  tests/identity/agents/home/migration/p2/test_projection_modes.py \
  --basetemp=/tmp/hr-pi-projection-<unique-id>
```

Test both the CI-pinned Pi version and the deployed supported version. The
settings setter must not write the frozen projection, including when C has no
settings.json. These tests prove local SDK/filesystem behavior, not E2E-006 or
provider authentication. Legacy corrupt projections still refuse start; do not
chmod them or erase drift as an automatic repair. An authorized operator uses
`agent config rebind --source <private-C> --dry-run --json`, reviews
`projectionChanges`, then explicitly applies rebind while quiescent. See
`docs/guides/pi-projection-recovery.md` for the recovery and rollback limits.

### Stored harness arguments across starts

For stored launch-argument changes, run
`uv run pytest tests/identity/agents/registry/entity/test_lifecycle.py tests/shell/cli/agents/claude/test_launch.py tests/identity/agents/registry/test_agent_read_port.py tests/shell/cli/agents/test_cli_headless_resume.py tests/daemon/application/transport/channel/sessions/test_guarded_resume.py`.
Include out-of-band writes in both directions, a projection-refresh control,
runtime session bookkeeping, resume preflight/rollback snapshots, explicit-option precedence and invalid permission
modes. Runtime metadata updates preserve current stored arguments; launch reads
use the bounded persistent registry lane. Claude starts announce stored arguments
without exposing their values; no persistent CLI editing option is introduced.

Acceptance needs five fresh starts in an owned temporary home, actual process
argv and new native session JSONL permission fields, plus an explicit invalid-value
refusal. Database rows, desired-state specs and captured launch mocks do not
substitute for that evidence. Report unavailable process inspection or SDK
startup as BLOCKED with the exact error; never call an unconfirmed start ready.

When invoking these tests from a managed worker, use owned HOME/TMPDIR roots and
a task-local UV cache. Keep inherited worker/node identity, provider credentials
and proxy routes out of the test environment; loopback fixtures must bypass proxies.
Record the execution environment with the source SHA. These are test preconditions,
not a replacement for the native acceptance rows above.

### Claude context sizing (2026-10-17 incident)

A Claude worker on `--provider deepseek` launches with a stated context window
and compaction boundary (`CLAUDE_CODE_MAX_CONTEXT_TOKENS=1048576`,
`CLAUDE_CODE_AUTO_COMPACT_WINDOW=200000`; see
`docs/reference/harness-model-providers.md`). It is stated because Claude Code
cannot derive a window from `deepseek-flash`, and without it a session ran to
the vendor's 1,048,576 limit with no compact boundary and became unusable.

When a change touches the provider seam, the launch environment, or the
child-environment vocabulary, check both halves:

1. Deterministic (no provider needed):
   `uv run --with libcst==1.9.0 pytest tests/daemon/harnesses/providers/configuration/test_claude_context_window.py`
   — the pair reaches the SDK worker, the PTY connector, and a resume; an
   explicit value in the launch environment wins and a blank one falls back.
2. Real environment (E2E-006/E2E-013's claude legs): after a long session, the
   transcript must show a compact boundary instead of a terminal provider
   limit error, and the session must resume. A one-turn roundtrip does not
   exercise this: the failure only appears once a conversation outgrows the
   window, so a green short roundtrip is not evidence either way.

`CLAUDE_CODE_AUTO_COMPACT_WINDOW`/`CLAUDE_CODE_MAX_CONTEXT_TOKENS` in the
daemon environment override the defaults; a deployment that sets them is
already exercising this path (`ps eww -p <worker pid>` lists the environment
the daemon composed for that child).

Treat every skip as an explicit result with a concrete reason and release
impact. Never report a required row as green when its prerequisite was
absent; that row is `BLOCKED`.

The full `tests/` gate includes the one-off migration-tool suite. Its LibCST
pin is owned by `scripts/refactor/pyproject.toml`; `--with` overlays this tool
dependency without changing the product lock. Product-only focused tests and
the scenario runners do not require the overlay. Never exclude the tool suite
or classify its import/collection errors as optional-environment skips.

### BLOCKED discipline: exact errors, and product vs environment

- A `BLOCKED` verdict must carry the **exact error text** (the runner line,
  the API code, the CLI stderr), never a paraphrase. "Blocked because the
  profile is bound to a production App" without the underlying runner output
  is how a previous verification misattributed an environment gap.
- Distinguish **product failure** (the code under test misbehaves — a
  release blocker) from **environment/prerequisite unavailability** (expired
  token, stopped test App, missing account, wrong config outside the repo).
  The latter is never a product defect: report it as `BLOCKED` with the
  exact error, name the owner action that unblocks it, and do not count it
  against the release candidate.
- Before declaring `BLOCKED`, re-read the runner's preflight logic: many
  runners pin their own identity/config (E2E-004 pins the App by name and
  app id and resolves the chat from config), so a plausible-sounding
  environmental objection may not actually apply.

## CLI regroup acceptance (0.5.5.2)

Use `org service` and `org fs` (`agent link`, `message thread` and `user proxy` are retired).
Their old top-level paths exit 2 with one `moved: hyprial <old>  →  hyprial <new>`
line on stderr and empty stdout, including under `--json`; they are not aliases.
`host` remains top-level. Bare groups print help and exit 2 except `init`,
`daemon upgrade`, `agent tiers`, and `routine templates`. Bare `init` initializes.
`hyprial init onboarding` no longer exists: `init` has no `onboarding` group,
so the old subcommands and step ids exit 2.
Run `uv run pytest tests/shell/cli/surface tests/shell/cli/installation/test_cli_init.py`
and `./contract/run-all.sh --isolated-only`. These do not prove live service or
provider behavior. The surface snapshot and exact owned exception table must
remain consistent with the registered tree.

## Service connect acceptance

`hyprial org service` mappings (see `docs/guides/service-connect.md`) need their
own evidence rows. Isolated checks are not live acceptance:

| Layer | Command or evidence | What it proves |
| --- | --- | --- |
| Contracts and unit | `contract/service-connect/` fixtures, `tests/` service focus, CLI black-box `service group and daemon-status command move` | RPCs, help shape, and the move of the old bare `hyprial service` status payload to `hyprial daemon status` (no alias) |
| Sidecar | `go test ./...` and `go test -race ./internal/engine/ ./internal/serve/` in `sidecar/hyprial-tailcat`, outside any sandbox that blocks `netmon` | Host/servicehost and loopback relay behaviour. `netmon.New: operation not permitted` is `BLOCKED(sandbox)`, never a pass |
| Generic host image | `docker compose -f deploy/compose.yaml build host` in `sidecar/hyprial-tailcat` | The operator image builds. It does not prove a reachable service |
| Mesh regression | E2E-021 plus the forwarding redial/coexist tests | Service mappings did not break OrgFS forwarding |
| `[REAL ENV]` live | Attended run on approved disposable hosts | The only row that proves a working service |

The live row needs, from the operator and never through chat or the repository:
an operator-built host image with a durable host key and approved caller-key
allow-set (`allowAny` false), a reviewed catalog (or a builtin-only decision),
the protected `$HYPRIAL_HOME/state/tailcat/service-access.json` (`0600` file in a
`0700` directory, daemon-owned), and an application key delivered through
that application's own secret flow. Run it against a disposable `HYPRIAL_HOME`,
never the production daemon.

Cover and record each item:

1. `hyprial org service guide <name> --full`, `connect`, then manually export only
   the printed environment hint. The daemon never edits the shell or injects
   application keys.
2. A real streamed application request through the mapping, while mesh traffic
   and several service streams run concurrently, including a same-DERP-region
   relay path.
3. Fixed port strictness: occupy the port and confirm the intent shows as
   failed instead of moving ports; then restart and confirm it binds the same
   port.
4. Record rotation and disable, and caller revocation on the host. Each must
   fail closed. Existing streams are expected to break; observe recovery with a
   new connection.
5. `hyprial org service disconnect <name>` removes the durable intent and the
   listener.

`lastDialMs` is TCP dial time, not RTT or application health, and
`path=unknown` is a valid honest answer. A connected mapping proves a listener
and transport, not HTTP status or authentication. Any missing input above makes
the live row `BLOCKED` with the named owner action.

## Apply the release gate

For selected-device removal, run
`uv run pytest tests/daemon/org/network/status/test_device_removal.py`.
`hyprial org remove <org> <user> --device <id> --yes` keeps user memberships
and refuses the last active device; omitting `--device` removes the user and
all active devices. Unit fixtures are not live Casdoor or sidecar acceptance.
Record actual roster, Casdoor group, directory and ACL before/after reads in
an approved disposable environment, including restart and error cases.

Publication policy has one source: `docs/guides/release-rules.md` (Allen,
2026-11-08 dev-first flow). Prepare VERSION and CHANGELOG on dev and pin a full
candidate SHA from its history. `release-tag.yml` validates that candidate;
main pushes no longer request a release. Internal publication and formal tag
writes are held during 0.5.5.4 convergence; dry-run checks are not evidence that
Python, GUI, migration E2E or all five native wheel builds passed. Complete the
exact-SHA matrix and release decision before separately removing a hold.
Verify GitHub Release assets, Forgejo registry files and all five anonymous R2
wheel downloads using the release-rules checklist. Local policy tests do not
prove live publication. Do not cancel an automatic publication without Allen's
explicit approval.

1. **Measure baseline and candidate repeatedly.** Before and after the release
   candidate change, capture gate/scenario outcomes against named SHAs by exact
   assertion ID. Run each revision at least twice and prefer an interleaved
   order such as baseline → candidate → baseline → candidate so time-dependent
   flakes do not masquerade as change effects. A single baseline snapshot plus
   a single candidate run proves only that those two observed sets differed;
   aggregate test-count changes prove no attribution. Do not label an
   assertion a new regression unless the exact ID repeatedly passes on baseline
   and repeatedly fails on candidate. If it varies on either revision, classify
   it as intermittent/unknown and gather more evidence.
2. **Run incremental revalidation.** Inspect the candidate diff, rerun the
   pytest and contract-corpus gates, and rerun every scenario
   whose transport, daemon, adapter, provider, distribution, or test seam
   changed. Record what was selected and why; a narrow rerun does not erase
   baseline failures.
3. **Run `[REAL ENV]` scenarios last.** Recheck accounts and tooling
   immediately before each run, use only the dedicated App and chat, warn
   participants, and capture correlated user-to-bot-to-actor evidence with
   sanitized IDs and unique markers.
4. **Merge the evidence sets.** Combine the baseline matrix, incremental
   results, and final `[REAL ENV]` results into one release verdict. Pass
   only when every required row has current passing evidence and every skip
   or accepted `BLOCKED` is explicitly accepted by the release owner.

If E2E exposes a product or runtime bug, preserve the failing `RESULT` line,
the command, SHA, expected versus observed behavior, sanitized evidence, and
cleanup status. Report the bug; do not silently change feature code while
performing the release gate.

## Report the result

Return:

- tested SHA and environment identity without secrets;
- preflight status;
- one row per gate and scenario with command, final `RESULT` line, duration,
  and classification (`PASS`, `FAIL`, `BLOCKED`, or `SKIP: <reason>`);
- baseline, incremental, and `[REAL ENV]` evidence references;
- Feishu and Docker visible side effects plus cleanup status;
- bugs or blockers, and a final `PASS` or `NO-GO` release verdict.

Never reduce `BLOCKED`, `FAIL`, or an unexplained `SKIP` to a warning.
