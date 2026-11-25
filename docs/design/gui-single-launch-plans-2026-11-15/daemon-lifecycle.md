# Decision memo: who starts the daemon, and how `hyprial gui` relates to it

- Date: 2026-11-15
- Tree read: the 6pre worktree (`dev` @ `9932b2b63` plus an unrelated small deletion), package version `0.5.5.4`. Read-only.
- Status: investigation and plan only. No repository file was edited. Nothing was installed into launchd/systemd.
- Evidence marks: **M** = measured in this investigation, **R** = read from code/docs, **U** = unknown.
- Paths are relative to the repository root unless absolute. `…/cli/commands/` = `src/hyprial/shell/impl/cli/commands/`.

## 0. Summary

1. The hypothesis is **confirmed by measurement**: after first-run initialization through the GUI backend, the daemon is the `request_http` child itself (`daemon.json` pid == child pid), no service unit is written, and when that child stops the socket disappears. The next admission for the same home is refused with `GUI_DAEMON_UNAVAILABLE`. The real `hyprial gui --no-open --json` on an initialized home without a daemon returns that code, exit 1.
2. The terminal path already implements direction (Y): `hyprial login`/`init`/`daemon start` all use one launch primitive that starts `daemon run` as a detached process in its own session (measured: PPID 1, own SID, survives its launcher and survives the GUI backend child), and routes through launchd/systemd instead when a service unit is installed. `init` installs that unit by default on macOS/Linux.
3. Attaching the GUI to a daemon it does not own already works, including logout against it (measured: the daemon stops itself, the GUI child reopens bootstrap in 1.0 s). The only two missing pieces are: (a) nobody starts the daemon in state 3, and (b) after GUI initialization the daemon stays embedded in the GUI child.
4. `nativeHost.promotion=external` / `serve_external` is **not** an "attach to an externally owned daemon" mechanism. It is the same in-process promotion with a different foreground owner. It has no producer left in the tree and is not reusable for (Y).
5. The OS service cannot be used by an app with its own home today: `ServiceManager` refuses any home other than `$HOME/.hyprial`, and the launchd label and systemd unit name are single global constants.
6. Recommendation: **Y, staged** — the daemon is always its own process, started by the one existing launch primitive; `hyprial gui` only ensures it (state 3) and hands over to it after initialization (state 1). Detached process first (works on every POSIX platform, no new OS integration), per-home OS service second (reboot/crash survival), login-item API (Y3) not adopted.

## 1. How the measurements were made

| Item | Detail |
|---|---|
| Interpreter | `.venv/bin/{hyprial,python}` of the 6pre worktree, `PYTHONDONTWRITEBYTECODE=1`, cwd outside the worktree |
| Isolation | fresh `/tmp/dlc.*` and `/tmp/dlp.*` roots (short paths because of the AF_UNIX path limit); `HOME`, all `XDG_*`, `HYPRIAL_HOME`, `HARNESS_STATE_DIR` inside the root; `HYPRIAL_NETWORK_ISOLATED=1`, empty Zenoh endpoints, `HYPRIAL_PEER_DISCOVERY=0`, explicit `HYPRIAL_NODE_ID`; `HYPRIAL_HOME` was never `$HOME/.hyprial`, and `init` / `daemon service install` were never run |
| Initialized home | `settings.json` `{"version":1,"owner":"dl-owner"}` (the GUI forbids `HYPRIAL_OWNER`, `admission.py:106-110`) |
| GUI backend | the real `python -m hyprial.shell.impl.request_http serve --config …` child with the same `local-gui` document `write_request_http_config` emits (`gui/backend/source.py:229-281`), and the real `admit_source()` |
| Real CLI | `hyprial gui --no-open --json --port 47931` was run for state 3 only. For states 1 and 2 the launcher would continue into `npm run build` inside `6pre/gui` (no `node_modules` there, and writing into that worktree is forbidden), so the Node/browser half was **not** exercised: launcher-after-admission is **U** in this run |
| First-run promotion | driven with `docs/notes/gui-single-launch-prototypes-2026-11-15/daemon-lifecycle-prototype/measure_promotion_relaunch.py`, which reuses the child entry of `tests/integration/request_http/onboarding/test_gui_live_lifecycle.py:149-190`. Real HTTP, real locks, real `DaemonApplication`; **identity commit and device provisioning are controlled ports** (no real account). Real OIDC login: **BLOCKED** (needs an account and network) |
| Cleanup | every process stopped (`pgrep` empty), both temp roots removed, `git status` of `6pre` unchanged by this work |

## 2. Facts

### 2.1 Daemon lifecycle today, terminal path

