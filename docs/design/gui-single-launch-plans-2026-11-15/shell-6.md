# PR 6 plan — the minimal native shell (macOS arm64 first)

- Base read: `dev` @ `9932b2b63` (detached worktree the plan-6 worktree, removed after planning). Date 2026-11-15.
- Authority: `docs/design/gui-single-launch-2026-11-14.md` §4, §4.1, §4.3, §4.4, §5, §7a; `contract/gui-session/README.md`; `docs/design/gui-single-launch-plans-2026-11-15/host-2-3.md` §0, A.1, C.3, G, I.
- Planning only. Nothing was edited, built or tested in any worktree. No test suite and no build was run.
- Marks: **[R]** read from source at the cited line; **[W]** looked up on the web on 2026-11-15 (sources in §J); **[P]** small probe run in this sandbox; everything about macOS, Windows and a real webview is **UNKNOWN** until run on that platform.
- At `9932b2b63` PR 2b/2c/3 are not merged: the wheel still starts the 501 static server. Every statement about "the record the shell reads" is the record *after* PR 2c.

## 0. Findings that shape the plan

| # | Finding | Evidence |
|---|---|---|
| F1 | Nothing of the old shell is left in the tree: no `clients/`, no `packaging/`, no desktop workflow. The deletion commit is `4796a6f80` (397 files, −117,170 lines). The old shell was built locally; no `desktop-*.yml` workflow existed at its parent | [R] `git show --shortstat 4796a6f80`; `git ls-tree 4796a6f80^ .forgejo/workflows` |
| F2 | The old shell was 131 Rust files (2.29 MB of source), Tauri `=2.12.1`, plugins single-instance / deep-link / updater, a Rust HTTP host (hyper), a Go mobile bridge and its own setup, account, onboarding and update pages | [R] `4796a6f80^:clients/native/src-tauri/Cargo.toml`, `tauri.conf.json` |
| F3 | The shared GUI opens the login page with `window.open('about:blank', '_blank')` and `target="_blank"` links. The old shell denied new windows, so login did not open a browser (KP-056, open) | [R] `gui/src/features/onboarding/impl/useFirstRunController.ts:201-210`; `useAccountSettings.ts:187`; `docs/reference/known-problems.md:560-572` |
| F4 | A served home needs a running daemon **of the same package version** as the `hyprial` that runs `hyprial gui`; otherwise `GUI_DAEMON_UNAVAILABLE` or `GUI_DAEMON_CATALOG` | [R] `gui/backend/admission.py:212-221, 285-292, 339-342` |
| F5 | A second `hyprial gui` on the same home and driver is refused with `GUI_RUNTIME_BUSY` (flock on `<state>/gui/<driver>/supervisor.lock`). There is no "attach" and no way to get a second grant from a running host | [R] `gui/backend/source.py:118-160`; `contract/gui-session/README.md:76-97` |
| F6 | A view lives at most `sessionTtl` (12 h) from its grant; refresh never extends it. Sessions are in memory | [R] `contract/gui-session/README.md:120-135` |
| F7 | The GUI keeps one value in `localStorage` (the Squire identity store). `localStorage` is per origin, so a new port on every launch loses it | [R] `gui/src/app/capabilities.tsx:144-148` |
| F8 | `hyprial gui` has no parent binding and no stdin stop. `request_http` has both a stdin stop and a precedent text for it ("Windows has no SIGTERM, so the shell keeps the launch stdin pipe open") | [R] `gui/backend/lifecycle/process.py:102,341,408` (`stdin=DEVNULL`); `request_http/__main__.py:14-24` |
| F9 | An existing gate already scans `clients/native/src-tauri/src/**/*.rs` for process spawns and has an empty, count-bounded exception registry | [R] `scripts/ci/check_daemon_request_architecture.py:59,72-76,593-632,689` |
| F10 | `./VERSION` is `0.5.5.4` (four parts). Tauri's `version` must be semver | [R] `VERSION`; [W] Tauri config schema |
| F11 | This sandbox has Rust 1.97, cargo, xvfb, uv 0.11.32, but **no** WebKitGTK development package | [P] `pkg-config --modversion webkit2gtk-4.1` → not found |
| F12 | Runners named in workflows: `release-macos-arm64` and `macos-host` (the Mac Studio, host mode), `release-ci`, `release-linux-arm64`, `ubuntu-latest`, `ci-light`, `publish-github`. There is no Windows runner and no macOS x86-64 runner | [R] `native-conformance.yml:45,125,284,426,581`; `e2e-macos-credentialed.yml:65`; `kp027-native-candidate.yml:30-34` |

---

## A. Inventory

### A.1 What remains from the old shell and packaging line

| Item | State | Evidence | Disposition |
|---|---|---|---|
| `clients/native/`, `packaging/clients/` | absent | `ls` | rebuilt by PR 6 |
| `.forgejo/workflows/*desktop*` | absent; never existed at `4796a6f80^` | F1 | new workflow in PR 6 |
| `scripts/build/` | `build-wheel.sh`, `build_native_engine.sh`, `manylinux_native_evidence.sh`, `docker/` — none is desktop packaging | `ls scripts/build` | reused (wheel) |
| `src/hyprial/shell/impl/desktop_network.py` (126 lines) | no product caller; exported from `shell/interface.py:9,26,62`; one test | `git grep desktop_network` | **6-pre: delete** |
| `tests/integration/desktop/test_desktop_network.py` (195) | tests only the module above | line 10 | **6-pre: delete** |
| `tests/integration/desktop/test_desktop_sdk_bootstrap.py` (28) | tests `sdk_worker_command` with `HYPRIAL_BUNDLED_SDK_BOOTSTRAP` (`daemon/impl/harnesses/agent_sdk/__init__.py:248`). The variable was set by the old payload | — | not in 6-pre (daemon code; owner of the harness decides). Move the file out of `desktop/` only if the directory is deleted |
| `HYPRIAL_BUNDLED_NODE` (`shell/impl/onboarding/harnesses.py:208`) | same origin | — | not in 6-pre; listed for PR 7 |
| `contract/native-host/native-host.v1.json` (34) | contract of the deleted Rust host ↔ `request_http` | file | PR 7 (host plan §F), or "6-pre-b" if the owner wants it now (OD-12) |
| `request_http` `nativeHost` config + external promotion | `config.py:39-42,170-172,289-312,452-484`; `__init__.py:51-63`; `bootstrap/external.py` (270); `catalog.py:218`; no writer of `nativeHost` anywhere in `shell/impl/gui` or the CLI | `git grep nativeHost` | same as above |
| `hostContract` / `hostContractVersion` in `server.json` | published in `contract/http-request/http-contract.v1.json:185-223`; `request_http/server.py:303-304,584-585` | — | same; removing it is a contract change |
| `tests/shell/request_http/launch/test_external_promotion.py` (433), `test_launch_contract.py` (380), `test_config.py:115-142` | tests of the two rows above | — | same |
| `scripts/ci/check_structural_limits.py:35,40` | optional roots `src/gui`, `app/src`, `app/src-tauri`; none exists; `clients/native` was never a root | file | PR 6 replaces `app/*` with `clients/native` |
| `tests/tooling/architecture/limits/test_structure_limits.py:94,130-164` | tests of those roots | — | PR 6 |
| `scripts/ci/check_daemon_request_architecture.py:59` `NATIVE_ROOT` | points at `clients/native/src-tauri/src`; scanner alive, registry empty | F9 | PR 6 uses it |
| `.forgejo/workflows/gate-python.yml:151` | comment "Product, Tauri and tests" | — | 6-pre (one word) |
| `.agents/skills/gui-workflow/SKILL.md:79-213` | describes `app/src-tauri`; header (line 14) says deleted | — | PR 7 |
| `AGENTS.md:24,108-111`, `CONTRIBUTING.md:16`, `src/hyprial/skills/hyprial-ops/SKILL.md:209` | say the shell is deleted and will be rebuilt | — | PR 6 updates the layout sentences |
| `tests/tooling/ci/acquisition/test_ci_dependency_caches.py:32-77` | frozen inventory of historical `desktop-*.yml` contexts | — | leave (frozen by design) |
| `docs/reference/known-problems.md` KP-056 | open; names deleted files | F3 | PR 6 re-tests and updates the entry |
| `gui/src/features/onboarding/impl/auth-actions.ts:127-129` `openAuthorization` | optional port "a native host owns the system-browser opener" | — | not wired by PR 6 (no IPC); see D.2 step 6b-5 |

### A.2 What the old shell did (from history; reusable assets)

