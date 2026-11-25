# PR 2 + PR 3 plan — one Python host for the GUI (the "host line")

- Base: `dev` @ `182c118ab` (investigation worktree the host-plan worktree, removed after planning). Date 2026-11-15.
- Authority: `docs/design/gui-single-launch-2026-11-14.md` (§3 target, §4.1–4.3 constraints, §5 PR table, §7 open items).
- Planning only. Nothing in any worktree was edited. Scratch: `docs/notes/gui-single-launch-prototypes-2026-11-15/host-2-3-prototype/` (a working Python port and the scripts that measured it).
- Measured facts are marked **[M]**. Everything else is read from source at the cited line.

## 0. What the measurements say

| # | Measurement | Result |
|---|---|---|
| M1 | Node host unit tests on the base: `npx vitest run --maxWorkers=1` over `host`, `bootstrap-session`, `handoff-plugin`, `browser-session`, `app-composition` | 5 files, 74 tests pass, 15 s |
| M2 | The 17 cases of `gui/tests/daemon-request-host/host.test.ts`, mechanically rewritten to spawn a Python host over real HTTP (`make-python-driver-test.mjs`), against `host_proto.py` | 17 / 17 pass, 18–20 s |
| M3 | `real-daemon-loopback.test.ts` (isolated real daemon + `request_http`, generated client, 4 adapters, lost-response reconcile) and `contract-mock-loopback.test.ts`, `startHost()` swapped for the Python prototype | 5 / 5 pass, 43 s |
| M4 | Real Chromium → Python prototype (`--root gui/dist`, built from the base) → Python Contract Mock | App renders; `POST /daemon/session` 200; 15 `/daemon/*` calls, 0 non-200; both cookies `Path=/daemon; HttpOnly; SameSite=Strict`; reload refreshes the session; 0 console errors |
| M5 | Real Chromium → Python prototype → Vite dev server (HTTP and HMR WebSocket reverse-proxied by the Python host) | 290 responses, all 2xx/304, every one on the host port; `[vite] connected.` on `ws://<host port>/?token=…`; 15 `/daemon/*` calls |
| M6 | `uv run --with libcst==1.9.0 pytest -q -n 2 tests/shell/request_http tests/shell/gui tests/integration/request_http` on the base | 738 passed, **2 failed** (pre-existing, §E.2), 503 s |
| M7 | `npm --prefix gui run build` on the base | exit 0, 24 s, `dist` 6.1 MB (index.html, favicon.svg, `assets/*`) |
| M8 | `npm --prefix gui run test:daemon-request-browser` on the base | 16 tests, **6 pass, 10 fail**, 587 s (§E.3) |

Consequence: the Node host's observable behaviour can be reproduced by a small ASGI app on dependencies the wheel already has (`uvicorn==0.54.0`, `httpx==0.28.1`, `httptools==0.9.0`, `websockets==15.0.1`; `pyproject.toml:18-38`). The prototype is 1,058 lines in one file, Vite proxy included.

---

## A. Inventory

### A.1 `hyprial gui` today

| Topic | Fact | Evidence |
|---|---|---|
| CLI surface | `hyprial gui [dev] --ip --port --no-open --gui-home --json`; foreground; `session.wait()` then `session.stop()`; exit 130 / 143 | `shell/impl/cli/commands/gui/commands.py:26-113` |
| Modes | `""` → `normal`, `dev` → `dev`; anything else `INVALID_ARGUMENT` | `gui/backend/config.py:76-87` |
| Port | default 4173; `1..65535`, **0 refused** ("the URL is printed"); pre-bound check then child binds (TOCTOU) | `config.py:31-32,104-111,168`; `backend/handoff.py:46-64` |
| `--ip` | any IP literal is accepted, including `0.0.0.0`; Node `serve.mjs` then refuses non-loopback, the wheel static server does not | `config.py:90-101`; `handoff.py:67-71`; `gui/scripts/serve.mjs:22,102-106`; `install/server.py:121-138` |
| Target choice (normal) | wheel `_gui` present → static; installed but no assets → `GUI_ASSETS_MISSING`; else source checkout | `install/launch.py:57-88` |
| **Wheel (①)** | spawns `python -m hyprial.shell.impl.gui.install.server --root <_gui> --host --port`; a driver in the environment is **refused** (`GUI_INSTALLED_BACKEND_UNSUPPORTED`); no admission, no lease, no grant, browser opens the bare URL | `config.py:170-202`; `lifecycle/session.py:386-424` |
| Wheel static server | `http.server`; `GET /__gui/healthz` → `dataMode: "fixture"`; SPA fallback to `index.html`; `/api/gui/v1/*` → 501 `BACKEND_NOT_WIRED` / `not implemented: hyprial backend not wired`; manifest verified at start; **no** `/daemon/*`, no Host/Origin check | `install/server.py:24-27,59-78,80-118,121-138` |
| **Source normal (③)** | admission → `npm run build` → `request_http` child (daemon driver) or Contract Mock child → Node child `npm start -- --host --port --strict-port` (Vite preview + `daemonRequestPlugin`) | `config.py:204-256`; `lifecycle/session.py:427-571` |
| **Source dev (④⑤⑥)** | same, Node child is `npm run dev` (Vite dev server + the same plugin); installed dev uses the managed exact-SHA worktree | `config.py:206,218`; `install/launch.py:90-145` |
| Driver | `HYPRIAL_GUI_BACKEND_DRIVER` must be exactly `daemon` or `contract-mock`; no default | `backend/admission.py:26,90-104` |
| Principal | never an input. Serving: `user:<owner>` derived from the committed owner; `HYPRIAL_GUI_PRINCIPAL`, if present, must equal it. Ownerless home: `None`, and a provided principal is refused. `HYPRIAL_OWNER` is forbidden | `admission.py:105-109,170-197` |
| Config conflicts | endpoint, upstream token, grant, handoff/publication paths, auth/setup/reference/dev-preview variables are refused | `admission.py:30-41,110-118`; `config.py:259-277` |
| Daemon readiness | serving home: live `ping` + catalog identity check; bootstrap home: none (the `request_http` child hosts bootstrap itself) | `admission.py:217-227,243-313` |
| Mock driver | `python -m contract_mock … --allow-origin <origin>` from `<source>/contract/daemon-request`; scenario from `HYPRIAL_GUI_CONTRACT_MOCK_SCENARIO` | `backend/source.py:314-338,349-362`; `admission.py:124-153` |
| Runtime dir | `<state>/gui/{daemon,contract-mock}` 0700, `supervisor.lock` flock, stale files removed | `source.py:118-226`; `admission.py:81-87` |
| Private files | `request-http.json` (config), `server.json` (bearer, 0600, verified against pid/birth/owner/node/socket), `frontend-authority.json`, `launch.url`, `launch.ready`, `logout-custody.json`, three logs | `source.py:52-57,229-281,365-456,459-531` |
| Launch grant | `secrets.token_urlsafe(48)`; URL is `<origin>/#grant=<quoted>`; opened in the browser, or written to `launch.url` (O_EXCL 0600) when `--no-open`; `launch.ready` marks publication | `source.py:652-705`; `lifecycle/session.py:507-565` |
| Node child env | driver, endpoint, node, upstream token, grant, optional principal, handoff/publication/authority paths | `source.py:610-641`; `lifecycle/session.py:516-521` |
| Readiness | Node prints `HYPRIAL_GUI_READY {…}` (service, mode, dataMode `hosted`, driver, port, url); launcher then polls `/__gui/healthz` | `gui/scripts/serve.mjs:251-264`; `source.py:721-748`; `lifecycle/process.py:257-293` |
| Backend rotation | `wait()` re-reads `server.json` every 0.2 s; a new transport generation republishes `frontend-authority.json`; the Node host revokes business sessions and keeps the control proof | `lifecycle/session.py:215-236`; `lifecycle/rebind.py:22-73`; `gui/server/daemon-request/authority.ts:11-55` |
| `--json` | one startup object; never contains the grant, bearer or a credential URL; `handoff_path` names the 0600 file | `gui/install/report.py:13-70`; `commands.py:94-103` |
| Platform | the source runtime needs `fcntl` and `os.getuid`; the wheel static path does not (a test pins this) | `backend/platform_support.py:16-40`; `tests/shell/gui/installed/test_installed_admission.py:37-55` |
| Placeholder wording | AGENTS.md still describes a "Node 占位服务"; `docs/guides/frontend-components.md:138` records it as deleted. What remains is `install/server.py` (501) and the Node host's "no driver" state (`dataMode: unavailable`) | `AGENTS.md:40-44`; `gui/scripts/serve.mjs:244-246` |

### A.2 `request_http` today (the bearer upstream; stays as is)

| Topic | Fact | Evidence |
|---|---|---|
| Location, start | `src/hyprial/shell/impl/request_http/`; `python -m hyprial.shell.impl.request_http serve --config <abs>` (also `bootstrap --home`, `--launch-nonce-stdin`) | `request_http/__main__.py:32-62`; `gui/backend/source.py:284-292` |
| Listener | FastAPI on Uvicorn in a thread, `127.0.0.1` only, port 0 allowed, `Connection: close` on every response | `config.py:407-417`; `server.py:177-224`; `app.py:299-301` |
| Routes | `GET /healthz` (open); bearer: `GET /readyz`, `GET /daemon/request`, `GET /openapi.json`, `POST /daemon/request/<method>` (one route per catalog method plus a catch-all); `GET /docs`, `/docs-assets/<asset>` only with `HYPRIAL_REQUEST_HTTP_DEV_SWAGGER=1` | `app.py:821-916,641-661`; `http_contract/swagger.py:20-21,186-200` |
| Auth | bearer generated per listener (`token_urlsafe(32)`), published only in `server.json` | `server.py:126,294-335`; `app.py:469-481` |
| Guards | Host allowlist, Origin allowlist (`allowedOrigins` + own endpoint), **no query string**, fixed path set, header budgets, `Content-Length` required, semaphore | `app.py:330-430,503-548` |
| Reaches the daemon by | kernel IPC `daemon_request_once(method, params, socket_path=…)`; for `local-gui` the callable is a `GuiRuntimeHost` (bootstrap runtime, in-process promotion) | `server.py:97,385-392,712-714`; `__init__.py:91-136` |
| `local-gui` lifecycle | `serve_gui`: on initialize the child promotes in-process and **publishes a new listener: new port, new bearer, new generation**; on logout it reopens the bootstrap host | `bootstrap/gui.py:25-29,58-131` |
| `nativeHost` branch | `nativeHost.promotion=external` → `serve_external` (target-owned promotion for the deleted Tauri shell); `hostContract` mirrors `contract/native-host/native-host.v1.json` and is published in `server.json` and the catalog | `config.py:41,170-172,289-314`; `__init__.py:51-65`; `bootstrap/external.py:1-20`; `catalog.py:40-43,217-227`; `server.py:302-303,584-585` |
| `desktop_network.py` | still exported by `shell/interface.py:9,26`, tested by `tests/integration/desktop/test_desktop_network.py` (195 lines); unrelated to the session host | — |
| Size limits | `app.py` is 985 lines; the directory holds 7 files. **Nothing can be added to this package** | `wc -l` |
| Tests | `tests/shell/request_http/**` (11 files, 3,582 lines); `tests/integration/request_http/**`; M6 | — |

