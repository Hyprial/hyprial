# dev-04 组织、邀请与成员

按组织交互设计（v1.1，OC4 对齐版）实现的第一版。组织相关的交互不只在组织页：开始页、Agent 页和聊天页里的组织入口也由本模块提供（设计第 3 节）。

- 设计：`gui/docs/org/interaction-design.zh-CN.md`（下称“设计”）与 `gui/docs/org/prd.zh-CN.md`，均为 v1.1（OC4 组织模型对齐版）
- 旅程：设计第 16 节 UJ01–UJ12
- 页面：组织页（8 个子页签）；开始页、Agent 页、聊天页的组织入口
- 后端：未接通。数据源接口 `OrgGateway` 已定义，真实适配器等后端
- 状态：第一版实现完成；第一轮独立评审（codex-gui-orgs-review）的 R1–R6 已修复，待复审。交接见 `docs/notes/handoff/gui-dev-04-handoff-2026-10-30.md`，修复记录见 `docs/notes/dev04-org-review1-fixes-2026-10-30/`

## 组织页的三种形态

`pages/OrgPage.tsx` 按组合入口给的数据决定显示什么：

| 条件 | 显示 | 说明 |
| --- | --- | --- |
| 挂了 `OrgDomainProvider` | 组织工作区 `OrgWorkspace` | 设计 v1.1 的组织页。没有数据源时没有组织入口，工作区不渲染；数据源没有实现的动作不渲染控件；被拒绝的动作只发通知 |
| 没有数据源，有 J5 模型 | `OrgCompatView` | 开发定位层的旧 J5 场景与联动会话；保持原单页布局，J5/J6/J7 契约照旧 |
| 都没有 | `OrgCompatDefault` | 产品默认；基线 DOM 不变（共享测试锁定） |

跨 tab 入口同理：`useOrgDomain().provided` 为 false 时一律不渲染，开始页、Agent 页和聊天页保持基线。产品入口目前不挂数据源；挂载和共享断言的更新由集成人处理（交接 §0.4）。

## 文件

| 目录 | 内容 |
| --- | --- |
| `features/org/impl/domain/` | 类型（快照、`OrgGateway`、结果）、`OrgDomainProvider` 与 `useOrgDomain`、选择器、文案（含中文句子拼接 `zh`）、未接通数据源、名称校验 |
| `features/org/impl/workspace/` | 组织页外壳：切换器、页头、子页签、侧栏、通用控件，以及新界面的样式表 `workspace.css` |
| `features/org/impl/sections/` | 8 个子页签；只读表格 `ConfigViews`；拓扑的展示模型 `topology-model.ts`、图与 Inspect `TopologyViews.tsx`、图的样式 `topology-views.css` |
| `features/org/impl/dialogs/` | 新建、修复、邀请结果、领取、移除成员、离开、改名、配置编辑与导入、文件、迁移单；编辑器退出保护 `useDiscardGuard` |
| `features/org/impl/crosstab/` | 跨 tab 组件：开始页提醒卡、Agent 组织徽标与筛选、Agent 详情组织分区、迁移向导、聊天被拒提示 |
| `src/dev/org/` | 开发数据、内存网关、开发情形、`OrgDevApp`；只被开发定位层和测试引用 |
| `pages/org/OrgCompatView.tsx` | 从 `OrgPage.tsx` 移出的旧视图（J5 兼容与产品默认），待集成人移除 |

## 组织页

页头是组织切换器（本版放在组织页页头，设计 Q03）。8 个子页签在窄屏改成下拉。