| Topic | Old value | Reuse |
|---|---|---|
| Config | `productName` "Hyprial Desktop Prototype"; `identifier` `org.hyprial.desktop.prototype.test.a064nodefix20261111`; `version` 0.1.3; `frontendDist: "ui"`; `withGlobalTauri: false`; CSP with `connect-src ipc:`; targets `app, dmg, nsis` | identifier and name are test values; choose new ones (OD-6) |
| Signing | `bundle.macOS.signingIdentity: "-"`, `hardenedRuntime: false`; `packaging/clients/signing/README.md`: internal packages unsigned, credentials never supplied, ephemeral keychain procedure, Windows options | the README text is the starting point for `packaging/clients/README.md` |
| Updater | plugin present, `endpoints: []`, `pubkey: ""` — never configured | nothing to reuse |
| Deep link | scheme `hyprial` | deferred |
| Windows | NSIS `installMode: currentUser`, installer hooks and a PowerShell preflight | deferred |
| Icons | `clients/native/src-tauri/icons/icon.icns` (878,949 B), `icon.ico` (51,268 B), `icon.png` (738,635 B); `setup-ui/assets/hyprial-mark.svg` | **reuse**: `git show 4796a6f80^:clients/native/src-tauri/icons/icon.icns > …` |
| Capabilities | one local capability per window, each granting commands (`allow-app-core-call`, …) | none is reused: the new shell has no command |
| Payload | PBS Python + wheel + Node + harness components staged by ~40 `.cjs` scripts, payload lock, size check, release manifests r5–r15 | not reused; the idea "pin by URL + SHA-256" is |
| CI | none; local builds on a "Mac probe" | new |

### A.3 What exists for the target platforms

| Platform | hyprial wheel (KP-027) | Runner | GUI runtime | Verdict for PR 6 |
|---|---|---|---|---|
| macOS arm64 | complete wheel built and receipted on the Mac Studio (`kp027-native-candidate.yml:19,1180`); tag `macosx_13_0_arm64` (ADR line 30) | `release-macos-arm64`, `macos-host` | POSIX locks present | **in scope** |
| Linux x86-64 | manylinux_2_17 wheel assembled on `release-ci` (`kp027…yml:14-18,326`) | `release-ci`, `ubuntu-latest` | POSIX locks present | launcher tests here; package deferred to a follow-up (6d) |
| Windows x86-64 | no build + import host (`kp027…yml:30-33`) | none | `GUI_RUNTIME_UNAVAILABLE` (no `fcntl`, `platform_support.py:16-33`) | **BLOCKED** three times: wheel, runner, lock port |

---

## B. Options

### B.1 What every option must do (derived from the principle and the session contract)

1. Start `hyprial gui --no-open --json` as a child and read one record. No Python in the shell process.
2. Read the launch URL from the 0600 handoff file named by the record; never log it.
3. Show that URL in a window whose main frame can only stay on that origin.
4. Give the page no native bridge.
5. Send `window.open` / `target=_blank` to the system browser (F3).
6. On quit, stop the child and wait for it; on child exit, tell the user.
7. One instance per user.

### B.2 Comparison

| | A. Tauri v2 | B. wry + tao | C. no shell (launcher + default browser) | D. pywebview process |
|---|---|---|---|---|
| Our code | ≈ 450 lines Rust launcher (no Tauri), ≈ 180 lines Tauri glue, 1 static page, 1 config | ≈ 450 + ≈ 300 Rust, plus our own bundling: `Info.plist`, `hdiutil`, `codesign`, `notarytool`, NSIS/WiX script, AppImage recipe (≈ 300–500 lines per platform) | ≈ 30 lines of script per platform | ≈ 150 lines Python + a per-platform launcher + our own bundling as in B |
| Toolchain | Rust, `tauri-cli` 2.12.2, Xcode CLT; Linux: `webkit2gtk-4.1`; Windows: WebView2 | Rust; same system webviews | none | Python + pyobjc (macOS), pythonnet + WebView2 (Windows), GTK or Qt (Linux, must be chosen) [W] |
| Current versions | tauri 2.12.3, bundler 2.10.2 (2026-10-10) [W]; ≥ 2.11.1 required by design §4.3 | wry 0.57.2, tao 0.37.1 [W] | — | pywebview 6.1 [W] |
| Native bridge to the page | present in Tauri, **refused by default** for remote origins: "By default the API is only accessible to bundled code"; remote needs `remote.urls` in a capability [W]. PR 6 ships zero capabilities and zero commands | **absent** unless `with_ipc_handler` is called [W] | none | `window.pywebview` is injected; whether it can be fully removed is UNKNOWN |
| Navigation guard | `on_navigation` | `with_navigation_handler` [W] | none (a real browser) | limited events; UNKNOWN |
| New window → system browser | `on_new_window` (tauri-runtime ≥ 2.8) [W] | `with_new_window_req_handler` [W] | native | setting exists; granularity UNKNOWN |
| Single instance | `tauri-plugin-single-instance` | ours | the CLI's `GUI_RUNTIME_BUSY` only | ours |
| Bundles | `.app`, `.dmg`, NSIS/MSI, AppImage, deb, rpm from one config [W] | ours (or `cargo-packager`, maintenance UNKNOWN) | a script `.app` still needs a bundle | ours, or briefcase / PyInstaller |
| macOS signing + notarization | built in: `APPLE_SIGNING_IDENTITY`, `APPLE_CERTIFICATE`, `APPLE_API_KEY`… [W] | by hand | still needed for any downloaded `.app` | by hand |
| Auto-update, tray, deep link (design §4 keeps them for later) | plugins | ours | impossible / n.a. | ours |
| Same behaviour as the browser | same engine family as Safari / Edge / Epiphany; differences listed in B.4 | same | identical | same as A, plus toolkit differences |
| Window | yes | yes | **no** (design §4 keeps "窗口、托盘、单实例") | yes |
| Verdict | **recommended** | fallback if the Tauri bridge is judged unacceptable | rejected: no window, no lifecycle tie, still needs a signed bundle and a bundled Python | rejected: three GUI toolkits, an injected bridge, a second Python environment |

### B.3 Recommendation: A, reduced to one window and no command

Reasoning against the owner's principle ("client 只是最简化封装"):

1. The code we own is the smallest for the whole target set. B removes a dependency but makes bundling, signing, notarization, the installer and (later) the updater our code on three platforms. The principle is about what the client *does*, and A and B do exactly the same at run time.
2. The design's acceptance conditions (§4.3 items 4–6) are already written in Tauri terms, and items the design keeps for later (tray, deep link, update, signing) are Tauri plugins, so no second rewrite.
3. The launcher logic lives in a crate with **no Tauri dependency** (`clients/native/launcher`). If the owner later prefers B, only the ≈ 180-line glue crate changes.
4. C is the literal reading of "the same as the browser", and it is what `hyprial gui` already is. It is not a client.

Shape of A:

```
clients/native/
  Cargo.toml              workspace: launcher, src-tauri (standalone; not part of crates/)
  Cargo.lock
  launcher/               lib `hyprial_shell_launcher`: no tauri, no webview
    Cargo.toml            deps: serde, serde_json, url, libc (unix)
    src/lib.rs            public face
    src/record.rs         parse + validate the startup record and the failure line
    src/handoff.rs        read launch.url (owner, mode, size, origin, fragment)
    src/origin.rs         same_origin(url, origin); external-link classification
    src/child.rs          spawn, stdout reader, stop (stdin close → SIGTERM → SIGKILL of the group), exit status
    tests/                fake-launcher integration tests (sh scripts written to a temp dir)
  src-tauri/              bin `hyprial-desktop`
    Cargo.toml            deps: tauri =2.12.3, tauri-plugin-single-instance, open (system browser)
    build.rs
    tauri.conf.json       no windows, no capabilities, no plugins config, frontendDist "ui"
    src/main.rs           setup → spawn → window → guards → quit handling (target ≤ 200 lines)
    ui/index.html         static status page: "starting" / error code + message read from location.hash; no script import, no IPC call
    icons/                the three recovered icons
packaging/clients/
  README.md               build, signing (credentials outside the repo), Gatekeeper notes
  runtime-lock.json       PBS URL + SHA-256 per target, hyprial wheel tag per target
  stage_runtime.py        stages PBS + the platform wheel + locked deps into src-tauri/runtime/
  verify_runtime.py       checks the staged tree (interpreter runs, `hyprial version --json`, no symlink, manifest)
tests/tooling/release/…   pytest for the two scripts (Linux)
```

