# scripts/ops — operational instruments

## `dev_upgrade_smoke.py`

A small, host-neutral instrument for the five-step test-host upgrade flow:

1. record the installed wheel, CLI version and the state of the home's
   per-home OS service (`hyprial daemon service status --json`);
2. install the candidate into a throwaway uv tool root and exercise its CLI;
3. stop the daemon with `hyprial daemon stop --json` and prove it is gone;
4. install the candidate into the live uv tool root and start the daemon with
   `hyprial daemon start --json` against the existing home;
5. reinstall the recorded old wheel and start it again if any step fails.

`preflight` is read-only apart from a throwaway temp root. `apply` mutates the
live tool environment and stops the daemon, so it requires `--expect-host` to
match the current host and `--yes` in non-interactive runs.

### Service control (Linux and macOS)

The script never calls `systemctl` or `launchctl` for the daemon in the normal
path. Every service action is a command of the live CLI (`<bin-dir>/hyprial`,
by absolute path) with one environment: `HYPRIAL_HOME` and `HARNESS_STATE_DIR`
of the target home, `PATH` with `--bin-dir` first, and without
`HYPRIAL_DAEMON_AUTOSTART`, `HYPRIAL_SERVICE_AUTOINSTALL`, `HARNESS_SOCKET_PATH`,
`HYPRIAL_OWNER` and `HYPRIAL_NODE_ID`. The units hyprial installs set none of
these, so the supervised daemon takes its socket, owner and node from the home
(the node from `<home>/node-id`); a `HYPRIAL_NODE_ID` left in your shell would
make the CLI (and `hyprial gui`, which then refuses with `GUI_DAEMON_IDENTITY`)
disagree with it. `droppedEnvironment` in `summary.json` names each of these
variables that was set in your environment (never its value). The home is `--home`, else `HYPRIAL_HOME`, else
`~/.hyprial` (state: `--state-dir`, else `HARNESS_STATE_DIR`, else
`<home>/state`); hyprial derives the unit from it (`hyprial.service` /
`hyprial-<id>.service`, `com.hyprial.hyprial.daemon[.<id>]`).

Refused before anything stops (exit `2`, result `ABORTED`):

- the home has no installed unit (`service.installed`), or one this
  installation cannot attribute (`service.attributed`,
  `SERVICE_OWNERSHIP_REFUSED` with its `ownershipReason`). The script never
  installs a unit: run `hyprial daemon service install` yourself first;
- a daemon answers but its unit does not supervise it (`service.supervised`;
  checked again just before the stop as `apply.pre-stop`);
- `--service` is given and differs from the unit hyprial derives
  (`service.unit`). `--service` is deprecated and only compared;
- `HYPRIAL_DAEMON_AUTOSTART=0` is set in your environment (`apply.guard` /
  `env.autostart`): `hyprial daemon start` would then bypass the unit.

`SERVICE_PLATFORM_UNSUPPORTED` is `BLOCKED`, exit `4`. Any other service status
error or an answer without JSON fails `baseline.service`, exit `1`.

The stop is `hyprial daemon stop --json` (runner budget at least 180 s). It
fails at once when the answer reports `supervisorMayRestart` or does not name
this home's supervisor (the stop then went over IPC and the manager may start
the daemon again). Otherwise it is proven within `--stop-timeout`: the service
status shows no running unit and no pid, `daemon status` shows no daemon, and
the pid seen before the stop is gone (`ps -o stat=,lstart= -p <pid>`; a zombie
or a reused pid with another start time counts as gone).

The start is `hyprial daemon start --json`; `alreadyRunning: true` after the
stop fails (something restarted the daemon while it was held down). The start
command's own window (30 s without startup progress, 120 s at most) is not the
script's readiness limit; `--ready-timeout` is, counted from the start command.
A start answered `DAEMON_START_TIMEOUT` while the service status shows the unit
running with a pid (during startup `servingPid` is null and `supervision` is
`lost`) passes `apply-start` / `rollback-start` with the answer in the check
data, and the script keeps waiting for readiness; with nothing running it fails
at once. Every other error answer fails the start. The readiness wait never
sleeps past `--ready-timeout` and the failure says how long it really waited. After the
candidate install, the candidate CLI must still attribute the unit
(`service.attributed-after-install`), else the rollback runs without starting
the candidate. Readiness also requires custody: `supervision` `supervised` with
`pid` and `servingPid` equal to the daemon's pid; `post.service` checks the
same. A daemon in the bootstrap phase has no `version` in `daemon status`; the
version of the `daemon start` answer is used when it names the same pid
(`daemon.ready` data `versionSource`).

