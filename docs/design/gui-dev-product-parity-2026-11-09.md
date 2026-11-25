# GUI dev/product 等价改造目标与集成计划

- 日期：2026-11-09
- 集成分支：`intg/gui-dev-diff-eliminate`
- 目标分支：`dev`
- 原始基线：`b4b0c1fe28fa4f5331de998ac7cfbc17cb922319`
- 当前 dev 合并基线：`59b62d98a2963f6bed4cab0daffc79fc57c837c0`
- 状态：实现完成；第 9 节记录实测结果，最终 Python/CI 状态以 PR #2612 当前正文为准

## 1. 目标

消除 GUI 开发预览与正式入口之间的产品代码、组件、路由和交互差异。开发预览与正式入口只允许在组合根选择不同的数据源、能力适配器和控制器；产品状态与能力差异必须由输入数据表达。

开发工具收进独立浮动球，默认收起，点击后展开。浮动球及其面板是唯一允许的开发期附加 UI，必须位于产品 `#root` 之外，不改变产品布局，且不进入正式构建。

## 2. 完成定义

同时满足以下条件才算完成：

1. 在相同 mock 数据、capabilities、路由和视口下，dev 与 production 的 `#root` 规范化 DOM、关键布局和操作结果一致。规范化只允许移除框架生成的 ID，不得忽略额外节点、文本、class、禁用状态或路由差异。
2. 产品组件、页面、reducer 和 view model 中不存在 `import.meta.env.DEV`、`__HYPRIAL_DEV_*`、dev-only import 或按构建模式选择产品组件的分支。
3. `firstRunWizardEnabled` 只由注入的 capabilities 决定。为 `true` 时两个模式都能打开同一个首次设置页；为 `false` 时两个模式执行相同的隐藏和路由拒绝逻辑。是否自动显示首次设置不得再检查 DEV。
4. fixture 文案、来源标记、初始页面、Agent、消息、组织及可用操作全部由 state、view model、capabilities 或 adapter 输入决定。场景切换不得选择另一套产品页面。
5. 常驻 dev 工具栏、底部模块索引、导航 `devLabels` 和改变产品布局的 `.dev-preview-frame` 全部移除。场景选择、数据来源说明、会话工具和模块索引只存在于独立浮动工具层。
6. 浮动工具支持默认收起、点击展开、Escape/外部点击关闭、键盘操作、焦点恢复、窄屏和 safe-area；production 不挂载开发工具 root。
7. 静态源码门、双入口浏览器等价门和正式产物门接入 `.forgejo/workflows/gate-gui.yml`。浏览器或产物缺失必须 FAIL，不得 skip 后报告 PASS。
8. 每个行为 WP 在其 PR 正文记录先失败后通过的测试名称和读数；最终在集成分支完整 HEAD 上重新验证，不能以各 WP 分支结果替代集成结果。
9. 所有 WP 已合入集成分支，集成分支相对最新 `dev` 可 fast-forward，最终 CI 和本地适用门禁满足本文件要求后，主 agent 才能去掉集成 PR 的 WIP 并通知用户合并。主 agent 不自行合并集成 PR。

## 3. 非目标与红线

- 不恢复旧 `src/gui/`、`app/` 或第二套 preview/live 产品页面。
- 不把 mock 登录、模型、消息、组织或首次设置结果描述为真实后端成功。
- 不改变 daemon、认证、已发布 JSON 字段、事件名或错误码；实施若发现契约变化，暂停相关改动并同步 `contract/` 或在 PR 中明确报批。
- 不读取或修改生产 daemon、真实凭据、用户级配置、生产 Lark App。
- 不通过删除断言、扩大忽略规则、隐藏额外 DOM 或跳过浏览器来制造等价结论。
- 不新增行内注释，不新增未钉定依赖，不绕过结构和模块边界门。
- 主 agent 不合并 `intg/gui-dev-diff-eliminate → dev`；该 PR 由用户在收到验收结果后处理。

## 4. 目标架构

### 4.1 单一产品组合

`main.tsx` 和 composition root 只负责选择 mock/real 输入并挂载同一个 `App`。产品组件不得感知构建模式。

首次设置、聊天、来源说明、QuickChat 等现有条件展示必须由数据和能力驱动。相同输入在 dev 和 production 必须走同一代码路径并产生同一产品树。

### 4.2 独立开发工具层

开发工具使用独立 root，与产品 `#root` 并列。默认只显示右下浮动球；展开面板采用非模态交互，限制最大尺寸并内部滚动，处理视口边界、键盘和焦点。

