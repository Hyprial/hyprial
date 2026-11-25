# Hyprial GUI 设计与研发规范

文档版本：2026-11-09（补充 dev/product 等价改造约束）

适用范围：仓库根 `gui/` 下的共享 Web UI，以及后续 Web、桌面、移动对同一套组件和页面的复用

读者：产品负责人、GUI 研发人员、AI 编码 agent、验收与集成人员

本规范不把规划、原型或占位结果写成已实现能力。下文标注 2026-10-28 批次的实现快照仅反映当时的 fixture/占位范围；后续各能力已逐步接线，须以目标提交的源码和实测证据逐项确认，不能沿用旧快照认定全部未接线或全部可用。预览与正式的共享入口见 [入口映射](shared-ui-entry.zh-CN.md)，团队单套界面约束见 §1.3.1。

规范用语：

- **必须**：交付前必须满足；例外需要在 PR 和交接记录中写明理由、影响和后续动作。
- **应**：默认遵守；偏离时需要有可审查的技术或产品理由。
- **可**：允许的选择，不构成强制要求。

## 0. 产品负责人先看这六条

1. **唯一界面主线是 `gui/` 的 React + TypeScript + Vite 实现。** 不再把原型 HTML 直接装进客户端，也不长期维护“原型一套、正式页面另一套”的双实现。
2. **产品设计由瑞华统一负责。** 研发为解除阻塞可以先补 UI，但必须标记“研发补齐，待产品规范设计”、登记编号并交回产品统一，不能把临时补丁当成最终设计。
3. **原型是视觉与交互参考，不是运行时后端。** 原型里的按钮、状态和样例不能证明真实业务已接通；演示数据必须显著标记。
4. **当前页面不是全部完成了组件化。** 首次设置和设置页已使用新 primitives；开始、聊天、Agent、组织、连接仍有旧样式页面。不能对外宣称“全站组件化”或“无障碍已验收通过”。
5. **真实能力必须逐层举证。** 启动预览、显示 fixture、组件单测、构建成功、真实后端、真实登录、真实回复、安装包、移动真机是不同证据，不能互相替代。
6. **改动先读源码和 [组件目录](component-catalog.zh-CN.md)。** 组件参数、token、断点和状态语义以源码及自动化契约测试为准，不以截图、旧原型或聊天记录为准。

## 1. 范围与单一事实来源

### 1.1 本规范负责什么

- 共享页面的结构、布局、视觉语言、响应式、键盘与基础无障碍要求。
- 现有组件、primitives、token 的复用和扩展规则。
- 页面、状态、适配器的职责边界，以及 fixture/真实数据/未知状态的表达。
- 开发预览、适用验证、PR、产品验收和研发补齐 UI 的交接方式。

### 1.2 本规范不负责什么

- 不定义正式 HTTP、认证、授权、会话、消息或组织协议。正式契约属于对应 contract/business owner，必须在契约变更中单独落实。
- 不定义 CLI/daemon 的权限模型，也不授权浏览器或客户端执行任意 shell。
- 不把桌面完整打包、移动正式登录、更新器、签名或发布说成已经完成。这些是独立工作和独立验收。

### 1.3 唯一 UI 来源

共享 UI 的源代码只在根 `gui/` 内维护：

| 路径 | 职责 |
| --- | --- |
| `gui/src/components/primitives/` | 无业务语义的基础组件和统一交互/无障碍外壳 |
| `gui/src/components/ui/` | 本地 shadcn Button/Card 内部实现，由 primitives 适配层消费 |
| `gui/src/components/` | 共享的非 primitive 组件，如导航、账号菜单、头像、徽标 |
| `gui/src/pages/` | 页面组合、页面局部组件、页面级展示逻辑 |
| `gui/src/state/` | 展示状态、reducer、选择器、操作生命周期、视图模型 |
| `gui/src/adapters/` | 类型化业务操作到当前宿主/占位服务的适配层 |
| `gui/src/styles/tokens.css` | 旧 token 层与新增 `--ui-*` 语义 token 层 |
| `gui/src/styles/primitives.css` | 仅服务于 `.ui-*` primitives 的样式 |
| `gui/src/styles/shadcn.css` | Button/Card 的 `tw:` utilities 与 `--ui-*` 映射，不导入 Preflight |
| `gui/src/styles/app.css`、`workspace-pages.css` | 现有应用壳和旧页面的样式，按迁移批次逐步收敛 |

必须遵守：

- 新页面、新组件、新样式必须落在 `gui/`，不得在 `src/gui/`、`app/`、原型目录或原生壳目录里再建一套正式页面。
- 产品原型原件如需归档，只能作为版本化的设计参考并保留 hash；不得由构建流程直接把它当作生产 UI 入口，也不得通过注入脚本伪造真实业务成功。
- 同一个用户路径不得存在两套并行维护的实现。迁移期如果新旧页面并存，必须有明确主入口、迁移编号和退场条件，不能长期双写。
- Web、桌面、移动共享页面、组件、状态机；平台能力必须通过适配层注入，不得在每个页面里复制平台分支。

<a id="single-gui"></a>

### 1.3.1 预览与正式必须共用页面

本节约束所有 GUI 研发人员和 AI agent；[共享入口说明](shared-ui-entry.zh-CN.md) 记录具体实现路径。