Legacy baseline: when the installed wheel's `daemon service status` has no
`supervision`/`attributed` (wheels older than 2026-10-20), Linux keeps the former
`systemctl --user` path (`service.control` says so, `toolchain.systemctl` and
the unit `Environment=` cross-checks `baseline.unit-home` / `baseline.unit-state`
apply, the unit is `--service` or `hyprial.service`); macOS refuses (exit `2`).

The service logs of the upgrade window (`<state>/daemon-service.stdout.log`,
`daemon-service.stderr.log`, `daemon-launch.log`, `daemon-start-failures.json`)
are copied after each stop/start and at the end into
`<artifacts>/logs/service-*.log`: only what was appended since the stop began,
at most 256 KiB per copy, device-authorization URLs and codes redacted, the
home only read.

`summary.json` schema is `hyprial-dev-upgrade-smoke/v2`: `service` is the
derived unit, `serviceControl` is `hyprial` or `legacy-systemd`,
`baseline.service` is the `daemon service status --json` object (the systemctl
properties on the legacy path), `serviceLogs` lists the copied log windows,
`droppedEnvironment` names the caller variables kept away from the CLI.

The interpreter for `uv tool install` is `--python`, else the `python` of the
tool's `uv-receipt.toml` when that is an existing absolute path, else
`<tool-dir>/bin/python` (a receipt written by `uv tool install --python 3.12`
holds the request string `3.12`). `paths.resolve` names what it could not find.

### Preflight

```sh
uv run --no-project --python 3.12 python scripts/ops/dev_upgrade_smoke.py \
  preflight \
  --new-wheel <candidate.whl> \
  --wheelhouse <offline-wheelhouse> \
  --expect-host <test-host> \
  --expect-commit <expected-source-sha> \
  --artifacts-dir <evidence-dir>
```

The old wheel is read from the installed uv receipt (`uv-receipt.toml`) or
`direct_url.json`; pass `--old-wheel` when the receipt no longer points at the
retained rollback artifact. The command writes `summary.json`, per-command logs
and `commands.json` under `--artifacts-dir`.

`--cold-start-timeout <seconds>` exercises daemon-first bootstrap in a throwaway
`<cold-home>` seeded with `node-id` and `HYPRIAL_NETWORK_ISOLATED=1`. It records
`PASS` when the bootstrap daemon answers `running: true`, or `BLOCKED` for older
device-auth builds and over-limit socket paths. Auth URLs and one-time codes are
redacted from the log.

### Apply

```sh
uv run --no-project --python 3.12 python scripts/ops/dev_upgrade_smoke.py \
  apply \
  --new-wheel <candidate.whl> \
  --wheelhouse <offline-wheelhouse> \
  --old-wheel <retained-old.whl> \
  --expect-host <test-host> \
  --home <production-home> \
  --state-dir <production-state> \
  --expect-routine <routine-name> \
  --require-native-actor \
  --require-pac-schema \
  --yes \
  --artifacts-dir <evidence-dir>
```

`apply` first runs the same preflight. It refuses before stopping anything if
the old wheel is missing, the host is wrong, the installed origin is not the
recorded wheel, or the home fails one of the service rules above. After the
new install it waits up to `--ready-timeout` seconds for
`hyprial daemon status --json` to report the expected version, a positive PID,
no crash, and custody. Any failed required check triggers the rollback: stop
with the live CLI, reinstall the old wheel, require the old CLI to attribute
the unit (`rollback.attributed`), start, wait ready. Before a candidate start
attempt, when the candidate CLI cannot answer with JSON at all or shifts
units, the rollback stops the unit recorded at baseline directly
(`systemctl --user stop <unit>` or `launchctl bootout gui/<uid>/<label>`, check
`rollback.raw-stop`); this is the only place the script calls a service manager
itself, and starts always go through hyprial. Once candidate start has been
attempted, if the candidate CLI loses JSON service status/stop or shifts units,
rollback refuses to call `rollback.raw-stop` or reinstall over an unproven
unit and exits `3` (`ROLLBACK_FAILED`) for manual cleanup. A failed rollback
exits 3 and must be handled manually.

