# PR 4b + PR 5 plan — `--with-dev-tools` and `--live-source`

- Base read: `dev` @ `9932b2b63` (`forgejo/dev-f`). Date 2026-11-15. Planning only; nothing in a repository worktree was changed.
- Authority: `docs/design/gui-single-launch-2026-11-14.md` (§3, §4.2, §5, §7, §7a), the owner decisions in the task, `docs/design/gui-single-launch-plans-2026-11-15/host-2-3.md` (host line), `contract/gui-session/README.md` (v1).
- Measured facts are marked **[M]**. Everything else is read from source at the cited line (paths are relative to the repository root on the base commit; `h2b:` means the unmerged PR 2b worktree the h2b worktree @ `b27b95739`).
- Scratch kept next to this plan: `docs/notes/gui-single-launch-prototypes-2026-11-15/dev-4b-5-prototype/` (`design-a-chunk-split.patch`, `scan-closure.mjs`, `second-tab-session-storage.mjs`).

**Limits of this investigation.** The disk had 0.2–0.6 GB free. A full checkout, `uv sync` and the native engine did not fit, so the investigation worktree was a sparse checkout (`gui/`, `contract/gui-session`, `scripts/ci`, two Python packages) with hard-linked `node_modules`. GUI builds and one Chromium probe were run. **No Python was run**: no host, no launcher, no pytest, no wheel build. Those items are UNKNOWN or BLOCKED below and are the first thing each PR measures.

## 0. Measurements

| # | Measurement | Result |
|---|---|---|
| M1 | `npm run build` on the base (module boundaries, `tsc`, `vite build`, production-copy gate) | exit 0, 27.9 s wall; `vite build` alone 2.4–2.9 s |
| M2 | Product `dist` on the base | 6,097,958 bytes, 101 files: `main-*.js` 1,317,755 (gzip 304,696), `main-*.css` 258,932, 98 `woff2` ≈ 4.5 MB, `index.html` 512 |
| M3 | `dist-parity` on the base (`HYPRIAL_GUI_PARITY=1`) | 6,025,729 bytes: `main-*.js` 983,640, `parity-composition-*.js` 261,199, same CSS |
| M4 | Design (a) prototype (`design-a-chunk-split.patch`: 4 lines in `main.tsx`, 12 in `vite.config.ts`) | `dist/dev-tools/tools-*.js` 3,891 bytes, `dist/dev-tools/tools-*.css` 5,043 bytes; `main-*.js` +2,034; `dist` total 6,108,926 (**+10,968 bytes, +0.18 %**); `index.html` unchanged (no preload of the tool chunk) |
| M5 | Production-copy markers on the prototype, per directory (`scan-closure.mjs`) | `index.html`: 0. `assets/**`: 0. `dev-tools/**`: 8 (`dev-01`, `样例`, `dev-data-source`, `dev-module-index`, `dev-module-dock`, `dev-module-entry`, `dev-module-scroll-hint`, `dev-scenario`) — the tool layer's own names, the set PR 4a already lists in `gui/tests/dev-tools/tool-chunk.test.mjs:18-29` |
| M6 | Gates on the prototype | `check:module-boundaries` PASS (dynamic import of `src/dev/**` from `main.tsx` is its built-in exception, `gui/scripts/check-module-boundaries.mjs:11,261-265`). `verify:dev-product-parity` FAIL: `src/main.tsx:125: dev-import: dev-only module: "./dev/tools"` (its allowlist names one path, `gui/scripts/verify-dev-product-parity.mjs:64-67`). `verify-production-copy.mjs dist` FAIL with the 8 markers of M5 |
| M7 | `swagger-ui-dist@5.17.14` (the version pinned in `http_contract/swagger.py:30`) from npm | Apache-2.0. `swagger-ui-bundle.js` 1,452,753 bytes (gzip 403,150, sha256 `c2e4a9ef…88ba1`), `swagger-ui.css` 152,071 (gzip 23,592, sha256 `40170f0e…6117`), `LICENSE` 11,358, `NOTICE` 55. Latest 5.x on npm is 5.35.0 |
| M8 | Chromium 153, second same-origin tab (`second-tab-session-storage.mjs`) | `window.open(url, '_blank')` and `<a target="_blank" rel="opener">` copy `sessionStorage` to the new tab; `noopener` and a plain `<a target="_blank">` do not. A cookie set with `Path=/daemon; HttpOnly; SameSite=Strict` is sent by a `fetch` to `/daemon/*` from a document at `/docs` in all four cases |
| M9 (from `host-2-3.md` §0 M5, not repeated) | Chromium → Python host prototype → Vite dev server | 290 responses on the host port, `[vite] connected.` over `ws://<host port>/?token=…` |

Wheel size delta, computed from M2/M4/M7 (the wheel was not built, BLOCKED): `_gui` grows from 6.10 MB to 7.73 MB uncompressed (+1,627,205 bytes, +26.7 %); compressed about **+0.44 MB** (Swagger 427 KB gzip, tool chunk 3 KB, licence 4 KB). Fonts dominate today's payload and do not compress.

---

## A. Inventory

### A.1 Developer components under `gui/src/dev/` (58 files, 7,556 lines of TS/TSX, plus a 237,428-byte generated JSON)

| Component | Files | Works over a real backend? | Ships in 4b? |
|---|---|---|---|
| Floating layer shell: ball, panel, Escape/outside close, portal outside `#root` | `tools/index.ts`, `tools/impl/{mount.tsx,DevToolsLayer.tsx,context.ts}` (277 lines) | yes — `BackendDevToolsContext`, `tools/impl/context.ts:36-38` | **yes** |
| Module index (6 static rows `dev-01`…`dev-06`) | `DevModuleIndex.tsx`, `module-registry.ts`, `dev-module-index.css` | yes; it is the only tool the layer renders over a backend today (`DevToolsLayer.tsx:26-37`) | **yes** |
| Fixture tools: data-source notice, scenario browser, archived scenarios, preview store, URL mirror (`hyprialScenario`, `hyprialFixture`, `hyprialFirstRun`) | `fixture-tools/**`, `archive/ArchivedScenarios.tsx`, `DevScenarioSelector.tsx`, `SessionToolbar.tsx`, `DevPreviewApp.tsx` | no — they operate the in-process fixture store | no (stay until Stage F) |
| Mock composition and both mock entries (`renderDevEntry`, `renderParityEntry`) | `parity-composition.tsx` (606 lines) | no | no |
| Fixture data and in-process mocks | `fixtures/**`, `mock/**` (incl. `examples.generated.json`), `*-scenarios.ts`, `org/**`, `session/**`, `journey/**`, `first-run-capabilities.ts`, `demo-actions.ts`, `legacy-org-source.ts` | no | no (mock-source gate ledger, shrink-only) |
| Test harness apps | `FixtureApp.tsx` (21 test files import it), `org/OrgDevApp.tsx` | no | no |
| Onboarding private preview | `onboarding-preview/preview.html` + `preview.tsx`; reached only as a Vite dev-server path, not a build input (`vite.config.ts:115-119`) | no | no |

Consequence: over a real backend the shipped tool set is the **module index plus the new API-docs entry**. Over the Contract Mock it is the module index only, until Stage F adds a preset switcher.

### A.2 Entry points and how the product build excludes dev code today

