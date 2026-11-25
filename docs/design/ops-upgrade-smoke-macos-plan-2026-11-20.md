# 运维升级脚本支持 macOS — investigation and implementation plan

## Status and handover (2026-11-22)

This file is the investigation and plan written on 2026-11-20 for the GUI single-launch line, landed in the
repository so it does not live only in a session. Sections 1 to 8 are preserved verbatim below as written on
that date and were not revised afterwards; where they disagree with this section, this section is newer.

- **Done (implementation & real-host drills):**
  - **PR-1 (`#3024`, merged):** GUI variables read from the live daemon, GUI record validated, a GUI failure
    rolls back, `--stop-timeout` default 180, `--gui-hold-minutes`, per-unit GUI log, results
    `FAILED_STOP_TIMEOUT_RESTORED`, `FAILED_STOP_TIMEOUT_RESTORE_FAILED`, `ROLLED_BACK_GUI_UNIT_LEFT`. Verified
    twice on a real Linux host on 2026-11-21 (`#3024` comments `39022` and `39023`; run 1 caught `GUI_RUNTIME_BUSY`
    and rolled back cleanly; run 2 passed).
  - **PR-2 (`#3049`, merged):** service seam through `hyprial daemon service`, derived unit comparison,
    `summary.json` schema `hyprial-dev-upgrade-smoke/v2`, and F1 fix filtering `HYPRIAL_NODE_ID` from smoke
    child environments (`droppedEnvironment`).
  - **PR-3 (`#3051`, merged):** portable `--stdin-stop --no-open --json` GUI child (removing `systemd-run`),
    plus pre-stop `GUI_RUNTIME_BUSY` probe (`gui.runtime-free`). Real macOS 27.0.1 ARM64 dedicated-home drill
    completed on 2026-11-21 (`#3051` comment `39184`: KeepAlive non-relaunch proof, `preflight`, `apply`,
    `apply --gui --gui-port 0`, forced rollback, and `GUI_RUNTIME_BUSY` pre-stop `ABORTED` exit `2`).
  - **PR-4 (`#3083`, `feat/ops-upgrade-smoke-macos` up to `bf39b0648a`):** macOS `lsof` native-actor loaded
    check, short `/tmp/hsm-*` isolated cold-start root with seeded `node-id` (`70c912e3c2`), F2
    `<tool-dir>/bin/python` fallback, F3 `post.routines` non-zero exit handling, `scripts/ops/README.md` macOS
    section, and post-drill error-path guards (`708df4c438` pre-mkdir socket-length `BLOCKED` check;
    `bf39b0648a` `lsof` `timed_out`/`rc=127` guard, unit-tested).
  - **PR-5 (`docs/ops-upgrade-smoke-mac-evidence`, current):** redacted WORM evidence for the 2026-11-21 PR-3
    and 2026-11-22 PR-4 real macOS 27.0.1 ARM64 drills recorded in
    [docs/notes/ops-upgrade-smoke-macos-evidence-2026-11-22.md](../notes/ops-upgrade-smoke-macos-evidence-2026-11-22.md);
    macOS verification status table added to `scripts/ops/README.md`; F4 (`hyprial gui --stdin-stop` killed via
    `SIGHUP` leaving its frontend child in an orphan session; stdin close cleans up) deferred and registered
    as `KP-120` in `docs/reference/known-problems.md`; F5 documented in `scripts/ops/README.md` and automated in
    throwaway `cold-home` (`70c912e3c2`).
- **Owner decisions & handover:** 2026-11-20 the owner accepted every recommendation in section 7 (Q1 to Q9).
  2026-11-21 the owner handed the remaining work and real-host drills to the Mac packaging line, and accepted
  the pre-stop `GUI_RUNTIME_BUSY` refusal shape implemented in PR-3 (`#3051`). The GUI single-launch session
  is closed; questions go to the owner.
- **Measured Linux systemd probe (from 2026-11-21 handover, `#2980`):** a unit killed by stop timeout ends
  `failed` with `Result=timeout` and `MainPID=0`, while `systemctl --user stop` exits 0; a `start` issued while
  the stop job is pending is queued and runs after the kill (`Result` reads `success`); `systemd-run --setenv`
  and `StandardOutput=append:` accept spaces; stopping an unloaded unit exits 5.
- **Remaining `UNKNOWN` / `NOT_RUN` scope** (detailed in
  [docs/notes/ops-upgrade-smoke-macos-evidence-2026-11-22.md](../notes/ops-upgrade-smoke-macos-evidence-2026-11-22.md)):
  owned/authenticated macOS home upgrade (`--expect-routine` passing, `--require-pac-schema`), real macOS
  `--gui-hold-minutes` + SSH tunnel, headless SSH session without console login (`SERVICE_MANAGER_FAILED`),
  macOS `rollback.raw-stop` / stop-timeout / `DAEMON_START_TIMEOUT` recovery, post-drill `708df4c438` /
  `bf39b0648a` error-path guards on real macOS (`socketPathLimit` `BLOCKED`, `lsof` `timed_out=True` / SIP),
  and verification of any future `KP-120` fix.

---