| # | Fact | Mark | Evidence |
|---|---|---|---|
| T1 | Commands: `login`, `logout`, `init` (+ `init onboarding …`), `daemon status|start|run|stop|restart`, `daemon service status|install|uninstall`, `daemon upgrade status|apply|run`, `doctor`, `gui`. There is no `up`. | M, R | `hyprial --help`, `hyprial daemon --help`; `docs/reference/cli-command-inventory.json` |
| T2 | `init`: create home → org-context slots → probe daemon (0.5 s) → if no owner run the login flow in-process → `resolve_node_owner()` → launch the daemon → then install the per-user service. | R | `…/init/init.py:95-216, 302-327` |
| T3 | Service install is the **default** in `init` (`requested = True if service is None else service`); failure to install is non-fatal; unsupported platform is a skip. The `--help` text ("interactive init offers once and non-interactive init leaves it unchanged") disagrees with the code. | R (help text M) | `…/init/init.py:328-358`; help output |
| T4 | `login` on an ownerless home is first-time setup: commits identity, then calls the same `_complete_initialization` to start the daemon. It does **not** install the service. | R | `…/login/commands.py:155-212` |
| T5 | One launch primitive, `_launch_daemon_process`: if an attributable service unit is installed, ask the supervisor to start; otherwise spawn `[sys.executable, "-m", "hyprial.cli", "daemon", "run", "--json"]` with `start_new_session=True`, stdio to `state/daemon-launch.log`, under an exclusive `state/daemon-launch.lock`, re-probing under the lock so only one generation is created. Used by `init`, `login`, `daemon start`, upgrade restart. | R | `…/common/daemon_start.py:339-381, 384-429, 499-507`; `…/daemon/commands.py:81-99` |
| T6 | The daemon runs detached with no service manager. `daemon start` on an initialized isolated home: serving in **5.2 s**; process had PPID 1 and its own session id; a second `daemon start` returned `alreadyRunning: true` with the same pid. | M | isolated run |
| T7 | `daemon start` needs only a home with an owner; it does not need `init` or `login` to have run in that process. | M | isolated run (home held only `settings.json`) |
| T8 | Service identity: launchd label `com.hyprial.hyprial.daemon`, plist `~/Library/LaunchAgents/<label>.plist`, `RunAtLoad`, `KeepAlive`, `ThrottleInterval 5`, `ExitTimeOut 120`; systemd `~/.config/systemd/user/hyprial.service`, `Restart=always`, `RestartSec=5`, `WantedBy=default.target`, `loginctl enable-linger` on install. Program is `<hyprial executable> daemon run` with `HYPRIAL_HOME`, `HARNESS_STATE_DIR`, `HYPRIAL_SERVICE_MANAGED=1` in the unit environment. | R | `daemon/impl/operations/service.py:41, 219-228, 638-639`; `daemon/impl/services/*.template` |
| T9 | Restart behaviour: under a supervisor the unit relaunches on any exit; consecutive start failures are counted in `state/daemon-start-failures.json` and a service-managed daemon waits 5 s → 60 s (doubling) before exiting. A service-managed `daemon run` that finds a healthy lock holder waits instead of exiting. Without a supervisor nothing restarts the daemon. | R | `daemon/impl/application/start_backoff.py:22-33, 68-78`; `…/daemon/commands.py:138-165, 353` |
| T10 | With a unit installed, start/stop/restart go through the supervisor (`kickstart`/`bootstrap`/`bootout`, `systemctl --user start|restart`); a CLI spawn next to `KeepAlive` is avoided on purpose. KP-005 records the earlier stray-daemon failure. | R | `…/daemon/service.py:420-470, 519-571`; `docs/reference/known-problems.md` KP-005 |
| T11 | Ownership: `state/daemon.lock` (`DaemonStateOwnershipFence`, a process file lock, never handed to another process) plus the home identity-transaction lock (`home/.identity-transaction.lock`); the listener is the unix socket `state/daemon.sock`. Measured modes: state dir `0700`, socket `srw-------`, `daemon.json`/locks `0600`. | R, M | `daemon/impl/configuration/ownership.py:17-52`; `…/daemon/commands.py:334-377`; `ls -la` of the isolated state dir |
| T12 | CLI business commands call the daemon once over the socket (`daemon_request_once`: "One connect, one frame, one response. Nothing here retries") and **never auto-start** it. No daemon: `agent list --json` → `{"ok":false,"code":"DAEMON_UNAVAILABLE","error":"cannot connect to the Harness daemon socket …/daemon.sock: [Errno 2] No such file or directory"}`, exit 1. `daemon status` → `{"ok":true,"daemon":{"running":false}}`, exit 0. | M, R | isolated run; `kernel/impl/ipc/client.py:203-239` |
| T13 | `daemon start` on an ownerless home fails in 5.4 s: `DAEMON_START_FAILED`; launch log: "user identity is not set … Run: hyprial login". | M | isolated run |
| T14 | `daemon run --bootstrap` exists: one process cold-starts an ownerless home, runs login from the terminal, accepts initialize and becomes the daemon in the same PID. It has no wire surface (no socket, no HTTP). | R | `…/daemon/commands.py:247-304`; `shell/impl/daemon_wiring/onboarding/launch.py:85-197`; `daemon/impl/onboarding/runtime.py:11-13` |
| T15 | Autoupdate is now daemon-owned (calendar 03:17/15:17 inside the daemon); the old OS timer (`com.hyprial.hyprial.autoupdate`, `hyprial-autoupdate.timer`) is only retired by an upgrade step. `autoUpgrade` is **off** unless `settings.json` sets it. The timer installs and records a pending restart; `daemon upgrade apply` restarts. | R | `daemon/impl/autoupdate/__init__.py:1-7, 30-32`; `daemon/impl/updates/installation.py:361-380`; `…/upgrade/commands.py:385-399` |

### 2.2 Daemon lifecycle today, GUI path

