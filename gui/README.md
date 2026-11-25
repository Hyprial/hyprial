# Hyprial shared GUI (`gui/`)

团队及 AI agent 必读：[单套 GUI 规则](docs/ui-development-standard.zh-CN.md#single-gui) ·
[共享入口映射](docs/shared-ui-entry.zh-CN.md)。预览与正式共用展示实现，只替换数据与能力适配。

架构入口：[GUI 术语表](GLOSSARY.md) · [当前架构](docs/architecture.md) ·
[架构决策记录](docs/architecture-decisions.md) · [文档索引](docs/README.md)。

中文操作指南：[GUI 开发模式操作手册](docs/dev-mode-user-guide.zh-CN.md)。

设计与研发规范：[GUI 设计与研发规范](docs/ui-development-standard.zh-CN.md)、
[组件目录](docs/component-catalog.zh-CN.md)、
[UI 变更交接模板](docs/ui-change-handoff-template.zh-CN.md)。组件参数、状态语义和
断点以源码及契约测试为准，不以原型或聊天记录为准。

shadcn/ui 的组件选型、主题映射与分阶段迁移另见
[前端组件接入规范](../docs/guides/frontend-components.md)。该规范补充上述 GUI
设计与研发规范，面向已有的 TypeScript primitives。实际依赖和已迁移组件以源码及
组件目录为准。

[shadcn Button / Card 示例](docs/shadcn.md)：开始页推荐区域使用本地 shadcn
组件，通过现有 primitives 保持公共接口；包含来源、样式边界、运行与验证命令。

This package is the shared React + TypeScript + Vite surface for the GUI
mainline. The ordinary source entry is the M1 same-origin host: one trusted
driver (`daemon` or `contract-mock`), one `HttpTransport`, and one generated
client feed every enabled business adapter. The browser sends no upstream
bearer and selects neither an upstream endpoint nor its identity. M1 uses only
an already-initialized isolated home; login and first-run setup remain M2 work
and are explicitly unavailable rather than falling back to a fixture.

`hyprial gui --with-dev-tools` adds the floating developer tool layer (module
index, and an API docs link over the daemon driver) to the same build; without
the flag the host answers `/dev-tools/*` with 404 and the layer is never loaded.

An explicit development preview remains available only with
`HYPRIAL_GUI_DEV_PREVIEW=1 npm run dev`. It is fixture/component review, not the
ordinary product path and not evidence of backend success.

- Applicable decisions: **ADR-GUI-003 / ADR-GUI-004 / ADR-GUI-005 /
  ADR-GUI-006** (`gui-architecture-20261022-v3`) and `GUI-FRESHESTART`.
- Integration target and base: follow the current task dispatch and verify the
  live refs before branch or PR work. This package does not merge `dev`, modify
  frozen `intg/*` branches, or change repository protection.
- Source boundary: the component, token and page sources were recovered
  file-by-file from the frozen GUI sources. The prototype direct-package pilot,
  legacy `src/gui` core/product pages, native shell and Python launchers are
  **not** part of this package.
- The daemon request contract owns operation names and envelopes.

## Dev/product parity model

Since the 2026-11-09 dev/product parity effort
([`../docs/design/gui-dev-product-parity-2026-11-09.md`](../docs/design/gui-dev-product-parity-2026-11-09.md)),
the binding rules for this package are:

- One product composition. `main.tsx` and the composition root only select the
  mock/real inputs (data source, capability adapters, controllers) and mount the
  same `App`. Product components, pages, reducers and view models must not read
  `import.meta.env.DEV`, `__HYPRIAL_DEV_*`, dev-only imports, or pick product
  components by build mode. Identical inputs must produce an identical product
  tree in both entries.
- Capability data, not build mode. `firstRunWizardEnabled` is decided solely by
  the injected capabilities. When it is `true`, both entries open the same
  first-run page; when it is `false`, both entries run the same hide/routing
  rejection. Auto-showing first-run setup must not consult DEV.
- One development tool surface. The former resident dev toolbar, bottom module
  index, navigation `devLabels` and the layout-shifting `.dev-preview-frame`
  are retired by this effort. Development tools live only in the independent
  floating tool layer (default collapsed, expand on click), mounted outside the
  product `#root`, never altering product layout and never entering production
  builds.
- Three anti-regression gates wired into `.forgejo/workflows/gate-gui.yml`:
  the static gate (`verify:dev-product-parity`), the dual-entry browser parity
  gate (`verify:dev-browser-parity`) and the production artifact gate
  (`verify:production-copy`). A missing browser or artifact is FAIL, never a
  skip reported as PASS. The browser gate's production side runs against a
  separate `dist-parity` test artifact: built like the release bundle (PROD
  build with DCE), with explicit mock inputs enabled only at the composition
  root and no dev tools mounted. `dist-parity` is for parity testing only and
  is never a release artifact; the release `dist` must contain no mocks,
  fixture URLs or scenario data, and the artifact gate scans only the release
  `dist`.

Status as of 2026-11-09: this model is the agreed target and review standard.
Its implementation is being integrated on `intg/gui-dev-diff-eliminate`
(integration PR #2612); the parity gates and their readings are not yet
verified at an integration HEAD and must not be reported as passing until that
verification happens.

## What is and is not wired

Both entries render the same shared implementation; the two columns below
differ only in the selected inputs (real client vs labelled fixture
composition), never in product components, routes or interaction logic.

| Area | Ordinary M1 source entry | Explicit development preview |
| --- | --- | --- |
| Shared shell, navigation and responsive layout | Shared implementation | Shared implementation |
| Chat | Real `message.query` / `message.history` reads; send remains unavailable until its contract is registered | Labelled fixture conversation |
| Agent/contacts | Same generated client for list/create/start/stop; missing methods are unavailable | Fixture composition |
| Organization | Same generated client through `createOrgLiveSource` | Fixture composition |
| Settings | Reviewed hosts/top-snapshot/provider adapters through the same client; unregistered settings remain unavailable | Fixture composition |
| Account, login, device, Squire first run | Explicitly unavailable M2 boundary | Fixture demo only |
| Upstream daemon or Contract Mock | Selected once by the trusted host environment; bearer stays in the host | Not used as product authority |
| Native desktop shell, packaging, mobile authentication | Separate owner/batch | Separate owner/batch |

The ordinary host mints a per-view browser session from a one-time launch grant.
The upstream bearer, endpoint and trusted principal stay in the launcher/host;
the launch grant is scrubbed from the URL before the first request. Reload uses
an HttpOnly control cookie plus an independent sessionStorage view proof, and a
refresh rotates the business session, CSRF and transport generation. Fixture
data, when explicitly selected, is labelled with `FIXTURE · 演示数据`.

## Commands

Requires Node 24+ and npm 11+.

```sh
npm ci --ignore-scripts --prefer-offline

npm run typecheck
npm run test
npm run test:acceptance               # Chromium browser acceptance, both host drivers
npm run build
npm run verify            # typecheck + test + build

# Explicit fixture/component preview (development mode only).
HYPRIAL_GUI_DEV_PREVIEW=1 npm run dev
```

The development server (`npm run dev`, `scripts/serve.mjs`) accepts `--host`,
`--port`, `--strict-port` / `--no-strict-port` and `--mode` (`development`, or
`dist-parity` for the parity gate). With npm, pass them after `--`, for example
`npm run dev -- --port 0`. The default is `127.0.0.1:4173` with strict port
selection. Only loopback hosts are accepted. It serves no data: it refuses a
`HYPRIAL_GUI_BACKEND_DRIVER` in its environment, and `npm start` no longer
exists.

The ordinary source host is the Python session host
(`src/hyprial/shell/impl/gui/host`), started by `hyprial gui` (it serves
`gui/dist` after `npm run build`) or by `hyprial gui dev` (it relays to this
Vite dev server). It reads `HYPRIAL_GUI_BACKEND_DRIVER` (`daemon` or
`contract-mock`), `HYPRIAL_BACKEND_ENDPOINT`, `HYPRIAL_GUI_PRINCIPAL`,
`HYPRIAL_GUI_UPSTREAM_TOKEN` and `HYPRIAL_GUI_LAUNCH_GRANT` from the launcher's
environment. The endpoint must be an explicit loopback HTTP origin. The
operation guide
[`../docs/guides/gui-dev-mode.md`](../docs/guides/gui-dev-mode.md) lists the
bounded TTL/capacity options and the direct invocation; do not use a resident
daemon home or real credential for tests. Browser tests start that host through
`tests/support/python-host.ts`.

`HYPRIAL_GUI_HOME` may point at this package or its worktree root for the dev
server. The dev server prints a machine-readable readiness line on stdout; the
launcher reads its loopback port from it:

```text
HYPRIAL_GUI_READY {"service":"hyprial-gui","mode":"development","dataMode":"unavailable","host":"127.0.0.1","port":4173,"url":"http://127.0.0.1:4173","driver":null,"backendConfigured":false,"session":"unavailable","guiHome":"..."}
```

SIGTERM/SIGINT produce `HYPRIAL_GUI_STOPPED {"signal":"SIGTERM"}` after the
server closes. The Python host prints its own `HYPRIAL_GUI_READY` line with
`"driver":"daemon"` and `"session":"launch-required"` and answers
`GET /__gui/healthz`, which reports whether a driver is configured, not whether
the upstream is ready. Readiness is proved only by the session-time upstream
`/readyz`, catalog and principal probe. CLI start/exit/lifecycle remains owned by
`local-macos-worker`; this package supplies the npm/Vite dev server and the build.

## Verification performed for the M1 source candidate

On the committed/candidate head, with Node 26.8.1 / npm 11.19.0:

- `npm run typecheck` — passed.
- `npm run test` — full focused/test suite passed; exact counts are recorded in
  the M1 PAC evidence for the fixed SHA.
- `npm run build` — passed. Vite reports only its existing config-loader and
  chunk-size warnings.
- `npm run test:daemon-request-browser` (later split into `gui/tests/acceptance/`
  and renamed to `npm run test:acceptance`) — passed in headless Chromium against the
  same built SPA for both `contract-mock` and a disposable real daemon. Each run
  used 8 same-origin `/daemon/*` requests, produced 3 read receipts carrying the
  upstream request ID, scrubbed the launch grant, sent no Authorization header,
  wrote no credential to storage and emitted no page errors.
- The Contract Mock is one JavaScript module, `gui/contract-mock`, in two forms:
  the Node process the `contract-mock` driver starts (`node contract-mock/serve.ts
  --scenario success`) and a `fetch`-shaped function for front-end work without
  a backend (`connectContractMock` in `src/dev/contract-mock`). Its examples are
  the generated table `src/dev/contract-mock/contract.generated.json`
  (`npm run client:reference` writes it, `client:reference:check` byte-checks
  it); `scripts/ci/check_gui_mock_source.mjs` keeps every other mock data source
  out of `gui/src`. Behaviour tests live in `tests/contract-mock`.
- Real isolated loopback tests also exercise daemon and Contract Mock ready/catalog/
  principal probes, the ordinary generated client and agent/settings/chat-read/
  org adapters, missing-scenario 409 and principal mismatch refusal.

Not run or not claimed by M1: login, registration, device enrollment, first-run
initialization, real provider credentials/IdP, wheel packaging, native WebView/
Tauri/mobile acceptance, chat send, and upstream methods lacking an applicable
contract. The Tasks page contains one clearly labelled visual sample and no real
task projection. See the fixed-SHA evidence under
`../docs/notes/gui-common-m1-pac-20261105/` for the exact PASS/UNKNOWN/BLOCKED
matrix.

## Repository checks

The shared GUI job may run `npm --prefix gui ci` and
`npm --prefix gui run verify`. A green local/build run is not a substitute for
the applicable repository CI or for real backend/browser acceptance by the
owner of that integration. Browser and backend status for other platforms must
be reported separately.

### 后端接线状态

普通源码入口使用已合入的 daemon request HTTP 契约、`MethodSpec`、generated
client 与四线已审工厂。该包的是 M1 源码候选，不是 wheel/M2 交付：未注册方法、
登录/首次初始化与 chat send 必须显示 unavailable，不得回退 fixture 或本地成功。
