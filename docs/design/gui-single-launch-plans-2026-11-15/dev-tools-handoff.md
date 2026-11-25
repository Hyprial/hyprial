# Hand-over: the developer tools line of the GUI single-launch project (PR 4b-H, 4b-G, 4b-C, 4b-S2, PR 5)

Date: 2026-11-16. Issued by: the GUI single-launch line. Status: open for an external developer.
Base read for every statement about code below: `dev` @ `f20e37ca8`. Line numbers are from that commit and drift;
re-check them before you start. Anything not read or not run is marked UNKNOWN.

This document replaces nothing. The plan of record for this line is still
[`dev-4b-5.md`](dev-4b-5.md); it was written on 2026-11-15 against `dev` @ `9932b2b63`, before the host
side landed. Section 4 below goes through it step by step and says what still holds.

## 1. Goal, non-goals, binding owner goals

**Goal.** `hyprial gui` gets two orthogonal switches, and nothing else starts a GUI:

```
hyprial gui [dev] [--with-dev-tools] [--live-source] [--gui-home DIR] [--ip A] [--port N] [--no-open] [--stdin-stop] [--json]
hyprial gui dev  ≡  hyprial gui --with-dev-tools --live-source
```

- `--with-dev-tools`: the session host serves the developer tool layer (a floating panel outside `#root`) and
  an API docs page (Swagger UI over the daemon request contract), and tells the page so in the session response.
- `--live-source`: page bytes come from a Vite dev server behind the same host (this half already exists as
  `hyprial gui dev`, see 3.1).

**Binding owner goals.** These are not open for trade-offs:

1. ONE launch path. `hyprial gui dev` ≡ `hyprial gui --with-dev-tools --live-source`.
2. Developer tools exist only behind `--with-dev-tools`. A default `hyprial gui` (wheel, source checkout or the
   native shell) serves none of the developer tool layer and no API docs.
3. Delete first. An old path is removed, not kept behind a flag or an environment variable.
4. No second page set. Preview and live share the same UI; only the data source, the capability adapter, the
   controller and the developer toolbar differ.

**Non-goals.**

- Replacing the in-process fixtures with Contract Mock presets (Stage F), and deleting the fixture preview
  (`HYPRIAL_GUI_DEV_PREVIEW`, `parity-composition.tsx`, `fixture-tools/**`, `dist-parity`).
- Folding the session host into `request_http` (design §7a).
- The native shell (`clients/native/**`), packaging, the Docker entry (PR 7), Windows.
- Any new daemon request method, and any change to the daemon.

## 2. Read first (in this order; on conflict the earlier wins)

1. [`../gui-single-launch-2026-11-14.md`](../gui-single-launch-2026-11-14.md): §3 (the two switches), §4.2 (GUI rules),
   §5 (PR sequence, rows 4b and 5), §7 item 1 (topology, decided), §7a.
2. [`dev-4b-5.md`](dev-4b-5.md): the plan for this line. [`host-2-3.md`](host-2-3.md): the Python host (PR 2/3, merged).
3. `AGENTS.md` at the repository root.
4. `contract/gui-session/README.md` (§4 session model, §5 routes, §12 static resources, §13 versioning) and
   `contract/cli/README.md` with `contract/cli/gui-startup-record.v1.json`.
5. The file-header decision blocks of `src/hyprial/shell/impl/gui/host/{__main__,app}.py`,
   `host/web/{assets,live}.py`, `gui/backend/{config,source}.py`, `gui/install/launch.py`.

## 3. Current state on `dev` (verified)

### 3.1 The launch path today

| Topic | Fact | Evidence |
|---|---|---|
| Grammar | positional `[dev]`, `--ip`, `--port` (0 allowed), `--no-open`, `--gui-home` (dev only), `--stdin-stop`, `--json`. Neither `--with-dev-tools` nor `--live-source` exists | `src/hyprial/shell/impl/cli/commands/gui/commands.py:65-110` |
| Modes | `parse_action`: `""` → `normal`, `dev` → `dev`, anything else `INVALID_ARGUMENT` | `src/hyprial/shell/impl/gui/backend/config.py:131-142` |
| One host | every launch (wheel, source, dev) runs `python -P -m hyprial.shell.impl.gui.host serve`. The Node host is deleted | `config.py:148-162`; `gui/server/` is absent; `gui/vite.config.ts` has no daemon-request plugin |
| Normal, wheel | host `--root <site-packages>/hyprial/_gui` (manifest-verified) | `config.py:272-293` |
| Normal, source | `npm run build`, then host `--root <gui>/dist` (unverified tree) | `config.py:309-312` |
| `dev` | host without `--root`; one more owned child `npm run dev -- --host 127.0.0.1 --port 0 --strict-port`; the session appends `--live-upstream <origin>` after Vite's ready line | `config.py:313-326`; `backend/lifecycle/session.py:449-452,518-528` |
| Host argv | `serve --host --port`, and exactly one of `--root DIR` / `--live-upstream URL`. No developer flag | `host/__main__.py:93-102` |
| Live relay | GET/HEAD and the HMR WebSocket are relayed to a loopback-literal upstream; `/daemon/*` and `/__gui/healthz` never are; an upgrade needs Host, a same-origin Origin and same-origin Fetch Metadata | `host/web/live.py:1-34,78-99`; `host/app.py:231-242,255-275` |
| WebSocket stack | `websockets-sansio` only with `--live-upstream`, `none` otherwise | `host/__main__.py:302,367` |
| Static tree | `AssetTree` serves **every** file the manifest names (wheel) or every file under the root (source `dist`); a missing path without an extension falls back to `index.html`; names starting with a dot are never served | `host/web/assets.py:50-67,76-91,110-119` |
| Session body | `driver, phase, view, generation, csrf, expiresAt, sessionContract, principal, catalog`. No `devTools` | `host/app.py:405-436` |
| Health | `ok, service, mode, dataMode, driver, backendConfigured, session` (+ `assets`, `assetsVerified`) | `host/app.py:306-318` |
| Session routes | `session`, `session/status`, `session/logout`, `readyz`, `request`, `request/{method}`; anything else under `/daemon/` is 404 `METHOD_NOT_FOUND` | `host/app.py:126-146`; corpus `routes` in `contract/gui-session/gui-session.v1.json` |
| Backend child | the launcher still starts one `request_http` child per GUI session (or the Node Contract Mock); the daemon itself is ensured, not owned (DL-1). Dev tools must add no further backend | `backend/source.py:301-325`; `commands.py:49-62` |
| Startup record | `contract/cli/gui-startup-record.v1.json` (v1): required keys `ok, record_version, mode, url, host, port, handoff_path, backend_driver, frontend_pid, backend_pid, assets_pid`; `mode` is `normal` or `dev`; `additionalProperties: true` | `gui/install/report.py:19-91`; `tests/tooling/contracts/cli_startup/test_gui_startup_record.py` |
| Release wheel | a wheel whose manifest remote is `PUBLIC_SOURCE_REMOTE` refuses `gui dev` with `INVALID_ARGUMENT` unless `--gui-home` / `HYPRIAL_DEV_GUI_HOME` names a source; any other wheel keeps the managed exact-SHA worktree | `gui/install/launch.py:13-17,137-148`; `tests/shell/gui/installed/test_release_dev_refusal.py` |
| Native shell | spawns exactly `gui --no-open --json --port 0 --stdin-stop` | `clients/native/launcher/src/child.rs:64` |
| Daemon request | `contract/daemon-request/openapi.json` has 128 paths (HTTP-exposed methods only), including the five `agent.secret.*` paths | counted from the file |

