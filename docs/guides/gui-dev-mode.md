# Hyprial GUI source mode (`hyprial gui`)

This is the CLI entry for the componentized web GUI. An installed wheel serves
its verified built snapshot; a source checkout keeps the npm build/start path.
It is not the retired bundled GUI and not the final desktop packaging path.

For UI design, component usage, state/credential rules and handoff expectations,
start with the [GUI design and development standard](../../gui/docs/ui-development-standard.zh-CN.md),
the [component catalog](../../gui/docs/component-catalog.zh-CN.md) and the
[UI change handoff template](../../gui/docs/ui-change-handoff-template.zh-CN.md).
These documents separate the current fixture implementation from target
requirements; source and tests remain authoritative.

## Commands

```sh
hyprial gui                 # installed: verified wheel assets; source: npm build + start
hyprial gui dev             # exact source worktree, then npm run dev (Vite)
```

An installed wheel carries a built GUI snapshot under `hyprial/_gui` plus a
`release.json` manifest (full source commit, source remote, package/GUI
versions, and a SHA-256 for every served file). Bare `hyprial gui` verifies and
serves that snapshot with a loopback Python server, so it needs no Git, Node,
npm, source checkout, or `HYPRIAL_GUI_HOME`. A missing, malformed, incomplete,
or tampered snapshot fails closed with `GUI_ASSETS_MISSING`,
`GUI_MANIFEST_INVALID`, or `GUI_ASSETS_INVALID`.

A source checkout without built-in wheel assets keeps the original normal-mode
behavior: the CLI runs the project's `build` script before serving. Build output
goes to stderr when `--json` is used. A failed build exits with
`GUI_BUILD_FAILED` before any backend or frontend child starts.

Only the bare command and the `dev` leaf command are public. `start`, `status`,
`stop`, and `upgrade` are retired and are rejected with `INVALID_ARGUMENT`;
there is deliberately no compatibility shim.

Minimal options:

| Option | Meaning |
| --- | --- |
| `--ip IP` | Frontend bind address; default `127.0.0.1`. An IP literal or `localhost` only. |
| `--port PORT` | Frontend port; default `4173`, range 1–65535. A conflict fails; there is no silent fallback. |
| `--gui-home PATH` | Normal mode: a built GUI directory (`dist/index.html` or `index.html`); a source-only project is `GUI_HOME_INVALID`. Dev mode: an existing Git worktree/project; overrides `HYPRIAL_DEV_GUI_HOME` and disables automatic worktree creation. |
| `--no-open` | Print the URL without opening a browser. Intended for automation. |
| `--json` | Emit exactly one startup record on stdout, then run in the foreground. Child output goes to stderr. |

The browser opens once by default, only after the frontend health endpoint is
ready. `--no-open` suppresses that single call. If the desktop opener fails, the
URL is still printed and the already-started, recorded session remains under
the CLI's control; no second service is started.

`--json` format is one object with `ok`, `mode`, `gui_home`, `git_worktree`,
`git_branch`, `url`, `host`, `port`, `hot_reload`, `backend_endpoint`,
`backend_owned`, PIDs, Node/npm versions, the explicit business status,
`installed`, `source_commit`, `source_remote`, `dev_prompt`, and
`auto_worktree` (an object in automatic dev mode, otherwise `null`). The
`auto_worktree` object records `source`, `path`, `branch`, `base`, `created`,
`managed`, `dirty`, `remote`, and the dependency
`install` command/cwd/exit/reason. The command exits with the frontend's exit
status, 130 on Ctrl-C, or 143 on SIGTERM; it does not print a second JSON value
after the long-running process ends.

## Development source selection

`hyprial gui dev` resolves a source in this order:

1. `--gui-home PATH`;
2. a non-empty `HYPRIAL_DEV_GUI_HOME`;
3. the nearest source checkout whose HEAD contains `gui/package.json`;
4. the installed wheel manifest's exact `sourceCommit`.

`HYPRIAL_GUI_HOME` is the normal GUI variable and is never used as a dev
source. Both variables may be set to different paths; they are independent. Pointing dev at
the same path as the normal home, at the wheel's `_gui` assets, or at an
extracted asset directory is `GUI_DEV_HOME_INVALID`.

### Installed wheel

The manifest records a full 40-character `sourceCommit` and the trusted
`sourceRemote`. The CLI keeps a bare cache at
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
creates or restores one stable linked worktree:

- path: `<source>/.worktrees/gui-dev-auto`
- branch: `work/gui-dev-auto`
- sparse checkout: cone mode containing only `gui`
- base: the source checkout's current HEAD (no fetch, rebase, or branch guess)

First creation runs exactly:

```sh
git -C <source> worktree add -b work/gui-dev-auto --no-checkout \
  <source>/.worktrees/gui-dev-auto <source-HEAD>
git -C <source>/.worktrees/gui-dev-auto sparse-checkout set --cone gui
git -C <source>/.worktrees/gui-dev-auto checkout
```