| 子页签 | 内容 |
| --- | --- |
| 概览 | 计数卡（成员、Agent、机器），点击跳到对应子页签；快捷动作；没有加入任何组织时显示空状态与引导动作 |
| 成员 | 人、Agent 两组。侧栏查看详情。所有者移除成员：先预览受影响的设备，预览变化时要求再次确认，提交后显示处理阶段 |
| 邀请 | 发出邀请（https 链接 + 加入命令，1 小时有效）；我收到的邀请（逐条领取）；用邀请链接加入（四步进度） |
| 机器 | 机器与连接状态；对休眠机器“立即重试”；“这台电脑”侧栏处理迁入请求：同意或拒绝；同意后、完成前可以取消 |
| 拓扑 | 结构视图：已采信配置里的部门汇报关系（分层）、业务线、常驻角色。通信视图：link（本机 Agent → 对端，带方向）与路由约定（静态，不画方向）两层。点节点或边在侧栏看 Inspect，关闭后焦点回到原节点或边 |
| 统计 | 时间窗口（近 1/7/30 天）、Agent 视角、每小时消息量、关系 Top 10、参与者 |
| 文件 | OrgFS 空间：编辑（版本冲突时先选择保留哪一份）、历史（按空间和路径区分）、删除进回收站、恢复；文件夹可以选中、移动或改名、连同子项删除和恢复；空间异常时只读 |
| 设置 | 基础信息与改名、结构与部门、通信权限、规范、版本（候选与采信）、危险操作（离开组织） |

角色只有所有者和成员（姚升悦决定）。组织均为工作组织；发出邀请、移除成员、设置组织配置由所有者操作（设计第 6 节）。

## 跨 tab 入口

| 位置 | 内容 | 接入（他人文件里的改动） |
| --- | --- | --- |
| 开始页 | 组织提醒卡（待处理邀请、迁入请求）；登录后的邀请确认（每次打开只提示一次） | `StartPage.tsx` 挂 `OrgReminderCards`、`PostLoginInvites` |
| Agent 列表 | 卡片上的组织徽标；按组织筛选（单独一行；选中的组织排第一并高亮） | `AgentList.tsx` |
| Agent 详情 | 所属组织汇总（头部事实行）；组织分区渲染在详情「关联」页签的组织卡内（implG4 前在详情底部）：所属组织（加入、移出）、通信（link 的添加与收回）、迁移（向导与进度） | `AgentDetail.tsx` 保留 `orgSections` 插槽并渲染进「关联」页签，`ContactsPage.tsx` 传入 `AgentOrgPanel` |
| 新建 Agent | 默认当前组织，不提供“不加入组织”（设计第 7 节） | `AgentForm.tsx` |
| 聊天 | 发送被拒：显示原因与修复入口（加入组织、邀请对方、查看通信权限），修复后手动重试；观察期只提示，不拒绝 | `AgentChatSection.tsx` 与 `AgentHistoryField.tsx` 提供 `renderAfterMessage` 插槽，`ChatPage.tsx` 传入 `ChatRefusalHint` |

## 状态与文案

- 没有数据源：没有“组织”入口，工作区不渲染（`domain/unwired.ts` 只是默认上下文与未登录壳层的值，动作返回不带文字的 `unavailable`）；`OrgGateway` 的 17 个动作是可选成员，数据源没有实现就不定义、对应控件不渲染；`unavailable` 结果由 `useOrgDomain()` 的网关统一转成通知。
- 写操作最多显示到“已提交，等待处理”。结果以数据源返回为准，界面不显示“已生效”。
- 配置发布只做交互：校验 → 发布候选 → 各节点 owner 采信。修改后需要重新校验；校验有错误时不能发布。
- 邀请链接是凭据：用密码输入框，提交后立即清空，不存储、不写日志。
- 数据源标为已跳过的邀请（本机已在该组织）显示“已跳过：原因”，不计入待处理数（`pendingInvites`）。
- 迁移进度以目标机器上报为准，不估算剩余时间。参与方在完成前都可以取消：目标方在迁移单里，源端在 Agent 详情里，都先在同一对话框内确认；失败时留在确认步骤显示原因，可以重试。已结束的迁移单只有“关闭”。取消迁移的确认框用“返回”退出，不用“取消”。
- 编辑器（配置、文件）有未保存的修改时，Esc、×、“关闭”都先在同一对话框内确认“继续编辑 / 放弃修改”；确认步骤里再按 Esc 等于继续编辑，回到原来的输入框。没有修改、或已发布且没有新改动时直接关闭。点遮罩不关闭对话框（共享 Dialog 的默认行为）。
- 配置编辑器的成员输入保留原始文字（刚键入的分隔符、输入法合成中的拼音都不改写），按中英文逗号或顿号解析，失焦后整理显示。
- 文案集中在 `domain/copy.ts`。含名字的句子和标题用 `zh` 拼接：中文挨着中文不留空格，中英文、数字之间留一个空格。“主人 姚”这类“标签 值”元信息保留空格作分隔。