开发工具可以修改 mock 场景，但不得直接修改产品 DOM、路由或组件类型。产品只响应新的 mock 输入。

### 4.3 三层防回归门

1. 静态门：使用现有 TypeScript 工具链检查产品 import graph 和禁止的环境分支。
2. 浏览器门：两个入口消费同一 mock 输入，比较产品根 DOM、布局和相同操作序列的结果，并覆盖浮动工具交互。
3. 产物门：正式 bundle 不得包含开发工具、fixture composition、场景文案或 dev-only CSS。

浏览器门的 production 侧使用独立 `dist-parity` 测试产物：与正式产物一样执行 PROD 构建和 DCE，只在 composition root 开启显式 mock 输入，且不挂载开发工具。`dist-parity` 只用于等价测试，不是发布物。正式 `dist` 不得包含 mock、fixture URL 或其场景数据，产物门只扫描正式 `dist`。不得为了让浏览器门直接驱动正式入口而把测试 URL、mock composition 或场景数据打包进正式发布产物。

## 5. 并发工作包

每个 WP 使用独立 worktree、独立分支和指向 `intg/gui-dev-diff-eliminate` 的 WIP PR。子 agent 只修改自己的文件范围；需要跨 WP 接口变更时先在自身 PR 记录，不直接改其他 WP 文件。

### WP1 — 产品组合与首次设置

建议分支：`fix/gui-product-composition`

文件范围：

- `gui/src/main.tsx`
- `gui/src/App.tsx`
- `gui/src/app/AppShell.tsx`
- `gui/src/app/capabilities.tsx`
- `gui/src/state/**`
- `gui/src/components/Rail.tsx`
- `gui/src/components/RailFrame.tsx`
- 上述范围的单元测试

责任：统一产品组合，移除 DEV/连接态产品分支、devLabels 和首次设置环境逻辑，向其他 WP 提供稳定的 state/capabilities 接口。

### WP2 — 浮动开发工具

建议分支：`fix/gui-dev-floating-tools`

文件范围：

- `gui/src/dev-mode/DevPreviewApp.tsx`
- `gui/src/dev-mode/DevModuleIndex.tsx`
- `gui/src/dev-mode/DevScenarioSelector.tsx`
- `gui/src/dev-mode/SessionToolbar.tsx`
- `gui/src/dev-mode/dev-module-index.css`
- `gui/src/dev-mode/module-registry.ts`
- `gui/src/dev-mode/index.ts`
- `gui/vite.config.ts`
- `gui/src/vite-env.d.ts`
- 上述范围的单元测试

责任：只实现独立浮动工具及其公开挂载接口，不渲染产品 `App`，不修改 WP1 的入口文件。与 WP1 约定挂载接口和 mock 场景接口。

### WP3 — mock 数据与能力适配器

建议分支：`fix/gui-mock-composition`

文件范围：

- `gui/src/dev-mode/session/**`
- `gui/src/dev-mode/chat-scenarios.ts`
- `gui/src/dev-mode/j2-scenarios.ts`
- `gui/src/dev-mode/machine-scenarios.ts`
- `gui/src/dev-mode/org-scenarios.ts`
- `gui/src/dev-mode/remaining-scenarios.ts`
- `gui/src/dev-mode/topology-scenarios.ts`
- `gui/src/dev-mode/first-run-capabilities.ts`
- `gui/src/dev-mode/legacy-org-source.ts`
- `gui/src/fixtures/**`
- `gui/src/features/org/dev/**`
- 各业务现有的 `api/mock.ts` 与对应测试

责任：把场景差异迁移到 mock 数据、状态和能力适配器，不提供产品页面，不修改 WP2 的工具组件。

### WP4 — 页面与文案数据化

建议分支：`fix/gui-data-driven-pages`

文件范围：

- `gui/src/features/onboarding/**`
- `gui/src/pages/live/LiveChatPage.tsx`
- `gui/src/pages/ConnectedChatPage.tsx`
- `gui/src/features/agent-chat/api/**`（WP3 明确拥有的 `api/mock.ts` 除外）
- `gui/src/features/chat-channels/**`
- 上述范围的单元测试

责任：移除 onboarding/chat 的 DEV 和 demo 文案分支，以来源元数据、view model 和共享控制器驱动两个入口，保留真实失败、未知、重试和历史语义。

### WP5 — 静态 CI 与正式产物门

建议分支：`test/gui-dev-static-parity`

