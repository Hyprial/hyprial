# Plan of record: the daemon as the only long-lived backend from the first command (daemon-first, per-home OS service)

- Date: 2026-11-15
- Tree read: the 6pre worktree (HEAD `a4fb89919`, `dev` @ `9932b2b63` plus one unrelated deletion commit), package `0.5.5.4`. Read-only; `git status` of the worktree was identical before and after the probe.
- Base check: `dev` has since moved to `f1a4caf86` (`forgejo/dev-g`, PR #2824, pykka backend removed). Every source, test and contract file this plan cites was compared with `git diff 9932b2b63 forgejo/dev-g`: all are byte-identical, so the line references hold for `f1a4caf86`. The only differences inside the cited directories are `daemon/impl/ipc/ipc_stats.py`, four `request_catalog` files and `contract/daemon-request/openapi.json`, none of which is referenced. Section 8 covers the effect of that change.
- Direction: **confirmed by the owner on 2026-11-15**. `init` is decoupled from `login`; the service starts without an owner; login is a request to the running service from CLI and GUI alike; the OS service supports any `HYPRIAL_HOME`. This document is the plan for that, not one option among several. The staged alternative is kept only as the cost comparison in section 6.
- Status: plan only (direction decided, implementation not started). No repository file edited, nothing committed, no launchd/systemd unit touched.
- Builds on `docs/design/gui-single-launch-plans-2026-11-15/daemon-lifecycle.md` (facts T1–T15, G1–G13, Y1–Y14, V1–V5 are reused by number, not re-measured).
- Evidence marks: **M** measured here, **R** read from code, **E** estimate, **U** unknown. Paths are relative to the repository root; `S/` = `src/hyprial/`.

## 0. Summary

1. The owner's model is reachable with code that already exists. A 110-line prototype (not product code) put a unix-socket listener in front of the existing bootstrap dispatcher and ran the existing promotion: **one PID** served an ownerless home on `state/daemon.sock`, accepted login and initialize over the socket, became the real `DaemonApplication` on the same socket in 0.91 s, answered the real CLI (`agent list` exit 0), handled `auth.logout` and was back in the bootstrap phase 0.74 s later, then did a second login and initialize in the same PID (§3, M).
2. Why today's code is not this model: the daemon refuses an ownerless home (T13), so login runs in a front-end process, in two separate implementations (CLI in-process OIDC, GUI child hosting `BootstrapRuntime`); the bootstrap runtime has no socket listener (T14); the OS service is tied to `$HOME/.hyprial` and one global label (Y4, Y5); no command starts the daemon implicitly (T12).
3. "Unauthenticated-owner phase on the IPC socket" needs no new authentication design: the socket has no per-request principal today either. Access control is file mode `0600` in a `0700` directory (same uid). The bootstrap phase serves 8 onboarding methods plus `ping` and `daemon.catalog`; tokens never cross the socket.
4. Z written code: about 1,700 source lines and 2,200 test lines added, about 1,500 source and 2,000 test lines deleted (net near zero), in 14 PRs. Estimated 14–19 implementer-days plus Mac validation. The staged alternative (DL-1, DL-2, DL-5) is about 5–7 days and writes about 1,200 lines, but leaves two login implementations, the GUI-hosted bootstrap, and listener rotation in place.
5. Sequence: **DL-1 now (ensure the daemon before GUI admission for an initialized home), then the Z PRs**, with the per-home service built once in its final form. The interim step makes the previously planned **DL-2 (hand-over after in-process promotion) unnecessary**; it is not built. DL-5b's "do not relaunch after a clean exit" policy is not built either.
6. The plan removes blocker 1 of the session-host fold (#2822) only together with PR Z-6c (principal change without port/bearer change). Without Z-6c the port no longer has to change for promotion, but the child still restarts to change its fixed principal.

## 1. The owner's model as requirements

Why the process is coupled to an owner today (R): `resolve_node_owner` raises when neither `HYPRIAL_OWNER` nor `settings.json` names an owner (`S/identity/impl/onboarding/identity_record.py:126-133`). The owner is minted into every address `agent:<owner>:<machine>:<name>` and must be one value across a user's machines; the fallback to the host login was removed on purpose (`identity_record.py:109-124`). `DaemonApplication.from_environment` resolves it at construction, so the whole process inherits the requirement. Only the daemon's business functions (addresses, inbox, routing, mesh) need an owner. The process, its locks, its socket and the onboarding methods do not. The plan keeps the rule "no owner, no business function" and "no local-login fallback" unchanged and moves the boundary from "the process cannot start" to "the business application is not constructed until an owner exists".

| # | Requirement |
|---|---|
| R1 | For every home there is at most one long-lived backend process: `hyprial daemon run` for that home. No other process hosts onboarding, holds `daemon.lock` or the identity-transaction lock for longer than one request. |
| R2 | That process can start on a home that has no owner (and on a missing home, which it creates) and stays up. |
| R3 | Login, device preparation, initialize and logout are daemon requests in every state. CLI and GUI are clients; the front end only shows the verification URL and code, and may open a browser. |
| R4 | After logout the service is still running and accepts the next login. |
| R5 | The daemon is started either by the OS service manager or, when no unit exists, by the first `hyprial` command that needs it. That command registers the service (where supported and allowed) and starts it. Later commands presuppose it. |
| R6 | The service is registered per home: a non-default home gets its own unit carrying its `HYPRIAL_HOME`. Two homes do not collide. |
| R7 | A CLI command is answered by the already-running daemon; the CLI process performs no business work and takes no daemon lock. |
| R8 | The GUI backend chain (`session host → request_http → daemon`) is a pure client in every state; closing the GUI never stops the daemon. |
| R9 | Explicit lifecycle commands remain and never auto-start: `daemon status`, `daemon stop`, `daemon service uninstall`, `doctor`, `--help`, `--version`. |
| R10 | Tests and CI never register or start a real user service, and can opt out of implicit start. |
| R11 | `init` is decoupled from `login`: `init` creates the home, registers the service and starts it, and succeeds on a home without an owner. It never runs a login. |

## 2. Today versus required

| Req | Today | Evidence | Gap |
|---|---|---|---|
| R1 | Three hosts can own a home's locks: `daemon run`; the GUI `request_http` child in bootstrap and after promotion (G2, G3, G5); `daemon run --bootstrap`. | `S/daemon/impl/application/onboarding/gui_host.py:202-236,347-409`; `S/shell/impl/request_http/bootstrap/gui.py:59-131`; `S/shell/impl/daemon_wiring/onboarding/launch.py:85-203` | GUI hosting must go (Z-6b). |
| R2 | `daemon run` requires an initialized home and an owner. Failure text comes from identity. | `S/shell/impl/cli/commands/daemon/commands.py:305,363`; `S/identity/impl/onboarding/identity_record.py:130`; T13 | Lifecycle loop in `daemon run` (Z-2). |
| R3 | Bootstrap methods are already dispatched through the daemon method registry, but only inside the GUI child. The eight methods: `onboarding.status`, `auth.login.begin`, `auth.operation.get`, `auth.operation.cancel`, `auth.relogin`, `auth.logout`, `onboarding.device.ensure`, `onboarding.initialize`. The CLI has its own OIDC device flow that writes credentials itself. | `gui_host.py:128-138,291-314`; `S/daemon/impl/request_catalog/onboarding/handlers.py:258-267`; `S/shell/impl/login/flow.py:1-60,408,434` (859 lines, `webbrowser.open`); `S/shell/impl/cli/commands/login/commands.py:155-212` | Socket listener for the bootstrap phase (Z-1); CLI login as a client (Z-5). |
| R4 | Logout: the daemon stops itself; a GUI child reopens a bootstrap host; with no GUI nothing is left running (G8). | `S/daemon/impl/application/onboarding/account_lifecycle.py:482-500,607-617`; `bootstrap/gui.py:46-56,117-122` | Same loop, inside the daemon process (Z-2). |
| R5 | No implicit start. All CLI requests pass one function, which is where an ensure step belongs. `init` installs the unit by default (T3); `login` and `gui` do not. | `S/shell/impl/cli/commands/common/support.py:176-205` (only caller of the kernel client in `shell/impl/cli`); T12 | Ensure on connect failure (Z-4); registration in the launch primitive (Z-8c). |
| R6 | The unit already carries `HYPRIAL_HOME` and `HARNESS_STATE_DIR` (T8). Label and unit name are constants; a non-default home is refused. | `S/daemon/impl/services/launchd.plist.template`; `S/daemon/impl/operations/service.py:41,219-228,577-599` | Per-home naming, refusal lifted (Z-8b). |
| R7 | True for business commands when a daemon runs (T12). `login`, `init`, parts of `daemon`, `doctor`, `profile`, `upgrade` work in-process; the CLI-convergence plan postpones exactly these as wave W4 and says the "no-daemon phase" must be defined first. | `docs/design/cli-daemon-request-convergence-2026-11-14.md:183`; `…/cost-and-waves.md:19,22` | Z is that definition: the no-daemon phase shrinks to "start the daemon". |
| R8 | True only for an initialized home with a running daemon (G7). | G2, G3, G5, G6 | Z-6a/b. |
| R9 | Holds (nothing auto-starts). | T12 | Keep when adding ensure (Z-4). |
| R11 | `init` runs the login flow in-process when the home has no owner, then resolves the owner, then launches (T2). | `S/shell/impl/cli/commands/init/init.py:95-216,302-327` | `init` stops calling login; reports `phase: bootstrap` and the next step (Z-5). |
| R10 | Real supervisor commands are already refused when `HOME` is not the account's passwd home; homes outside the operating home are refused; a tripwire watches the real unit directories. | `service.py:982-988` (`service-account-home-mismatch`); `service.py:580-587`; `tests/support/runtime/environment.py:1-5,86-112` | Keep these two guards, add an opt-out variable for implicit start (Z-4, Z-8c). |

## 3. Measurement: the prototype

Files: `docs/notes/gui-single-launch-prototypes-2026-11-15/daemon-first-prototype/z_daemon_proto.py` (the process) and `measure_z.py` (the driver). Isolation as in the earlier memo: fresh `/tmp/dfz.*` root (short path for AF_UNIX), `isolated_environment()` from the test support, `HYPRIAL_OWNER` removed, network isolated, never `$HOME/.hyprial`, no service command. Identity commit and device provisioning are the controlled ports of `tests/integration/request_http/onboarding` (no account, no network). The process and the root were removed afterwards.

What the prototype adds to existing code: an accept loop on `state/daemon.sock` (one request per connection, the frame of `S/kernel/impl/ipc/client.py:223-301`), a call into the existing `GuiRuntimeHost` bootstrap dispatcher, the existing post-reply hook, and the loop "bootstrap → `run_promoted` → bootstrap". Clients: the real `daemon_request_once` and the real CLI.

| Step | Result (M) |
|---|---|
| A1 | Socket answers 4.37 s after spawn (interpreter and imports; `BootstrapRuntime.admit` itself 0.01 s). `ping` → `phase: bootstrap, owner: None`. |
| A2 | State dir: `bootstrap.json`, `daemon.lock`, `daemon.sock` (mode `0600`), `onboarding/`. RSS 146 MB. |
| A4 | `daemon.catalog`: 153 methods, 8 callable (the eight of R3). |
| A6 | `agent.list` → `CALLER_NOT_AUTHORIZED` "method is not available in login bootstrap". |
| A7 | Real `hyprial daemon status --json` → exit 1 `CALLER_NOT_AUTHORIZED` (it calls `ps`). **Gap to close in Z-2.** |
| A8 | Real `hyprial agent list --json` → exit 1, same code, 0.49 s. **Error wording/code decision OD-Z5.** |
| B1–B2 | `auth.login.begin` returns `verification {verificationUri, userCode, expiresAt, intervalSeconds}`; `auth.operation.get` → `authenticated`; owner written to `settings.json` by the identity worker port. |
| C1–C3 | `onboarding.initialize` → `promoting`; serving 0.91 s after the reply; same PID; `daemon.json` pid equals the process pid; RSS 168 MB. |
| C4 | `onboarding.status` over the socket as the local operator: `serving, authenticated, initialized: true, deviceReady: true, nextActions: []`. |
| C5 | Callable methods 8 → 124. Epoch changed (bootstrap epoch ≠ serving epoch). |
| C6–C7 | Real CLI `agent list` exit 0 (0.69 s); `daemon status` exit 0, `running: true`. |
| D1–D3 | `auth.logout` over the socket → `loggedOut: true`; bootstrap again after 0.74 s in the same PID, new epoch. |
| E1 | Second login + initialize in the same PID: serving after 0.37 s. |
| F1–F2 | SIGTERM → exit 0; socket and `daemon.json` removed. |

Not covered: real OIDC (**BLOCKED**, account and network), the GUI chain against this process (**U**), `owns_process_exit()` behaviour in the loop (the prototype does not set it; `daemon run` does, `commands.py:372`) (**U**), macOS (**BLOCKED**), kill -9 at each phase (**U**).

## 4. Design

### 4.1 Process model

```
  launchd LaunchAgent / systemd user unit, one per home          (or: detached spawn by the
  label com.hyprial.hyprial.daemon[.<id>] / hyprial[-<id>].service   same launch primitive, T5)
          │ RunAtLoad / KeepAlive / Restart=always
          ▼
  ┌──────────────────────────────────────────────────────────────────────────────┐
  │ hyprial daemon run            ONE process per home                            │
  │   holds state/daemon.lock + home/.identity-transaction.lock for its lifetime  │
  │                                                                              │
  │   phase bootstrap : BootstrapRuntime + bootstrap listener                     │
  │                     ping, daemon.catalog, shutdown, 8 onboarding methods      │
  │   phase restoring/serving/reconciled : DaemonApplication, full catalog        │
  └───────────────▲──────────────────────────────────────────────────────────────┘
                  │ state/daemon.sock   (AF_UNIX, 0600, state dir 0700, same uid)
      ┌───────────┼──────────────────────────┬─────────────────────┐
      │           │                          │                     │
  hyprial <cmd>   request_http child         MCP / adapters /      doctor, daemon status
  ensure → one    (HTTP projection of the    workers (unchanged)   (never ensure)
  request         same MethodSpecs, bearer)
                        ▲ loopback HTTP + bearer
                  Python session host  ◄── browser / native shell (HyprialOS, own home)
```

### 4.2 Phases of the daemon

```
            start (home has no owner)                 start (home has an owner)
                     │                                          │
                     ▼        onboarding.initialize             ▼
   ┌────────────► bootstrap ───────────────────────────►  restoring → serving → reconciled
   │   awaiting-auth → authenticating → authenticated          │
   │                        → promoting                        │ auth.logout (receipt durable,
   │                                                           │ reply written, application stops)
   └───────────────────────────────────────────────────────────┘
        same PID; if the process exits instead, the next start lands in bootstrap
```

- `ping.phase` already has `restoring`, `serving`, `reconciled` (`S/daemon/impl/ipc/router.py:340-356`), and both bootstrap hosts already answer `phase: "bootstrap"` (`gui_host.py:292-299`; `S/daemon/impl/application/onboarding/http.py:48`). `METHOD_PHASES = {"bootstrap", "serving"}` and per-method `(phase, caller)` pairs exist in the method declarations (`S/kernel/impl/contracts/daemon/method_spec.py:76-80,149-179`; `S/daemon/impl/request_catalog/onboarding/specs.py:49-66`). The bootstrap phase is therefore an existing value that gains a socket, not a new concept. The bootstrap `ping` must be widened to the fields launch code reads (`running`, `pid`, `socket`).
- Bootstrap state held by the process: the two continuous locks (`S/daemon/impl/onboarding/locks.py`), `state/bootstrap.json` (`pid`, `birthIdentity`, `epoch`, `socket`, `phase`; M), the onboarding operation journal under `state/onboarding/` (`runtime.py:135-137`), an identity worker while a login is in flight (`runtime.py:330-346`).
- Promotion is unchanged: `accept_initialize(defer_quiesce=True)` arms a post-reply action, the listener closes after the reply is fully written, `run_daemon` constructs the application and hands over both lock streams, `complete_initialize` runs at `daemon.ready` (`runtime.py:412-488,619-687`; `gui_host.py:347-409`). `runtime.py:11-13` and `:251-258` already describe "a wire host" and "the public IPC" as the caller of these hooks.
- What is specific to the `request_http` child and is not carried over: the HTTP response subclass that fires the post-reply hook (`S/shell/impl/request_http/bootstrap/response.py`, 73 lines), listener and bearer rotation (`bootstrap/gui.py:24-28,82-116`), the GUI control-session surface (`gui_host.py:216-227`). Everything else in the bootstrap path is transport-independent.
- Logout: recommended form is the in-process loop (measured D2); an owned start enters the same loop after a pure logout (Z-6pre) unless the owner survives it, so both start kinds use it. If the application ends the process instead (`owns_process_exit`), the result is the same one start later, because an ownerless start is now legal: under `KeepAlive`/`Restart=always` the unit relaunches into bootstrap after the 5 s throttle; without a supervisor the next command's ensure step starts it. The relaunch-loop risk named in the earlier memo existed because an ownerless start was a failure that fed `state/daemon-start-failures.json` (`S/daemon/impl/application/start_backoff.py:22-33`); under Z it is not a failure. No change to `KeepAlive` or `Restart=` is needed.
- Crash recovery: a kill between `accept_initialize` and `daemon.ready` leaves an `initialize` receipt in `promoting`/`unknown` while the home has an owner. The next start serves, and `onboarding.status` reports `initialized: false, nextActions: [initialize]` although serving refuses initialize (KP-059, `account_lifecycle.py:771-777`). Z-3 closes this: at `daemon.ready` a serving daemon completes a non-terminal initialize receipt whose `account_owner` equals the current owner and whose device record is present (OD-Z4).

### 4.3 What each client does in each phase

| Client | No daemon | bootstrap | restoring | serving |
|---|---|---|---|---|
| CLI business command | connect fails before anything is sent (`client.py:228-239`) → ensure → retry once | stable refusal "not signed in; run `hyprial login`" (OD-Z5) | existing `DAEMON_RESTORING` wait (`client.py:187-200`) | one request |
| `hyprial login` (first time) | ensure | `auth.login.begin` → print URL and code, open browser unless `--no-open` → poll `auth.operation.get` → `onboarding.initialize` → poll `ping` until `serving` → existing aftercare | — | account switch path, unchanged in this plan |
| `hyprial init` | create home if missing, register the unit (where allowed), start | succeeds: reports `phase: bootstrap`, `nextSteps: ["hyprial login"]`; runs no login | — | reports existing state |
| `hyprial logout` | ensure | `auth.logout` (bootstrap variant, pre-initialize) | — | `auth.logout`; then daemon is in bootstrap |
| `hyprial gui` | ensure (all states) | admission by `ping.phase`; child forwards everything | wait | as today (G7) |
| `request_http` child | start refused `BACKEND_UNAVAILABLE` (launcher ensures first) | pure forwarder | forwards, readiness false | forwarder |
| `daemon status`, `doctor`, `daemon stop`, `daemon service *` | report absence; never start | report `phase: bootstrap` | as today | as today |

### 4.4 Socket and authentication rule

Facts (R): the listener is created with the directory at `0700` and `chmod 0600` on the socket (`S/daemon/impl/ipc/server.py:131-148`). A request frame is `{version, id, method, params}` with no caller field (`client.py:223`). The server marks every frame `_trusted_message_origin="ipc"` (`server.py:609-611`). The peer PID is read for statistics only (`S/daemon/impl/ipc/params.py:52-72`); there is no uid check. Onboarding handlers take their caller from the process, never from parameters: a serving daemon synthesizes `caller="local-operator"` for IPC (`S/daemon/impl/application/onboarding/__init__.py:130-155`).

Rule for the bootstrap phase:

1. Same socket path, same modes. Additionally verify the peer uid (`SO_PEERCRED` on Linux, `LOCAL_PEERCRED` on macOS) and refuse another uid. This is defence in depth; the file mode already enforces it.
2. Every IPC frame in bootstrap runs as `("bootstrap", "local-bootstrap")` on one daemon-minted operator surface (the existing `local_operator_surface`, `runtime.py:169-194`). All local clients therefore see the same login operation.
3. Allowed methods: `ping`, `daemon.catalog`, `shutdown`, and the eight onboarding methods. Everything else is refused.
4. Secrets: the device code, access and refresh tokens stay inside the identity worker and `secrets/`; the socket carries only `verificationUri`, `userCode`, expiry and operation ids (M: B1).

Consequences: the trust boundary equals today's CLI login. Any same-uid process can already run `hyprial login` or write `settings.json`. What changes is that a same-uid process can observe or cancel a login another client started (today the GUI's login is bound to the GUI control session and reachable only with the HTTP bearer). The browser cannot reach a unix socket; the HTTP child keeps its bearer and origin rules.

### 4.5 Catalog and epoch handling

Facts (R):

- The declaration digest ignores per-method `state` (`S/shell/impl/request_http/catalog.py:21-33,168-182`). It does not change with the phase.
- The HTTP contract digest is computed over the routes, and a method whose state is `disabled` is not a route (`catalog.py:329-331,185-214`). In bootstrap 145 methods are `disabled`; in serving the two bootstrap-only methods are `disabled`. So the digest differs between phases, and the child answers `CATALOG_STALE` (`S/shell/impl/request_http/server.py:427-438`); this is G10.
- An epoch change with the same version, node and owner is already absorbed by a re-handshake (`server.py:440-471`). A changed owner is `CATALOG_MISMATCH` (`server.py:441-452`), and the child's owner and actor are fixed in its config (`server.py:103-123,241-254`).
- `not-ready` with a reason is an existing catalog state for "declared, not callable now" (`S/daemon/impl/request_catalog/daemon/catalog.py:120-125,259-267`; `gui_host.py:516-521` uses reason `ACCOUNT_REQUIRED`).

Rule:

1. Phase unavailability is always `not-ready` with a reason (`ACCOUNT_REQUIRED` in bootstrap, `BOOTSTRAP_ONLY` in serving), never `disabled`. The route set and both digests are then identical in every phase. A test asserts digest equality of the two projections. This changes the reported `state` of existing methods: `Contract-Impact` line and `contract/daemon-request` update.
2. `ping.epoch` changes at every phase change. Stateless clients (CLI) follow automatically.
3. `request_http` treats an epoch change whose `phase` changed as a legitimate re-handshake even when `owner` changed, republishes its capability with the new principal and a new transport generation, and keeps port and bearer (Z-6c). The session host revokes business sessions on that generation change. This is rule 11 of `contract/gui-session/README.md:394-419`, which says the launcher "may" publish a new authority; nothing in that contract is removed, and under the fold the file disappears because the state is in-process.
4. During promotion the socket is absent for under a second (M: 0.37–0.91 s). The initialize caller polls `ping`; other clients get a connect failure and go through ensure, which re-probes under `state/daemon-launch.lock` (`S/shell/impl/cli/commands/common/daemon_start.py:413-429`) and finds the daemon. Optional later refinement: hand the bound listener to the application so clients queue in the backlog (`server.py:131-148`; E: about 30 lines).

### 4.6 Login through the daemon: what moves

| Part | Today | Under Z |
|---|---|---|
| OIDC device authorization, token poll, UserInfo, credential and owner commit | CLI: `S/shell/impl/login/flow.py` + `oidc.py` (1,349 lines) in the CLI process. GUI: identity worker started by `BootstrapRuntime` (`runtime.py:263-354`; `S/identity/impl/onboarding/service.py:58`) | daemon only (the identity worker) for first-time login |
| Showing URL and code | CLI prints (`S/shell/impl/cli/commands/login/flow.py:60-68`); GUI renders `verification` | unchanged, from the `auth.login.begin` result |
| Opening a browser | CLI `webbrowser.open` (`S/shell/impl/login/flow.py:408`) | stays in the front end; the daemon never opens a browser |
| Device key | CLI `_ensure_login_device` (`…/login/flow.py:829-876`) | `onboarding.device.ensure` / `onboarding.initialize` (`S/daemon/impl/application/onboarding/service.py:641-671`) |
| Account switch on a home that has an owner | CLI, with daemon stop/start orchestration | not changed here; it is the CLI-convergence W4 item, and `auth.relogin` already exists |

Risk introduced: the device flow runs with the daemon's environment. A service unit has `PATH` only; a machine that needs `HTTPS_PROXY` (KP-001) logs in from a shell today and would fail from the service. Z-8b must render proxy variables into the unit or the login request must carry them (OD-Z7).

### 4.7 Per-home OS service

| Item | Today (R) | Change |
|---|---|---|
| Home in the unit | already rendered (`launchd.plist.template` `HYPRIAL_HOME`, `HARNESS_STATE_DIR`; `systemd.service.template:11-12`) | none |
| Name | `LAUNCHD_LABEL` constant, used in 12 places in `service.py` plus `S/shell/impl/cli/commands/daemon/service.py:468,577`, the systemd template line 15, `tests/support/runtime/environment.py:65-66` | default home (`$HOME/.hyprial`) keeps `com.hyprial.hyprial.daemon` / `hyprial.service`; any other home: `com.hyprial.hyprial.daemon.<id>` / `hyprial-<id>.service`, `<id>` = first 12 hex of sha256 of the resolved home path. `HYPRIAL_SERVICE_UNIT` in the unit carries the derived name. |
| Non-default refusal | `service.py:588-593` | removed |
| Test safety | (a) home outside the operating home refused (`service.py:580-587`); (b) every real supervisor command refused when `HOME` ≠ passwd home (`service.py:982-988`); (c) tripwire on the real unit directories | (a), (b), (c) stay. (b) is what actually protects tests that fake `HOME`, and it does not depend on the removed refusal. Add `HYPRIAL_SERVICE_AUTOINSTALL=0` and `HYPRIAL_DAEMON_AUTOSTART=0`, set once in `tests/conftest.py` (the autouse environment fixture at `:64-130`) and in `isolated_environment()`; product code never sets them. Unit tests keep the fake runner. |
| Program | `shutil.which("hyprial") or sys.argv[0]` (`…/daemon/service.py:63`); template has one `{{EXECUTABLE}}` then `daemon run` | when no console script resolves to this install, render `[sys.executable, "-m", "hyprial.cli", "daemon", "run"]`; `argv_runs_daemon` (`S/daemon/impl/configuration/launchd_jobs.py:79-93`) gains that shape and matches on the unit's `HYPRIAL_HOME` |
| File size | `service.py` 996 lines; `daemon/impl/operations/` has 7 direct files | split first into `operations/service/`: `types.py` (errors, `ServiceStatus`, `ServiceConfig`, about 95), `templates.py` (about 80), `identity.py` (new naming, about 60), `supervisor.py` (launchctl/systemctl calls, about 200), `manager.py` (rest, about 620); the directory drops to 6 files |
| Exit policy | `KeepAlive true`, `Restart=always` | unchanged (see 4.2) |
| Commands | `daemon service install` (register and start, as `brew services start`), `daemon service uninstall` (stop and remove, as `brew services stop`), `daemon service status`, `daemon start|stop|restart` routed through the supervisor when a unit exists (T10) | no new command needed. `daemon service uninstall --home` is not needed because the home comes from the environment as everywhere else. |
| Contract text | `contract/daemon-lifecycle/service-supervision.md` (192 lines), `contract/cli-blackbox/run.py` | updated with the naming rule |

"First run of any command registers and starts the service", concretely:

- Where: inside the one launch primitive (`daemon_start.py:339-381`). If a unit is installed, start through the supervisor (today). If not, and the platform is supported, auto-install is not disabled, `HOME` is the account home, and the program path is stable (not translocated, Y13), install the unit and start through it. Otherwise spawn detached (today).
- Who calls it implicitly: the CLI request function on a connect failure (`support.py:176`), `hyprial gui`, `hyprial login`, `hyprial init`.
- Who never calls it: commands that make no daemon request (`--help`, `--version`, completion); `daemon status`, `daemon stop`, `daemon run`, `daemon service status|uninstall`, `daemon upgrade status`, `doctor`, `profile *`. These pass `ensure_daemon=False` to `runtime.execute`.
- Non-interactive use: ensure never prompts. In bootstrap a business command fails with one stable code. CI sets `HYPRIAL_DAEMON_AUTOSTART=0`.
- Cost: nothing when the daemon runs (the ensure is on the failure path). When it does not: about 4.4 s to the bootstrap phase (M: A1) or 5.2 s to serving (T6). A CLI command takes 0.5–0.7 s in this sandbox (M: A8, C6).
- Tests that expect no daemon: 29 test files mention `DAEMON_UNAVAILABLE` (M: grep). With the opt-out set globally they keep their meaning.

### 4.8 Interaction with parallel work

- **CLI convergence** (`request_http/**`, `http_contract/**`, kernel IPC). Z-1 to Z-3 do not touch those trees: they are in `daemon/impl/onboarding`, `daemon/impl/application/onboarding`, `shell/impl/daemon_wiring`, `shell/impl/cli/commands/daemon`. Z-4 edits `support.py:176-205`, the request function that developer also edits: one agreed small change. Z-5 is their wave W4 for `login` and `init` and should be done by or with them. Z-6a/b/c are inside `request_http/**` and must be scheduled with them. Z supplies the "no-daemon phase" definition their plan requires before W4, so it is the continuation of that work, not a competitor. The kernel IPC client is not changed.
- **GUI host line (2b merged soon, 2c and 3 next)**. No change to what 2c and 3 build. The session host keeps relaying to `request_http`; it should not speak to the daemon socket, because `request_http` is the one HTTP projection of the method declarations (parameter constraints, envelope, preview confirmation: `app.py` 985 lines plus `http_contract/`), and the convergence plan keeps it (`cli-daemon-request-convergence-2026-11-14.md:45-47`). After Z-6a the launcher's admission asks the daemon in every state (`S/shell/impl/gui/backend/admission.py:212-232` loses the "ownerless: no daemon check" branch). `rebind.py` and the authority file stay until Z-6c plus the fold.
- **#2822 (fold the session host into `request_http`)**. Blocker 1 is "the listener is reopened at initialize and logout". Under Z the reopen is no longer needed for promotion (Z-6b), and Z-6c removes it for the principal change. After Z-6c the fold has no listener rotation to absorb. The other recorded obstacle (file size and directory limits of `request_http`, `host-2-3.md:283-285`) is unaffected.
- **Native app (HyprialOS, own home)**. The shell still runs `hyprial gui --no-open --json`. Ensure registers `com.hyprial.hyprial.daemon.<id>` with the app home and the bundle interpreter in module form. Y9 (socket path length), Y13 (translocation), Y8 (same account in two homes) remain as in the earlier memo.

## 5. PR list

Estimates: deletions are counted from the tree (`wc -l` and line ranges). Additions are estimated from the prototype (listener 45 lines, loop 45 lines) multiplied for product concerns (bounds, timeouts, peer check, logging, signals, header decision block), or from the size of the comparable existing code named in the row. Test lines are estimated from the existing tests of the same kind. All additions are **E**.

| PR | Content | Files | Src +/− | Tests +/− | Red test first | Depends on | Sandbox |
|---|---|---|---|---|---|---|---|
| **DL-1** | `hyprial gui` ensures the daemon for a home with an owner (earlier memo, unchanged) | `S/shell/impl/cli/commands/gui/commands.py` | +45 / 0 | +200 / 0 | as in the memo: isolated owner home without daemon → today `GUI_DAEMON_UNAVAILABLE` | PR 2c | ok |
| **Z-0** | Design note in the repo (this document condensed), `ping.phase` and catalog rule in `contract/daemon-request` | `docs/design/daemon-first-2026-11-15.md`, `contract/daemon-request/README.md` | docs +200 | — | — | owner decisions | ok |
| **Z-1** | Bootstrap wire host: dispatcher extracted from `GuiRuntimeHost` (ping, catalog projection, registry invoke: `gui_host.py:291-319,471-492`) plus a socket listener with frame bounds, peer-uid check and the post-reply hook. `GuiRuntimeHost` delegates to the dispatcher. No product path uses the listener yet. | new `S/daemon/impl/onboarding/wire.py`; `gui_host.py`; `S/daemon/interface.py` | +300 / −120 | +280 / 0 | `tests/daemon/onboarding/wire/test_bootstrap_wire.py`: `daemon_request_once("ping")` against the listener on a temp ownerless home returns `phase: bootstrap`; initialize post-reply fires only after a complete write | none | ok |
| **Z-2** | `daemon run` on an ownerless or missing home runs the lifecycle loop (bootstrap → promote → serve → logout → bootstrap). `daemon start` treats `phase: bootstrap` as started; `daemon status` and `daemon stop` work in bootstrap (`shutdown` served; `status` falls back to `ping`). `--bootstrap` kept as an accepted no-op flag. Rule 1 of 4.5 (`not-ready` instead of `disabled`). | `S/shell/impl/daemon_wiring/onboarding/launch.py`; `S/shell/impl/cli/commands/daemon/commands.py`; `S/shell/impl/cli/commands/common/daemon_start.py`; `gui_host.py:347-409` (`run_promoted` moves to the daemon side); `S/daemon/impl/request_catalog/daemon/catalog.py` | +300 / −60 | +330 / 0 | integration: isolated ownerless home, `hyprial daemon start --json` → today `DAEMON_START_FAILED` (T13); expected `ok`, `daemon status` shows `running: true, phase: bootstrap`, `daemon stop` exits 0. Second test: a custom child entry with the controlled identity port reproduces §3 steps B–E through the product loop. | Z-1 | ok |
| **Z-3** | Serving start completes a non-terminal initialize receipt of the current owner (KP-059). Crash matrix: kill -9 in `awaiting-auth`, `authenticating`, `promoting`; the next start reaches a valid phase and `onboarding.status` is truthful. | `S/daemon/impl/application/onboarding/__init__.py:193-225`; `account_lifecycle.py` | +90 / −10 | +220 / 0 | journal with a `promoting` initialize receipt and an owner → `on_daemon_ready` → today `initialized: false`; expected `true` | Z-2; OD-Z4 | ok |
| **Z-8a** | Mechanical split of `service.py` into `operations/service/` | `S/daemon/impl/operations/service/**`, imports | moves 996 | 0 | existing service tests unchanged; structural gate | none | ok |
| **Z-8b** | Per-home unit name, refusal lifted, module-form program, job scanner, proxy variables (OD-Z7); exit policy unchanged | `operations/service/**`; `launchd_jobs.py`; both templates; `…/daemon/service.py:59-78,468,577`; `contract/daemon-lifecycle/service-supervision.md`; `contract/cli-blackbox/run.py` | +200 / −70 | +240 / −20 | fake runner: two homes → two labels and unit paths; default home unchanged; home outside the operating home refused; fake `HOME` → `service-account-home-mismatch` | Z-8a | unit tests ok; real launchd **BLOCKED**; real systemd user session not probed (**U**) |
| **Z-8c** | The launch primitive installs the unit when allowed, else spawns detached; opt-out variable; `conftest.py` and `isolated_environment()` set it | `daemon_start.py`; `…/daemon/service.py`; `tests/conftest.py`; `tests/support/runtime/environment.py` | +80 / 0 | +170 / 0 | fake manager: installed once when policy allows; never with the opt-out; install failure falls back to detached and does not fail the command | Z-8b; OD-Z2 | unit ok; reboot survival on a Mac **BLOCKED** |
| **Z-6a** | `request_http` `local-gui` child forwards in bootstrap when a daemon answers; launcher ensures the daemon in every state and admits by `ping.phase` | `S/shell/impl/request_http/__init__.py`, `server.py`; `gui_host.py`; `S/shell/impl/gui/backend/admission.py`; `…/gui/commands.py` | +120 / −40 | +200 / 0 | isolated ownerless home with the Z-2 daemon running: start the child → today exit with `DaemonOwnershipBusy` refusal (G12 mirror); expected `/readyz` 200 and `onboarding.status` → `bootstrap` relayed | Z-2; **CLI-convergence developer** | ok (browser half **U**) |
| **Z-4** | CLI ensure on connect failure, `ensure_daemon=False` for the R9 commands, opt-out variable, one stable refusal for business commands in bootstrap | `S/shell/impl/cli/commands/common/support.py`; `S/shell/impl/cli/runtime/composition.py`; six command modules for the flag | +110 / −10 | +240 / 0 | fake services: connect failure → launch requested once → request retried once; with the opt-out the old `DAEMON_UNAVAILABLE` payload byte for byte; `daemon status` never launches | Z-2, Z-6a (so an auto-started ownerless daemon cannot lock out the GUI); **CLI-convergence developer** | ok |
| **Z-5** | First-time `hyprial login` as a daemon client (published `--json` shape kept). `hyprial init` decoupled from login: home, unit, start, report; the in-process login branch of `init` is removed (R11; changes `init --json` on an ownerless home: `Contract-Impact` and `contract/cli`) | new `S/shell/impl/cli/commands/login/client_flow.py`; `…/login/commands.py:155-212`; `…/init/init.py:95-216,302-327` | +300 / −150 | +320 / −80 | controlled-identity daemon entry + real CLI: `hyprial login --json --no-open` on an ownerless home → the CLI process never holds the identity lock, `daemon.json` pid is the daemon's, output keys equal the recorded ones | Z-2, Z-4; **CLI-convergence developer (their W4)** | ok with controlled identity; real OIDC **BLOCKED** |
| **Z-6b** | Delete GUI-hosted bootstrap: `serve_gui` promotion and rotation, `rotated_config`, `reopen_after_logout`, the HTTP post-reply response, the bootstrap half of `GuiRuntimeHost` | `bootstrap/gui.py` (131), `bootstrap/response.py` (73), `gui_host.py` (about 230 of 570), `server.py`/`app.py` (about 30) | +30 / −460 | 0 / −500 (`test_gui_promotion.py` 303, `test_gui_live_lifecycle.py` 196, replaced by Z-6a's test) | a `local-gui` child on an ownerless home with no daemon is refused `BACKEND_UNAVAILABLE` instead of hosting (corrections of the Z-6b plan, 2026-11-17: Z-6a made rotation the way a forwarding child publishes a daemon phase change, so `rotated_config`, `new_host` and the attach half of `reopen` stay until Z-6c; `reopen_after_logout` was already replaced by `reopen`, Z-6b deletes its hosting half; `bootstrap/response.py` has a second consumer, `serve_external`, and goes with Z-7; tests deleted about 1,050 lines, not 500; the prerequisites are Z-6a and Z-6pre, not Z-4 and Z-5) | Z-6a, Z-6pre | ok |
| **Z-6c** | Principal change in place: re-handshake on phase change keeps port and bearer; new transport generation; launcher publishes authority with the same endpoint | `server.py:427-471`; `S/shell/impl/gui/backend/lifecycle/rebind.py`; `S/shell/impl/request_http/config.py` | +110 / −40 | +160 / 0 | daemon in bootstrap + child; initialize; the child's port and token are unchanged and `server.json` carries a new generation and the owner (today: new port, G3) | Z-6b; **CLI-convergence developer**; PR 3 | ok |
| **Z-7** | Delete `serve_external`, `nativeHost`, `BootstrapHttpHost`, the terminal login loop of `daemon run --bootstrap` (widened DL-6) | `bootstrap/external.py` (270), `bootstrap/__init__.py` (36), `http.py` (63), `config.py` nativeHost (about 30), `launch.py` terminal loop (about 180), `contract/native-host/` (34), `contract/wire/daemon-login-http-bootstrap.md` (36) | 0 / −600, contract −70 | 0 / −1,390 (`test_external_promotion.py` 433, about 200 of `test_launch_contract.py`, `test_native_hosted_bootstrap.py` 182, `test_bootstrap_http.py` 40, `test_bootstrap_launch.py` 533) | a config with `nativeHost` is refused as an unknown key; `Contract-Impact` line | Z-6b; GUI PR 7 | ok |
| **Z-9** | Docs, skills, KP-059 update, `AGENTS.md` paragraph, operator guide | `docs/guides/`, `docs/reference/known-problems.md`, `.agents/skills/hyprial-e2e`, `S/skills/hyprial-ops` | docs +220 / −80 | — | — | all | ok |

Also needed in both designs, unchanged from the earlier memo: **DL-3** (version rule in ensure) and **DL-4** (an open GUI recovers from a daemon restart).

Each row is one concern with one to five product files. Z-2 and Z-5 are the largest; Z-2 can be split into "loop" and "daemon start/status/stop in bootstrap" if a reviewer prefers.

## 6. Effort comparison

| | Staged (DL-1, DL-2, DL-5a/b/c, DL-6) | Z + per-home service (DL-1, Z-0 … Z-9) |
|---|---|---|
| Source lines written | about 415 | about 1,715 |
| Test lines written | about 750 | about 2,360 |
| Lines deleted (source + tests + contract) | about 1,200 (DL-6 only) | about 3,570 |
| Implementer-days (E, one PR = 0.5–2 days including review) | 5–7 | 14–19 |
| Of which identical in both | DL-1, service split, per-home naming, first-run install: about 4–5 days | same |
| Mac validation | DL-5b exit policy on real launchd, DL-5c reboot | Z-8b/c on real launchd, reboot; no exit-policy change to verify |
| Coordination with the CLI-convergence developer | one file (DL-2), one deletion (DL-6) | Z-4, Z-5, Z-6a/b/c |
| End state | daemon survives the GUI; two login implementations; GUI child still hosts bootstrap and promotes; listener rotates; ownerless home has no daemon; CLI never auto-starts | R1–R10 |

What the staged path builds that Z later deletes or reverts:

- **DL-2** entirely: the hand-over in `serve_gui` (about 70 source and 130 test lines, about 1 day plus coordination). Z-6b deletes `serve_gui`.
- **DL-5b exit policy** (`KeepAlive.SuccessfulExit=false`, `Restart=on-failure`) and its verification on real managers. Under Z a clean exit must be relaunched, so the policy is reverted.
- **DL-1's "not for an ownerless home" branch**: one condition, removed in Z-6a.

Nothing else in the staged list is thrown away; DL-5a, the naming part of DL-5b, DL-5c and DL-6 are the same work as Z-8a, Z-8b, Z-8c and part of Z-7.

The incremental cost of reaching the owner's model instead of the staged end state is about 9–12 implementer-days, of which about 4 (Z-5 and the request-function change) fall inside work the CLI convergence has to do anyway for W4.

## 7. Sequence

1. **Now**: DL-1 after PR 2c. Removes the reported failure (state 3).
2. **Remaining owner decisions** (§10), then Z-0.
3. **Daemon side, in parallel with GUI 2c/3 and CLI W1–W3** (no shared files): Z-1 → Z-2 → Z-3. Independently: Z-8a → Z-8b → Z-8c, with a Mac for validation.
4. **With the CLI-convergence developer**: Z-6a → Z-4 → Z-5.
5. **Deletion and fold preparation**: Z-6b → Z-6c (after PR 3) → Z-7 (with GUI PR 7) → Z-9.

DL-2 (hand-over after in-process promotion, earlier memo §6) is made unnecessary by this sequence and is not built: DL-1 covers every launch after the first, and Z-6a/b remove the in-process promotion it would have patched. The clean-exit unit policy of DL-5b is likewise not built. What the user sees meanwhile:

- From DL-1 until Z-6a: a user who initializes through the GUI has the daemon inside the GUI child for that first session only. Closing the window stops delivery until the next `hyprial gui`, which then starts a standalone daemon that survives. Terminal users are unaffected.
- From Z-2: `hyprial daemon start` works on an ownerless home; nothing starts it implicitly yet.
- From Z-8c: the first `daemon start`, `init` or `gui` on a supported platform registers the per-home unit; the daemon returns after a reboot.
- From Z-4/Z-5: any command starts the daemon; `hyprial login` is answered by it.

If the first app release must not have the "first session" limitation and Z-6a cannot land before it, DL-2 is the insurance: about one day, deleted later (OD-Z3).

## 8. Effect of the finished actor-rustify work on this plan

Read from `forgejo/dev-g` (`f1a4caf86`) with `git show` and `git diff`; no worktree was created.

### 8.1 What changed

- PR #2824 removed the pykka backend and the `ActorRuntime` facade: `kernel/impl/actor_runtime/backends/pykka.py` (139 lines) and `runtime.py` (94) deleted, `port/engine.py` reduced by 555 lines, `pykka` dropped from `pyproject.toml`. `ACTOR_BACKENDS` is now `("reference-abi", "native")`; an unset variable means `native`, and a missing or foreign compiled extension fails closed (`kernel/impl/actor_runtime/port/engine.py:37-56` on `dev-g`; `backends/native.py:54-60`).
- `crates/` is identical between `9932b2b63` and `f1a4caf86` (`git diff --stat` is empty). The compiled engine is the same binary.
- Daemon startup, promotion, bootstrap, IPC, service manager and CLI daemon commands are byte-identical (header of this document). In `daemon/impl/application` only six files changed, none on the startup or promotion path; `shutdown.py` changed one description string ("actor engine stop; its actors are daemon threads").

### 8.2 Is the promoted `DaemonApplication` still constructible in-process the same way

Yes, as far as reading and the existing measurement go.

- The native engine was already the default on the tree the prototype ran on (`port/config.py:39-53` at `9932b2b63`), and the isolated environment strips `HYPRIAL_*`, so section 3 was measured **with the Rust engine**: bootstrap → serving → logout → bootstrap → serving in one PID, two full application lifetimes (M).
- There is no "one engine per process" constraint. An engine is created per actor host (`port/host.py:87-91`), and each engine owns its own tokio multi-thread runtime, 4–16 workers (`crates/hyprial-py/src/engine.rs:81,374-383`). A second application in the same process builds new engines; the first ones are shut down by the application's `actor-runtime` shutdown phase.
- Process-global state in the extension: the native handler registry (`crates/hyprial-py/src/handlers.rs:147-159`, populated at module init, read-only afterwards) and the interpreter-exit attach gate (`crates/hyprial-py/src/gate.rs:1-30`). The gate closes once, from `atexit`; it does not close when an engine shuts down, so it does not prevent a second engine. It does mean the process must end through normal interpreter exit: a handler still inside Python is given a bounded grace, and a thread that takes the GIL after finalization began aborts the process (SIGABRT) on CPython 3.12/3.13.
- Consequence for the logout loop: returning to bootstrap in the same process (4.2) avoids an interpreter exit at logout entirely, which is the safer side of that constraint. The fallback (process exit, next start in bootstrap) goes through the same exit path every `daemon stop` uses today.
- Threads and GIL: handlers run on dedicated OS threads, never on tokio workers (`crates/hyprial-py/src/pump.rs:11`); engine calls release the GIL (`engine.rs:563,758,778,886,893`). The bootstrap phase constructs no engine (it serves the registry handlers directly, `gui_host.py:309-314`), so the bootstrap listener thread and the engine never coexist.
- Fork and spawn: the plan forks nothing. The daemon is started with `subprocess` and `start_new_session` (T5) or by the supervisor; the identity worker is a separate process started during bootstrap, before any engine exists. A process that holds a live tokio runtime must not `fork()` without `exec`; nothing here does.
- Not re-measured (**U**): the same double promotion on `f1a4caf86` itself. The Python-side adapter changed (`backends/abi.py` +67: admission tracing, event logging moved in). Z-2's second test is that measurement; run it first on the new base.

### 8.3 Packaging

- The wheel is platform-specific (`cp312-abi3` extension) and there is no fallback backend. `reference-abi` still exists as an explicit selection, not as a fallback. KP-027 (five-platform wheel publication, installers and manifests still hard-code `py3-none`) is open and blocks release (`docs/reference/known-problems.md:262-269` on `dev-g`). This plan does not depend on KP-027 for development or tests, but **no user gets any of it before KP-027 is closed**, the same as every other 0.5.5.4 change.
- OS service whose program is inside an app bundle: the unit's interpreter and the extension must be the same build. Rendering the program as `[<bundle python>, "-m", "hyprial.cli", "daemon", "run"]` (4.7) guarantees that, because the interpreter resolves its own `hyprial._core`. A unit that pointed at a `hyprial` console script found on `PATH` could start a different install with a different or missing engine; `shutil.which("hyprial")` (`…/daemon/service.py:63`) must therefore only be used when it resolves into the running install (Z-8b).
- Fail-closed start under a supervisor: a daemon whose extension is missing or foreign exits during startup. That is a start failure, so it is counted in `state/daemon-start-failures.json` and throttled 5 s → 60 s (T9). With the lifecycle loop this can only happen at promotion or at an owner start, not in bootstrap, because bootstrap loads no engine. Z-2 should verify the engine is loadable before entering bootstrap (import `hyprial._core.actors` once) so a broken install fails at start instead of after the user has logged in.
- uv tool directory: `uv tool install` replaces the environment in place (V4). A running daemon keeps the old `.so` mapped; the unit path stays valid; the restart (`kickstart -k` / `systemctl restart`, or detached relaunch) loads the new extension. The upgrade refuses an editable install and an interpreter outside the uv tool directory already (V4). New with a compiled engine: an upgrade to a tag that has no wheel for this platform fails at install, before the restart; the old daemon keeps running. That is the correct outcome and should be asserted in the upgrade tests once KP-027 lands.
- App update while the old daemon runs (V5): unchanged, and now also covers the engine. DL-3 (restart a daemon of another package version through the supervisor) is the fix in both worlds.

### 8.4 Tests

- Isolated daemon tests need the compiled engine or an explicit `HYPRIAL_ACTOR_BACKEND=reference-abi`. Tests of this plan that start a real application (Z-2 second test, Z-3, Z-5, Z-6a, Z-6c) must run with the native engine, because that is what ships; unit tests of the wire host (Z-1), the ensure step (Z-4) and the service manager (Z-8) construct no engine.
- The staged build at `the planner's scratch area (native-engine/, not committed)` (`actors.abi3.so` and `actors.stamp.json`, built from `9932b2b63`) is valid for `f1a4caf86`: `git diff --stat forgejo/dev-f forgejo/dev-g -- crates` is empty, and `scripts/build` did not change. It stops being valid at the first commit that touches `crates/`; the stamp carries `source_sha256`, so compare it rather than trust the date.
- CI: the pykka job is gone; `native-conformance.yml` keeps `reference-abi`, `native-linux-amd64`, `native-linux-arm64`, `native-darwin-arm64` and `free-threaded-3-15t`. A new red test must not select a backend by name that no longer exists.
- Memory: the measured 146 MB (bootstrap) and 168 MB (serving) per daemon are with the Rust engine (K11).

## 9. Risks and unknowns

| # | Item | Mark | How to settle |
|---|---|---|---|
| K1 | `owns_process_exit()` may end the process at logout instead of returning to the loop | U | Z-2 test with the flag set; the fallback (exit, next start is bootstrap) is specified in 4.2 |
| K2 | Device flow from the service environment behind an HTTP proxy | R, U | OD-Z7; test on the cloud machine of KP-001 |
| K3 | A same-uid process can observe or cancel another client's login | R | accepted by rule 4.4, written into the contract |
| K4 | `disabled` → `not-ready` changes a published catalog field value | R | `Contract-Impact`; GUI reads `callable`, verify the GUI and the Contract Mock corpus in Z-2 |
| K5 | Published `hyprial login --json` shape under the client flow (identity, device, daemon blocks, resume, `--dry-run`) | U | golden comparison in Z-5; account switch and `--dry-run` stay on the old path |
| K6 | An auto-started ownerless daemon before Z-6a locks the GUI child out (the mirror of G12) | R | ordering: ownerless ensure is enabled only with Z-6a |
| K7 | Real launchd: per-home label, module-form program inside a signed bundle, translocation, uninstall leaving a unit that fails every 5–60 s | BLOCKED here | Mac run as listed in the earlier memo §8 |
| K8 | systemd user session without linger, or no user session (Y11) | U | container run; ensure must fall back to detached |
| K9 | Windows: the bootstrap listener needs the named-pipe listener the full daemon uses (`server.py:135-142`) | U | out of scope until the Windows wheel exists (Y10) |
| K10 | Real OIDC through the daemon from a terminal | BLOCKED here | account and network |
| K13 | Double promotion in one PID not re-measured on `f1a4caf86` (Python adapter changed, engine binary identical) | U | Z-2 second test on the new base, first |
| K14 | Release of any part of this plan is behind KP-027 | R | none in this plan |
| K11 | Memory: a bootstrap-phase daemon holds 146 MB per home before login (M) | M | acceptable for one home; note for multi-home machines |
| K12 | The `dev` base moves under the convergence work; line ranges here are verified for `9932b2b63` and `f1a4caf86` | R | re-check ranges at implementation time |

## 10. Owner decisions

| # | Decision | Recommended default | Visible to users |
|---|---|---|---|
| OD-Z1 | Direction | **decided (owner, 2026-11-15)**: daemon-first, `init` decoupled from `login`, per-home service; DL-1 as the interim step; DL-2 not built | yes |
| OD-Z2 | Does the first command register the OS service without asking | yes on macOS and Linux with a user session, non-fatal, one stderr notice naming the unit and `hyprial daemon service uninstall`; never when `HOME` is not the account home, never with the opt-out. Alternative: only `init`, `gui` and `daemon service install` register; other commands start detached | yes (macOS "background item added") |
| OD-Z3 | Close the "first GUI session" gap before Z-6a | no; accept it until Z-6a. Alternative: build DL-2 (about 1 day, deleted later) | yes |
| OD-Z4 | A serving start completes a pending initialize receipt of the same owner | yes, when the device record is present | yes (KP-059 symptom disappears) |
| OD-Z5 | What a business command returns in the bootstrap phase | new stable code `ACCOUNT_REQUIRED`, message "not signed in on this home; run `hyprial login`", exit 1. Alternative: keep `CALLER_NOT_AUTHORIZED` (measured today) | yes |
| OD-Z6 | Logout under a supervisor | return to bootstrap in the same process; unit options unchanged | no |
| OD-Z7 | Proxy for daemon-side login | render `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY` present at install time into the unit, as `PATH` is today. Alternative: the client passes them per login request | no |
| OD-Z8 | `daemon run --bootstrap` terminal login | flag accepted and ignored from Z-2, terminal loop deleted in Z-7; `hyprial login` is the terminal entry | yes (operators using the flag) |
| OD-Z9 | Who implements Z-4, Z-5, Z-6a–c | the CLI-convergence developer, or this line with their review; decide before Z-6a | no |
| OD-Z10 | Peer-uid check on the bootstrap listener | yes | no |
| OD-Z11 | Unit naming for non-default homes | sha256 prefix of the home path (as OD-5 of the earlier memo) | no |

Decisions OD-3 (version mismatch), OD-6 (app home path), OD-7 (node id for two homes) and OD-8 (quit does not stop the daemon) of the earlier memo are unchanged and still open.

## 11. Files

- `docs/design/gui-single-launch-plans-2026-11-15/daemon-first.md` (this document)
- `docs/notes/gui-single-launch-prototypes-2026-11-15/daemon-first-prototype/z_daemon_proto.py` (prototype of the Z process; not product code; a starting point for the Z-2 test entry)
- `docs/notes/gui-single-launch-prototypes-2026-11-15/daemon-first-prototype/measure_z.py` (driver that produced §3)

## Owner decision (2026-11-15): Windows out of scope for now

Only Linux and macOS are built and verified. Corrected by the owner the same day: do NOT delete Windows-related tests, gates or CI targets; existing checks such as `scripts/ci/check_windows_portability.py` and the PR-template item keep running and must stay green. Windows-only problems are recorded and ignored for now: no new Windows code or tests, no blocking on them. Packaging targets are macOS arm64 and Linux x64; Windows items in this plan are not scheduled.