| Mechanism | Fact | Evidence |
|---|---|---|
| Build tokens | `__HYPRIAL_DEV_PREVIEW__` (`mode === 'development'` and `HYPRIAL_GUI_DEV_PREVIEW=1`) and `__HYPRIAL_PARITY__`; both false in `dist`, so both dynamic imports of `./dev/parity-composition` are dead code | `gui/vite.config.ts:63-68,102-105`; `gui/src/main.tsx:91-101` |
| Where the layer is mounted | only inside `renderDevEntry`, with the fixture context | `gui/src/dev/parity-composition.tsx:599-600` |
| `prod-imports-dev` | product files may not import `src/dev/**`; the single built-in exception is a dynamic `import()` in `src/main.tsx` | `gui/scripts/check-module-boundaries.mjs:11,217,261-265` |
| Static parity gate | `main.tsx` may dynamically import exactly `./dev/parity-composition` | `gui/scripts/verify-dev-product-parity.mjs:63-71` |
| Artifact gate | 56 forbidden patterns over every text file under `dist` | `gui/scripts/verify-production-copy.mjs:29-86,95-116` |
| 4a guard | "keeps the formal entry free of the tool layer in this change" | `gui/tests/dev-tools/import-graph.test.ts:201-204` |
| Other entry | `HYPRIAL_GUI_DEV_PREVIEW=1 npm --prefix gui run dev` (fixture preview) and `build:dist-parity` / `serve:dist-parity` are outside the CLI | `gui/package.json:10,26-27` |

### A.3 `hyprial gui dev` today, end to end

| Step | Fact | Evidence |
|---|---|---|
| Grammar | positional `[dev]`; `--gui-home` is dev-only | `shell/impl/cli/commands/gui/commands.py:26-59`; `gui/backend/config.py:76-87` |
| Source choice | `--gui-home` > `HYPRIAL_DEV_GUI_HOME` > the checkout's own `gui/` > wheel manifest `sourceCommit` | `gui/install/launch.py:90-145` |
| Installed wheel | bare cache `<home>/state/gui-dev/source.git`, detached sparse (`--cone gui`) worktree `worktrees/<sha>`, fetch of exactly that object from the manifest remote, `npm ci` inside it; never fetches a branch, resets or cleans | `gui/install/dev_source.py:1-8,39-112,229-256`; `gui/worktree/__init__.py` (309 lines) |
| Requirements | Git, Node ≥ 24, npm; errors `GUI_GIT_MISSING`, `GUI_NODE_MISSING`, `GUI_NPM_MISSING`, `GUI_NODE_VERSION`, `GUI_DEV_SOURCE_{INVALID,MISSING,CONFLICT,FAILED}`, `GUI_HOME_INVALID` (no `node_modules`) | `gui/project.py:114-202`; `dev_source.py:33-36,72`; `launch.py:147-156` |
| Children | `request_http` (or the mock) + `npm run dev -- --host --port --strict-port` (Vite dev server with the Node session plugin) | `config.py:206,218-230`; `lifecycle/session.py:427-571` |
| Output | startup record with `mode`, `hot_reload`, `dev_prompt` (path, HEAD, branch, remote, baseline, dirty, explicit update commands) | `gui/install/report.py:13-100`; `launch.py:214-260` |
| Dev tools | **never mounted**: the CLI refuses `HYPRIAL_GUI_DEV_PREVIEW` | `config.py:259-277` |
| Docs | `docs/guides/gui-dev-mode.md` (659 lines; line 646 forbids the two flags until they exist), `gui/docs/dev-mode-user-guide.zh-CN.md` (258 lines) | — |

### A.4 Swagger today

| Topic | Fact | Evidence |
|---|---|---|
| Switch | `HYPRIAL_REQUEST_HTTP_DEV_SWAGGER=1`, exact value | `http_contract/swagger.py:20,186-190` |
| Routes | `/docs` (HTML shell), `/docs-assets/{asset}`; registered only with the switch. `/openapi.json` is always registered and needs the bearer | `request_http/app.py:79,371-375,421-423,775-802,845-869` |
| Assets | **not vendored and not from a CDN**: the operator supplies a directory through `HYPRIAL_REQUEST_HTTP_SWAGGER_ASSETS`; a missing file is `CONFIG_INVALID` at start | `app.py:650-668` |
| Bearer | the page asks the operator to type the bearer, keeps it in a closure and sends `Authorization` from the browser | `swagger.py:45-56,92-165` |
| Tests | `tests/shell/http_contract/test_swagger.py` (102 lines), `tests/shell/request_http/test_server.py:660-720` | — |
| Limits | `request_http/app.py` is 985 / 1000 lines; the directory holds 7 / 7 files | — |

The existing page cannot be relayed to the GUI browser: it requires the bearer in the page.

### A.5 The host this builds on (PR 2b, unmerged)

| Topic | Fact | Evidence |
|---|---|---|
| Argv | `serve --host --port --root DIR`; no other input | `h2b: gui/host/__main__.py:53-60` |
| WebSocket | off: `ws="none"` | `h2b: __main__.py:18-19,121` |
| Static | `AssetTree`: a manifest-verified tree serves **every** file the manifest names, preloaded at start; SPA fallback for a path without an extension | `h2b: gui/host/web/assets.py:47-86,102-111` |
| Static seam | `Resources` protocol with synchronous `serve(path, head)` and `health()` | `h2b: gui/host/app.py:71-76,250-278` |
| Session body | built in one place | `h2b: app.py:371-402` |
| Refresh | revokes the control's earlier business sessions | `h2b: gui/host/session.py:197-205` |
| Limits | `gui/host/` holds 7 / 7 files (new modules go in `web/`, 3 files today); `app.py` 592 lines; `tests/shell/gui/host/` 7 / 7 (new tests go in `app/` or `web/`) | — |
| Contract | cookies are `Path=/daemon`; flag-gated developer routes are an additive revision listed in the corpus `routes` | `contract/gui-session/README.md` §4, §12, §13 |

---

## B. Design

### B.1 PR 4b: shipping the tools

| Option | How the normal product stays clean | Size **[M]** | Verdict |
|---|---|---|---|
| **(a) One build; the tool layer is a lazy chunk emitted under `dist/dev-tools/`; the host serves that prefix only with the flag and says so in the session response** | product closure = everything outside `dev-tools/`; it has 0 markers today (M5). The host answers 404 for `dev-tools/**` without the flag, and `main.tsx` imports the chunk only when the session carries `devTools` | +11 KB (M4) | **chosen** |
| (b) A second pre-built bundle in the wheel, selected by the flag | two artifacts to keep equal; the flag would change every product byte the browser runs | +6.1 MB (a second `dist`, M2) | rejected: two products, against §1 of the design |
| (c) A second Vite entry that the host injects into `index.html` | product entry has no dev reference at all | +11 KB | rejected: the host would rewrite verified HTML, and live-source needs a second injection path inside Vite; (a) is one mechanism for both |

Design (a) in detail:

| Concern | Decision |
|---|---|
| Chunk placement | `vite.config.ts` `build.rollupOptions.output.chunkFileNames` / `assetFileNames`: a chunk whose modules all live under `src/dev/` and an asset that originates there go to `dev-tools/[name]-[hash]`; everything else stays in `assets/`. Proven by M4 |
| Authority | the host. Without `--dev-tools` it (1) answers 404 for `dev-tools/**`, `/docs`, `/docs-assets/*`, `POST /daemon/session/attach`, `GET /daemon/openapi.json`, and (2) omits `devTools` from the session body. A browser cannot turn the tools on: the chunk does not exist for it |
| How the flag reaches the page | additive field in the session response: `devTools: {"apiDocs": <bool>}`, present only with the flag. `browser-session.ts` (the sanctioned transport file) parses it into `BrowserSession.devTools`; `main.tsx` mounts the layer after `connected(bootstrap)` when it is set. `/__gui/healthz` and the ready line gain `devTools: <bool>` for the launcher and tests |
| Contract | additive revision of `contract/gui-session` v1 (§13 "Compatible": a response field and routes). Integer version stays 1. Old GUI + new host: field ignored, no tools. New GUI + old host: field absent, no tools |
| Module boundary | no new exception in `check-module-boundaries` (M6). `verify-dev-product-parity` gains one allowed specifier, `./dev/tools`. The layer still may not import scenario, session, mock or fixture modules (`tests/dev-tools/import-graph.test.ts:118-137`, unchanged) |
| "No capability → not rendered" | `BackendDevToolsContext` gains `apiDocs: boolean`. The API-docs entry renders only when it is true. No explanatory copy; the unavailable-copy gate is unaffected |
| Fixture preview | unchanged: `renderDevEntry` keeps mounting the same layer with the fixture context until Stage F |

