# E2E-032: switching the viewed organization in `hyprial gui`

Runner: `run-org-switch-browser.py` (isolated; runs in
`.forgejo/workflows/gate-gui.yml` `gui-acceptance`, see "CI" below).
Scenario package: `_org_switch/` (`phases.py` for the Python run and
assertions, `browser.mjs` for the browser steps). Shared browser-scenario
pieces: `_gui_browser/` (`session.py` for the processes, `page.mjs` for the
browser launch, the handoff and the request recorder).
Feature: the org page of the GUI served by `hyprial gui`; the viewed-org
selector (查看组织) next to the org title, the org detail read for the newly
viewed org, and the 刷新 action on the 成员 section header.

This is acceptance row 4 (old id F3) of
`docs/design/gui-single-launch-plans-2026-11-15/1e-4-stage-f-acceptance.md`:

> Switching the viewed org loads that org's detail (`org.network` with the
> second org's id) without detouring through the overview refresh; the
> machines page keeps a refresh action; no automatic `org.fetch`.

Two parts of that wording no longer match `dev` and are restated here as
asserted:

- The top-level 机器 tab was removed on 2026-11-16. The machine list is the
  设备 segment of 成员, and the refresh action is the 刷新 button on the 成员
  section header. "The machines page keeps a refresh action" is asserted as:
  成员 → 设备 opens, the 成员 header has an enabled 刷新 button, clicking it
  rereads the org list and the viewed org's detail, and no top-level 机器 tab
  exists.
- The two organizations are created by two local `hyprial org create` calls on
  the isolated home. Nothing is joined.

## Preconditions

- `uv sync --extra test`, the native actor engine
  (`scripts/build/build_native_engine.sh`), Node and npm on `PATH`, and
  `npm --prefix gui ci`. From a source checkout `hyprial gui` runs the GUI
  production build itself (`npm run build`) before serving `gui/dist`, so
  installed `gui/node_modules` is the prerequisite, not a prebuilt `dist`.
- A browser found by `gui/tests/support/browser.mjs`:
  `HYPRIAL_BROWSER_EXECUTABLE`, an installed Google Chrome, or the Chromium of
  the `playwright-core` version locked in `gui/package.json`. It runs
  headless. The repository has no Python browser driver; the runner calls a
  Node script for the browser part.
- Any missing prerequisite (including a timed-out, errored, non-zero, or
  non-object JSON browser probe) is BLOCKED (exit 2) with the reason, checked
  before any process starts. BLOCKED is never a pass.
- `real_environment_required: false`: one throwaway root under the short temp
  root (`/private/tmp` on macOS, `HYPRIAL_TEST_TMP_ROOT` when set) holding
  `HOME`, `HYPRIAL_HOME` and `HARNESS_STATE_DIR` for daemon/GUI children, and
  isolated `HOME`, XDG config/cache/data/state, `TMPDIR`/`TMP`/`TEMP` and
  `npm_config_cache` (0700) for the Node/browser process (keeping
  `PLAYWRIGHT_BROWSERS_PATH` or an existing host default Playwright browser
  cache when unset). The runner refuses a root under the account HOME. No
  credential, no identity issuer, no `hyprial init`, no `hyprial login`.
- The home is prepared by `python -m tests.support.gui_home prepare --home
  <root>/home --state-dir <root>/state --owner e2e-org-switch --json`, which
  commits the owner and a synthetic device record so the connected GUI treats
  the home as device-ready. It writes no first-run record; the runner sends
  the real request `onboarding.firstRun.skip`.
- The daemon is `hyprial daemon run`, a plain child of the runner in its own
  process group, with `HYPRIAL_NETWORK_ISOLATED=1`, empty Zenoh endpoints,
  `HYPRIAL_PEER_DISCOVERY=0`, `HYPRIAL_SERVICE_AUTOINSTALL=0` and
  `HYPRIAL_DAEMON_AUTOSTART=0`, and without proxy variables. It must report
  itself isolated (`IsolatedDaemon.require_network_isolated`). `hyprial gui`
  rejects `HYPRIAL_OWNER` (`GUI_OWNER_OVERRIDE`), so neither child carries it;
  the owner comes from the prepared settings.
