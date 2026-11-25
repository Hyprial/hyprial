# HyprialOS macOS package (native shell + bundled runtime)

This directory turns the native shell in `clients/native/` into a macOS arm64
`HyprialOS.app` and `.dmg` that run without Python, uv, Node or Git on the
user's machine. Plan: `docs/design/gui-single-launch-plans-2026-11-15/shell-6.md`
§D.4 (6c), §E.2, §E.3, §F; hand-over: `macos-handoff.md` stage 3.

Only macOS arm64 is packaged. Linux x86-64 appears in the runtime lock only for
the staging script's own tests and a later Linux package. There is no Windows
target (owner decision 2026-11-15).

| File | Role |
| --- | --- |
| `runtime-lock.json` | python-build-standalone (PBS) asset per target: URL, size, SHA-256; the hyprial wheel tag and the uv target triple |
| `stage_runtime.py` | builds `clients/native/src-tauri/runtime/` (git-ignored) from the locked PBS archive, the repository's locked dependencies and one hyprial wheel |
| `verify_runtime.py` | checks a staged runtime against its manifest and runs its interpreter in an isolated, ownerless home |
| `smoke_bundle.py` | launch smoke of a built `HyprialOS.app` (M4, M5 and the observable part of M6) |
| `sign_bundle.py` | Developer ID signing, notarization and stapling of a built app into a `-signed.dmg`; `--check-only` and `--verify-only` for the packaging script ("Developer ID signing") |
| `signing/modes.py` | `sign_bundle.py`'s `--check-only` and `--verify-only` (kept apart so `sign_bundle.py` stays under 1000 lines) |
| `signing/notary.py` | `sign_bundle.py`'s notarization: submit, the id from `notarytool history` during the upload, wait, log |
| `signing/failure.py` | `sign_bundle.py`'s cleanup and marking of a failed run's unfinished output (no Apple tool) |
| `signing/archives.py` | archive and Mach-O scan, used by `sign_bundle.py` and, on its own, by the ad-hoc `verify_bundle` stage (no Apple tool) |
| `tauri.bundle.json` | the only place the runtime enters the bundle: `bundle.resources` `runtime/` → `Contents/Resources/runtime/`, passed with `--config` at package time |

Each script carries its decisions in its file header. Tests:
`tests/tooling/release/shell/`. The single packaging script is `scripts/build/package-macos-shell.sh`;
it runs only by hand: no workflow packages, signs or notarizes (owner decision
2026-11-20, "Package by hand").

## Build locally on a Mac

One script does the packaging (owner decision 2026-11-17):
`scripts/build/package-macos-shell.sh`. Run it on your Mac directly; since
2026-11-20 there is no packaging workflow at all ("Package by hand" has the
release procedure).

```sh
bash scripts/build/package-macos-shell.sh
```

Prerequisites: macOS 13 or newer on arm64 (the bundle declares
`bundle.macOS.minimumSystemVersion` `13.0`, the wheel's `macosx_13_0_arm64`
tag), Xcode command line tools, uv 0.11.7, Rust 1.93.1, Node 24 for the GUI
build inside the wheel, and Python 3.12. The script pins Python 3.12 itself
(`PYTHON_VERSION`, exported as `UV_PYTHON` before any uv call, so a uv whose
default is a free-threaded 3.14 cannot build the cp312-abi3 extension wrongly);
the `toolchain` stage prints the interpreter uv actually selects and refuses
anything but a 3.12 with the GIL. A `UV_PYTHON` of your own is overridden. Node 24 is taken from `PATH` when the `node` there is
v24; otherwise the script looks only in the keg-only Homebrew formula
(`brew install node@24`, `/opt/homebrew/opt/node@24/bin`, or the directories in
`NODE24_BIN_CANDIDATES`) and puts it in front of `PATH` for the run. It never
downloads Node and never uses another major. Network access is needed for the
runtime lock's PBS archive, the locked Python dependencies and `cargo install`.

Options (`--help` lists them):