Production-copy gate after 4b (`verify:production-copy`):

| Scope | Rule |
|---|---|
| Product closure: `index.html` and every text file outside `dev-tools/` | the full list of 56 patterns, unchanged, 0 hits |
| `dev-tools/**` except `dev-tools/docs/**` | the list minus `TOOL_LAYER_OWN` = {`dev-01`, `样例`, `dev-module-index`, `dev-module-dock`, `dev-module-entry`, `dev-module-scroll-hint`, `dev-data-source`, `dev-scenario`}. This is the design's "no mock, fixture URL or scenario data". The last two are CSS classes of fixture tools inside `dev-module-index.css`; step G-7 moves them to the fixture side and drops them from the set |
| `dev-tools/docs/**` (third-party) | not text-scanned; the file set is exact (`swagger-ui-bundle.js`, `swagger-ui.css`, `LICENSE`, `NOTICE`) and each sha256 equals the file in `node_modules/swagger-ui-dist` |
| Structure | `index.html` references nothing under `dev-tools/`; no file outside `dev-tools/` is imported by nothing; every `dev-tools/*.js` is reachable from the entry only through a dynamic import |

### B.2 PR 4b: Swagger

| Decision | Text |
|---|---|
| Who serves it | the session host, from its own page. `request_http` is **not edited in 4b**. Its `/openapi.json` already exists behind the bearer |
| Assets | npm devDependency `swagger-ui-dist` pinned exactly, lock integrity `sha512-CVbSfaLp…jHRw==` (M7). A build-only Vite plugin (`generateBundle`, `emitFile` with fixed names) copies the four files to `dist/dev-tools/docs/`. `gui_build_hook.py` copies all of `dist` and hashes every file into `release.json` (`gui_build_hook.py:387-399`), so **no build-hook change**. Licence and NOTICE travel with the bytes |
| Routes (all 404 without the flag) | `GET /docs` → host-owned HTML shell. `GET /docs-assets/{name}` → the host's bootstrap script, or the two Swagger files. `POST /daemon/session/attach` and `GET /daemon/openapi.json` (below) |
| Why a second document needs `attach` | cookies are `Path=/daemon`, so `/docs` itself cannot be cookie-gated; generation and CSRF live only in the main page's memory; a normal refresh from the docs tab would revoke the main page's session (`h2b: session.py:197-205`) and the two tabs would revoke each other |
| `POST /daemon/session/attach`, body `{}` | needs Origin, the control cookie, the session cookie and `x-hyprial-view`. Returns `{generation, csrf, expiresAt}` of the **live** business session; rotates nothing, sets no cookie. It grants nothing a refresh does not already grant to the same proof (refresh needs only control cookie + view) |
| View proof in the docs tab | the tool layer's entry is `<a href="docs" target="_blank" rel="opener">`; the new tab inherits `sessionStorage` (M8) and reads `hyprial.gui.session-view`. Opened any other way, the page says to open it from the developer tools panel and makes no request |
| `GET /daemon/openapi.json` | normal session authentication (cookie, view, generation). The host fetches the upstream `/openapi.json` with the bearer, keeps only the paths `/daemon/request` and `/daemon/request/*`, and removes `security` and `components.securitySchemes`. Contract Mock driver: the route is 404 and `devTools.apiDocs` is `false` |
| "Try it out" | Swagger UI calls `/daemon/request/<method>` on the host origin; the bootstrap's `requestInterceptor` adds `x-hyprial-view`, `x-hyprial-generation`, `x-hyprial-csrf` and deletes `Authorization`. On 401 / `GENERATION_REJECTED` it attaches again once |
| Bearer | never leaves the host: no token field, no bearer in any page, script or response. Test: the token from `server.json` appears in no response body and no request header seen by the browser |
| Page headers | the CSP of `swagger.py:22-26` (`script-src 'self'`, `connect-src 'self'`), `Cache-Control: no-store`, `X-Frame-Options: DENY`, `nosniff`; Host/Origin/Fetch-Metadata checks as on every static path |
| Old switch | the launcher removes `HYPRIAL_REQUEST_HTTP_DEV_SWAGGER` and `HYPRIAL_REQUEST_HTTP_SWAGGER_ASSETS` from the `request_http` child's environment (`gui/backend/source.py:295-309`), so `hyprial gui` cannot start the bearer page. Deleting the code in `request_http` is a separate follow-up (§E, PR 4b-S2) to keep out of the CLI-convergence developer's way |

Rejected for Swagger:

| Alternative | Why not |
|---|---|
| Relay `request_http`'s `/docs` through the host (the sketch in `host-2-3.md` §I) | that page asks for the bearer |
| Render Swagger UI inside the tool chunk in the main document | the chunk would have to name `/daemon/openapi.json` and attach session headers itself; the request-paths gate forbids both outside the transport files and its ledger can only shrink (`scripts/ci/check_gui_request_paths.mjs` header). 1.45 MB of third-party script would also run in the product document |
| Docs tab does a normal refresh | mutual revocation, and the main page remounts on every generation change (`main.tsx:112`) |
| Cookie-only docs session | loopback cookies are shared across ports; the view proof is what stops another local server that received the cookie. Keep it |

### B.3 PR 5: `--live-source`

| Topic | Decision |
|---|---|
| Topology | owner default: Python host on the public port, Vite dev server behind it. Children: backend (`request_http` or mock), Vite (`npm run dev -- --host 127.0.0.1 --port 0`, **no** `HYPRIAL_GUI_*` backend variable, no grant, no bearer), host (`--live-upstream http://127.0.0.1:<port from Vite's ready line>`). `serve.mjs` already accepts port 0 and prints the bound port (`gui/scripts/serve.mjs:107,251-264`), so there is no pre-bind race |
| What PR 3 already plans | `host-2-3.md` steps 3-2 and 3-3: `GuiLaunchPlan.assets_argv`, `web/live.py` (HTTP + WebSocket relay), stop order host → Vite → backend. PR 5 then **only maps flags**. If PR 3 ships without them, PR 5 takes those two steps first (marked "if absent" in §C.4) |
| Host changes for live | `--live-upstream URL` replaces `--root`; `ws="websockets"` only then, `"none"` otherwise (`h2b: __main__.py:121`). WebSocket upgrade: Host and Origin checked with Origin **required** (the prototype did not check it — `host-2-3-prototype/host_proto.py:669-676`), path outside `/daemon`, subprotocol forwarded (`vite-hmr`), query forwarded (Vite's token). HTTP: GET/HEAD only, request headers from an allowlist, `/daemon/*` and `/__gui/healthz` never forwarded, upstream must be a loopback literal, `trust_env=False`, upstream down → 502 host envelope |
| Dev tools with live source | same mechanism: `main.tsx` imports `./dev/tools` when the session says so; Vite serves the source module. Swagger files come from `--docs-assets DIR`: `<gui>/node_modules/swagger-ui-dist` in live mode, `<root>/dev-tools/docs` otherwise |
| Source for a checkout | its own `gui/`, or `--gui-home` / `HYPRIAL_DEV_GUI_HOME` |
| Source for an installed wheel | **keep** the managed exact-`sourceCommit` worktree unchanged (724 lines, tested, never updates or cleans). The user edits `<home>/state/gui-dev/worktrees/<sha>/gui`; the path is printed |
| Wheel user without the tools | fail before any child starts, with the existing codes and reworded messages: no Git → `GUI_GIT_MISSING` "`--live-source` needs Git to fetch the GUI source of this wheel; install Git, pass --gui-home <a gui/ checkout>, or run `hyprial gui` for the built GUI". No Node/npm → `GUI_NODE_MISSING` / `GUI_NPM_MISSING` "`--live-source` runs the GUI dev server and needs Node 24 or newer with npm; `hyprial gui` (built GUI) needs neither". Remote unreachable → `GUI_DEV_SOURCE_FAILED` (existing text) |
| Security | loopback bind on both servers; the browser origin is the host's; the Vite port carries source files only (the standard Vite dev exposure to local pages; Vite's own host check, CORS rule and WS token stay on). `--ip` non-loopback is already refused from PR 2c (OD-5 of the host plan) |
| Port | public port as today (default 4173, 0 refused until PR 6); Vite's port is ephemeral and appears only in `--json` as `live_source_port` |