- Baseline: `dev` at `1e4cfcc7fb` (2026-11-20, merge #3016).
- Method: reading code only. The script, its tests and every hyprial/service command were NOT run.
  Every statement about runtime behaviour below is derived from code and is UNKNOWN until measured.
- All paths are relative to the repo root unless absolute.

---

## 1. Current state of the script on dev

### 1.1 Files and history

| File | Lines | Role |
|---|---|---|
| `scripts/ops/dev_upgrade_smoke.py` | 9 | entrypoint, imports `_dev_upgrade_smoke.cli.main` |
| `scripts/ops/_dev_upgrade_smoke/cli.py` | 121 | argparse; flags; exit codes in DESCRIPTION |
| `scripts/ops/_dev_upgrade_smoke/base.py` | 354 | state, `Runner` use, `service_show` (systemctl), paths, env, summary |
| `scripts/ops/_dev_upgrade_smoke/common.py` | 454 | `Runner` (only subprocess seam), `Recorder`, wheel inspection, parsers |
| `scripts/ops/_dev_upgrade_smoke/preflight.py` | 445 | baseline + isolated install + optional cold start |
| `scripts/ops/_dev_upgrade_smoke/apply.py` | 538 | stop/install/start/ready/postcheck/GUI/rollback |
| `scripts/ops/_dev_upgrade_smoke/smoke.py` | 56 | dispatcher |
| `scripts/ops/_dev_upgrade_smoke/__init__.py` | 88 | re-exports |
| `scripts/ops/README.md` | 85 | operator doc |
| `tests/tooling/ops/test_dev_upgrade_smoke.py` | 283 | 10 tests, fake runner |

- `git log -- scripts/ops tests/tooling/ops`: exactly one commit, `8a4e98ca0d` (2026-11-16, author
  `agent-infra-ops`, "feat(ops): add host-neutral dev upgrade smoke instrument"), merged as #2872
  (`80d9eefaf8`). **Nobody changed it since 11-16.** Owning line: agent-infra-ops (ops/infra line).
- Commit message: "Optional GUI starts through a transient user unit; the handoff grant is never read."

### 1.2 Every OS-specific call

| Where | Call | Purpose | macOS today |
|---|---|---|---|
| `base.py:113-142` `service_show` | `systemctl --user show <service> --property=ActiveState,SubState,MainPID,NRestarts,Result,Environment` | the only service-state probe; used by preflight, stop, start, wait_ready, postcheck, pre-stop guard | rc 127 (`OSError` → `common.py:194-201`) |
| `preflight.py:121-129` | `systemctl --user --version` | toolchain check | FAIL `toolchain.systemctl` |
| `preflight.py:143-151` | uses `service_show` | baseline; `prepare()` returns False on rc≠0 | FAIL `baseline.service` → preflight exit 1 |
| `preflight.py:165-189` + `common.py:120-132` | parses unit `Environment=` for `HYPRIAL_HOME`/`HARNESS_STATE_DIR` | derive home/state, cross-check `--home`/`--state-dir` | unreachable |
| `apply.py:64-97` `stop_service` | `systemctl --user stop <service>` + poll `ActiveState=inactive, MainPID=0` | stop | unreachable |
| `apply.py:99-126` `start_service` | `systemctl --user start` + poll `active/running` (fixed 30 s) | start | unreachable |
| `apply.py:128-172` `wait_ready` | `service_show` gate before each `daemon status` | readiness | unreachable |
| `apply.py:194-210` `postcheck` | `MainPID == daemon pid` | custody | unreachable |
| `apply.py:502-519` `run_apply` | `MainPID` vs `daemon status` pid | "daemon outside the service" guard | unreachable |
| `apply.py:292-316` | reads `/proc/<pid>/maps` (guarded by `sys.platform.startswith("linux")`) | native actor loaded | NOT_RUN on macOS ("process-map check is Linux-only") |
| `apply.py:323-346` `start_gui` | `systemd-run --user --collect --unit=hyprial-gui-smoke-<ts> --property=StandardOutput=append:… --setenv=HYPRIAL_GUI_LAUNCH_TTL_MS=… -- <cli> gui --no-open --json --port <p>` | host GUI beyond the script's life | BLOCKED "systemd-run --user is required" (`apply.py:323-325`) |
| `base.py:194-197,216` | fallback `~/.local/share/uv/tools`, `~/.local/bin` (XDG) | only when `uv tool dir` fails | same defaults as uv on macOS; fine |
| `preflight.py:369-445` `cold_start` | `subprocess.Popen(start_new_session=True)`, `os.killpg` | optional empty-home start | POSIX, works; **risk**: socket path under macOS `$TMPDIR` (`/var/folders/../T/hyprial-smoke-isolated-XXXX/cold-state/daemon.sock`, ~100+ chars) may exceed the 104-byte AF_UNIX limit → `DAEMON_START_FAILED` with `socketPath/length/limit` (contract `service-supervision.md:113-118`) → FAIL, not BLOCKED |
| `apply.py:36`, `preflight.py:275` | `socket.getfqdn()` | `--expect-host` | works; may be slow on macOS without reverse DNS |

Not used anywhere: `journalctl`, `loginctl`, `launchctl`, `ss`, `lsof`, `ps` (grep of `scripts/ops/**`).
The script collects **no daemon/service logs** at all; evidence is only the CLI outputs in `logs/`.

**macOS outcome today (from code):** `preflight` → `toolchain.systemctl` FAIL, `baseline.service` FAIL,
`prepare()` False, exit 1 `FAILED`. `apply` → guard passes, `prepare()` False before anything mutates,
exit 1 `FAILED`. Safe (no mutation) but nothing useful; it is FAIL, not BLOCKED.

### 1.3 Present-day defects independent of macOS (Linux)

1. `--stop-timeout` default 60 s (`cli.py:257`) is shorter than the unit's `TimeoutStopSec=120`
   (`src/hyprial/daemon/impl/services/systemd.service.template`) and the CLI's own
   `SUPERVISOR_STOP_TIMEOUT_SECONDS = 150` (`shell/impl/cli/commands/daemon/service.py:519`). A slow but
   healthy teardown makes `stop_service` FAIL; `run_apply` then returns `FAILED_STOPPED_UNCHANGED` with
   `mutated=False` (`apply.py:520-522`) and **never restarts** the service, so the host can be left with the
   daemon stopped and exit 1 ("rollback succeeded" per README:83-85). The runner timeout `stop_timeout+15`
   (`apply.py:75`) also kills the `systemctl` client mid-job.
2. No unit at all: `systemctl show` of an unknown unit is rc 0 / `inactive`; `stop_service` says "already
   inactive", install proceeds, `systemctl start` fails, rollback start fails → exit 3 `ROLLBACK_FAILED` on a
   host that merely had no unit. Preflight does not refuse it.
3. Default `--service hyprial.service` (`cli.py:249`) is the default home's unit only; since #2830 a
   non-default home's unit is `hyprial-<id>.service` (contract `service-supervision.md:17-31`). The
   `baseline.unit-home` cross-check (`preflight.py:173-180`) catches a mismatch only because the default unit
   renders `HYPRIAL_HOME`; correct but surprising.
4. `base.py:125-126` carries an inline comment (allowed as existing; must move to a header block when the
   function is touched).

### 1.4 Documents describing the script (Skill-Impact)

- `scripts/ops/README.md` (whole file; "systemd user-service" wording lines 7, 66-70, 79-80).
- `scripts/ops/_dev_upgrade_smoke/cli.py:12-41` (DESCRIPTION: "never edits a systemd unit", "user service").
- `src/hyprial/skills/hyprial-ops/SKILL.md:262-263` — "`dev_upgrade_smoke.py apply --gui --ssh-host <host>`
  … 会启动 GUI 并打印隧道命令，但不读取 handoff". **Skill-Impact** if `--gui` semantics change.
- `docs/guides/gui-dev-mode.md:168-170` — same sentence in English.
- No `.agents/skills/**` file, no `contract/**` file and no `docs/design/**` file mentions the script
  (grep). The summary schema `hyprial-dev-upgrade-smoke/v1` (`common.py:20`) is published only via README.

---

## 2. Is `--gui` still correct on Linux after single-launch?

What `start_gui` checks (`apply.py:319-441`): `systemd-run` rc; first JSON object in the unit's stdout file
within 30 s; `payload.ok is True`; TCP connect `127.0.0.1:<--gui-port>`; `stat(handoff_path)` mode has no
group/other bits; prints `ssh -N -L <gui-port>:127.0.0.1:<gui-port> <user>@<ssh-host>`; a constant PASS
"handoff file was not read".

It does **not** read `server.json`, `frontend-authority.json`, `HYPRIAL_GUI_AUTHORITY_PATH`, `server.lock`,
`backend_pid`, or a second process. `handoff_path` still exists in record v1
(`contract/cli/gui-startup-record.v1.json` `startupRecord.required`), still 0600, still one-time.
`HYPRIAL_GUI_LAUNCH_TTL_MS` still exists and still reaches the host (`shell/impl/gui/host/config.py:36,152-155`;
only the build env drops it, `shell/impl/gui/backend/source.py:356-365`; the host env keeps it,
`source.py:309-317,532-546`). So **nothing it checks has vanished**. But it is wrong today in these ways:

1. **Wrong home (high).** `systemd-run --user` does not inherit the caller's environment; only
   `--setenv=HYPRIAL_GUI_LAUNCH_TTL_MS` is passed (`apply.py:330-345`). The GUI therefore runs with the
   user manager's environment: no `HYPRIAL_HOME`/`HARNESS_STATE_DIR` unless the manager has them, i.e. the
   default `~/.hyprial`. Since daemon-first (#2845 2026-11-16, #2916 2026-11-17 — both after the script),
   `hyprial gui` *ensures* the daemon of the home it resolves before admission
   (`docs/guides/gui-dev-mode.md:471-480`; `shell/impl/cli/commands/gui/commands.py:79-91`, launch primitive
   with `autoinstall=False`). With `--home <non-default>` (or a non-default state dir) `apply --gui` will:
   start the **default home's** daemon (through that home's installed unit if one exists, else detached),
   or fail `GUI_HOME_NOT_INITIALIZED`/`GUI_HOME_UNSAFE` when `~/.hyprial` is absent/broad. In the first case
   every GUI check passes against the wrong daemon. Before daemon-first this was a refusal
   (`GUI_DAEMON_UNAVAILABLE`); now it is a side effect on another home. With the default home and default
   state dir the behaviour is correct.
2. **GUI failure exit semantics (medium).** `start_gui` runs after the postcheck; a FAIL there gives
   `self.result = "FAIL"`, exit 1 (`apply.py:536-538`) with the candidate installed and no rollback, while
   README:83-85 and `cli.py:37-41` define exit 1 as "failed, rollback succeeded".
3. **Port taken from the flag, not the record (low).** Listener check and tunnel use `--gui-port`
   (`apply.py:389-424`). `hyprial gui --port 0` is valid since the single launch (`gui-dev-mode.md`
   Options table; contract README:185-187); with `--gui-port 0` the script connects to port 0 and prints
   `-L 0:127.0.0.1:0`. The record's `host` may be `::1`; the script always uses `127.0.0.1`.
4. **Record not validated (low).** `safe_gui_summary` (`common.py:421-439`) keeps keys that no longer exist
   (`sourceCommit`, `frontend_port`, `expires_at`, `ttl_ms`) and drops the ones that now matter
   (`record_version`, `backend_owned`, `source_commit`, `frontend_pid`). It never checks `record_version == 1`,
   `backend_driver == "daemon"`, `backend_pid is None`, `backend_owned is False` (contract README:201-214),
   nor that the daemon pid is unchanged by the GUI start (daemon-first: reuse, not restart).
5. **Lifetime (low).** The transient unit is never stopped; the README does not say how to stop it. A second
   run on the same home gets `GUI_RUNTIME_BUSY` or `GUI_PORT_IN_USE` (the latter only after the daemon ensure,
   `gui-dev-mode.md:489-493`). The grant TTL is set to 60 min (`cli.py:281`, `apply.py:329`) while the
   skill/guide describe a 60 s credential (`SKILL.md:259-262`, `gui-dev-mode.md:159-163`).

Verdict: `--gui` is **not checking vanished artefacts**, but it is **incorrect for non-default homes today
(it can start another home's daemon)** and mis-reports GUI failures. Fix #1 is worth a small standalone PR
even if macOS work is postponed (PR-1 below).

---

## 3. The per-home service surface on dev

### 3.1 Commands (`contract/cli/surface.json`, `contract/daemon-lifecycle/service-supervision.md:5-15`)

| Command | Options | Implementation |
|---|---|---|
| `hyprial daemon service status` | `--json` | `shell/impl/cli/commands/daemon/service.py:265-283`; payload `_status_payload` `service.py:162-186` |
| `hyprial daemon service install` | `--yes --json` | `service.py:286-301`, waits for custody, reverts on loss (`service.py:222-249`) |
| `hyprial daemon service uninstall` | `--yes --json` | `service.py:304-319` |
| `hyprial daemon start` | `--json` | `daemon/commands.py:108-129` → `_launch_daemon_process` `common/daemon_start.py:471-572` |
| `hyprial daemon stop` | `--json --expected-epoch` | `daemon/commands.py:446-460` → `_stop_daemon_for_operator` `common/daemon_stop.py:64-102` |
| `hyprial daemon restart` | `--json --yes` | `daemon/restart.py`; supervisor path `service.py:341-510` |
| `hyprial daemon status` | `--json` | `daemon/commands.py:58-105` |
| `hyprial daemon upgrade [apply/run/status]` | `--source --ref --tag --force --no-restart --json` | `upgrade/commands.py:196-330` |

There is no `daemon service start|stop`: `daemon start|stop` route through the supervisor when an
attributable unit is installed.

`daemon service status --json` keys (contract `service-supervision.md:67-72`): `ok, platform, unit, unitPath,
installed, loaded, running, pid, bootPersistent, changed, attributed, templateMatches, servingPid,
supervision (supervised|unsupervised|lost), relaunchCount, lastExitCode, ownershipBusy, ownershipReason,
backupPath`. Error codes: `SERVICE_PLATFORM_UNSUPPORTED` (exit 1, non-Linux/non-darwin, `service.py:39-44,192-198`),
`SERVICE_OWNERSHIP_REFUSED` (+`ownershipReason`, e.g. `service-account-home-mismatch`,
`hyprial-home-outside-operating-home`), `SERVICE_MANAGER_FAILED` (+`reason`), `SERVICE_SUPERVISION_LOST`,
`CONFIRMATION_REQUIRED`, `OPERATION_DECLINED`.

### 3.2 Guarantees per manager

| Operation | systemd | launchd |
|---|---|---|
| status | `systemctl --user show <unit> LoadState,ActiveState,SubState,MainPID,NRestarts,ExecMainStatus,FragmentPath` + `loginctl show-user` linger (`daemon/impl/operations/service/manager.py:150-212`) | `launchctl print gui/<uid>/<label>`; pid/`runs`/`last exit code` parsed (`operations/service/supervisor.py:80-95`); "not loaded" only for exit 113 + exact line (`service-supervision.md:128-134`) |
| stop (`daemon stop`, supervised daemon) | `systemctl --user stop <unit>` (`service.py:669-683`); unit stays loaded, inactive; `Restart=always` does not restart a manual stop | `launchctl bootout gui/<uid>/<label>` — **unloads** the job, so `KeepAlive` cannot relaunch; status afterwards `loaded:false, supervision:"lost"`; doctor `warn stopped-on-request` (`service-supervision.md:89-101`) |
| stop wait | waits for the teardown receipt (`TEARDOWN_BUDGETED_SECONDS`), **`require_exit=False`**: may return `survivingPid`; does not prove exit (`daemon_stop.py:64-90,120-160`) | same |
| stop, unattributable loaded unit | IPC shutdown only; `supervisorMayRestart: true` + `unattributedSupervisor` in JSON (KP-092 B, `service-supervision.md:103-111`) | same |
| start (`daemon start`, attributable unit installed) | `systemctl --user start <unit>` (`service.py:596-609`), then waits for `ping` (`DAEMON_START_IDLE_BUDGET_SECONDS = 30`, `common/support.py:43`) | `launchctl bootstrap gui/<uid> <plist>` if unloaded, else `kickstart` (`service.py:596-609`); RunAtLoad starts it |
| start, no unit | **auto-installs a unit** when `HYPRIAL_SERVICE_AUTOINSTALL!=0`, HOME is the account home and the program path is stable; else detached spawn (`daemon_start.py:487-572`) | same |
| start with `HYPRIAL_DAEMON_AUTOSTART=0` in env | explicit `daemon start` **bypasses the manager entirely** and spawns detached beside the unit (`daemon_start.py:494-500,530-546`) | same |
| restart | `systemctl --user restart` + fresh-pid custody check | `launchctl kickstart -k` + same check (`service-supervision.md:120-138`) |
| crash relaunch | `Restart=always RestartSec=5` | `KeepAlive` + `ThrottleInterval 5`; daemon-owned backoff ≤ 60 s (`service-supervision.md:214-234`) |
| logs | both: `<state>/daemon-service.stdout.log`, `<state>/daemon-service.stderr.log` (`operations/service/templates.py:47-52,74-79`; systemd `StandardOutput=append:`, launchd `StandardOutPath`) — **no journalctl / `log show` needed** | same |

Attribution depends on the program path the CLI computes from `sys.executable` and `shutil.which("hyprial")`
(`service.py:95-123`) — i.e. on `PATH` of the CLI process — compared with the unit's `ExecStart` /
`ProgramArguments` (`manager.py:601-630`). A uv tool reinstall keeps the venv path, so old and new CLIs
should both attribute the same unit (UNKNOWN until measured, esp. launchd venv symlinks).

### 3.3 How the contract says an upgrade must stop the service on launchd

It does not describe a "stop, replace files, start" upgrade. It describes:
- **stop routing**: an operator `daemon stop` of a supervised daemon goes through `launchctl bootout`
  (unload) precisely so `KeepAlive` cannot undo it (`service-supervision.md:89-101`, `service.py:504-518`
  header block);
- **restart custody** for `daemon upgrade`: install while the daemon runs, then `kickstart -k` and require a
  fresh job pid (`service-supervision.md:120-138`; `upgrade/commands.py:313-321` installs before restart).

`daemon upgrade` itself is unusable for this script: it refuses file:// installs with `UPGRADE_LOCAL_SOURCE`
(`upgrade/commands.py:214-235`) and only installs `git+<url>@<ref>`.
So the script's stop→install→start is consistent with the contract **if** it stops with `hyprial daemon stop`
(bootout) and starts with `hyprial daemon start` (bootstrap). While booted out, a crash or wheel swap cannot
trigger a relaunch. (A user logout/login or reboot during the window re-loads the LaunchAgent via RunAtLoad;
out of scope, record as residual risk.)

### 3.4 Coverage of the script's needs, and gaps

| Need | Covered by | Gap? |
|---|---|---|
| is a unit installed for this home, which one | `daemon service status --json` `installed, attributed, unit, unitPath, platform` | no |
| status with pid, custody | same: `running, pid, servingPid, supervision` | no |
| stop and stay stopped | `daemon stop --json` via manager | no |
| stop **and wait until the process is gone** | `daemon stop` returns after teardown, may report `survivingPid` | **soft gap**: script polls `service status` (`running:false, pid:null`) + `daemon status` (`running:false`) + its own pid liveness. No new CLI needed. |
| start and wait until ready | `daemon start --json` (ping) then script polls `daemon status`/`service status` | no |
| never install a unit during upgrade | refuse homes without an attributable unit, and strip `HYPRIAL_DAEMON_AUTOSTART`/`HYPRIAL_SERVICE_AUTOINSTALL` from the env | **G-A (approval)**: only test-documented opt-out env (`docs/design/daemon-first-z8-per-home-service-handoff-2026-11-15.md:23` "产品代码永不设置"); no CLI flag `--no-autoinstall` |
| rollback when the **candidate CLI cannot run** (import error, crash) | nothing hyprial-mediated: stop must be issued by some hyprial CLI that attributes the unit | **G-B (approval)**: script would need raw `systemctl --user stop <unit>` / `launchctl bootout gui/<uid>/<label>` for the unit recorded at baseline, or a different rollback order (install old first, then old CLI stops/starts). Conflicts with the agreed "no own launchd logic". |
| daemon version on a home without an account (bootstrap) | `daemon status --json` for bootstrap keeps only `running,pid,epoch,nodeId,owner,socket,phase` (`daemon/bootstrap.py:27-35`) — **no `version`**; ping has `version` (`bootstrap/dispatcher.py:298-306`) | **G-C (approval if fixed in product)**: readiness version check fails on bootstrap homes (the only kind a Mac engineer can create without credentials). Options: additive `version` in bootstrap `daemon status` (published JSON → `contract/` sync or `Contract-Impact`), or script reads `version` from `daemon start --json` (flat ping). |
| old wheels before #2830 (merged 2026-11-17 13:14 +0800) | their `daemon stop` may be an IPC shutdown that `Restart=always`/`KeepAlive` undoes | **G-D (decision)**: capability gate on `supervision`/`attributed` in the baseline status; keep the legacy systemctl path on Linux for such baselines or refuse |
| unit template drift (candidate renders other bytes) | `templateMatches` | no gap; record as informational, script never re-renders |

None of G-A..G-D requires new CLI surface if the owner accepts the script-side answers; G-C optionally
requires an additive field.

---

## 4. Design for macOS (one code path, small seam)

### 4.1 Service control: one path through hyprial commands

New module `scripts/ops/_dev_upgrade_smoke/service.py` with a `ServiceControl` seam and two implementations:

- `HyprialServiceControl` (default, both OSes). Every call runs the **live CLI by absolute path** with
  `live_environment()` hardened:
  - `HYPRIAL_HOME`, `HARNESS_STATE_DIR` set (as today, `base.py:176-182`);
  - `PATH` with `bin_dir` first (so `shutil.which("hyprial")` inside the CLI names this installation and
    attribution matches, `service.py:95-123`);
  - drop `HYPRIAL_DAEMON_AUTOSTART`, `HYPRIAL_SERVICE_AUTOINSTALL` (or refuse when set — owner Q6),
    `HARNESS_SOCKET_PATH`, `HYPRIAL_OWNER`.
  - `status()` = `hyprial daemon service status --json` → dict (platform, unit, installed, loaded, running,
    pid, servingPid, attributed, supervision, relaunchCount, lastExitCode, templateMatches).
    `SERVICE_PLATFORM_UNSUPPORTED` → BLOCKED/exit 4; `SERVICE_OWNERSHIP_REFUSED` → exit 2 with `ownershipReason`.
  - `stop()` = `hyprial daemon stop --json`, runner timeout ≥ 180 s. FAIL when `ok` is false or
    `supervisorMayRestart` is true. Then `wait_stopped()` polls until `status().running is False and pid is None`
    AND `daemon status --json` `running:false` AND the baseline pid is dead (4.3), bounded by
    `--stop-timeout` (new default 180 s).
  - `start()` = `hyprial daemon start --json`, runner timeout `--ready-timeout`. FAIL when `alreadyRunning` is
    true (something revived the daemon while we held it down).
  - `custody(pid)` = `status().supervision == "supervised" and status().pid == pid`.
- `LegacySystemdControl` (Linux only, time-boxed, owner Q5): today's `systemctl` code moved verbatim, chosen
  only when the baseline CLI's status lacks `supervision`/`attributed` (pre-#2830 wheel). On macOS such a
  baseline is refused (no legacy launchd code).

Preflight/apply rules that replace the systemd-specific guards:
- home is an input (`--home` > `HYPRIAL_HOME` > `~/.hyprial`); the unit is derived from it by hyprial, so
  `--service` becomes informational (refuse if given and ≠ `status.unit`, owner Q3). Unit-`Environment=`
  parsing (`preflight.py:165-189`) is deleted.
- apply requires `installed && attributed` (refuse exit 2 otherwise: solves defect 1.3-2 and avoids any
  autoinstall); when a daemon answers, require `supervision == "supervised"` (replaces `apply.py:502-519`).
- after the candidate install and **before** start: `status()` with the **new** CLI must still be
  `attributed`; else roll back without starting.
- readiness = `daemon.running`, positive pid, no `lifecycle.crashed`, version equal (from `daemon status`, or
  for `phase == "bootstrap"` from the `version` key of the `daemon start --json` payload, owner Q-G-C) AND
  `custody(pid)`.
- stop failure before mutation: if the daemon is down after a failed stop, issue `start()` with the
  unchanged old CLI and report `FAILED_RESTORED`/`FAILED_STOPPED` honestly (fixes 1.3-1).

### 4.2 Logs

Portable, no journalctl/`log show`: after every stop/start/rollback step and at the end, copy bounded,
redacted (`redact_auth_material`) tails of `<state>/daemon-service.stdout.log`,
`<state>/daemon-service.stderr.log`, `<state>/daemon-launch.log` and `<state>/daemon-start-failures.json`
(service-supervision.md:223-226) into `artifacts/logs/` with byte offsets recorded at baseline so only the
upgrade window is copied. Read-only on the home. Optional on Mac (manual only): `log show --last 10m
--predicate 'subsystem == "com.apple.xpc.launchd"'` is NOT used by the script.

### 4.3 Process / fd checks without /proc

`tests/support/process_table.py` is tests-only and imports `hyprial.kernel` (`process_table.py:35`); the
script runs as `uv run --no-project` and must not import hyprial or tests. New module
`scripts/ops/_dev_upgrade_smoke/process.py` re-implements the same minimal idea: `os.kill(pid, 0)`
(ESRCH = dead, EPERM = alive) plus `ps -o stat=,lstart= -p <pid>` (same flags on Linux/macOS) for zombie
state and a birth string recorded at baseline (pid-reuse fence). All via `Runner` so tests fake it.
Native-actor loaded: Linux keeps `/proc/<pid>/maps`; darwin uses `lsof -nP -p <pid> -Fn` and searches for the
installed `hyprial/_core/*.so` paths; missing tool/permission → BLOCKED, not FAIL.

### 4.4 `--gui` under daemon-first

The script no longer needs to host the GUI under an init system: the daemon is the only long-lived backend and
outlives the GUI (`gui-dev-mode.md:12-15,495-500`); the GUI is a foreground process with a supervising-launcher
mode (`--stdin-stop`, contract README:188-197). Design (`scripts/ops/_dev_upgrade_smoke/gui.py`):

1. Spawn `[live_cli, "gui", "--no-open", "--json", "--port", str(gui_port), "--stdin-stop"]` with
   `stdin=PIPE`, `stdout=PIPE`, `stderr=<artifacts>/gui-startup.err.log`, `start_new_session=True`, env =
   hardened live env minus every `HYPRIAL_GUI_*` except `HYPRIAL_GUI_LAUNCH_TTL_MS`, minus
   `HYPRIAL_BACKEND_ENDPOINT`, `HYPRIAL_GUI_HOME`, `HYPRIAL_DEV_GUI_HOME` (else `GUI_CONFIG_CONFLICT`,
   `gui-dev-mode.md:455-459`). Spawning goes through a new `Runner.spawn()` seam so tests fake it.
2. Read line 1 with a deadline of 120 s (daemon ensure + 60 s host budget, `gui-dev-mode.md:508-513`).
   "exit 0, no line" = clean stop before start → FAIL "GUI stopped before its record".
3. Validate the record: `ok`, `record_version == 1`, required keys, `backend_driver == "daemon"`,
   `backend_pid is None`, `backend_owned is False`, `url` origin = `host:port`, `port == gui_port` unless 0.
4. Listener: connect to (`record.host`, `record.port`). Handoff: stat only — regular file, owner uid, mode
   0600 (consumer checks in the contract). Never read.
5. `gui.daemon-reused`: `daemon status` pid before == after the GUI start.
6. Tunnel: `ssh -N -L <record.port>:127.0.0.1:<record.port> <user>@<ssh-host>` (`::1` when host is `::1`).
7. Hold: `--gui-hold-minutes N` (default 0 = smoke only) keeps the script in the foreground, printing the
   tunnel and the handoff path; Ctrl-C or expiry ends the hold.
8. Stop: close stdin → expect exit 0 within 30 s, else SIGTERM to the GUI's process group, then SIGKILL.
9. `gui.daemon-survives-close`: daemon still running with the same pid after the GUI exited.

Identical on Linux and macOS (`--stdin-stop` is POSIX-only and refused elsewhere). No `systemd-run`, no
`launchctl`. A long-lived GUI after the script exits becomes an owner choice (Q1).

### 4.5 Rollback on both OSes

`rollback_after_failure` keeps its order (stop → install old → start → ready), using the control seam:
1. stop with the **live** CLI (now the candidate). If that CLI cannot produce a JSON object (rc ∉ {0,1},
   ImportError, timeout) → owner Q4: (recommended) raw stop of the unit recorded at baseline
   (`systemctl --user stop <unit>` / `launchctl bootout gui/<uid>/<label>`), rollback-only; or install the old
   wheel first and stop with the old CLI.
2. `wait_stopped` (4.1).
3. reinstall old wheel (unchanged `build_install_command`).
4. `status()` with the old CLI must be `attributed`.
5. `start()` with the old CLI, `wait_ready(old_version)` incl. custody.
6. any failure → exit 3 `ROLLBACK_FAILED` with the collected log tails.

---

## 5. Implementation plan (PRs, all testable on Linux with fakes)

Rules for every PR: branch `<type>/<slug>` off fresh `dev`; conventional commits; no new inline comments
(decision records in module docstrings / file-header blocks; move `base.py:125-126` into the docstring when
touched); each file in `scripts/` ≤ 1000 lines (`scripts/ci/check_structural_limits.py:38,50-51`; the 7-file
rule does not apply to `scripts/`); `tests/tooling/ops/` ≤ 7 direct files and ≤ 1000 lines each; run
`check_structural_limits.py`, `check_module_boundaries.py`, focused tests, full `pytest tests/`, `ruff check .`.
No `contract/` change unless G-C is fixed in product. PR body: Skill-Impact line, UNKNOWN for real-host items.

### PR-1 `fix/ops-upgrade-smoke-gui-home` (Linux, present-day bug, small)
- Red tests first (`tests/tooling/ops/test_dev_upgrade_smoke_gui.py`):
  1. `systemd-run` argv carries `--setenv=HYPRIAL_HOME=<home>` and `--setenv=HARNESS_STATE_DIR=<state>`.
  2. With a record `port: 51234` and `--gui-port 0`, the tunnel and the listener use 51234.
  3. A record with `backend_pid: 123` or `backend_owned: true` for the daemon driver → `gui.record` FAIL.
  4. GUI failure result is not reported as "rolled back" (semantics per Q2).
- Change: `apply.py:330-345,389-424`, `common.py:421-439`; README/`cli.py` exit text.
- Optional: raise `--stop-timeout` default to 180 and restart the untouched old service on a failed stop
  (1.3-1) with its own red test (fake runner where stop times out).
- Superseded by PR-3 later; worth landing alone because it prevents starting another home's daemon.

### PR-2 `refactor/ops-upgrade-smoke-service-seam` (both OSes in code, Linux-tested)
- New `service.py` (~300 lines), `process.py` (~120), `logs.py` (~100). `base.py`/`preflight.py`/`apply.py`
  call the seam; `parse_systemd_properties`/`parse_environment` move into the legacy control.
- Red tests first (`test_dev_upgrade_smoke_service.py`, fakes in `tests/tooling/ops/fakes.py` emulating
  `daemon service status/stop/start --json` for both `platform: systemd` and `platform: launchd`):
  - apply refuses (exit 2, no stop) when `installed:false` / `attributed:false` / running and
    `supervision != supervised`; `SERVICE_PLATFORM_UNSUPPORTED` → exit 4 BLOCKED.
  - stop goes through `hyprial daemon stop --json` (not `systemctl`) for both platforms; `supervisorMayRestart`
    → FAIL before install; waits until the fake pid is dead (zombie counts as dead; reused pid with another
    birth does not count as alive).
  - start env lacks `HYPRIAL_DAEMON_AUTOSTART`/`HYPRIAL_SERVICE_AUTOINSTALL`; `PATH` starts with `bin_dir`.
  - `alreadyRunning: true` after a stop → FAIL + rollback.
  - new CLI reports `attributed:false` after install → rollback without start.
  - bootstrap daemon status without `version` → readiness uses the start payload `version` (per G-C decision).
  - rollback with a candidate CLI that exits 127/non-JSON → behaviour per Q4.
  - baseline status without `supervision` on Linux → legacy systemctl path; on darwin → refuse (Q5).
  - log tails copied, bounded, URL/code redacted.
  - failed stop before mutation → old service restarted, result names it.
- Existing tests that change: `UpgradeRunner` (`test_dev_upgrade_smoke.py:180-226`) matches
  `["systemctl", ...]` and uses `"status" in command`, which now also matches `daemon service status`; rewrite
  it on the new fakes; `test_apply_rolls_back_to_the_recorded_old_wheel` asserts on the new commands.
  `test_apply_guard_rejects_a_wrong_host_before_any_command` unchanged. Split the 283-line file by topic.
- Summary schema: bump to `hyprial-dev-upgrade-smoke/v2` (`baseline.service` becomes the hyprial status
  object) per Q3.

### PR-3 `feat/ops-upgrade-smoke-portable-gui`
- New `gui.py` (~250 lines), `Runner.spawn()` seam in `common.py`; delete `systemd-run` code and the
  `BLOCKED` branch (`apply.py:319-441`).
- Red tests: argv has `--stdin-stop --no-open --json`; env has no `HYPRIAL_GUI_UPSTREAM_TOKEN`/`_HOME`/
  `HYPRIAL_BACKEND_ENDPOINT`; stdin close → exit 0 → `gui.daemon-survives-close` PASS; "exit 0, no line" →
  FAIL; record schema violations → FAIL; daemon pid changed by the GUI start → FAIL; handoff file never
  opened (patch `open`/`Path.read_text` on that path to raise).
- Docs in the same PR (Skill-Impact): `scripts/ops/README.md`, `cli.py` DESCRIPTION,
  `src/hyprial/skills/hyprial-ops/SKILL.md:262-263`, `docs/guides/gui-dev-mode.md:168-170`.

### PR-4 `feat/ops-upgrade-smoke-macos` (small)
- darwin specifics only: native-actor via `lsof`; cold-start temp root under `/tmp` with a short prefix on
  darwin plus an explicit socket-length check (BLOCKED with the computed length instead of a FAIL);
  README "macOS" section with the dedicated-home procedure (section 6). Tests monkeypatch `sys.platform`.

### PR-5 `docs/ops-upgrade-smoke-mac-evidence` (after a real Mac run)
- WORM evidence under `docs/notes/` (append only), README status line PASS/FAIL/BLOCKED per check; register
  anything deferred in `docs/reference/known-problems.md`.

Order: PR-1 → PR-2 → PR-3 → PR-4 → Mac run → PR-5. PR-2 and PR-3 touch `apply.py` both; land sequentially.

---

## 6. What only a real Mac can verify

Setup (Mac engineer; all in one Terminal of a **logged-in GUI session** — LaunchAgents in `gui/<uid>` are
not reachable from an SSH session without a console login; record which one was used):

```sh
set -eu
export T="$HOME/hsm"                        # short, inside $HOME (outside-home is refused; AF_UNIX ≤ 103 chars)
export HYPRIAL_HOME="$T/home" HARNESS_STATE_DIR="$T/home/state"
export UV_TOOL_DIR="$T/tools" UV_TOOL_BIN_DIR="$T/bin" PATH="$T/bin:$PATH"
test "$HYPRIAL_HOME" != "$HOME/.hyprial"
mkdir -p "$T" && chmod 700 "$T"
mkdir -p "$HYPRIAL_HOME" && chmod 700 "$HYPRIAL_HOME"
printf 'mac-smoke-drill\n' > "$HYPRIAL_HOME/node-id" && chmod 600 "$HYPRIAL_HOME/node-id"
launchctl print "gui/$(id -u)/com.hyprial.hyprial.daemon" 2>&1 | grep -E '^\s*pid' || true   # read-only: default home's pid, record it
uv tool install --offline --no-index --find-links <wheelhouse> <old.whl>
"$T/bin/hyprial" daemon start --json        # bootstrap daemon; installs com.hyprial.hyprial.daemon.<id>
"$T/bin/hyprial" daemon service status --json
```

Ranked checks:
1. `daemon service status --json`: `platform: launchd`, `unit: com.hyprial.hyprial.daemon.<12hex>` (never the
   bare default label), `attributed: true`, `supervision: supervised`, `pid == servingPid`.
2. `"$T/bin/hyprial" daemon stop --json` → then for 30 s: `launchctl print gui/$(id -u)/<label>` exits 113,
   `ps -p <pid>` empty, `daemon service status --json` `loaded:false running:false pid:null`
   (KeepAlive does not relaunch while booted out).
3. While booted out: `uv tool install --force --reinstall --offline --no-index --find-links <wh> <new.whl>`;
   `"$T/bin/hyprial" daemon service status --json` from the **new** CLI still `attributed: true`.
4. `"$T/bin/hyprial" daemon start --json` → `started: true`, `version` = new; status `supervised`.
5. The script end to end, preflight then apply, against the dedicated roots:
   `uv run --no-project --python 3.12 python scripts/ops/dev_upgrade_smoke.py preflight --new-wheel <new.whl>
   --old-wheel <old.whl> --wheelhouse <wh> --tool-dir "$T/tools/hyprial" --bin-dir "$T/bin"
   --home "$HYPRIAL_HOME" --state-dir "$HARNESS_STATE_DIR" --expect-host "$(hostname)" --artifacts-dir "$T/ev1"`,
   then the same with `apply … --yes --artifacts-dir "$T/ev2"`.
6. Forced rollback on real launchd: `apply … --ready-timeout 1 --yes --artifacts-dir "$T/ev3"` → exit 1,
   `ROLLED_BACK`, old version serving, `supervised`.
7. Native actor: `lsof -nP -p <daemon pid> -Fn | grep '/hyprial/_core/'` non-empty; script
   `--require-native-actor` PASS.
8. GUI (after PR-3): `apply … --gui --gui-hold-minutes 5 --ssh-host <mac>`; from another machine run the
   printed tunnel; open the handoff URL only on the owner's machine; after the hold, daemon pid unchanged.
9. Cold start: `preflight … --cold-start-timeout 30` → BLOCKED (device authorization), not a socket-length FAIL.
10. Over SSH without a console login: record whether `daemon service status --json` / `daemon start` fail
    (expected `SERVICE_MANAGER_FAILED`) and that the script then refuses before mutation.
11. Default home untouched: the pid from the first `launchctl print …com.hyprial.hyprial.daemon` is unchanged;
    `ls ~/Library/LaunchAgents` shows only the added `com.hyprial.hyprial.daemon.<id>.plist`.

Cleanup: `"$T/bin/hyprial" daemon service uninstall --yes --json` (same env), `"$T/bin/hyprial" daemon stop --json`
if anything answers, then `rm -rf "$T"`.

Never, on any real host:
- run anything with `HYPRIAL_HOME` unset or equal to `~/.hyprial`; touch label `com.hyprial.hyprial.daemon`
  or `hyprial.service` (no `bootout`, `kickstart`, `systemctl stop`) — read-only `print`/`status` only;
- point `UV_TOOL_DIR`/`--tool-dir` at the uv root that hosts the engineer's real hyprial;
- `hyprial login`, `logout`, `init`, `doctor --fix`, `daemon upgrade`, or anything with credentials, Lark apps or
  a production daemon; paste a handoff URL into logs/tickets/chat;
- set `HOME` to a fake directory (service commands refuse: `service-account-home-mismatch`, and it would
  confuse the uid domain);
- run the script's `apply` on the production power-station host as a "Mac test".

---

## 7. Open questions for the owner (with recommendation)

| # | Question | Recommendation |
|---|---|---|
| Q1 | Keep `--gui`? (a) bounded GUI smoke via `--stdin-stop`, alive only while the script holds; (b) drop `--gui`, document manual `hyprial gui --no-open --json`; (c) long-lived detached GUI with a printed stop command | (a), with `--gui-hold-minutes` |
| Q2 | GUI failure after a healthy daemon upgrade: roll back, or keep the upgrade with a distinct result/exit code | roll back: `--gui` is opt-in, so it is a required check; README exit 1 stays true |
| Q3 | Published surface changes: `--service` becomes derived (refuse on mismatch, deprecate); summary schema v2 (`baseline.service` = hyprial status object); `--stop-timeout` default 60→180; new `--gui-hold-minutes` | accept all; keep `--service` accepted for one release |
| Q4 | Rollback when the candidate CLI cannot run: raw `systemctl --user stop`/`launchctl bootout` of the unit recorded at baseline (script owns two manager commands, rollback-only) vs install-old-first then stop with the old CLI | raw stop, rollback-only, unit name taken from the baseline `daemon service status --json`; starts always through hyprial |
| Q5 | Baselines older than #2830 (no `supervision` in status): keep the legacy systemctl path on Linux or refuse | keep on Linux until the test hosts run ≥ #2830, then delete; refuse on macOS |
| Q6 | Homes without an attributable unit: refuse, or support with `HYPRIAL_SERVICE_AUTOINSTALL=0` (documented as test-only, "product code never sets") | refuse (exit 2); and refuse when the caller's env sets `HYPRIAL_DAEMON_AUTOSTART=0` |
| Q7 | Bootstrap `daemon status --json` has no `version`: add it (additive JSON field → `contract/` sync or Contract-Impact) or let the script read `version` from `daemon start --json` | short term script-side; product additive field as a separate owner-approved PR |
| Q8 | New CLI surface (`daemon stop --wait-exit`, `daemon start --no-autoinstall`) | not needed now; script polls |
| Q9 | Grant TTL override 60 min (`--gui-ttl-minutes`) vs the documented 60 s credential | drop the override (product default) or cap at 10 min |

## 8. Unverified (UNKNOWN until measured)

All runtime claims: launchd bootout/bootstrap timings, attribution across a uv reinstall on macOS, `lsof`
visibility of the native library, socket-path length under `$TMPDIR`, SSH-session domain behaviour, and the
present-day Linux GUI defect (derived from `systemd-run` semantics and code, not reproduced).