| # | Fact | Mark | Evidence |
|---|---|---|---|
| G1 | Admission: ownerless home → `phase="bootstrap"`, no daemon check; home with an owner → `verify_ready_daemon` (ping + catalog, identity, version, epoch, complete catalog). Admission is documented as "without spawning or writing state". | R, M | `shell/impl/gui/backend/admission.py:96, 212-232, 243-312`; `ADMIT bootstrap None` / `ADMIT serving dl-owner` |
| G2 | Ownerless: the `request_http` child builds a `GuiRuntimeHost` that admits a `BootstrapRuntime`, takes both locks and writes `state/bootstrap.json` (`phase: awaiting-auth`, the child's pid). `/readyz` 200, `onboarding.status` → `phase: bootstrap, authenticated: false, nextActions: [login, register]`; `agent.list` → 404 `METHOD_NOT_FOUND`. | M, R | isolated run; `daemon/impl/application/onboarding/gui_host.py:202-236`; `request_http/__init__.py:91-136` |
| G3 | Initialize → in-process promotion: the reply is flushed, the bootstrap listener drains, `run_promoted` constructs the real `DaemonApplication` in the child and hands it both lock streams; a new listener (new port, bearer, generation) is published after `daemon.ready`. Measured: promoted in **1.1 s**, publication pid == child pid, `daemon.json` pid == child pid, endpoint changed, `onboarding.status` → `serving, initialized: true`. | M, R | harness output; `request_http/bootstrap/gui.py:59-116`; `gui_host.py:347-409`; `daemon/impl/onboarding/runtime.py:619-666` |
| G4 | No service unit is installed by promotion: nothing under `$HOME/Library/LaunchAgents` or `$HOME/.config/systemd/user` after promotion or after exit. No code path from `serve_gui`/`run_promoted` reaches `ServiceManager`. | M, R | harness output (`[]` twice); grep |
| G5 | The promoted daemon does **not** survive the GUI: SIGTERM to the child → exit 0, `daemon.sock` and `daemon.json` gone. If the embedded daemon stops while an owner still exists, `serve_gui` leaves its loop and the child exits. | M, R | harness output; `bootstrap/gui.py:115-116` |
| G6 | Second launch of the same home: `admit_source` → `REFUSED GUI_DAEMON_UNAVAILABLE the selected daemon is not reachable and ready`. Real CLI on an initialized home without a daemon: `{"ok":false,"code":"GUI_DAEMON_UNAVAILABLE","error":"the selected daemon is not reachable and ready","data":{}}`, exit 1. **Hypothesis confirmed.** | M | harness output; isolated run |
| G7 | Initialized home with a running daemon: the serving child holds no journal and no lock; every method is one `daemon_request_once` over the home's socket. `agent.list` 200 through the child. Stopping the child leaves the daemon running (same pid, same epoch). | M, R | isolated run; `gui_host.py:238-258, 279-289` |
| G8 | "Borrowed daemon" = a daemon the GUI child attached to but does not own. Logout is daemon-owned: the daemon completes the receipt and stops **itself**; the child sees the owner disappear, closes the listener, waits for the daemon's locks to drain (`reopen_after_logout`, never stops or steals) and reopens a bootstrap host. Measured against a standalone daemon: `loggedOut: true`, new publication after **1.0 s**, `onboarding.status` → `bootstrap`, `daemon status` → `running: false`. | M, R | isolated run; `bootstrap/gui.py:46-56, 76-77, 117-122`; `account_lifecycle.py:483-500`; `tests/integration/request_http/onboarding/test_gui_live_lifecycle.py:19-54` |
| G9 | A home initialized through the GUI and then served by a **standalone** daemon (`daemon start` after the GUI child exited) reports `initialized: true, deviceReady: true, nextActions: []` through a newly attached child. The durable initialize receipt survives the process change. | M | isolated run on the promoted home |
| G10 | A serving child started without a daemon still starts (`/readyz` 200 from a local projection, epoch `gui-host-<hash>`), answers 503 `DAEMON_UNAVAILABLE`, and after a daemon appears answers 503 `CATALOG_STALE` "daemon contract changed; restart the request-http process". The child pins the catalog and epoch at start. | M, R | isolated run; `gui_host.py:424-466` |
| G11 | The launcher has `GuiSession.replace_backend()` but nothing calls it, and nothing in the GUI reacts to `CATALOG_STALE`. A daemon restart under an open GUI is therefore not recovered today, in any state. | R | `gui/backend/lifecycle/session.py:96`; grep |
| G12 | While a bootstrap host holds an ownerless home, `daemon start` waits 30 s, reports `DAEMON_START_TIMEOUT`, and leaves a `daemon run` process blocked on the identity lock until the bootstrap host exits. | M | isolated run (the leftover exited when the host stopped) |
| G13 | A home that has an owner but was not initialized through the GUI reports `initialized: false, nextActions: ["device.ensure"]` (fabricated home, no device record). What a real CLI-`login` home reports is **U**; KP-059 records a related gap. | M (fabricated), U (real) | isolated run; `account_lifecycle.py:140-153`; KP-059 |

### 2.3 What direction (Y) touches

| # | Fact | Mark | Evidence |
|---|---|---|---|
| Y1 | `serve_external` / `nativeHost.promotion=external` promotes **in the same process** ("Promotion runs in this process under the continuous bootstrap locks … does not start a second target process"). "External" names who owns the foreground loop (the deleted Rust host), not who owns the daemon. No code in `src/` writes a `nativeHost` block any more; only the parser and tests remain (`bootstrap/external.py` 270 lines, `tests/shell/request_http/launch/test_external_promotion.py` 433 lines). | R | `request_http/bootstrap/external.py:1-20, 94-174, 215-267`; `request_http/config.py:301-312`; grep |
| Y2 | The existing attach path is the plain serving `GuiRuntimeHost` (G7) plus the borrowed-daemon logout path (G8). That is the mechanism (Y) reuses. | M | G7, G8, G9 |
| Y3 | Initialization completes only inside the promoting process: `initialized` is the journal state `completed`, set by `complete_initialize()` from the promotion finalizer at `daemon.ready`. A separately started daemon cannot complete a pending initialize receipt today. | R | `daemon/impl/onboarding/runtime.py:668-687`; `application/onboarding/service.py:261-270`; KP-059 ("跨进程 unknown 回执的自动恢复缺口") |
| Y4 | `ServiceManager` refuses a non-default home: "user-service management requires HYPRIAL_HOME=$HOME/.hyprial; use init --no-service for a non-default home" (`service-home-nondefault`), and refuses a home outside the operating home. Tests rely on that refusal because `HOME` does not isolate the launchd/systemd user domain. | R | `daemon/impl/operations/service.py:577-599`; `tests/support/runtime/environment.py:1-5` |
| Y5 | Label and unit name are constants, so two homes would collide on `com.hyprial.hyprial.daemon` / `hyprial.service`. The code already refuses to touch a label loaded from another unit file (`supervisor-label-foreign`). | R | `service.py:41, 228, 601-617` |
| Y6 | The unit's program is `shutil.which("hyprial") or sys.argv[0]`. Launched as `python -I -m hyprial.cli` (the shell plan), `sys.argv[0]` is the path of `cli.py`, not an executable. The launchd job scanner only recognizes `<exe> daemon run` shapes. | R | `…/daemon/service.py:59-78`; `daemon/impl/configuration/launchd_jobs.py:79-93`; `shell-6.md:151` |
| Y7 | No collision between two homes on the socket (`<state>/daemon.sock`, state is per home), on the launch lock, or on an OS autoupdate timer (T15). No fixed listening port: Zenoh endpoints are not persisted and unset by default. | R, M | T11, T15; `…/common/daemon_start.py:431-449` |
| Y8 | The node id defaults to the host name. Two homes on one machine logged into the same account are two daemons with the same `owner` and `nodeId`. The effect on the mesh is **U**. | R, U | `kernel/impl/primitives/node_identity.py:13-38` |
| Y9 | macOS AF_UNIX path limit is 104 bytes. `~/Library/Application Support/com.hyprial.hyprialos/home/state/daemon.sock` is about 73 bytes plus the user name; KP-059 records a real 110-byte failure in a long temp directory. | R | KP-059; `tests/integration/profiles/homes/test_profile_two_homes.py` (`_short_temp_root`) |
| Y10 | Windows: no service support (`ServicePlatformUnsupported`; `init` skips). The GUI source runtime needs `fcntl` and `os.getuid` and refuses with `GUI_RUNTIME_UNAVAILABLE` without them. `start_new_session` has no effect on Windows. Whether `hyprial gui` with the daemon driver runs on Windows at all is **U**. | R, U | `…/daemon/service.py:35-40`; `…/init/init.py:334-346`; `gui/backend/platform_support.py:16-40` |
| Y11 | Linux without a systemd user session: the install fails, `init` reports `service-install-failed` and keeps the detached daemon. | R | `…/init/init.py:348-358`; `service.py:25-31` |
| Y12 | macOS: `SMAppService.agent(plistName:)` (macOS 13+) registers a LaunchAgent whose plist sits in `<App>.app/Contents/Library/LaunchAgents/` and uses `BundleProgram` relative to the bundle; the item appears in System Settings → Login Items. It must be called from the app (Swift/ObjC API). | R (docs) | [SMAppService](https://developer.apple.com/documentation/servicemanagement/smappservice); [Updating helper executables](https://developer.apple.com/documentation/servicemanagement/updating-helper-executables-from-earlier-versions-of-macos); [forum 779379](https://developer.apple.com/forums/thread/779379) |
| Y13 | macOS App Translocation: a quarantined app run from where it landed (Downloads, a disk image) is run from a randomized read-only path; notarization does not exempt it; moving the app in Finder ends it. A plist or a detached process that records the bundle path during a translocated run points at a path that will not exist later. Exact behaviour for our bundle is **U**. | R (docs), U | [What causes App Translocation](https://eclecticlight.co/2023/05/09/what-causes-app-translocation); [Lost in Translocation](https://eclecticlight.co/2024/05/14/lost-in-translocation/) |
| Y14 | A daemon started from a GUI app inherits the app's environment (`PATH=/usr/bin:/bin:/usr/sbin:/sbin`); the service template sets a wider `PATH` (`~/.local/bin`, Homebrew). Harness CLIs may not be found by a detached daemon started from the app. | R, U | `service.py:101-109`; `shell-6.md:410` |

### 2.4 The version-match rule

| # | Fact | Mark | Evidence |
|---|---|---|---|
| V1 | Enforced in two places, both on the GUI path: the launcher's admission (`ping.version` and catalog `packageVersion` must equal this package's `__version__`; `GUI_DAEMON_IDENTITY` / `GUI_DAEMON_CATALOG`) and the `request_http` child at start (`VERSION_MISMATCH`). | R | `admission.py:253-291, 359-386`; `request_http/server.py:225-230` |
| V2 | Why: the child publishes a catalog digest computed from its own method declarations and pins it; the session contract pins that digest per browser session. A daemon of another version has a different catalog. | R | `admission.py:267-279`; G10; `host-2-3.md:251` |
| V3 | The plain CLI has no package-version gate; the IPC client only checks the IPC protocol version. | R | `kernel/impl/ipc/client.py:277` |
| V4 | Terminal upgrade: `uv tool install` of one exact tag, then restart the daemon onto the new install through the same launch/supervisor path; the automatic path installs and waits for `daemon upgrade apply`. Refused for an editable install and for an interpreter outside the uv tool directory. | R | `…/upgrade/commands.py:212-246, 318-326, 385-399, 478` |
| V5 | App update while the old daemon runs: nothing restarts it. The updated app's `hyprial gui` would be refused by V1; a detached daemon keeps executing from files that were replaced underneath it. Not measured. | R, U | V1, T5 |

## 3. `hyprial gui` today in the three home states

| State | Admission | Backend child | Result today | Mark |
|---|---|---|---|---|
| 1. Ownerless home | `bootstrap`, owner and principal `None`, no daemon check | hosts `BootstrapRuntime`, holds both locks; login and initialize are served; on initialize it becomes the daemon in the same PID (1.1 s), new port/bearer/generation | works; **the daemon lives and dies with the child**; no service unit | M (backend + admission; identity commit controlled). Launcher/Node half U in this run |
| 2. Initialized, daemon running (same version) | `serving`, principal `user:<owner>` | forwards every method over the socket; logout makes the daemon stop itself and the child reopens bootstrap (1.0 s) | works; stopping the GUI child leaves the daemon running | M (backend + admission). Launcher/Node half U in this run |
| 3. Initialized, no daemon | **refused**: `GUI_DAEMON_UNAVAILABLE`, exit 1 | not started | refused; this is every launch after the first one for a user who initialized through the GUI and has no terminal | M (real `hyprial gui --no-open --json`) |

## 4. Options

Common to all: the home with an owner needs a same-version daemon (V1).

- **X** — in state 3 the GUI child also embeds the daemon (what it already does after promotion).
- **Y** — the daemon is always a separate process; GUI and CLI are clients; `hyprial gui` ensures it.
  - **Y2** detached process (the existing primitive, T5/T6), nothing supervises it.
  - **Y1** OS user service with a per-home label/unit, installed by `hyprial` itself (as `init` does today).
  - **Y3** service registered by the app through `SMAppService` / Windows equivalents.
- **Z** — end state: the standalone daemon process also hosts the ownerless bootstrap behind the IPC socket, so the GUI child is a pure client in all three states and never holds a lock.

| Criterion | X | Y2 detached | Y1 per-home OS service | Y3 app login item | Z daemon-hosted bootstrap |
|---|---|---|---|---|---|
| Agents keep working with the window closed | **no** (G5, measured) | yes until logout/reboot/crash | yes, including reboot and crash | yes on macOS | as Y1/Y2 |
| Same behaviour for wheel and app | yes, but differs from the terminal path (terminal daemon outlives the CLI) | **yes** — same primitive as `init`/`login`/`daemon start` | yes once the non-default-home refusal is lifted | **no** — wheel users cannot use it | yes |
| New code | small (state-3 embed), plus a second owner of daemon lifetime to maintain | **smallest**: one ensure call in the gui command; ~30 lines in `bootstrap/gui.py` for the hand-over | medium: home-derived label/unit, program argv form, relaxed home check, exit-code policy for logout; `service.py` is at 996/1000 lines and its directory is at 7 files, so a split comes first | large and outside Python: Swift/ObjC call in the shell, plist in the bundle, Windows/Linux equivalents | large, in `daemon/**` and kernel IPC: a bootstrap wire host with peer verification |
| Reused | in-process promotion kept and extended | launch primitive, launch lock, serving attach, borrowed-daemon logout, listener rotation | everything in Y2 plus `ServiceManager`, backoff, supervisor-routed start/stop | little | `BootstrapRuntime`, `launch_bootstrap_daemon` |
| Deleted / made dead | nothing | `serve_external` branch stays dead and can be deleted; embedded daemon lifetime shrinks to the commit step | same as Y2 | `ServiceManager` unused for the app | `GuiRuntimeHost` bootstrap half, both `serve_gui` promotion and rotation, `serve_external` |
| Failure modes | closing the window stops message delivery; two lifetimes (embedded vs standalone) for the same home; a CLI `daemon start` races the GUI | start timeout or failure surfaces in the GUI launch (same data as `init`: code, log path); nothing restarts after a crash until the next launch; narrow app `PATH` (Y14) | relaunch loop after daemon-owned logout if the unit restarts on clean exit (R, not measured); foreign-label refusal; translocated bundle path (Y13) | approval prompt; translocation; registration state tied to the bundle | larger blast radius in the daemon start path |
| Upgrade | trivial (daemon restarts with every launch) | ensure must compare versions and restart an older daemon (V5) | same, through `kickstart -k` / `systemctl restart` (exists) | launchd restarts the agent after the bundle changes (U) | as Y1/Y2 |
| Uninstall | nothing left running | a running process until logout; no file outside the home | plist/unit outlives a deleted app and fails every 5–60 s until removed; needs an explicit "remove background service" action | macOS removes the item with the app (U) | as Y1/Y2 |
| Platforms | macOS, Linux; Windows U | macOS, Linux; Windows U (Y10) | macOS, Linux with a user session; not Windows | macOS 13+ only; others need separate work | as Y1/Y2 |
| Security | same uid; GUI child holds the daemon locks | same uid; no new principal: whoever can write the 0700 home can already start its daemon; socket 0600 | same, plus file attribution and foreign-label refusal (exists) | adds an OS-mediated approval | adds an unauthenticated-owner phase on the IPC socket that must be designed |

`serve_external` is not a variant of any option above (fact Y1).

## 5. Recommendation

**Adopt Y. Ship Y2 first, add Y1 for durability, do not adopt Y3, keep Z as the candidate end state after the CLI convergence.**

Reasoning against the owner's principles:

1. *"cli 和 gui 都依赖 daemon request，cli command 实际是由已经启动的 daemon 进行响应的"*. This is already true for state 2 and measured to work, including logout (G7, G8). X is the one design that contradicts it: the process answering requests would be the GUI's own child.
2. *The daemon holds the durable inbox.* Under X, closing the window stops delivery (G5). Under Y2 the daemon runs in its own session and survived its launcher and the GUI child (T6, G7).
3. *One launch path.* The terminal path has exactly one way to start a daemon (T5). Y makes the GUI use that same function. The front end (CLI or GUI backend) performs login because an ownerless home has no daemon in either path (T2, T4, G2); after that both start the daemon the same way.
4. *"client 只是最简化封装".* With Y1/Y2 the shell still only runs `hyprial gui --no-open --json`. Y3 moves daemon lifetime into the shell and makes app and wheel differ.
5. *Every data path through daemon request.* The GUI child's serving half is already only a forwarder (G7). Y shrinks the embedded daemon to the commit step of initialization.

How the two gaps close:

- **State 3**: before admission, the `gui` command calls the existing launch primitive when the home has an owner. It returns the running daemon or starts one (detached, or through the supervisor when a unit exists). Admission stays pure and unchanged.
- **State 1 → standalone**: keep the in-process promotion as the transaction that completes the initialize receipt (Y3 fact: it cannot complete elsewhere today). At `daemon.ready`, instead of publishing the serving listener from the embedded daemon, stop the embedded daemon, start the standalone one with `hyprial daemon start`, and re-enter the loop as an ordinary serving host. The browser sees one listener rotation, exactly as today (G3), and the same rotation the logout path already uses in the other direction (G8). Measured building blocks: promotion 1.1 s, clean child-side stop, standalone start 5.2 s, attach reports `initialized: true` (G9).
- The alternative of skipping the embedded daemon entirely (bootstrap releases the locks, a new process completes initialization) needs a cross-process initialize receipt, which is the open gap in KP-059. Not in this plan.

Why Y1 second and not first: it needs the refusal in Y4 lifted without weakening the test isolation it protects, a file split forced by the structural limits, and validation on a real Mac (BLOCKED in this sandbox). Y2 needs none of that and removes the user-visible failure.

## 6. Implementation plan

Order and position relative to the GUI single-launch PRs. "After 2c" because 2c routes the wheel through the same admission and edits `…/gui/commands.py` and `gui/backend/config.py`; doing DL-1 first would be redone for the wheel.

| PR | Content | Red test first | Files | Position |
|---|---|---|---|---|
| **DL-1** | `hyprial gui` ensures the daemon for a home that has an owner and the `daemon` driver: call `services._launch_daemon_process(ready_timeout=DAEMON_START_IDLE_BUDGET_SECONDS)` before `build_plan`; map its `CliError` to the GUI startup record unchanged (code, `logPath`). Not called for an ownerless home (G12) or for `contract-mock`. | (a) unit, `tests/shell/gui/lifecycle/test_gui_daemon_ensure.py`: with a fake services object, the launch is requested once for an owner home, zero times for ownerless and for the mock driver, and before admission. (b) integration, `tests/integration/request_http/onboarding/test_gui_relaunch.py`: isolated home with an owner and no daemon → today `GUI_DAEMON_UNAVAILABLE`; expected: admission passes and `daemon status` reports running; teardown `daemon stop`. | `…/cli/commands/gui/commands.py`; the two tests | after 2c; before PR 6 acceptance (it turns 6a-0 green); independent of 3 |
| **DL-2** | Hand-over after initialization in `serve_gui`: at ready call `host.stop_promoted()` instead of publishing; when `run_promoted` returns with an owner, run `[sys.executable, "-m", "hyprial.cli", "daemon", "start", "--json"]` with the child's home/state environment, refuse with `GUI_DAEMON_START_FAILED` on failure, then continue the loop with `rotated_config(owner=…)` and a new serving host instead of `break`. | extend `test_real_daemon_promotes_then_logout_reopens_empty_home` (or a sibling): after initialize, `daemon.json` pid **≠** child pid (today equal, measured); terminate the child; `ping` over the socket still answers; a second admission passes; then stop the daemon. | `request_http/bootstrap/gui.py`; the test | after 2c and DL-1; coordinate with the `request_http` developer (one file, bootstrap subpackage only) |
| **DL-3** | Version rule in the ensure step: a running daemon with another package version is restarted onto this install through the existing restart path (supervisor-aware), subject to OD-3. | unit: fake probe returns version `X`, package `Y` → restart requested once; same version → not requested. | `…/cli/commands/gui/commands.py`; test | after DL-1; needed before the first app **update** ships, not before the first app |
| **DL-4** | Recover an open GUI from a daemon restart: when the daemon epoch changes (or `/readyz` reports `CATALOG_STALE`), the launcher calls the existing `replace_backend()`; the session host rotates as it already does for promotion/logout. | integration: daemon + serving child; `daemon restart`; `/readyz` must return `ready: true` again within a bound (today stays 503, measured G10). | `gui/backend/lifecycle/session.py` (+ `rebind.py`); test | after 3 (one host to rotate) |
| **DL-5a** | Mechanical split of `daemon/impl/operations/service.py` (996 lines, directory at 7 files) into a `service/` package with the same public face. No behaviour change. | the existing service tests pass unchanged; structural gate passes with room. | `daemon/impl/operations/service/**`, imports | any time; before DL-5b |
| **DL-5b** | Per-home service identity: default home keeps `com.hyprial.hyprial.daemon` / `hyprial.service`; any other home uses `com.hyprial.hyprial.daemon.<id>` / `hyprial-<id>.service`, `<id>` = first 12 hex of sha256 of the resolved home path. Keep the "home outside the operating home" refusal (that is what protects test homes); lift only the "must be `$HOME/.hyprial`" refusal. Program rendered as `[interpreter, "-m", "hyprial.cli", "daemon", "run"]` when there is no console script; the launchd job scanner also matches on the unit's `HYPRIAL_HOME`. Clean exit after daemon-owned logout must not be relaunched (launchd `KeepAlive.SuccessfulExit=false`, systemd `Restart=on-failure`), verified on real managers. | `tests/daemon/application/composition/service/test_service_identity.py` with the fake runner: two homes → two labels and unit paths; default home unchanged; a home outside the operating home still refused; interpreter-form argv recognized. No real `launchctl`/`systemctl`. | `daemon/impl/operations/service/**`, `daemon/impl/configuration/launchd_jobs.py`, templates, `…/daemon/service.py`, `contract/cli-blackbox/run.py` (it names the unit; `Contract-Impact` line otherwise) | after DL-5a; before the app is given to users who expect reboot survival (OD-2) |
| **DL-5c** | `hyprial gui` installs the unit after a successful ensure, exactly as `init` does (non-fatal, skipped on unsupported platforms and when the bundle path is translocated or not under an applications directory). | unit: fake manager; installed once when policy allows, never for ownerless, failure does not fail the launch. Manual on a Mac: reboot, daemon is back without opening the app. | `…/cli/commands/gui/commands.py`; test; `docs/guides/gui-dev-mode.md` | after DL-5b; with or after PR 6 |
| **DL-6** | Delete the dead `nativeHost` branch: `bootstrap/external.py`, the `nativeHost` config block, `hostContract.features.externalPromotion`, its tests, `contract/native-host/`. | a config containing `nativeHost` is refused as an unknown key; existing launch tests for the branch are removed. `Contract-Impact` line required. | `request_http/{__init__,config,server}.py`, `bootstrap/external.py`, tests, `contract/` | with PR 7 (already listed there by `host-2-3.md:536`); coordinate with the `request_http` developer |
| **DL-7** | Stop and removal: document `hyprial daemon stop` / `daemon service uninstall`; add the GUI action that stops the background daemon and removes its unit (design needed: a daemon cannot unload its own supervisor while serving). | to be defined with the design | docs, later GUI | after DL-5c |

Each PR is one concern, one or two product files, and fits a Sonnet implementer. DL-1 and DL-2 together remove the reported failure. DL-5\* and DL-7 are the durability layer.

Interaction with the other work:

- **PR 2c / 3 (Python session host)**: no contract change. The host keeps seeing listener rotation at initialize and logout; DL-2 does not add a rotation. DL-4 uses the rotation the host must already support.
- **WIP PR #2822 (fold the session host into `request_http`)**: blocker 1 in design §7a (the listener is reopened at initialize/logout) remains under Y, because the child still hosts the bootstrap. Only option Z removes it. If the fold is wanted early, Z has to be scheduled first.
- **CLI convergence (other developer)**: do not touch `request_http/**`, `http_contract/**`, `kernel/impl/ipc/**` except the two agreed places — `request_http/bootstrap/gui.py` (DL-2) and the deletion in DL-6. DL-1, DL-3, DL-4, DL-5 stay outside those trees. `…/common/daemon_start.py` is used as is.
- **Core / extensions (design §4.1, §7.2)**: under Y the daemon is started by `hyprial` (the wheel), never by the shell. That keeps constraint 3 (the shell holds no identity or credentials) and agrees with the owner's oral definition (core includes communication and daemon start). `docs/design/core-power-pack-architecture-2026-10-19.md:56` assigns daemon start to Power Pack; that line needs to be reconciled when the split starts. Y3 would put daemon lifetime in the shell and conflict with constraint 3.

## 7. Owner decisions

| # | Decision | Recommended default | Changes user-visible behaviour |
|---|---|---|---|
| OD-1 | Direction | Y (daemon always its own process; GUI ensures and attaches) | **yes** — closing the GUI no longer stops the daemon |
| OD-2 | Durability for the first app release | Y2 only (detached; the daemon returns at the next app launch after a reboot); Y1 follows in DL-5 | **yes** — after a reboot agents are offline until the app is opened |
| OD-3 | Running daemon has another package version | restart it onto the launching install automatically, for both homes (the app user has no terminal). Alternative: refuse with the two versions and the command | **yes** — running worker turns are interrupted by the restart |
| OD-4 | Does `hyprial gui` install the OS service by default (DL-5c) | yes, the same default as `init`, non-fatal; macOS shows a "background item added" notice | **yes** |
| OD-5 | Service naming for non-default homes | hash of the home path (works for any home, needs nothing from the shell). Alternative: bundle id passed in by the app | no |
| OD-6 | App home location | keep the separate home, at a short path, and add a socket-path-length preflight (Y9). `~/Library/Application Support/com.hyprial.hyprialos` is acceptable for normal user names | no |
| OD-7 | Same account in the app home and in `~/.hyprial` on one machine (Y8) | measure first; until then the app sets a distinct `HYPRIAL_NODE_ID` (for example `<host>-hyprialos`) | **yes** — the app shows up as a separate node |
| OD-8 | Stop the daemon when the user quits the app explicitly | no; provide a separate "stop background service" action (DL-7) | **yes** |
| OD-9 | `hyprial gui` for wheel users in state 3 | same ensure as the app (identical behaviour); no flag | **yes** — a wheel user's `hyprial gui` now starts the daemon instead of failing |
| OD-10 | `serve_external` / `nativeHost` | delete in PR 7 (DL-6) | no |
| OD-11 | Option Z | not now; revisit when the CLI convergence is done and the session-host fold (#2822) is scheduled | no |

## 8. Unknowns and how to measure them

| Unknown | Measurement |
|---|---|
| Launcher/Node (later Python host) half of states 1 and 2 on this base | run `hyprial gui --no-open --json` in a worktree that has `gui/node_modules`, or on the wheel after 2c; this is step 6a-0 of `shell-6.md` |
| Real login (OIDC device flow) through the GUI, then relaunch | a Mac or Linux box with a real account; repeat `measure_promotion_relaunch.py` without the controlled identity |
| What a real CLI-`login` home reports for `initialized` in the GUI (G13) | log in with the CLI in an isolated home, attach the GUI child, read `onboarding.status` |
| Stopping the embedded daemon right after `daemon.ready` (DL-2): time and side effects of start-then-stop | DL-2's test records both durations; compare with the 1.1 s / 5.2 s measured here |
| launchd/systemd relaunch after daemon-owned logout (clean exit under `KeepAlive` / `Restart=always`) | real Mac and a Linux user session: install the unit, log out through the GUI, read `launchctl print` `runs` and `state/daemon-start-failures.json` |
| LaunchAgent whose program is inside a Developer ID signed, notarized bundle; behaviour when the app is moved, updated or deleted; behaviour while translocated | signed build on a Mac: install the unit from `/Applications`, from Downloads and from the mounted dmg; move and delete the app; read `launchctl print` |
| App update while the old daemon runs (V5) | install version N, start, replace the bundle with N+1, run `hyprial gui`; expected before DL-3: `GUI_DAEMON_IDENTITY` |
| Two homes, one account, one host name (Y8) | two isolated homes with the same owner on a network profile that reaches a test mesh |
| Detached daemon started from the app's narrow `PATH` (Y14) | start from a Finder-launched build; start a harness that lives in `~/.local/bin` |
| Windows: `hyprial gui` with the daemon driver, detached start, survival of the parent's exit or job object | Windows x64 box once the wheel exists (KP-027) |
| Linux desktop without a systemd user session | container or a minimal session: DL-1 must pass (detached), DL-5c must skip |

## 9. Files

- `docs/design/gui-single-launch-plans-2026-11-15/daemon-lifecycle.md` (this memo)
- `docs/notes/gui-single-launch-prototypes-2026-11-15/daemon-lifecycle-prototype/measure_promotion_relaunch.py` (measurement harness for G3–G6; read-only against the worktree; a starting point for the DL-2 test)

## Owner decision after this memo (2026-11-15)

- Direction confirmed: decouple `init` from `login`. The service starts without an owner; `init` registers and starts it; login is a request sent to the running service by CLI and GUI alike; the OS service supports any HYPRIAL_HOME (per-home label). Plan of record: docs/design/gui-single-launch-plans-2026-11-15/daemon-first.md (in progress). Interim step kept: ensure the daemon before GUI admission for an initialized home (DL-1). Dropped: the hand-over after in-process promotion (DL-2).
- The five owner decisions of §7 as recommended (daemon survives closing the app; detached process first; restart on version mismatch; app is a separate node with its own home; wheel users get the same ensure).
- actor-rustify is complete: dev f1a4caf86 (#2824) removed the pykka backend; the Rust engine is the only actor backend. `crates/` did not change between 9932b2b63 and f1a4caf86, so the staged engine build stays valid.