## 开发场景

开发定位层工具条的“组织数据”下拉（`#dev-org-data`），默认“未挂数据源”，此时开发定位层与改动前一致。选中其他项时，用 `OrgDomainProvider` 包住 App，带上配套的 Agent 与聊天开发数据，并跳过首次设置。这个下拉由姚升悦决定接入（交接 §0.3）。

| 值 | 名称 | 内容 |
| --- | --- | --- |
| `off` | 未挂数据源（基线） | 不挂数据源 |
| `unwired` | 数据源未接通 | 挂数据源；没有“组织”入口，工作区不渲染 |
| `owner` | 工作组织所有者（完整数据） | hyprial-dev（所有者）、acme-lab（成员），带成员、机器、文件、配置、统计 |
| `member` | 工作组织成员视角 | 当前组织为 acme-lab；有待采信的候选 |
| `no-orgs` | 未加入组织（空列表） | 零个工作组织，引导建立或加入工作组织 |
| `invites` | 收到多条邀请（登录后确认） | 3 条网页上已接受的邀请，其中两条指向同一组织 |
| `partial` | 创建未完成，需要修复 | research-hub 创建未完成 |
| `template` | 新组织：从模板导入 | growth-team 还没有配置 |
| `migration` | 迁移源端与迁入请求 | 三个迁入请求：等待同意、迁移中（第一次取消会失败，用来走通重试）、已完成；研究者的迁出等待目标方 |
| `history-anomaly` | 空间历史层异常 | 团队空间历史层异常，只读 |

内存网关（`dev/fixture-gateway.ts`）模拟延迟、失败、过期和冲突，只改内存中的快照。

## DEV-UI 编号

按 UI 规范 §9.2 登记的研发补齐 UI：交互依据是 dev-04 交互设计 v1.1，状态为“研发补齐，待产品规范设计”。组件根节点带 `data-dev-ui`。

| 编号 | 内容 | 实现 |
| --- | --- | --- |
| DEV-UI-ORG-001 | 组织切换器与页头 | `workspace/OrgSwitcher.tsx`、`workspace/OrgWorkspace.tsx` |
| DEV-UI-ORG-002 | 新建组织 | `dialogs/CreateOrgDialog.tsx` |
| DEV-UI-ORG-003 | 修复创建未完成的组织 | `dialogs/RepairDialog.tsx` |
| DEV-UI-ORG-004 | 移除成员（预览、再次确认、阶段） | `dialogs/RemoveMemberDialog.tsx` |
| DEV-UI-ORG-005 | 发出邀请与邀请结果 | `sections/InvitesSection.tsx`、`dialogs/InviteResultDialog.tsx` |
| DEV-UI-ORG-006 | 用邀请链接加入 | `sections/InvitesSection.tsx` |
| DEV-UI-ORG-007 | 领取邀请确认 | `dialogs/ClaimInviteDialog.tsx` |
| DEV-UI-ORG-008、009 | 保留，未使用 | — |
| DEV-UI-ORG-010 | 拓扑：通信视图（link 与路由约定两层、Inspect） | `sections/TopologySection.tsx`、`sections/TopologyViews.tsx` |
| DEV-UI-ORG-011 | 统计 | `sections/StatsSection.tsx` |
| DEV-UI-ORG-012 | 文件与文件对话框 | `sections/FilesSection.tsx`、`dialogs/FileDialogs.tsx` |
| DEV-UI-ORG-013 | 设置、配置编辑、改名、离开 | `sections/SettingsSection.tsx`、`dialogs/ConfigDialogs.tsx`、`dialogs/SettingsDialogs.tsx` |
| DEV-UI-ORG-014 | 迁移单（目标方） | `dialogs/MigrationTicketDialog.tsx` |
| DEV-UI-ORG-015 | Agent 详情组织分区 | `crosstab/AgentOrgPanel.tsx` |
| DEV-UI-ORG-016 | 迁移向导（源端） | `crosstab/MigrationWizardDialog.tsx` |
| DEV-UI-ORG-017 | 聊天被拒提示 | `crosstab/ChatRefusalHint.tsx` |
| DEV-UI-ORG-018 | 开始页提醒卡与登录后的邀请确认 | `crosstab/OrgReminderCards.tsx` |
| DEV-UI-ORG-019 | 拓扑：结构视图（部门汇报关系、业务线、常驻角色、Inspect） | `sections/TopologyViews.tsx` |