### A.3 The Node host `gui/server/daemon-request/` (1,781 lines, 7 files)

| File | Lines | Role |
|---|---|---|
| `index.ts` | 75 | Vite plugin: mounts the middleware on dev and preview servers; launch-file lifecycle |
| `authority.ts` | 55 | reads `frontend-authority.json`; rotates upstream authority |
| `config.ts` | 158 | environment → frozen config |
| `envelope.ts` | 136 | host envelopes, body limits |
| `proxy.ts` | 569 | routing and per-route rules |
| `session.ts` | 376 | `SessionStore`, cookies, Host/Origin check |
| `upstream.ts` | 412 | upstream probe (daemon / mock) and relay |

**Configuration** (`config.ts:127-158`): `HYPRIAL_GUI_BACKEND_DRIVER` (absent → host not mounted), `HYPRIAL_BACKEND_ENDPOINT` (`http://<loopback>:<port>`, no path), `HYPRIAL_GUI_UPSTREAM_TOKEN`, `HYPRIAL_GUI_LAUNCH_GRANT` (≥ 32 chars), `HYPRIAL_GUI_PRINCIPAL` (`user:<owner>`; **absent = anonymous bootstrap**, blank = error), `HYPRIAL_NODE_ID`, `HYPRIAL_GUI_HANDOFF_PATH` (`…/launch.url`), `HYPRIAL_GUI_PUBLICATION_PATH` (`…/launch.ready`), `HYPRIAL_GUI_AUTHORITY_PATH` (`…/frontend-authority.json`), `HYPRIAL_GUI_LAUNCH_TTL_MS` (60 000), `HYPRIAL_GUI_SESSION_TTL_MS` (43 200 000), both `1000..86 400 000`, `HYPRIAL_GUI_MAX_SESSIONS` (32, `1..1024`).

**Global rules**