Run-time sequence of the shell (all of it):

1. Single-instance: a second start focuses the window and exits.
2. Window 1 opens on the bundled `ui/index.html#starting`.
3. Launcher command: `HYPRIAL_SHELL_LAUNCHER` if set (tests, development), else `<resources>/runtime/bin/python3.12 -I -m hyprial.cli`. Arguments: `gui --no-open --json --port 0 --stdin-stop`. Child: own process group, `stdin` = a pipe the shell keeps open, `stdout` = pipe, `stderr` = `<app log dir>/launcher.log` (0600).
4. Read stdout line 1 (bounded: 64 KiB, 60 s). `ok:false` or exit → status page with `code` and `error`.
5. Validate the record (§C.2), read the handoff (§C.3), navigate the same window to the launch URL.
6. Guards: main-frame navigation allowed only to the record's origin and to the bundled status page; `window.open`/new-window with an `http(s)` URL of another origin → system browser, everything else denied.
7. Child exits while the window is open → status page with the exit code or the failure line.
8. Quit or last window closed → close the stdin pipe; after 8 s SIGTERM to the group; after 7 s more SIGKILL; then exit.

### B.4 What could differ from a browser, per engine (all UNKNOWN until measured; expectation given)

| Topic | Contract rule | WKWebView (macOS) | WebView2 (Windows) | WebKitGTK (Linux) |
|---|---|---|---|---|
| Top-level load of `http://127.0.0.1:<port>/#grant=…` | Host must be loopback with the listener's port; GET may omit `Origin`; `Sec-Fetch-Site` absent, `none` or `same-origin` (§2) | expected `none` or absent → pass. ATS is expected not to apply to IP literals (from memory, not looked up; §J); verify | Chromium: `none` → pass | expected as WKWebView |
| Fragment | client removes it with `history.replaceState` before any request (§3) | standard | standard | standard |
| `POST /daemon/session` from the page | `Origin` equal to the page origin, exact string (§2) | WebKit sends `Origin` on same-origin POST; expected pass | pass | expected pass |
| `Sec-Fetch-Site` on fetch | `same-origin` | Safari ≥ 16.4 sends it; an older WebKit omits it → still pass (rule is "when present") | pass | version dependent → pass either way |
| Cookies `Path=/daemon; HttpOnly; SameSite=Strict`, no `Secure`, HTTP | sent on same-origin fetch | expected pass. A report of a `SameSite=Strict` cookie on `127.0.0.1:<random port>` not being sent concerns a cross-site *initiated* navigation [W]; here the cookie is only needed on fetches made by the page itself. Verify | pass | expected pass |
| `sessionStorage` (view proof) | survives reload in the same view (§4) | per webview; expected pass | pass | pass |
| `localStorage` | not in the contract; F7 | persistent data store keyed by origin → lost when the port changes (OD-4) | same | same |
| `window.open('about:blank')` then assignment | not in the contract; F3 | handler denies → `null` → the GUI's link fallback (one extra click compared with a browser) | same | same |
| File input, clipboard | `ConfigDialogs.tsx:203`, `DiagnosticBox.tsx:29-35` | native picker expected; `navigator.clipboard` needs a secure context — loopback HTTP is one in WebKit; verify | pass | verify |
| Tauri bridge object | "A native shell is a browser and nothing more … no IPC" (§1) | `window.__TAURI_INTERNALS__` exists; every `invoke` must be refused. Verify with a probe page | same | same; note [W]: on Linux Tauri cannot tell an iframe from the window |
| 12 h session end (F6) | view ends | the page shows its bootstrap error; the user restarts the app (OD-9) | same | same |

---

## C. Launcher contract the shell relies on

### C.1 Requirements on the host line (must be merged before PR 6)

| ID | Requirement | Provided by | State at `9932b2b63` |
|---|---|---|---|
| R1 | Wheel `hyprial gui` goes through admission, `request_http` and the Python host; no static 501 server | PR 2c (2c-1…2c-4) | not merged |
| R2 | Driver defaults to `daemon` when `HYPRIAL_GUI_BACKEND_DRIVER` is unset (the shell sets no backend variable) | PR 2c, OD-2 | not merged; today mandatory (`admission.py:90-104`) |
| R3 | With `--no-open` the launch URL is written to `<runtime>/launch.url` (O_EXCL, 0600) and `launch.ready` marks publication; the record names it in `handoff_path` and never contains the grant | PR 2c-3; `source.py:652-705`; contract §3 | exists for the source path |
| R4 | The host child binds, listens, then reports the bound port; port 0 works in the child | PR 2b-7, C.3 | not merged |
| R5 | A non-loopback `--ip` is refused | PR 2c, OD-5 | today accepted (`config.py:90-101`) |
| R6 | The host removes `launch.url` and `launch.ready` on consumption, expiry and close | contract §3; PR 2b-5 | Node host only |
| R7 | SIGTERM → children reclaimed → exit 143; SIGINT → 130 | `commands.py:104-111`; `termination.py:11-45` | exists |
| R8 | A platform wheel for the target can be built from the commit being packaged | KP-027 (darwin/arm64 candidate job) | candidate only; publication frozen |

### C.2 Added by PR 6 itself (Python side, "PR 6a")

| ID | Change | Why | Where |
|---|---|---|---|
| S1 | `--port 0` is accepted; `port` and `url` in the record are the port the host bound | design §4.3 item 1; host plan §I | `commands.py:38-44` (`min=0`); `backend/config.py:104-111` (`parse_port`), `:168` (skip `assert_port_free` for 0); `source.py` ready-line check `payload.port != plan.port` (`:735-745`) takes the reported port when the plan's is 0; the launch URL is built after the ready line |
| S2 | `--stdin-stop`: a `shutdown` line or EOF on stdin stops the session exactly like SIGTERM and exits 0 | no orphan after a shell crash (F5, F8); a stop path that needs no signal (Windows later). Mirrors `request_http/__main__.py:14-24` | `commands.py` (option), `termination.py` (a watcher thread that requests termination on the main thread) |
| S3 | The record gains `record_version: 1` | lets the shell refuse a record it does not understand | `install/report.py:13-70` |
| S4 | `contract/cli/gui-startup-record.v1.json` + a section in `contract/cli/README.md` + a row in `contract/README.md` | host plan §I; AGENTS "改变已发布字段语义须同步 contract/" | new files |
| S5 | `contract/cli/surface.json` regenerated (new option, `--port` help text) | `contract/cli/README.md:8-12` | generated |

### C.3 Proposed `contract/cli` entry: the `hyprial gui --json` startup stream

File `contract/cli/gui-startup-record.v1.json` (schema + examples), prose in `contract/cli/README.md`:

**Stream.** With `--json`, stdout carries newline-delimited compact JSON objects (`output/emit.py:19-26`). Line 1 is the startup record (`ok: true`) or a failure (`ok: false`). After a startup record the process stays in the foreground; at most one more line may follow, a failure (`ok: false`) that explains why the session ended. Nothing else is written to stdout. Progress and warnings go to stderr.

**Startup record — fields a consumer may rely on (v1):**

| Field | Type | Rule |
|---|---|---|
| `ok` | `true` | |
| `record_version` | integer | `1`. A consumer refuses a larger value |
| `url` | string | `http://<loopback literal>:<port>` with an optional trailing `/`, no path beyond it, no query, no fragment, no userinfo. Never carries a credential |
| `host` | string | `127.0.0.1` or `::1` |
| `port` | integer | 1–65535, the bound port (never 0) |
| `handoff_path` | string or null | absolute path of a regular file, mode 0600, owned by the caller, present when `--no-open` was given. Content: one line, `<url origin>/#grant=<percent-encoded grant>` + `\n`. One-time; the host removes the file when the grant is consumed, when it expires (60 s after publication) and on stop |
| `mode` | `"normal"` \| `"dev"` | |
| `backend_driver` | `"daemon"` \| `"contract-mock"` | |
| `frontend_pid`, `backend_pid` | integer / integer or null | informational |

All other keys present today (`gui_home`, `git_worktree`, `git_branch`, `hot_reload`, `backend_endpoint`, `backend_owned`, `backend_principal`, `node_version`, `npm_version`, `installed`, `assets`, `source_commit`, `source_remote`, `auto_worktree`, `dev_prompt`, `business`, `note`, `m1_source`, `handoff_note`; `report.py:34-62`) are informational and may change without a version bump. The record never contains the grant, the upstream bearer or `server.json` content (contract §3).

