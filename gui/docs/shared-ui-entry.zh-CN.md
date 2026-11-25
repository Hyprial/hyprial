# 预览与正式入口共用 GUI

本说明承接首次设置统一（#2168），覆盖其余页面。目标是让产品在预览中修改、验收的页面，就是正式入口使用的页面；不维护第二份预览页面。

## 页面与分层

| 页面 | 唯一展示入口 | 数据与操作来源 |
|---|---|---|
| 首次设置 | `pages/FirstRunPage.tsx`、`features/onboarding/` | 共享能力入口；开发场景或真实账号、模型、本机初始化能力；首次对话步复用共享聊天契约运行时（`features/agent-chat` 的 `ChatController`），发送被服务接收后自动完成首次设置 |
| 聊天 | `features/agent-chat/impl/AgentChatSection.tsx`、`impl/AgentTargetField.tsx`、`impl/AgentHistoryField.tsx`、`impl/ChatComposer.tsx`（公共入口 `features/agent-chat/index.ts`） | `ChatPage` 适配内存会话；`LiveChatPage` 适配真实聊天控制器；`ConnectedChatPage` 组合 1:1（`ConnectedChatController`）与群聊只读（`GroupChatController`，在 `conversations.get` 可用时经 `conversations.list(kinds: ["direct","group"])` 与 `conversations.history` 读取） |
| 平台接入 | `features/connections/PlatformIntegrationsSection.tsx`、`PlatformIntegrationDetail.tsx` | capability 注入正式 adapter 或 `dev/mock/platform-integrations.ts`；没有端口时整个模块不渲染，端口上没有的方法不渲染对应控件；平台身份明细只读展示记录（2026-11-19 起 IM 账号只是用户的可选信息，无 Hyprial 账号列，无“查询绑定”“配置绑定”面板） |
| Agent | `pages/ContactsPage.tsx`、`features/agents/` | 同一列表和管理组件；未接通时数量未知、创建禁用，不展示模拟 Agent |
| 组织 | `features/org/impl/workspace/OrgWorkspace.tsx` | `OrgDomainProvider` 注入数据；旧 J5 场景由 `dev/legacy-org-source.ts` 转换，不再使用 `OrgCompatView` |
| 机器、拓扑 | 组织工作区内的对应页签 | 使用现有数据源及兼容展示能力；组织未加入时不泄露预置机器数据 |
| 设备 | `pages/DevicePage.tsx` | 外部主 tab；组合当前设备、`LocalManagementSection`、升级、主机、在线诊断与日志；初始化经一次 `onboarding.initialize`（daemon 内准备设备），其余读取继续共用既有 capabilities |
| 设置 | `pages/SettingsPage.tsx` | 仅组合账号及设备/组织导航；不内嵌 provider、设备卡、daemon 状态卡或组织表单 |
| 开始、连接 | 对应 `pages/*Page.tsx` | 继续共用现有页面；本次未增加后端接口 |

控制器可以有多个，页面不能按预览/正式分叉。两个入口只在组合根选择不同输入（mock/real 数据源、能力适配器、控制器）；相同输入下产品 `#root` 的规范化 DOM、关键布局与操作结果一致。`firstRunWizardEnabled` 只由注入的 capabilities 决定，两个入口共用同一首次设置页与同一隐藏/路由拒绝逻辑，不检查构建模式。真实服务特有的授权链接、取消确认、消息结果未知、刷新和历史续载必须保留。模拟场景不能伪造这些服务已接通。

mock 组合集中在 `src/dev/parity-composition.tsx`；`src/main.tsx` 只按编译期入口标记动态加载它。产品 `App` 与 feature 公共入口不导入 dev/fixture，开发浮动工具独立挂载在产品 `#root` 之外。

浮动工具层与 fixture 组合自 2026-11-14 起分开：工具层是 `src/dev/tools/`（公共入口 `index.ts`），入口经动态 `import('./tools')` 取得 `mountDevTools(container, context)` 后挂载；“当前旅程 / 归档场景”场景浏览（账号与初始化预设、归档场景选择）与“本地测试数据”说明属于 fixture 组合，位于 `src/dev/fixture-tools/`，通过 `context.contributions` 交给工具层。工具层自身不导入场景、会话、mock 或 fixture 模块（`tests/dev-tools/import-graph.test.ts` 逐文件钉住其 import 图）。`context.dataSource` 为 `backend` 时没有 contributions 字段：工具层只显示模块索引，上述仅限模拟数据的工具不渲染、不附说明。本次只落地接缝：三种入口形态不变（正式 `dist` 不含工具层与 mock；开发预览为 fixture + 工具层；`dist-parity` 为 mock、无工具层），真实后端入口尚未挂载工具层。

2026-11-15 更新（PR 4b-G）：正式入口 `main.tsx` 在会话响应带 `devTools` 时才动态加载 `./dev/tools` 并以 `{ dataSource: 'backend', apiDocs }` 挂载；chunk 位于 `dist/dev-tools/`，`index.html` 不引用它，没有该能力时不请求、不渲染。`apiDocs` 为假时不渲染 API 文档入口，也不附说明。开发预览与 `dist-parity` 的组合不变。

## 团队修改规则

