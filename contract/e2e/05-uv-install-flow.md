# E2E-005: isolated uv install, upgrade, and uninstall flow

Status: executable; fully automated in a disposable local environment.

This scenario exercises the production-form `uv` install, upgrade, and
uninstall path using the persistent official GitHub source plus the explicit
one-shot Forgejo source. The runner maps both URLs to a test-owned bare remote,
so it can publish synthetic version boundaries without reading or writing a
real remote. Ambient uv
tools, caches, Python installations, and home state are not read or changed.

## Preconditions

- `uv`, `git`, and the current Python interpreter are available.
- The checkout is an installable Python package with an `hyprial` console script;
  its current version may be a development or stable release.
- Dependency downloads are allowed when the disposable uv cache is empty.

## Steps

1. Create temporary `UV_TOOL_DIR`, `UV_TOOL_BIN_DIR`, `UV_CACHE_DIR`, `HOME`,
   Git config, working clone, and bare remote directories.
2. Clone the current integration SHA, require its `uv.lock`, rewrite only the
   disposable clone to package version `0.4.0.dev0`, and publish tag
   `v0.4.0.dev0` to the bare remote.
3. Hand the wheel build hook a prebuilt `hyprial-tailcat` through
   `HYPRIAL_TAILCAT_PREBUILT` (taken from `HYPRIAL_TAILCAT_BINARY`; the runner
   reports BLOCKED without it), so every wheel this scenario builds bundles the
   sidecar under `hyprial/_bin/` (tailnet cutover G12; no separate sidecar
   release exists any more).
4. Run `uv tool install` with the Forgejo
   `HyprialOS/harness-bridge.git@v0.4.0.dev0` bootstrap requirement.
5. With the installed tool's own Python, call `locate_tailcat_sidecar` and
   `verify_tailcat_sidecar`; require the sidecar to resolve inside the installed
   wheel (`.../hyprial/_bin/hyprial-tailcat`), be executable, report the pinned
   Tailcat commit, and write no identity files.
6. Execute the installed `hyprial version --json`; require the expected initial
   package and semantic versions.
7. Commit an untagged `0.4.0rc1` early build, push a disposable branch so its
   commit is reachable, and install it once with
   `hyprial daemon upgrade --source forgejo --ref <commit>`.
8. Commit stable package version `0.4.0` and publish tag `v0.4.0`; run a plain
   `hyprial daemon upgrade` and prove it returns to the official GitHub source.
9. Execute `hyprial version --json`; require the official persistent source and
   `dependencyLock.status=match` with no mismatches.
10. Run `uv tool uninstall hyprial` and inspect both tool directories.

## Assertions

- The install uses isolated uv tool, bin, cache, and home directories.
- The installed `hyprial` exists, is executable, reports
  `packageVersion=0.4.0.dev0`, and retains `localVersion=0.4.0-dev.0`.
- The installed wheel bundles `hyprial-tailcat`: it is located inside the uv
  tool environment's `hyprial/_bin/`, is executable, its `version` reports the
  pinned Tailcat commit, and locating it writes no `secrets/login.json`.
- The one-shot command installs the exact Forgejo commit and still reports the
  official persistent source; the next plain `hyprial daemon upgrade` resolves the
  newly published `v0.4.0` tag through the official source and reports its exact
  `resolvedTag` and `resolvedCommit`.
- The final version report has `packageVersion=0.4.0`, `localVersion=0.4.0`,
  `persistentUpdateSource=https://github.com/Hyprial/hyprial.git`, and a matching
  dependency lock with no mismatches.
- An explicit `updateTrack: stable` resolves the movable `stable` tag as a
  no-op at the same commit (`track`/`resolvedTag`/`resolvedCommit` reported),
  and a forced track reinstall pins the exact commit as the PEP 610
  `requested_revision` (never the movable tag name).
- A missing track tag (`updateTrack: nightly` with no such tag) fails
  `UPGRADE_CHECK_FAILED` and never falls back to the highest version tag.
- Uninstall removes the `hyprial` executable and the `hyprial` tool environment.
- The source checkout, real Forgejo `dev`, ambient uv directories, and
  production Harness state remain unchanged.