### B.4 CLI surface after PR 5

```
hyprial gui [dev] [--with-dev-tools] [--live-source] [--gui-home DIR] [--ip A] [--port N] [--no-open] [--json]
hyprial gui dev  ≡  hyprial gui --with-dev-tools --live-source
```

| Input | After 4b | After 5 |
|---|---|---|
| `--with-dev-tools` | new flag; valid in normal and `dev` | same |
| `--live-source` | absent | new flag |
| `dev` | unchanged launch (live source as today), **plus** dev tools | alias for both flags; `dev --with-dev-tools` etc. are accepted and idempotent |
| `--gui-home` | `dev` only | only with live source, else `INVALID_ARGUMENT` naming `--live-source` |
| `HYPRIAL_DEV_GUI_HOME` | `dev` only | read only with live source; ignored otherwise (as today for normal mode) |
| `HYPRIAL_GUI_HOME` | launcher → child handoff; refused as a user input (`launch.py:286-301`) | same |
| `HYPRIAL_GUI_BACKEND_DRIVER` | keep; unset means `daemon` (host plan OD-2) | same |
| `HYPRIAL_GUI_DEV_PREVIEW` | still refused by the CLI | same; Stage F removes the variable with the fixture preview |
| `HYPRIAL_REQUEST_HTTP_DEV_SWAGGER`, `…_SWAGGER_ASSETS` | stripped from the `request_http` child | deleted from `request_http` by 4b-S2 |
| New environment variables | none: the two switches are flags only | none |

`--json` startup record (`gui/install/report.py:33-69`): existing keys stay. Added: `dev_tools` (bool, 4b), `api_docs` (bool, 4b), `live_source` (bool, 5), `live_source_port` (int or null, 5). `hot_reload` equals `live_source`. `mode` stays `"dev"` exactly when the positional `dev` was given or both flags are set, else `"normal"`; it is informational. PR 6 reads `ok`, `url`, `port`, `handoff_path` only.

What `hyprial gui dev` prints (human form): the current block, with `driver`, then `dev tools : on (floating button; API docs inside)` or `off`, `live source: <path> (edits reload in the browser)` or `built assets`, then the existing `Development source` block (`launch.py:246-260`).

---

## C. Ordered steps

Every step: write the named test, see it fail for the stated reason, implement, run the command. `export PATH=<node24-tools>/node_modules/.bin:$PATH`; Python only through `uv run`. No new inline comments; decisions go in file-header blocks. Each PR starts with **step 0: record the base result** of the commands in §D on an untouched checkout.

### C.0 Split and order

| PR | Content | Product Python | Can be **written** | Can be **merged** |
|---|---|---|---|---|
| **4b-G** `feat(gui): ship the developer tool layer as a host-gated chunk` | chunk placement, session field, mount, API-docs entry, Swagger files in `dist`, artifact and parity gates | no | now | after 4b-H **and** PR 3 (before them the old wheel static server, `vite preview` and the Node host serve `dist/dev-tools/**` to anyone) |
| **4b-H** `feat(gui): host serves developer tools and API docs behind --dev-tools` | host argv, prefix gating, `attach`, schema relay, docs shell, contract revision | yes (full pytest) | when 2b is merged (can be drafted on `feat/gui-python-session-host` now) | after 2b |
| **4b-C** `feat(cli): hyprial gui --with-dev-tools` | CLI flag, plan, child argv and environment, `--json`, browser acceptance, docs and skills | yes (full pytest) | after 2c for the wheel path; complete only after PR 3 | after PR 3, 4b-H, 4b-G |
| **4b-S2** `refactor(request-http): remove the bearer Swagger page` | deletions of §E in `request_http` and `http_contract` | yes | any time | after 4b-C, when the CLI-convergence developer agrees (OD-7) |
| **5** `feat(cli): hyprial gui --live-source; dev is an alias` | flag mapping, source rule, messages, docs | yes (full pytest) | after PR 3 | after 4b-C |

Splitting 4b three ways is recommended: G has no Python and one GUI implementer; H is host-internal and unreachable from the CLI; C is the first user-visible change. G and H are written in parallel; merge order is H → G → C.

### C.1 PR 4b-G (GUI)