| Rule | Detail | Evidence |
|---|---|---|
| Scope | only `/daemon/*`; everything else falls through to Vite | `proxy.ts:67-89,348-361` |
| Host | required; hostname in `{localhost,127.0.0.1,::1}`; port present and equal to the listener's local port; no userinfo/path → else 403 `HTTP_HOST_DENIED` | `session.ts:317-347` |
| Origin | must equal `http://<Host>`; may be absent **only on GET**; else 403 `ORIGIN_REQUIRED` / `ORIGIN_REJECTED` | `session.ts:348-365`; `proxy.ts:364` |
| Fetch metadata | `Sec-Fetch-Site`, if present, must be `same-origin` or `none` → else 403 `ORIGIN_REJECTED` | `session.ts:366-374` |
| Authority | read before every route; file must be regular, ≤ 16 384 B, mode `& 077 == 0`, owned by the process uid, opened `O_NOFOLLOW`; new content revokes all business sessions; a revision may never repeat; any fault → 503 `GUI_AUTHORITY_UNAVAILABLE` | `authority.ts:16-54` |
| `assertCurrent` | if the authority object changed during a request → 409 `GUI_SESSION_CHANGED` (`dispatch: unknown` after a write was sent) | `proxy.ts:366-373,493,507` |
| Envelope | `{schemaVersion:1, ok, requestId, …}`; failures add `code, message, dispatch`, optional `method, data`; headers `Content-Type: application/json; charset=utf-8`, `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, `X-Hyprial-Request-Id` | `envelope.ts:41-80` |
| Request id | host ids are `gui-<uuid4>`; a relayed upstream id always wins | `envelope.ts:37-39`; `upstream.ts:343` |
| Limits | session bodies 8 192 B, method bodies 131 072 B → 413 `HTTP_REQUEST_TOO_LARGE`; upstream response 4 MiB → 502 `UPSTREAM_RESPONSE_TOO_LARGE`; upstream timeout 15 s | `envelope.ts:33-35,86-106`; `upstream.ts:61-82,113,373` |
| Body | `Content-Type` must be `application/json` (415 `HTTP_UNSUPPORTED_MEDIA_TYPE`); must parse to a JSON **object** (400 `INVALID_REQUEST`) | `envelope.ts:116-136` |
| Cookies | `hyprial_gui_control`, `hyprial_gui_session`; `Path=/daemon; Max-Age=<s≥1>; HttpOnly; SameSite=Strict`; no `Secure` (loopback HTTP); first value wins on duplicates; undecodable value → 400 | `session.ts:69-70,274-315` |
| Tokens | 32 random bytes base64url for control id, view, session id, generation, CSRF; constant-time comparison | `session.ts:73-82,252-271` |
| Upstream headers | only `accept`, `content-type`, `x-hyprial-catalog-digest` (the **pinned** digest), `x-hyprial-request-id` (browser's or the host id), `authorization: Bearer <upstream>`; cookies and browser identity headers are never forwarded | `upstream.ts:299-314` |
| Relay | upstream status and body bytes pass through; response headers are rebuilt: content-type, no-store, nosniff, `x-hyprial-request-id`, `x-hyprial-catalog-digest`, `x-hyprial-dispatch` | `upstream.ts:333-350,411`; `proxy.ts:526-536` |
| Internal fault | 500 `HOST_INTERNAL_ERROR`, `not-dispatched` | `proxy.ts:510-523` |

**Routes**

| Route | Method | Requires | Behaviour | Errors (status code) | Evidence |
|---|---|---|---|---|---|
| `/daemon/session` (grant) | POST | Origin; body exactly `{"grant":"…"}` | check grant → probe upstream → consume grant → remove launch files → mint control + business session; `Set-Cookie` ×2; body: `requestId, driver, phase, view, generation, csrf, expiresAt (ms), principal{kind,owner,node,actor}, catalog{digest, declarationDigest, backendEpoch, httpContractVersion, catalogVersion, methodCount, methods[{name,callable,state,mutates}]}` | 400 `INVALID_REQUEST`; 403 `LAUNCH_GRANT_INVALID`; 409 `LAUNCH_GRANT_USED`; 401 `LAUNCH_GRANT_EXPIRED`; 429 `SESSION_CAPACITY`; probe errors below | `proxy.ts:151-224` |
| `/daemon/session` (refresh) | POST | Origin; body `{}`; control cookie; `X-Hyprial-View` | probe upstream → revoke this control's business sessions → new session with **same view**, new generation and CSRF | 400; 401 `SESSION_REQUIRED` | `proxy.ts:225-250`; `session.ts:160-178` |
| `/daemon/session/status` | POST | Origin; body `{}`; control cookie + view | `{current: bool}` — is the presented session cookie + generation the live one for this control | 400; 401 `SESSION_REQUIRED`; 405 | `proxy.ts:387-396`; `session.ts:185-189` |
| `/daemon/session/logout` | POST | Origin; session cookie + view + generation + CSRF | revoke session and control; expire both cookies; `{revoked:true}` | 401; 403 `GENERATION_REJECTED` / `CSRF_REJECTED`; 405 | `proxy.ts:287-306` |
| `/daemon/readyz` | GET | session cookie + view + generation; optional digest pin | daemon: relay upstream `/readyz`; mock: synthesised `{ready, transport, driver, backend{available,epoch}, catalog{…}}` | 401; 403; 409 `CATALOG_MISMATCH`; 405 | `proxy.ts:401-433` |
| `/daemon/request` | GET | same | daemon: relay upstream catalog (query preserved); mock: synthesised catalog (`generatedFrom: contract-mock`) from the session pin | same | `proxy.ts:308-335,434-459` |
| `/daemon/request/<method>` | POST | same; CSRF iff the pinned catalog says `mutates` | name must match `^[a-z][A-Za-z0-9]*(?:[._-][A-Za-z0-9]+)*$`, exist in the **session's pinned catalog** and be `callable`; body relayed as bytes; query preserved; envelope checked (`schemaVersion, ok, requestId, dispatch, method`) | 404 `METHOD_NOT_FOUND`; 409 `METHOD_NOT_CALLABLE`; 409 `CATALOG_MISMATCH`; 403 `CSRF_REJECTED`; 405 `HTTP_METHOD_NOT_ALLOWED`; 502 `UPSTREAM_UNAVAILABLE` (write → `unknown`); 502 `UPSTREAM_INVALID_RESPONSE`; 409 `GUI_SESSION_CHANGED` | `proxy.ts:460-509`; `upstream.ts:316-331,352-412` |
| other `/daemon/…` | any | — | 404 `METHOD_NOT_FOUND`; an unparseable target → 400 `INVALID_REQUEST` | — | `proxy.ts:87,349-360,374-382` |

**Upstream probe** (at every mint and refresh; `upstream.ts:173-293`)

| Driver | Calls | Accept when | Refusals |
|---|---|---|---|
| daemon | `GET /readyz`, `GET /daemon/request` (bearer) | 200; `ok/ready true`, `transport: ready`; `backend.available`; `catalog.fresh === true`; node, epoch, both digests present; `notReady` empty; owner/node equal the trusted principal (or owner `null` for bootstrap); catalog `schemaVersion 1`, `generatedFrom: daemon.catalog`; catalog backend == ready backend; catalog principal == trusted principal; digests agree (body, header, ready) | 503 `HTTP_NOT_READY` (with `data.upstreamCode`); 502 `UPSTREAM_AUTH_REJECTED` (upstream 401/403); 502 `PRINCIPAL_MISMATCH`; 502 `UPSTREAM_CONTRACT_MISMATCH`; 502 `CATALOG_MISMATCH`; 503 `UPSTREAM_UNAVAILABLE`; 502 `UPSTREAM_INVALID_RESPONSE` |
| contract-mock | `GET /healthz` (open), `GET /mock/contract` (bearer) | `ok`; `mock === true`; `contractDigest`; `methods` object with `scenarios` arrays. `mutates` comes from **the GUI build's generated `methodMetadata`**, default `true` (`upstream.ts:14,280-281`); every method `callable`, `state: available`; `phase: ready` | same family |

**Launch files** (`index.ts:16-75`; `session.ts:117-145`): with a publication path the grant TTL starts only when `launch.ready` exists (polled every 100 ms); without one it starts at construction. At expiry, on grant consumption and at close the host unlinks `launch.url` and `launch.ready` (regular files only, never through a symlink).

**Static serving** is Vite's, not the host's: `vite preview` over `dist` (normal) or the dev server (dev), `base: './'`, `/__gui/healthz` from a second plugin (`gui/vite.config.ts:19-60,84,106-110`). Vite applies no Host/Origin rule of ours to these paths.

**Finding.** `SESSION_CAPACITY` cannot be reached over HTTP: one host has one grant, so one control session, and refresh revokes before it issues. It is tested only by calling `SessionStore` directly (`host.test.ts:471-476`).

### A.4 What the GUI client depends on

| Dependency | Evidence |
|---|---|
| `#grant=` fragment, removed with `replaceState` before any request | `gui/src/app/backend/browser-session.ts:97-113` |
| `POST /daemon/session` with `{grant}` or `{}` + `X-Hyprial-View`; `credentials: include`; 10 s budget | `browser-session.ts:215-243` |
| Session body fields listed in A.3; `expiresAt` a safe integer; principal all-null in `bootstrap`, all non-empty in `ready`; `methods.length === methodCount`; unique names. **Unknown fields are ignored** | `browser-session.ts:120-189` |
| View proof kept in `sessionStorage['hyprial.gui.session-view']`; removed on 401/403 of a refresh; a refresh must return the same view | `browser-session.ts:19,224-266` |
| `POST /daemon/session/status` with view + generation | `browser-session.ts:198-213` |
| `POST /daemon/request/<method>`, `GET /daemon/request`; headers `X-Hyprial-View`, `-Generation`, `-Csrf` (only when **the build's** `methodMetadata` says the method mutates; unknown → true), `-Catalog-Digest` (the digest from the session, not a build constant) | `browser-session.ts:23-26,267-278`; `gui/src/client/transport.ts:161,184-204,443,632-639` |
| Response headers `x-hyprial-catalog-digest`, `x-hyprial-request-id`; envelope `schemaVersion`, `dispatch`, `code`; 401/403/408 handling | `transport.ts:229-345,468-504` |
| Not used by product code: `/daemon/readyz`, `/daemon/session/logout`, `/__gui/healthz` | `git grep` over `gui/src` |

### A.5 Tests of the Node host

| File | Lines | Kind | Fate |
|---|---|---|---|
| `host.test.ts` | 827 | 17 cases; real HTTP against the middleware, fake upstream servers. In-process seams: `now` (2 cases), injected `fetch` (1), `onLaunchGrantConsumed` (1), direct `SessionStore`/`HostHttpError` (1 block) | **conformance suite** (§C.4) |
| `contract-mock-loopback.test.ts` | 296 | 2 cases; real mock, generated client, 4 adapters | conformance |
| `real-daemon-loopback.test.ts` | 514 | 3 cases; isolated real daemon + `request_http` | conformance |
| `bootstrap-session.test.ts` | 366 | 5 blocks: config parsing, `describeUpstream`, `SessionStore` (server side, lines 90-205, 257-366) and `connectBrowserBackend` (client side, 207-256) | server blocks → pytest; client block stays |
| `handoff-plugin.test.ts` | 183 | 6 cases on the Vite plugin with fake timers | → pytest |
| `browser-session.test.ts`, `app-composition.test.tsx` | 356, 438 | client only; import nothing from `gui/server` | stay |
| `browser-acceptance.mjs` | 3,159 | 16 cases through the real `hyprial gui` / `hyprial gui dev`; host-agnostic | stays; exercises the Python host after PR 3 |

Other consumers of the Node host through `gui/scripts/serve.mjs`: `tests/support/isolated-daemon-host.mjs`, `tests/first-run-browser-contract.mjs:704-755`, `tests/modules/dev02/j2-browser-contract.mjs:545-563`, `tests/modules/dev01/j10-browser-contract.mjs:407-420` (production mode with a driver), `tests/serve.test.mjs:138-200`.

### A.6 Parity table: Node host → Python today → PR 2

"Python today" is the wheel static server (S) and `request_http` (R).

| Surface | Node host | Python today | Status | PR 2 |
|---|---|---|---|---|
| `POST /daemon/session` (grant, refresh) | yes | none | **missing** | new host |
| `POST /daemon/session/status` | yes | none | **missing** | new host |
| `POST /daemon/session/logout` | yes | none | **missing** | new host |
| `GET /daemon/readyz` | session-gated relay | R: `GET /readyz`, bearer | **differs** (auth, path) | host relays to R |
| `GET /daemon/request` | session-gated relay / mock synthesis | R: same path, bearer | **differs** (auth) | host relays to R |
| `POST /daemon/request/<m>` | session + CSRF + pinned catalog, query preserved | R: bearer, per-request catalog refresh, query → 400 | **differs** | host relays to R; R's 400 for a query is relayed unchanged |
| Cookies, view, generation, CSRF | yes | none | **missing** | new host |
| Host check | loopback name + exact listener port, `/daemon/*` only | R: name allowlist, port ignored (`app.py:397-402`); S: none | **differs** | new host: same rule, **all paths** (stricter; invisible to the GUI) |
| Origin / `Sec-Fetch-Site` | exact; required on non-GET | R: allowlist, optional | **differs** | new host: Node rule |
| Host error codes | `ORIGIN_REQUIRED`, `ORIGIN_REJECTED`, `HTTP_REQUEST_TOO_LARGE`, `HOST_INTERNAL_ERROR`, `SESSION_REQUIRED`, `GENERATION_REJECTED`, `CSRF_REJECTED`, `LAUNCH_GRANT_*`, `METHOD_NOT_CALLABLE`, `GUI_SESSION_CHANGED`, `GUI_AUTHORITY_UNAVAILABLE`, `UPSTREAM_*`, `PRINCIPAL_MISMATCH`, `SESSION_CAPACITY` | R uses `HTTP_ORIGIN_DENIED`, `HTTP_BODY_TOO_LARGE`, `HTTP_INTERNAL`, `HTTP_UNAUTHORIZED` | **differs** | new host emits the Node codes; R's codes still arrive inside relayed envelopes |
| Request id | `gui-<uuid>` | R: `http-<hex>` | differs | new host: `gui-` |
| Catalog digest header | pinned digest on relay | R: sets its own | exists | host rebuilds headers as Node does |
| Upstream selection | daemon or mock by env | R is the daemon upstream; mock is separate | exists | unchanged; host takes the same env |
| Authority rotation | `frontend-authority.json` | launcher writes it (`rebind.py`) | writer exists, reader **missing** | new host |
| Launch files | plugin unlinks at expiry/consume/close | launcher writes, cleans at stop (`process.py:307-325`) | reader **missing** | new host |
| Static assets | Vite preview / dev server | S: `http.server` + manifest check | **differs** | new host serves `_gui` (manifest-verified) or `gui/dist` |
| SPA fallback | Vite | S: yes | exists | new host |
| `/__gui/healthz` | `{ok, service, mode, dataMode: hosted, driver, backendConfigured, session}` | S: `{ok, service, dataMode: fixture, assets, assetsVerified}` | **differs** | new host: Node fields plus `assets`, `assetsVerified` |
| `/api/gui/v1/*` → 501 | absent | S: yes | Python-only | **deleted** with S (design §4.2 rule 1) |
| Ready line | `HYPRIAL_GUI_READY {…}` on stdout | S: none (launcher polls healthz) | **missing** | new host prints the same line |
| Swagger `/docs` | none | R: env-gated, bearer | n/a | untouched (PR 4b) |

### A.7 Existing contracts

| Path | Content | Says about sessions |
|---|---|---|
| `contract/daemon-request/openapi.json` | generated projection of 153 declared methods, 124 HTTP paths, `x-hyprial-examples`, `x-hyprial-methods[].mutates` | nothing |
| `contract/daemon-request/http-envelope.schema.json` | success/error envelope; closed status-by-code map for `request_http` codes | nothing; the host-only codes of A.6 are **not** in the map |
| `contract/http-request/http-contract.v1.json` (+ `validate.py`, run by `run-all.sh`) | frozen corpus for the bearer entry: headers, endpoints, dispatch rules | nothing |
| `contract/native-host/native-host.v1.json` | launch/lifecycle contract of the deleted shell; still mirrored by `request_http/catalog.py:40-43` | launch nonce, `server.json`; no browser session |
| `contract/gui-live/README.md` | retired | — |
| `gui/docs/native-host-session-contract.md` | referenced by the design (§7.3), **does not exist** | — |

The browser session contract is written nowhere. It exists only as the Node code and its tests.

### A.8 Layering and limits that bind the design

| Rule | Effect |
|---|---|
| `kernel → identity → daemon → biz → shell`; cross-domain imports only through public faces (`scripts/ci/check_module_boundaries.py`) | The host is shell code. It imports `hyprial.kernel` (private-file checks) and nothing from `daemon`. Mock `mutates` fallback uses the public `hyprial.daemon.daemon_catalog` already used by `request_http/app.py:45,588-596` |
| ≤ 7 direct files per code directory; ≤ 1000 lines per code file (`check_structural_limits.py:41-42`) | `shell/impl/gui/` has 7 files, `request_http/` 7, `gui/backend/` 6, `gui/install/` 6. The host gets its own package with a sub-package |
| CLI output only through `shell/impl/cli/output` | the host child writes one protocol line to stdout and JSON errors to stderr, like `request_http/__init__.py:168-172`; the CLI command is unchanged |
| No inline comments in new lines; decisions in the file-header block | every new file starts with a decision block |
| Supervision gate (`tests/tooling/architecture/boundary/test_supervision_boundary_gate.py`, manifest `tests/supervision_exemptions.json`, shrink-only) | new product symbols must not be named like start/ready timeouts, retries or backoff. Reuse the existing launcher constants; name host values by what they bound (`upstream_request_seconds`, `publication_poll_seconds`) and run the gate |
| Post-install import surface (`tests/shell/cli/installation/restart/post_install/post_install_surface.json`) | the host package must be imported only inside the launch path, never at CLI registration |
| GUI request-path gate (`scripts/ci/gui-request-paths-scanner.json`, ledger shrink-only, `REVIEWED_SCANNER_SHA256`) | conformance tests keep their paths so existing ledger rows keep matching; a spawned Python host is not a JS finding |

### A.9 Security properties to keep

| Property | Today | Kept by |
|---|---|---|
| Loopback bind | Node: `serve.mjs:102-106`; R: `config.py:412-413`; wheel static: **not enforced** | host refuses a non-loopback bind; launcher refuses a non-loopback `--ip` (OD-5) |
| Host/Origin allowlist | A.3 | same rule, extended to static paths |
| Browser cannot assert a principal | principal comes from launcher env / authority file; projected from session phase (`proxy.ts:131-149`); no request field is read | host config is the only source; no header, cookie, body or query field is consulted |
| Driver and principal named by the launcher | `admission.py:90-121` | unchanged; wheel now goes through the same admission |
| No second durable authority in the GUI | sessions are in memory; a host restart ends them; durable state stays in the daemon | `SessionStore` is in-memory only; the host writes no file except unlinking launch files |
| Upstream bearer never reaches the browser | `upstream.ts:6-7`; asserted in `host.test.ts:358-359,694-695` | conformance cases keep the assertion |
| Grant is one-time and never in argv, a query or a log | `source.py:11-13`; `browser-session.ts:3-5` | host takes the grant from env; launcher redaction set unchanged (`lifecycle/session.py:530`) |
| Proxy environment | Node `fetch` ignores `HTTP(S)_PROXY`; **httpx honours it by default** | `httpx.AsyncClient(trust_env=False, follow_redirects=False)` — a red-first test |

---

## B. Session contract (the document PR 2 lands first)

### B.1 Where

| File | Content | Why here |
|---|---|---|
| `contract/gui-session/README.md` | normative prose, ≤ 300 lines | `contract/` is where cross-implementation contracts live and what `Contract-Impact` checks; the two parties are a JS client and a Python host that may be different SHAs |
| `contract/gui-session/gui-session.v1.json` | machine corpus: version, routes, header names, cookie names and attributes, limits, closed code → status → dispatch table, session body schema (JSON Schema 2020-12) | one source for the vitest conformance suite, the pytest unit tests and the client test |
| `docs/design/gui-single-launch-2026-11-14.md` §7.3 | one-line pointer replacing the missing `gui/docs/native-host-session-contract.md` | closes the open item |

`contract/run-all.sh` gets no new step in PR 2; the corpus is enforced by tests on both sides (OD-9).

### B.2 Outline

| § | Section | Must state |
|---|---|---|
| 1 | Parties and trust | launcher (trusted, names driver and principal), host, browser view (untrusted), upstream (bearer). The shell is a browser and nothing more (design §4.1 rule 3) |
| 2 | Origin | one loopback HTTP origin; `Host` and `Origin` rules of A.3; applies to every path |
| 3 | Launch | grant in the URL fragment only; one use; TTL; publication marker; the 0600 handoff file as the only other carrier; what `--json` may and may not contain |
| 4 | Session model | control cookie, business cookie, view proof, generation, CSRF; what rotates on refresh; expiry; logout; capacity |
| 5 | Routes | the eight rows of A.3 with request and response shapes |
| 6 | Headers | request: `X-Hyprial-View`, `-Generation`, `-Csrf`, `-Catalog-Digest`, `-Request-Id`, `-Session-Contract`; response: `X-Hyprial-Request-Id`, `-Catalog-Digest`, `-Dispatch`; names are case-insensitive |
| 7 | Envelope and errors | host envelope; closed table of host codes; relayed upstream envelopes are opaque bytes governed by `contract/http-request` |
| 8 | Dispatch truth | `not-dispatched` / `dispatched` / `unknown`; the host never retries; a write whose response was lost is `unknown` |
| 9 | Principal projection | `anonymous-bootstrap` with explicit nulls, or `serving`; never from a request |
| 10 | Catalog pin | the session pins digest and method list; `mutates` and `callable` in that list are authoritative for CSRF and admission |
| 11 | Authority change | `GUI_SESSION_CHANGED`, `GUI_AUTHORITY_UNAVAILABLE`; the view refreshes, never replays |
| 12 | Static resources | `/`, hashed `assets/*`, SPA fallback, `/__gui/healthz` (launcher probe, no business data); nothing else outside `/daemon/*` |
| 13 | Versioning | below |
| 14 | Not in the contract | upstream endpoint, bearer, runtime files, ready line (launcher ↔ host, same wheel, same SHA) |

### B.3 Versioning without assuming one SHA

| Rule | Text |
|---|---|
| Version | integer `sessionContractVersion`, starts at 1 |
| Host declares | session response gains `sessionContract: {version: 1, min: 1, max: 1}`. Additive: the current client ignores unknown fields (`browser-session.ts:127-189`) |
| Client declares | request header `X-Hyprial-Session-Contract: <n>` on `/daemon/session`. Absent means 1, so GUI builds older than the contract keep working |
| Refusal | unsupported version → 409 `SESSION_CONTRACT_UNSUPPORTED`, `not-dispatched`, `data: {min, max}`. The client shows the bootstrap error page; it does not guess |
| Compatible change | adding a response field, an error code inside an existing status class, a route. Both sides ignore what they do not know |
| Breaking change | removing or renaming a field, header or cookie; changing a status or dispatch value; changing who supplies a value. Requires a new version; the host serves `[min, max]` |
| Methods are not part of it | which methods exist is the catalog's job. The client takes the digest, the method list, `callable` and `mutates` from the session, not from its build |

One client change follows from the last row (PR 2a): `methodMutates` reads the session's method list first and falls back to the generated metadata, then to `true` (`browser-session.ts:23-26`). Today a build whose metadata says "read" for a method the backend marks "write" omits CSRF and gets 403.

---

## C. Target design

### C.1 Decision: where the session layer runs

| Option | Verdict |
|---|---|
| **A. Separate ASGI app, own listener, owned child process `python -m hyprial.shell.impl.gui.host`; relays to `request_http` (or the mock) over loopback HTTP with the bearer** | **chosen** |
| B. Mount session routes inside the `request_http` FastAPI app | rejected |

Reasons for A:
1. `request_http` changes port and bearer when a bootstrap home is initialised or logged out (`bootstrap/gui.py:25-29,58-131`). The browser origin must not move. Today the Node host absorbs this through `frontend-authority.json`; a separate Python host absorbs it the same way with no launcher change.
2. `request_http/app.py` is at 985 of 1000 lines and its directory is at 7 of 7 files.
3. The mock driver needs a host in front of a bearer upstream that is not `request_http`.
4. `request_http` refuses query strings and unknown paths and closes every connection (`app.py:299-301,346-382`); a browser-facing asset server needs the opposite.
5. It is the Node host's position, so the launcher, the runtime files and the redaction rules stay as they are. M2–M5 ran in exactly this shape.

This is the design's own picture (§3: `/daemon/* → session → request-http → daemon`). The wheel ships both parts; the user sees one origin.

Framework: plain ASGI on Uvicorn with `httptools`, upstream calls with `httpx`. FastAPI adds nothing here and would add redirect and 404 behaviour to undo.

### C.2 Modules

```
src/hyprial/shell/impl/gui/host/
  __init__.py     public face of the package: HostConfig, build_app, serve; lazy, import-light
  __main__.py     `python -m hyprial.shell.impl.gui.host serve --host --port [--root DIR | --live-upstream URL]`
  config.py       HostConfig; read_config(env) (mirror of config.ts); read_authority (mirror of authority.ts)
  envelope.py     HostFailure, success/failure encoders, bounded body readers, limits, request id
  session.py      SessionStore, cookie parse/format, same_origin()
  upstream.py     describe_upstream (daemon / mock probe), relay(); one httpx.AsyncClient(trust_env=False)
  app.py          ASGI entry: routing of /daemon/*, principal projection, assertCurrent, lifespan
  web/
    __init__.py
    assets.py     asset provider over a verified directory: index, hashed assets, SPA fallback, healthz
    launch.py     publication poll, grant expiry timer, unlink of launch.url / launch.ready
    live.py       PR 3: reverse proxy to the Vite dev server (HTTP + WebSocket)
tests/shell/gui/host/            pytest units (≤ 7 files; split into web/ when needed)
contract/gui-session/            README.md, gui-session.v1.json
```

Line budget from the prototype: config 170, envelope 120, session 230, upstream 300, app 330, assets 110, launch 70, live 90, `__main__` 70 → about 1,500 product lines, no file near 1000.

| Public face | Consumers |
|---|---|
| `python -m hyprial.shell.impl.gui.host serve …` + environment of A.3 + ready line | the launcher (`gui/backend`), the vitest conformance driver, 1e-3's browser helper |
| `hyprial.shell.impl.gui.host.build_app(config, provider)` | pytest |
| nothing added to `shell/interface.py` | — |

### C.3 Configuration and lifecycle

| Item | Design |
|---|---|
| Config source | the same `HYPRIAL_GUI_*` variables the Node host reads (A.3). Secrets stay in the child environment, never in argv |
| New inputs | argv `--root <dir>` (static provider) or, in PR 3, `--live-upstream http://127.0.0.1:<port>`; exactly one. `--host` must be loopback |
| Bind | the child creates, binds and listens on the socket, then prints `HYPRIAL_GUI_READY {service, mode, dataMode:"hosted", host, port, url, driver, backendConfigured:true, session:"launch-required"}` and hands the socket to Uvicorn. (The prototype's first run failed with `ECONNREFUSED` until `listen()` preceded the line **[M]**) |
| Port 0 | supported by the child; the CLI keeps refusing it in PR 2/3 (PR 6 lifts it) |
| Uvicorn | `server_header=False`, `date_header=False`, `access_log=False`, `lifespan="on"`, `http="httptools"`, graceful timeout from the existing launcher stop budget |
| Stop | SIGTERM/SIGINT → Uvicorn drains → launch files unlinked → `HYPRIAL_GUI_STOPPED {}` → exit 0. The launcher's process-group termination is unchanged (`lifecycle/process.py:472-513`) |
| Startup refusal | invalid config → one JSON line on stderr `{ok:false, code, message}`, exit 2; bind failure exit 4 — the `request_http` convention (`request_http/__init__.py:42-50,168-172`) |
| Asset provider | wheel: `load_assets(root, verify=True)` at start (`install/resources.py:68-75`), then serve only files named in the manifest. Source: `gui/dist` unverified, reported as `assets: "explicit-unverified"` as today (`install/server.py:125-131`) |
| Asset headers | `X-Content-Type-Options: nosniff`; `Cache-Control: no-store` for `index.html` and the fallback; `public, max-age=31536000, immutable` for `assets/*`; types from `mimetypes` with charset for text |
| Path safety | raw path; reject control characters and dot segments (400); resolve under the root; a missing path with a file extension → 404, without one → `index.html` |
| Mock `mutates` | `/mock/contract.methods[name].mutates` when present (1e-2's JS mock has the value in its table, `1e-2.md:118`), else the local `daemon_catalog()` declaration, else `true` |
| Authority | same file, same checks, same revocation as `authority.ts`; written by the unchanged `rebind.py` |
| Clock | wall-clock milliseconds; `expiresAt` stays epoch ms |

Launcher changes are confined to `gui/backend/config.py` (plan), `gui/backend/source.py` (child environment and ready-line names), `gui/backend/lifecycle/{session,process}.py` (which child is spawned), `gui/install/{launch,report}.py`. `source.py` is also edited by PR 1e-2 (`mock_invocation`, `_contract_root`; `1e-2.md:168`): different functions, same file.

**Shared code with the CLI-convergence work.** This plan does not touch `request_http/**`, `http_contract/**`, `kernel` IPC or any CLI command other than the `gui` command's help text. If that work changes `request_http`'s catalog or ready payloads, the probe in `upstream.py` and the corpus must follow; the real-daemon conformance case catches it.

### C.4 Conformance: one black-box suite, two hosts

| Element | Design |
|---|---|
| Seam | `gui/tests/daemon-request-host/host-driver.ts` exports `startHost(config, knobs) → {origin, close, advance(ms)}` and reads `HYPRIAL_HOST_IMPL` (`node` default, `python`) |
| Node driver | today's in-process code: `createDaemonRequestHost` behind `createServer`; `advance` moves an injected clock |
| Python driver | `spawn('uv', ['run','--no-sync','python','-m','hyprial.shell.impl.gui.host','serve','--port','0'], {env})`, waits for the ready line; `advance` sleeps (the two TTL cases use 1 000 ms) |
| Rewrites needed in `host.test.ts` (shown by `make-python-driver-test.mjs`) | `{ now }` → `advance()`; the injected `fetch` case → an upstream that destroys the socket (`captured.length === 4` replaces the call counter); `onLaunchGrantConsumed` → assert that a real `launch.url` is gone; the `SessionStore` capacity block → pytest |
| Loopback suites | `startHost()` in both files goes through the driver (M3) |
| Runs | `npm test` runs the Node driver (as today). `npm run test:host-conformance:python` runs the same three files with `HYPRIAL_HOST_IMPL=python`. `gate-gui.yml` gains that command; it already syncs uv for these tests (`gate-gui.yml:162-186`) |
| Unit level | Python: `tests/shell/gui/host/**` ports `handoff-plugin.test.ts` and the server blocks of `bootstrap-session.test.ts`, plus corpus equality. Node keeps its own until PR 3 |
| CLI level | `browser-acceptance.mjs` runs unchanged before and after PR 3 |
| After PR 3 | the Node driver is deleted; the suite stays as the Python host's HTTP test, same paths, same ledger rows |

A recorded-comparison approach (replay one traffic capture against both) was rejected: tokens and request ids are random, so a diff needs normalisation rules that are themselves a second specification.

### C.5 Composition with PR 1e-2

| Point | Statement |
|---|---|
| Role | the mock is an upstream bearer endpoint (`/healthz`, `/mock/contract`, `/daemon/request/<m>`). It has no session. The session comes from the host in both drivers |
| Protocol | the host probes exactly what `probeMock` probes today; 1e-2 keeps the stderr lines and the two metadata endpoints byte-compatible (`1e-2.md:138`) |
| Launcher | 1e-2 changes how the mock child is spawned; PR 2c/3 change how the host child is spawned. Both edit `source.py`; the second to merge rebases |
| `mutates` | ask 1e-2 to include `mutates` per method in `/mock/contract` (one field; it already has the data). The host works without it through the fallback |
| Wheel | after 1e-2 the mock needs Node and a source tree. `hyprial gui` from a wheel with `contract-mock` → `GUI_MOCK_UNAVAILABLE` (existing code, `source.py:356-361`). The wheel supports the `daemon` driver |
| Tests | `contract-mock-loopback.test.ts` is edited by both (1e-2: `startContractMock`; here: `startHost`). Disjoint functions |
| Order | PR 2a and 2b do not depend on 1e-2. PR 2c and PR 3 can land before or after it |

---

## D. Ordered steps

Every step: write the named test first, see it fail for the stated reason, implement, run the commands. Environment for all commands: `export PATH=<node24-tools>/node_modules/.bin:$PATH`; Python only through `uv run`. Commit messages are conventional commits.

### D.0 Split

| PR | Content | Product Python | Depends on | Parallel with |
|---|---|---|---|---|
| **2a** | contract document + corpus; conformance seam (Node driver only); client reads `mutates` from the session and sends the contract header | none | 1a (merged) | 2b |
| **2b** | the Python host package, its pytest units, `HYPRIAL_HOST_IMPL=python` conformance run; not reachable from the CLI | yes | 2a's seam and corpus (can be developed against 2a's branch) | 2a review |
| **2c** | wheel `hyprial gui` goes through admission, `request_http` and the Python host; static 501 server deleted | yes | 2b | 1e-2, 4b prep |
| **3** | source `hyprial gui` and `hyprial gui dev` use the Python host; Node host deleted; tests moved | yes | 2c | — |

Splitting is recommended: 2a is reviewable without Python, 2b adds code no user can reach, 2c is the first behaviour change. If the owner wants one PR 2, the same commits apply in the same order.

### D.1 PR 2a — `docs(contract): GUI session contract v1 and host conformance seam`

| Step | Red test first | Change | Run |
|---|---|---|---|
| 2a-1 | `gui/tests/daemon-request-host/session-contract.test.ts`: loads `contract/gui-session/gui-session.v1.json`; fails because the file is absent | add `README.md` and the corpus (B.2, B.3); values copied from A.3 | `npx vitest run tests/daemon-request-host/session-contract.test.ts` |
| 2a-2 | same file: assert the Node host's constants equal the corpus — cookie names (`session.ts:69-70`), limits (`envelope.ts:33-35`), route paths (`proxy.ts:41-44,80`), and every `code`/`status`/`dispatch` literal in `proxy.ts`, `session.ts`, `upstream.ts`, `envelope.ts`, `authority.ts` appears in the corpus table (source-text scan) | fix the corpus until green; no host change | same |
| 2a-3 | `browser-session.test.ts`: a session whose method list says `agent.list` mutates makes the transport send `X-Hyprial-Csrf` for it; fails today | `browser-session.ts`: `methodMutates` consults the session list first | `npx vitest run tests/daemon-request-host/browser-session.test.ts` |
| 2a-4 | `browser-session.test.ts`: handshake carries `x-hyprial-session-contract: 1`; a response with `sessionContract` is accepted | add the header constant and send it; no parsing change | same |
| 2a-5 | none (pure refactor; M1's 74 tests are the guard) | add `host-driver.ts` with the Node driver; `host.test.ts`, both loopback files use it; apply the four rewrites of C.4 except the capacity block, which stays until 2b | `npx vitest run --maxWorkers=1 tests/daemon-request-host` |
| 2a-6 | — | design doc §7.3 pointer; `contract/README.md` row | `./contract/run-all.sh --isolated-only` |

Gate commands: `npm --prefix gui run typecheck`; `npm --prefix gui test`; `node --test gui/tests/architecture/daemon-request.node.mjs`; `node scripts/ci/check_gui_request_architecture.mjs --base-ref <base sha> --base-branch dev --json`; `node --test scripts/ci/check_gui_request_paths.selftest.mjs`; `node scripts/ci/check_gui_request_paths.mjs --base-ref <base sha> --base-branch dev --json` (the workflow's invocation, `gate-gui.yml:225-239`). No product Python changes → the full pytest run is not required; say so in the PR body.

### D.2 PR 2b — `feat(gui): Python session host for /daemon/* and built assets`

| Step | Red test first (`tests/shell/gui/host/…`) | Change | Run |
|---|---|---|---|
| 2b-1 | `test_config.py`: ports `handoff-plugin.test.ts:44-75` and `bootstrap-session.test.ts:90-117` (absent principal = bootstrap, blank = error, path basenames, ranges, loopback endpoint); import fails | `host/__init__.py`, `config.py` | `uv run pytest -q tests/shell/gui/host/test_config.py` |
| 2b-2 | `test_session.py`: mint/refresh/revoke/expiry/capacity (ports `bootstrap-session.test.ts:257-307` and `host.test.ts:466-476`), cookie format and strict decoding, `same_origin` table (Host, port, Origin, `Sec-Fetch-Site`) | `envelope.py`, `session.py` | `uv run pytest -q tests/shell/gui/host/test_session.py` |
| 2b-3 | `test_upstream.py`: probe matrix with `httpx.MockTransport` (ports `bootstrap-session.test.ts:119-205,309-366`); **`HTTPS_PROXY`/`HTTP_PROXY` set in the environment must not change the target** (fails if `trust_env` is left at its default); relay header set; 4 MiB bound; mock `mutates` precedence | `upstream.py` | `uv run pytest -q tests/shell/gui/host/test_upstream.py` |
| 2b-4 | `test_app.py`: ASGI-level cases for each route of A.3 through `httpx.ASGITransport`, including `GUI_SESSION_CHANGED` and authority-file faults (mode 0644 → 503) | `app.py`; authority reader in `config.py` | `uv run pytest -q tests/shell/gui/host/test_app.py` |
| 2b-5 | `web/test_launch.py`: ports the five timer cases of `handoff-plugin.test.ts:77-183` with a fake clock | `web/launch.py`, lifespan wiring | `uv run pytest -q tests/shell/gui/host/web` |
| 2b-6 | `web/test_assets.py`: manifest-verified root served; tampered file refused at start; SPA fallback; extension 404; dot-segment 400; cache headers; healthz fields; **Host check on `/`** | `web/assets.py` | same |
| 2b-7 | `test_process.py`: spawn `-m hyprial.shell.impl.gui.host serve --port 0`, read the ready line, connect immediately, SIGTERM → exit 0, launch files gone, `STOPPED` line; non-loopback `--host` → exit 2 | `__main__.py` | `uv run pytest -q tests/shell/gui/host/test_process.py` |
| 2b-8 | `host-driver.ts` Python driver; run the suite — fails wherever behaviour differs | fix the host, never the suite | `HYPRIAL_HOST_IMPL=python npx vitest run --maxWorkers=1 tests/daemon-request-host/host.test.ts tests/daemon-request-host/contract-mock-loopback.test.ts tests/daemon-request-host/real-daemon-loopback.test.ts` → expect 22 cases pass |
| 2b-9 | `test_contract_corpus.py`: host constants and the code table equal `gui-session.v1.json`; session response validates against the corpus schema and carries `sessionContract` | add the field; refuse an unsupported `X-Hyprial-Session-Contract` | `uv run pytest -q tests/shell/gui/host` |
| 2b-10 | — | `package.json` script `test:host-conformance:python`; `gate-gui.yml` step after "Run independent GUI tests"; `tests/shell/gui/host/` added to the lifecycle step list (`gate-gui.yml:305-318`) | — |

Gate commands (§E.1, all of them, including the full pytest run).

Reference for the implementer: `docs/notes/gui-single-launch-prototypes-2026-11-15/host-2-3-prototype/host_proto.py` passes 2b-8 today. It is a single file with dict-shaped records; the product code follows C.2 with dataclasses and file-header decision blocks, and must not be copied verbatim.

### D.3 PR 2c — `feat(gui): installed hyprial gui serves the real backend through the Python host`

| Step | Red test first | Change | Run |
|---|---|---|---|
| 2c-1 | `tests/shell/gui/installed/test_installed_admission.py`: with built-in assets and `HYPRIAL_GUI_BACKEND_DRIVER=daemon`, `build_plan` returns a plan with `driver == "daemon"`, an admission and `frontend_argv` naming `hyprial.shell.impl.gui.host` with `--root <assets>`; today it raises `GUI_INSTALLED_BACKEND_UNSUPPORTED` | `backend/config.py:170-202`: the static branch runs admission and returns a source-shaped plan with `installed_assets`; `build_argv=None`; child cwd is the asset root | `uv run pytest -q tests/shell/gui/installed` |
| 2c-2 | same file: driver unset → `daemon` (OD-2); `contract-mock` from a wheel → `GUI_MOCK_UNAVAILABLE`; no `fcntl` → `GUI_RUNTIME_UNAVAILABLE` (replaces `test_installed_static_without_driver_does_not_require_source_platform`) | `admission.py` default for the installed target only; `platform_support` preflight on the installed path | same |
| 2c-3 | `tests/shell/gui/backend/test_source_session.py`: an installed plan starts `request_http`, then the host child with the same environment keys as the Node child, waits for the ready line, writes `launch.url` when `--no-open`, and never opens the bare URL | `lifecycle/session.py`: `start_session` routes every plan with a driver through `_start_source_session`; the driver-less branch (386-424) is deleted. `source.py`: `node_environment` → `host_environment`, `parse_node_ready` → `parse_host_ready` (mode `production`), log name `host.log`; old names kept as aliases until PR 3 | `uv run pytest -q tests/shell/gui/backend tests/shell/gui/lifecycle` |
| 2c-4 | `tests/shell/gui/installed/test_installed_gui.py`: the four `test_static_server_*` cases are replaced by one that starts the host on the built-in assets and asserts healthz `dataMode: hosted`, `assetsVerified: true`, and **404 with the host envelope for `/api/gui/v1/x`** | delete `install/server.py`; `install/report.py` drops `NOT_IMPLEMENTED_TEXT` and describes the hosted backend; `__init__` exports | `uv run pytest -q tests/shell/gui/installed` |
| 2c-5 | `tests/integration/request_http/onboarding/test_gui_admission.py` gains one live case: isolated home + daemon, installed-shaped plan, session opened with the handoff grant, `agent.list` returns 200 through the host | fixes only | `uv run pytest -q tests/integration/request_http/onboarding` |
| 2c-6 | — | docs: `AGENTS.md:33-44`, `docs/guides/gui-dev-mode.md`, `src/hyprial/skills/hyprial-ops/SKILL.md:210-225`, `gui/docs/dev-mode-user-guide.zh-CN.md:20`, `README.md`/`CONTRIBUTING.md` lines naming the 501 placeholder; regenerate `docs/reference/cli-command-inventory.json` (help text); `tests/supervision_exemptions.json` reason text for `FRONTEND_READY_TIMEOUT_SECONDS`; add `docs/reference/known-problems.md` entry for Windows (OD-4) | repo doc gates in §E.1 |

Wheel acceptance (report PASS / FAIL / BLOCKED in the PR body): build a wheel, install it into a throwaway venv with an isolated `HYPRIAL_HOME`, run `hyprial gui --no-open --json --port <free>`, read `launch.url`, open it in Chromium, see the Start page and at least one 200 `/daemon/request/*`. If the wheel cannot be built on the machine (native engine, KP-027), record **BLOCKED** and keep 2c-5 as the evidence that exists.

### D.4 PR 3 — `refactor(gui): source hyprial gui uses the Python host; delete the Node host`

| Step | Red test first | Change | Run |
|---|---|---|---|
| 3-1 | `tests/shell/gui/lifecycle/test_gui_source_selection.py`: a source checkout, normal mode → `frontend_argv` is the Python host with `--root <gui>/dist`; `build_argv` is `npm run build`; a `package.json` without a `start` script is accepted | `backend/config.py:204-256` | `uv run pytest -q tests/shell/gui/lifecycle` |
| 3-2 | `test_gui_source_selection.py` + `test_source_session.py`: dev mode → a Vite child (`npm run dev -- --host 127.0.0.1 --port <ephemeral> --strict-port`, **no** `HYPRIAL_GUI_*` backend variables in its environment) and a Python host child with `--live-upstream http://127.0.0.1:<that port>` on the public port; ready order: Vite line, then host line | `GuiLaunchPlan` gains `assets_argv` (optional third child); `lifecycle/session.py` spawns and stops it; `GuiSession.stop` order: host, assets child, backend | same + `tests/shell/gui/backend` |
| 3-3 | `tests/shell/gui/host/web/test_live.py`: against a stub upstream — GET passthrough with streaming, `/daemon/*` never proxied, WebSocket frames both ways, upstream down → 502 host envelope, Host check applies | `web/live.py` (≈ 90 lines; shape proven by M5) | `uv run pytest -q tests/shell/gui/host/web` |
| 3-4 | `gui/tests/support/python-host.mjs` + `gui/tests/support/python-host.test.mjs`: `startPythonHost({driver, endpoint, token, principal, node, root | liveUpstream}) → {origin, launchUrl, close}`; fails until written | the helper (generalises the driver of C.4) | `npx vitest run tests/support/python-host.test.mjs` |
| 3-5 | run `verify:first-run-browser`, `verify:j2-browser`, `verify:j10-browser` after swapping their production-mode host for the helper — they fail until `isolated-daemon-host.mjs` stops setting Node host variables on `process.env` | `tests/support/isolated-daemon-host.mjs`, `first-run-browser-contract.mjs:704-755`, `j2-browser-contract.mjs:545-563`, `j10-browser-contract.mjs:407-420`; 1e-3's helper (if merged) changes its one `startGuiServer` call | the three `verify:*` scripts; compare with their base result (§E.2) |
| 3-6 | `tests/serve.test.mjs`: the two "host driver" cases (138-200) are replaced by "a backend driver in the environment is refused: the Vite server no longer hosts data" | `gui/vite.config.ts`: drop the `daemonRequestPlugin` import and use (lines 5, 69-73, 108); `guiHealthPlugin` reports `fixture` or `unavailable` only. `serve.mjs`: reject `HYPRIAL_GUI_BACKEND_DRIVER`; delete `--mode production`; `package.json`: delete `start`, drop `test:host-conformance:python` in favour of the default | `npx vitest run tests/serve.test.mjs` |
| 3-7 | conformance suite with the Node driver removed must still pass on Python | delete `gui/server/daemon-request/` (7 files); delete the Node driver from `host-driver.ts`; delete `handoff-plugin.test.ts`; delete the server blocks of `bootstrap-session.test.ts`; default `HYPRIAL_HOST_IMPL` is gone | `npx vitest run --maxWorkers=1 tests/daemon-request-host` |
| 3-8 | `node scripts/ci/check_gui_request_paths.selftest.mjs` cases that name `gui/server/daemon-request` as the sanctioned root are rewritten first (they fail against the old scanner config) | `gui-request-paths-scanner.json`: `sanctionedHostRoots` loses `gui/server/daemon-request/`, `hostRoots` loses `gui/server`; ledger: the four `createServer@host` / `createServer@startHost` rows and the `serve.mjs` reason text change or leave; set `REVIEWED_SCANNER_SHA256` from `--print-scanner-digest`. `check_gui_request_architecture.mjs:15-17` roots and `gui-request-architecture-baseline.json` rows for `gui/server` | the selftest; both gate scripts with `--base-ref <base sha> --base-branch dev --json`; `node --test gui/tests/architecture/daemon-request.node.mjs` |
| 3-9 | `test_source_runtime.py` / `test_source_session.py`: old names gone | `source.py`: remove the `node_*` aliases, `_NODE_LOG_NAME`; `process.py`: `_wait_for_node_ready` → `_wait_for_host_ready`; stale-file lists updated (`source.py:206-217`, `process.py:310-318`); `report.py` wording | `uv run pytest -q tests/shell/gui` |
| 3-10 | `npm --prefix gui run test:daemon-request-browser` | no change expected; fix the launcher or host, not the cases | compare the failing set with §E.2: no test that passes on the base may fail |
| 3-11 | — | docs and skills: `AGENTS.md:40-44,82`; `CONTRIBUTING.md:18`; `docs/guides/gui-dev-mode.md:95,257,277,340`; `gui/README.md:130,152`; `gui/GLOSSARY.md:18-19`; `gui/docs/architecture.md:23`, `architecture-decisions.md:23`, `module-boundaries.md:77`, `ui-development-standard.zh-CN.md:98`; `src/hyprial/skills/hyprial-ops/SKILL.md:210-225`; `.agents/skills/gui-workflow/SKILL.md:39`; `cli-command-inventory.json`; `commands.py:61-66` docstring; `gate-gui.yml` step names | doc gates |

`dist-parity` serving (`serve.mjs --mode dist-parity`) and the fixture dev preview (`HYPRIAL_GUI_DEV_PREVIEW=1`) keep using Vite directly. They carry no backend and leave with PR 4b/5.

---

## E. Verification

### E.1 Commands (every PR that touches product Python runs all of them)

| Command | Expected |
|---|---|
| `uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py` | exit 0; no new directory over 7 files, no file over 1000 lines |
| `uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py` | exit 0 |
| `uv run ruff check .` | clean |
| `uv run --no-project --python 3.12 python scripts/ci/check_inline_comments.py --base <base sha>` | no new inline comment (reminder gate; treat a finding as a failure) |
| `uv run pytest -q tests/shell/gui/host` | all pass (new) |
| `uv run --with libcst==1.9.0 pytest -q -n 2 tests/shell/request_http tests/shell/gui tests/integration/request_http` | base: 738 passed, 2 failed (§E.2); after: same 2 failures, more passes |
| `uv run --with libcst==1.9.0 pytest -n 2 tests/` | **full run, required**; failing set must equal the base's failing set — measure the base on the same machine first, it was not measured for this plan |
| `uv run pytest -q tests/tooling/architecture/boundary/test_supervision_boundary_gate.py tests/shell/cli/installation/restart/post_install` | pass; manifest and post-install surface unchanged |
| `./contract/run-all.sh --isolated-only` | `RESULT PASS 14/14` (value from `1e-2-step0.md`) |
| `uv run python scripts/verify/check_contract_impact.py` (as CI invokes it) | pass, or `Contract-Impact:` line present (§H) |
| `npm --prefix gui run typecheck` | exit 0 |
| `npm --prefix gui test` | base at `9099d0411`: 193 files, 1 failing file (`dev01-onboarding-browser.test.mjs`, known). Re-measure on the PR's base; same failing set |
| `HYPRIAL_HOST_IMPL=python npx vitest run --maxWorkers=1 tests/daemon-request-host/host.test.ts tests/daemon-request-host/contract-mock-loopback.test.ts tests/daemon-request-host/real-daemon-loopback.test.ts` (from `gui/`) | 22 cases pass (M2 + M3 on the prototype) |
| `npm --prefix gui run build` | exit 0, "Production copy gate passed" |
| `node scripts/ci/check_gui_request_paths.mjs --base-ref <base sha> --base-branch dev --json`, `node --test scripts/ci/check_gui_request_paths.selftest.mjs`, `node scripts/ci/check_gui_request_architecture.mjs --base-ref <base sha> --base-branch dev --json`, `node --test gui/tests/architecture/daemon-request.node.mjs` | pass; ledger did not grow |
| `npm --prefix gui run test:daemon-request-browser` (PR 2c optional, PR 3 required) | failing set ⊆ base failing set |

### E.2 Pre-existing failures

| Suite | Base | Failing | Source |
|---|---|---|---|
| focused pytest (M6) | `182c118ab` | `tests/integration/request_http/setup/test_squire_prepare_http.py::test_start_over_http_returns_a_ready_receipt_and_projects_the_role_skill` and `::test_lost_start_response_is_not_replayed` (`HARNESS_START_FAILED` in the daemon log) | measured |
| `npm test` | `9099d0411` | `tests/dev01-onboarding-browser.test.mjs` (colour wait, `:91`) | `1e-2-step0.md` |
| `test:daemon-request-browser` | `9099d0411` | 11 of 16 (first: `browser-acceptance.mjs:550`, `chat-layout` not visible) | `1e-2-step0.md` |
| `test:daemon-request-browser` | `182c118ab` | 10 of 16, listed in §E.3 | measured |
| full `pytest tests/` | `182c118ab` | **UNKNOWN** — not run (4 cores shared; hours). Design §8 and `ENV.md` say `dev` has failing shards | — |
| `verify:first-run-browser`, `verify:j2-browser`, `verify:j10-browser` | `182c118ab` | **UNKNOWN** — measure before step 3-5 | — |

### E.3 Result of M8 (`test:daemon-request-browser`, base `182c118ab`, Node host, 587 s)

| Result | Test | First failure |
|---|---|---|
| FAIL | actual CLI ordinary App: both drivers share build, page and paths | `browser-acceptance.mjs:550` `chat-layout` not visible, 20 s |
| pass | actual CLI Settings reads are wired and read-only for both drivers | — |
| FAIL | actual CLI dev entry uses the same App and consumes its private handoff | same locator, `:550` |
| pass | actual CLI no-open handoff is removed by launch expiry | — |
| pass | actual CLI SIGTERM reclaims owned children and private files | — |
| FAIL | isolated daemon local organization creation preserves nullable publication and lists its member | see log |
| FAIL | actual CLI org.create reaches the shared client for both drivers without a fake success | see log |
| FAIL | actual CLI real-daemon G6 drops one IPC response without retry | see log |
| FAIL | actual CLI real-daemon Connections page conditionally unbinds Lark | see log |
| pass | actual CLI real-daemon orgfs.write drops one response without retry | — |
| pass | actual CLI real-daemon folder restore keeps a partial unknown batch without retry | — |
| pass | actual CLI real-daemon org refresh failure keeps the write result and needs manual refresh | — |
| FAIL | actual CLI Contract Mock chat workspace reads, sends and recovers without resending | see log |
| FAIL | actual CLI real-daemon chat shows an empty-inbox directory and refuses unconfirmed send | see log |
| FAIL | actual CLI real-daemon org surfaces: invite, join, machines and settings are wired | `:3010` "the isolated daemon must not initialize a tailnet" |
| FAIL | actual CLI real-daemon agent link surface preflights list and blocks an unbound add | see log |

The failures are page-locator and environment assertions, not host errors. The six passing cases are the PR 3 floor: both drivers through Settings, handoff expiry, SIGTERM reclaim, and three lost-response cases through the real daemon. PR 3 must keep all six and must not add a failure. The dev-entry case fails on the base, so `hyprial gui dev` through the Python host has **no passing CLI-level browser case to inherit**; step 3-2's pytest cases and a manual run (report PASS / FAIL in the PR body) are its evidence until that test is repaired. Full log: `docs/notes/gui-single-launch-prototypes-2026-11-15/host-2-3-prototype/base-test-daemon-request-browser.log`.

---

## F. Deletions in PR 3 (and the one in PR 2c)

| PR | Path | Lines | Note |
|---|---|---|---|
| 2c | `src/hyprial/shell/impl/gui/install/server.py` | 177 | 501 placeholder |
| 2c | `tests/shell/gui/installed/test_installed_gui.py` static-server cases (658-746, 941-967) | ≈ 115 | replaced by one hosted case |
| 2c | `lifecycle/session.py:386-424` (driver-less start) | 39 | |
| 3 | `gui/server/daemon-request/index.ts` | 75 | |
| 3 | `gui/server/daemon-request/authority.ts` | 55 | |
| 3 | `gui/server/daemon-request/config.ts` | 158 | |
| 3 | `gui/server/daemon-request/envelope.ts` | 136 | |
| 3 | `gui/server/daemon-request/proxy.ts` | 569 | |
| 3 | `gui/server/daemon-request/session.ts` | 376 | |
| 3 | `gui/server/daemon-request/upstream.ts` | 412 | directory `gui/server/` disappears: **1,781** |
| 3 | `gui/tests/daemon-request-host/handoff-plugin.test.ts` | 183 | ported in 2b-1, 2b-5 |
| 3 | `gui/tests/daemon-request-host/bootstrap-session.test.ts` lines 90-205, 257-366 and their helpers | ≈ 240 of 366 | ported in 2b-1..3; client block stays |
| 3 | Node driver in `host-driver.ts` | ≈ 40 | |
| 3 | `gui/scripts/serve.mjs` production branch and usage text | ≈ 30 of 318 | file stays for `development` and `dist-parity` |
| 3 | `gui/vite.config.ts` lines 5, 69-73, 108 | 7 | |
| 3 | `gui/package.json` `start` script | 1 | |
| 3 | `gui/tests/serve.test.mjs` cases 138-200 | ≈ 60 | replaced by one refusal case |
| 3 | `source.py` `_NODE_LOG_NAME`, aliases; `process.py` node naming | ≈ 15 | renames |
| 3 | scanner/ledger/baseline rows naming `gui/server` | ≈ 40 JSON lines | |

Not deleted here, recorded for PR 6/7: `contract/native-host/native-host.v1.json`, `request_http` `nativeHost`/`serve_external` (`bootstrap/external.py`, 270 lines; tests 813 lines), `hostContract` in `server.json`, `desktop_network.py`. PR 6's shell uses `hyprial gui --no-open --json`, so these have no remaining caller once it lands.

---

## G. Risks, UNKNOWNs, owner decisions

### G.1 Owner decisions (the default is what the implementer does if nobody answers)

| ID | Decision | Default | Alternative |
|---|---|---|---|
| OD-1 | Session layer placement | separate child process relaying to `request_http` (C.1) | mount in `request_http` — blocked by its size limits and by listener rotation |
| OD-2 | Driver when `HYPRIAL_GUI_BACKEND_DRIVER` is unset | wheel: `daemon` from PR 2c (the shell and an ordinary user run `hyprial gui` with no environment); source: `daemon` from PR 3. The launcher states the driver in `--json` (`backend_driver`). The principal stays derived from durable identity. AGENTS.md's "显式指定" is reworded to "the launcher names it; the browser never does" | keep the variable mandatory everywhere — then PR 6's shell must set it |
| OD-3 | `hyprial gui dev` after the Node host is gone | Python host in front, Vite dev server behind it for assets and HMR (M5). This also answers design §7.1 | Vite in front proxying `/daemon` — needs a `/daemon` literal and route in Node config (request-path gate, ledger growth) and a "public origin" exception to the Host rule; or refuse `gui dev` until PR 5 |
| OD-4 | Windows wheel | PR 2c returns `GUI_RUNTIME_UNAVAILABLE` (POSIX file locks, `platform_support.py`); registered in `known-problems.md`; port to `kernel.ProcessFileLock` before PR 6 targets Windows | keep the 501 static server on Windows — two products again |
| OD-5 | Non-loopback `--ip` | refused with `INVALID_ARGUMENT` from PR 2c (`config.py:90-101`); the session contract requires a loopback Host | allow the bind, sessions then fail with `HTTP_HOST_DENIED` |
| OD-6 | Split of PR 2 | 2a / 2b / 2c (D.0) | one PR, same commits |
| OD-7 | Contract location | `contract/gui-session/` (B.1) | `docs/design/` only — then nothing machine-checks it |
| OD-8 | Version negotiation | additive `sessionContract` + optional request header, absent = 1 (B.3) | no header until a version 2 exists |
| OD-9 | Corpus enforcement | tests on both sides; no `run-all.sh` step | add `contract/gui-session/validate.py` to `run-all.sh` |
| OD-10 | `mutates` for the mock driver | wire field if present, else local declarations, else `true`; ask 1e-2 for the wire field | generate a JSON table from the GUI build — couples host and GUI SHA |
| OD-11 | Host check on static paths | yes, all paths | `/daemon/*` only, as Node |
| OD-12 | `npm start` and `serve.mjs --mode production` | deleted in PR 3 | keep as a data-less preview |
| OD-13 | `/__gui/healthz` | kept as the launcher probe, `dataMode: hosted` | rename under `/daemon/` — changes the allowlisted ledger rows |

### G.2 Risks

| Risk | Mitigation |
|---|---|
| httpx honours proxy variables; this machine sets `HTTPS_PROXY` | `trust_env=False`; red-first test 2b-3 |
| Ready line printed before the socket listens | bind + listen in `__main__` before printing (seen in the prototype) |
| Path handling differs from WHATWG URL parsing (Node normalises dot segments; ASGI gives the raw path) | reject dot segments and control characters with 400; stated in the contract; browsers normalise before sending |
| Uvicorn WebSocket support for `web/live.py` relies on `websockets` 15 with Uvicorn 0.54 | worked in M5; pin nothing new; 3-3 covers it |
| Vite dev server reached through a proxy: HMR token and host checks | worked in M5 with Vite 8.3.5 defaults; forwarded headers are an allowlist |
| Supervision gate flags new timeout or polling symbols | naming rule in A.8; run the gate in every step that adds a constant |
| Post-install import surface changes | import the host only inside launch functions; test in §E.1 |
| Wheel path now needs a live daemon or an ownerless home; a user with a stopped daemon gets an admission error instead of a blank shell | intended (design §2.2); the existing admission error text is kept and the start command is documented in `gui-dev-mode.md` |
| Three children instead of two in dev mode | stop order in 3-2; `browser-acceptance.mjs` SIGTERM case covers reclaim |
| Conformance run adds ~60 s and a uv dependency to the GUI gate | the gate already syncs uv for the loopback tests |
| Merge conflicts with 1e-2 (`source.py`, `contract-mock-loopback.test.ts`, scanner JSON, ledger, `gate-gui.yml`) and with 1e-3 (browser helper) | disjoint functions; second to merge rebases; recompute `REVIEWED_SCANNER_SHA256` after the rebase |
| `SESSION_CAPACITY` unreachable over HTTP | keep the rule and the unit test; note it in the contract as defensive |
| The CLI convergence work changes `request_http` payloads | real-daemon conformance case fails loudly; corpus updated in the same PR |

### G.3 UNKNOWN

| Item | How to resolve |
|---|---|
| Full `pytest tests/` failing set on `182c118ab` | run once before PR 2b |
| Wheel build on the implementer's machine (native engine, KP-027) | try in 2c; BLOCKED is an acceptable recorded outcome |
| Whether `check_contract_impact.py` treats a new `contract/` directory as satisfying itself | run it on the 2a diff |
| `verify:first-run-browser` / `j2` / `j10` state on the base | measure before 3-5 |
| Behaviour under IPv6 loopback (`::1`) binds | host accepts `[::1]` in Host as Node does; the launcher only displays IPv4/IPv6 literals (`handoff.py:67-71`); not measured |
| Whether duplicate GUI bytes in a future shell package are still an issue | PR 6 |
| macOS behaviour of the prototype | every measurement here is Linux |

---

## H. PR-body inputs

| PR | Skill-Impact | Contract-Impact |
|---|---|---|
| 2a | none (no operator-visible change) | **adds** `contract/gui-session/` v1; no existing published field changes. The client sends one new optional request header |
| 2b | none (host not reachable from the CLI) | implements `gui-session` v1; session response gains the additive `sessionContract` object |
| 2c | `src/hyprial/skills/hyprial-ops/SKILL.md:210-225` (installed GUI is now the real backend; 501 text removed); `AGENTS.md:33-44` | removed published strings: `BACKEND_NOT_WIRED`, `not implemented: hyprial backend not wired`, header `X-Hyprial-Backend: not-wired`, error `GUI_INSTALLED_BACKEND_UNSUPPORTED`; `/__gui/healthz` `dataMode` `fixture` → `hosted`; `hyprial gui --json` for a wheel now carries `backend_driver`, `backend_pid`, `handoff_path`. `contract/cli` has no entry for these (verify with `git grep` at PR time); otherwise `Contract-Impact: launcher startup record gains fields already published for the source path` |
| 3 | `hyprial-ops/SKILL.md`, `.agents/skills/gui-workflow/SKILL.md:39`, `AGENTS.md:40-44,82` (data host is the Python host; `npm start` removed; `hyprial gui dev` topology) | none for the browser surface (same contract, proven by the suite). Launcher ↔ child protocol changes are internal. `HYPRIAL_GUI_READY.mode` for normal source stays `production` |

Every body also lists: commands run with results, the base failing set next to the PR's failing set, and UNKNOWN / BLOCKED items (wheel install, macOS, Windows).

---

## I. What later PRs need from this design

| PR | Needs | Provided by |
|---|---|---|
| 4b `--with-dev-tools` | a place to serve extra static trees and `/docs` behind a flag | the host's provider seam (`web/`): add `web/devtools.py`; `/docs`, `/docs-assets/*`, `/openapi.json` are session-gated relays to `request_http`, which already renders Swagger when `HYPRIAL_REQUEST_HTTP_DEV_SWAGGER=1` and has the assets directory variable (`http_contract/swagger.py:20-21`; `app.py:641-661`). The launcher sets those two variables on the `request_http` child. No route is added to the session contract's closed set without a contract bump — `/docs` is listed as "optional, flag-gated" in §12 from v1 |
| 4b | runtime-loadable dev tools | a second manifest-verified asset root passed as `--dev-tools-root`; the page learns the flag from `/__gui/healthz` or a field in the session response (additive) |
| 5 `--live-source` | Vite behind the host | `web/live.py` from PR 3. PR 5 only maps flags: `--live-source` selects `--live-upstream`, independent of `--with-dev-tools`; `hyprial gui dev` ≡ both |
| 6 minimal shell | `hyprial gui --no-open --json` and a private handoff | unchanged record and `launch.url`. Still to do in PR 6: accept `--port 0` and report the bound port (the child already can), define the JSON record as a `contract/cli` entry, Windows runtime (OD-4), default driver (OD-2) |
| 6 | navigation guard to one origin; no IPC for that origin | the contract's single-origin statement (§2); nothing in the host |
| 7 Docker, docs, skills, contract | one entry to document | after PR 3 every path is `hyprial gui`; PR 7 retires `contract/native-host` and the `nativeHost` branch (§F) |

---

## Summary (15 lines)

1. Today there are two Python pieces and one Node piece: the wheel's static server (no session, 501 for business), `request_http` (bearer upstream to the daemon over IPC), and the Node host that owns browser sessions.
2. The Node host is 1,781 lines in 7 files; its behaviour is 8 routes, 2 cookies, 4 browser headers, about 25 host error codes, an upstream probe and a byte relay (A.3).
3. The session contract is written nowhere; PR 2a lands it as `contract/gui-session/` with a machine corpus and version negotiation that is additive for old GUI builds.
4. The Python host is a separate ASGI child process in the Node host's position, relaying to `request_http` or the mock; it cannot live inside `request_http` (size limits, rotating listener).
5. A prototype of that host passes all 17 Node host HTTP cases and all 5 loopback cases, and a real browser boots the built GUI through it.
6. Conformance is one vitest suite with a driver seam (`HYPRIAL_HOST_IMPL=node|python`); after PR 3 the same files remain as the Python host's HTTP tests.
7. PR 2 is split: 2a contract + seam (no Python), 2b host package (unreachable from the CLI), 2c wheel wiring and deletion of the 501 server.
8. PR 3 switches source `hyprial gui` to the Python host over `gui/dist`, and `hyprial gui dev` to the Python host in front of the Vite dev server (measured: HTTP and HMR WebSocket work).
9. PR 3 deletes `gui/server/` (1,781 lines), about 420 lines of Node-only tests, `npm start`, and the scanner/ledger rows that named the Node host.
10. The mock stays an upstream bearer endpoint; 1e-2 and this line touch different functions of `source.py` and compose in either order.
11. Security rules are kept and tightened: loopback bind enforced, Host/Origin on every path, principal only from the launcher, no proxy environment for upstream calls.
12. The client gets one small fix: CSRF is decided from the session's method list, not the build's.
13. Pre-existing failures measured on the base: 2 pytest cases in `test_squire_prepare_http.py`; the full pytest run and three browser gates are UNKNOWN and must be measured before the PRs that depend on them.
14. Thirteen owner decisions have defaults; the ones that change behaviour are the default driver (OD-2), the dev topology (OD-3), Windows (OD-4) and non-loopback `--ip` (OD-5).
15. Later PRs get a provider seam for dev tools and Swagger, the live-source proxy, and an unchanged `--no-open --json` handoff for the shell.

## Estimated diff

| PR | Added | Deleted | Files |
|---|---|---|---|
| 2a | ≈ 650 (contract 350, seam 120, tests 150, client 30) | ≈ 120 | ≈ 10 |
| 2b | ≈ 2,700 (host 1,500, pytest 1,100, driver + workflow 100) | 0 | ≈ 22 |
| 2c | ≈ 450 | ≈ 450 | ≈ 16 |
| 3 | ≈ 700 (live proxy + tests 250, launcher 150, helper 120, docs 180) | ≈ 2,500 | ≈ 45 |

## Owner decision (2026-11-15): Windows out of scope for now

Only Linux and macOS are built and verified. Corrected by the owner the same day: do NOT delete Windows-related tests, gates or CI targets; existing checks such as `scripts/ci/check_windows_portability.py` and the PR-template item keep running and must stay green. Windows-only problems are recorded and ignored for now: no new Windows code or tests, no blocking on them. Packaging targets are macOS arm64 and Linux x64; Windows items in this plan are not scheduled.