`--stop-timeout` defaults to 180 s, longer than the unit's own stop timeout.
If the stop still does not complete, nothing has been installed: the script
waits up to 60 s for the stop to end (a pending stop would kill a daemon that
was started on top of it), starts the old daemon (or finds it still running
under its unit), waits for it to be ready and exits `1` (`result`
`FAILED_STOP_TIMEOUT_RESTORED`), or exits `3`
(`FAILED_STOP_TIMEOUT_RESTORE_FAILED`) when the stop outcome stays unknown or
that restart fails. On the legacy path the restart is issued at once (systemd
queues it behind the pending stop).

`--gui` is a required check once requested. After the daemon passes its checks
the script starts `hyprial gui --no-open --json --port <gui-port> --stdin-stop`
of the live CLI as its own child process (Linux and macOS alike; no
`systemd-run`, no transient unit, no service manager). The child gets the same
environment as every other live CLI command, with the same `HYPRIAL_HOME` and
`HARNESS_STATE_DIR` (`hyprial gui` ensures the daemon of the home it resolves),
minus every `HYPRIAL_GUI_*` variable, `HYPRIAL_BACKEND_ENDPOINT` and
`HYPRIAL_DEV_GUI_HOME` from your shell. The GUI never outlives the script: it
stops when its standard input closes, which also happens if the script dies.
The script then:

