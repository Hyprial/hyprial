# Hyprial GUI: launch and developer modes (`hyprial gui`)

`hyprial gui` is the one way to start the web GUI. It runs in the foreground,
serves the page on one loopback address and opens the browser on a one-time
launch URL.

- An installed wheel serves its verified built snapshot (`hyprial/_gui`). A
  source checkout builds `gui/` and serves `gui/dist`. `hyprial gui dev` is
  `hyprial gui --with-dev-tools --live-source`: the page comes from the GUI
  source through Vite, with the developer tool layer on. The tool layer is
  served only behind `--with-dev-tools`.
- The daemon is the only long-lived backend. In every state of the selected
  home `hyprial gui` ensures the daemon first: a home with an owner gets its
  daemon, a home without one gets a bootstrap daemon. Closing the GUI never
  stops the daemon.
- Login and logout change the principal inside the running GUI. The GUI
  process, its address and the browser origin stay the same (see
  [Login, logout and the daemon](#login-logout-and-the-daemon)).
- `hyprial gui start|status|stop|upgrade` are rejected with
  `INVALID_ARGUMENT`; there is no compatibility shim. The foreground GUI is
  not a background service.
- The native shell (`clients/native/`, packaged by `packaging/clients/`) runs
  `hyprial gui --no-open --json --port 0 --stdin-stop` and shows what that
  command reports. It does not start or stop the daemon.

The daemon driver (the default) reaches the daemon through `request_http`, the
HTTP projection of the daemon's request methods, which the session host runs
inside its own process: `hyprial gui` starts one Python child and there is no
second HTTP server between the host and the daemon. The explicit `contract-mock`
driver serves the synthetic Contract Mock instead, as a separate Node child, and
needs no daemon.

For UI design, component usage, state/credential rules and handoff expectations,
start with the [GUI design and development standard](../../gui/docs/ui-development-standard.zh-CN.md),
the [component catalog](../../gui/docs/component-catalog.zh-CN.md) and the
[UI change handoff template](../../gui/docs/ui-change-handoff-template.zh-CN.md).
These documents separate the current fixture implementation from target
requirements; source and tests remain authoritative.

## Dev/product parity

`hyprial gui dev` and `hyprial gui` serve one product composition: both mount
the same `App`, and the launcher-level driver selection (`daemon` vs
`contract-mock`) plus the injected capabilities are the only intended
differences. Product components must not branch on `import.meta.env.DEV`,
`__HYPRIAL_DEV_*` or build mode, and `firstRunWizardEnabled` is capability
data, never a build-mode check. The dev-only surface is the independent
floating tool layer (default-collapsed launcher ball, expand on click) mounted
outside the product `#root`; it never alters product layout and never ships in
a production build. Anti-regression is enforced by three gates wired into
`gate-gui.yml`: `verify:dev-product-parity` (static import/env-branch gate),
`verify:dev-browser-parity` (dual-entry browser comparison plus floating-tool
interaction; its production side runs a separate `dist-parity` test artifact,
built with the same PROD build and DCE as the release bundle, explicit mock
inputs enabled only at the composition root, no dev tools mounted; test-only,
never a release artifact) and `verify:production-copy` (artifact gate scanning
only the release `dist`, which must contain no mocks, fixture URLs or scenario
data). The parity target is
[`docs/design/gui-dev-product-parity-2026-11-09.md`](../design/gui-dev-product-parity-2026-11-09.md).

## Commands

For an isolated local Docker test of a static build from remote `dev`, see
[GUI Docker debug run](gui-docker-debug.md).

```sh
hyprial gui                 # installed: verified wheel assets; source: npm build, then the Python host serves gui/dist
hyprial gui dev             # source checkout: its own gui/ + npm run dev (Vite) behind the Python host; wheel: exact sourceCommit worktree
hyprial gui --with-dev-tools   # the built GUI plus the developer tool layer
hyprial gui --live-source      # the Vite dev server behind the host, without the tool layer
```

`dev` is an alias for `--with-dev-tools --live-source`; combining it with either
flag changes nothing. The two flags are independent: `--with-dev-tools` decides
whether the tool layer is served, `--live-source` whether the page comes from the
GUI source through Vite (edits reload in the browser) or from built assets. What
this guide says about `hyprial gui dev` and its source selection applies to
`--live-source` in the same way.

An installed wheel carries a built GUI snapshot under `hyprial/_gui` plus a
`release.json` manifest (full source commit, source remote, package/GUI
versions, and a SHA-256 for every served file). Bare `hyprial gui` verifies and
serves that snapshot with a loopback Python server, so it needs no Git, Node,
npm, or source checkout. Normal mode has two targets only: the wheel `_gui`,
else the source checkout it runs in. `--gui-home` is rejected with
`INVALID_ARGUMENT`, and a `HYPRIAL_GUI_HOME` inherited from the caller is
rejected with `GUI_CONFIG_CONFLICT`; the variable is only the launcher's handoff
to its own child processes. A missing, malformed, incomplete,
or tampered snapshot fails closed with `GUI_ASSETS_MISSING`,
`GUI_MANIFEST_INVALID`, or `GUI_ASSETS_INVALID`.

A source checkout without built-in wheel assets runs the project's `build`
script before serving. Build output
goes to stderr when `--json` is used. A failed build exits with
`GUI_BUILD_FAILED` before any child starts.

Only the bare command and the `dev` leaf command are public. `start`, `status`,
`stop`, and `upgrade` are retired and are rejected with `INVALID_ARGUMENT`;
there is deliberately no compatibility shim.

Minimal options:

| Option | Meaning |
| --- | --- |
| `--ip IP` | Frontend bind address; default `127.0.0.1`. An IP literal or `localhost` only. |
| `--port PORT` | Frontend port; default `4173`, range 0–65535. A conflict fails; there is no silent fallback. `0` lets the session host bind a free loopback port; `port` and `url` in the startup record (and the handoff URL) name the port it bound, never 0. In `dev` mode the public port is the host's, not Vite's. With `0` the browser origin changes on every launch, so `localStorage` starts empty. |
| `--gui-home PATH` | Live source only (`--live-source` or `dev`): the `gui/` project directory containing `package.json`; overrides `HYPRIAL_DEV_GUI_HOME`. Without live source it is rejected with `INVALID_ARGUMENT` naming `--live-source`, and `HYPRIAL_DEV_GUI_HOME` is not read. |
| `--with-dev-tools` | Serve the developer tool layer (module index; an API docs link over the daemon driver). The only switch for it: no environment variable, URL or storage key stands in. `dev` implies it. |
| `--live-source` | Serve the GUI source through the Vite dev server behind the host; needs Git, Node and npm. `dev` implies it. The startup record says `live_source: true`; the Vite port is not exposed. |
| `--no-open` | Do not call the browser. The only secret-bearing handoff is an exclusive private file; stdout/JSON expose its path, never the URL or grant. |
| `--stdin-stop` | EOF on stdin or a `shutdown` line stops the GUI like SIGTERM, but exits 0: the host and, when they exist, the dev server and Contract Mock process groups are reclaimed and the daemon is left running. Without the flag stdin is never read. Keep the pipe open for as long as the GUI should run (stdin at EOF, such as `< /dev/null`, stops it at once). Meant for a supervising launcher (a native shell) that must leave no orphan if it dies. |
| `--json` | Emit exactly one startup record on stdout, then run in the foreground. Child output goes to stderr. |

The browser opens once by default, only after the host has printed its
readiness line and answers its health route (and, with the `contract-mock`
driver, the Contract Mock answers its own health route). For `--no-open`, the complete private handoff is written
first and the `launch.ready` marker that starts the grant lifetime is published
after it; the lifetime never starts from host process start or before the
handoff is complete.
If the desktop opener fails, the CLI reports `GUI_BROWSER_FAILED`, terminates
only the children it started, removes the private runtime files and returns
nonzero. This controlled failure and cleanup also applies to the installed
entry (the Python session host); it never silently reports success. With `--no-open`, the JSON
`handoff_path` names a non-symlink `0600` file inside the `0700` runtime
directory. Because the file contains the one-use fragment URL, do not print its
contents into logs. Open it in the intended browser; the Python session host unlinks it
after the launch grant establishes a session. A handoff created after startup
gets its own full launch lifetime, and expiry of that lifetime also unlinks it.
Every SIGINT/SIGTERM/error path has supervisor cleanup as the final fallback.

`--json` prints one startup object; its keys are listed under
[Startup record](#startup-record). `auto_worktree` is an object for the
installed-wheel managed worktree, otherwise `null`; it records `source`, `path`,
`branch`, `base`, `created`, `managed`, `dirty`, `remote`, and the dependency
`install` command/cwd/exit/reason. The command exits with the host's exit
status (a non-zero status after start is first explained once on stderr, see
[Processes and files](#processes-and-files)), 130 on Ctrl-C, 143 on SIGTERM, or
0 after a `--stdin-stop` request. The dev server (`GUI_ASSETS_EXITED`) or, with the
`contract-mock` driver only, the Contract Mock (`GUI_BACKEND_EXITED`) exiting
after the record makes the command print one more stdout JSON line, `{"ok": false, ...}`, and
exit 1; at most one such line follows the record. With `--stdin-stop`, a stop
requested before the record exits 0 with no stdout line at all; read "exit 0 and
no line" as a clean stop. A stop request that arrives during cleanup is ignored,
so the first outcome's exit status and failure line stand.
The record carries `record_version: 1`; its schema, the failure object
(`stages_seconds`, `children` log tails) and examples are the contract in
`contract/cli/gui-startup-record.v1.json`.

## Remote use through an SSH tunnel

To open the GUI from another machine, start it on the host with
`hyprial gui --no-open --json`. The record gives `port` and `handoff_path`; the
file at `handoff_path` (mode 0600, one line, `launch.url`) holds the full URL
including the one-use `#grant=...` fragment.

- Forward the port with the LOCAL port equal to the remote port, and open the
  browser on `127.0.0.1`, not `localhost`:
  `ssh -N -L <port>:127.0.0.1:<port> <host>`. The host checks Host and Origin
  against its exact origin, so a different local port or host name is refused.
- The grant is valid for 60 s from the moment the handoff is complete and can
  be used once. After expiry or use the file is removed and a new start is
  needed. Opening the bare origin without the fragment shows the
  `SESSION_REQUIRED` page ("这个页面不能直接打开").
- For those 60 s the URL is a login credential: do not paste it into logs,
  tickets, commit messages or shared channels. An agent that starts the GUI for
  a user who will open it from another machine gives that user the URL from the
  handoff file directly and promptly in its reply, because it is the only way in.
- `scripts/ops/dev_upgrade_smoke.py apply --gui` (see `scripts/ops/README.md`)
  runs `hyprial gui --no-open --json --stdin-stop` as a child of the script
  against the upgrade's own home and checks the startup record, listener and
  daemon identity without reading the handoff. It stops the GUI by closing its
  stdin when the check ends (a failed GUI check rolls the upgrade back). Before
  it stops the daemon it probes the home with the installed CLI's
  `hyprial gui --port 0 --stdin-stop`, stopped at once (not read-only: a GUI
  host runs briefly and writes a one-time grant file), and refuses with exit 2
  when that answers `GUI_RUNTIME_BUSY`. With
  `--gui-hold-minutes N --ssh-host <host>` it keeps the GUI open for N minutes
  with a login grant of N minutes (at most 10) and prints the tunnel command;
  the GUI closes when the hold ends.

## Development source selection

`hyprial gui dev` and `hyprial gui --live-source` resolve a source in this order:

1. `--gui-home PATH`;
2. a non-empty `HYPRIAL_DEV_GUI_HOME`;
3. the nearest source checkout whose HEAD contains `gui/package.json`; its own `gui/` directory is used in place;
4. the installed wheel manifest's exact `sourceCommit` (not for a release wheel, see below).

Dev ignores `HYPRIAL_GUI_HOME`. Pointing dev at the wheel's `_gui` or at an
extracted asset directory is `GUI_DEV_HOME_INVALID`.

### Installed wheel

The manifest records a full 40-character `sourceCommit` and the trusted
`sourceRemote`. A release wheel records the public repository address there
(`HYPRIAL_BUILD_SOURCE_REMOTE`, see
[wheel distribution](wheel-distribution.md)); a wheel built locally records the
builder's checkout remote and is for local use only. The public mirror receives
snapshot commits and a release wheel's `sourceCommit` is an internal commit it
does not contain, so a release wheel does not support `hyprial gui dev`
(decision 2026-11-16): with no `--gui-home`, no `HYPRIAL_DEV_GUI_HOME` and no
verifiable source checkout, a manifest whose `sourceRemote` is the public
repository is refused at once with `INVALID_ARGUMENT` (this is a release build;
run from a source checkout or pass `--gui-home <path>`; plain `hyprial gui`
works), before any fetch or managed-cache operation (the local source-checkout
discovery still runs first, so a checkout keeps priority). A wheel built locally
from a clone of the public mirror also records the public repository and
therefore also refuses `gui dev` without `--gui-home`; run `gui dev` from that
clone instead. A wheel whose manifest names any other remote (an internal or
local build) continues as follows. The CLI keeps a bare cache at
`<HYPRIAL_HOME>/state/gui-dev/source.git` and a detached worktree at
`<HYPRIAL_HOME>/state/gui-dev/worktrees/<full-sourceCommit>`. It checks the
cache first, then fetches **only that object id** from the recorded remote and
verifies the result. It never substitutes a latest branch or another remote.
An unreachable remote or missing object is `GUI_DEV_SOURCE_MISSING` with the
exact commit and retry instructions. A cache with a different remote, or an
automatic worktree whose HEAD is not the pinned commit, is
`GUI_DEV_SOURCE_CONFLICT`; the CLI does not switch, reset, clean, stash, rebase,
force-checkout, or overwrite it. Existing untracked/modified files are preserved
and reported as dirty.

### Source checkout

When neither explicit home is set and a source checkout is discovered, the CLI
uses `<checkout>/gui` directly and read-only: no worktree, branch, fetch,
checkout, or dependency install. Run `npm --prefix gui ci --ignore-scripts`
once if `gui/node_modules` is missing; without it (also for an explicit dev
home and for the source-checkout normal build) the CLI fails before starting
any child with `GUI_HOME_INVALID`, naming that command and carrying `gui_home`
and `command` in `data`. Nothing is installed automatically. An older `.worktrees/gui-dev-auto`
worktree or `work/gui-dev-auto` branch is no longer used and is never removed by
the CLI; after checking it for uncommitted work, remove it with
`git worktree remove .worktrees/gui-dev-auto` and
`git branch -D work/gui-dev-auto`.

### Explicit worktree

Explicit `--gui-home` and `HYPRIAL_DEV_GUI_HOME` use that project and create
nothing. Inspection is read-only: the CLI verifies Git provenance, real HEAD,
branch, remote, and dirty state, but never runs `checkout`, `reset`, `clean`,
`rebase`, or writes into the selected worktree. The startup record and the
development prompt print those real values.

For the installed-wheel managed worktree only, when `gui/node_modules` is absent
dependencies are installed with:

```sh
npm ci --ignore-scripts
```

The startup/evidence metadata records the command, working directory, and exit
code. The lockfile is not edited, and existing `node_modules` is never
reinstalled or updated automatically. Install failure in the
managed worktree returns `GUI_AUTO_WORKTREE_INSTALL_FAILED` and preserves the worktree scene. Missing
`git`/`npm` uses `GUI_GIT_MISSING`/`GUI_NPM_MISSING`; an installed normal GUI
does not require either tool.

The startup output and human prompt print the real source path, HEAD, branch,
remote, baseline, and dirty state. The suggested update commands are explicit
(`git fetch`, `git status`, `git rebase`), but the CLI never fetches, rebases, or
updates automatically; there is no update subcommand.

For a manually managed isolated visual task, create a sparse worktree so the
task can commit and open a PR without touching the main checkout:

```sh
git worktree add -b work/gui-visual-<task> --no-checkout \
  /path/to/gui-visual-<task> origin/intg/gui-refactor-freshestart
git -C /path/to/gui-visual-<task> sparse-checkout set --cone gui
git -C /path/to/gui-visual-<task> checkout
cd /path/to/gui-visual-<task>/gui
npm ci --ignore-scripts
HYPRIAL_DEV_GUI_HOME="$PWD" hyprial gui dev
```

Use a base that contains `gui/`; do not copy files into a directory without Git
provenance. Automatic discovery never fetches a missing branch, so check out a
candidate PR explicitly only when that is the intended source.

**A Git worktree is not a security sandbox.** OS users, filesystem permissions,
and the repository's access controls still apply. Isolation here means branch
and working-tree separation, not a boundary against malicious code.

## Isolated manual run

For the supported macOS and Linux source checkouts, the repository launcher
wraps this recipe into one command:

```sh
./scripts/build/run-gui-dev-isolated.sh
```

The default is `networked`, which keeps the home, daemon state, sockets and
temporary files private while leaving the real Tailcat forwarding path enabled
for organization work. The launcher fetches `origin/dev`, requires a clean
checkout, runs the locked editable install with the repository's native Rust
engine build enabled, builds the GUI and Tailcat from that checkout, mints a
fresh node id, and starts the official daemon-backed GUI. A machine needs the
repository toolchain prerequisites (including Rust 1.93.1; rustup's standard
`$HOME/.cargo/bin` location is detected automatically); no `HYPRIAL_*` or
provider environment setup is required for the default run.
The new home has no login or organization membership; complete the authorization
in the browser before using organization features. The daemon completes device
registration and initialization before it reports the serving phase. Use
`--network-mode labrelay` for a private two-node test topology or
`--network-mode isolated` for UI-only work. `--no-open`, `--port`,
`--run-root`, and `--go-cache-home` are available for non-default runs; the
older `HYPRIAL_GUI_*` environment variables remain accepted.

The launcher is a POSIX Bash source-development tool. It is tested on macOS
and uses Linux-specific path and permission handling, but Windows is not a
supported target for this workflow.

The following run uses an empty, disposable home. It uses no production home,
daemon, Lark/IdP/provider credential or machine-level settings. Run it from the
source checkout with Node 24 and npm installed; `uv sync --extra test` pins the
checkout's Python entry point. `hyprial gui` ensures the daemon itself: there
is no daemon to start by hand, and the empty home gets a bootstrap daemon, so
the page opens on the login entry.

```sh
cd /path/to/harness-bridge
uv sync --extra test
export HYPRIAL_BIN="$PWD/.venv/bin/hyprial"
if [[ ! -d gui/node_modules ]]; then npm --prefix gui ci --ignore-scripts; fi

export GUI_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/hyprial-gui.XXXXXX")"
chmod 700 "$GUI_ROOT"
umask 077
export HOME="$GUI_ROOT/user"
export HYPRIAL_HOME="$GUI_ROOT/home"
export HARNESS_STATE_DIR="$GUI_ROOT/state"
export XDG_CONFIG_HOME="$GUI_ROOT/xdg-config"
export XDG_STATE_HOME="$GUI_ROOT/xdg-state"
export XDG_CACHE_HOME="$GUI_ROOT/xdg-cache"
export XDG_DATA_HOME="$GUI_ROOT/xdg-data"
export XDG_RUNTIME_DIR="$GUI_ROOT/runtime"
export TMPDIR="$GUI_ROOT/tmp"
export UV_CACHE_DIR="$GUI_ROOT/uv-cache"
export npm_config_cache="$GUI_ROOT/npm-cache"
export HYPRIAL_NODE_ID=gui-demo-node
export HYPRIAL_NETWORK_ISOLATED=1
export HYPRIAL_ZENOH_LISTEN=
export HYPRIAL_ZENOH_CONNECT=
export HYPRIAL_PEER_DISCOVERY=0
export NO_PROXY=127.0.0.1,localhost,::1
export no_proxy="$NO_PROXY"
unset HYPRIAL_OWNER HYPRIAL_BACKEND_ENDPOINT HYPRIAL_GUI_BACKEND_DRIVER \
  HYPRIAL_GUI_PRINCIPAL HYPRIAL_GUI_UPSTREAM_TOKEN HYPRIAL_GUI_LAUNCH_GRANT \
  HYPRIAL_GUI_HANDOFF_PATH HYPRIAL_GUI_AUTH HYPRIAL_GUI_SETUP \
  HYPRIAL_GUI_SETUP_CLI HYPRIAL_GUI_CONTRACT_MOCK_SCENARIO
mkdir -p "$HOME" "$HYPRIAL_HOME" "$HARNESS_STATE_DIR" "$XDG_CONFIG_HOME" \
  "$XDG_STATE_HOME" "$XDG_CACHE_HOME" "$XDG_DATA_HOME" "$XDG_RUNTIME_DIR" \
  "$TMPDIR" "$UV_CACHE_DIR" "$npm_config_cache"
test "$HYPRIAL_HOME" != "$HOME/.hyprial"
```

To get a serving daemon without a login, write a synthetic owner into the
settings before the first launch, as the Docker entry does:
`printf '%s\n' '{"version":1,"owner":"gui-demo-owner"}' > "$HYPRIAL_HOME/settings.json"`.
`hyprial gui` then starts the daemon for that home and admits it by the
daemon's own phase.

Run the normal source entry. It builds the ordinary SPA, ensures the daemon,
and serves `gui/dist`. `--json` prints exactly one startup object and then
remains in the foreground.

```sh
"$HYPRIAL_BIN" gui --no-open --json \
  > "$GUI_ROOT/gui.json" 2> "$GUI_ROOT/gui.log" &
gui_pid=$!
for _ in {1..1200}; do grep -q '"handoff_path"' "$GUI_ROOT/gui.json" 2>/dev/null && break; sleep 0.1; done
grep -q '"handoff_path"' "$GUI_ROOT/gui.json"
handoff="$HARNESS_STATE_DIR/gui/daemon/launch.url"
test -f "$handoff"
launch_url="$(< "$handoff")"
open "$launch_url"
unset launch_url
```

On Linux, replace the macOS `open` command with `xdg-open`; in a headless run,
use browser automation that reads the file and navigates to that URL. Do not
print, `cat`, `echo` or shell-trace the handoff contents. The URL's fragment is
consumed once, scrubbed from browser history/storage, and the Python session host
unlinks `launch.url`. If you do not open it within `HYPRIAL_GUI_LAUNCH_TTL_MS` (default
60 seconds), the file is removed and the launch must be restarted to mint a new
grant.

Stop the GUI with SIGTERM and verify the expected cleanup. The daemon the GUI
ensured must still be alive after the GUI exits; stop it separately.

```sh
kill -TERM "$gui_pid"
wait "$gui_pid" || gui_status=$?
test "${gui_status:-0}" -eq 143
test ! -e "$handoff"

"$HYPRIAL_BIN" daemon status --json
"$HYPRIAL_BIN" daemon stop
rm -rf "$GUI_ROOT"
```

Ctrl-C exits 130 through the same cleanup path. A normal frontend exit or any
owned-child failure also stops the remaining owned children; it never stops the
daemon. The foreground GUI is not a background service and has no
status/stop/upgrade command.

For the dev entry, use the same isolated environment. There is no production build:
the launcher starts the Vite dev server on an ephemeral loopback port with no
`HYPRIAL_GUI_*` variable, waits for its readiness line, and then starts the
Python session host on the public port with `--live-upstream` pointing at it.
The browser only ever sees the host's origin; the host serves `/daemon/*` and
`/__gui/healthz` itself and relays everything else, including the HMR WebSocket
(which must carry a same-origin `Origin`), to Vite:

```sh
"$HYPRIAL_BIN" gui dev --gui-home "$PWD/gui" --no-open --json \
  > "$GUI_ROOT/gui-dev.json" 2> "$GUI_ROOT/gui-dev.log" &
```

The runtime and handoff path are the same as normal mode. `--gui-home` is the
`gui/` project directory; it selects that project explicitly.

For the explicit synthetic Contract Mock, no daemon or real home is required.
It uses the same built/served App, client and `/daemon/*` paths; only the host
upstream changes. `success`, `empty`, `unconfigured` and `error` are the accepted
scenario names.

```sh
HYPRIAL_GUI_BACKEND_DRIVER=contract-mock \
HYPRIAL_GUI_CONTRACT_MOCK_SCENARIO=empty \
  "$HYPRIAL_BIN" gui dev --gui-home "$PWD/gui" --no-open --json \
  > "$GUI_ROOT/gui-mock.json" 2> "$GUI_ROOT/gui-mock.log" &
mock_gui_pid=$!
handoff="$HARNESS_STATE_DIR/gui/contract-mock/launch.url"
```

## Trusted inputs and child-only values

User-provided source-entry inputs are limited to:

| Variable | Meaning |
| --- | --- |
| `HYPRIAL_GUI_BACKEND_DRIVER` | Optional: exactly `daemon` (the default when unset) or `contract-mock`. |
| `HYPRIAL_GUI_CONTRACT_MOCK_SCENARIO` | Contract Mock only: `success`, `empty`, `unconfigured` or `error`; default `success`. |
| `HYPRIAL_GUI_PRINCIPAL` | Optional; if present it must exactly equal the verified canonical `user:<owner>`. It is never accepted from a browser request. The Python session host also receives the verified `HYPRIAL_NODE_ID`. |
| `HYPRIAL_GUI_LAUNCH_TTL_MS` | Optional one-time grant/handoff lifetime, 1000–86400000 ms; default 60000. |
| `HYPRIAL_GUI_SESSION_TTL_MS` | Optional host session lifetime, 1000–86400000 ms; default 12 hours. |
| `HYPRIAL_GUI_MAX_SESSIONS` | Optional live session capacity, 1–1024; default 32. |

Any `HYPRIAL_OWNER` value is forbidden, including one equal to the committed
settings owner. `HYPRIAL_BACKEND_ENDPOINT`, `HYPRIAL_GUI_UPSTREAM_TOKEN`,
`HYPRIAL_GUI_LAUNCH_GRANT`, `HYPRIAL_GUI_HANDOFF_PATH`, `HYPRIAL_GUI_HOME` (normal mode),
`HYPRIAL_GUI_PUBLICATION_PATH`, and the old
auth/setup/reference/preview variables are child-only or retired and are
rejected when inherited from the user environment. The launcher gives its own
children private values and none of them is accepted from the user environment
or from a browser request. With the daemon driver the host gets the launch
grant, the handoff and publication paths, the driver name, the node id and the
home and state directory (`HYPRIAL_NODE_ID`, `HYPRIAL_HOME`,
`HARNESS_STATE_DIR`), and the arguments `--home`, `--state-dir`, `--socket` and
`--node`; it gets no endpoint, no bearer, no authority file and no principal
variable, because it derives the principal from the daemon in its own process.
With the `contract-mock` driver the host gets the mock's endpoint and bearer and
the verified principal instead. The Vite server gets no `HYPRIAL_GUI_*`
variable. The file-based authority (`frontend-authority.json` and the
`HYPRIAL_GUI_AUTHORITY_PATH` variable) no longer exists: the daemon-driver host
receives a new bearer and principal in memory, and the Contract Mock host keeps
the authority it was started with.

Build and dependency-install children do not inherit the launch grant, the
upstream bearer or `HYPRIAL_OWNER`. Contract Mock's synthetic bearer is captured
from its private output, redacted before logging, and never passed in argv.

## Daemon admission

No platform capability preflight runs before admission (owner 2026-11-21,
KP-080): the runtime's single-instance lock is the kernel's `ProcessFileLock`
(flock on POSIX, LockFileEx on Windows), so a platform without `fcntl` or
`os.getuid` is not refused with `GUI_RUNTIME_UNAVAILABLE`. The installed wheel
launch goes to admission with driver `daemon`
by default, and never falls back to a source checkout; `contract-mock` is refused
there with `GUI_MOCK_UNAVAILABLE`.

With the daemon driver the launcher resolves the home, state directory and
derived socket before selecting a source target, installing dependencies,
creating the source runtime or spawning any child, then does the following in
this order:

1. Checks the environment first, before the home is looked at. The driver
   must be exactly `daemon` or `contract-mock` (`GUI_BACKEND_DRIVER`).
   `HYPRIAL_OWNER` is forbidden (`GUI_OWNER_OVERRIDE`). Inherited endpoint,
   credential, grant, handoff, publication and preview variables are refused
   (`GUI_CONFIG_CONFLICT`, `data.variables` lists them).
2. Checks the home. It must exist as a directory (`GUI_HOME_NOT_INITIALIZED`
   otherwise) and be owned by the current account and private (mode `0700` on
   POSIX; on Windows the kernel checks account-private access). A broader
   home is refused with `GUI_HOME_UNSAFE` and `data.home`; the launcher does
   not change permissions, so verify the directory and its ownership before
   correcting them. A directory created by plain `mkdir -p` may not qualify.
3. Checks identity inputs. An explicit
   `HYPRIAL_GUI_PRINCIPAL` must equal the canonical `user:<owner>`, and a home
   without an owner has no principal to match (`GUI_PRINCIPAL_MISMATCH`).
   `HARNESS_SOCKET_PATH` cannot replace the socket derived from the home
   (`GUI_SOCKET_MISMATCH`).
4. Ensures the daemon. If nothing answers, the launcher starts the home's
   daemon through the same launch primitive as `hyprial daemon start`; a running
   daemon is reused. The primitive returns once the daemon answers `ping`,
   which is before restore ends. A start failure is reported with the
   primitive's own code and message. `hyprial gui` never installs or re-homes an
   OS service; an already installed unit of this home still starts the daemon
   through its service manager. This step runs in every home state, a home
   without an owner included, and only after the checks above, so a refused
   invocation (unknown action, `--ip`, `--port`, driver, `--gui-home`,
   platform, identity) starts nothing.
5. Admits by the daemon's own `ping` phase.
   - `bootstrap` (no account yet): admitted as a bootstrap launch while the
     daemon owns the login transition. The launcher checks node, socket, package version and a usable
     catalog, not HTTP readiness: a bootstrap daemon reports its account-bound
     routes as `not-ready` by design. The principal is empty.
   - Otherwise the owner, node, socket, package version and epoch must agree
     between ping and catalog, the phase must be serving or reconciled and not
     restoring, every catalog route must be HTTP-ready and the full local
     method catalog must agree. A restoring daemon is waited for up to 60
     seconds before `GUI_DAEMON_NOT_READY`.
   - A home without an owner whose daemon still does not answer is
     `GUI_DAEMON_UNAVAILABLE`. A stopped daemon and a home without an owner
     are not admission errors by themselves: the launcher starts the daemon.
6. Checks that the chosen port is free after admission. A launch refused with
   `GUI_PORT_IN_USE` has therefore already started the daemon; it stays and the
   next launch reuses it. In a source checkout the daemon is started before
   the GUI project is inspected, so a project that then fails inspection (for
   example no `node_modules`) leaves the started daemon running.

The supervisor contacts the daemon only through the public IPC helpers. It does
not stop, adopt or restart the daemon, and closing the GUI never stops one.
Stop it with `hyprial daemon stop`; the operator guide
([operator-guide.md](operator-guide.md), section 关闭 GUI 不停 daemon) says how to
see and stop it.

## Processes and files

After admission the launcher spawns the Python session host, which attaches to
the daemon and serves the daemon driver in its own process, and waits for the
host's readiness line for up to 60 seconds (`BACKEND_READY_TIMEOUT_SECONDS`; the
Contract Mock driver keeps the 30 seconds of `FRONTEND_READY_TIMEOUT_SECONDS` for
its host). The host spends its start on imports and on building the local
catalog, not on the daemon, so a loaded or small machine needs far longer than an
idle one (about 3 s warm and 7 s cold when idle, 21 s to 51 s cold under heavy CPU
contention). A host that exits is reported at once as `GUI_START_FAILED`; one
that never prints its line ends with `GUI_READY_TIMEOUT` and its process group is
stopped. After 5 seconds the command writes one progress line (`gui-backend-wait`
under `--json`) to stderr so the wait is not mistaken for a hang. A host that ends
on its own with a non-zero status after its ready line is explained once on stderr
(its exit code and the redacted tail of its log; the `gui-host-exited` event under
`--json`); the command still exits with that status and prints no further stdout line. The Vite
child (dev only) gets its own 30 seconds to print its readiness line.

The Python supervisor (the `hyprial gui` command) owns only these children, each
in its own process group:

1. an optional production build (normal source mode only);
2. the Python session host, the one same-origin host that serves the page, the
   session routes and `/daemon/*`;
3. the Vite dev server (`--live-source` only, loopback, no `HYPRIAL_GUI_*`
   variable); and
4. the Contract Mock (`contract-mock` driver only), a separate Node child.

With the daemon driver the session host serves `request_http` in its own
process: the HTTP-to-daemon dispatch is not a second process and has no
listener of its own. `hyprial gui` therefore starts one Python child and there
is one loopback listener, the public address. The launcher starts no
`request_http` child, writes no `server.json` and no `frontend-authority.json`,
creates no `server.lock` and passes the host no bearer; the host holds its
bearer in memory. After start a runtime directory holds `host.log`,
`launch.ready` and `supervisor.lock`, plus `launch.url` for `--no-open`,
`assets.log` for a dev server and `mock.log` for the Contract Mock. The startup
record has `backend_pid: null` and an empty `backend_endpoint` for this driver
(`record_version` stays 1). With the `contract-mock` driver the launcher still
starts the Contract Mock as a separate Node child, records its pid and endpoint,
and the host relays to it. `python -m hyprial.shell.impl.request_http serve`
still exists as a standalone entry for `local-operator` configurations only; given
a `local-gui` configuration it exits 2 with `CONFIG_INVALID`, because the GUI host
serves that principal in its own process. `hyprial gui` does not use it.

The supervisor records the process groups it created. It stops the host first,
then the Vite server, then the Contract Mock if there is one. The Vite server
exiting on its own stops the whole GUI with `GUI_ASSETS_EXITED`. SIGINT,
SIGTERM, a normal exit, readiness failure or any owned-child failure sends
signals only to those groups, waits, escalates, removes the private session
files and releases the runtime lock. It never scans ports, uses `pkill`, kills
by process name, or stops the daemon.

Runtime state is under
`$HARNESS_STATE_DIR/gui/{daemon,contract-mock}` with mode `0700`; the two
drivers have separate directories and supervisor locks. The exclusive
supervisor lock rejects concurrent foreground launches. Private files and
child logs are mode `0600`. The `launch.ready` publication marker and the
no-open `launch.url` are created exclusively with mode `0600`, are not symlinks
and are not reused. Both are removed on grant consumption, expiry or session
cleanup.

The public startup object contains the non-secret GUI URL, derived principal,
PIDs, source provenance and (for no-open) only the handoff path. It never
contains the launch grant or an upstream bearer. The browser receives neither
an upstream origin or bearer nor the launch grant after consumption. It may
receive the public canonical principal in the session response, but cannot
supply or override that identity.

### Startup record

`--json` prints one object. Its keys are `ok`, `record_version` (1), `mode`,
`gui_home`, `git_worktree`, `git_branch`, `url`, `host`, `port`, `hot_reload`,
`live_source`, `dev_tools`, `api_docs`, `backend_endpoint`, `backend_owned`,
`backend_driver`, `backend_principal`, `frontend_pid`, `backend_pid`,
`assets_pid`, `node_version`, `npm_version`, `installed`, `assets`,
`source_commit`, `source_remote`, `auto_worktree`, `dev_prompt`, `business`,
`note`, `handoff_path` and `handoff_note`. The schema is
`contract/cli/gui-startup-record.v1.json`; the record is built by
`src/hyprial/shell/impl/gui/install/report.py`. `backend_principal` is `null`
for a bootstrap launch, `dev_tools` and `api_docs` are optional booleans, and
`live_source` equals `hot_reload`. The Vite port is not part of the record.
`backend_owned` is `false` for the daemon driver (the launcher starts no backend child and never stops the
daemon) and `true` for the contract-mock driver, which launches its own child.

### Session and request behavior

- `POST /daemon/session` with exactly `{"grant":"..."}` consumes the one-time
  grant and issues HttpOnly control/business cookies. A refresh sends `{}` with
  the control cookie and `x-hyprial-view`; it rotates the business session,
  CSRF and transport generation while retaining the same view.
- Every business request carries the business cookie, `x-hyprial-view` and the
  current `x-hyprial-generation`. Mutating catalog methods additionally require
  `x-hyprial-csrf`.
- The host admits only a method present and `callable: true` in the pinned
  catalog; otherwise it returns `404 METHOD_NOT_FOUND` or `409
  METHOD_NOT_CALLABLE` with `dispatch: "not-dispatched"`.
- `x-hyprial-catalog-digest` must equal the session pin when supplied. The host
  forwards its pinned digest even when the browser omitted it, and refuses a
  differing browser value with `409 CATALOG_MISMATCH`.
- State-changing session/request calls require an exact same-origin `Origin`
  and loopback `Host`. Authenticated GET reads may omit `Origin`, but cross-site
  Fetch Metadata is rejected.
- Host refusals use the top-level v1 envelope with `schemaVersion`, `ok`,
  `requestId`, `code`, `message`, `dispatch` and, where applicable, `method`.
  Upstream envelopes and request IDs are relayed without rewriting. A write
  whose response is lost is `dispatch: "unknown"` and is never retried; a read
  lost before a response is `not-dispatched`.
- Request, session and response bodies are bounded to 128 KiB, 8 KiB and 4 MiB
  respectively. Upstream status/auth/catalog drift, invalid response framing,
  oversized responses and transport loss map to explicit host envelopes rather
  than generated success.

Source mode admits every method that the served catalog declares present and
`callable: true`, whether it reads or writes; the browser does not keep a second
write allowlist or a development approval switch. Missing methods fail before
dispatch with `METHOD_NOT_FOUND`, and non-callable methods fail with
`METHOD_NOT_CALLABLE`. The host and daemon remain authoritative for readiness,
principal authorization, CSRF and method-specific admission. A write whose
response is lost keeps `dispatch: "unknown"` and is never retried; its declared
lookup or receipt method, if any, remains the only recovery path. `message.send`
remains registry-inactive. The client uses one generated `GeneratedClient`, one
transport and one `/daemon/*` path.

## Installed wheel boundary

The installed wheel verifies its `hyprial/_gui/release.json` manifest and serves
the built snapshot through the Python session host
(`python -m hyprial.shell.impl.gui.host`). Its launch goes through the same
daemon admission as the source stack, including ensuring the daemon, and the
host serves `/daemon/*` by dispatching to the daemon from its own process with
the principal it derives from the daemon. It
starts no Node/npm/Git child.

- `HYPRIAL_GUI_BACKEND_DRIVER` defaults to `daemon` for the installed target;
  `hyprial gui --json` reports the driver as `backend_driver`. `contract-mock`
  from a wheel returns `GUI_MOCK_UNAVAILABLE` (the mock needs a source tree and
  Node). A blank or unknown value is still `GUI_BACKEND_DRIVER`.
- A stopped daemon and a home without an owner are not admission errors: the
  launcher starts the daemon (see [Daemon admission](#daemon-admission)). A
  home directory that does not exist is `GUI_HOME_NOT_INITIALIZED`.
- A wheel on a platform without POSIX file locks (Windows) is no longer refused
  with `GUI_RUNTIME_UNAVAILABLE`; the runtime lock and `--stdin-stop` are
  portable (owner 2026-11-21 lifted "Windows not scheduled"; the Windows
  real-machine check is pending, known-problems KP-080).
- `/api/gui/v1/*` is not a route (POST is 405, a missing file 404, both host
  envelopes).
- With `--no-open` the one-time grant URL is written to the private
  `launch.url` handoff file; without it the browser is opened on the same
  grant URL, never on the bare origin.

## Login, logout and the daemon

A first login or same-owner reauthentication in the GUI starts one daemon
operation with `until: "serving"`. The daemon performs authorization polling,
identity commit, device registration and initialization. The GUI reads the
operation with `auth.operation.get` and renders the current receipt. The
external authorization website opens separately; the GUI window stays. A lost
response is reconciled by reading the status or receipt, never by replaying a
write.

When the daemon's phase or account changes (login reaches serving or logout),
the principal is installed in place. The GUI process, its listener
and its port keep running, so the browser origin does not change. The new
principal has a new bearer and a new transport generation, and the previous
bearer is refused. The host revokes the business sessions and keeps the original
launcher control and view proof. The open window re-reads its session and
catalog, clears prior account data and stays on the selected page. It does not
open another tab, and `--no-open` produces a private `launch.url` only for the
initial launch. In-flight writes are never replayed; a response that crosses
the switch is reported as outcome unknown.

Logout inside the GUI uses the daemon's `auth.logout`, and a terminal
`hyprial logout` sends that same one request (it starts a daemon when none
runs and does not wait for any exit). It removes only
login-owned artifacts, and the daemon returns to its bootstrap phase in the
same process, and the GUI re-attaches in place. A daemon
started with `daemon run` on an owned home re-enters the bootstrap loop after a
pure logout only when no owner resolves afterwards; if one still does (an
owner in the environment or left in the settings by a partial removal), that
daemon returns and exits with status 0
(`shell/impl/cli/commands/daemon/commands.py`), and the GUI then answers as
described below until a daemon runs. Logout does not sign
out the Casdoor web session. Device and agent credentials and history remain,
and a later login again uses the daemon operation before serving is reported.

When the daemon goes away while the GUI is open (`hyprial daemon stop`, a
crash, or a logout after which an owner still resolves), the GUI stays open. Every request is answered `DAEMON_UNAVAILABLE`
(HTTP 503, not dispatched). Start a daemon with `hyprial daemon start`: the same
GUI works again without a restart, with a new bearer if the daemon returns in
another phase or under another account. Do not run a second `hyprial gui` for
this; a concurrent foreground launch of the same home is refused. If the daemon
cannot be started when `hyprial gui` launches, the command exits with the start
primitive's error, or `GUI_DAEMON_UNAVAILABLE` for a home without an owner.

Recovery in the page has a single 30-second deadline from entering the
transition, covering generation rotation, status checks and the refresh
handshake. Repeated failures do not extend it. Success clears the deadline;
401/403 ends recovery immediately. On timeout the GUI stops polling, ignores
late handshakes, and shows a connection timeout with one recheck action.
Recheck only reloads and reads current state; it never repeats login,
initialization or logout. A connection timeout says nothing about the outcome
of a previously submitted business operation.

The host's `POST /daemon/session/status {}` uses the HttpOnly control cookie,
view proof and current generation to report whether the business session is
current. It exposes no backend token, endpoint or identity override, and it is
not a daemon business method. A change retires the old client before a normal
`POST /daemon/session {}` handshake. The route is specified in
[`contract/gui-session/README.md`](../../contract/gui-session/README.md).

To try a real first login by hand, use an empty isolated home, the daemon
driver and a Tailcat executable available to that home
(`HYPRIAL_TAILCAT_BINARY`) for the device step. The run contacts the real
account authorization website; the isolated network settings only turn off
node discovery. Such a run is not accepted evidence of the account flow.

The canonical capability ledger is
[用户旅程与方法对账](../design/gui-v0.1-method-plan-2026-11-13.md).

## 三种数据源与启动命令

以下三种数据源共用同一套 GUI；fixture 只检查交互，Contract Mock 检查 HTTP 契约，daemon 用于真实接线。
当前能力与缺口以方法对账为准，不能仅比较两套 mock 的字段就证明后端通过。

本节之后只剩纯前端开发预览；Contract Mock 与 daemon 的启动命令在上文“Commands”与“Isolated manual run”两节。

## 纯前端开发预览

```sh
env -u HYPRIAL_GUI_BACKEND_DRIVER -u HYPRIAL_BACKEND_ENDPOINT \
  -u HYPRIAL_GUI_AUTH -u HYPRIAL_GUI_SETUP -u HYPRIAL_GUI_SETUP_CLI \
  HYPRIAL_GUI_DEV_PREVIEW=1 \
  npm --prefix gui run dev -- --host 127.0.0.1 --port 4180
```

打开 `http://127.0.0.1:4180/?hyprialScenario=logged-in-init&hyprialFirstRun=false`。
这是已登录、已初始化、业务为空的前端 mock 起点。头像菜单 → Agent 设置 → 凭据设置/平台接入；
再到 Agent 创建与配置，最后开始聊天。刷新会重置模拟业务数据，不访问真实 daemon/平台。
未登录起点将 `logged-in-init` 换为 `logged-out-init`。

## 开发工具层：`--with-dev-tools`

```sh
uv run hyprial gui --with-dev-tools --port 4181
```

- 这是开发工具层唯一的开关：没有环境变量、URL 参数或存储键能替代它。不带该参数的
  `hyprial gui`（以及原生外壳）不提供工具：`/dev-tools/*`、`/docs-assets/*` 返回 404，`/docs` 只是应用页面。
- 带上后，页面右下角出现“开发”浮动球，展开为模块索引。`hyprial gui dev` 自带该开关。
  源码 checkout 与发布 wheel 都接受这个参数，产物相同。
- 后端驱动是 daemon 且构建带有 Swagger UI 文件时，面板里多一个“API 文档”链接。它在新标签页打开
  会话 host 自己的 `/docs` 页面，沿用当前页面的会话；页面里没有也不需要填写 bearer。
  直接在地址栏打开 `/docs`（没有来源标签页）会回到 `/`。Contract Mock 下没有 API 文档。
- `hyprial gui dev`（live source + 工具层）的 API 文档文件取自所选 `gui/node_modules/swagger-ui-dist`
  （wheel 的受管工作树取自内置 `_gui/dev-tools/docs`）；目录里两个文件不全时照常启动，只是没有文档链接
  （`api_docs` 为 `false`）。
- `--json` 启动记录多两个可选布尔键：`dev_tools`（是否请求了工具层）与 `api_docs`
  （host 是否确认提供文档页）。人读输出多一行 `dev tools`。
- `request_http` 原来自带的 bearer Swagger 页面已删除：它的 `/docs`、`/docs-assets/*` 与任何未知路径
  一样返回 404，原先开启它的两个环境变量不再被读取。API 文档只有上面这一个入口；`/openapi.json` 仍在，
  需要 bearer。

## 源码热更新：`--live-source`

```sh
uv run hyprial gui --live-source --gui-home "$PWD/gui" --port 4181
```

- 页面来自 GUI 源码：launcher 多起一个 Vite 开发服务器（临时 loopback 端口，不持有任何后端变量），
  会话 host 在公开端口前转发它的资源与热更新 WebSocket。浏览器只看到 host 的地址。
- 单独使用时没有工具层：会话不报告 `devTools`，host 对 `/docs`、`/docs-assets/*`、`/dev-tools/*`
  回 404；源码文件本身照常可取（live source 本来就暴露全部源码）。要工具层就再加 `--with-dev-tools`，
  或直接用 `hyprial gui dev`。
- `--gui-home` 与 `HYPRIAL_DEV_GUI_HOME` 只在 live source 下生效；不带 `--live-source`（或 `dev`）时
  传 `--gui-home` 返回 `INVALID_ARGUMENT` 并提示加 `--live-source`。
- 需要 Git、Node 24+ 与 npm；缺失时在启动任何子进程前以 `GUI_GIT_MISSING` / `GUI_NODE_MISSING` /
  `GUI_NPM_MISSING` 失败。发布 wheel 不含 GUI 源码：没有 `--gui-home` 也不在源码 checkout 内时以
  `INVALID_ARGUMENT` 拒绝，普通 `hyprial gui`（可加 `--with-dev-tools`）照常可用。
- `--json` 启动记录多一个可选布尔键 `live_source`（与 `hot_reload` 相同）；不暴露 Vite 端口。
  `mode` 只有在两个开关都开启时才是 `dev`。

## Docker：必须区分当前 PR 与远端 dev

```sh
make accept-gui MODE=dev PORT=4182
```

`accept-gui` 从当前**已提交 HEAD**同时打包前后端，使用容器内合成 owner 和真实 daemon；
适合接线检查，不是空 home 的真实注册。命令输出私有 handoff 文件路径，打开其中的一次性 URL。

`make debug-gui` 则从远端 **dev** 另取 GUI，即使当前 checkout 是 PR，也可能看到旧界面。
本 PR 界面验收不要用它。两种 Docker 命令均不会包含未提交修改，Ctrl-C 删除本次容器及其临时状态。