| Step | Red test first | Change | Run |
|---|---|---|---|
| G-1 | `gui/tests/daemon-request-host/browser-session.test.ts`: a session body with `devTools: {apiDocs: true}` yields `session.devTools.apiDocs === true`; a body without it yields `undefined`; `devTools: "x"` and `{apiDocs: "yes"}` yield `undefined` (never throw) | `gui/src/app/backend/browser-session.ts`: optional `devTools?: {readonly apiDocs: boolean}` on `BrowserSession`, read in `parseSession` | `npx vitest run tests/daemon-request-host/browser-session.test.ts` |
| G-2 | `gui/tests/dev-tools/mount-seam.test.tsx`: backend context with `apiDocs: true` renders one link `[data-dev-tools-api-docs]` with `href` ending `docs`, `target="_blank"`, `rel="opener"`; with `apiDocs: false` the link is absent and the panel holds no text beyond the module index | `tools/impl/context.ts`: `BackendDevToolsContext.apiDocs: boolean`; `tools/impl/DevToolsLayer.tsx`: the link above the module index | `npx vitest run tests/dev-tools` |
| G-3 | `gui/tests/dev-tools/import-graph.test.ts`: replace the case at 201-204 with "the formal entry loads the tool layer with exactly one dynamic import, after `connected(`, guarded by `session.devTools`"; `gui/tests` case for `verify-dev-product-parity` (`COMPOSITION_ROOT_EXCEPTIONS`) expects `./dev/tools` | `main.tsx` (the 4 lines of the prototype patch, passing `{dataSource: 'backend', apiDocs}`; remount on a new generation is not needed, the layer is independent of it); `verify-dev-product-parity.mjs:66` adds the specifier | `npm run verify:dev-product-parity`; `npm run check:module-boundaries` |
| G-4 | `gui/tests/production-artifact-contract.test.mjs` (builds `dist`): every `.js`/`.css` whose content has a `TOOL_LAYER_OWN` marker is under `dev-tools/`; `index.html` names no `dev-tools/` path | `vite.config.ts`: the `output` block of the prototype patch | `npm run build` (fails at the copy gate until G-5) |
| G-5 | `gui/tests/production-copy-gate.test.mjs`: synthetic trees — (1) marker in `assets/x.js` fails; (2) the same marker in `dev-tools/x.js` passes; (3) `hyprialScenario` in `dev-tools/x.js` fails; (4) `index.html` referencing `dev-tools/` fails; (5) `dev-tools/docs/` with a fifth file or a wrong hash fails; (6) no `dev-tools/` directory passes (the gate must not require it, so 4b-G can be reverted) | `verify-production-copy.mjs`: the three scopes of §B.1; export `TOOL_LAYER_OWN`; `tool-chunk.test.mjs:18-29` imports it instead of its own copy | `npx vitest run tests/production-copy-gate.test.mjs tests/dev-tools/tool-chunk.test.mjs`; `npm run build` |
| G-6 | same artifact test: `dist/dev-tools/docs/` holds exactly the four files with the hashes of M7 | `package.json` devDependency `swagger-ui-dist` exact version (OD-3), `package-lock.json`; a build-only plugin in `vite.config.ts` (`apply: 'build'`, `generateBundle`); it must not define `configureServer` (request-paths gate, `routeHooks`) | `npm ci`; `npm run build`; `node scripts/ci/check_gui_request_paths.mjs --base-ref <base> --base-branch dev --json` |
| G-7 (optional, same PR) | `tool-chunk.test.mjs`: the standalone tool bundle no longer hits `dev-data-source` or `dev-scenario` | move those rules from `dev/dev-module-index.css` to a CSS file imported by `fixture-tools`; drop both from `TOOL_LAYER_OWN` | `npx vitest run tests/dev-tools` |
| G-8 | — | `gui/docs/dev-mode-user-guide.zh-CN.md` §1 paragraph of 2026-11-14, `gui/docs/shared-ui-entry.zh-CN.md`, `gui/docs/module-boundaries.md`, `docs/design/gui-dev-product-parity-2026-11-09.md` (one paragraph: the artifact gate's scopes) | `npm run verify` subset of §D |

No product Python changes → the full pytest run is not required; say so in the PR body.

### C.2 PR 4b-H (host)

| Step | Red test first (`tests/shell/gui/host/…`) | Change | Run |
|---|---|---|---|
| H-1 | `test_process.py`: `serve --dev-tools` prints a ready line with `devTools: true`; without it `false`; `--docs-assets` that is not a directory → exit 2 `GUI_ASSETS_UNAVAILABLE` | `__main__.py`: `--dev-tools` (store_true), `--docs-assets DIR` (default `<root>/dev-tools/docs` when it exists); pass a small frozen `DevTools` record to `build_app` | `uv run pytest -q tests/shell/gui/host/test_process.py` |
| H-2 | `web/test_assets.py`: with a manifest that names `dev-tools/tools-a.js`, flag off → 404 `NOT_FOUND` for it and for `dev-tools/docs/swagger-ui.css`; flag on → 200 with `Cache-Control: no-store`; `assets/*` unaffected either way; the same for an unverified `dist` | `web/assets.py`: `AssetTree.open(root, dev_tools=…)`; a `dev-tools/` path is a miss when off (and is never an SPA fallback) | `uv run pytest -q tests/shell/gui/host/web` |
| H-3 | `app/test_routes.py`: session body has no `devTools` key when off; `{"apiDocs": true}` with the flag, the daemon driver and docs assets; `{"apiDocs": false}` with the mock driver or without assets; `/__gui/healthz` carries `devTools` | `app.py:371-402` (`_session_reply`), `:266-278` (`_health`) | `uv run pytest -q tests/shell/gui/host/app` |
| H-4 | `app/test_devtools.py` (new file; `app/` has 5): `POST /daemon/session/attach` — off → 404 `METHOD_NOT_FOUND`; GET → 405; no Origin → 403; body `{"x":1}` → 400; missing control cookie or view → 401; wrong view → 401; session cookie of a revoked session → 401; success → `{generation, csrf, expiresAt}` equal to the live session, **no** `Set-Cookie`, and the main session still validates afterwards | `session.py`: `SessionStore.attach(control_id, session_id, view) -> GuiSession` (reuses `require_control` and `get`); `app.py`: route kind `attach` in `_route_of` and `_dispatch` | same |
| H-5 | same file: `GET /daemon/openapi.json` — off → 404; mock driver → 404; no session → 401; success with `httpx.MockTransport` upstream → only `/daemon/request` paths, no `security`, no `securitySchemes`, upstream called with the bearer, the bearer string absent from the reply; upstream 503 → the host's `HTTP_NOT_READY` envelope | `upstream.py`: reuse `get_json(config, "/openapi.json", authenticated=True)`; `app.py`: route kind `openapi` | same |
| H-6 | `web/test_devtools.py`: `/docs` off → the SPA fallback (`index.html` bytes), on → the shell with the CSP header, `no-store`, `X-Frame-Options: DENY`; `/docs-assets/hyprial-docs.js` on → script that contains `/daemon/session/attach` and no `Authorization: "Bearer`; `/docs-assets/swagger-ui-bundle.js` → the file bytes; unknown name → 404; Host check applies; the shell and script contain no token | `web/devtools.py` (new; `web/` has 3): shell HTML and bootstrap script as constants (adapted from `http_contract/swagger.py:35-167`: no token form; attach → schema → `SwaggerUIBundle` with the header interceptor), asset loading; `app.py:250-264` (`_static`) asks it first when the flag is on | `uv run pytest -q tests/shell/gui/host/web` |
| H-7 | `test_contract_corpus.py`: corpus `routes` lists `POST /daemon/session/attach`, `GET /daemon/openapi.json` with `"flag": "devTools"`; `sessionResponse` schema allows optional `devTools`; the host's route table equals the corpus | `contract/gui-session/gui-session.v1.json`, `README.md` §5 (two rows), §12 (static developer paths, the `sessionStorage` key `hyprial.gui.session-view` as the docs page's input), §15.1 capability `devTools` (false for the Node driver), §16 decision line | `uv run pytest -q tests/shell/gui/host`; `npx vitest run tests/daemon-request-host/session-contract.test.ts` |
| H-8 | conformance driver: one new case in `gui/tests/daemon-request-host/host.test.ts` guarded by capability `devTools`: attach returns the live generation and does not rotate | Python driver passes `--dev-tools` for that case | `HYPRIAL_HOST_IMPL=python npx vitest run --maxWorkers=1 tests/daemon-request-host/host.test.ts` |

`request_http/**` and `http_contract/**` are not edited. Gate commands: §D, all of them, including the full pytest run.

### C.3 PR 4b-C (launcher and CLI)