The automatic path is inside the source checkout, so it may appear as
`.worktrees/` in the source's untracked status. The source's HEAD, branch, and
existing modified/untracked files are not changed. When the same command runs
later, or from inside that auto worktree, the CLI reuses the same path; it never
nests another worktree. Restore validates that the path is its own worktree of
the same Git repository, is on exactly `work/gui-dev-auto`, has sparse list
exactly `gui`, and contains `gui/package.json`. Any failed validation is a
`GUI_AUTO_WORKTREE_CONFLICT`; nothing is deleted, moved, switched, or reused.

### Explicit worktree

Explicit `--gui-home` and `HYPRIAL_DEV_GUI_HOME` use that project and create
nothing. Inspection is read-only: the CLI verifies Git provenance, real HEAD,
branch, remote, and dirty state, but never runs `checkout`, `reset`, `clean`,
`rebase`, or writes into the selected worktree. The startup record and the
development prompt print those real values.

Whichever dev source is selected, when `gui/node_modules` is absent dependencies
are installed with:

```sh
npm ci --ignore-scripts
```

The startup/evidence metadata records the command, working directory, and exit
code. The lockfile is not edited, and existing `node_modules` is never
reinstalled or updated automatically. Install failure returns
`GUI_AUTO_WORKTREE_INSTALL_FAILED` and preserves the worktree scene. Missing
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

## Process and endpoint contract

### Installed wheel normal mode

The installed path verifies `hyprial/_gui/release.json` and every listed file,
then starts only a loopback Python static server:

```sh
python -m hyprial.shell.impl.gui.install.server \
  --root <verified-assets> --host 127.0.0.1 --port 4173
```

`GET /__gui/healthz` returns 200 JSON with `assets: "installed"`. A missing SPA
route falls back to the verified `index.html`. It starts no npm, Node, Git, or
placeholder-backend child and does not require a source checkout. Business
requests under `/api/gui/v1/*` still return HTTP 501 with
`X-Hyprial-Backend: not-wired` and the exact message:

```text
not implemented: hyprial backend not wired
```

The static server exits 130/143 through its SIGINT/SIGTERM handlers, closes its
listening port, and is reclaimed by the launcher's normal child cleanup.

### Source mode (normal and dev)

For a source checkout, the CLI starts an independent Node placeholder service
before the frontend:

```sh
npm run stub -- --host 127.0.0.1 --port 0
```

The stub prints exactly one line beginning `HYPRIAL_STUB_READY ` with
`{service:"hyprial-gui-stub", host, port, endpoint}`. A redundant `"ok":true`
is accepted when present (and `"ok":false` is rejected), and additional fields
are ignored. The launcher parses that line, checks `GET /healthz`, and passes
the resulting loopback endpoint as `HYPRIAL_BACKEND_ENDPOINT` to the frontend.
Port `0` lets the OS allocate a separate port, so the frontend and backend
cannot collide.

Frontend commands and readiness are:

```sh
npm run build                                              # source normal mode only
npm start -- --host 127.0.0.1 --port 4173 --strict-port    # source normal mode
npm run dev -- --host 127.0.0.1 --port 4173 --strict-port  # source dev mode
GET /__gui/healthz   # 200 JSON when the page server is ready
```

The source UI reads `HYPRIAL_GUI_HOME` and `HYPRIAL_BACKEND_ENDPOINT` through
Vite's `loadEnv(mode, root, '')` and `process.env`. Browser requests are
same-origin `/api/gui/v1/*`; the frontend proxies them to the stub. Setting an
environment variable in a shell is not enough if the UI does not read it—the UI
owner's candidate must provide that wiring. In dev mode the child environment's
`HYPRIAL_GUI_HOME` is the selected development source, never the normal-mode
wheel asset directory.

All source-mode business paths and methods other than the stub health endpoints
return HTTP 501 with `X-Hyprial-Backend: not-wired` and the same exact
not-implemented message. Credentials, tokens, and keys are never echoed. There
is no fake login, reply, online state, or success result in this batch. The
normal and `dev` commands use the same UI and the same placeholder boundary.

If `HYPRIAL_BACKEND_ENDPOINT` is already set to a plain loopback base URL, the
CLI uses it and does not start another stub. That is a controlled test seam; it
does not make the endpoint a production service.

## Lifecycle and failure behavior

- Installed normal mode verifies the built-in manifest and all hashes, then
  checks the frontend port before starting the static server; it does not check
  Node, npm, Git, or package scripts. Source mode checks Node/npm, `package.json`,
  the requested npm script, Git provenance, automatic worktree state, and the
  frontend port before the frontend starts.