- `hyprial gui --no-open --json --port 0 --stdin-stop` is the second child,
  with the same environment, the daemon driver and the default session TTL.
  Its startup record's `handoff_path` must be a 0600 regular non-symlink
  `launch.url` owned by the current user inside the throwaway root, and `url`
  must be a loopback `http` URL matching `port`.
- Budgets: daemon socket 60 s; GUI startup record 300 s (it includes the
  production build); browser script 180 s; every browser wait 30 s; leftover
  check 15 s.

## Steps

- **OS-00 fixture**: prepare the home; start the daemon; send
  `onboarding.firstRun.skip {expectedRevision: 0, operationKey}` over the
  daemon socket and require `firstRun.state == "recorded"`; run
  `hyprial org create e2e032-a-<nonce> --json` and
  `hyprial org create e2e032-b-<nonce> --json` and extract their two distinct
  non-empty `org` ids; read `org.list` over the socket and require its `org`
  id set to match the two created ids exactly.
- Start `hyprial gui`. The Node script reads the private `launch.url`, verifies
  it parses as a loopback HTTP URL matching the startup record's origin, opens
  it at 1440 x 900, waits for the connected shell (`main#main-content`) and
  requires the handoff file to be gone and the page URL to remain on the
  expected origin and carry no fragment.