| Step | Red test first | Change | Run |
|---|---|---|---|
| C-1 | `tests/shell/gui/lifecycle/test_gui_source_selection.py`: `build_plan("", with_dev_tools=True)` → `plan.dev_tools is True` and the host argv ends with `--dev-tools`; default → no such argument; `build_plan("dev")` → `dev_tools is True` | `commands.py:26-60` (`--with-dev-tools`), `backend/config.py` (`GuiLaunchPlan.dev_tools`, argv), `launcher.py` signature | `uv run pytest -q tests/shell/gui/lifecycle` |
| C-2 | `tests/shell/gui/backend/test_source_runtime.py`: with `HYPRIAL_REQUEST_HTTP_DEV_SWAGGER=1` and `…_SWAGGER_ASSETS=/x` in the caller's environment, `request_http_environment(plan)` contains neither | `backend/source.py:295-309` | `uv run pytest -q tests/shell/gui/backend` |
| C-3 | `tests/shell/gui/installed/test_installed_gui.py`: startup payload has `dev_tools`, `api_docs`; the human rendering has the `dev tools` line; no key holds the grant or bearer (existing redaction test extended) | `install/report.py:33-100`; `api_docs` is read from the host's ready line | `uv run pytest -q tests/shell/gui/installed` |
| C-4 | `gui/tests/daemon-request-host/browser-acceptance.mjs`, three new cases against the real `hyprial gui` (isolated home): **(N)** no flag, mock driver → no `[data-dev-tools]`, no request whose path starts `/dev-tools/`, `GET /dev-tools/<name from dist>` is 404, `GET /docs` returns the app shell; **(M)** `--with-dev-tools`, mock driver → layer present with `data-dev-tools-source="backend"`, module index visible, no `[data-dev-tools-api-docs]`; **(D)** `--with-dev-tools`, isolated real daemon → the link opens a tab, Swagger renders, "Try it out" on one read method returns 200, no browser request carries `Authorization`, the `server.json` token occurs in no response body, the main tab's next call still succeeds | fixes only | `npm --prefix gui run test:daemon-request-browser`; compare with the base failing set |
| C-5 | `uv run python scripts/verify/check_cli_help_inventory.py` and the `contract/cli` check fail on the new option | regenerate `docs/reference/cli-command-inventory.json` (`--write`) and `contract/cli/surface.json` (per `contract/cli/README.md`); update the docstring at `commands.py:61-66` | both checks; `./contract/run-all.sh --isolated-only` |
| C-6 | — | docs and skills (§G) | doc gates of §D |

### C.4 PR 5

| Step | Red test first | Change | Run |
|---|---|---|---|
| 5-0 (if PR 3 shipped without them) | `host-2-3.md` steps 3-2 and 3-3 as written | `GuiLaunchPlan.assets_argv`, `web/live.py`, `ws` switch | as there |
| 5-1 | `tests/shell/gui/host/web/test_live.py`: a WebSocket upgrade without `Origin`, with a foreign `Origin`, or with a foreign `Host` is closed before the upstream is contacted; a path under `/daemon` is never relayed; subprotocol and query reach the stub upstream; a non-loopback `--live-upstream` → exit 2 | tighten `web/live.py`; `__main__.py`: `ws="websockets"` only with `--live-upstream` | `uv run pytest -q tests/shell/gui/host/web` |
| 5-2 | `test_gui_source_selection.py`: table over (`dev`, `--with-dev-tools`, `--live-source`) → (`dev_tools`, `live_source`, `mode`): `dev` = both; `--live-source` alone = live without tools; `dev --live-source` accepted; anything else as the positional → `INVALID_ARGUMENT` | `commands.py` (`--live-source`), `config.py`: `parse_action` returns the pair; `GuiLaunchPlan.live_source`; `hot_reload` follows it; target selection keys on `live_source` (rename `select_dev_target` → `select_live_source_target`) | `uv run pytest -q tests/shell/gui/lifecycle` |
| 5-3 | same file: `--gui-home` without live source → `INVALID_ARGUMENT` whose message names `--live-source`; with it → accepted; `HYPRIAL_DEV_GUI_HOME` read only with live source | `install/launch.py:286-301` and the `explicit` branch at 101-113 | same |
| 5-4 | `test_gui_source_selection.py` + `test_source_session.py`: live source + dev tools → host argv has `--live-upstream`, `--dev-tools` and `--docs-assets <gui>/node_modules/swagger-ui-dist` (source) or `<_gui>/dev-tools/docs` (managed wheel worktree); the Vite child's environment has no `HYPRIAL_GUI_BACKEND_DRIVER`, endpoint, token or grant | `config.py`, `lifecycle/session.py` | `uv run pytest -q tests/shell/gui/backend tests/shell/gui/lifecycle` |
| 5-5 | `tests/shell/gui/installed/test_installed_gui.py`: wheel + live source without Git / Node / npm → the codes and reworded messages of §B.3, raised before any child or lease; the managed worktree cases keep passing with `--live-source` in place of `dev` | messages in `install/dev_source.py:70-76`, `project.py:129-139`; no logic change | `uv run pytest -q tests/shell/gui/installed` |
| 5-6 | payload test: `live_source`, `live_source_port`; rendering lines of §B.4 | `install/report.py` | same |
| 5-7 | `browser-acceptance.mjs` case **(L)**: a temporary `git worktree add --detach` of HEAD with `node_modules` symlinked, `hyprial gui dev --gui-home <tmp>/gui --no-open --json`, mock driver; edit one string in a component file under `<tmp>/gui/src`; the text changes in the open page within 10 s without a new launch grant; every request and the WebSocket are on the public port; the tool layer is present; remove the worktree | fixes only | `npm --prefix gui run test:daemon-request-browser` |
| 5-8 | inventory and surface checks fail | regenerate both; `commands.py` help for `[dev]`: "alias for --with-dev-tools --live-source" | as C-5 |
| 5-9 | — | docs and skills (§G); design doc §7.1 marked decided | doc gates |

---

## D. Verification

| Command | Expected |
|---|---|
| `npm --prefix gui run typecheck` | pass |
| `npm --prefix gui run check:module-boundaries` | pass, 0 new exceptions (M6) |
| `npm --prefix gui run verify:dev-product-parity` | pass after G-3 (fails before, M6) |
| `npm --prefix gui run build` | pass; `dist/dev-tools/` present; gate line reports the three scopes |
| `npm --prefix gui test` | no test that passes on the base fails |
| `npm --prefix gui run build:dist-parity && npm --prefix gui run verify:dev-browser-parity` | unchanged result versus the base (the parity entry does not mount the backend layer) |
| `npm --prefix gui run test:browser` (dev overlay, fixture preview) | unchanged versus the base |
| `node scripts/ci/check_gui_request_paths.mjs --base-ref <base> --base-branch dev --json`; `node --test scripts/ci/check_gui_request_paths.selftest.mjs` | pass; ledger and scanner unchanged (4b adds no request path under `gui/src`; the docs script lives in the Python host) |
| `node scripts/ci/check_gui_unavailable_copy.mjs …` (workflow invocation, `gate-gui.yml:241-247`) | pass; no new copy |
| `node scripts/ci/check_gui_mock_source.mjs …` (once 1e-2 is merged) | pass; ledger unchanged |
| `HYPRIAL_HOST_IMPL=python npx vitest run --maxWorkers=1 tests/daemon-request-host` | pass including the `devTools` case |
| `npm --prefix gui run test:daemon-request-browser` | cases N, M, D (4b-C) and L (5) pass; others as on the base |
| `uv run --with libcst==1.9.0 pytest -q -n 2 tests/shell/gui tests/shell/request_http tests/shell/http_contract tests/integration/request_http` | pass apart from the base failing set |
| `uv run --with libcst==1.9.0 pytest tests/` (`-n 2`) | required for 4b-H, 4b-C, 4b-S2, 5; compare with the base set |
| `uv run ruff check .` | pass |
| `uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py`; `…/check_module_boundaries.py` | pass (`gui/host/` stays at 7 files; new code in `web/`) |
| `uv run python scripts/verify/check_cli_help_inventory.py`; `./contract/run-all.sh --isolated-only` | pass after regeneration |
| Wheel acceptance (4b-C; PASS / FAIL / BLOCKED in the body) | build a wheel, install into a throwaway venv with an isolated `HYPRIAL_HOME`; `unzip -l` shows `hyprial/_gui/dev-tools/…` and `release.json` lists them; `hyprial gui --no-open --json` → case N; with `--with-dev-tools` → case D |

What proves the two owner requirements:

