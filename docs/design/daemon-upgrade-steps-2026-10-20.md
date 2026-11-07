# daemon upgrade steps and mcp argv migration (2026-10-20)

## Scope
Only files the launcher itself persisted under the selected state root:
- `<state>/claude-channel-<sessionRef>-<nonce>.json`: per-launch Claude MCP
  config. Rewrite `mcpServers.*.args` where the prefix is exactly
  `["-m", "hyprial.cli", "mcp", <entry>, ...]` to
  `["-m", "hyprial.daemon.impl.mcp.entry", <entry>, ...]`.
- Any settings-shaped JSON discovered beside it (current releases pass
  `--settings` inline, so normally none): recursively rewrite
  `hooks.*[].hooks[].args` only under the same exact prefix.

Never scan/rewrite `~/.hyprial`, `~/.claude`, project files, or arbitrary
JSON. Only regular files directly under the configured state root, no symlinks,
owner-only reads/writes.

## Idempotency
- Recognise only the exact old prefix; already-new files are untouched.
- Atomic write to a sibling temporary, fsync, rename, preserve mode.
- One scan per daemon startup after version change; a marker
  `<state>/mcp-entry-migration.v1.json` records completed paths and the scan
  time, but a newly appearing old file is still eligible on later starts.
- Re-running with no old prefix produces no writes.

## Failure / doctor
- Parse error, unreadable file, symlink, or ambiguous argv:
  do not rewrite; emit `daemon.mcp-entry-migration.failed` with path + error.
- Doctor check scans the same files (read-only) and reports
  `legacyArgv` count/paths with the required relaunch action. It must not
  auto-fix on a doctor read.
- Running processes are out of scope: their argv is immutable. An old
  SessionStart/StopFailure hook survives only inside a live process and is
  replaced by the new launcher on the next relaunch; doctor reports the
  session as needing relaunch, not as a daemon failure.

## Removal of compatibility surface
After migration ships, delete the hidden `mcp` group and its tests/docs;
remove `run_mcp_entry` from the shell's public use. `docs/design/cli-surface/expected-commands.md`
changes from hidden alias to retired-with-migration. No operator aliases.

## Live-process gap (added after review)