- 同一产品路由必须复用相同展示组件、布局、样式 token、表单校验及状态展示。禁止复制为 Preview/Fixture 与 Live/Production 两份页面，也不能以独立 HTML 代替产品预览。
- 差异只允许存在于组合根选择的输入：数据源、能力适配器与控制器，fixture 与真实控制器向同一个视图传递数据、能力和回调。相同输入下，开发预览与正式入口的产品 `#root` 规范化 DOM、关键布局和操作结果必须一致。产品组件、页面、reducer 和 view model 不得包含 `import.meta.env.DEV`、`__HYPRIAL_DEV_*`、dev-only import 或按构建模式选择产品组件的分支；不能为了统一组合丢失真实授权、取消确认、重试、历史续载或结果未知状态。
- `firstRunWizardEnabled` 是能力数据，只由注入的 capabilities 决定，不得再检查构建模式。为 `true` 时两个入口打开同一个首次设置页；为 `false` 时两个入口执行相同的隐藏与路由拒绝逻辑。
- 开发工具收进独立浮动工具层：默认只显示右下浮动球，点击展开；支持 Escape/外部点击关闭、键盘操作与焦点恢复，面板非模态、限制最大尺寸并内部滚动。浮动工具及其面板是唯一允许的开发期附加 UI，必须位于产品 `#root` 之外，不改变产品布局，且不进入正式构建；production 不挂载开发工具 root。常驻 dev 工具栏、底部模块索引、导航 `devLabels` 和改变产品布局的 `.dev-preview-frame` 随 2026-11-09 dev/product 等价改造退役，不得恢复。
- 浮动工具层只经 `src/dev/tools` 的 `mountDevTools(container, context)` 挂载，容器必须在产品 `#root` 之外；工具层不得导入场景、会话、mock 或 fixture 模块。仅对模拟数据有意义的工具（账号与初始化预设、归档场景选择）由 fixture 组合经 context 提供；组合为真实后端时这些工具不渲染，也不显示说明文字；工具层不得改写 URL 或产品状态。
- 开发浮动工具只选择场景、注入 mock 数据或展示诊断，不能替换产品页面或直接修改产品 DOM、路由、组件类型；产品只响应新的 mock 输入。平台差异通过适配层注入；页面不得新增私有 CLI、系统调用或传输通道。
- 真实能力不可用时不换另一套页面、不回退模拟成功、也不在页面上写说明；按 [§4.7](#unavailable)：没有能力端口的不渲染，被拒绝的在用户操作时发通知。缺少数据证据不等于空列表。预览标记模拟，默认不得触发真实登录、凭据写入、进程启动或消息发送。
- 新页面先登记共享视图与控制器路径。公共组件接口、路由和 token 改动协调收拢，不能复制一份规避团队并行冲突。研发补齐的视觉差异按交接模板交产品纳入后续设计。
- 开发与正式模式运行同一组适用的 UI 交互断言，分别注入模拟能力与真实/不可用能力，覆盖草稿、加载、失败、未知、禁用和响应式状态。两种模式的业务能力不必相等，模拟测试不代表真实收发通过。
- PR 列出两个入口复用的视图路径、能力差异、验证命令与结果。双入口等价防回归以三层门为准：静态门 `verify:dev-product-parity`（产品 import graph 与禁止的环境分支）、浏览器门 `verify:dev-browser-parity`（相同 mock 输入下比较产品根 DOM、布局与操作序列，并覆盖浮动工具交互；其 production 侧使用独立 `dist-parity` 测试产物——与正式产物一样执行 PROD 构建和 DCE，只在组合根开启显式 mock 输入、不挂载开发工具，只用于等价测试，不是发布物）、产物门 `verify:production-copy`（正式 bundle 不得包含开发工具、fixture 组合、场景文案或 dev-only CSS；正式 `dist` 零 mock——不含 mock、fixture URL 或场景数据，产物门只扫描正式 `dist`）；三者接入 `.forgejo/workflows/gate-gui.yml`，浏览器或产物缺失必须 FAIL，不得 skip 后报告 PASS。不得为了让浏览器门直接驱动正式入口而把测试 URL、mock composition 或场景数据打包进正式发布产物。启动器相关改动还须验证实际 `hyprial gui dev` 与 `hyprial gui`。未运行项写 NOT RUN/BLOCKED，不宣称全量通过。

发现新增第二套展示实现，应先收敛再交付。临时迁移例外必须经团队明确确认，并记录 owner、唯一主入口、退场条件和跟踪任务，不能自行以“预览需要”作为例外。

### 1.4 平台边界

- Web：页面由当前 CLI 实例提供，浏览器只通过同源的类型化业务入口访问，不直接持有 CLI/daemon 权限。
- 桌面：原生壳负责窗口、更新、进程与平台能力；页面仍消费共享组件。完整自包含桌面载荷属于后续打包工作，本规范不宣称已经完成。
- 移动：提供对授权主机的会话入口，不创建本地 daemon/harness，不把桌面打包机制或本地 shell 能力带到移动端。
- 任何平台的能力缺失按 [§4.7](#unavailable) 处理，不得伪造成功或回退到 demo。

<a id="single-request-path"></a>

### 1.4.1 数据请求只有一条路径

owner 2026-11-14 决定：GUI 的所有 API 路径都走 daemon request，由 CI 门禁守住，不靠评审记忆。

- `gui/src` 中只有传输模块可以引用 `fetch`、`XMLHttpRequest`、`EventSource`、`WebSocket`、`sendBeacon`（含 `globalThis['fetch']`、`const { fetch } = window`、`Reflect.get(globalThis, 'fetch')`、`window.navigator.sendBeacon` 等括号、解构与全局链写法），或书写请求路径（`/api/…`、`/daemon/…`、`/__gui/…`、绝对 URL）；`/api/gui/v1/`、`/api/reference/`、`/__gui/auth/` 三个旧前缀出现在任何字符串或模板字面量的任意位置都是违规（传输模块也不例外）。任何位置都不得导入 `axios`、`ky`、`got`、`undici`、`node-fetch`、`superagent`、`cross-fetch`、`ofetch`，`gui/package.json` 也不得依赖它们。`gui/index.html` 与 `gui/src`、`gui/public` 下 html 的内联 `<script>` 同样扫描；`gui/src` 内名为 `dist`、`node_modules` 的目录不跳过。传输模块是 `client/transport.ts`、`client/types.ts` 与 `app/backend/browser-session.ts`；`HttpTransport` 只在 `app/backend/browser-session.ts` 构造。feature、页面、适配器和 `gui/src/dev` 都不例外。
- 数据宿主有两个：Python 会话 host（`src/hyprial/shell/impl/gui/host`，同源宿主；`gui/server/daemon-request` 的 Node host 已于 2026-11-16 删除），以及 Contract Mock 引擎 `gui/contract-mock`（Node 进程形态与浏览器内 `fetch` 形态，只回放契约例子，不含自己的业务数据，产品代码不得导入；见下文 mock 来源门禁）。只有 Contract Mock 的 `gui/contract-mock/serve.ts` 登记为 `sanctionedHostRoots`，新增豁免需要 `REVIEWED_SCANNER_SHA256`。`gui/tests`、`gui/scripts`、`gui/server`（已无内容，回来的服务目录是无豁免的违规）与每个 `vite*.config.*`、`vitest*.config.*`、`playwright*.config.*` 不得挂载路由（`configureServer`、`configurePreviewServer`、`middlewares.use`，包括经别名、解构或括号访问）、启动服务（`node:http`/`https`/`http2`/`net`/`tls` 的命名、默认、命名空间、动态与 `require` 形式，`express`、`connect`、`koa`、`fastify`、`polka`、`sirv`，或带 `plugins`/`configureServer` 的 `vite` `createServer`），`route.fulfill`（含括号访问）也一样；浏览器测试也不得新增 `route.fulfill` 伪造业务应答（存量已随 PR 1e-4 全部删除）。`pending` 只剩 `gui/tests/daemon-request-host/host.test.ts` 的假上游 `listen`（Python host 契约套件的故障注入上游，见 KP-063）。需要模拟后端时使用 daemon request 的 Contract Mock driver。
- 门禁：`node scripts/ci/check_gui_request_paths.mjs`，接入 `.forgejo/workflows/gate-gui.yml` 的 “Check daemon request client generation and architecture”。扫描 `gui/src` 全部源码（含 `dev/` 与生成 client），没有目录级豁免。
- 清单文件 `scripts/ci/gui-request-paths-ledger.json` 有两个列表，行都带 `kind`、`path`、`subject`、`count`，未知键或未知 `kind` 直接失败：`allowlist` 是暂时保留的例外，每行必须写明自己的理由（例如 Tauri 的强制要求）；`pending` 登记后续 PR 尚未删除的存量。条目不再匹配时门禁同样失败。指向保留域名 `.invalid` 的 URL 不可能被请求，不算违规；仅有 `https://` 前缀的输入框占位文案不是 URL。
- 两个列表都只减不增：门禁用 `--base-ref` 与基线分支上的清单比较，新增行或变大的 `count` 失败，因此在同一 PR 里新增违规并新增清单行不会通过。基线没有清单时（首次落地）只在 `--allow-first-seed` 且基线分支为 dev/main、清单摘要与脚本内固定值一致时放行；空的、解析不了的基线引用或读不出的基线清单一律失败。已消除的条目必须从清单删除，`pending` 清空后保持为空。
- 扫描器配置（模式、扫描根、跳过项、扩展名、禁用前缀、清单路径）是数据文件 `scripts/ci/gui-request-paths-scanner.json`，门禁用 `--base-ref` 读取基线分支上的该文件逐项比较：删除或改动模式、扫描根、扩展名、库名单、禁用前缀，新增跳过项或豁免，改清单路径，删除或新增未知键都会失败；新增模式、扫描根、扩展名与删除跳过项放行。其他需要人工判断的改动只在脚本内 `REVIEWED_SCANNER_SHA256` 与 `--print-scanner-digest` 一致时放行，因此改扫描器必然同时改这个被评审的脚本。基线有清单但没有配置时同样需要该常量；首次落地摘要只在基线没有清单时使用。扫描逻辑本身（脚本与测试文件）与字符串拼接、计算属性、内联事件属性等规避手法不在门禁覆盖内，由评审负责。
- 新增规则或例外先写一个在植入违规时失败的用例：`node --test scripts/ci/check_gui_request_paths.selftest.mjs`。
- mock 数据只有一个来源（owner 2026-11-15，PR 1e-2）：例子只在契约 `contract/daemon-request/openapi.json`，引擎只有 `gui/contract-mock`。`gui/src` 中不得出现手写的按方法名分派的应答（对象键、`case`、`===` 比较 daemon 方法名，生成 client 除外）、不得出现生成表以外的数据文件、不得导入或以路径引用生成表（只有 `src/dev/contract-mock/index.ts` 与生成器可以）；`gui/contract-mock` 中不得写方法名字面量，`presets.json` 只能放引用、不得放载荷；`contract/daemon-request` 下的 Python 只保留例子的加载与校验，不得再有 mock 服务或其启动；`gui/src/dev` 下手写 fixture 文件登记行数上限，新增、变大或文件已删而行未删都失败。门禁 `node scripts/ci/check_gui_mock_source.mjs`，清单 `scripts/ci/gui-mock-source-ledger.json`（`allowlist`、`pending`、`fixtures`）只减不增并与基线分支比较，扫描器配置 `scripts/ci/gui-mock-source-scanner.json` 逐项与基线比较，规则同上一条；自测 `node --test scripts/ci/check_gui_mock_source.selftest.mjs`，只在 `gate-gui` 运行（需要 `gui/node_modules`）。`gui/tests` 单元测试里的手写替身（D11）仍然合法。

## 2. 当前基线与目标标准

### 2.1 当前真实基线

> 2026-11-09 等价改造状态：dev/product 等价改造（目标见
> [gui-dev-product-parity-2026-11-09](../../docs/design/gui-dev-product-parity-2026-11-09.md)，
> 集成 PR #2612）已确立 §1.3.1 的新约束：单一产品组合、差异仅 mock/real 输入、
> 独立默认收起的浮动开发工具、`firstRunWizardEnabled` 仅由能力数据决定、
> 三层防回归门。截至本日期，改造实现正在集成分支进行，常驻开发工具栏与
> 环境分支的移除尚未在集成 HEAD 上验收；下表"当前真实基线"保留改造启动前
> 的快照事实，落地状态以集成 HEAD 的三层门实测为准，不提前宣称通过。

截至本文档更新时：

- 普通源码入口经 Python 会话 host：同一构建只选择一个 daemon 或 Contract Mock driver，经同一 generated client 装配已注册的 Agent、设置读取、聊天读取与组织能力。fixture 仅由 `HYPRIAL_GUI_DEV_PREVIEW=1` 的显式开发预览或测试组合选择，不是普通入口的回退。
- 普通入口在首次设置共享组件中按 `account → device → 普通 Agent 运行配置（DeepSeek 凭证保存或 configureAgent）→ conversation（P3 reply confirmation）` 推进：DeepSeek 凭证保存消费 `deepseek-official`/`key` 映射，经同一 generated client 调用已注册的 `agent.secret.provider-write`，保存前读取 `agent.secret.list` 并拒绝在已有 grant 引用时静默覆盖；原首启 `SquireStep`、`prepareSquire`、`SquireIdentityStore` 与 `name-frozen` 分支已在 S2d 删除。Settings 不再承载 provider，只组合账号、用户身份和设备/组织导航；账号登录与设备初始化已有 daemon request 适配。
- S1 primitives 已存在并通过组件测试：Button、Card/StatusCard、States、Dialog、Field、Notice、PageHeader、ProgressBar、Stepper、DiagnosticBox。
- Button/Card 已通过原有 primitives 接口适配本地 shadcn 实现；fixture 开始页仅推荐区域完成迁移，详见 [shadcn 示例说明](shadcn.md)。公共参数与其他页面的迁移范围见 [组件目录](component-catalog.zh-CN.md)。
- 首次设置页、设置页和设备页已使用这些 primitives；开始、聊天、Agent、组织、连接页仍主要使用旧 `.page`、`.card`、`.button` 等类名。应用壳、导航、账号菜单、通知条、登出对话框也保留旧样式实现。
- token 存在两层：旧变量和新增 `--ui-*` 语义层。新 primitives 只消费 `--ui-*`；旧页面继续消费旧变量，尚未全量迁移。
- 尺寸契约有自动化断言：不允许 980px 桌面最小宽度；聊天列宽、1359px/979px 断点、`100dvh`、安全区、滚动稳定和 reduced-motion 都有测试保护。视觉实测只覆盖了已记录的局部页面/宽度，不能外推成全页面、所有平台或真机已验收。
- 没有真实后端、真实登录、真实历史、桌面安装包、移动正式认证或更新流程的完成证据。

### 2.2 目标标准

- 新页面优先由 primitives 和共享组件组合；旧页面按迁移任务逐步收敛，不为了“看起来统一”而一次性大改并破坏行为。
- 所有异步业务都具备明确状态：加载、空、错误、不支持、未知；未知不等于失败，也不等于成功。
- 页面只投影状态和触发类型化操作，不构造 CLI 命令、不自行判定权限、不自行把服务状态改成成功。
- 共享组件在 Web、桌面和移动间复用，平台差异放在适配器；产品设计、研发补齐、验收证据各自留痕。
- 视觉、交互、无障碍和多宽度检查与真实后端验收分开记录。

### 2.3 状态用词

文档、PR、交接和截图中应使用以下词：

| 词 | 含义 |
| --- | --- |
| 已实现 | 当前源码存在，且有对应测试或可复现证据 |
| 部分实现 | 只覆盖部分页面/平台/状态，必须列出缺口 |
| 目标 | 设计或计划中的方向，尚未完成 |
| 未接线 | 真实业务调用不可用；界面按 [§4.7](#unavailable) 处理，不显示说明文字（该词留给文档与 PR） |
| 未验证 / UNKNOWN | 没有足够证据，不得写成通过 |
| 阻塞 / BLOCKED | 已知原因导致无法继续，需写明 owner 和解除条件 |

## 3. 布局、排版与视觉规范

### 3.1 token 使用

样式分三层（详见 [主题系统](theming.zh-CN.md)）：

1. **主题文件** `gui/src/styles/themes/hyprial-v5.css`：shadcn 标准语义变量，是颜色、阴影、字体族字面量的唯一来源。
2. **`gui/src/styles/tokens.css`**：`--ui-*` 语义别名（指向主题变量）与间距、字号、行高、z-index、动效、控件尺寸刻度，是组件的消费面。旧层（`--ink`、`--paper`、`--line`、`--green`、`--btn-*` 等）已退役。
3. **`gui/src/styles/shadcn.css`**：`@theme inline` 把 `--color-*` 映射到主题变量。

新增或迁移中的 `.ui-*` 组件必须：

- 只引用已声明的 `--ui-*` token；主题文件之外不得出现颜色、阴影、字体族字面量，也不直接引用主题变量（`tests/theme-contract.test.ts` 强制）。
- 不引入远程字体、外部 `url(...)` 或网络资源；primitives 样式不得含 `@import` / `url(...)`（token 契约测试强制）。随包字体只在主题文件中以本地包 `@import` 与 `@font-face` 引入。
- 新增 token 先加到主题文件与 `tokens.css`，再使用，并在 PR 中说明语义和影响；不得在页面里复制一套私有颜色/间距常量。
- 不为了通过测试而删除或放宽 `token-contract` 断言。需要调整契约时，先说明产品/技术理由并更新测试和文档。

当前常用 token：

| 类别 | token 示例 | 当前值/用途 |
| --- | --- | --- |
| 文本 | `--ui-ink-1` … `--ui-ink-4` | 近黑主文本、次级文本、弱提示 |
| 表面 | `--ui-ground`、`--ui-surface`、`--ui-surface-muted` | 页面底 `#FAFBF6`、白卡片、井/轨道底 `#F2F4EA`；静止表面是平面色块，不用阴影与描边 |
| 边框 | `--ui-border`、`--ui-border-strong` | 极淡发丝线，仅白碰白处使用 |
| 动作 | `--ui-accent`、`--ui-accent-hover`、`--ui-accent-wash` | 近黑 `#17131F` 是唯一行动色（主按钮、选中、焦点环）；wash 为青柠浅底，只做底不做字 |
| 状态 | `--ui-info`、`--ui-positive`、`--ui-warning`、`--ui-danger` | 信息（紫蓝）、成功（深绿）、警示（橙）、危险（红，仅报错与破坏性操作） |
| 间距 | `--ui-space-1`、`--ui-space-2`、`--ui-space-3`、`--ui-space-4`、`--ui-space-5`、`--ui-space-6`、`--ui-space-8`、`--ui-space-10`、`--ui-space-12` | 4px 基准：4/8/12/16/20/24/32/40/48px，另有 `--ui-space-16/20/24/32`（64/80/96/128px） |
| 字号 | `--ui-text-xs` … `--ui-text-3xl` | 12/13/14/16/20/24/30px |
| 圆角 | `--ui-radius-xs` … `--ui-radius-pill` | 8/12/20/28/40/48/999px；按钮、输入框、标签页、标签、开关用胶囊 |
| 阴影 | `--ui-shadow-float-sm`、`--ui-shadow-popover`、`--ui-shadow-dialog` | 只给浮层（Dialog、菜单、popover、Toast、Tooltip） |
| 控件高度 | `--ui-control-height-sm/md/lg` | 40/48/56px |
| 层级 | `--ui-z-*` | 具名 z-index，不写数字字面量 |
| 宽度上限 | `--ui-measure-content`、`--ui-measure-form` | 内容栈 720px、表单 480px |
| 动效 | `--ui-motion-fast`、`--ui-motion-base`、`--ui-motion-slow` | 120/180/260ms |

### 3.2 字体与排版

- 拉丁字体为随包本地的 Geist（OFL，见 `docs/licenses/Geist-OFL.txt`），中文为随包思源黑体（Noto Sans SC 可变字重，OFL，见 `docs/licenses/NotoSansSC-OFL.txt`，按 `unicode-range` 切片本地按需加载），系统中文字体只作回退；数据、ID、时间戳、命令用 mono（Geist Mono 未分发，使用回退栈）。不发起远程字体请求。字重上限 600：400 正文、500 强调、600 标题。
- 新组件使用 `--ui-font-ui`、`--ui-text-*`、`--ui-leading-*`。页面标题优先使用 `PageHeader`，新 primitives 的标题层级为 30px；旧页面的 38px 品牌标题仍属于旧样式，不会因为本文档自动改变。
- 中文正文应保持可读行高：正文/说明使用 normal 或 relaxed，不要用极小字号承载关键状态。
- 长文本、长 ID、错误信息必须允许换行（如 `overflow-wrap: anywhere` / `min-width: 0`），不能靠截断隐藏唯一诊断信息。
- 品牌字标使用 `--ui-font-brand`（与正文同族），字号和字重以现有样式为准；不要用品牌字体承载长正文或表单标签。

### 3.3 色彩与层级

- 颜色必须表达语义，不只表达审美：信息、成功、警示、危险、普通状态分别映射到 `info`、`positive`、`warning`、`danger`、`neutral`。
- `statusToTone` 是新增状态组件的统一映射：ready→positive、loading→info、error→danger、empty→neutral。
- 错误、警告不能只靠颜色区分；必须有文字、图标或可读标签。
- 图标统一 Phosphor（`@phosphor-icons/react`，经 `primitives/Icon.tsx`）：默认线性 bold，选中/激活用 fill，尺寸 18/22/26；不用 emoji 或字符充当图标。
- 按钮变体：`primary` 近黑；`secondary`（默认）为 neutral 平面底、无描边；`ghost`/`text` 为 ghost；`danger` 为浅红底深红字。按钮为胶囊，按下 `scale(.98)`。
- 阴影和圆角是层级提示，不应成为唯一可点击/选中提示。可交互卡片还需要焦点、悬停、选中和禁用语义。
- 页面不得混用未声明的任意十六进制颜色来“微调”新组件；需要新色时先扩展主题与 token 语义。

### 3.4 当前布局契约

当前应用壳与聊天布局的硬约束：

| 项目 | 当前实现 |
| --- | --- |
| 应用框架 | 窗口占满视口，无外边距；`.app` 高 100dvh，只在 `.content` 内滚动 |
| 导航（≥641px） | `.shell` 为 72px + `minmax(0, 1fr)`；侧栏是全站唯一的 ink-block（`--ui-rail`），图标栏顶部图形标 32px、中间 56px × 56px 导航项（图标在上、12px 文字在下）、底部通知与账号；641–979 与桌面同构 |
| 内容区 | `.content` 内距 `32px 40px`（641–979 为 24px，≤640 为 16px）；`.page` 水平居中，窄档 `--ui-measure-content`（720px：设置、设备、首次设置），宽档 `--ui-measure-wide`（1080px：开始、Agent、组织、连接） |
| 聊天布局 | 不居中不限宽，占满内容区；`280px minmax(0, 1fr) 300px`，收起信息栏后为 280px + 内容；会话列表 `--ui-ground`，对话 `--ui-surface`，无外圈圆角 |
| 1359px | 聊天 `248px minmax(0, 1fr) 272px` |
| 979px | 聊天会话列表 240px + 对话，信息栏在对话下方（最高 40dvh）；导航保持 72px 侧栏 |
| 640px | 导航变为底部标签栏，顶栏为图形标 + 通知 + 账号；聊天单列、粘性输入区、底部安全区；primitives 的页头操作换行、对话框收边距、通知间距收紧 |
| 820px | 设置/首次设置/组织表单网格收为单列 |
| 430px | 概览卡等窄屏进一步单列 |

这些数值是**当前实现与测试契约**，不是永久不可变的产品法则。变更时必须：

1. 先确认产品意图和目标平台；
2. 同步更新布局样式、契约测试和本文档/组件目录中的相关说明；
3. 用真实浏览器在受影响宽度检查，不能只改 CSS 后宣称完成；
4. 不以 980px 最小宽度重新引入桌面横向滚动。

### 3.5 响应式与溢出

- 所有新容器必须有 `min-width: 0` 或等价的收缩策略，避免网格/弹性子项把页面撑破。
- 桌面、窄桌面、平板、手机宽度都要检查：长标题、空列表、大列表、长错误、软键盘、旋转和安全区。
- 不得用固定最小宽度换取“桌面看起来正常”。窄宽下应重排、换行、折叠或滚动局部区域，而不是让整页横向溢出。
- 聊天、设置、首次设置等长页面必须验证粘性/固定操作区不会遮住输入或底部按钮；移动检查 `env(safe-area-inset-bottom)`。
- 滚动容器应保持滚动位置稳定；不得因为重渲染把用户拉回顶部。聊天已有滚动位置记忆和 `overflow-anchor`/`scrollbar-gutter` 保护。
- 视觉验收必须在真实浏览器或目标 WebView 进行；截图只能作证据，不能替代键盘、状态和真实数据检查。

### 3.6 动效

- 动画用于状态变化和焦点提示，不用于掩盖加载失败或伪造进度。
- 使用 `--ui-motion-*` 和 `--ui-ease-standard`；不新增无节制的长动画。
- 必须在 `prefers-reduced-motion: reduce` 下关闭或弱化非必要动画；primitives 已提供该回退。
- 未知进度不画成满格；`ProgressBar` 的 `null` 必须显示“未知”，不得显示 100%。

## 4. 交互、状态与无障碍

### 4.1 必备状态

任何展示真实业务数据的视图都必须考虑：

| 状态 | 要求 |
| --- | --- |
| loading | 明确忙碌状态，使用 `aria-busy` 或对应 live 语义；不要先显示 0 或空数据冒充结果 |
| ready | 展示真实值；来源和单位清楚 |
| empty | 说明“没有内容”而不是“加载失败”，提供下一步（如新建、重试） |
| error | 展示可读错误和安全诊断码；可重试时才给重试 |
| unknown | 明确“结果未知，先核对原请求”；不得重发写操作或宣称成功 |

当前 `AsyncView` 是 `loading | ready | empty | error`；没有“不支持”状态，见 [§4.7](#unavailable)；`ready` 的数据不能为空来冒充成功。聊天发送状态另有 `not-sent | pending | sent | failed | unknown`，其中 `unknown` 的文案必须提示核对原请求。

### 4.2 焦点与键盘

- 所有交互元素必须是语义化 button/input/link，保留 `:focus-visible`；不得用可点击 `div` 代替按钮。
- Dialog 必须提供模态语义、初始焦点、Tab 焦点陷阱、Escape 关闭策略，并在关闭后把焦点还给触发元素。现有 `Dialog` 已实现这些基础行为；旧 `LogoutDialog` 也有对应实现，但新代码应优先复用 primitive。
- 打开菜单/对话框后必须能用键盘进入和离开；账号菜单已有方向键、Home/End、Escape 处理。
- 禁用/加载中的按钮必须阻止重复提交；`Button` 的 `loading` 同时禁用并设置 `aria-busy`。
- 表单控件必须有可见 label；`Field` 负责 hint/error/required 的关联。不要只靠 placeholder 作为标签。
- 图标按钮必须有可读的 `aria-label`；纯装饰图标应 `aria-hidden`。

### 4.3 读屏与状态播报

- 异步状态、复制结果、加载变化使用 `role="status"` / `aria-live="polite"`；危险错误使用 `alert` / `assertive`，但要避免重复播报。
- 可读标签要包含上下文，例如 `StatusCard` 的 `aria-label` 形如“设备：未提供”，不能只播报裸数字。
- 未知值不能渲染成 0。当前 `StatusCard` 用“未提供”，`ProgressBar` 用“未知”。
- 步骤条只是展示层，当前步骤由调用方提供；状态文字通过视觉隐藏文本补充，不能只给颜色/符号。
- 不能把“已有 primitives 测试”当成整页无障碍通过。交付时应按变更范围检查标题层级、焦点顺序、表单关联、键盘路径和 live region，未见到的项目标 UNKNOWN。

### 4.4 文案真实性与内容

- 界面文案必须区分“已保存”“已校验”“已连接”“已发送”“已回复”“已完成”。这些不是同义词，不能由一个占位结果推导出来。
- fixture/演示数据必须显著标记；真实模式绝不回退 fixture。当前全局标记为 `FIXTURE · 演示数据`。
- 未接线动作不能静默失败：用户操作时经统一通知说明（[§4.7](#unavailable)）；不能把 HTTP 501 说成网络错误或账号错误。
- 错误提示可以包含安全诊断码、命令名和已脱敏参数；不得包含 token、密码、cookie、完整请求头、私钥或原始响应体。
- 外部文本（Agent 名称、消息、组织名、节点名）只作为文本渲染；不使用 `dangerouslySetInnerHTML` 或拼接 HTML。React 默认转义应保持。
- 时间、ID、状态等权威字段应由适配器/后端提供；UI 不得自造后端时间、消息 ID 或完成状态。

### 4.5 凭据与敏感数据

- 密码、访问令牌、模型 key 使用 `type="password"`，关闭自动完成和不必要的拼写检查。
- 提交后立即从组件状态清除敏感字段；不得写入 `localStorage`、`sessionStorage`、URL、日志、错误提示或截图。
- UI 和适配器都应该对敏感字段进行脱敏；占位适配器（`adapters/stub.ts`）已随 PR 1e 删除，数据请求只经 daemon request。新增命令/字段必须扩展测试，而不是只相信调用方不打印。
- 不得把真实凭据放进 fixture、PR 描述、截图、控制台或播放录制。需要真人输入时，由产品/操作者在受支持的本地入口完成。
- 失败诊断只展示安全 code 和已脱敏摘要；`DiagnosticBox.copyText` 必须由调用方先脱敏。

### 4.6 通知与反馈的去向

反馈先看它属于哪一类，再决定放在哪里：

| 反馈类型 | 去向 | 说明 |
| --- | --- | --- |
| 一次性操作结果（已保存、已复制、已发送、已删除） | toast（`push-notice`） | 看完即走；警告与错误不自动消失并进入通知中心 |
| 字段校验错误（密码错误、格式不对、必填） | 字段旁（`Field` 的 `error`） | 必须挨着出错的输入框；`data-inline-feedback="validation"` |
| 进行中（正在保存、正在读取） | 触发处或内容区（按钮 loading、`LoadingState`） | `progress` |
| 持续状态（需要连接模型服务、离线、结果未知） | 页面内（`Notice`、状态卡、空状态） | 不是某次操作的结果；`state` |
| 功能不可用 | toast（warning，`useNotifyUnavailable`） | 用户操作时提示；页面不写说明，见 [§4.7](#unavailable) |
| 阻断性错误（对话框内必须先解决才能继续；整页加载失败） | 原位（对话框内 `Notice`、`ErrorState` + 重试） | 弹 toast 会在用户还没处理时消失或被遮挡；`blocking` |
| 一次性凭据与需要抄录的内容（邀请链接、恢复码、诊断码） | 原位展示，可复制 | 不能放进会消失的 toast；`secret` |
| 消息流里的单条状态（已发送、发送失败、结果未知） | 该条消息旁 | 归属于那条消息；`message` |
| 多步流程的阶段提示（授权中、等待确认） | 流程内（`Stepper`、阶段行） | `flow` |
| 结果未知、不得重试 | 原位且持续显示，同时可发 warning toast | 真实性优先，不能只给一个会消失的提示 |

toast 的自动消失默认只适用于 info / success；错误与警告停留到用户关闭，除非通知显式带 `autoDismiss`（“不可用”通知如此，条目仍留在通知中心）。同一条结果不要既弹 toast 又留一行界面文字。
确属留在页面的，在元素上标注表中的 `data-inline-feedback` reason；提示性检查 `inline-feedback` 见
[Refactoring UI 提示性检查](refactoring-ui-advisory.zh-CN.md)，只提醒，不阻断。

<a id="unavailable"></a>

### 4.7 不可用的功能

规则（owner 2026-11-14）：**能力端口不存在（本构建没有这个功能，或只是一个没有可操作内容的占位）→ 不渲染；端口存在但调用被拒绝（方法目录、写准入、后端或宿主返回）→ 控件保持可操作，用户操作时发一条通知。页面上不再写“不可用 / 未接通 / 尚未开放”。**

| 情形 | 做法 |
| --- | --- |
| 组合没有该能力的端口（`api` / `client` / 处理函数为 `undefined`），或只是没有可操作内容的占位、永远 `disabled` 的控件 | 不渲染元素和说明；整段只有占位的区块返回 `null` |
| 端口存在，但某个方法或某次调用被拒绝 | 去掉静态说明、tooltip 与 `disabled`；点击时本地已知被拒则调用 `notifyUnavailable(...)` 且不发请求，调用返回被拒则把返回的文案交给 `notify`；不得既弹 toast 又留一行界面文字 |

仍留在页面上的（不受本规则约束）：加载中、空状态、实际发出的请求失败、结果未知、离线、权限、校验、忙碌，以及生产启动失败页。

调用方式：`useNotifyUnavailable()`（`components/notifications`，由共享 `AppShell` 挂载 provider 与 toast 宿主，首次设置框架同样挂载）。文案固定为 `${subject}暂不可用。`，tone 为 warning；toast 与其他 toast 同样时长后自动消失，条目留在通知中心，不会成为常驻 toast；技术原因或代码只作为通知中心条目的来源（`source · code`），不写进 toast 文案。

门：`tests/architecture/unavailable-copy.test.ts` 扫描 `gui/src` 的字符串、模板与 JSX 文本；命中词表的文案必须登记到 `STATE`、`NOTIFICATION`（或 `ELSEWHERE`、`LOGIC_NOT_COPY`）并在 PR 中说明理由，尚待移除的登记在按功能区分块的 `PENDING_*`。vitest 门只保证清单与扫描结果一致（不漏登记、不留过期条目），不能阻止“新文案连同新条目一起加入”；清单只能缩小由 CI 的 `scripts/ci/check_gui_unavailable_copy.mjs` 对基线分支比较来强制（任一桶新增条目或计数上升即失败，仅把 `PENDING_*` 中已有的字符串改登记到永久桶是允许的；首次引入时基线没有清单，按 first seed 通过）。扫描是词法层面的，运行时拼接的字符串不在其能力范围内。

## 5. 组件复用与新增

### 5.1 复用顺序

1. 先查 [组件目录](component-catalog.zh-CN.md) 和 `gui/src/components/primitives/`。
2. 若现有 primitive 能满足语义、交互和无障碍要求，直接使用；不要复制 CSS 或另写一个“看起来差不多”的按钮。
3. 若是多个页面共享、但没有业务语义的组合，提升为 `gui/src/components/` 下的共享组件。
4. 只服务单个页面的结构，保留在页面目录，不提前抽象成全局组件。
5. 只有确实需要新的通用交互/视觉语义时，才新增 primitive；新增前说明为什么现有组件不够。

### 5.2 新增 primitive 的最低条件

- 至少有两个真实消费场景，或它承载需要统一保证的复杂行为（焦点、模态、表单关联、异步状态）。
- 不带业务名词、不直接发起网络请求、不读取全局 store、不绑定具体 CLI 命令。
- 使用 `.ui-*` 类名和 `--ui-*` token；不覆盖旧页面选择器。
- props 明确、受控优先、可组合；危险/加载/禁用/未知状态有明确语义。
- 有对应测试：行为、ARIA、键盘、状态边界、错误与未知。
- 更新组件目录和 token 文档；PR 说明对旧页面的影响。

### 5.3 props 与状态约束

- 组件应是展示/交互外壳；业务状态由调用方持有，组件不偷偷做异步写入。
- `value: null` 表示“来源未提供”，不能自动变成 0；未知进度不能变成完成。
- `loading` 是派生展示状态，不能替代真实成功/失败；加载结束必须回到明确的 ready/error/unknown。
- 回调命名和参数保持简单；不要传入完整后端对象让组件自行猜字段。
- 破坏性 props 变更必须列出迁移点和旧调用方，不能静默改语义。
- 尽量保留原生 HTML 属性透传（如 Button 支持 button attributes），避免为了一个属性重写组件。

### 5.4 样式与类名

- 新 primitive 使用 `ui-` 前缀和 BEM 风格的块/元素/修饰符；页面专属样式使用页面前缀。
- `primitives.css` 只服务 primitives，不覆盖 `.card`、`.button`、`.page-*` 等旧页面选择器。
- 禁止在新 primitives 中直接写颜色值、远程字体、外链样式；新增视觉值先进入 token。
- Button/Card 的内部 shadcn utilities 使用 `tw:` 前缀，保留 `.ui-*` 布局钩子；`shadcn.css` 中的本地 npm CSS 导入在构建时处理，不加载远程资源，也不覆盖旧 token。组件接入不改变页面的业务行为。
- 旧页面迁移时，优先把结构替换为 primitives，再删除重复样式；不要在一轮任务里同时重写业务行为、布局和品牌视觉。

## 6. 页面、状态与适配器职责

### 6.1 分层

| 层 | 负责 | 不负责 |
| --- | --- | --- |
| Page | 组合布局、选择器、把 view model 映射到组件、触发 UI action | 直接调用 CLI/shell/HTTP、鉴权、持久化密钥、构造原始命令 |
| Shared component | 可复用的展示与交互外壳 | 业务决策、网络请求、全局状态 |
| State/view model | 页面展示状态、reducer、selectors、操作生命周期、去重/合并 | 定义正式传输协议、自行判定权限 |
| Adapter | 把类型化操作映射到当前宿主/服务，投影真实结果，脱敏和错误归一 | 把页面当命令行、伪造成功、绕过服务端授权 |
| Host/CLI/daemon | 身份、权限、daemon、会话、持久化权威 | 被 UI 绕过 |

### 6.2 UI 不得越界

页面和 primitives 必须：

- 不调用 `child_process`、shell、文件系统或任意命令行。
- 不拼装 `hyprial` 命令或把 command 字符串当成正式业务接口。当前占位适配器中的 command 名只是本批本地边界，不是未来契约。
- 不自行保存/刷新 token、不自行判定服务端权限、不自行重试写操作。
- 不直接导入 `src/hyprial`、daemon 数据库或内部实现。
- 不把“本地能显示”当成“服务端已授权”。

正式 HTTP、认证、事件、错误码和跨端接口由对应契约 owner 定义。本规范只规定界面如何消费类型化适配器和展示状态；不得在 UI 文档里私自创造接口 schema。

### 6.3 状态与消息规则

- 聊天消息以权威 `id` 合并/去重；重复事件只更新同一条，不新增行。local 占位 id 不得被提升为权威历史。
- 写操作进入 in-flight 后，若结果不明，只能标记 `unknown`；必须用原始 request id 核对，不得在 UI 里自动重发或改成成功。
- 读失败应保留上次成功数据并标记 stale，不能清空数据或把失败说成空结果。
- request id 由调用方/契约提供；生命周期模块不自行创建、改写或猜测。
- 切页/切目标不应丢失草稿；聊天已按 target 保存草稿。新页面如有未提交表单，应明确说明离开行为。
- 未知、失败、空、忽略是四个不同结果；不能共用一个“没有数据”文案。

### 6.4 fixture 与真实模式

- fixture 数据只能用于明确标记的演示/视觉核对，不能含真实凭据或个人隐私。
- 真实模式缺接口时失败关闭：按 [§4.7](#unavailable)，不回退 fixture 或演示成功。
- 当前 `dataMode` 只有 fixture；引入 live 前必须先有真实适配器和契约，不得靠改一个字符串把 fixture 页面叫成 live。
- 适配器可以有一个受控的本地占位实现用于开发，但 UI 必须在术语和视觉上如实标注；占位 HTTP 501 不是正式业务契约。

## 7. 平台、Web、桌面与移动

- 共享页面和组件不为平台复制；平台差异通过 adapter/capability 注入。
- Web 必须使用当前 CLI 实例提供的页面/本地入口，不自行创建第二个 daemon 或第二个 durable inbox。
- 桌面壳可以调用包内 CLI，但页面不能直接获得宿主全部执行权限；窗口、更新、进程生命周期属于原生壳职责。
- 移动只访问授权主机的受认证会话入口，不执行本机 CLI，不要求远端模型 key，不声称已具备本地 daemon。
- 不能用屏幕宽度推断平台；能力由宿主显式提供，服务端仍需独立检查权限。
- 安装包、签名、更新、A→B 回滚和真机认证都是单独的交付门。组件构建通过不等于桌面/移动验收通过。

## 8. 开发、预览与验证

### 8.1 开发预览

首选 `hyprial gui dev`，按 [开发模式操作手册](dev-mode-user-guide.zh-CN.md) 使用自动稀疏工作树。必须：

- 在独立任务 worktree 中修改，不覆盖他人的 dirty/untracked 文件。
- 修改前先读适用的 `AGENTS.md`、本规范和组件目录。
- 明确当前 GUI_HOME 和预览 URL；热更新失败时检查目录/端口/编译错误。
- 开发期附加 UI 只允许独立浮动工具层（默认收起的右下浮动球，点击展开）；不在产品页面、产品组件或正式构建中添加任何开发专用 UI，不恢复常驻开发工具栏。
- 不运行 `reset`/`clean` 来“让页面更新”；不删除依赖或工作树规避问题。
- dev 预览不是安装包验收；fixture 预览更不是真实后端验收。

### 8.2 验证矩阵

按变更范围选择并记录：

| 变更 | 最低验证 |
| --- | --- |
| 纯文档 | 相对链接、示例与源码对应、`git diff --check`；不跑重型构建 |
| 文案/局部样式 | 相关页面在真实浏览器检查；必要时 typecheck；不影响逻辑时说明未跑全量 |
| 组件行为/ARIA | 组件测试 + 相关页面测试 + 键盘/焦点检查 |
| 页面状态/适配器 | 类型检查、状态单元测试、fixture/真实模式边界、错误与 unknown |
| 响应式/布局 | 受影响宽度真实浏览器检查；确认无横向溢出、输入不被遮挡、安全区正确 |
| 发布/打包 | 另按对应平台构建、安装、启动、更新和回滚流程验收，不能只跑 `npm run verify` |

`npm run verify`（typecheck + test + build）是 GUI 包的基线，不是全部验收。自动测试通过不能代替真实浏览器、真实后端、原生 WebView、安装包或真机。文档任务不应为了“看起来完整”跑无关的全量构建。

### 8.3 代码与测试纪律

- 修改行为先加/更新测试；至少覆盖成功、空、错误和 unknown 的相关边界。
- 不删除、跳过或弱化 token/responsive 契约测试来让改动通过。
- 保持 `min-width: 0`、`100dvh`、安全区、滚动稳定和 reduced-motion 等既有保护。
- 不引入跨平台共享的 `node_modules`/构建产物。
- 不在 UI 中打印完整环境变量、凭据、token 或原始错误对象。

### 8.4 PR 与交付

- 从目标分支的当前 head 建 `docs/*`、`fix/*`、`feat/*` 等短分支；提交使用 conventional commit。
- PR 说明动机、改动范围、验证命令与实测结果；未测项明确写 UNKNOWN 或 BLOCKED。
- 真实目标分支、合入权限和 CI 要求按仓库当前工作流执行；不要复用过期分支或旧规则。
- 改变已发布字段/事件/错误码语义时同步契约，否则如实声明 `Contract-Impact`；改变 agent 操作方式时声明 `Skill-Impact` 并同步相关 skill。
- PR 行为按仓库模板记录 `Submitted-by`，不冒用他人身份，不自行合入只允许独立审核的目标分支。
- 文档 PR 必须声明自己是否改变运行时契约；本文档本身**不改变**发布字段、HTTP、认证或 GUI 业务行为。

## 9. 产品设计、研发补齐与验收

### 9.1 所有权

- 产品负责人负责视觉、信息架构、交互和最终产品一致性。
- 研发负责真实能力、组件实现、状态边界、平台适配、可访问性与技术验证。
- 产品稿/原型中的交互不能自动证明后端存在；研发不得为了对齐截图而编造接口或成功状态。

### 9.2 研发补齐 UI

当缺失 UI 阻塞研发或验收时，研发可先补：

- 表单、按钮、校验提示、取消/重试入口；
- 必要布局、响应式、键盘和安全区修复；
- 明确的能力边界/错误/刷新入口。

但必须：

1. 保留产品原件及 hash；在可审查的派生/扩展层实现，不偷偷改归档原件。
2. 标注“研发补齐，待产品规范设计”，但不要用标识遮挡正常操作或当成验收豁免。
3. 使用稳定编号登记（如 `DEV-UI-001`），记录关联需求、页面/平台、实现路径、PR、验证、缺口和产品待办。
4. 集中交回产品统一设计；产品纳入后做回归并收敛临时补丁。
5. 不维护永久第二套页面，不因为“先补”而绕过真实能力和安全边界。

交接模板见 [UI 变更交接模板](ui-change-handoff-template.zh-CN.md)。研发已实现、已交接、产品已纳入、回归通过是不同状态，不能合并成一个“完成”。

### 9.3 验收分层

至少分别记录：

1. 设计验收：视觉、文案、交互和状态是否符合产品意图。
2. 技术验收：组件、类型、测试、键盘、响应式、构建。
3. 真实集成验收：真实登录/模型/Squire/聊天/历史等是否实际可用。
4. 平台验收：Web、桌面壳、移动 WebView/真机是否可安装、可启动、可恢复。

任何一层通过都不能替代另一层。fixture、mock、截图、模拟器和构建成功必须按实际范围标注。

## 10. 完成定义与 agent 执行清单

### 10.1 一个 UI 任务完成的最低证据

- 变更文件与目标一致，没有修改无关产品或本地规则。
- 使用现有组件/token，或新增组件已说明理由并进入目录。
- 相关状态（loading/empty/error/unknown）有可复现结果。
- 键盘、焦点、标签、live region 和颜色以外的提示按范围检查。
- 受影响的宽度/平台没有横向溢出、遮挡或安全区问题。
- 相对链接、文档示例和源码一致；验证命令与结果已记录。
- 未验证项明确写 UNKNOWN/BLOCKED，没有把 fixture 或占位结果写成真实成功。
- PR 申明 Contract/Skill 影响；研发补齐项已登记并交接。

### 10.2 Agent 执行清单

开始前：

- [ ] 读仓库 `AGENTS.md`、本规范、[组件目录](component-catalog.zh-CN.md)和适用的开发手册。
- [ ] 确认独立 worktree、当前分支/基线、GUI_HOME 和预览 URL。
- [ ] 检查 Git 状态，保留他人的修改；不 reset/clean 别人的工作树。
- [ ] 判断任务属于产品实现、文档、研发补齐、平台适配还是纯验收；不要混成一轮。

实现中：

- [ ] 先查现有页面、组件、token、测试，不从旧原型直接复制一套新实现。
- [ ] 页面不拼 CLI 命令、不访问文件系统、不存凭据、不伪造成功。
- [ ] 使用 `--ui-*` token 和 `.ui-*` primitives；旧页面迁移不混入无关视觉重写。
- [ ] 所有异步路径有 loading/empty/error/unknown 处理；没有新增“不可用/未接通/尚未开放”类页面文案（`unavailable-copy` 门）。
- [ ] 表单有 label/hint/error 关联；密码字段提交后清空；不记录敏感值。
- [ ] 键盘可完成主路径；Dialog/菜单焦点可进入、可离开、可恢复。
- [ ] 窄宽、长文本、空/大列表、错误/未知状态可正常展示。

交付前：

- [ ] 运行适用验证：组件/页面测试、typecheck、必要的 build/browser 检查。
- [ ] 检查相对链接和组件示例；文档变更运行 `git diff --check`。
- [ ] 检查 `git status`，只提交预期文件；不提交依赖、构建产物、凭据或本地规则。
- [ ] PR 写明精确 head、验证范围、未测项、Contract/Skill 影响和产品/研发补齐状态。
- [ ] 不自行合入只允许独立审核的目标分支，不冒用 verifier 身份，不改保护规则。

## 11. 参考入口

- [组件目录](component-catalog.zh-CN.md)：现有 primitives、共享组件、页面覆盖和源码链接。
- [UI 变更交接模板](ui-change-handoff-template.zh-CN.md)：产品/研发/agent 共用的交接和验收格式。
- [GUI 开发模式操作手册](dev-mode-user-guide.zh-CN.md)：启动、编辑、停止和常见问题。
- [开发者技术说明](../README.md)：包边界、命令和当前验证状态。
- 仓库级约束以根 `AGENTS.md` 和当前工作流为准；本规范不替代它们。


## 预览与正式入口一致性

页面与组件只维护一套。预览只在组合根替换数据/能力适配器等输入及独立的开发浮动工具，不另写页面。各线共享入口、例外状态与本轮调整见 [预览与正式入口共用 GUI](shared-ui-entry.zh-CN.md)。