**Failure line:** `{"ok": false, "code"?: string, "error": string, "data"?: object}` (`output/errors.py:33-52`).

**Exit status:** 0 — the host exited 0 or a stdin stop was honoured; 1 — a failure line was written; 130 — SIGINT; 143 — SIGTERM; any other value — the host child's status (`lifecycle/session.py:215-223`).

**Stop:** SIGTERM, SIGINT, or (with `--stdin-stop`) `shutdown\n` / EOF on stdin. Only this invocation's process groups are reclaimed.

**Second launch:** the same home and driver → failure `GUI_RUNTIME_BUSY`, `data.runtime` names the directory.

**Codes a consumer should expect on line 1** (examples in the corpus; not a closed set): `GUI_RUNTIME_BUSY`, `GUI_RUNTIME_UNAVAILABLE`, `GUI_DAEMON_UNAVAILABLE`, `GUI_DAEMON_NOT_READY`, `GUI_DAEMON_CATALOG`, `GUI_DAEMON_IDENTITY`, `GUI_ASSETS_MISSING`, `GUI_PORT_IN_USE`, `GUI_PORT_UNAVAILABLE`, `GUI_START_FAILED`, `GUI_BACKEND_CONTRACT`, `INVALID_ARGUMENT`.

Both sides test against the same corpus: pytest runs the real CLI and validates its output with the schema; the Rust launcher's unit tests parse every example in the file.

### C.4 What the shell must check itself (not the CLI's job)

1. `url` parses, scheme `http`, host a loopback literal, port equals `port`.
2. The handoff file is a regular file (no symlink followed: `O_NOFOLLOW`), owned by the current uid, mode exactly 0600, at most 4 KiB, one line.
3. Its origin equals the record's origin; its fragment starts with `grant=`; no query.
4. The content is passed to the webview and nowhere else: not to a log, not to the status page, not to an error.

---

## D. Ordered steps

Conventions for every step: write the named test first and see it fail for the stated reason; Python only through `uv run`; no inline comments, decisions in the file-header block; conventional commits; ≤ 7 direct files per code directory and ≤ 1000 lines per file.

### D.0 Split

| PR | Title | Depends on | Verifiable in this sandbox |
|---|---|---|---|
| **6-pre** | `refactor(shell): remove the orphaned desktop network adapter` | nothing | fully |
| **6a** | `feat(gui): hyprial gui accepts --port 0 and --stdin-stop; startup record contract` | PR 2c | fully (Linux) |
| **6b** | `feat(clients): minimal native shell — launcher crate and Tauri window` | 6a | launcher crate fully; the Tauri crate compiles only where WebKitGTK dev packages exist (not here, F11) |
| **6c** | `build(clients): macOS arm64 package of the shell` | 6b, R8 | staging scripts on Linux; the package only on the Mac runner |

6b and 6c may be one PR if the owner prefers (same commits, same order). 6-pre can merge today.

### D.1 PR 6-pre