- reads the target daemon (pid, socket, the unit's pid) before the start,
  retrying a failed `daemon status` read up to 3 times;
- waits up to 120 s for the first stdout line, the startup record. The GUI's
  stderr goes to `<artifacts>/gui-<epoch>.err.log`; the run refuses to start
  (`gui.start` FAIL) if that file already exists. A GUI that exits before the
  record fails `gui.start` (exit 0 with no line included: nothing asked it to
  stop);
- validates the startup record (the required keys of
  `contract/cli/gui-startup-record.v1.json`, `record_version` 1, `ok`,
  `backend_driver` `daemon`, `backend_pid` null, `backend_owned` false, a
  loopback `host` (`127.0.0.1` or `::1`) that is the host of `url`, a valid
  `port` that is the port of `url` and, unless `--gui-port 0`, equals
  `--gui-port`); a record that fails is a failed check;
- checks the listener on the record's `host`/`port`, so `--gui-port 0` works;
- requires the target daemon to be unchanged by the GUI start. A daemon started
  for another home cannot be observed without reading that home, which the
  script never does, so this is the strongest check available;
- `lstat`s the handoff file only: it must be a regular file owned by you with
  mode `0600`; its one-time grant is never read;
- stops the GUI by closing its stdin and expects exit 0 within 30 s
  (`gui.stop`); otherwise it sends SIGTERM, then SIGKILL, to the GUI's own
  process group, and `gui.stop` fails. Then it confirms the daemon is still
  running with the same pid (closing the GUI never stops the daemon).

A GUI that already runs for the same home holds its runtime: a second GUI
answers `GUI_RUNTIME_BUSY` ("another foreground GUI already owns this
runtime"); `--gui-port 0` does not avoid it (one foreground GUI per home). The
product has no read-only query for this, so with `--gui` the script asks the
product itself **before anything is stopped**: right after the pre-stop
re-check it starts the installed CLI's
`hyprial gui --no-open --json --port 0 --stdin-stop` against the target home
(same child mechanism and environment as the GUI check, product-default grant),
reads its first stdout line and stops it at once (stdin close, then SIGTERM,
then SIGKILL). **This probe is not read-only**: a GUI host runs for a moment and
the product writes a one-time grant file (never read by the script). Its stderr
goes to `<artifacts>/gui-probe-<epoch>.err.log`; if that file exists the run is
refused. Outcomes:

- `GUI_RUNTIME_BUSY` → `gui.runtime-free` FAIL naming the home and the runtime
  directory; refused with exit `2`, `result` `ABORTED`: nothing stopped,
  nothing installed, no rollback. Stop that GUI, or run without `--gui`;
- a valid startup record → `gui.runtime-free` PASS, the upgrade proceeds;
- the probe GUI still alive after SIGKILL (`gui.probe-stop`, naming the
  `kill -KILL -- -<pid>` command) or a target daemon changed across the probe
  (`gui.probe-daemon-identity`) → refused the same way, exit `2`;
- anything else (an installed CLI without `--stdin-stop`, no record within
  120 s, another failure code, a record that fails validation, no running
  target daemon, a probe that cannot start) → `gui.runtime-free` `NOT_RUN` with
  the reason; the upgrade proceeds.

A GUI that appears between the probe and `gui.start` is still caught there
(`gui.start` FAIL naming the home and runtime) and the upgrade is rolled back.
`preflight` does not probe: it stays read-only and has no `--gui`.

| Check | When | FAIL means |
|---|---|---|
| `gui.runtime-free` | before the stop | `GUI_RUNTIME_BUSY`: refused, exit 2 (`NOT_RUN` when the probe gives no answer) |
| `gui.probe-stop` | before the stop | probe GUI alive after SIGKILL: refused, exit 2 |
| `gui.probe-daemon-identity` | before the stop | target daemon changed by the probe: refused, exit 2 |
| `gui.daemon-identity` | after the upgrade | target daemon missing before, or changed by, the GUI start: rollback |
| `gui.start` | after the upgrade | GUI did not start or wrote a failure line: rollback |
| `gui.record` | after the upgrade | startup record rejected: rollback |
| `gui.listener` | after the upgrade | record host/port does not accept connections: rollback |
| `gui.handoff-mode` | after the upgrade | handoff file not a regular 0600 file of yours: rollback |
| `gui.grant` | after the upgrade | never fails (records that the grant was not read) |
| `gui.tunnel` | after the upgrade | never fails (`NOT_RUN` without a hold or `--ssh-host`) |
| `gui.hold` | after the upgrade | GUI exited by itself during the hold: rollback |
| `gui.stop` | after the upgrade | GUI needed a signal or exited non-zero: rollback; alive after SIGKILL: exit 3 |
| `gui.daemon-survives-close` | after the upgrade | daemon changed after the GUI closed: rollback |

By default the GUI therefore closes as soon as the check ends, and `gui.tunnel`
is `NOT_RUN` (informational). `--gui-hold-minutes N` (0 to 60, default 0) keeps
the GUI running for N minutes after all GUI checks passed so a person can open
it: the script prints the SSH tunnel command (with `--ssh-host`) and says that
the GUI closes when the hold ends, waits, stops the GUI, then runs
`gui.daemon-survives-close`. `gui.tunnel` is `PASS` only when a hold keeps the GUI
open (with `--ssh-host`). Ctrl-C during the hold stops the GUI and ends the hold
normally; the upgrade is kept. A GUI that exits by itself during the hold fails
`gui.hold`. `--gui-hold-minutes` needs `--gui`.

The login grant (`HYPRIAL_GUI_LAUNCH_TTL_MS`) belongs to the script. Without a
hold it is not set: the product default (60 s) applies, since nobody opens the
GUI. With a hold it is the hold, capped at 10 minutes and at least 60 s
(`gui.launchTtlMs` in the summary); open the GUI within that time.
`--gui-ttl-minutes` no longer exists.

A failed GUI check rolls the upgrade back like any other failed check (exit `1`,
`result` `ROLLED_BACK`; `rollback.reason` names the first failing `gui.*` check).
If the GUI is still alive after SIGKILL the upgrade is still rolled back first,
then the run exits `3` with `result` `ROLLED_BACK_GUI_LEFT` (or `ROLLBACK_FAILED`
when the rollback itself also failed); `rollback.guiPid`,
`rollback.guiStopCommand` and the `gui.stop` / `rollback.manual` checks name the
process group and the exact `kill -KILL -- -<pid>` to run. The summary's `gui`
object has `command`, `pid`, `stderrLog`, `launchTtlMs`, `startup`, `handoff`,
`exitCode` and `stopMethod`. Do not add a reverse proxy or disable host-key
checking for tunnel access.

Earlier versions of this script ran the GUI in a transient user unit
(`hyprial-gui-smoke-<epoch>`) and did not always stop it; such a unit can still
hold the GUI port or the runtime: `systemctl --user list-units
'hyprial-gui-smoke-*'`, then `systemctl --user stop <unit>` for each one you
find.

### Optional observations

- `--expect-routine <name>` (repeatable) requires the routine to exist and be
  enabled; in-flight work is recorded but is not a failure.
- `--require-pac-schema` reads `pac-graph.sqlite3` in read-only mode and
  requires a positive `PRAGMA user_version`.
- `--require-native-actor` checks that the installed wheel carries
  `hyprial/_core/*.so` and that it is mapped in the daemon process: via
  `/proc/<pid>/maps` on Linux and `lsof -nP -p <pid> -Fn` on macOS (missing
  `lsof` or permission/SIP restrictions record `BLOCKED` without rolling back).

The instrument deliberately does not run `hyprial doctor --fix`, install or
edit a unit, or change the production home. Exit codes: `0` pass (optional
`BLOCKED` checks may remain), `1` failed (a failed `--gui` check included) with
the daemon restored on the old wheel, `2` guard/usage/service refusal before
mutation, `3` rollback failed, the stop outcome stayed unknown, or the GUI was
still alive after SIGKILL, `4` interrupted/blocked (including
`SERVICE_PLATFORM_UNSUPPORTED`).

### macOS

On macOS (`darwin`), `dev_upgrade_smoke.py` uses the same `hyprial` service
control seam (`launchd`) and adds two darwin-specific behaviors:

- **Native-actor loaded check (`lsof`)**: `--require-native-actor` inspects the
  daemon process with `lsof -nP -p <pid> -Fn`, matching `n`-lines (exact or
  `os.path.realpath`, so `/var` -> `/private/var` resolves cleanly) against the
  installed `hyprial/_core/*.so` files. Missing `lsof`, timeouts, non-zero
  exits without a match, or outputs lacking `p<pid>` / `n`-lines record
  `post.native-actor.loaded` as `BLOCKED` without triggering rollback; a
  successful `lsof` run with mapped paths that omits the library records `FAIL`
  and rolls back.
- **Short isolated cold-start root & `AF_UNIX` guard**: `isolated_preflight`
  creates `/tmp/hsm-*` instead of using `$TMPDIR` (`/var/folders/...`) so
  `daemon.sock` stays within macOS's 103-byte `AF_UNIX` `sun_path` budget.
  Before creating `cold-start.log` or spawning `daemon run --bootstrap`,
  `cold_start` checks the UTF-8 byte lengths of both `state / "daemon.sock"` and
  `state.resolve() / "daemon.sock"`; if `max(...) > 103` (107 on Linux), it
  records `isolated.cold-start` as `BLOCKED` with `socketPathBytes` and
  `socketPathLimit`. Otherwise it seeds a throwaway `node-id` and sets
  `HYPRIAL_NETWORK_ISOLATED=1` for `daemon run --bootstrap`.
- **Console session requirement**: LaunchAgents under `gui/<uid>` require a
  logged-in GUI console session; an SSH session without a console login fails
  `SERVICE_MANAGER_FAILED` before any mutation.

#### Dedicated-home verification on macOS

Run inside a Terminal of a logged-in GUI session using a short root under
`$HOME` (`AF_UNIX` <= 103 bytes, inside `$HOME` so `service-account-home-mismatch`
and `hyprial-home-outside-operating-home` do not reject it):

```sh
set -eu
export T="$HOME/hsm"
export HYPRIAL_HOME="$T/home" HARNESS_STATE_DIR="$T/home/state"
export UV_TOOL_DIR="$T/tools" UV_TOOL_BIN_DIR="$T/bin" PATH="$T/bin:$PATH"
test "$HYPRIAL_HOME" != "$HOME/.hyprial"
mkdir -p "$T" && chmod 700 "$T"
mkdir -p "$HYPRIAL_HOME" && chmod 700 "$HYPRIAL_HOME"
printf 'mac-smoke-drill\n' > "$HYPRIAL_HOME/node-id" && chmod 600 "$HYPRIAL_HOME/node-id"
launchctl print "gui/$(id -u)/com.hyprial.hyprial.daemon" 2>&1 | grep -E '^\s*pid' || true
uv tool install --offline --no-index --find-links <wheelhouse> <old.whl>
"$T/bin/hyprial" daemon start --json
uv run --no-project --python 3.12 python scripts/ops/dev_upgrade_smoke.py \
  preflight \
  --new-wheel <new.whl> --old-wheel <old.whl> --wheelhouse <wheelhouse> \
  --tool-dir "$T/tools/hyprial" --bin-dir "$T/bin" \
  --home "$HYPRIAL_HOME" --state-dir "$HARNESS_STATE_DIR" \
  --expect-host "$(hostname)" --cold-start-timeout 30 --artifacts-dir "$T/ev1"
uv run --no-project --python 3.12 python scripts/ops/dev_upgrade_smoke.py \
  apply \
  --new-wheel <new.whl> --old-wheel <old.whl> --wheelhouse <wheelhouse> \
  --tool-dir "$T/tools/hyprial" --bin-dir "$T/bin" \
  --home "$HYPRIAL_HOME" --state-dir "$HARNESS_STATE_DIR" \
  --expect-host "$(hostname)" --require-native-actor --yes --artifacts-dir "$T/ev2"
```

Cleanup after verification:

```sh
"$T/bin/hyprial" daemon service uninstall --yes --json
"$T/bin/hyprial" daemon stop --json || true
rm -rf "$T"
```

Red lines on any real Mac host: never run with `HYPRIAL_HOME` unset or pointing
at `~/.hyprial`, never touch `com.hyprial.hyprial.daemon` (read-only `launchctl
print` only), never point `UV_TOOL_DIR`/`--tool-dir` at the user's real hyprial
installation, never fake `HOME` (triggers `service-account-home-mismatch`), and
never run `login`, `logout`, `init`, `doctor --fix`, or `daemon upgrade` on the
test home.

#### macOS verification status

Real macOS 27.0.1 ARM64 dedicated-home drill results are recorded in
[docs/notes/ops-upgrade-smoke-macos-evidence-2026-11-22.md](../../docs/notes/ops-upgrade-smoke-macos-evidence-2026-11-22.md):

| Check / Scenario | Status | Evidence |
|---|---|---|
| Dedicated-home `launchd` attribution, KeepAlive non-relaunch (`daemon stop`), and `apply --yes` | `PASS` | 2026-11-21 PR-3 (`#3051` comment `39184`) & 2026-11-22 PR-4 (`#3083`) Run 1/Run 2 |
| `preflight --cold-start-timeout` without `--python` (F2 fallback & seeded throwaway `node-id` under `/tmp/hsm-*`) | `PASS` | 2026-11-22 PR-4 Run 2 (`RESULT PASS`, `9 s`, zero `/tmp/hsm-*` leftover) |
| `apply --require-native-actor --gui --gui-port 0 --yes` (`lsof` native actor check + `--stdin-stop` GUI child + `0600` handoff + daemon custody) | `PASS` | 2026-11-22 PR-4 Run 1 D3 (`RESULT PASS`, `30 s`, `lsof` corroborated) |
| Pre-stop `GUI_RUNTIME_BUSY` refusal (`apply --gui` while another GUI owns the runtime) | `PASS (ABORTED)` | 2026-11-21 PR-3 (`#3051` comment `39184`, exit `2`, `7.1 s`, zero stop/install) |
| Forced rollback + `post.routines` non-zero exit (`--expect-routine nonexistent-smoke-routine`, F3 fix) | `PASS (ROLLED_BACK)` | 2026-11-22 PR-4 Run 1 D4 (exit `1`, `33 s`, `PAC_OWNER_UNKNOWN` -> prior wheel restored & `supervised`) |
| `hyprial gui --stdin-stop` killed via `SIGHUP` leaving frontend child in orphan session (F4; stdin close cleans up) | `DEFERRED (KP-120)` | Registered in `docs/reference/known-problems.md` (`KP-120`) |
| GUI hold + SSH tunnel (`--gui-hold-minutes N --ssh-host`) | `NOT_RUN` | No hold run on real macOS (`gui.tunnel` `NOT_RUN` in D3) |
| Headless SSH session without console login (`SERVICE_MANAGER_FAILED` pre-mutation refusal) | `NOT_RUN` | All real drills ran inside a logged-in Aqua `gui/<uid>` session |
| Owned / authenticated macOS home upgrade (`--expect-routine` passing, `--require-pac-schema`) | `UNKNOWN` | Only dedicated ownerless bootstrap homes have been drilled on macOS |
| `rollback.raw-stop` (exit `3`), stop-timeout / `DAEMON_START_TIMEOUT` recovery, pre-mkdir `socketPathLimit` `BLOCKED` guard (`708df4c438`), `lsof` `timed_out=True` / `rc=127` / SIP `BLOCKED` guards (`bf39b0648a`) | `UNKNOWN` (unit-tested only) | Verified in unit tests (`222` ops + `287` focused suite); not re-triggered live on macOS |