An editable install makes the argv in a live process stale the moment the code
changes: new spawns (StopFailure hooks and a crashed channel's MCP reconnect)
use the in-memory old argv and exit 2. Running MCP servers stay up because
their module is already loaded; only new spawns fail.

Doctor therefore adds a read-only live-process check:
- scan this machine's process table for argv containing
  `hyprial.cli mcp` (POSIX `ps`/`/proc`; Windows equivalent), parse the
  `mcp <entry>` suffix, and list pid/entry plus session/actor when the argv
  carries them;
- report `legacyArgvProcesses` with `relaunchRequired: true`; never signal or
  kill a process;
- never print credentials or unrelated command text; only the matched argv
  fields needed for the report.

Deployment order becomes mandatory: migrate files, then relaunch every agent
session immediately, then run doctor until `legacyArgvProcesses` is empty.
This goes in the implementation PR body, ROADMAP and the deployment runbook;
the deploy batch itself needs Allen's terminal authorization.

## Versioned daemon upgrade steps (Allen-approved scope)

### Framework
- Add a daemon-domain step registry (kernel -> identity -> daemon; shell only
  displays). Each step has a stable ID, an idempotent `run`, and a result
  recorded under `<state>/upgrade-steps/<id>.json` (attempt, outcome, error,
  updatedAt).
- One registry: `daemon/impl/upgrade/steps/` with unique IDs, idempotent
  steps, atomic result records, and a shared runner.
- Execution has exactly two entry points, both using that runner:
  `hyprial daemon upgrade` (install -> restart -> run pending steps -> report)
  and `hyprial doctor --fix` (rerun pending/failed steps). The daemon
  scheduler's automatic update calls the same upgrade function, not a third
  path.
- Daemon startup performs a READ-ONLY check only: if steps are pending it
  logs a warn event and does not execute them.
- A failing step does not block daemon startup. It remains `pending`; plain
  `doctor` lists it, `doctor --fix` retries. Successful steps are skipped by
  marker.
- Step execution is bounded/locked so two runners cannot execute a step
  twice; results are written atomically.
- Existing scattered lazy migrations (restore_migration, routine address
  rewrite, schema_version, ...) are out of scope for this change and stay as
  they are.

### Step 1: mcp-entry argv migration
Exactly the file migration above, plus the read-only live-process detector
(process-table argv containing `hyprial.cli mcp` -> `relaunchRequired`). File
migration is step 1; live processes are reported, never killed.

### Step 2: persisted legacy timer argv
Old installed launchd/systemd units may still call `upgrade run` (or the
removed `autoupdate run`). They would fail silently after the command-tree
move. Step 2 detects those units and:
- removes/disables them once the new daemon-owned scheduler is available
  (the units are retired, so no argv rewrite is needed for them); OR
- if removal is not possible, rewrites their argv to the new hidden
  `daemon upgrade run` entry and records the fallback.
Either way the step is idempotent, records the paths/outcome, and leaves a
doctor finding when neither removal nor rewrite succeeds. This is the explicit
answer to "why no rewrite": the units are removed because the daemon scheduler
supersedes them; rewrite is only a fallback.

### `hyprial daemon upgrade`
- Move the existing top-level `upgrade` group (`status`, `apply`, `run`) under
  `daemon upgrade`; remove the hidden `autoupdate` status/restart/run aliases.
- `hyprial daemon upgrade` performs: install -> restart onto the installed
  version -> wait for upgrade steps to settle -> print step results and the
  sessions that still require relaunch (from step 1's process scan).
- `daemon upgrade status` shows step/marker state and pending failures;
  `doctor` links to it. `run` remains hidden for the daemon-owned scheduler
  and any rewritten legacy unit fallback.
- No operator aliases. After step 1 ships and migrations are confirmed, delete
  the hidden `mcp` group and `run_mcp_entry` shell binding.

### Deployment
Order for this change: after the git/editable switch run
`hyprial daemon upgrade` (or `doctor --fix`) -> immediately relaunch every
agent session -> run doctor until legacy argv/process counts are zero.
Deployment batches need Allen's terminal authorization; implementation PR,
ROADMAP and runbook must state this and the editable-install new-spawn
failure mode.

## Step 2 safety fence (hard requirement)

Only the legacy **autoupdate** units are eligible, matched by exact label or
exact filename, never by substring/glob:
- launchd: `~/Library/LaunchAgents/com.hyprial.hyprial.autoupdate.plist`
  with label `com.hyprial.hyprial.autoupdate`;
- systemd: `~/.config/systemd/user/hyprial-autoupdate.service` and
  `~/.config/systemd/user/hyprial-autoupdate.timer`
  (`SYSTEMD_SERVICE_UNIT` / `SYSTEMD_TIMER_UNIT`).

Never touch `com.hyprial.hyprial.daemon` / `com.hyprial.hyprial.daemon.plist`,
the `hyprial-daemon` service unit, any other hyprial unit, or third-party
units. Removal uses an injected command runner:
- launchd only probes/bootouts `gui/<uid>/com.hyprial.hyprial.autoupdate`;
- systemd only `systemctl --user disable --now hyprial-autoupdate.timer` and
  `daemon-reload`.

Tests must include:
- must-fire: the exact autoupdate timer/plist is removed (with the injected
  runner recording only the timer label);
- must-not-fire: `com.hyprial.hyprial.daemon.plist`, another hyprial unit and
  a third-party unit in the same directory remain byte-identical;
- a fuzzy/malformed/ambiguous match or parse failure leaves the file untouched
  and records a doctor finding.

## Production daemon plist naming (must-not-fire fixture)

Production has `~/Library/LaunchAgents/com.hyprial.daemon.plist` with label
`com.hyprial.daemon`, while the current tree's
`daemon/impl/operations/service.py` uses
`com.hyprial.hyprial.daemon`. Current refs contain no producer of the shorter
`com.hyprial.daemon` name; it is an older installed artifact, not a file this
migration may touch.

The must-not-fire fixture therefore places BOTH:
- `com.hyprial.daemon.plist`, and
- `com.hyprial.hyprial.daemon.plist`
next to the autoupdate plist and asserts both stay byte-identical. The exact
autoupdate whitelist cannot match either daemon name.

Additional must-not-fire fixtures from the real HQ LaunchAgents directory
(placeholder contents, byte-identical checks, no launchctl call):
- `com.hyprial.daemon-watchdog.plist`
- `com.hyprial.infra-ops-daemon-upgrade.plist`
- `com.hyprial.infra-ops-daemon-supervise.plist`
- `com.hyprial.infra-ops-daemon-reinstall.plist`
These contain `daemon`/`upgrade` substrings but are not the autoupdate label;
the exact whitelist must leave every one untouched.

## Review hardening (2026-10-20)

- `mcp-entry-v1` is `repeatable`: a done marker does not suppress a later scan,
  so an old launcher file that appears after the first run is still migrated.
  Only `mcpServers[*].args` and `hooks.*[*].hooks[*].args` are rewritten;
  arbitrary arrays in the JSON are left untouched.
- A malformed exact autoupdate unit is never deleted: Step2 validates the
  launchd plist label / systemd `[Unit]` before removal and records a failed
  step plus doctor finding.
- `upgrade_step_status` is read-only and does not create `<state>/upgrade-steps`.
- Doctor returns `relaunchRequired: true` and `warn` whenever the live-process
  scan finds legacy argv, even when no file/step is pending.
- `daemon upgrade run` and `daemon upgrade apply` execute the same step runner;
  the scheduler child uses `daemon upgrade run`.
- Marker/config writes are fsync'd, mode 0600, and the parent directory is
  fsync'd after rename.

### Second review corrections

- Step1 is a rescan step; its marker records the last scan, and `upgrade_step_status`
  also scans files read-only so late old argv is visible without writing.
- Doctor's live field is `legacyArgvProcesses` with `relaunchRequired`, computed
  at read time and never stored in a completed step marker.
- Plain status/startup never creates `<state>/upgrade-steps`; only the runner does.
- Step2 validates the launchd label/ProgramArguments and systemd ExecStart/Unit
  references before calling the exact-label remover; malformed exact files stay
  untouched and become a failed step/doctor finding.
- Auto `daemon upgrade run` and `daemon upgrade apply` run the shared step runner.

### Retired timer argv compatibility

The pre-F8 real templates invoke `hyprial autoupdate run --json` (after R5 some
variants invoke `hyprial upgrade run --json`). Step2's validator accepts either
old argv, while still requiring the exact autoupdate label/path; `daemon upgrade
run` is the new fallback. Red-first fixture `8d9420a84` covers both real launchd
and systemd templates.

The CLI entry also keeps unregistered machine-argv shims because the process
performing an upgrade is the previous release. These shims may be removed only
in a release whose **previous** release already writes the new argv everywhere
and whose upgrade steps have rewritten persisted timers and harness configs.
Therefore 0.5.6 is the earliest possible removal release. They must not become
operator aliases or appear in CLI help.

### Exact retired command tail

Step2 parses `ProgramArguments` / `ExecStart` as argv and accepts only the
retired tails `autoupdate run [--json]`, `upgrade run [--json]`, or
`daemon upgrade run [--json]` after the executable. Unordered membership or a
comment mentioning the words is not evidence; foreign argv at the exact label
is preserved and reported. Red-first `7b1e9653d`; fixed at `afeeedcd0`.

The parsed executable basename must be `hyprial`; reordered/extra argv,
foreign executables, and more than one `ExecStart`/`Unit` are foreign and stay
byte-identical. Red-first `dc8d84465`; fixed at `9ea884cd6`.

A symlink at an exact timer path is refused before validation/removal (lstat,
not follow); the link and target remain unchanged and the step records
`symlink refused at the exact timer path`. Red `6137beb36`, fix `59dd54c06`.

Every component from HOME to the exact timer path is lstat-checked; a
symlinked ancestor, a final symlink, a non-regular file, `st_nlink != 1`,
foreign uid or world-writable mode is refused before any service command.
Removal repeats the same check. Red `6491c0cd8`; fix `49d1d3dc1`.

HOME itself must be canonical: `abspath(HOME)` is compared with `resolve()`,
so a symlinked parent above HOME or a `symlink/../` alias is refused too;
relative paths containing `..` are rejected. Red `126e8dcc7`, fix `006fc16fb`.

### Canonical HOME correction

`canonical_timer_home` canonicalizes HOME once with `os.path.realpath`.
Only the macOS OS temp aliases (`/tmp -> /private/tmp`, `/var ->
/private/var`) are accepted; any other parent symlink or `symlink/../home`
alias is refused before candidates are built. The canonical HOME is then used
for the path and every component is lstat-checked. Red `126e8dcc7`; fix
`0e904914d`.

The OS temp exception compares the suffix verbatim: `/tmp/x` is accepted only
when it canonicalizes to exactly `/private/tmp/x` (same for `/var`), never by
prefix. Relative HOME is refused outright. Red `bd86bb996`, fix after it.

Any `..` component in the supplied HOME is refused before `normpath`/
`realpath`, so a user symlink plus `..` can never cancel into an OS temp
alias. Red `d4e1c55ed`, fix `22e467966`.

Duplicate leading slashes are collapsed for canonical comparison: legal
`//private/tmp/...` and `//tmp/...` HOMEs are accepted and processed; the
`..`/user-symlink checks still run on the original components first. Red
`270590678`, fix `2a9729a5e`.