- Every request the page sends to `/daemon/request/<method>` on the host
  origin is recorded with `page.on('request')` / `page.on('response')`, with a
  phase label: `open` (handoff and app start; the org provider reads the org
  list and the first org's detail here), `org-open` (click 组织), `switch`,
  `members`, `refresh`. Recorded per request: phase, method, the `org`
  parameter, HTTP status, the envelope's `ok`, `dispatch` and error code, and
  for `org.network` a shape summary; boolean `boundary` flags (`crossOrigin`,
  `hasAuthorization`, `hasGrantInUrl`) are recorded across HTTP requests.
  Nothing is answered for the page; no route handler, no `fulfill`, no product
  test hook.
- Open 组织. Read the viewed org id from `data-org-viewed` on the org section;
  the other listed id is the target. Wait until `org.network` for the viewed
  org has been answered.
- **Switch**: open 查看组织 and choose the one option that is not the current
  title. Barrier: `data-org-viewed` becomes the target, an `org.network` with
  the target id is answered in the `switch` phase, and the title shows the
  target's name.
- **Members**: open the 成员 tab and its 设备 segment, find the 刷新 button on
  the 成员 section header, count top-level 机器 tabs.
- **Refresh**: click that 刷新. Barrier: an `org.list` and an `org.network`
  with the target id are answered in the `refresh` phase. Take a full-page
  screenshot.
- Teardown, always: browser script, browser, `hyprial gui` (one `shutdown`
  line on its stdin), daemon (`hyprial daemon stop`, then the daemon lock);
  each started process group (`daemon`, `gui`, `browserScript`, and the
  detached Chromium browser group whose PID is written atomically to
  `browser.pid` before `chromium.connect` and observed live via a watcher)
  and child-reported PID (`host`, `browser`) records `process_birth_identity`
  at launch or observation time. Any `SIGTERM`/`SIGKILL` is strictly fail-closed:
  signalling a group requires both live members and a live leader matching its
  recorded `process_birth_identity` (`observed == birth`). Reused PIDs/PGIDs
  (`observed != birth`) are marked dead without signalling, and any group or
  child-reported PID whose birth identity is missing (`observed is None`,
  including when the leader exited while descendants remain), whose PGID/PPID
  provenance fails (even if already dead), or whose PID is unparseable or out
  of range (`<= 0` or `> 2147483647`) is recorded as unverified, never
  signalled, and fails OS-07. Read the daemon's own logs (`<state>/logs/*.jsonl`
  and the child's stdout) for counts, then remove the root.

## Assertions

- **OS-00**: the prepared home commits the scenario owner, the first-run skip
  is recorded, and `org.list` lists exactly the two created orgs.
- **OS-01**: the handoff is consumed, the org page opens, 查看组织 offers both
  orgs, and the viewed org is one of the two listed ids.
- **OS-02**: after the switch the page sends at least one `org.network` whose
  `org` is the target id, every `org.network` of the `switch` phase carries
  the target id, at least one of them is answered `200` with `ok: true`, and
  no `org.network` in the whole run carries an id that `org.list` did not
  list.
- **OS-03**: no `org.list` is sent in the `switch` or `members` phases: the
  switch loads the detail directly and does not go through the overview
  refresh.
- **OS-04**: the title shows the target's name, and `data-org-viewed` stays
  the target id in 成员 and after the refresh.
- **OS-05**: the 成员 section header has a visible, enabled 刷新 button, there
  is no top-level 机器 tab, and clicking 刷新 sends `org.list` and an
  `org.network` with the target id.
- **OS-06**: no `org.fetch` is sent in the whole run, and the browser request
  boundary flags (`crossOrigin`, `hasAuthorization`, `hasGrantInUrl`) are all
  `false`.
- **OS-07**: after teardown none of the process groups the runner started and
  none of the pids its children reported (the GUI host, the browser) is
  alive (any survivors are force-killed before removing the root, and any
  survivors after `stop()` or force-kill still fail OS-07), the throwaway root
  is gone, and on macOS `launchctl list` succeeds and the number of launchd
  jobs labelled `com.hyprial` is the same integer before and after.

Deliberately not asserted:

- No machine name and no device row. On the isolated daemon the directory of
  each new org is empty (`org.network` answers `directory: {<org>: []}`), so
  the 设备 segment shows the empty state; the text is recorded as evidence
  only.
- The daemon's `orgfs.update.rejected` warnings with reason
  `supplier-not-member`. On the prepared home, which never went through a
  real login, they start after the first `org create` and every one names
  the node itself as the supplier (measured 2026-11-21: 48 of 166 daemon log
  lines per run). Every page read still answers `200` with `ok: true`. The
  counts are recorded in `result.json`, not asserted.
- Which org is viewed first. Both orgs are `current: false` on a fresh home
  and the page picks one; the scenario reads it from the page.
- The other requests of a detail read (`hosts`, `orgfs.*`) and their order.
  They are recorded in the evidence.
- Ordering or late answers (acceptance row 5, F3.1) and failed reads (row 3,
  F2.1): the isolated daemon cannot produce them on demand.

## Evidence

The runner prints `RESULT <status> E2E-032 <step>` per assertion, one
`EVIDENCE OS-REQUESTS` line with the recorded requests (phase, method, `org`,
status, error code), and a final `RESULT <verdict> E2E-032 org-switch-browser`
line, and writes `result.json` plus the screenshot to
`<artifacts>/E2E-032/` (`HYPRIAL_E2E_ARTIFACTS`, else `$HYPRIAL_E2E_ROOT/artifacts`,
else `./e2e-artifacts`). `result.json` also holds the fixture's org rows, the
page facts, the daemon log counts (`supplier-not-member`,
`orgfs.update.rejected`, error lines, with the line total as positive
control), child exit codes, the started process groups and reported pids,
and the launchd counts. No token, cookie, session id or handoff URL is
recorded; quoted child output passes through a scrubber that removes grant
fragments and long opaque tokens.

## CI

`E2E-032` runs in `.forgejo/workflows/gate-gui.yml` (the `gui-acceptance` job,
with `HYPRIAL_ACTOR_BACKEND=native` on schedule and `reference-abi` on pull
requests after the GUI build and Chromium provisioning steps).
`tests/tooling/ci/runner/test_ci_e2e_baseline.py` enforces the invariant that
`EXPECTED_IDS` is completely partitioned into disjoint `ISOLATED_IDS`,
`CONTROLLED_IDS`, and `GUI_ACCEPTANCE_IDS` sets.

Run it standalone:

```sh
npm --prefix gui ci --ignore-scripts --no-audit --no-fund
uv run python contract/e2e/run-org-switch-browser.py
```