### 3.2 Where the developer tool layer and the API docs live, and who can reach them today

| Thing | Where | Reachable in a default launch today? |
|---|---|---|
| Floating tool layer (ball, panel, module index) | `gui/src/dev/tools/**` (`index.ts`, `impl/{mount.tsx,DevToolsLayer.tsx,context.ts}`), split from the fixture composition by #2758 | **No.** On `dev`, `gui/src/main.tsx` imports only `./dev/parity-composition`, under the build tokens `__HYPRIAL_DEV_PREVIEW__` / `__HYPRIAL_PARITY__` (lines 92-98), both false in `dist`. The layer is mounted only by the fixture entry, `gui/src/dev/parity-composition.tsx:599-600` |
| Fixture preview | `HYPRIAL_GUI_DEV_PREVIEW=1 npm --prefix gui run dev` | Not through the CLI: `HYPRIAL_GUI_DEV_PREVIEW` in the environment is refused with `GUI_CONFIG_CONFLICT` (`config.py:351-369`) |
| `hyprial gui dev` | Vite source behind the host | The tool layer is **not mounted** (same `main.tsx`). The source files under `gui/src/dev/**` are fetchable through the relay like any other source file |
| Swagger page (old) | `src/hyprial/shell/impl/http_contract/swagger.py` (239 lines) and `request_http/app.py:48-52,79,81,371-375,421-423,650-669,775-802,857-869` | Not on the GUI origin. It exists only on the `request_http` child's own loopback port, only when the caller's environment has `HYPRIAL_REQUEST_HTTP_DEV_SWAGGER=1` plus `HYPRIAL_REQUEST_HTTP_SWAGGER_ASSETS=<dir>`, and the page asks the operator to type the bearer. The launcher does **not** strip those two variables today: `request_http_environment` removes only `HARNESS_SOCKET_PATH` and `HYPRIAL_GUI_*` (`backend/source.py:313-325,867-870`) |
| Swagger UI assets | not vendored anywhere on `dev` | — |
| Session contract | `contract/gui-session/README.md` §12: nothing outside `/daemon/*` is served except `/`, `assets/*`, the SPA fallback and `/__gui/healthz`; flag-gated developer routes are named as a later additive change | — |

Consequence for ordering: the host has no notion of a gated prefix. If a `dist` that contains `dev-tools/**`
reaches `dev` before the host gates that prefix, every default launch serves those files to anyone who asks for
the URL (they are not mounted, but they are reachable). That is why 4b-H merges before 4b-G (section 5).

### 3.3 Branches

| Branch | State |
|---|---|
| `refactor/gui-dev-tools-decouple` | **Merged** as #2758 (PR 4a). Its head `e21807fe3` is an ancestor of `dev`; nothing is left on it. Do not reuse it |
| `feat/gui-dev-tools-chunk` | PR 4b-G, implemented 2026-11-15 on `9932b2b63`, never merged. Brought up to date for this hand-over by a plain merge of `dev` @ `f20e37ca8` (400 commits). **This document is committed on that branch** |

What the 4b-G branch contains (8 commits, 24 files, +629 / −105 against `dev`; no file under `src/`, `tests/`,
`contract/` or `scripts/` differs from `dev`):

| Step of the plan | Commit | Files |
|---|---|---|
| G-1 session field | `bd9caf42a` | `gui/src/app/backend/browser-session.ts`, `gui/tests/daemon-request-host/browser-session.test.ts` |
| G-2 API docs entry | `d0d7af7be` | `gui/src/dev/tools/impl/{context.ts,DevToolsLayer.tsx}`, `gui/src/dev/dev-module-index.css`, `gui/tests/dev-tools/mount-seam.test.tsx` |
| G-3 mount from the formal entry | `3f4d75f7d` | `gui/src/main.tsx`, `gui/scripts/verify-dev-product-parity.mjs` (one more allowed specifier, `./dev/tools`), `gui/tests/dev-tools/import-graph.test.ts`, `gui/tests/dev-product-parity-static.test.mjs` |
| G-4 chunk placement | `adae91efd` | `gui/vite.config.ts`, `gui/tests/dev-tools/dist-layout.test.mjs` |
| G-5 artifact gate in three scopes | `f5f261ef8` | `gui/scripts/verify-production-copy.mjs`, `gui/tests/production-copy-gate.test.mjs`, `gui/tests/dev-tools/tool-chunk.test.mjs` |
| G-6 Swagger UI files in `dist` | `fa5de4eaf` | `gui/package.json` (`swagger-ui-dist` `5.17.14`, exact), `gui/package-lock.json`, `gui/vite.config.ts` |
| G-7 fixture-only styles out of the tool stylesheet | `e01d30552` | `gui/src/dev/dev-module-index.css`, `gui/src/dev/fixture-tools/impl/fixture-tools.css` (new), `gui/src/dev/fixture-tools/index.ts`, three tests |
| G-8 docs | `6c134f9e3` | `gui/docs/{dev-mode-user-guide.zh-CN.md,shared-ui-entry.zh-CN.md,module-boundaries.md}`, `docs/design/gui-dev-product-parity-2026-11-09.md` |