| Option | Meaning |
| --- | --- |
| `--out <dir>` | Where the results go. Default `dist/shell-macos` (git-ignored, emptied each run); any other directory must not exist or be empty. |
| `--tauri-cli-dir <dir>` | Install `tauri-cli` into `<dir>` and reuse an install of exactly the pinned version (developer convenience, saves a compile). Without it a fresh temporary directory is used and removed at exit, never a persistent tool cache (owner decision on PR #2889). |
| `--skip-smoke` | Skip the two launch smokes, for a machine without a window server. The summary says SKIPPED; it is never a pass. |
| `--source-sha <40-hex>` | Refuse (exit 2, before anything is built or deleted) unless `HEAD` is that commit and the tree is clean (untracked files count). Always pass it for a package meant for anyone else. |

Exit status: `0` every required stage passed; `1` a build or verification
stage failed (the summary names it); `2` a usage error or a refused input
(unknown option, malformed or wrong or dirty `--source-sha`, non-empty
`--out`). The script unsets every `APPLE_*` and `TAURI_SIGNING_PRIVATE_KEY*`
variable at start (Tauri signs with a Developer ID and notarizes when the
`APPLE_*` ones are set) and prints the names it cleared, never a value.

What the stages do, in order (each prints `==> <stage>`; the first failure
stops the build path, collect still runs, and the summary at the end lists
every stage as PASSED, FAILED, SKIPPED or NOT RUN, and the stage line of a stage
that did not run says `NOT RUN` too; a non-zero exit when a required stage
failed):

1. `source` records the commit and whether the tree is dirty. A dirty local
   build is allowed without `--source-sha`, but the summary and
   `BUILD-INFO.txt` say so.
2. `host` asserts macOS arm64; `node` selects Node 24 as above; `toolchain`
   checks uv, cargo, Node 24 and an `npm` that runs under Node 24 against the
   pins (the version words are compared exactly: 0.11.70 is not 0.11.7).
3. `tauri_cli` runs `cargo install tauri-cli --version 2.12.2 --locked`.
4. `wheel` is `scripts/build/build-wheel.sh` into `dist/shell-macos-work/wheel`.
5. `stage` is `stage_runtime.py` (runtime into
   `clients/native/src-tauri/runtime/`, git-ignored); `verify_staged` is
   `verify_runtime.py` on that tree (M2).
6. `build_app` is `cargo tauri build --bundles app,dmg --config
   packaging/clients/tauri.bundle.json -- --locked` with `CI=true` (M3); an
   earlier `.app` and `.dmg` are removed first.
7. `verify_bundle` is `codesign --verify --deep --strict` plus
   `verify_runtime.py` on `Contents/Resources/runtime`, the interpreter inside
   the bundle.
8. `smoke_sigterm` and `smoke_kill9` run `smoke_bundle.py` (M4: SIGTERM with a
   second start; M5: kill -9, then a restart with the app-chosen home). They
   are independent launches: both run when the first fails, each reports its
   own result, and they are NOT RUN only when an earlier build-path stage failed.
9. `collect` puts the `.dmg`, `runtime-manifest.json`, the evidence JSON files,
   `SHA256SUMS` and `BUILD-INFO.txt` (commit, dirty flag, tool versions, stage
   summary) in the output directory. When the build did not succeed it writes
   `NOT-BUILT.txt` naming the first stage that did not succeed instead of an
   empty `SHA256SUMS`.

Without `--developer-id` the script signs ad hoc only (Tauri does that), never
signs with a Developer ID, never notarizes, never publishes or uploads and reads
no secret; with it, see "Developer ID signing" below. It lists, and
never deletes, the WebKit data the smoke leaves in the account's `~/Library`
(KP-096).

The isolated socket parent the verifier and smoke use is `/tmp/hsv.*` and
`/tmp/hsm.*` (`SHORT_TMP` overrides `/tmp`); it must be short because the macOS
AF_UNIX limit is 103 bytes. Every stage removes it when empty on every exit
path, failure included.

### Daemon of the isolated home

Since the daemon-first change `hyprial gui` ensures the daemon of the selected
home in every state (an ownerless home included) and closing the GUI never stops
it (owner's model, not a defect; pinned by
`tests/integration/gui_daemon_first/test_gui_command_ownerless.py`). So after a
run the home used by the verifier or the smoke has one daemon: a python process
in its own session holding `<state>/daemon.lock` and publishing its pid in
`<state>/daemon.json` (serving) or `<state>/bootstrap.json` (bootstrap).

- The daemon is expected only when the pid named by the home's own record is
  alive and holds the isolated root. `verify_runtime.py` (step `daemon`) and
  `smoke_bundle.py` (steps `daemon`, `daemon-stop`) stop it with
  `hyprial daemon stop --json` for that home through the bundled runtime and
  confirm the pid exited (`--daemon-stop-timeout`, default 30 s).
- They FAIL when a record names a live pid that does not hold the home, when a
  process running `daemon run` on the root is not the recorded one (a second
  daemon), when more than one recorded daemon is live, when `daemon stop`
  fails, and when the pid outlives the bound. In the last case
  `kill_carriers` was the fallback; the step says so and the evidence lists
  `fallbackKilled`. The leftover check (`children`, `orphans`) stays as strict
  as before for every other process.
- M5 (`--stop kill9 --app-home`): the daemon must survive the kill -9 with the
  same pid and be the only one; the restarted app must attach to that same
  daemon (same pid, still exactly one) before it is stopped.
The overlay is not in `tauri.conf.json`, so `cargo check`/`cargo test` of the
shell never need a staged runtime.

Smoke by hand (needed when `--skip-smoke` was used or the machine has no window
server; starts a real `hyprial gui` with the bundled runtime on a throwaway
home and refuses to run while another `hyprial-desktop` is running):

```sh
uv run --no-project --python 3.12 python packaging/clients/smoke_bundle.py \
  --app "$PWD/clients/native/src-tauri/target/release/bundle/macos/HyprialOS.app" \
  --root-parent "$(mktemp -d /tmp/hsm.XXXXXX)" --stop sigterm --second-start
uv run --no-project --python 3.12 python packaging/clients/smoke_bundle.py \
  --app … --root-parent … --stop kill9 --app-home
```

## The bundled runtime

`Contents/Resources/runtime` holds a PBS CPython 3.12 (`install_only_stripped`),
the repository's locked product dependencies (`uv export --locked --no-dev
--no-emit-project`, installed with `--require-hashes` for the lock's target
triple) and the hyprial wheel for the commit being packaged. The shell starts
`runtime/bin/python3.12 -I -m hyprial.cli gui --no-open --json --port 0
--stdin-stop`; `HYPRIAL_SHELL_LAUNCHER` still overrides the program
(development, tests). There is no first-run environment build and no uv in the
package (plan OD-1, option P1).

- No symlink anywhere: PBS links are aliases and are skipped.
- Pruned: pip/idle scripts, headers, man pages, Tcl/Tk, the stdlib `test`,
  `idlelib`, `tkinter`, `turtledemo`, `ensurepip`, the static
  `config-3.12-*`, the bundled pip, upstream `__pycache__`.
- Bytecode is compiled at staging time (`unchecked-hash`, fixed hash seed,
  single-process `compileall -j 1`), so a launch never writes `__pycache__`
  into the signed bundle; the smoke checks `codesign --verify` and the manifest
  after every run. A module that fails to compile refuses the staging (on the
  Linux lock entry all 15062 modules compile; there is no exclusion list).
- `runtime/runtime-manifest.json` records the target, Python, PBS tag and
  SHA-256, the wheel (file, version, SHA-256), the source commit and SHA-256 plus
  mode of every file. Staging twice gives identical bytes: compilation is
  single-process because with `-j 0` the marshal reference flags depend on
  which worker compiled which file (three stagings of the real Linux tree
  differed in three `.pyc` files before the fix). Checked on Linux for the
  Linux lock entry; the macOS check is M1 on the Mac.

Measured on macOS 26.5.2, M3 Ultra (PR 6c): runtime about 380 MB on disk,
`HyprialOS.app` about 340 MiB, `.dmg` about 114 MiB. The PR body carries the
exact sizes and SHA-256 of the candidate.

A wheel built outside a release records its source remote in
`hyprial/_gui/release.json` (the build prints a notice); a candidate `.dmg`
built that way is for internal testers only and is not distributed.

## App home

The app runs `hyprial gui` on its **own** hyprial home, never the CLI's default
`~/.hyprial`: the daemon requires an exact package-version match with the
`hyprial` that runs `hyprial gui`, and a home has one lock holder, while the
app carries its own hyprial version.

- Default: `~/.hyprialos`, created with mode 0700 when missing (`hyprial gui`
  refuses a missing home with `GUI_HOME_NOT_INITIALIZED`). The state directory
  is `~/.hyprialos/state`; an inherited `HARNESS_STATE_DIR` is removed so a
  terminal's state directory can never be paired with the app home.
- An explicit, non-empty `HYPRIAL_HOME` in the app's environment wins and is
  passed through unchanged together with its `HARNESS_STATE_DIR`. Tests and
  smokes use this (or a redirected `HOME`) and never touch the real app home.
- Socket lengths decide the location. Under a home the product binds
  `<home>/state/daemon.sock` (home + 18 bytes) and, when a DSH TUI agent
  runtime starts, `<home>/state/dsh-tui-<16 hex>/bridge.sock` (home + 43,
  capped at 100 bytes by DSH itself); smolvm and tmux sockets live under
  `/tmp`. The macOS limit measured here is 103 bytes. With `~/.hyprialos`
  the daemon socket fits a user home of up to 74 bytes and the DSH socket one
  of up to 46 bytes (a 39-character user name under the 7-byte macOS user-home prefix). Under
  `~/Library/Application Support/com.hyprial.hyprialos` the daemon socket
  would fit only up to a 35-byte user home and the DSH socket never.
- Owner question: confirm `~/.hyprialos` (name and location).

Decision record in code: `clients/native/launcher/src/home.rs`.

## App data and uninstall

| Location | Written by | Follows a redirected `HOME`? |
| --- | --- | --- |
| `/Applications/HyprialOS.app` (or wherever the user copied it) | the user | — |
| `~/.hyprialos` | `hyprial gui` and the daemon of the app home | yes |
| `~/Library/Logs/com.hyprial.hyprialos/launcher.log` (stderr of the CLI, 0600) | the shell | yes (measured) |
| `~/Library/WebKit/com.hyprial.hyprialos`, `~/Library/Caches/com.hyprial.hyprialos`, `~/Library/HTTPStorages/com.hyprial.hyprialos.binarycookies` | WKWebView | **no**: always the real account's `~/Library` (measured) |

The shell configures no webview data directory, so WKWebView uses its default
per-bundle-id store. The GUI session lives in memory on the host side. The one
value the GUI keeps in `localStorage` (the Squire identity store) is per origin,
and the origin's port changes on every launch, so it is not kept between
launches (KP-095).

Uninstall:

1. Quit HyprialOS (the app stops its `hyprial gui`).
2. If a daemon runs for the app home (an initialised home), stop it with the
   bundled CLI:
   `HYPRIAL_HOME=~/.hyprialos /Applications/HyprialOS.app/Contents/Resources/runtime/bin/python3.12 -I -m hyprial.cli daemon stop`
3. Delete the app, `~/.hyprialos` (this deletes the app home's identity and
   messages) and the four `~/Library` entries above.

The default home `~/.hyprial` and its OS service are not touched by any of this.

## Signing

### Now: ad-hoc

`tauri.conf.json` sets `bundle.macOS.signingIdentity` to `"-"` and
`hardenedRuntime` to `false`. Apple Silicon needs at least an ad-hoc signature
to execute; Tauri applies it to the app. The Mach-O files in
`Contents/Resources/runtime` keep the linker's ad-hoc signatures from PBS and
the wheels and are sealed as resources of the app.

What a tester sees after downloading the `.dmg`: the quarantine attribute makes
Gatekeeper refuse the first open. Since macOS 15, Control-click → Open no longer
bypasses it; open once, then System Settings → Privacy & Security → "Open
Anyway". An internal tester may instead run
`xattr -dr com.apple.quarantine /Applications/HyprialOS.app`. Whether the bundled
interpreter then runs without a further prompt is UNKNOWN until checked on a
second Mac (M8). A build run in place on the machine that built it carries no
quarantine attribute and shows no prompt.

### Developer ID signing

History: deferred on 2026-11-17 (owner direction on PR #2899: no workflow
signs). Reversed on 2026-11-19 (KP-100): the machine owner chose to sign on
the packaging Mac, which is also the `release-macos-arm64` runner, through a
manual workflow. Changed again on 2026-11-20: packaging leaves CI entirely,
that workflow is deleted and signing is done only by running the script by
hand ("Package by hand"). The
repository default does not change: `tauri.conf.json` keeps `signingIdentity`
`"-"` and `hardenedRuntime` `false`, and the package script without the switch
behaves exactly as before. Signing is done after the Tauri build by
`sign_bundle.py` with `codesign`, opened only by an explicit switch:

```sh
bash scripts/build/package-macos-shell.sh --developer-id \
  --discover-identity --notary-profile <profile> \
  --source-sha "$(git rev-parse HEAD)"
```

(The complete release command, with the source remote, is in "Package by
hand".)

| Option | Meaning |
| --- | --- |
| `--developer-id` | The switch. Needs `--source-sha` (clean tree at a named commit), one of the two identity options and `--notary-profile`; refuses `--skip-smoke`. Without it the options below are a usage error (exit 2). |
| `--identity <40-hex>` | SHA-1 of the Developer ID Application identity (`security find-identity -v -p codesigning`). |
| `--discover-identity` | Use the single valid Developer ID Application identity; refused when there is none or more than one. |
| `--notary-profile <name>` | The stored notarytool credentials profile of the account. |
| `--notary-timeout-minutes <n>` | Wait per notary submission, 1..1440; default `sign_bundle.py`'s 30, which "Package by hand" keeps. |

Stages with the switch: `source host node toolchain sign_preflight tauri_cli
wheel stage verify_staged build_app verify_bundle sign verify_signed
smoke_sigterm smoke_kill9 collect`.

- `sign_preflight` (`sign_bundle.py --check-only`) runs before the long build:
  the identity is a valid Developer ID Application identity, a test signature
  of a scratch copy of `/usr/bin/true` with it succeeds within 120 seconds
  (then removed), and the profile answers `notarytool history`. No submission
  is spent. A locked keychain or a wrong identity or profile fails the run
  here, in minutes, instead of hanging (every child process has a timeout
  and no stdin).
- `verify_bundle` still checks the ad-hoc build, and in both modes runs
  `packaging/clients/signing/archives.py` on it: an archive the signing tool
  refuses (one holding Mach-O, an unreadable one, tar, gzip, bzip2, xz,
  zstd, xar or a static library) fails the ordinary build early. Static
  libraries stay refused: they are not loaded at run time and the app does
  not need them, codesign cannot sign the objects inside, and whether Apple's
  notary service accepts unsigned objects inside an `.a` was not measured
  (the measured bundle has none); there is no decision to prune them.
  `sign` copies the app, signs
  every Mach-O inside out with hardened runtime and a secure timestamp, no
  entitlements, regenerates `runtime-manifest.json`, seals the app, notarizes
  the app (zip) and staples it, builds the dmg from the stapled app, signs,
  notarizes and staples the dmg: two submissions per run, so an app copied out
  of the dmg also launches offline. Its file header holds the decisions.
- `verify_signed` (`sign_bundle.py --verify-only`) re-checks the final
  artifact: `codesign --verify --deep --strict`, the Developer ID, Team ID,
  hardened runtime and timestamp of the app and every inner item, the leaf
  certificate, the dmg's signature, `stapler validate` of app and dmg,
  `spctl --assess` of both (`source=Notarized Developer ID`), the checksums,
  the manifest and the launch of the signed interpreter, and records the
  SHA-256 of the dmg, manifest and `SHA256SUMS` it checked. Last
  (`--extract-to`) it attaches the final dmg read-only, copies the app out
  with `ditto` to `dist/shell-macos-work/final`, gives the copy a
  `com.apple.quarantine` value of the browser-download form, checks the copy
  and detaches. Both smokes launch THAT copy. A smoke executes the binary
  directly, not through LaunchServices, so there is no app translocation and
  no first-launch dialog: this is not the Finder first launch.
- Collect copies the dmg and manifest, regenerates `SHA256SUMS` and requires
  all three to be byte for byte what `verify_signed` checked (and the dmg the
  one it attached); any difference collects nothing signed.
- Output when every stage passed: `HyprialOS_<version>_aarch64-signed.dmg`
  (never the ad-hoc name), the regenerated `runtime-manifest.json`,
  `SHA256SUMS`, `BUILD-INFO.txt` (the usual record plus signing method, Team
  ID, notary submission ids, Gatekeeper verdicts and
  `distributable: internal verification only|no`) and the evidence JSON (`sign-preflight.json`,
  `sign-bundle.json` with the notary logs next to it, `verify-signed.json`,
  the smokes).
- Truthful when stopped: `NOT-SIGNED.txt` ("did not finish") is
  written as soon as the output exists and removed only on success, last;
  `UNFINISHED.txt` marks the signed work directory and the final copy
  directory from the moment they exist until a passing collect
  (`sign_bundle.py --mark-unfinished` writes it before the rename into
  `signed/` and before the app is copied into `final/`), so a run killed
  before collect (SIGKILL, or a signal that ends the script's shell) leaves
  them. Ctrl-C during a smoke interrupts the smoke, which fails its stage;
  the script goes on to collect, which removes the signed work and writes
  `NOT-SIGNED.txt` (exit 1; measured 2026-11-20 with the smoke before
  PR #3022). The interrupted smoke cleans up after itself (PR #3022): it
  prints `BLOCKED runner interrupted by SIGINT`, exits 130 and stops the app
  it started and the isolated home's daemon, also when a second Ctrl-C
  arrives during that cleanup (measured 2026-11-20 on the Mac: `smoke_bundle.py
  --hold`, two SIGINTs to its process group 0.2 s apart, no process and no
  file left under the isolated root), so there is nothing to stop by hand;
  `sign_bundle.py` rewrites its evidence after every step and, while
  `notarytool submit` (without `--wait`) uploads, asks `notarytool history`
  every 15 seconds for the new submission and saves its id as soon as Apple
  lists it (measured: Apple lists it in the first second, notarytool itself
  printed nothing during a 5-minute upload), then runs `notarytool wait`.
- Fail closed: any failed stage exits 1. No path falls back to ad hoc. A
  failed signed run collects no dmg, no checksums and no manifest; the ad-hoc
  dmg of `build_app` is never collected in this mode (an ad-hoc package is a
  run without the switch); the signed work directory
  `dist/shell-macos-work/signed` is removed even when only a smoke failed; the
  output holds `BUILD-INFO.txt` (`signing: FAILED ...`), `NOT-SIGNED.txt`
  (first stage that did not succeed, notary submission ids already spent: do
  not resubmit) and the evidence.
- Distribution: BUILD-INFO.txt says `internal verification only` when the
  wheel inside records the public source remote (checked on the built wheel
  with `scripts/ci/wheel_source_remote.py`), otherwise `no`; never `yes`.
  The build hook writes it when `HYPRIAL_BUILD_SOURCE_REMOTE` is set by the
  caller (the hook accepts only the public repository address; the
  "Package by hand" commands set it); a run without it gives a dmg for
  internal testers only. Even with it, BUILD-INFO.txt says "internal
  verification only": until the repository has moved to GitHub signed
  packages are not distributed externally (see "Package by hand").
- What "signed" guarantees: signed with the Developer ID in the login keychain of the account that ran the script,
  notarized by Apple and stapled (app and dmg), accepted by Gatekeeper on the
  packaging Mac, and both launch smokes passed on exactly that app. It does not
  say the artifact is distributable (see above) or that a second Mac was tried.
- Prerequisites on the Mac: the identity and the notary profile in the login
  keychain ("Installing the signing identity") and an unlocked keychain. After
  a reboot without a GUI login the login keychain is locked and the run fails
  in `sign_preflight` or at the first `codesign`; how to unlock it is an open
  question (KP-100). Each run depends on Apple's notary and timestamp services
  (one early submission waited 62 minutes).
- Security: repository checks only keep packaging and signing from being
  switched on in CI by accident
  (`tests/tooling/release/shell/signing/test_signing_only_by_switch.py`: no
  workflow calls the script or `sign_bundle.py`, passes the switch, signs or
  notarizes; and `signing/policy/`). That rests on the absence of a
  workflow, not on isolation: the identity is in the login keychain of the
  account the runner process also uses, so every job on this Mac can use it
  while the keychain is unlocked, among them `native-conformance.yml`'s
  `native-darwin-arm64`, triggered by pull requests. Which commit gets
  signed is the operator's choice ("Package by hand"); nothing checks that
  it is on the mainline. The control points are on Forgejo (who can push
  and open pull requests; owner 2026-11-19: internal accounts only) and in
  the keychain (which programs may use the key); before the repository
  goes public the identity must leave the account that runs pull-request
  jobs, or no such job may run there (KP-100).

### Installing the signing identity

The machine owner installs the Developer ID Application identity and a
notarytool profile on the packaging Mac by hand, over SSH, without Xcode
(Command Line Tools are enough). It only prepares the identity; nothing is
signed or notarized. In a checkout on that Mac, run one command and answer
its questions:

```sh
./scripts/build/import-app-dev-id.sh
```

It checks the Mac, makes the private key and CSR in `~/devid-private` (or a
directory you choose), tells you what to do at developer.apple.com, takes the
certificate back as a file or as pasted `base64 -b 64 -i developerID_application.cer`
output (handy when the browser is on another computer; a terminal does not take
a line much over 1000 characters, so the base64 must be wrapped), installs it (asking
for the keychain password itself), optionally stores notarytool credentials
(suggesting the Team ID from the certificate, which you confirm), checks that
signing works (if the keychain is locked, as in a new SSH session, it offers to
run the import again, which unlocks it) and, after a passing check, offers to
delete the private directory (`yes` typed in full). Run it again after an interruption: it continues from what already
exists. `q` stops at any prompt.

The guide only drives `scripts/build/setup-macos-signing-identity.sh`, whose
file header holds the decisions and measurements; its subcommands are the
manual fallback:

```sh
bash scripts/build/setup-macos-signing-identity.sh csr --dir ~/devid-private
# browser: developer.apple.com → Certificates → "+" → Developer ID Application (G2 Sub-CA),
# upload ~/devid-private/devid.csr, download the certificate to
# ~/devid-private/developerID_application.cer on this Mac
bash scripts/build/setup-macos-signing-identity.sh import --dir ~/devid-private \
  --cer ~/devid-private/developerID_application.cer
bash scripts/build/setup-macos-signing-identity.sh notary --profile <PROFILE> \
  --apple-id <APPLE_ID> --team-id <TEAM_ID>
bash scripts/build/setup-macos-signing-identity.sh check --profile <PROFILE>
```

- `csr` asks for the CSR's e-mail and common name when they are not given,
  creates the directory with mode 0700 outside any git work tree and never
  overwrites a key. After `import`, delete or move that directory: it holds
  the unencrypted private key, which then lives in the keychain.
- `import` asks once for the keychain password (hidden; it reaches `security`
  only on stdin), checks that the certificate matches the key and is a
  Developer ID Application certificate, checks Apple's Developer ID G2
  intermediate against a SHA-256 pinned in the script (`--intermediate <file>`
  when the download fails), and gives the key to `/usr/bin/codesign` only.
  Running it again changes nothing that is already installed.
- `notary` lets notarytool ask for the app-specific password; the script never
  takes it as an option. `check` is read-only: it signs a throw-away copy of
  `/usr/bin/true` and, with `--profile`, asks Apple whether the profile works.
- SSH: the login keychain is locked in an SSH session and must be unlocked in
  every session (`import` does it; before `notary` or `check` run
  `security unlock-keychain ~/Library/Keychains/login.keychain-db`). A CI
  runner started in the GUI session has a different keychain state; whether a
  job there could use the identity is UNKNOWN until tried.
  Measured 2026-11-19 by a probe run: a `release-macos-arm64` job, running in
  the logged-in GUI session with the login keychain unlocked, signs without a
  prompt (hardened runtime and timestamp) and queries the notary history.
- Security consequence (accepted by the owner, KP-100): the CI runner on this
  Mac runs under the same macOS account, so once the identity is in the login
  keychain, any workflow on any pushed branch of any repository that can use
  the runner can sign with it while the keychain is unlocked. The cheaper
  mitigation is a separate keychain (`--keychain <path>` on `import`, `notary`
  and `check`; the script never creates it: whether `security
  create-keychain` adds it to the account's search list was not measured, so
  check `security list-keychains -d user` afterwards), kept off the search
  list and unlocked only when signing; codesign then needs
  that keychain on the search path of the signing process (measured for PR #2907:
  `codesign --keychain <file>` alone does not find the identity).
- Measured only with a self-signed placeholder in temporary keychains. A real
  Developer ID certificate, the real login keychain and a real notarytool
  profile are UNKNOWN until the owner runs the commands above.

## Package by hand

Owner decision 2026-11-20: packaging is not done in CI. No workflow builds,
signs or notarizes the macOS package (the former `shell-macos-candidate.yml`
and `shell-macos-signed.yml` are deleted); an operator runs
`scripts/build/package-macos-shell.sh` on the packaging Mac. The commands
below are the whole procedure; `--help` lists every option.

Prerequisites (both forms):

- The tools listed in "Build locally on a Mac" (macOS 13+ on arm64, Xcode
  command line tools, uv 0.11.7, Rust 1.93.1, Node 24, network access), a
  logged-in GUI session for the two launch smokes, and no other
  `hyprial-desktop` running.
- A clean checkout of the chosen commit (`--source-sha` refuses anything
  else, untracked files included) and an empty or new `--out` directory.
- **The operator chooses the commit.** It must be on the mainline (`dev`, or
  `main` for a release). The script does not check this (it never contacts
  the remote); the first line of each command below does, and stops when
  the commit is not on `origin/dev`. For a release replace that line with
  `git fetch origin && git merge-base --is-ancestor <source-sha> origin/main || exit 1`.
  Run each block as a script (`bash -e`, or paste it into one): in an
  interactive shell `exit 1` closes the shell.
- The public source remote: set `HYPRIAL_BUILD_SOURCE_REMOTE` to the value
  of `PUBLIC_SOURCE_REMOTE` in `src/hyprial/shell/impl/gui/install/resources.py`
  (the only value the build hook accepts; the script never sets it). Without
  it the wheel records the checkout's internal origin (a signed run's
  BUILD-INFO.txt then says `distributable: no`). Check a built wheel with
  `uv run --no-project --python 3.12 python scripts/ci/wheel_source_remote.py dist/shell-macos-work/wheel/<wheel>`.
- tauri-cli: nothing to install by hand. The `tauri_cli` stage runs
  `cargo install tauri-cli --version 2.12.2 --locked` into a fresh temporary
  directory removed at exit; `--tauri-cli-dir <dir>` keeps the install in
  `<dir>` and reuses it on the next run when its version is exactly the pin.

Ad hoc (an internal candidate; no Developer ID, no notarization):

```sh
git fetch origin && git merge-base --is-ancestor <source-sha> origin/dev || exit 1
git checkout --detach <source-sha>
HYPRIAL_BUILD_SOURCE_REMOTE=https://github.com/Hyprial/hyprial.git \
  bash scripts/build/package-macos-shell.sh \
  --source-sha <source-sha> --out "$PWD/dist/shell-macos-adhoc"
```

Developer ID (signed, notarized, stapled), additionally needs the identity and
the notary profile in the unlocked login keychain ("Installing the signing
identity"); `<sha1>` comes from `security find-identity -v -p codesigning`,
or use `--discover-identity` instead of `--identity <sha1>` when exactly one
Developer ID Application identity is installed:

```sh
git fetch origin && git merge-base --is-ancestor <source-sha> origin/dev || exit 1
git checkout --detach <source-sha>
HYPRIAL_BUILD_SOURCE_REMOTE=https://github.com/Hyprial/hyprial.git \
  bash scripts/build/package-macos-shell.sh --developer-id \
  --identity <sha1> --notary-profile <profile> \
  --source-sha <source-sha> --out "$PWD/dist/shell-macos-signed"
```

Each run spends two notary submissions (app zip, then dmg). Each waits up to
`sign_bundle.py`'s default 30 minutes (`--notary-timeout-minutes` lengthens
it); one wait of 62 minutes has been observed, most take 2 to 4. A failed or
timed-out run names the submission ids already spent in `NOT-SIGNED.txt`
and `sign-bundle.json`: do not resubmit; check them later with
`xcrun notarytool info <id> --keychain-profile <profile>`.

Where the outputs land:

- `--out`: the dmg (`HyprialOS_<version>_aarch64.dmg` ad hoc,
  `HyprialOS_<version>_aarch64-signed.dmg` signed), `runtime-manifest.json`,
  `SHA256SUMS`, `BUILD-INFO.txt` and the evidence JSON; on failure
  `NOT-BUILT.txt` (and `NOT-SIGNED.txt` for a signed run) instead of the dmg.
  A signed run writes `NOT-SIGNED.txt` when it starts and removes it last,
  only when every stage passed:
  only an `--out` without `NOT-SIGNED.txt` or `NOT-BUILT.txt` counts.
- Ad hoc app: `clients/native/src-tauri/target/release/bundle/macos/HyprialOS.app`.
- Signed work: `dist/shell-macos-work/signed` (the verified dmg, and the
  stapled app under `app/HyprialOS.app`) and `dist/shell-macos-work/final/app`
  (the app copied out of the dmg that the smokes launched). These are working
  copies, never the result: both directories hold `UNFINISHED.txt` from the
  moment they exist (`dist/shell-macos-work/signed/UNFINISHED.txt` and
  `dist/shell-macos-work/final/UNFINISHED.txt`) and only a passing collect
  removes them, so a notarized dmg or app left there by a run that never
  reached collect is marked as such. Collect also re-checks that `HEAD` is still `<source-sha>`
  and the tree still clean; a change during the run collects nothing.

Verify a signed result (the script already did all of this in
`verify_signed`; these repeat it by hand):

```sh
shasum -a 256 -c SHA256SUMS                     # in the --out directory
spctl --assess --type open --context context:primary-signature -vv \
  dist/shell-macos-signed/HyprialOS_<version>_aarch64-signed.dmg
spctl --assess --type execute -vv dist/shell-macos-work/signed/app/HyprialOS.app
xcrun stapler validate dist/shell-macos-signed/HyprialOS_<version>_aarch64-signed.dmg
xcrun stapler validate dist/shell-macos-work/signed/app/HyprialOS.app
uv run --no-project --python 3.12 python packaging/clients/sign_bundle.py \
  --verify-only --identity <sha1> --out dist/shell-macos-work/signed \
  --evidence "$(mktemp -d)/reverify.json" --root-parent "$(mktemp -d /tmp/hsv.XXXXXX)"
```

`spctl` must print `accepted` and `source=Notarized Developer ID`;
`--verify-only` (`--discover-identity` works in place of `--identity <sha1>`)
ends with a PASS verdict line.

Distribution: BUILD-INFO.txt says `distributable: internal verification
only` even with the public remote. Until the repository has moved to GitHub,
packages are for internal verification only; afterwards an externally
distributed package must be built from a commit that already exists on
GitHub (KP-100). The build never contacts GitHub to check this.