1. 在共享页面、组件、样式中修改视觉和交互；开发期场景选择、数据来源说明、会话工具与模块索引只存在于独立浮动开发工具层（默认收起的右下浮动球，点击展开，Escape/外部点击关闭），位于产品 `#root` 之外，不进入正式构建。产品内常驻开发工具栏、底部模块索引、导航 `devLabels` 与 `.dev-preview-frame` 随 2026-11-09 dev/product 等价改造退役，不得恢复。
2. 业务控制器负责操作状态、重试与结果确认；展示组件不直接调用 CLI、系统 API 或网络。
3. 真实模式不得注入开发会话、模拟 Agent 或模拟凭据。未提供的数据标记未知，不能转换为“已连接”或零条记录。
4. 新增状态同时检查开发场景与真实控制器。`unknown` 不等于失败，更不等于成功；保留原请求标识，避免重复发送。
5. 开发预览使用 `hyprial gui dev --gui-home <工作树>/gui`，正式入口在该工作树内运行 `hyprial gui`（不带 `--gui-home`）。修改均在自己的分支完成，目标由集成负责人统一收拢。 两个入口自 2026-11-16 起经同一个 Python 会话 host（`src/hyprial/shell/impl/gui/host`）：`hyprial gui` 由它提供构建后的 `gui/dist`，`hyprial gui dev` 由它反向代理 Vite dev server（Vite 不持有 backend 变量，已无 Node host 与 `npm start`），所以会话、`/daemon/*` 与同源规则只有一份实现。

## 本次交互调整，交产品同步

- 聊天统一使用现有共享聊天工作区。未连接时可以编辑草稿，但不能发送；失败时保留消息与草稿，重试原消息。
- 飞书统一为选择 Agent → 授权 → 切换渠道 → 查看结果。授权成功、绑定成功、在线是不同状态。
- 组织统一为工作区和页签；旧预览中直接铺开的成员、机器、拓扑改从页签访问。邀请码提交后清空敏感输入，失败可重新输入；不显示虚构加入成功。
- 正式 Agent 页面保留与预览相同的列表框架，未知时禁用创建；后端不可用不再替换成一段不同页面的提示。
- 模型服务未接通时显示“状态尚未确认”，不声称“没有模型服务”。普通入口首启固定走 `account → device → 普通 Agent 运行配置（DeepSeek 凭证保存或 configureAgent）→ conversation`；固定首启 Squire 准备/自动授权与模型连通性推断已移除，不把 grant 元数据或保存回执扩写为模型已连接。

## 验证入口

- `npm run typecheck`、`npm test`、`npm run build`、`node scripts/verify-production-copy.mjs dist`。
- `node tests/linked-preview-browser.mjs`：开发与生产构建的浏览器组合检查，覆盖窄屏、聊天草稿、飞书流程、组织/机器/拓扑以及无真实写入。
- J3–J7 的原浏览器命令保留，统一调用 `tests/support/shared-views-browser.mjs`。旧页面专属 DOM/按钮顺序不再作为契约；错误重试、授权、组织权限和真实控制器仍有独立组件测试。
- 浏览器页面通过不替代 CLI 启动、桌面安装、真实登录或真实聊天验收。分别记录实际执行范围。

开发模式在组合根保留聊天契约与历史 fixture 输入，但用户可见的初始化入口收敛为两个状态：
`?hyprialScenario=logged-out-init`（未登录 · init）和
`?hyprialScenario=logged-in-init&hyprialFirstRun=false`（已登录 · init）。浮动开发面板的“当前旅程”保留这两个状态，登录态使用空 Agent 管理基线，后续从 Agent 设置开始。

“归档场景”页签复用既有 J1、J2、J3、J5、J6、J7 与组织数据选择器，选择后点击“打开归档场景”在新标签页加载同一个 `App`。选项只修改归档草稿，不改当前预览的账号、Agent、凭据或聊天内存；URL 编码所有选择（包括 J3），刷新可恢复场景起点。归档是历史模拟数据，不冻结旧版 UI，不代表真实后端能力。J4/J10 的旧联动会话数据仍保留于 `remaining-scenarios.ts`，不伪装为当前 URL 组合已经支持的独立场景；旧 `#mock=startup` 入口不恢复。

这两个状态仍是隔离前端组合，不代表真实账号、daemon、平台接入、凭据或聊天已经接通。真实入口的账号/设备认证仍遵守 `gui-account-auth-layers-2026-10-29.md` 和当前 GeneratedClient 账号/设备链的隔离边界；未实测项目必须显示未连接、状态未确认或暂不可用。

> 2026-11-09 等价改造调整：场景选择、数据来源说明、会话工具与模块索引从产品内常驻工具栏迁入独立浮动开发工具层（默认收起，点击展开；Escape/外部点击关闭、键盘与焦点处理）。场景 key 及其 mock 语义保留为组合根注入的 mock 输入；切换场景重建 controller 的行为不变，但产品页面只响应新的 mock 输入，不感知工具层。旧工具栏 DOM 契约（`.dev-scenario`、`data-mock-scenario`、`j1` hash 初始选择）随常驻工具栏退役，浮动工具与双入口等价的交互契约以 `tests/dev-product-parity-browser.mjs` 与 `tests/dev-overlay-interaction.mjs` 的浏览器门为准（由集成验收确认落地）；浏览器门的 production 侧使用独立 `dist-parity` 测试产物（与正式产物一样 PROD 构建/DCE，仅组合根开显式 mock、不挂载开发工具），只用于等价测试，不是发布物；正式 `dist` 零 mock，产物门只扫描正式 `dist`。开发工具只改变 fixture 能力，不创建第二套业务页面，不进入正式构建。