| Step | Red test first | Change | Run |
|---|---|---|---|
| pre-1 | a test next to the existing `hyprial.shell` interface tests (locate with `git grep -n "shell.interface" tests/`): `hyprial.shell` exports no `desktop_network` (fails today) | delete `src/hyprial/shell/impl/desktop_network.py`; remove `shell/interface.py:9,26,62`; delete `tests/integration/desktop/test_desktop_network.py`; move `test_desktop_sdk_bootstrap.py` to `tests/daemon/…/agent_sdk/` (same content) and delete `tests/integration/desktop/`; remove the two rows from `scripts/ci/pytest-durations.json:1451-1452`; fix the row in `docs/guides/network-isolation.md:169` | `uv run pytest -q tests/shell/cli tests/tooling/architecture -n 2`; the moved test |
| pre-2 | — | `.forgejo/workflows/gate-python.yml:151` comment: drop "Tauri" | — |
| pre-3 | — | gates | `uv run ruff check .`; `uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py`; `… check_module_boundaries.py`; `… check_network_reachability.py` (`docs/guides/network-isolation.md:169` lists the module; whether the gate's own table names it was not checked — if it does, remove that row in the same commit) |

Before deleting, re-run `git grep -n desktop_network` on the PR base; if `scripts/refactor/module-map.original.json:23` or `scripts/refactor/preview/plan.json:1770` are checked by a test, leave them (they are frozen originals) and say so in the PR body.

### D.2 PR 6a (Python; all on Linux)

| Step | Red test first | Change | Run |
|---|---|---|---|
| 6a-0 | **measurement, no code**: isolated `HYPRIAL_HOME` + state dir, wheel-shaped plan; (i) ownerless home → `hyprial gui --no-open --json` → initialise through the GUI → stop → start again without starting a daemon. Record the code of the second start (expected `GUI_DAEMON_UNAVAILABLE`, F4). (ii) after a failure during `wait()` is a second JSON line written to stdout? | result goes into the PR body and decides OD-2 and the wording of C.3 | — |
| 6a-1 | `tests/shell/gui/lifecycle/test_gui_launcher.py`: `parse_port(0)` is accepted; `tests/shell/gui/backend/test_source_session.py`: a plan with port 0 and a fake host ready line `port: 41234` yields `payload["port"] == 41234`, `payload["url"] == "http://127.0.0.1:41234"`, and the handoff URL starts with that origin. Fails: `INVALID_ARGUMENT` | S1 | `uv run pytest -q tests/shell/gui/lifecycle tests/shell/gui/backend` |
| 6a-2 | `tests/shell/gui/lifecycle/test_gui_signals.py`: a subprocess `hyprial gui … --stdin-stop` with a fake plan exits 0 and reclaims its children when stdin is closed, and when `shutdown\n` is written; without the flag, closed stdin changes nothing. Fails: no such option | S2 | `uv run pytest -q tests/shell/gui/lifecycle/test_gui_signals.py` |
| 6a-3 | `tests/tooling/contracts/…/test_gui_startup_record.py`: loads `contract/cli/gui-startup-record.v1.json`; validates every example; validates `startup_payload(fake_session)`; asserts no value in the payload contains the grant or the bearer (property test over a session built with known secrets). Fails: file absent | S3, S4 | `uv run pytest -q <that file> tests/shell/gui/installed` |
| 6a-4 | the CLI surface test fails (new option) | regenerate `contract/cli/surface.json` (`uv run --python 3.12 python scripts/verify/update_cli_surface_snapshot.py`) and `docs/reference/cli-command-inventory.json` | `uv run pytest -q tests/shell/cli/surface` |
| 6a-5 | one live case in `tests/integration/request_http/onboarding/test_gui_admission.py`: real CLI, isolated ownerless home, `--no-open --json --port 0 --stdin-stop`; the record validates; the handoff opens a session over HTTP (grant consumed, `launch.url` gone); closing stdin → exit 0 and no child left | fixes only | `uv run pytest -q tests/integration/request_http/onboarding` |
| 6a-6 | — | docs: `docs/guides/gui-dev-mode.md` (port 0, stdin stop), `src/hyprial/skills/hyprial-ops/SKILL.md` (same two options) | doc gates; `./contract/run-all.sh --isolated-only`; full `uv run --with libcst==1.9.0 pytest tests/ -n 2`; `uv run ruff check .`; both structural gates |

### D.3 PR 6b (shell; launcher crate testable on Linux, Tauri crate not buildable here)

| Step | Red test first | Change | Run here? |
|---|---|---|---|
| 6b-1 | `clients/native/launcher/src/record.rs` unit tests: every example of `contract/cli/gui-startup-record.v1.json` (read with `include_str!` through a relative path) parses; rejected: `record_version: 2`, `port: 0`, `url` with a path / query / fragment / userinfo / non-loopback host / port ≠ `port`, missing `handoff_path`, a line over 64 KiB, non-JSON. Fails: crate absent | workspace `Cargo.toml`, `launcher/Cargo.toml`, `lib.rs`, `record.rs` | yes: `cargo test --manifest-path clients/native/Cargo.toml -p hyprial-shell-launcher --locked` |
| 6b-2 | `handoff.rs` tests on a temp dir: accepts 0600 regular file with one valid line; refuses mode 0644, a symlink, a directory, a file over 4 KiB, two lines, an origin different from the record's, a fragment that is not `grant=…`, a query. **The `Debug`/`Display` of the returned type and of every error must not contain the grant** (test formats them and searches for the secret) | `handoff.rs`: a `LaunchUrl` newtype with a redacting `Debug` | yes |
| 6b-3 | `origin.rs` table test: `allowed_main_frame(url, origin)` true only for the exact origin (scheme, host literal, port) and for the bundled status page; `external_target(url)` is `Some` only for `http`/`https` of another origin; `about:blank`, `file:`, `javascript:`, `data:`, custom schemes → `None` | `origin.rs` | yes |
| 6b-4 | `launcher/tests/fake_launcher.rs` (integration, unix): shell scripts written to a temp dir act as `hyprial`: (a) prints a record, writes the handoff, waits → `start()` returns the record and URL; (b) prints `{"ok":false,"code":"GUI_RUNTIME_BUSY",…}` and exits 1 → typed error with the code; (c) exits without output → typed error with the status; (d) never prints → timeout error and the group is killed; (e) stop: the script exits on stdin EOF → `stop()` returns its status within the first budget; (f) a script that ignores EOF and SIGTERM and has a grandchild → both are gone after `stop()` (process group kill); (g) a failure line after the record is delivered to the exit callback; (h) the argv received is exactly `gui --no-open --json --port 0 --stdin-stop` | `child.rs` (≈ 200 lines): spawn with `process_group(0)`, piped stdin/stdout, bounded line reader, three-stage stop | yes |
| 6b-5 | none possible here (needs a webview). Reviewed by reading + the macOS smoke of 6c | `src-tauri/`: `Cargo.toml`, `build.rs`, `tauri.conf.json`, `src/main.rs`, `ui/index.html`, icons. `main.rs` only wires: single-instance → window on `index.html#starting` → `launcher::start` on a thread → `navigate(launch_url)`; `on_navigation` = `origin::allowed_main_frame`; `on_new_window` = `origin::external_target` → `open::that` + deny; window close / `RunEvent::ExitRequested` → `launcher::stop`; child exit → navigate to `index.html#error=…`. No `invoke_handler`, no `capabilities/` directory, no `plugins` config, `withGlobalTauri: false`, devtools off in release | **no** (F11): `cargo check -p hyprial-desktop` is BLOCKED locally; first compiled on the Mac runner or a CI container with `libwebkit2gtk-4.1-dev` |
| 6b-6 | `tests/tooling/architecture/ipc/test_daemon_request_architecture.py`: (i) the real tree reports exactly one `native-process-invocation` at `clients/native/launcher/src/child.rs` and it is covered by the registry with count 1; (ii) a second `Command::new(` anywhere under `clients/native` is a violation. Fails: root not scanned | `check_daemon_request_architecture.py:59` `NATIVE_ROOT = "clients/native"` (skip `target/`, `gen/`); `REVIEWED_NATIVE_EXCEPTIONS` gains that one triple; the three existing native tests move to the new root | yes: `uv run pytest -q tests/tooling/architecture/ipc` |
| 6b-7 | new `tests/tooling/architecture/…/test_native_shell_surface.py` (static, runs everywhere): `tauri.conf.json` has no `app.windows`, no `plugins`, `withGlobalTauri` false, CSP without `ipc:`; no `clients/native/src-tauri/capabilities/` directory; `main.rs` contains no `invoke_handler`, no `#[tauri::command]`, no `remote`; `Cargo.toml` of `src-tauri` depends on no crate outside an allowlist (`tauri`, `tauri-build`, `tauri-plugin-single-instance`, `open`, `hyprial-shell-launcher`); the `tauri` requirement is `=2.12.3` or newer and never below 2.11.1; `ui/index.html` has no `<script src>` and no `fetch(` | the test only | yes |
| 6b-8 | `tests/tooling/architecture/limits/test_structure_limits.py`: `clients/native/launcher/src` with 8 files is a violation; `clients/native/src-tauri/target` is excluded. Fails: root not known | `check_structural_limits.py:35,40`: optional roots become `("clients/native",)`, excluded paths `clients/native/target`, `clients/native/src-tauri/gen`, `clients/native/src-tauri/runtime`; drop the `app/*` and `src/gui` rows and their tests (130-164) | yes |
| 6b-9 | — | `.gitignore` (`clients/native/target/`, `clients/native/src-tauri/runtime/`, `gen/`); `AGENTS.md:108-111` (the shell exists again; what it may and may not do); `gui/docs/ui-development-standard.zh-CN.md:87`; `scripts/ci/ci-plan.mjs` (a `clients/native/**` or `packaging/clients/**` change selects the shell job) + its test; a job `shell-launcher` in `gate-python.yml` or a new `gate-shell.yml` on `ubuntu-latest`: `bash scripts/ci/ci-ensure-rust.sh` then `cargo test -p hyprial-shell-launcher --locked` and `cargo fmt --check`, `cargo clippy -p hyprial-shell-launcher -- -D warnings` | the cargo commands yes; the workflow only on CI |

Network note for 6b-1: `cargo` needs crates.io through the agent proxy; a registry cache exists in `~/.cargo/registry` [P]. If the fetch is refused, record BLOCKED and continue with code review only; do not vendor.

### D.4 PR 6c (package; staging on Linux, bundle on the Mac runner)

| Step | Red test first | Change | Run here? |
|---|---|---|---|
| 6c-1 | `tests/tooling/release/…/test_shell_runtime_lock.py`: `packaging/clients/runtime-lock.json` names, per target, a PBS asset URL under `github.com/astral-sh/python-build-standalone/releases/download/<tag>/`, a 64-hex SHA-256 and the wheel platform tag; unknown keys fail. Fails: file absent | the lock. Values for the first entry [P, 2026-11-15, tag `20261110`]: `cpython-3.12.16+20261110-aarch64-apple-darwin-install_only_stripped.tar.gz`, SHA-256 `2f11b8aa3726bdbd87dd9eb76568412faddf4f47e26913fa45d6a2e424972acd`, 25,237,581 bytes; Linux x86-64 `6a9e62da…0b39` (34,709,145 bytes) for the Linux test of the script | yes |
| 6c-2 | `test_stage_runtime.py` with a tiny fake PBS tarball and a fake wheel served from a temp dir: wrong SHA → refusal before extraction; staged tree has `bin/python3.12` as a regular file and **no symlink anywhere**; `test/`, `idlelib/`, `tkinter/`, `ensurepip/`, `include/`, `share/`, `__pycache__` are pruned; a `runtime-manifest.json` lists wheel version, source commit, PBS tag and a SHA-256 per file; a second run is byte-identical | `packaging/clients/stage_runtime.py`: download → verify → extract → `uv export --locked --no-dev --no-emit-project --format requirements-txt` → `uv pip install --python <staged>/bin/python3.12 --target <staged site-packages> --require-hashes -r … ` + the platform wheel `--no-deps`. The exact `uv pip` flags are confirmed in 6c-3 | yes |
| 6c-3 | probe (Linux x86-64, real PBS + a locally built Linux wheel if the worktree can build one, else BLOCKED): `packaging/clients/verify_runtime.py` runs `<staged>/bin/python3.12 -I -m hyprial.cli version --json` and `… gui --no-open --json --port 0 --stdin-stop` against an isolated ownerless home, validates the record, closes stdin | `verify_runtime.py` | yes if a wheel exists (ENV.md: `bash scripts/build/build_native_engine.sh`; `scripts/build/build-wheel.sh` needs a clean tree and Node 24) — otherwise BLOCKED, recorded |
| 6c-4 | — | `.forgejo/workflows/shell-macos-candidate.yml`: `workflow_dispatch` (input: full 40-hex SHA) and push of `shell/**` branches; `runs-on: [release-macos-arm64]`; steps: host assert Darwin/arm64 (copy of `native-conformance.yml:608-616`) → pinned Rust → build the wheel as the kp027 darwin job does → `stage_runtime.py --target aarch64-apple-darwin` → `verify_runtime.py` → `cargo install tauri-cli --version 2.12.2 --locked` (cached) → `cargo tauri build --bundles app,dmg` with `signingIdentity "-"` → smoke (E.2) → upload `.dmg`, `runtime-manifest.json`, `SHA256SUMS`, the smoke log. **Never publishes**; no secret is read | only on the runner |
| 6c-5 | `test_shell_workflow.py`: the workflow contains no `secrets.` other than `GITHUB_TOKEN`, no `R2`, no `release` upload, and pins `tauri-cli` and the Rust version | the test | yes |
| 6c-6 | — | `packaging/clients/README.md`: how to build locally on a Mac, what "ad-hoc signed" means for the tester (E.3), the signing and notarization procedure for later (from the old signing README), where credentials live (never in the repo). `docs/reference/known-problems.md`: KP-056 updated with the measured result; new entries for Windows (lock port), localStorage per port (if OD-4 keeps port 0), served home without a daemon (if 6a-0 confirms) | doc gates |

---

## E. Verification

### E.1 Linux x86-64 (this sandbox)

| Check | Command | Expectation |
|---|---|---|
| 6-pre, 6a | commands in D.1, D.2; full pytest `-n 2`; ruff; both structural gates; `./contract/run-all.sh --isolated-only` | PASS, or the failing set equals the base's failing set (ENV.md: `dev` has failing shards; compare, list in the PR body) |
| Launcher crate | `cargo test --manifest-path clients/native/Cargo.toml -p hyprial-shell-launcher --locked` | PASS (BLOCKED if crates.io is unreachable) |
| Static shell surface, architecture and limits gates | `uv run pytest -q tests/tooling/architecture -n 2` | PASS |
| Staging scripts | `uv run pytest -q tests/tooling/release -n 2` | PASS |
| Real CLI behind the launcher crate | a `#[ignore]` test run with `HYPRIAL_SHELL_LAUNCHER="<venv>/bin/hyprial"` and an isolated home | PASS after 6a; needs the native engine built (ENV.md) |
| Tauri crate compiles on Linux | `cargo check -p hyprial-desktop` | **BLOCKED** here (no `webkit2gtk-4.1`); UNKNOWN until a CI container installs `libwebkit2gtk-4.1-dev` |
| Headless webview smoke under xvfb | — | **BLOCKED** here for the same reason; optional CI job in follow-up 6d |

### E.2 macOS arm64 (`release-macos-arm64`; nothing here is assumed)

| # | Check | How | Expectation |
|---|---|---|---|
| M1 | Wheel for the SHA builds | workflow step (same as kp027 darwin) | PASS expected; FAIL blocks 6c |
| M2 | Staged runtime runs | `verify_runtime.py` | UNKNOWN |
| M3 | `cargo tauri build --bundles app,dmg` | workflow | UNKNOWN (first compile of the Tauri crate anywhere) |
| M4 | Launch smoke, automated: start `Hyprial.app/Contents/MacOS/hyprial-desktop` with an isolated `HYPRIAL_HOME`/state dir; assert within 30 s that `<state>/gui/daemon/launch.url` appeared and **was removed while the 60 s grant TTL had not passed** (removal before TTL = the grant was consumed = the webview loaded the page, ran it, and `POST /daemon/session` passed the Host, Origin and cookie rules); then SIGTERM the app and assert no `hyprial` child remains and `supervisor.lock` can be taken | script in the workflow | UNKNOWN. BLOCKED if the runner's launchd session has no window server access; then M4 is run by hand on the Mac |
| M5 | No orphan after a crash: `kill -9` the app; the launcher exits by stdin EOF within 5 s | same script | UNKNOWN |
| M6 | Manual, on a Mac with a display: served home with a same-version daemon → Start page, at least one 200 `/daemon/request/*`; reload keeps the session; login click opens the system browser (KP-056); external link opens the system browser; a typed `http://example.com` in a link cannot replace the main frame; second app start focuses the window; quit leaves no process | checklist in the PR body | UNKNOWN each; reported PASS / FAIL individually |
| M7 | Bridge probe: a debug build pointed (via `HYPRIAL_SHELL_LAUNCHER` fake) at a page that calls `window.__TAURI_INTERNALS__.invoke('plugin:app|version')` → rejected | manual once | UNKNOWN; a success of the call is a release blocker |
| M8 | Gatekeeper on a second Mac: download the `.dmg` with a browser, copy the app, open | manual | expected: blocked on first open; "Open Anyway" in Privacy & Security works (E.3). UNKNOWN whether the bundled interpreter then runs without a further prompt |

### E.3 Without signing credentials (state now)

- Buildable: an **ad-hoc signed** (`signingIdentity: "-"`) arm64 `.app` and `.dmg`. Apple Silicon requires at least an ad-hoc signature to execute; Tauri applies it [W].
- Consequence for a tester who downloads it: the quarantine attribute makes Gatekeeper refuse the first launch. Since macOS 15 Control-click → Open no longer bypasses; the user opens it once, then System Settings → Privacy & Security → "Open Anyway" (admin password) [W]. Alternative for internal testers: `xattr -dr com.apple.quarantine /Applications/Hyprial.app`. Behaviour on macOS 26 is not confirmed [W].
- Not possible without credentials: Developer ID signature, hardened runtime that passes notarization, notarization, stapling, a silent first launch, a trustworthy auto-update.
- Built on the runner and run in place (no quarantine): no prompt. That is what M4 exercises.

### E.4 Windows x86-64 and Linux package

| Platform | Claim | Status |
|---|---|---|
| Windows | wheel `hyprial gui` runs | **BLOCKED**: `GUI_RUNTIME_UNAVAILABLE` until the lock port (OD-4 of the host plan) |
| Windows | platform wheel | **BLOCKED**: no build host (KP-027) |
| Windows | shell builds, stop by stdin works, NSIS installer | **UNKNOWN**: no runner; `child.rs` keeps its unix-only parts behind `cfg(unix)` and has no Windows implementation in PR 6 |
| Linux | AppImage / deb | **not attempted** in PR 6; follow-up 6d on `ubuntu-latest` with `libwebkit2gtk-4.1-dev` and xvfb |

---

## F. Distribution

| | macOS arm64 | Windows x86-64 | Linux x86-64 |
|---|---|---|---|
| Artifact | `.app` in a `.dmg` | NSIS `-setup.exe` (current user) | AppImage (+ deb) |
| Webview | system WKWebView | WebView2 (preinstalled on Windows 11; bootstrapper for Windows 10) [W] | system WebKitGTK 4.1 (deb dependency; AppImage bundles it) |
| Minimum OS | macOS 13 (wheel tag `macosx_13_0_arm64`) | UNKNOWN | glibc ≥ 2.17 for the wheel; WebKitGTK 4.1 needs a newer distribution |
| Size, estimate only | shell binary 5–10 MB; PBS Python 25 MB compressed; site-packages of the dev environment is 213 MB unpacked, of which `lark_oapi` 99 MB, `zenoh` 30 MB, `cryptography` 16 MB [P, a Linux venv with test extras]. Expect a `.dmg` of roughly 90–130 MB. **UNKNOWN until built** | similar | similar + WebKitGTK in the AppImage |
| CI | `shell-macos-candidate.yml` on `release-macos-arm64` | none | future job on `ubuntu-latest` |
| Secrets for a signed release (never in the repo; runner-local files in an ephemeral keychain, or forge secrets) | `APPLE_CERTIFICATE` (base64 `.p12`, Developer ID Application), `APPLE_CERTIFICATE_PASSWORD`, `APPLE_SIGNING_IDENTITY`, and `APPLE_API_ISSUER` + `APPLE_API_KEY` + `APPLE_API_KEY_PATH` (or `APPLE_ID` + `APPLE_PASSWORD` + `APPLE_TEAM_ID`) [W]. Apple Developer Program: 99 USD per year [W] | an Authenticode identity: Azure Artifact Signing (eligibility by country, see the old signing README) or a CA certificate on an HSM | none required; optional GPG for a deb repository |
| Update | none in PR 6: a new `.dmg` replaces the app. The Tauri updater needs its own key pair (`TAURI_SIGNING_PRIVATE_KEY`) and a signed build to be meaningful | — | — |

How Python and the wheel reach the user (decision OD-1):

| | P1. Runtime assembled at build time inside the bundle (**recommended**) | P2. Wheel + `uv` in the bundle, environment built on first run (the literal row of design §4) | P3. Require `uv tool install hyprial`; the shell only locates it |
|---|---|---|---|
| First run | nothing to do; no network | builds an environment; needs a start page with progress; needs the network unless a wheelhouse and a Python archive are bundled too | none |
| Moving parts in the shell | one path to an interpreter | uv invocation, progress, failure and repair states, version marker | lookup heuristics (`~/.local/bin`, a GUI app does not inherit the shell's `PATH`) and a "not installed" state |
| Notarization later | every Mach-O in `Contents/Resources/runtime` is signed in place by a staging step; nothing is hidden in an archive | the notary service looks inside zip archives, and a wheel is a zip: unsigned `.so` files inside bundled wheels are rejected [W]; they would have to be signed and repacked | only the shell binary |
| Version coupling with a running daemon (F4) | the app is its own hyprial; a user who also runs a CLI daemon of another version gets `GUI_DAEMON_CATALOG` | same | always the user's own version — no mismatch |
| "Almost the same as wheel + browser" | same code, different install location | same | identical |
| Works for a user without a terminal | yes | yes | no |

P1 deviates from one row of the design's table ("用安装包内的 wheel 与 uv 建环境，以及建环境期间的一个启动页"): there is no first-run environment build and no uv in the package; the start page shrinks to a static "starting" page. The package still carries the platform wheel's content (design §4.4). This deviation needs the owner's yes (OD-1).

---

## G. Risks, UNKNOWNs, owner decisions

### G.1 Owner decisions (the default is what the implementer does if nobody answers)

| ID | Decision | Default | Alternative | Changes user-visible behaviour / needs money or credentials |
|---|---|---|---|---|
| OD-1 | How Python and hyprial get onto the machine | P1 (runtime inside the bundle, built at package time) | P2 (design's literal row) or P3 | **yes** (first run, size, offline) |
| OD-2 | A served home with no running daemon (F4; confirm in 6a-0) | PR 6 shows the CLI's error (`GUI_DAEMON_UNAVAILABLE` and its text) on the status page. The fix belongs in `hyprial gui` (Python), so the browser path gets it too; it is not put in the shell | the shell runs `hyprial daemon start` first — a second responsibility in the shell and a core/extensions question (design §7.2) | **yes** — until it is decided, a user who only has the app can finish first-run once and cannot reopen the app later without a daemon |
| OD-3 | Shell engine | Tauri v2, zero capabilities, launcher logic in a Tauri-free crate | wry + tao (no bridge object at all; bundling and signing become our scripts) | no |
| OD-4 | Port | `--port 0` (design §4.3 item 1). Consequence: `localStorage` is empty on every launch (F7) | try `4173` first and fall back to 0 — keeps `localStorage`, shares the origin with a browser session, and a fixed port can be squatted by another local process | **yes** (what the GUI remembers between launches) |
| OD-5 | `--stdin-stop` in `hyprial gui` | add it (S2) | SIGTERM only, plus a stale-runtime message after a crash | no (new CLI option; contract/cli changes) |
| OD-6 | Bundle identifier and product name | `org.hyprial.desktop`, "Hyprial". The identifier is permanent: it keys the webview data directory, the keychain and the update lineage | keep a test identifier until the first signed release | **yes** |
| OD-7 | Shell version (F10) | the shell has its own semver in `tauri.conf.json`, starting at `0.2.0`; the bundled wheel version and source commit are in `runtime-manifest.json` and on the status page | map `0.5.5.4` → `0.5.5` + build number | yes (what "About" shows) |
| OD-8 | Signing for the first package | ad-hoc, internal testers only, Gatekeeper steps documented | wait for a Developer ID | **yes — credentials and 99 USD/year** for the alternative |
| OD-9 | End of the 12 h view (F6) and child exit | status page with the reason; the user restarts the app | the shell restarts the launcher by itself (a supervision policy in the shell) | yes |
| OD-10 | Another `hyprial gui` already runs for the home (F5) | status page with `GUI_RUNTIME_BUSY` and the runtime path | a "re-grant" facility in the host so a second viewer can attach — a session-contract change | yes |
| OD-11 | `HYPRIAL_SHELL_LAUNCHER` in release builds | honoured (same trust as the user's environment); documented | debug builds only | no |
| OD-12 | `contract/native-host` and the `nativeHost` / external-promotion branch of `request_http` (≈ 1,350 lines with tests) | PR 7, as the host plan says; they share files with the CLI-convergence work and with §7a | a separate "6-pre-b" now | no; `hostContract` leaves a published schema → Contract-Impact |
| OD-13 | Split | 6-pre, 6a, 6b, 6c | 6-pre + one PR 6 | no |
| OD-14 | Structural limits for `clients/native` | add it as an optional product root (≤ 7 files, ≤ 1000 lines) | leave Rust shell code ungated | no |

### G.2 Risks

| Risk | Mitigation |
|---|---|
| Tauri injects its bridge into the remote page | zero capabilities, zero commands, static test 6b-7, probe M7; fallback OD-3 |
| The first compile of the Tauri crate happens on the Mac runner, late | 6b-5 is ≤ 200 lines of glue over a tested crate; if `ubuntu-latest` can `apt install libwebkit2gtk-4.1-dev`, add `cargo check` there in 6b-9 |
| `window.open('about:blank')` is denied → one extra click at login | the GUI already has the link fallback; measured in M6; a later, optional improvement is a daemon-request method that opens the browser from the host side (no IPC) |
| A daemon promoted inside the app's child (first run) inherits a GUI-app environment: `PATH` is `/usr/bin:/bin:/usr/sbin:/sbin`; harness CLIs installed by the user are not on it | UNKNOWN; measure in M6; not solved by the shell (OD-2 territory) |
| Bundled version ≠ running daemon version → `GUI_DAEMON_CATALOG` | status page shows the CLI's message with both versions; documented in `packaging/clients/README.md` |
| Tauri's resource copier and symlinks in PBS (`bin/python3 → python3.12`) | the staging step removes every symlink and the shell names `python3.12` exactly; test 6c-2 |
| `uv pip install --target` into a PBS tree: exact flags and whether console scripts with absolute shebangs appear | the shell uses `-m hyprial.cli`, never a script; flags settled in 6c-3 |
| Hardened runtime + notarization with an embedded interpreter | out of scope now; P1 keeps it tractable (sign in place) |
| Grant leaks through a log or an error | newtype with redacting `Debug` (6b-2); stderr of the child goes to a 0600 file; the CLI never prints it (contract §3) |
| The handoff TTL is 60 s; a slow webview start loses the race | the shell navigates as soon as line 1 arrives; on `LAUNCH_GRANT_EXPIRED` the page shows its bootstrap error and the user restarts |
| Cargo.lock of a Tauri app is ≈ 5,500 lines and ≈ 400 crates | pinned with `--locked`; the launcher crate has 4 direct dependencies |
| Mac runner is shared with E2E gates (`e2e-controlled` concurrency) and is a single machine | `workflow_dispatch` only; no schedule; own concurrency group |
| Merge conflicts: `check_daemon_request_architecture.py`, `check_structural_limits.py`, `ci-plan.mjs`, `gate-python.yml`, `commands.py`, `source.py` (also edited by 2c/3 and 1e-2) | 6a rebases on 2c/3; different functions |

### G.3 UNKNOWN (and how each is resolved)

| Item | Resolved by |
|---|---|
| Everything in B.4 for WKWebView | M4, M6 |
| Whether the Mac runner can open a window from a workflow job | M4 |
| Second start of an app-only user (F4) | 6a-0 |
| Second stdout line on a late failure | 6a-0 |
| `.dmg` size | M3 |
| Whether an ad-hoc signed, quarantined app's bundled interpreter runs after "Open Anyway" | M8 |
| WebKitGTK and WebView2 behaviour | not in PR 6 |
| `cargo` reaching crates.io from the implementer's sandbox | first run of 6b-1 |
| Whether `ubuntu-latest` jobs can install `libwebkit2gtk-4.1-dev` (mirror) | 6b-9 |
| Tauri 2.12.3 MSRV against the repo's pinned Rust 1.93.1 | M3 (old shell: `rust-version = "1.85"`) |
| Failing set of `dev` at the PR base | run before each PR |

---

## H. PR-body inputs

| PR | Motivation | Skill-Impact | Contract-Impact |
|---|---|---|---|
| 6-pre | dead adapter left by the deleted shell | none | none (`hyprial.shell` loses an unpublished attribute) |
| 6a | the shell and any script need a bound port, a stop without signals and a stated record | `src/hyprial/skills/hyprial-ops/SKILL.md` (two options) | **adds** `contract/cli/gui-startup-record.v1.json`; record gains `record_version`; `--port` accepts 0; new option `--stdin-stop`; `contract/cli/surface.json` regenerated |
| 6b | design §4 rebuild | `AGENTS.md` layout section; `.agents/skills/gui-workflow/SKILL.md` gets a pointer (full rewrite is PR 7) | none (consumer of 6a's contract and of `gui-session` v1; no new field) |
| 6c | first package | `packaging/clients/README.md` is the operator document | none |

Every body also carries: commands run with results; the base failing set next to the PR's; the table of E.2 with PASS / FAIL / BLOCKED / UNKNOWN per row (never "assumed"); the statement that Windows and the Linux package are not delivered; for 6c the SHA-256 of the `.dmg` and the `runtime-manifest.json`.

---

## I. Deferred, with an owner

| Item | Owner |
|---|---|
| Windows: port the GUI runtime locks to `kernel.ProcessFileLock` (`kernel/impl/platform/file_lock.py:89`) and replace `os.getuid` checks | a host-line PR after PR 3 ("PR 3w"); prerequisite of any Windows shell |
| Windows: platform wheel and a runner | KP-027 W1 |
| Windows: `child.rs` stop path, NSIS, Authenticode | "PR 6w", after the two rows above |
| Linux package (AppImage / deb) and an xvfb smoke | "PR 6d" |
| Developer ID signing, hardened runtime, notarization, stapling | "PR 6s", when OD-8 provides credentials |
| Auto-update | after 6s (an update channel without signatures is not acceptable) |
| Tray, deep link `hyprial://` | separate small PRs after 6c; each needs its own decision |
| Mobile | not planned; after the core/extensions split |
| core/extensions split, including who starts the daemon (OD-2) | owner decision first (design §7.2), then a host-line PR |
| `contract/native-host`, `nativeHost` branch, `hostContract`, `HYPRIAL_BUNDLED_*` variables, gui-workflow skill rewrite, Docker entry | PR 7 |
| Session host folded into `request_http` | design §7a; invisible to the shell |
| Attach / re-grant for a second viewer | a session-contract revision, if OD-10 asks for it |

---

## J. Sources looked up (2026-11-15)

- Tauri releases (tauri 2.12.3, tauri-cli 2.12.2, bundler 2.10.2, wry 0.57.0 listed; docs.rs shows wry 0.57.2, tao 0.37.1): https://v2.tauri.app/release/ , https://docs.rs/wry/latest/wry/struct.WebViewBuilder.html
- Capabilities, remote API access: https://v2.tauri.app/security/capabilities/
- macOS signing, ad-hoc identity, notarization variables, programme cost: https://v2.tauri.app/distribute/sign/macos/
- Bundle formats: https://v2.tauri.app/distribute/ ; sidecar naming: https://v2.tauri.app/develop/sidecar/
- `on_new_window` (tauri-runtime 2.8.0): https://docs.rs/tauri/2/tauri/webview/struct.WebviewWindowBuilder.html , https://v2.tauri.app/release/tauri-runtime/v2.8.0/
- python-build-standalone latest release `20261110`, asset names, SHA256SUMS and sizes: https://raw.githubusercontent.com/astral-sh/python-build-standalone/latest-release/latest-release.json , https://github.com/astral-sh/python-build-standalone/releases/tag/20261110 , https://docs.astral.sh/python-build-standalone/running/
- Notary service inspects zip archives: https://developer.apple.com/forums/thread/762836 , https://developer.apple.com/forums/thread/764017 ; unsigned code under `Contents/Resources`: https://developer.apple.com/forums/thread/724916
- Gatekeeper on macOS 15: https://mjtsai.com/blog/2024/07/05/sequoia-removes-gatekeeper-contextual-menu-override , https://eclecticlight.co/2024/10/01/living-without-notarization/
- pywebview 6.x engines and dependencies: https://sources.debian.org/src/python-pywebview/6.1%2Bdfsg-1/docs/guide/web_engine.md , https://sources.debian.org/src/python-pywebview/6.0%2Bdfsg-1/docs/guide/installation.md
- `SameSite=Strict` on a loopback port (cross-site initiated case): https://kaweah.freedombox.rocks/kiwix/stackoverflow.com_en_all_2023-11/questions/75253898/why-is-a-samesite-strict-cookie-for-http-127-0-0-1-12345-not-being-sent-in-and
- Not looked up, stated from memory and therefore to be confirmed by measurement: ATS exemption for IP literals; WebView2 preinstalled on Windows 11; `Sec-Fetch-Site` support in Safari 16.4.

---

## Summary (15 lines)

1. Nothing of the old shell remains; it was 131 Rust files with its own HTTP host, mobile bridge and five windows. Only the icons are reused.
2. Orphans left behind: `desktop_network.py` and its test (deleted in 6-pre); the `nativeHost` branch of `request_http` and `contract/native-host` stay with PR 7.
3. The shell consumes one thing: `hyprial gui --no-open --json`, line 1 of stdout, and the 0600 `launch.url` it names.
4. That contract is written down as `contract/cli/gui-startup-record.v1.json` and tested from both sides against one corpus.
5. Python gets three small additions (PR 6a): `--port 0` with the bound port reported, `--stdin-stop`, `record_version`.
6. Recommended shell: Tauri v2 (2.12.3) with one window, no command, no capability, no plugin except single-instance.
7. All logic (record, handoff, origin guard, child stop) is a Tauri-free Rust crate that is tested on Linux with fake launchers.
8. The Tauri glue is about 180 lines and cannot be compiled in this sandbox (no WebKitGTK); its first build is on the Mac runner.
9. Python reaches the user as a python-build-standalone interpreter plus the platform wheel and locked dependencies, assembled at package time inside the app (deviation from one row of design §4; owner decision).
10. macOS arm64 is the only package in PR 6: ad-hoc signed `.dmg` built by a dispatch-only workflow on `release-macos-arm64`; it never publishes and reads no secret.
11. The automated macOS check is black-box: the grant file disappears before its TTL, which proves the webview loaded the page and passed the Host, Origin and cookie rules.
12. Windows is blocked three times (lock port, no wheel host, no runner); the Linux package is a follow-up.
13. Three findings change user-visible behaviour: a served home needs a same-version running daemon; a new port per launch empties `localStorage`; login uses the link fallback because `window.open` is denied.
14. Without credentials the package is for internal testers: Gatekeeper blocks the first open and "Open Anyway" is needed.
15. Everything about WKWebView, the runner's window server and the package size is UNKNOWN until the Mac run and is reported row by row.

## Estimated diff

| PR | Added | Deleted | Files |
|---|---|---|---|
| 6-pre | ≈ 40 | ≈ 330 | ≈ 7 |
| 6a | ≈ 420 (product 110, contract 130, tests 180) | ≈ 30 | ≈ 12 |
| 6b | ≈ 1,450 hand-written (launcher 450, its tests 420, Tauri glue + page + config 300, gates and their tests 200, docs 80) + `Cargo.lock` ≈ 5,500 generated + 3 icon binaries (1.67 MB) | ≈ 120 (stale `app/*` roots and tests) | ≈ 30 |
| 6c | ≈ 900 (scripts 330, tests 250, workflow 200, README + known-problems 120) | 0 | ≈ 10 |

## Owner decisions recorded after the plan (2026-11-15)

- Bundle identifier: `com.hyprial.hyprialos` (replaces the plan's `org.hyprial.desktop`, OD-6). Display name: pending owner answer ("Hyprial" vs "HyprialOS").
- Signing (OD-8): the owner has an Apple Developer ID. Target Developer ID signing + notarization for the first macOS package; credentials are runner secrets configured by the owner, referenced by name only, never in the repo.
- App home: the app keeps its own HYPRIAL_HOME, separate from `~/.hyprial` (original design, confirmed as the recommendation; reason: the daemon requires an exact package-version match and one lock holder per home). Pending owner confirmation.
- OD-2 root cause (read from code, not yet measured): `gui/backend/admission.py` requires a live same-version daemon for a home that has an owner (`verify_ready_daemon`), while only the bootstrap (ownerless) path hosts the daemon in the `request_http` child (`request_http/bootstrap/gui.py` `serve_gui`: "only explicit initialize can start a new daemon"). Unrelated to which home is used; a wheel user hits it too. Proposed fix location: `hyprial gui` hosts the daemon when a served home has none running and borrows a running one; separate PR after 2c; pending owner confirmation. Step 6a-0 measures first.
- Display name: "HyprialOS" (owner, 2026-11-15). Bundle identifier `com.hyprial.hyprialos`.
- Framing agreed with the owner: three launch states — (1) ownerless home: GUI hosts bootstrap, login/init in the GUI, in-process promotion (works today); (2) initialized home with a running daemon: GUI attaches (works today); (3) initialized home, no daemon running: refused today with GUI_DAEMON_UNAVAILABLE — the gap. Proposed: `hyprial gui` hosts the daemon in state 3. Pending owner confirmation.

## Owner decision (2026-11-15): Windows out of scope for now

Only Linux and macOS are built and verified. Corrected by the owner the same day: do NOT delete Windows-related tests, gates or CI targets; existing checks such as `scripts/ci/check_windows_portability.py` and the PR-template item keep running and must stay green. Windows-only problems are recorded and ignored for now: no new Windows code or tests, no blocking on them. Packaging targets are macOS arm64 and Linux x64; Windows items in this plan are not scheduled.