Merge result. One content conflict, `gui/vite.config.ts`: `dev` removed the Node daemon-request plugin and
narrowed `guiHealthPlugin` to `(mode, dataMode)`; the branch added the chunk placement and the build-only
Swagger asset plugin. Resolved mechanically (both kept, `dev`'s plugin list and signature). Four other files
auto-merged.

Results on the merged branch (Linux, Node 24.22, 4 shared cores, `--maxWorkers=2`):

| Command | Result |
|---|---|
| `npm --prefix gui run typecheck` | PASS |
| `npm --prefix gui run check:module-boundaries` | PASS |
| `npm --prefix gui run build` | PASS; gate line `0 forbidden markers (scopes: product closure, dev-tools, dev-tools/docs)` |
| `npm --prefix gui run verify:production-copy` | PASS (same line) |
| `npm --prefix gui run verify:dev-product-parity` | PASS (262 product sources) |
| `dist` layout | `dev-tools/tools-*.js` 4,082 bytes, `dev-tools/tools-*.css` 4,580 bytes, `dev-tools/docs/{swagger-ui-bundle.js 1,452,753, swagger-ui.css 152,071, LICENSE 11,358, NOTICE 55}`; `index.html` names no `dev-tools/` path; `dist` total 7,740,651 bytes |
| vitest: the 8 test files the branch adds or touches, plus `production-artifact-contract.test.mjs` and `dev-tools-floating.test.tsx` (10 files) | PASS, 200 / 200 |
| `node scripts/ci/check_gui_request_paths.mjs --base-ref <dev>` | PASS (findings 7, allowlist 7, pending 0); self-test 131 / 131 |
| `node scripts/ci/check_gui_request_architecture.mjs` | PASS |
| `node scripts/ci/check_gui_unavailable_copy.mjs --base-ref <dev> --base-branch dev --json` | PASS, no violations |
| `node scripts/ci/check_gui_mock_source.mjs` (with and without `--base-ref`) | **FAIL**, see below |
| Whole GUI vitest suite (`npx vitest run --maxWorkers=2`, 208 files, 2,712 tests, 1 skipped by the suite itself) | first run: 201 files passed, 7 failed. Six failed because the fresh worktree had no synced Python environment (`ModuleNotFoundError` for the session host module in every case that starts it); one (`tests/contract-mock/serve.test.ts`) had its 31 tests pass and its `afterAll` hook time out at 10 s under a machine load of about 13. After `uv sync --extra test --python 3.12` the seven files were re-run: 7 / 7 files, 248 / 248 tests PASS. No failure is attributed to the branch; the hook timeout is recorded, not explained away |
| `verify:dev-browser-parity`, `test:browser`, `test:daemon-request-browser`, wheel build | not run: UNKNOWN |

**Finding F-1 (blocks 4b-G as it stands).** The mock-source gate and its shrink-only fixture ledger
(`scripts/ci/gui-mock-source-ledger.json`) landed after the branch was written. Step G-7 moved fixture-only
rules into a new file on the fixture side, which the ledger forbids:

```
FAIL {"kind":"unlisted-fixture-file","path":"gui/src/dev/fixture-tools/impl/fixture-tools.css","lines":60}
FAIL {"kind":"fixture-file-grew","path":"gui/src/dev/fixture-tools/index.ts","lines":20,"maxLines":16}
```

Reverting `e01d30552` alone is not enough (measured): `gui/src/dev/dev-module-index.css` is then 359 lines against
its ledger ceiling of 340, because G-2 added the 19 lines of `.dev-tools-api-docs` rules to it. `gui/src/dev/tools`
is an exempt root of that gate. The ledger must not be widened; see open question Q-5 for the compliant fix.

**Finding F-2 (for the reviewer).** G-3 adds one entry to an allowlist,
`COMPOSITION_ROOT_EXCEPTIONS['src/main.tsx'].allowDevImportSpecifiers += './dev/tools'`
(`gui/scripts/verify-dev-product-parity.mjs:66`). This is the one widening the plan calls for (G-3) and the
design row 4b implies; it needs the GUI line's explicit acceptance in review. No other gate was loosened: the
artifact gate keeps its full pattern list for the product closure.

### 3.4 What is missing

Everything on the Python side: no host flag, no prefix gating, no `devTools` in the session body, no docs page,
no schema relay, no CLI flags, no startup-record keys, no contract revision, no browser acceptance cases, and the
old bearer Swagger page is still in `request_http`.

## 4. The plan, re-validated step by step

Legend: **valid** = do it as written; **changed** = do it, differently; **done** = another merged PR did it.

### 4.1 PR 4b-G (GUI) — implemented on `feat/gui-dev-tools-chunk`

| Step | Verdict | Note |
|---|---|---|
| G-1 … G-6, G-8 | done on the branch, re-verified after the merge (3.3) | `browser-session.ts` parses an optional `devTools: {apiDocs: boolean}` and never throws on a malformed value |
| G-3 | valid, needs explicit review acceptance | finding F-2 |
| G-6 | valid | lock integrity of `swagger-ui-dist@5.17.14` re-checked against the registry tarball: equal. The build-only plugin defines no server hook, and the request-paths gate passes |
| G-7 | **changed**: as implemented it fails the mock-source gate | finding F-1, question Q-5 |
| "Merge after 4b-H and PR 3" | PR 3 is merged; the 4b-H half still holds | 3.2 |
| Risk "conflict with PR 3 in `vite.config.ts`" | happened, resolved | 3.3 |

### 4.2 PR 4b-H (host) — not started

| Step | Verdict | Where on `dev`, and what changed |
|---|---|---|
| H-1 argv `--dev-tools`, `--docs-assets DIR`, ready line `devTools` | valid | `host/__main__.py:93-102` (parser), `:119-132` (`_ready_payload`), `:325-374` (`serve`). `--root` and `--live-upstream` are now a required mutually exclusive group; `--docs-assets` defaults to `<root>/dev-tools/docs` only with `--root`. The launcher's `parse_host_ready` (`backend/source.py:755-792`) compares a fixed key set and ignores extra keys, so a new ready key is additive |
| H-2 prefix gating in `AssetTree` | valid, **extended** | `host/web/assets.py:50-67,76-91`. Gate both tree kinds (verified and explicit). New: with `--live-upstream` there is no `AssetTree`; the gate for live mode sits in `host/app.py:255-275` before `relay_http` (see Q-2) |
| H-3 session body and health | valid | `host/app.py:405-436` (`_session_reply`), `:306-318` (`_health`) |
| H-4 `POST /daemon/session/attach` | valid, pending Q-1 | `host/session.py:217` (`require_control`), `:240` (`get`); `host/app.py:126-146` (`_route_of`), `:347-374` (`_dispatch`). `tests/shell/gui/host/app/` holds 6 files: one new file fits, not two |
| H-5 `GET /daemon/openapi.json` | valid | `host/upstream.py:141-159` (`get_json`, bearer added by the host). The upstream document is built by `request_http/app.py:599-620,766-773`; whether it is bare or enveloped is still read-not-run: assert it first. UNKNOWN: what `request_http` answers for `/openapi.json` while the home is in the `bootstrap` phase |
| H-6 `web/devtools.py`, `/docs`, `/docs-assets/*` | valid | `host/web/` holds 4 files, `host/` is at 7 / 7 (new module goes in `web/`); `host/app.py:301-304` (`_static_resource`) asks it first. `tests/shell/gui/host/web/` holds 5 files |
| H-7 contract revision | valid | `contract/gui-session/gui-session.v1.json` (`routes` has 7 entries; `static.everythingElseOutsideDaemon: "not served"` must gain the flag-gated paths), README §5, §12, §15, §16. The "capability false for the Node driver" wording is obsolete: the Node host is deleted |
| H-8 conformance case | changed | `gui/tests/daemon-request-host/host.test.ts` now always drives the Python host through `gui/tests/support/python-host.ts`; there is no `HYPRIAL_HOST_IMPL` switch to pass. That file is a sanctioned root of the request-paths gate; do not add another listener anywhere under `gui/tests` |
| "`request_http/**` and `http_contract/**` are not edited" | valid | they are in the coordination zone (section 9) |

### 4.3 PR 4b-C (launcher and CLI) — not started

| Step | Verdict | Where on `dev`, and what changed |
|---|---|---|
| C-1 `--with-dev-tools`, `GuiLaunchPlan.dev_tools`, host argv | valid | `commands.py:65-110`, `config.py:98-128` (plan), `:148-162` (`host_argv`), `:195-348` (`build_plan`), `launcher.py:152`. `tests/shell/gui/lifecycle/` is at 7 / 7 files; extend `test_gui_source_selection.py` (559 lines) or add a subdirectory |
| C-2 strip the two Swagger variables from the `request_http` child | valid | `backend/source.py:313-325`; that file is 973 / 1000 lines, so the change must be a few lines (for example inside `_drop_runtime_controls`, `:867-870`). `tests/shell/gui/backend/test_source_runtime.py` is 837 lines |
| C-3 startup record keys `dev_tools`, `api_docs` | **changed** | the record now has a contract (PR 6a). Add both as optional properties to `contract/cli/gui-startup-record.v1.json` and its examples in the same PR; `record_version` stays 1 (no required key changes meaning). The native launcher embeds that file (`clients/native/launcher/src/record.rs:193`): run its tests or state BLOCKED. `tests/shell/gui/installed/test_installed_gui.py` is 989 / 1000 lines and its directory is at 7 / 7: new cases need a new subdirectory |
| C-4 browser cases N, M, D | valid | `gui/tests/daemon-request-host/browser-acceptance.mjs` (`npm --prefix gui run test:daemon-request-browser`). Case D needs an isolated real daemon with a controlled identity; without one it is BLOCKED, not PASS |
| C-5 inventory and surface | valid | `scripts/verify/check_cli_help_inventory.py`, `docs/reference/cli-command-inventory.json`, `scripts/verify/update_cli_surface_snapshot.py` → `contract/cli/surface.json` |
| C-6 docs and skills | valid | `docs/guides/gui-dev-mode.md:758` (the "do not use the flags yet" sentence), `src/hyprial/skills/hyprial-ops/SKILL.md:211-226`, `gui/docs/dev-mode-user-guide.zh-CN.md`. A change to `AGENTS.md` needs the owner's sign-off |
| new | **added** | release wheels: Q-3. Native shell: a test that pins `CLI_ARGS` without the flag already exists on the Rust side; add a Python-side case that a default plan's host argv has no `--dev-tools` |

### 4.4 PR 4b-S2 (remove the bearer Swagger page) — not started

| Step | Verdict | Note |
|---|---|---|
| Deletions of plan §E | valid; line references re-checked | `http_contract/swagger.py` whole file; `http_contract/__init__.py:46-58` and the names in `__all__`; `request_http/app.py:48-52,79,81,371-375,421-423,650-669,775-802,857-869`; `tests/shell/http_contract/test_swagger.py` (102 lines); `tests/shell/request_http/test_server.py:25-26,333` and the Swagger cases, now at about `:660-760` |
| Timing (OD-7) | **changed** | the CLI → daemon request convergence is merged (#2834), but `request_http/**` and `http_contract/**` are still a coordination zone. This PR touches them by design: it needs the GUI line's sign-off before the first edit (Q-4) |

### 4.5 PR 5 (`--live-source`) — partly done by PR 3

| Step | Verdict | Note |
|---|---|---|
| 5-0 `assets_argv`, `web/live.py`, `ws` switch | **done** (PR 3) | `config.py:120,313-326`; `host/web/live.py`; `host/__main__.py:302` |
| 5-1 Origin required on the upgrade, `/daemon` never relayed, non-loopback upstream exits 2 | **done** (PR 3) | `tests/shell/gui/host/web/test_live.py:175,194,391,418,580,595` |
| 5-2 flag mapping, `live_source`, rename `select_dev_target` | valid | `config.py:126-128` (`hot_reload` keys on `mode`), `:131-142`, `:242-263`; `install/launch.py:96` |
| 5-3 `--gui-home` only with live source | valid | `install/launch.py:107-119,301-316` |
| 5-4 host argv with `--live-upstream`, `--dev-tools`, `--docs-assets`; Vite child holds no backend variable | half done | the environment rule exists (`backend/source.py:369-371`, `assets_environment`); the argv part is new |
| 5-5 failure messages for a wheel without Git / Node / npm | valid, **extended** | keep the release-wheel refusal (`install/launch.py:137-145`) and reword it to name `--live-source`; `test_release_dev_refusal.py` must keep passing with `--live-source` in place of `dev` |
| 5-6 `live_source`, `live_source_port` in the record | changed | sync `contract/cli/gui-startup-record.v1.json`; `assets_pid` already exists; see Q-6 for the port |
| 5-7 browser case L | valid | the existing case "actual CLI dev entry uses the same App and consumes its private handoff" (`browser-acceptance.mjs:2345`) is the starting point |
| 5-8, 5-9 | valid | design §7 item 1 is already marked decided |
| "port 0 refused until PR 6" | obsolete | `--port 0` is allowed (PR 6a) |

## 5. Merge order

The plan said H → G → C → S2 → 5, with G after PR 3. Re-checked:

| Order | Why it still holds |
|---|---|
| 4b-H first | 3.2: without host gating, `dist/dev-tools/**` is served by every launch. H is testable alone with a synthetic tree |
| 4b-G second | PR 3 is merged, so only H is left in front of it. G needs F-1 fixed first |
| 4b-C third | first user-visible change; needs both |
| PR 5 after 4b-C | flag mapping on top of `dev_tools` |
| 4b-S2 after 4b-C, when signed off | until then the launcher strips the two variables (C-2) |

H and the G rework have no file in common (`src/hyprial` + `contract/gui-session` + `tests/shell` versus `gui/`)
and can be written in parallel. S2 can be prepared at any time and merged last.

## 6. Decisions

### 6.1 Decided

| ID | Decision | Source |
|---|---|---|
| OD-1 | One build; the tool layer ships in the wheel as a host-gated chunk; not loaded and not served by default | design §3 and §5 row 4b; implemented on the 4b-G branch |
| OD-2 (first half) | The one host serves the API docs at `/docs`; Swagger UI assets ship in the wheel; the tool layer holds the entry | design §5 row 4b |
| OD-6 | `hyprial gui dev` ≡ both flags | binding goal 1 |
| OD-8 | An installed wheel keeps the managed exact-`sourceCommit` worktree. Later addition: a release wheel refuses live source without an explicit source (#2874) | design §3, §6; `install/launch.py:13-17` |
| OD-9 | Flags only; no environment twin for either switch | binding goals 2 and 3 |
| OD-11 | `mode` stays in the startup record (`normal` / `dev`) | it is a required key of `contract/cli/gui-startup-record.v1.json` |
| OD-12 | 4b is split G / H / C (+ S2) | this hand-over |
| Topology | Python host in front, Vite behind | design §7 item 1 (PR 3) |

### 6.2 Plan defaults in force (no owner statement found in the repository; proceed unless told otherwise)

| ID | Default |
|---|---|
| OD-3 | Swagger UI stays at `5.17.14` (pinned on the 4b-G branch) |
| OD-5 | No API docs over the Contract Mock: `apiDocs` is `false`, the entry is not rendered, no copy explains it |
| OD-10 | `--gui-home` without live source is `INVALID_ARGUMENT` (what normal mode does today) |
| OD-13 | The module index ships as a tool (it is what the layer renders over a backend today) |

### 6.3 Open questions for the owner

Each has a recommendation; the developer follows the recommendation only after the GUI line confirms it in the PR.

- **Q-1 (OD-2 second half, OD-4). How does the docs tab use the session?**
  Options: (a) the plan's design: a new flag-gated route `POST /daemon/session/attach` returns the live
  `{generation, csrf, expiresAt}` to a tab that proves control cookie + session cookie + view + Origin, rotates
  nothing, plus `GET /daemon/openapi.json` as a session-authenticated schema relay; a docs tab opened without
  the opener makes no request; (b) read-only docs: same relay, "Try it out" disabled, no CSRF handed to the
  second tab; (c) the docs tab performs a normal refresh (rejected by the plan: the two tabs revoke each other).
  And for a tab opened without the opener: (i) a one-line instruction page, or (ii) a redirect to `/` with no copy.
  Recommendation: (a) with (ii). (ii) keeps the "no static unavailable copy" rule without a debate about
  whether a host-served page is covered by it.
- **Q-2. `--live-source` without `--with-dev-tools`: what must the host refuse?**
  In live mode every non-`/daemon` path is relayed to Vite, so `gui/src/dev/**` is fetchable as source like any
  other file, and Vite has more than one spelling for a file (`/src/...`, `/@fs/...`).
  Options: (a) the guarantee is "not mounted, session reports no `devTools`, `/docs`, `/docs-assets/*` and the two
  developer `/daemon` routes are 404"; source files stay fetchable; (b) additionally refuse to relay paths under
  `/src/dev/` (best effort, not a boundary); (c) forbid `--live-source` without `--with-dev-tools`.
  Recommendation: (a). Live source is already a developer mode that exposes all source; (b) would look like a
  boundary and not be one; (c) removes the orthogonality the design asks for.
- **Q-3. Does a release wheel accept `--with-dev-tools`?**
  A release wheel refuses `gui dev` because it carries no sources; the built tool chunk and Swagger files would
  be inside it (+1.6 MB uncompressed, about +0.44 MB compressed, plan M7; wheel not built here).
  Options: (a) accepted on every wheel: the tools ship, off by default, and the docs page calls only the user's
  own daemon with the session the GUI already has; (b) refused on a release wheel with `INVALID_ARGUMENT`, files
  still shipped; (c) refused and the release build drops `dev-tools/**` (a second artifact shape).
  Recommendation: (a), which is what design §3 says ("ship with the wheel, not loaded and not served by
  default"); the wheel acceptance then proves the default launch serves none of it. (c) contradicts "one build".
- **Q-4 (OD-7). When may 4b-S2 edit `request_http/**` and `http_contract/**`?**
  Options: (a) right after 4b-C merges; (b) only after the session host is folded into `request_http` (§7a);
  (c) inside 4b-C. Recommendation: (a), as its own small PR, with the GUI line's sign-off recorded in the PR
  before the first edit. Until then C-2 keeps `hyprial gui` from starting that page.
- **Q-5 (finding F-1). How is G-7 reconciled with the shrink-only fixture ledger?**
  Options: (a) drop G-7 (revert `e01d30552`), move the 19 lines of `.dev-tools-api-docs` rules into a stylesheet
  under `gui/src/dev/tools/impl/` (an exempt root) imported by `DevToolsLayer.tsx`, and keep `dev-data-source` and
  `dev-scenario` in `TOOL_LAYER_OWN` until Stage F deletes the fixture tools; (b) the owner approves two ledger
  changes (a new fixture row and a raised ceiling). Recommendation: (a). It needs no ledger change;
  the cost is that two fixture class names stay allowed inside `dev-tools/` until Stage F. Measured: after the
  revert the only remaining gate finding is the 359-versus-340 line count that the move removes; the full result
  of (a) is UNKNOWN until it is built.
- **Q-6. Does the startup record expose the Vite port (`live_source_port`)?**
  Options: (a) add `live_source` (bool) only; (b) add both, as the plan says. Recommendation: (a). PR 3 made the
  host's port the only public one and `assets_pid` already identifies the child; nothing reads the port.
- **Q-7. Does a developer build of the native shell ever pass `--with-dev-tools`?**
  The plan (§H) offered it; the binding goal says the shell never enables the tools.
  Options: (a) never: `CLI_ARGS` stays constant and a developer runs `hyprial gui --with-dev-tools` in a browser;
  (b) a debug-only shell switch. Recommendation: (a). Nothing in this line touches `clients/native/**`.

## 7. Work packages

One PR per package. Branch from fresh `dev`, named `<type>/<slug>`. Every package starts with **step 0**: run
its verification commands on the untouched base and record the failing set.

### WP-H — `feat(gui): host serves developer tools and API docs behind --dev-tools` (4b-H)

- **Scope.** `src/hyprial/shell/impl/gui/host/{__main__,app,session,upstream}.py`, `host/web/assets.py`, new
  `host/web/devtools.py`; `contract/gui-session/{gui-session.v1.json,README.md}`; tests under
  `tests/shell/gui/host/{.,app,web}`; one conformance case in `gui/tests/daemon-request-host/host.test.ts` (and
  `session-corpus.ts` if the corpus shape requires it).
- **Behaviour.** Without `--dev-tools`: `dev-tools/**` (any tree kind), `/docs-assets/*`,
  `POST /daemon/session/attach` and `GET /daemon/openapi.json` are 404; `/docs` is the ordinary SPA fallback; the
  session body has no `devTools` key; health and the ready line say `devTools: false`. With it: the prefix is
  served `no-store`; the session body carries `devTools: {"apiDocs": bool}` (`true` only for the daemon driver
  with docs assets present); `/docs` is a host-owned shell with CSP `script-src 'self'; connect-src 'self'`,
  `X-Frame-Options: DENY`, `nosniff`, `no-store`; the schema relay keeps only `/daemon/request` paths and strips
  `security` and `securitySchemes`; the upstream bearer appears in no response. Details: plan §B.1, §B.2, steps
  H-1 … H-8, subject to Q-1 and Q-2.
- **Tests, red first.** The H-1 … H-8 cases of the plan, each "off" case included: flag off → 404 for every gated
  path and no `devTools` key; attach refusals (GET 405, no Origin 403, non-empty body 400, missing or wrong
  control cookie / view 401, revoked session 401) and success without `Set-Cookie` and without rotation; relay
  off / mock driver / no session / success / upstream 503; corpus equals the host's route table.
- **Verification.** Section 10 commands; full `pytest tests/` is required (product Python changes); expected: no
  test that passes on the base fails; `check_structural_limits.py` passes (`host/` stays at 7 files, `app.py`
  under 1000 lines); `./contract/run-all.sh --isolated-only` passes.
- **Do not touch.** The coordination zone (section 9), `gui/src/**`, `clients/native/**`, the launcher and CLI
  (`gui/backend/**`, `gui/install/**`, `cli/commands/**`): the flag stays unreachable from the CLI in this PR.
- **Declarations.** `Skill-Impact: not-needed: not reachable from the CLI`. Contract: additive revision of
  `contract/gui-session` v1, synced in the same PR (integer version stays 1).

### WP-G — `feat(gui): ship the developer tool layer as a host-gated chunk` (4b-G, rework)

- **Scope.** The existing branch `feat/gui-dev-tools-chunk`; only `gui/**` and the four doc files it already
  touches.
- **Remaining work.** Fix F-1 per Q-5; re-run every GUI gate; run the browser gates that were not run here.
- **Tests.** Already red-then-green on the branch for G-1 … G-6. For the F-1 fix: the mock-source gate fails
  before and passes after, with `scripts/ci/gui-mock-source-ledger.json` and
  `scripts/ci/gui-mock-source-scanner.json` byte-identical to `dev`.
- **Verification.** The GUI commands of section 10. Expected: all PASS, the artifact gate line reports three
  scopes, `git diff <dev> -- scripts/ci` is empty. No product Python changes, so the full pytest run is not
  required; say so in the body.
- **Do not touch.** Any ledger, scanner configuration or gate script under `scripts/ci/`; `gui/contract-mock/**`;
  `src/**`.
- **Merge gate.** Not before WP-H is on `dev`.
- **Declarations.** `Skill-Impact: not-needed: no operator-visible change until the CLI flag exists`. No
  contract change (the client reads an optional field it previously ignored).

### WP-C — `feat(cli): hyprial gui --with-dev-tools` (4b-C)

- **Scope.** `cli/commands/gui/commands.py`, `gui/launcher.py`, `gui/backend/config.py`, `gui/backend/source.py`
  (C-2 only), `gui/install/report.py`; `contract/cli/{surface.json,gui-startup-record.v1.json,README.md}`,
  `docs/reference/cli-command-inventory.json`; tests under `tests/shell/gui/**` (mind the 7-file and 1000-line
  limits listed in 4.3) and `tests/tooling/contracts/cli_startup/`; browser cases in
  `gui/tests/daemon-request-host/browser-acceptance.mjs`; docs and the `hyprial-ops` skill.
- **Behaviour.** `--with-dev-tools` is valid with and without `dev`; `dev` implies it. The plan carries
  `dev_tools`; the host argv gains `--dev-tools` only then. The `request_http` child never receives the two
  Swagger variables. The record gains `dev_tools` and `api_docs`; the human rendering gains one `dev tools` line.
  Release wheels: per Q-3.
- **Tests, red first.** C-1 … C-5 of the plan; a default plan (wheel and source) has no `--dev-tools` in the host
  argv; the record validates against the contract with and without the new keys; no record key holds the grant
  or the bearer; browser cases N (no flag: no `[data-dev-tools]`, no request under `/dev-tools/`,
  `GET /dev-tools/<a real file name>` is 404, `GET /docs` is the app shell), M (flag + Contract Mock: layer
  present, module index visible, no API docs entry) and D (flag + isolated real daemon: docs open, one read
  method returns 200 through "Try it out", no browser request carries `Authorization`, the bearer occurs in no
  response body, the main tab still works).
- **Verification.** Section 10, all of it, including full `pytest tests/` and the wheel acceptance of section 11.
- **Do not touch.** The coordination zone, in particular `shell/impl/cli/commands/common/support.py`;
  `clients/native/**`; the host beyond what WP-H delivered.
- **Declarations.** `Skill-Impact: updated`. Contract: `contract/cli/surface.json` regenerated and the startup
  record extended additively in the same PR.

### WP-L — `feat(cli): hyprial gui --live-source; dev is an alias` (PR 5)

- **Scope.** `commands.py`, `gui/backend/config.py`, `gui/backend/lifecycle/session.py`,
  `gui/install/{launch,dev_source,report}.py`, `gui/project.py` (messages); the same contract and inventory files
  as WP-C; tests under `tests/shell/gui/**`; browser case L; docs and skills.
- **Behaviour.** `--live-source` alone = live source without tools; `dev` = both; `dev --live-source` and
  `dev --with-dev-tools` are accepted and idempotent. `--gui-home` and `HYPRIAL_DEV_GUI_HOME` are read only with
  live source. With both flags the host argv has `--live-upstream`, `--dev-tools` and
  `--docs-assets <gui>/node_modules/swagger-ui-dist`. The release-wheel refusal stays and names `--live-source`.
  `hot_reload` equals `live_source`; `mode` is `dev` exactly when both are set.
- **Tests, red first.** Steps 5-2 … 5-8 of the plan (5-0 and 5-1 are done); the table over
  (`dev`, `--with-dev-tools`, `--live-source`); `test_release_dev_refusal.py` cases repeated with `--live-source`.
- **Verification.** As WP-C. Browser case L: an edit under `<tmp>/gui/src` shows in the open page within 10 s
  without a new launch grant, and every request and the WebSocket are on the public port.
- **Do not touch.** `host/web/live.py` semantics (the relay rules are pinned by PR 3), the coordination zone,
  `clients/native/**`.
- **Declarations.** `Skill-Impact: updated`. Contract: surface regenerated; record extended additively;
  `--gui-home` now requires live source (state it under `Contract-Impact:` if `contract/` has no entry for it).

### WP-S2 — `refactor(request-http): remove the bearer Swagger page` (4b-S2)

- **Scope.** The deletions of 4.4, nothing else. **This package edits the coordination zone by design**
  (`shell/impl/request_http/app.py`, `shell/impl/http_contract/{swagger.py,__init__.py}`) and must not start
  before the GUI line signs off in the PR (Q-4).
- **Behaviour.** `request_http` no longer has `/docs`, `/docs-assets/*` or the two environment switches;
  `/openapi.json` stays, behind the bearer.
- **Tests.** Delete `tests/shell/http_contract/test_swagger.py` and the Swagger cases of
  `tests/shell/request_http/test_server.py`; add one case that `/docs` is 404 with the old variables set. The host
  tests of WP-H are the replacement coverage: list them in the body.
- **Verification.** Section 10 Python commands and full `pytest tests/`; `git grep -n SWAGGER -- src tests contract`
  returns nothing outside the session host.
- **Do not touch.** Anything else in `request_http/**` or `http_contract/**`; `daemon/impl/request_catalog/**`;
  `contract/daemon-request/openapi.json`.
- **Declarations.** `Contract-Impact: development-only routes and switches of request_http are removed; the session
  host serves the API docs` (if `git grep` finds no `contract/` entry for them at PR time).

## 8. Security requirements

1. **Prefix gating in the host, not hidden links.** Without the flag the host answers 404 for `dev-tools/**`,
   `/docs-assets/*` and the developer `/daemon` routes, whatever the tree contains, and `/docs` is only the
   application shell. A test requests a real file name from the built tree and expects 404.
2. **The page cannot turn the tools on.** `devTools` is read only from the host's session response; no URL
   parameter, storage key, cookie or header enables it (already so on the 4b-G branch, `main.tsx:131-135`).
3. **No credential in a URL** beyond the existing one-use launch grant in the fragment. The docs entry is a
   plain link to `docs`; no generation, CSRF value, grant or bearer is put in a URL, a query or `sessionStorage`
   by this line (the existing key `hyprial.gui.session-view` is reused as is).
4. **The upstream bearer never leaves the host.** No token form, no bearer in any page, script, response body or
   browser-visible header. Tests compare every response against the token of the run.
5. **The Host / Origin exact-origin rule is unchanged** and applies to every new path: `require_same_origin` on
   static paths, Origin required on mutations and on the attach route.
6. **`attach` grants no more than refresh already does** to the same proof (control cookie + session cookie +
   view + Origin), rotates nothing, sets no cookie, and exists only with the flag.
7. **Nothing enables the tools implicitly.** No environment twin, no configuration file key, no default in the
   shell (`CLI_ARGS` unchanged), no default in a release wheel. Release wheels and the explicit flag: Q-3.
8. **Live source.** Both servers bind loopback; the Vite child holds no backend variable, grant or bearer
   (`assets_environment`); the browser never learns the Vite port.
9. **Third-party script.** Swagger UI runs only in the docs document, under the CSP above, never in the product
   document; its files are pinned by exact version, lock integrity and the artifact gate's hash check.

## 9. Coordination zone and contracts

Do not edit any of these without the GUI line's sign-off, given in the PR before the edit:

- `src/hyprial/kernel/impl/contracts/daemon/ipc_errors.py` (the kernel `ipc_errors`)
- `src/hyprial/shell/impl/request_http/**`
- `src/hyprial/shell/impl/http_contract/**`
- `src/hyprial/daemon/impl/request_catalog/**`
- `src/hyprial/shell/impl/cli/commands/common/support.py`
- `contract/daemon-request/openapi.json`

WP-S2 touches `request_http/**` and `http_contract/**` by design and is the only package that may, after sign-off.
If another package seems to need one of these files (for example a new error code), stop and ask in the PR.
New host error codes are plain strings in the host today (`GUI_ASSETS_UNAVAILABLE`, `NOT_FOUND`); follow that.

Contract changes must be additive and land in `contract/` in the same PR, or be declared with
`Contract-Impact: <reason>`: `contract/gui-session` (routes, optional response field, static paths; integer
version stays 1), `contract/cli/surface.json` and `docs/reference/cli-command-inventory.json` (new options),
`contract/cli/gui-startup-record.v1.json` (optional keys only; no change to `required`).

## 10. Rules of engagement

- Branch `<type>/<slug>` from fresh `dev`. Sync with plain merges, never rebase, never force-push. Conventional
  commits, each readable and testable.
- Open the PR with a title starting `WIP:`. Body: the `Submitted-by:` first line the template requires, then
  **Summary / Evidence / UNKNOWN-BLOCKED / Checklist**, plus the `Skill-Impact:` line (`updated` or
  `not-needed: <reason>`, exactly) and a `Contract-Impact:` line where section 9 asks for one.
- CI skips its heavy jobs while the title starts with `WIP:`, so evidence is local until the title changes: paste
  the commands and their real results.
- When a package is done: remove `WIP:` from the title and comment "已完成" on the PR with the changed files, each
  command's result, and every UNKNOWN / BLOCKED item. The GUI line reviews and merges; the developer does not merge.
- Questions for the owner or the GUI line go in a PR comment. Do not widen the scope on your own.
- No skip or xfail, no automatic retry, no deleted test, no loosened assertion, no widened allowlist or ledger,
  no timeout stretched to go green. BLOCKED is not PASS; skip is not a conclusion.
- Never read, change, start or stop a production daemon, the real `~/.hyprial`, credentials or user-level
  configuration. Homes, sockets, ports and caches are isolated per run.
- Never run `hyprial login` or `hyprial init` on an ownerless home without a controlled identity: they contact
  the public identity issuer even under network isolation. Real login is BLOCKED, not PASS.
- Python only through `uv`, Python 3.12 (`uv sync --extra test --python 3.12`, `uv run --python 3.12 …`); Node 24.
  Tests with `-n 2` / `--maxWorkers=2`.
- No new inline comments (decisions go in the file-header block; docstrings stay). At most 7 direct files per
  code directory and 1000 lines per file under `src/hyprial` and `tests/`. Documents go under `docs/`;
  `docs/notes/**` is append-only. No machine names, user names, e-mail addresses or intranet host names in any
  repository file. CLI output only through `shell/impl/cli/output`.
- Windows: do not delete existing Windows checks; do not add Windows code or tests.

Verification commands:

```bash
# GUI (every package that touches gui/**)
npm --prefix gui ci --ignore-scripts --no-audit --no-fund
npm --prefix gui run typecheck
npm --prefix gui run check:module-boundaries
npm --prefix gui run build                      # ends with the three-scope gate line
npm --prefix gui run verify:production-copy
npm --prefix gui run verify:dev-product-parity
(cd gui && npx vitest run --maxWorkers=2 <the files you add or touch>)
node scripts/ci/check_gui_request_paths.mjs --base-ref <dev> --base-branch dev --json
node scripts/ci/check_gui_mock_source.mjs --base-ref <dev> --base-branch dev --json
node scripts/ci/check_gui_request_architecture.mjs --base-ref <dev> --base-branch dev --json
node scripts/ci/check_gui_unavailable_copy.mjs --base-ref <dev> --base-branch dev --json
npm --prefix gui run build:dist-parity && npm --prefix gui run verify:dev-browser-parity
npm --prefix gui run test:daemon-request-browser    # WP-C, WP-L

# Python (every package that touches src/hyprial or tests/)
uv sync --extra test --python 3.12
uv run --python 3.12 --with libcst==1.9.0 pytest -q -n 2 tests/shell/gui tests/shell/request_http tests/shell/http_contract \
  tests/integration/request_http tests/shell/compatibility tests/identity/provider_keys/structure \
  tests/daemon/ipc/error tests/tooling/architecture tests/tooling/ci tests/integration/native_harnesses
uv run --python 3.12 ruff check .
uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py
uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py
uv run --python 3.12 python scripts/verify/check_cli_help_inventory.py      # WP-C, WP-L
./contract/run-all.sh --isolated-only                                        # when contract/ changed
uv run --python 3.12 --with libcst==1.9.0 pytest -n 2 tests/                 # WP-H, WP-C, WP-L, WP-S2
```

Expected: every command passes, or fails only with tests that also fail on the base. Attribute the full-suite
failures against `docs/reference/dev-red-baseline-2026-11-15.md` and list them in the body; a failure inside the
baseline is not a new defect but must be listed.

## 11. Acceptance for the whole line

The line is done when all five packages are merged and each item below is reported PASS / FAIL / BLOCKED with the
command and an output excerpt.

Browser acceptance (Chromium, isolated home, real `hyprial gui` child):

| # | Launch | Expectation |
|---|---|---|
| B-1 | `hyprial gui --no-open --json`, Contract Mock (source) | no `[data-dev-tools]`; no request under `/dev-tools/`; `GET /dev-tools/<real file>` 404; `GET /docs-assets/swagger-ui.css` 404; `GET /docs` is the app shell; session body has no `devTools` |
| B-2 | same, real isolated daemon | as B-1 |
| B-3 | `hyprial gui --with-dev-tools`, Contract Mock | layer present with `data-dev-tools-source="backend"`; module index; no API docs entry; no explanatory copy |
| B-4 | `hyprial gui --with-dev-tools`, real isolated daemon | API docs entry opens a tab; Swagger renders the HTTP-exposed methods; one read method returns 200 through "Try it out"; no browser request carries `Authorization`; the bearer is in no response body; the main tab's next call succeeds |
| B-5 | `hyprial gui dev --gui-home <tmp>/gui` | tool layer present; an edit under `src/` appears within 10 s without a new grant; all HTTP and the WebSocket on the public port |
| B-6 | `hyprial gui --live-source` | live source, no tool layer, `/docs` not served (per Q-2) |
| B-7 | the page is the same UI in B-1 … B-6 | `verify:dev-product-parity` and `verify:dev-browser-parity` pass; no second page set was added |
| B-8 | native shell arguments | `CLI_ARGS` unchanged; a launch with exactly those arguments behaves as B-1 |
| B-9 | Firefox / Safari / WKWebView opener behaviour for the docs tab | Chromium only was measured by the plan (M8); otherwise UNKNOWN |

Wheel acceptance (throwaway venv, isolated `HYPRIAL_HOME`, wheel built from the merged `dev`):

| # | Check | Expectation |
|---|---|---|
| W-1 | `unzip -l` and `release.json` | `hyprial/_gui/dev-tools/…` present and listed with hashes |
| W-2 | `hyprial gui --no-open --json` | B-1 over the verified tree |
| W-3 | `hyprial gui --with-dev-tools --no-open --json` | B-4 (or the refusal Q-3 decides) |
| W-4 | release wheel (`sourceRemote` = the public repository): `hyprial gui dev` | `INVALID_ARGUMENT` naming `--live-source` / `--gui-home`; plain `hyprial gui` works |
| W-5 | wheel size | compressed and uncompressed delta against the base wheel, reported as numbers |

Also required: the old bearer Swagger page is gone (`git grep SWAGGER -- src` is empty outside the session host),
the docs (`docs/guides/gui-dev-mode.md`, the `hyprial-ops` skill, `gui/docs/dev-mode-user-guide.zh-CN.md`) describe
the two flags, and `contract/` matches the code.

## 12. Size and parallelism

| Package | Size | Can run in parallel with | Must merge after |
|---|---|---|---|
| WP-H (4b-H) | L (≈ 900 added lines, 14 files, full pytest) | WP-G, WP-S2 preparation | — |
| WP-G (4b-G rework) | S (one fix, re-verification) | WP-H | WP-H |
| WP-C (4b-C) | M (≈ 600 lines, 20 files, browser + wheel acceptance) | WP-S2 preparation | WP-H, WP-G |
| WP-L (PR 5) | M (≈ 450 lines; the relay half is already merged) | — | WP-C |
| WP-S2 (4b-S2) | S (≈ 500 deleted lines) | anything, as a draft | WP-C and the sign-off of Q-4 |

Line counts are the plan's estimates, not measurements.

## 13. UNKNOWN / not verified in this hand-over

- No Python test, no `ruff`, no structural gate and no contract runner was run for this hand-over: the merged
  branch has no Python difference from `dev`. Each Python package measures its base in step 0.
- Wheel build, wheel size, and every item of the wheel acceptance.
- `verify:dev-browser-parity`, `test:browser`, `test:daemon-request-browser` on the merged 4b-G branch.
- Whether the `afterAll` timeout of `tests/contract-mock/serve.test.ts` seen once in the whole-suite run (3.3) also
  occurs on an idle machine or on `dev`: not measured.
- The shape of `request_http`'s `/openapi.json` reply (bare or enveloped), and its behaviour in the `bootstrap`
  phase.
- Whether Swagger UI `5.17.14` works with only `spec`, the header interceptor and the CSP above (browser case B-4).
- Opener behaviour outside Chromium; macOS; Windows.
- Whether the owner confirmed the plan defaults of 6.2 outside the repository.
- The result of the recommended fix for F-1 (Q-5 option a) as a whole build.