文件范围：

- `gui/scripts/verify-dev-product-parity.mjs`
- `gui/scripts/verify-production-copy.mjs`
- `gui/tests/production-copy-gate.test.mjs`
- 新增静态门测试
- `gui/package.json`
- `.forgejo/workflows/gate-gui.yml`

责任：实现静态门和产物门，登记 `verify:dev-product-parity` 与 `verify:dev-browser-parity` 命令并接入 CI。浏览器命令约定调用 WP6 的 `gui/tests/dev-product-parity-browser.mjs`。工作流文件的合入需要授权 maintainer 处理，主 agent 不越权。

### WP6 — 双入口浏览器等价门

建议分支：`test/gui-dev-browser-parity`

文件范围：

- `gui/tests/dev-product-parity-browser.mjs`
- `gui/tests/dev-overlay-interaction.mjs`
- `gui/tests/support/shared-views-browser.mjs`
- 新增浏览器 parity helper/fixture
- 明确要求旧常驻 dev 组件的浏览器测试

责任：实现相同输入下的 DOM、布局、操作和首次设置双态比较，以及浮动球和正式无工具检查。保留业务覆盖，不以截图代替交互断言。

### WP7 — 文档与旧问题状态

建议分支：`docs/gui-dev-product-parity`

文件范围：

- `gui/README.md`
- `gui/docs/ui-development-standard.zh-CN.md`
- `gui/docs/shared-ui-entry.zh-CN.md`
- `gui/docs/dev-mode-user-guide.zh-CN.md`
- `docs/guides/gui-dev-mode.md`
- `docs/reference/known-problems.md`

责任：使文档与最终单一产品组合和浮动工具一致，处理要求恢复常驻开发组件的旧条目。不得宣称未实测的真实 backend/native 能力。

## 6. 接口与合并顺序

2026-11-10 deep-module 跟进：已合并 `origin/dev@3dbea63c64e07057fbe46c7969c4a12fc93e9280`。#2631–#2634 是四个预合并范围迁移 Draft，最终内容由主 agent 在集成树统一复核。正式 feature 遵循 `index.ts + impl/`；mock、fixture、session 和开发组合统一位于 `gui/src/dev/`。`main.tsx` 只允许动态导入精确的 `./dev/parity-composition`，产品 `App` 不导入 dev/fixture。两个 mock 入口共享 `createFixtureState` 与同一个 `App`，浮动工具仍只在 `#root` 外。下列 WP 文件清单保留原始工作范围，当前路径以本段和 `gui/docs/module-boundaries.md` 为准。

2026-11-14 接缝跟进（“GUI 启动方式收拢为唯一路径”PR 4a）：浮动工具层与 fixture 组合分开。工具层为 `gui/src/dev/tools/`，入口经动态 `import('./tools')` 调用 `mountDevTools(container, context)`；“当前旅程 / 归档场景”场景浏览（账号与初始化预设、归档场景选择）、“本地测试数据”说明与开发预览 store 归 `gui/src/dev/fixture-tools/`，由 fixture 组合经 `context.contributions` 提供。`context.dataSource` 为 `backend` 时这些工具列为不可用并写明原因，不渲染可操作控件。本次不改变三种入口形态，也不改 `main.tsx`、`verify:production-copy` 与发布产物；运行时开关与随 wheel 发布属于后续 PR 4b。`gui/tests/dev-tools/` 钉住工具层 import 图、独立打包内容与真实后端组合下的降级行为。

2026-11-15 产物门跟进（PR 4b-G）：`verify-production-copy.mjs` 按三个范围判定正式 `dist`。(1) 产品闭包（`index.html` 与 `dev-tools/` 之外的全部文本文件）用完整禁用清单，且 `index.html` 不得引用 `dev-tools/`；(2) `dev-tools/`（`docs/` 除外）用同一清单减去工具层自身名称 `TOOL_LAYER_OWN`，即仍禁止 mock、fixture URL 与场景数据；(3) `dev-tools/docs/` 为第三方文件，不做文本扫描，但文件集合必须恰为 `swagger-ui-bundle.js`、`swagger-ui.css`、`LICENSE`、`NOTICE`，且 sha256 与 `node_modules/swagger-ui-dist` 一致。没有 `dev-tools/` 目录时门照常通过。

同日后续（owner 2026-11-14）：`backend` 组合下这些工具改为不渲染且不显示说明，`describeDevTools` 与不可用列表已删除。