## 实现约定

- 只消费 `components/primitives` 的公共 API。对话框内的选择用原生 `select`：共享 Dialog 处理 Escape 时不检查 `defaultPrevented`，Radix Select 在对话框里按 Esc 会连对话框一起关掉（接口提案见交接 §0.4）。
- 本构建的 Tailwind 工具类不在 cascade layer 中，和单类选择器同优先级时谁在后谁生效；覆盖共享基件时用双类或属性选择器（如 `.ui-select.org-select-inline`）。
- 窄屏（≤640px，设计第 12 节）：对话框铺满视口，只有正文滚动，操作栏固定在底部；侧栏全屏，左上角有“返回”；只读表格改成“字段名：值”；页签改下拉；元信息按段不断行（`MetaParts`、`Keep`）。
- 新界面的样式在 `workspace.css`；拓扑图在 `sections/topology-views.css`，按所在区域宽度排版（不足 520px 时上下排列，例如打开 Inspect 侧栏或窄屏）。类名前缀 `org-`。
- 由程序移入焦点的目标（tabIndex=-1）：对话框里的说明、步骤标题与状态文字不画焦点框，只让读屏从这里读起（框线会被正文滚动区裁掉）；对话框外的标题（页标题、侧栏标题、通信视图标题、Agent 详情分区标题）用与控件相同的焦点框。

## 边界

- 不改壳层、全局状态、运行时、全局样式、共享测试和构建配置（任务登记第 3 节）。开发定位层只按姚升悦的决定加了“组织数据”下拉，默认关闭。
- 机器、拓扑子页签显示组织数据，依据设计 §15.1 Q03/Q09/Q10（姚升悦决定）；dev-05 的文件未改。拓扑的交互沿用 J7（视图切换、选节点、点边打开 Inspect），图在本模块内画：J7 的边只表示运行时通信（传输中、已完成），表达不了 link 与路由约定两层，也没有节点 Inspect。没有 J7 模型时使用通用的“状态未确认 + 刷新”。统一到 J7 的接口提案见交接 §0.4 第 8 条。
- 开发预览与测试：业务请求 0，`localStorage`、`sessionStorage` 写入 0。
- 正式构建不含组织数据下拉和开发数据（`verify:production-copy` 0 个禁用标记）。
- 未接真实后端：组织列表、邀请、加入、移除、配置、文件、迁移都没有真实验收，结论为 UNKNOWN。

## 验证

- 单元（`tests/modules/dev04/`）：`org-domain.test.ts`（21）、`org-workspace.test.tsx`（20）、`org-crosstab.test.tsx`（15，含迁移取消）、`org-editing.test.tsx`（9：成员输入、编辑器退出确认、文件历史、文件夹）、`org-topology.test.tsx`（8）；既有 `j5-org-integration.test.tsx`（20）照常通过。`org-render.tsx` 是共用渲染工具，支持 StrictMode。
- 浏览器验收：原 `tests/modules/dev04/org-acceptance-browser.mjs`（UJ01–UJ12 × 1440/760/375，36 条）在基线上已因选择器等待超时而全部失败、不在任何工作流中，随 PR 1e 第一段删除（见 `docs/reference/known-problems.md` KP-063）；浏览器侧验收以 `org-request-browser.mjs` 为准；`org-lifecycle-browser.mjs` 与 `org-rework-browser.mjs` 已随 PR 1e-4 删除，其八类场景见 `docs/design/gui-single-launch-plans-2026-11-15/1e-4-stage-f-acceptance.md`。
- 全量：`npm run verify` 与任务登记 §12 的门禁；实测结果见交接 §0.2。