- Missing Node/npm, a non-Git/copy GUI_HOME, a missing script, or a port conflict
  fails with a specific error code (`GUI_NODE_MISSING`, `GUI_NPM_MISSING`,
  `GUI_NOT_GIT_WORKTREE`, `GUI_SCRIPT_MISSING`, `GUI_BACKEND_SCRIPT_MISSING`,
  `GUI_PORT_IN_USE`). Missing, invalid, or tampered installed assets fail with
  `GUI_ASSETS_MISSING`, `GUI_MANIFEST_INVALID`, or `GUI_ASSETS_INVALID`;
  explicit normal homes fail with `GUI_HOME_INVALID`; an invalid dev home fails
  with `GUI_DEV_HOME_INVALID`. Automatic discovery/worktree failures use
  `GUI_AUTO_WORKTREE_SOURCE`, `GUI_AUTO_WORKTREE_CONFLICT`,
  `GUI_AUTO_WORKTREE_FAILED`, or `GUI_AUTO_WORKTREE_INSTALL_FAILED`. Installed
  exact-SHA source failures use `GUI_DEV_SOURCE_INVALID`,
  `GUI_DEV_SOURCE_MISSING`, `GUI_DEV_SOURCE_CONFLICT`, or
  `GUI_DEV_SOURCE_FAILED`; missing Git uses `GUI_GIT_MISSING`. No frontend child
  is started after a preflight failure. A source normal-mode build that fails
  returns `GUI_BUILD_FAILED`; no child is left running.
- The termination guard is installed before repository discovery, automatic
  worktree creation, installed exact-SHA fetching, and dependency installation,
  as well as around the blocking browser opener and the foreground wait. SIGTERM
  during startup maps to exit 143; SIGINT maps to 130. Discovery/install
  subprocesses that support interruption are terminated by their launcher, and a
  partial auto worktree is preserved rather than cleaned. No startup JSON is
  emitted if the session was not established.
- The source-mode frontend and stub each run in their own process group. The
  installed static server runs in the frontend group with no stub. Ctrl-C,
  SIGTERM, or a normal exit sends a signal to those groups, waits, then
  escalates, so npm and its Node/Vite descendants are reclaimed. SIGTERM is
  converted to cleanup control flow on the main thread and exits 143 after
  `stop()` completes. The launcher never discovers or stops processes by name
  or port and never touches another GUI instance.
- On Windows the tree is reclaimed with `taskkill /T` for the recorded PID. If a
  wrapper has already exited and left an unrelated orphan, the launcher cannot
  find that orphan by port; report it rather than killing by process name.
- The environment is passed only to child processes; the parent shell's
  environment is not modified.

## Verification status

The Python tests cover command/option validation, child environment
construction, exact npm argv, Git-worktree inspection without writes, stub
readiness parsing, port conflicts, lifecycle ordering/cleanup, and
process-group isolation. The focused suites are
`tests/shell/gui/lifecycle/test_gui_launcher.py`,
`tests/shell/gui/lifecycle/test_gui_auto_worktree_wiring.py`,
`tests/shell/gui/lifecycle/test_gui_signals.py`,
`tests/shell/gui/lifecycle/test_gui_auto_worktree.py`, and
`tests/shell/gui/installed/test_installed_gui.py`; the GUI suite includes a
real-pipe regression proving the startup record is readable while the
foreground session is still waiting, and real-child SIGTERM/SIGINT regressions
for a running session, a blocking opener before startup JSON, and a blocked
pre-startup plan. The auto-worktree suite uses real temporary Git repositories
to cover first creation, stable restore, dirty/untracked preservation, source
HEAD/branch preservation, sparse list/materialization, branch/path/repository/
symlink conflicts, install skip/failure/retry, and reuse without nesting. The
installed suite uses real temporary Git repositories and a bare fixture remote
to cover wheel-manifest full-SHA/tamper checks, verified built-in assets without
Node or Git inspection, explicit-home errors, dual environment variables,
exact-SHA fetch-only bootstrap, dirty preservation, HEAD-mismatch refusal,
offline missing-object diagnostics, normal-home/asset-directory rejection, and
the Python static server's health, 501 boundary, and port release. The npm
install is fake in these Python tests; a real automatic-worktree `npm ci` plus
dev startup acceptance is recorded by the independent integration run, not by
this unit suite.

On 2026-10-27 the combined CLI plus merged freshestart GUI
(`intg/gui-refactor-freshestart` at `b82c0b35`) was exercised with actual npm
commands: a clean worktree built its production assets before normal startup,
`npm start` served the built page with HTTP 200, `npm run dev` served Vite, a
real WebSocket connection received a full reload after a source edit (file
restored, worktree clean), the same-origin business POST returned the 501
not-wired response in both modes, and a second normal launch on an occupied
port failed with `GUI_PORT_IN_USE` instead of falling back.

Independent Linux verification at `518abf129` confirmed normal and `dev` SIGINT
exit 130 with recorded child-process reclamation and closed ports. It also
found that direct SIGTERM exited 143 without running `session.stop()`, leaving
the recorded children alive; the foreground termination guard and real-child
regression above are the fix for that gap. The final exact-head combined rerun
belongs to the independent integration owner. The early test leftovers on
ports 4181/4182 belong to the operator-supported cleanup path; they are not
treated as acceptance evidence.