| Claim | Proof |
|---|---|
| A normal launch shows no dev tool | artifact gate scope 1 (0 markers outside `dev-tools/`); H-2/H-3/H-4/H-5/H-6 "off" cases (404, no `devTools`); browser case N |
| With the flag the tools work over a real backend and over the Contract Mock | browser cases D and M; H-3 capability table; G-2 rendering rule |

`gate-gui.yml` changes: none for 4b-G (the build step and the artifact step at 290-295 run the new gate as they are); 4b-H adds `tests/shell/gui/host/` paths only if 2b's step list names files; 4b-C/5 rely on the existing browser step.

---

## E. Deletions

| PR | Path | Lines | Note |
|---|---|---|---|
| 4b-G | `gui/tests/dev-tools/import-graph.test.ts:201-204` | 4 | replaced by the mount case |
| 4b-G | `gui/tests/dev-tools/tool-chunk.test.mjs:18-29` | 12 | imports the gate's set |
| 4b-S2 | `src/hyprial/shell/impl/http_contract/swagger.py` | 239 | whole file |
| 4b-S2 | `src/hyprial/shell/impl/http_contract/__init__.py:46-58` and the eight names in `__all__` (67-95) | ≈ 20 | |
| 4b-S2 | `src/hyprial/shell/impl/request_http/app.py`: imports 48-52; `"/docs"` in 79; 81 (`_DOCS_ASSET_NAME`); 371-375; in 421-423 the `/docs` and `/docs-assets/` terms; 650-668; 775-802; 857-869 | ≈ 75 | the only edits to `request_http`; `/openapi.json` stays |
| 4b-S2 | `tests/shell/http_contract/test_swagger.py` | 102 | host tests H-5/H-6 replace it |
| 4b-S2 | `tests/shell/request_http/test_server.py:25-26,333,660-720` | ≈ 65 | |
| 4b-S2 | `gui/docs/http-request-client.md` Swagger paragraph | few | |
| 5 | `backend/config.py:76-87` (`parse_action` mode strings), `:206-212` (`frontend_script` / `GUI_SCRIPT_MISSING` for `start`, if PR 3 left it) | ≈ 20 | |
| 5 | `install/launch.py:286-301` (`_reject_normal_home` wording), `select_dev_target` name | renames | |
| 5 | `docs/guides/gui-dev-mode.md:644-647` (the "do not use the flags yet" note) | 4 | |

Not deleted here, recorded for Stage F: `HYPRIAL_GUI_DEV_PREVIEW`, `__HYPRIAL_DEV_PREVIEW__`, `__HYPRIAL_PARITY__`, `build:dist-parity`, `serve.mjs --mode dist-parity`, `parity-composition.tsx`, `fixture-tools/**`, `SimulatedDevToolsContext`.

---

## F. Risks, UNKNOWNs, owner decisions

### F.1 Owner decisions (the default is what the implementer does if nobody answers)

| ID | Decision | Default | Alternative | User-visible |
|---|---|---|---|---|
| OD-1 | How tools ship | one build, lazy chunk under `dev-tools/`, host-gated (§B.1 a) | second bundle | no |
| OD-2 | Swagger placement | host-served page in a second tab, `attach` + schema relay; no bearer in the browser | Swagger inside the tool chunk — needs a request-paths ledger exception | **yes** (new page, no token prompt) |
| OD-3 | Swagger UI version | keep `5.17.14`, the version the code pins today | move to the latest 5.x in the same PR | no |
| OD-4 | Docs when opened without the opener | page explains and does nothing | cookie-only docs session — weakens the view-proof rule | **yes** |
| OD-5 | API docs over the Contract Mock | not rendered (no capability) | host serves `contract/daemon-request/openapi.json` from a source checkout | **yes** |
| OD-6 | `hyprial gui dev` between 4b-C and 5 | gets the tool layer as soon as 4b-C lands | wait for PR 5 | **yes** |
| OD-7 | Removing the old Swagger code from `request_http` | separate PR 4b-S2 after 4b-C, timed with the CLI-convergence developer; until then the launcher strips the two variables | delete inside 4b-C (≈ 75 lines in a file at 985 / 1000 that another developer is editing) | **yes** for people who run `request_http` by hand with the variable: after 4b-S2 that page is gone |
| OD-8 | Installed wheel + `--live-source` | keep the managed exact-SHA worktree as it is | drop it and require `--gui-home` (−724 lines, wheel users must clone themselves) | **yes** |
| OD-9 | Flags only, no environment twins for the two switches | yes | add `HYPRIAL_GUI_WITH_DEV_TOOLS` / `…_LIVE_SOURCE` — a second way to turn tools on | **yes** |
| OD-10 | `--gui-home` without live source | `INVALID_ARGUMENT` | let `--gui-home` imply `--live-source` | **yes** |
| OD-11 | `mode` in `--json` | kept, informational; new booleans are authoritative | remove `mode` — breaks readers | no |
| OD-12 | Split of 4b | G / H / C (+ S2) | one PR, same commits in the order H, G, C | no |
| OD-13 | The module index as a shipped tool | ship it (it is what exists) | ship only the API-docs entry and leave the index to the fixture preview | **yes** |

### F.2 Risks

| Risk | Mitigation |
|---|---|
| 4b-G merged before the host gates `dev-tools/**` | merge order H → G, and G after PR 3 (§C.0); the gate itself does not depend on the directory existing |
| The tool chunk grows a dependency that pulls product or fixture code into `dev-tools/` | G-4 (markers only under `dev-tools/`), `import-graph.test.ts` allowlist, gate scope 2 |
| Rolldown changes how `chunkFileNames` receives `moduleIds` | G-4 fails loudly; Vite is pinned (`8.3.5`) |
| `attach` is a new way to read CSRF | needs control cookie + session cookie + view + Origin; strictly no more than refresh grants; flag-gated; H-4 pins every refusal |
| Docs tab outlives a session change | interceptor re-attaches once; otherwise the page asks for a reload |
| Swagger UI evaluates schema content | schema comes from the user's own daemon; CSP `script-src 'self'`, no inline script, `X-Frame-Options: DENY` |
| Popup blocked, or a webview that does not open new windows (PR 6) | the entry is a user-clicked link, not a scripted popup; shell behaviour is UNKNOWN and belongs to PR 6 |
| Vite dev port reachable by other local pages | source files only; no backend variable in that child (5-4) |
| WebSocket relay without an Origin check (as in the prototype) | 5-1 is red first |
| `source.py` is 891 / 1000 lines and is edited by 1e-2, 2c, 3 and here | C-2 touches one function; rebase last |
| Conflicts in `browser-session.ts`, `main.tsx`, `vite.config.ts` with PR 3 (which removes the Node plugin import from `vite.config.ts:5,69-73,108`) and 1e-2 | disjoint hunks; G rebases after PR 3 anyway |
| Wheel grows by 1.6 MB uncompressed for a page most users never open | accepted by the owner decision "tools ship in the wheel"; +0.44 MB compressed |

### F.3 UNKNOWN / BLOCKED

| Item | State | How to resolve |
|---|---|---|
| Anything in Python (host, launcher, pytest base set) at `9932b2b63` | not run (disk) | step 0 of H, C and 5 |
| Wheel build and real wheel size | BLOCKED (disk, native engine) | wheel acceptance in 4b-C |
| The exact shape `request_http` returns for `/openapi.json` (bare document or enveloped; `app.py:765-773`) | read, not run | first assertion of H-5 against a real `request_http` in `tests/integration/request_http` |
| Whether Swagger UI 5.17.14 works with only `spec`, the interceptor and the CSP above | the same combination is what `swagger.py` ships today, but no test loads the real bundle | browser case D |
| `sessionStorage` copy to an opener tab in Firefox, Safari and WKWebView | Chromium only (M8) | manual check before PR 6; OD-4 default degrades safely |
| Final shape of PR 3 (does it deliver `web/live.py` and the third child?) | PR 3 not started | 5-0 covers the gap |
| Final shape of the JS Contract Mock's metadata endpoints | 1e-2 in progress | `apiDocs` is false for the mock regardless |
| HMR through the real 2b host (M9 used the prototype) | not re-measured | browser case L |
| macOS and Windows | every measurement is Linux | PR 6 |

