# Hyprial shared GUI (`gui/`)

团队及 AI agent 必读：[单套 GUI 规则](docs/ui-development-standard.zh-CN.md#single-gui) ·
[共享入口映射](docs/shared-ui-entry.zh-CN.md)。预览与正式共用展示实现，只替换数据与能力适配。

中文操作指南：[GUI 开发模式操作手册](docs/dev-mode-user-guide.zh-CN.md)。

设计与研发规范：[GUI 设计与研发规范](docs/ui-development-standard.zh-CN.md)、
[组件目录](docs/component-catalog.zh-CN.md)、
[UI 变更交接模板](docs/ui-change-handoff-template.zh-CN.md)。组件参数、状态语义和
断点以源码及契约测试为准，不以原型或聊天记录为准。

shadcn/ui 的组件选型、主题映射与分阶段迁移另见
[前端组件接入规范](../docs/guides/frontend-components.md)。该规范补充上述 GUI
设计与研发规范，面向已有的 TypeScript primitives；本次仅添加规范文档，
fixture/占位接口边界保持有效。实际依赖和已迁移组件以源码及组件目录为准。

[shadcn Button / Card 示例](docs/shadcn.md)：开始页推荐区域使用本地 shadcn
组件，通过现有 primitives 保持公共接口；包含来源、样式边界、运行与验证命令。

This package is the shared React + TypeScript + Vite surface for the new GUI
mainline. **This batch is fixture-only and intentionally not wired to a real
backend.** The pages, navigation, forms and states are restored for visual and
interaction review; every business action goes through one same-origin
placeholder adapter and returns an explicit not-implemented response.

- Applicable decisions: **ADR-GUI-003 / ADR-GUI-004 / ADR-GUI-005 /
  ADR-GUI-006** (`gui-architecture-20261022-v3`) and `GUI-FRESHESTART`.
- Integration target and base: follow the current task dispatch and verify the
  live refs before branch or PR work. This package does not merge `dev`, modify
  frozen `intg/*` branches, or change repository protection.
- Source boundary: the component, token and page sources were recovered
  file-by-file from the frozen GUI sources. The prototype direct-package pilot,
  legacy `src/gui` core/product pages, native shell and Python launchers are
  **not** part of this package.
- The later contract batch owns the formal business contract. This package
  defines only the local placeholder interface documented in
  [`docs/stub-interface.md`](docs/stub-interface.md).

## What is and is not wired

| Area | This batch |
| --- | --- |
| Shared shell, navigation, responsive layout | Fixture UI |
| Start, Chat, Tasks, Agent/contacts, Org, Settings, First-run | Fixture UI and explicit placeholder results |
| Account, model, device, Squire, Agent, organization actions | HTTP **501** from the local Node placeholder |
| Real CLI, daemon, credentials, sessions, history | Not wired |
| Login state, online state, Agent replies | Never fabricated; fixture messages are labelled `演示` |
| Native desktop shell, packaging, mobile authentication | Separate owner/batch |

Fixture-only data is labelled with `FIXTURE · 演示数据`. The top bar also shows
the configured placeholder endpoint, read from the launcher's runtime health
endpoint. A configured endpoint means only "the local placeholder is
configured"; it never means "online", "logged in" or "backend available".

No credential is persisted. Password/token fields are cleared after submit, and
the placeholder service and adapter both redact secret-like params before
displaying or returning them. The stub does not log request bodies.

## Commands

Requires Node 24+ and npm 11+.

```sh
npm ci --ignore-scripts --prefer-offline

npm run typecheck
npm run test
npm run build
npm run verify            # typecheck + test + build

# Local placeholder service, ephemeral loopback port by default.
npm run stub

# Production assets (build first), with the placeholder configured.
HYPRIAL_BACKEND_ENDPOINT=http://127.0.0.1:<stub-port> npm start

# Vite development server, with the placeholder configured.
HYPRIAL_BACKEND_ENDPOINT=http://127.0.0.1:<stub-port> npm run dev
```

The launcher accepts `--host`, `--port`, `--strict-port` / `--no-strict-port`
and `--mode`. With npm, pass them after `--`, for example
`npm start -- --port 0`. The default is `127.0.0.1:4173` with strict port
selection. Only loopback hosts are accepted.

`HYPRIAL_GUI_HOME` may point at this package or its worktree root. The launcher
prints a machine-readable readiness line on stdout:

```text
HYPRIAL_GUI_READY {"service":"hyprial-gui","mode":"production","dataMode":"fixture","host":"127.0.0.1","port":4173,"url":"http://127.0.0.1:4173","backendConfigured":true,"backendEndpoint":"http://127.0.0.1:43123","guiHome":"..."}
```

SIGTERM/SIGINT produce `HYPRIAL_GUI_STOPPED {"signal":"SIGTERM"}` after the
server closes. The exact interface is in
[`docs/stub-interface.md`](docs/stub-interface.md). CLI start/exit/lifecycle
remains owned by `local-macos-worker`; this package supplies the npm/Node
interface and does not add a Python launcher.

## Placeholder HTTP path

The browser always calls the same-origin path:

```text
POST /api/gui/v1/not-wired
Content-Type: application/json

{"command":"chat.send","params":{"targetId":"agent-squire","text":"..."}}
```

Vite proxies `/api/gui/v1` to `HYPRIAL_BACKEND_ENDPOINT`; the browser never makes
a cross-origin business call and no CORS grant is required. A valid business
request returns HTTP 501 with:

```json
{
  "ok": false,
  "code": "BACKEND_NOT_WIRED",
  "message": "not implemented: hyprial backend not wired",
  "command": "chat.send",
  "params": {"targetId": "agent-squire", "text": "<redacted>"}
}
```

Malformed JSON, invalid commands, unsupported media and oversized bodies return
400/415/413 respectively. Unknown requests also fail explicitly with HTTP 501.
The exact message is part of this batch's test contract; it is **not** a formal
business contract and does not promise future operation names.

## Verification performed for this batch

On the exact worktree head, with Node 24.21.0 / npm 11.19.0:

- `npm run typecheck` — passed.
- `npm run test` — 17 files, 114 tests passed.
- `npm run build` — passed (`dist/index.html`, CSS 53.70 kB, JS 274.40 kB).
- Real Chromium run against `npm start` + the real Node stub: fixture marker and
  endpoint address visible; all seven pages navigable; account, model, org,
  invite, chat, device and Squire actions returned HTTP 501 with the exact text
  and redacted params; tokens were cleared and no storage was written; no fake
  login, online state or new Agent reply; no console/page errors; 390 px
  start/chat had no horizontal page overflow.
- `npm run dev` readiness and SIGTERM shutdown were exercised through the npm
  scripts as well as the production launcher. The browser harness is kept
  outside the repository and uses an existing cached Chromium; it is not a
  package gate.

Not run or not claimed: real backend, login, device enrollment, model
validation, Squire creation, organization changes, real chat/history, native
WebView/Tauri/mobile acceptance, packaging and update flows. The Tasks page
contains one clearly labelled visual sample and no real task projection.

## Repository checks

The shared GUI job may run `npm --prefix gui ci` and
`npm --prefix gui run verify`. A green local/build run is not a substitute for
the applicable repository CI or for real backend/browser acceptance by the
owner of that integration. Browser and backend status for other platforms must
be reported separately.

### 后端接线状态

PR #2043 的临时 daemon request 契约候选已撤回，不作为前端接线规范。
相关代码和能力矩阵仅保留在 Git 历史中，不应恢复为当前接口标准。
后端统一请求入口及接口定义由后端负责人姚升悦确认；本说明不表示 HTTP API 已交付。
前端继续按页面与组件规范开发，未接入的业务能力显示未连接、状态未知或暂不可用，
不得用本地成功状态冒充真实业务完成。后续接线以已确认的后端接口为依据，独立提交适配改动。