1. WP1 先提供产品组合和 capabilities 接口；WP2/WP3/WP4 依据本文件并发开发，跨边界问题写入各自 PR。
2. 优先合入 WP1，再合入 WP3、WP4；每轮冲突解决后重跑受影响焦点测试。
3. WP2 在产品组合接口稳定后合入，确保工具层不再渲染产品 App。
4. WP5、WP6 的测试应从旧代码失败开始；功能 WP 合入后，在集成 HEAD 上转为通过。
5. WP7 最后核对文档与代码一致。
6. 每次 WP 合入后立即推送集成分支并更新集成 PR 的进度、精确 HEAD 和验证读数。

## 7. 集成验收

至少在最终集成 HEAD 执行：

```bash
npm --prefix gui run typecheck
npm --prefix gui test
npm --prefix gui run build
npm --prefix gui run verify:dev-product-parity
npm --prefix gui run verify:dev-browser-parity
npm --prefix gui run verify:production-copy
npm --prefix gui run verify
uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py
uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py
git diff --check
```

只有 `src/hyprial/` 中的文件发生变化时，才追加 Python 全量测试与 Ruff；`tests/`、`scripts/`、`pyproject.toml`、`uv.lock`、GUI 和普通文档变更不单独触发它们，显式手动 full validation 例外。CI 选择器必须对实际 diff 给出同样的选择；未选择 Python 时，pytest-shard 按既有聚合规则为 skipped，不把它当成测试通过。Python 工作流与 CI 选择器的焦点测试、GUI/CI 策略门和结构检查仍按变更范围执行。

浏览器至少覆盖 375、760、1440 宽度和首次设置 true/false 矩阵。真实 backend、桌面壳、移动 WebView、真实登录和真实消息若未运行，结果必须标记 UNKNOWN/BLOCKED。

## 8. 最终交付与停止条件

主 agent 只有在所有 WP 完成、最终 HEAD 验收满足本文件、PR 正文记录完整证据和未测范围后，才能：

1. 将集成 PR 标题改为非 WIP 的 ready-for-review 标题。
2. 推送最终集成分支并确认 PR 指向精确 HEAD。
3. 通知用户验收结果、剩余 UNKNOWN/BLOCKED 和 PR 链接，请求用户合并。

若浏览器 CI 无法准备、工作流合入权限不足、出现必须的公开契约变化或同一路由仍需两套产品组件，则保持 WIP 并报告 BLOCKED，不缩小验收口径。

## 9. 验收记录

- `15c1a743815274b6c0fa0c3d17c5021e10b2ae65` 的完整 `npm --prefix gui run verify`：PASS，169 个测试文件、1860 项 Vitest。`verify` 保持先构建再运行读取 `dist` 的测试。
- deep-module 七条边界规则全部 PASS，0 个新增例外；dev/product 静态门扫描 286 个产品源文件，正式 `dist` 为 0 forbidden markers。正式 `App` 只接收中性的状态、基线和能力输入，mock 组合限定在 `src/dev/`。
- 真实 Chromium 双入口等价 12/12 PASS，覆盖 375/760/1440 和 firstRun true/false；浮动工具 5/5 PASS；首次设置开发 4 个场景、正式宿主 4 个视口 PASS；safe-r1、J2/J3/J4/J5/J6/J7/J10、组织生命周期/rework 和 linked 全部通过。
- 预合并范围 #2631–#2634 已审查关闭，最终内容统一收回到本分支；#2641 已 merged。后续 actor-runtime 上游合并不改变 GUI 文件。
- 首次设置浏览器曾复现 session revision 重建 capabilities 导致控制器重置；按组织相关字段缓存投影后，最小真实浏览器复现及完整首次设置契约均通过。
- CI 选择器（#2646）只在 `src/hyprial/` 变化时选择 Python 全量；Ruff/compileall（#2645）只跟随该选择。当前 diff 不含 `src/hyprial/`，真实 `CI_PLAN` 为 `python=false`，不再要求全量 pytest/Ruff；选择器 156 项、Python 工作流与 compileall 接线焦点测试 184 项通过。未选择 Python 时 shard skipped 仍由 unit 按规则验收，不冒充测试通过。
- 本文档提交只记录实现与实测，不改变产品或测试逻辑。Python 选择器、适用 Python 工作流焦点测试和 CI 结果在 PR #2612 正文持续记录。真实 daemon 全功能、真实登录/真实消息、桌面壳、移动 WebView 和 safe-area 真机行为仍为 UNKNOWN。