---

## G. PR-body inputs

| PR | Skill-Impact | Contract-Impact |
|---|---|---|
| 4b-G | none (no operator-visible change until 4b-C). `gui/docs/dev-mode-user-guide.zh-CN.md` §1 wording | none: the client reads an optional field it previously ignored |
| 4b-H | none (not reachable from the CLI) | `contract/gui-session` v1 **additive revision**: session body optional `devTools`; routes `POST /daemon/session/attach`, `GET /daemon/openapi.json`; static `/docs`, `/docs-assets/*`, `dev-tools/*`; `/__gui/healthz` gains `devTools`. All flag-gated; the integer version stays 1 |
| 4b-C | `src/hyprial/skills/hyprial-ops/SKILL.md:210-225` (new flag; API docs are opened from the tool panel; no bearer); `.agents/skills/gui-workflow/SKILL.md:9`; `AGENTS.md:26-44`; `docs/guides/gui-dev-mode.md`; `gui/docs/dev-mode-user-guide.zh-CN.md`; `gui/README.md:33,125` | `contract/cli/surface.json` regenerated (new option); `hyprial gui --json` gains `dev_tools`, `api_docs` (additive; not yet a `contract/` entry — PR 6 defines it) |
| 4b-S2 | `hyprial-ops` if it mentions the variable (it does not today: 0 hits) | removed: environment switches `HYPRIAL_REQUEST_HTTP_DEV_SWAGGER`, `HYPRIAL_REQUEST_HTTP_SWAGGER_ASSETS` and the `request_http` routes `/docs`, `/docs-assets/*`. `contract/` has no entry for them (confirm with `git grep` at PR time), otherwise `Contract-Impact: development-only routes, replaced by the session host's` |
| 5 | same skill and doc files as 4b-C, plus `CONTRIBUTING.md:18-20`, `gui/docs/ui-development-standard.zh-CN.md:80,120,419`, `scripts/build/docker/gui-debug-container-entrypoint.sh:79` (wording only; Docker entry is PR 7), `docs/reference/known-problems.md:639` | `contract/cli/surface.json` regenerated; `--json` gains `live_source`, `live_source_port`; `--gui-home` now requires live source (`INVALID_ARGUMENT` otherwise) |

Every body also lists: commands run with results, the base failing set next to the PR's, and UNKNOWN / BLOCKED items (wheel install, macOS, Windows, non-Chromium browsers).

---

## H. What PR 6 and Stage F need from this

| Consumer | Needs | Provided |
|---|---|---|
| PR 6 minimal shell | `hyprial gui --no-open --json` unchanged | no existing key changes; the shell passes neither flag by default and gets no tool, no `/docs`, no `dev-tools/**` |
| PR 6 | one origin for the navigation guard | the docs page is on the same origin; it opens as a new window, which the shell may refuse (then the entry does nothing — UNKNOWN, F.3) |
| PR 6 | a developer build of the shell | pass `--with-dev-tools`; nothing else |
| PR 6 | installer size | `_gui` +1.6 MB uncompressed on all three targets; no platform-specific bytes |
| PR 6 | still open from the host line | `--port 0`, the `contract/cli` entry for the startup record, Windows runtime |
| Stage F | a place for mock-only tools | `devTools` is an object: add `mockPresets: true` when the driver is the Contract Mock and the mock exposes preset control; the tool lives in `src/dev/tools`, gated by that capability |
| Stage F | removal of the fixture pairing | delete `SimulatedDevToolsContext`, `fixture-tools/**`, `parity-composition.tsx`, the two build tokens and `dist-parity`; `TOOL_LAYER_OWN` then shrinks to the module-index names |
| Stage F | one gate to tighten | artifact gate scope 2 already forbids mock, fixture URL and scenario data inside `dev-tools/`; the mock-source ledger covers the source side |
| §7a (fold the host into `request_http`) | no new obstacle | everything added here is in the session layer and `web/`; `attach` and the schema relay move with it |

---

## Summary (15 lines)

1. Over a real backend the tool layer renders one thing today, the module index; every other developer component under `gui/src/dev` operates in-process fixtures and stays out of the wheel until Stage F.
2. `hyprial gui dev` never mounts the tools today: the layer is mounted only by the fixture entry, and the CLI refuses the variable that selects it.
3. Chosen design: one build; the layer is a lazy chunk under `dist/dev-tools/`; the host serves that prefix only with `--dev-tools` and reports `devTools` in the session response.
4. Measured: the chunk is 3.9 KB JS + 5.0 KB CSS, `dist` grows 11 KB, and the product closure keeps 0 forbidden markers with the existing list.
5. The artifact gate gets three scopes: product closure (full list), `dev-tools/` (no mock, fixture URL or scenario data), vendored Swagger files (exact set and hashes).
6. Swagger UI is not vendored today; the operator supplies a directory and types the bearer into the page. That page cannot be shown to the GUI browser.
7. New Swagger path: npm devDependency copied into `dist/dev-tools/docs/` (1.6 MB, +0.44 MB compressed, Apache-2.0), a host-owned page at `/docs`, a schema relay, and `POST /daemon/session/attach` so a second tab can use the live session without rotating it.
8. Measured in Chromium: a tab opened with `rel="opener"` inherits the view proof and sends the `/daemon` cookies; a `noopener` tab does not.
9. The bearer never reaches the browser; `request_http` is not edited in 4b, and its old Swagger code leaves in a small follow-up.
10. All of this is an additive revision of the session contract v1.
11. 4b splits into G (GUI, no Python, writable now), H (host, after 2b) and C (CLI, after PR 3); merge order H → G → C.
12. PR 5 is flag mapping on top of PR 3's Vite-behind-host relay, plus an Origin check on the WebSocket upgrade, the source rule, and reworded failures for a wheel user without Git or Node.
13. The managed exact-SHA worktree for an installed wheel is kept unchanged.
14. Not run here: any Python, the wheel build, and the browser paths through the real host; each PR measures its base first.
15. Thirteen owner decisions have defaults; nine change something a user can see.

## Estimated diff

| PR | Added | Deleted | Files |
|---|---|---|---|
| 4b-G | ≈ 450 (tests 220, gate 70, config 40, source 60, docs 40, lockfile 20) | ≈ 60 | ≈ 16 |
| 4b-H | ≈ 900 (`web/devtools.py` 230, app/session/main 140, contract 60, tests 470) | ≈ 10 | ≈ 14 |
| 4b-C | ≈ 600 (launcher 90, tests 200, browser cases 150, generated JSON 40, docs 120) | ≈ 100 | ≈ 20 |
| 4b-S2 | ≈ 10 | ≈ 500 | ≈ 7 |
| 5 | ≈ 450 (+ ≈ 350 if step 5-0 is needed) | ≈ 250 | ≈ 22 |

## Owner decision (2026-11-15): Windows out of scope for now

Only Linux and macOS are built and verified. Corrected by the owner the same day: do NOT delete Windows-related tests, gates or CI targets; existing checks such as `scripts/ci/check_windows_portability.py` and the PR-template item keep running and must stay green. Windows-only problems are recorded and ignored for now: no new Windows code or tests, no blocking on them. Packaging targets are macOS arm64 and Linux x64; Windows items in this plan are not scheduled.
