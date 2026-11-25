# dev-02 Agent 创建与管理

> 历史模块设计与场景记录，保留当时的说明。当前页面、术语和后端接线请查 [统一方法对账](../../../docs/design/gui-v0.1-method-plan-2026-11-13.md)，不按本文旧状态表继续补方法。

开发预览默认渲染连续会话（J1 → J2 → J5 → J6 → J7 → J10 → J4）；Agent 的消息渠道状态由会话的 `messageConnection` 单一投影，不再有 `channelState` 字段；下方的场景下拉只作定向回归注入，选中任一非默认值才切回旧组合。正式构建的 Agent 写操作只显示状态句：`Agent 创建暂不可用。`

- 旅程：J2（创建、列出、详情、编辑、删除 Agent）
- 页面：开始页入口、Agent 页、Agent 详情（同页展开，含「管理 Agent」面板）
- 状态：已实现，候选冻结后由独立 QA 与 Reviewer 审查
- 设计参考：`gui/docs/modules/dev-02-prototype.html`（三视图原型：列表 / 详情管理 / 创建表单）

## 行为

| 区域 | 行为 |
| --- | --- | --- |
| 入口 | 开始页“创建 Agent”、Agent 页“创建 Agent”都打开同一创建表单，不调用占位适配器 |
| 面包屑 | 列表视图只有页面标题；详情 / 创建 / 找不到视图的页头是统一面包屑（`nav aria-label="面包屑"`）：父级「Agent」为链接、点击回列表（含列表标题焦点恢复），当前级 `aria-current="page"`（详情为 Agent 名、创建为「创建 Agent」、找不到为「详情」）；页面标识保持视觉隐藏的 h1「Agent」。面包屑是子视图唯一回退入口：详情动作行与「找不到」面板不再提供「返回列表」按钮 |
| 列表 | 按**所有者分组**（「我的」在前，网络 Agent 按所有者单列成组）；**卡片 / 列表两种视图**可切换（列表行为紧凑行：名称、职责、所有者 · 主机、状态徽标）；卡片显示名称、职责、owner · host、org 标签、运行状态与可用状态；计数“显示 n / m 个 Agent”；搜索与筛选后的计数与条目数一致 |
| 筛选 | 可用状态 chips（全部/可用/需要连接/已停用）+ **所有者 / 主机两个 Select 过滤**（选项取自全量数据，含「全部」）；「清除筛选」重置全部维度；搜索语料含所有者与主机 |
| owner/host | 种子记录可携带 `owner` / `host`；缺省为自己的 Agent（所有者「我」、主机「本机」）。网络可达 Agent 的真实数据源（daemon 网络面）未接，当前仅能由种子/测试注入演示 |
| 空态 | 没有任何 Agent 时显示“还没有 Agent”，不显示 0 计数卡 |
| 详情 | 头部常显：名称、职责、运行状态徽标、停止 / 开始对话（及编辑 / 删除）；事实行：所有者、主机、harness · 模型、所属组织、消息渠道；下方四个页签：概览（需要注意汇总 / 最近活动 / 职责与能力 / home 基础）、凭据与授权（模型凭据逐服务商一行、Agent 自有秘钥、能力授权）、运行（启动/运行时事实、Routine 表、home 体量与迁移、Cell 预留卡）、关联（PAC 工作流、Adapter 预留卡、组织分区）；J3“管理 Agent”与浮窗“查看 Agent”都落到这里 |
| 开始对话 | 详情动作行首位「开始对话」（主按钮）：Agent 可用（模型凭据 connected）且存在同名聊天目标时启用，点击经既有 `open-chat` 动作跳转聊天页并选中该 Agent；否则禁用（不隐藏、不跳转） |
| 创建 | 名称与职责必填，就地错误并聚焦首个缺失字段；创建不依赖模型凭据（创建后到详情页授予）；服务商 / 模型 / 首选 harness 为可选偏好（对应 `agent create --provider/--model/--preferred-harness`） |
| 编辑 | 预填当前值，保存成功回详情；失败保留输入 |
| 删除 | 模块内 `alertdialog` 二次确认：标题、取消、确认、Esc 关闭、初始焦点在取消、关闭后焦点回“删除”按钮 |
| 找不到 | 详情 ID 不存在时留在 Agent 页，`role=status` 显示“找不到该 Agent”，经面包屑父级返回列表 |

## 详情页签（凭据与授权 / 运行；implG4 重构，原管理面板并入页签）

遵循 0.5.5.3 设计 `docs/design/provider-key-ownership-2026-10-29.md` §7（秘钥属于用户、Agent 只持 grant、界面永不显示明文）与 org 模型设计（卡片 org 标签）。

| 区 | 内容 |
| --- | --- |
| 凭据与授权 | 模型凭据每个服务商一行（provider、来源「我的秘钥 → 授权给此 Agent」、状态徽标：已授权 / 未授权 / 已撤销 / 等待原主人设备 / 解封失败 / 状态未确认、授权时间——agent.secret.list 未提供时间时显示「—」、行级动作）；provider 单元格补充授权详情（主人 · keyId 尾号 · revision）。Agent 自有秘钥（agent-private/agent-shared）只显示名称与 revision，永不显示值。能力授权只读列表；无数据时“暂无授权记录，状态未确认”。 |
| 运行 | 启动经生成 client 调 agent.start（retry=never，不自动重试；结果未知只提示核对）；每个声明错误码有专属文案，AGENT_ALREADY_RUNNING 展示运行绑定（harness · runtime · 会话 · 绑定时间），launch warnings 原样透出；agent.start 未接线时（无目录 api）回退「启动 · 暂不可用」禁用态，零请求。停止在详情头部（接线 agent.stop）。运行时事实两项：**实际 harness** 与**正在使用的模型**（无数据源时显示「状态未确认」）。Routine 卡：routine.list 按 Agent 过滤出表（名称/周期/下次运行/上次结果/状态），行级「详情」惰性读 routine.status（调度事件与结果历史）；home 体量与迁移：agent.home-census 按 Actor 过滤，布尔事实组合成体量（就绪/配置/cwd 位置）与迁移（托管模式/接收迁移）两行；方法未接线时整卡回退暂不可用且零请求。Cell 仍为设计预留卡。 |
| 概览 | 需要注意（汇总凭据异常与 link 异常，带页签跳转；无异常显示“暂无需注意”）、最近活动（只用 agent.list 已有事实，无数据源时暂不可用槽位）、职责与能力、home 基础（工作目录 / 最近修改来自 agent.list，体量/迁移见运行页签的 home-census）。 |
| 关联 | PAC 工作流卡接线 workflow.list（agent=当前 agent，页签激活时读取；running/completed/failed/cancelled 给中文标签，未知值原样透出并标注未确认；当前节点由 currentNode 投影，已关闭且无当前节点显示已结束）；Adapter 与路由仍为设计预留卡（无公开方法，暂不可用零请求）；组织分区复用 dev-04 AgentOrgPanel。 |

- 2026-11-22 退役说明：`link.list` 与对应 Link 表已随 T2b 删除，下文相关描述仅作历史记录，不得新增调用。
- 2026-11-14 修订：页签随能力出现——平台绑定需要 adapterBindings，凭据需要 agents api，所属组织需要组织分区；没有对应来源的页签不渲染。运行页签与设计预留卡（Routine / home 体量 / Cell / PAC 工作流 / Adapter）从未挂载，已连同读取 hook 删除；上表「运行」「关联」两行描述的是已删除的实现，仅作历史记录。
- 2026-11-14 验证修订：发出前就知道被方法目录拒绝的读取不是失败也不是空数据——`agent.list` 被拒绝则没有 Agent 来源，入口被隐藏；`user.provider.list` / `agent.provider.allow` 被拒绝则创建表单与凭据页签没有 Key 来源，`agent.provider.login` 被拒绝则没有 Auth 来源，两者都没有则不渲染凭据字段；`link.list` 被拒绝则没有连接记录块；`agent.provider.status` 被拒绝则「需要注意」块不渲染（没有证据就不声明“暂无异常”）；`agent.provider.auth.list` 被拒绝则没有 Auth 清单。创建入口只有一处检查：外壳 dispatch 在 `open-agent-create` 进入 reducer 之前查 `agent.create`，被拒绝只发一条通知，不打开表单也不跳转；开始页的「创建 Agent」只在 Agent 页可用时渲染。重命名、启动/停止、保存凭据、移除登录文件、绑定/解除绑定在打开编辑器、对话框或开始忙碌之前先查方法目录。injected client 缺 handler 的 api 成员不存在，对应控件不渲染。已发出的读取失败仍保留失败卡片与重试。
- 2026-11-15 合并修订：凭据页签的 Auth 读取端口取自 `capabilities.nativeAuth`，只随 `agent.provider.auth.list`；`user.provider.list` 被拒绝（没有 `sharedCredentials`）时仍有 Auth 清单、「使用已有 Auth」与完成前重读，且不读取 Key 清单。
- 没有来源的操作不渲染（无网关无创建，网关无 update/delete 则无编辑/删除，无 agentDelete 则无删除）；有来源但被目录拒绝的操作保持可用，用户操作时发出通知（`创建 Agent暂不可用。` 等），页面内不再有解释「暂不可用」的文案。停止已接线（agent.stop，retry=never）。
- 凭据缺失（确认无授权记录）时空态显示「还没有模型凭据…」并附「去连接页登录」文字链接跳「连接」页；grant 为 ok / revoked / recipient-pending / unseal-failed 时不显示绑定引导（wired 路径的已授权是 inventory 语义，不证明凭据可用，G1-1）。
- 运行状态、provider grant、能力 grant、实际 harness/model 由目录读取或私有扩展 seed 注入（`features/agents/impl/agent-extensions.ts`），无数据源时全部显示「状态未确认」；Squire 预配置徽标按 fixture id `agent-squire` 识别。
- **接口提案（待集成人）**：扩展 seed 目前只能由测试直渲染 `ContactsPage` 注入；需要集成人把 `agentExtensions` 沿 `App → AppShell` 传入（与 `agentSeed` 同型），开发预览才能演示在线 / 已授权等注入态。
- **接口提案（待集成人，场景种子）**：开发预览默认体验应为「Squire 预配置好、归属个人组织」。现状差距：连续会话 J1 完成后 Squire 已带模型与渠道（`session-reducer.ts` squire 步），但 `SessionAgent` 无组织字段、且 J5 之前 `session.org` 为 null → 详情显示「未加入组织」；基线 fixture 的 Squire 渠道显示「状态未确认」。请求：在 `dev/session`（或基线 fixture，由集成人裁定落点）注入「个人组织」（如 `org-personal` / 「个人组织」），并让 Squire 自 J1/J2 起归属之；组织页在 J5 团队组织出现前的显示口径由 dev-04 协调。本 PR 不改 `dev/**`（track 规则）。

## 登录服务商流程（研发补齐）

连接页「登录服务商」打开页内对话框：provider（claude/codex/pi）→ 凭据方式（本机 CLI OAuth / 粘贴 API Key）→ 保存并校验（三段内存延迟）。依据 mvp-required-flows.md M1 与 #2077；API Key 只在输入框与一次提交中短暂存在，不落存储、不打日志。生产构建网关固定 unavailable（提示「凭据操作暂不可用。」），绝不显示成功；开发/测试走内存网关，成功后 provider 进入列表（本地条目退出登录为纯内存移除，零网络）。

## 统一通知

- **Toast 队列**（`components/ToastHost.tsx`）：所有动作反馈入队（最多 4 条，去重键为文案+来源），info/success 4s 自动消失，warning/error 常驻；容器为唯一 `role=status` live region。
- **通知中心**（`components/NotificationCenter.tsx`，Rail 底部铃铛）：铃铛为无边框纯图标，位于「我的账号」触发器旁（`rail-foot` 同排）；有未读时仅显示无数字小红点（数字保留在 `aria-label`「通知中心，N 条未读」供读屏）。warning/error 落历史（最多 20 条），打开清零，可清空；空态「暂无通知，状态未确认」。条目显示来源行：操作类通知为操作 id（如 `connection.adapter.manage`）、登录流程为 `provider-login`；旧 `set-notice` 镜像与未知来源诚实显示「来源未确认」。事件源接入（`provider.grant.blocked`、`org.invite.pending` 等）等后端事件面。
- 旧 `set-notice` 单槽横幅保留兼容：reducer 镜像进 toast 队列（含「暂不可用/操作失败」文案落中心），`confirm-logout` 亦镜像。
- `useStubAction` 与占位适配器已随 PR 1e 删除（无调用方）；不可用提示由各控件经 `push-notice` 直接给出。

## 状态映射

Agent 可用状态只由模型凭据连接推导（GUI 无“模型服务”概念，产品只有 agent / harness / model 三级）：

| 模型凭据连接 | 状态 |
| --- | --- | --- |
| connected | 可用 |
| unconfigured | 需绑定模型凭据 |
| disabled | 已停用 |
| 缺失或未知 | 状态未确认 |

渠道：connected → 已连接；unconfigured → 未连接渠道；缺失 → 状态未确认。

基线数据里 fixture 明确标记为已停用的 Agent（运营执行）在没有绑定模型时仍显示已停用；这是既有 fixture 语义，不新增状态来源。

## 开发场景

| 场景 | 内容 |
| --- | --- | --- |
| 基线 | 不注入种子与网关，写操作只返回“接口未接通” |
| M-04 | 3 张卡片，模型已连接，渠道分别为已连接、未连接渠道、状态未确认 |
| M-05 | 空列表与创建入口 |
| M-06 | 1 个 Agent，模型与渠道均未配置，没有已连接模型，创建提交禁用 |

创建成功由 M-04 / M-05 内的本地网关产生；失败与未知结果没有场景编号，只由测试注入的网关覆盖。选择器只提供上述 4 项，不新增编号。

## 边界

- 默认网关固定返回不可用，不产生成功文案。
- 开发预览与测试网关都在内存中工作：业务请求 0，`localStorage` 与 `sessionStorage` 写入 0。
- 正式构建不含 J2 场景选择器、Mock 文案与开发样式；组织页与 Agent 页在首次设置完成前都不可达。
- 未接真实后端，未做 J4 / J6 及以上旅程；readiness / move / restore / secret 等高级运维不在本轮。
- 组件只消费 `components/primitives` 公共 API；标准按钮与卡片外壳已迁 Button/Card 基件（#2080 shadcn 适配层，保留旧 className 布局钩子；`contacts-card`/`connection-card` 用 `padding="lg"` 维持 24px）。表单单选下拉已迁 Select 基件（Radix，2026-10-29 dev-02 批次；来源与本地修改见 `gui/docs/shadcn.md`）：创建表单的首选 harness、编辑表单的所属组织与登录服务商对话框的 provider 选择；创建时不选择组织。保留原生元素的例外：filter-chip / contact-card（非标准按钮形态）、AgentDetail 外壳 `section`（共享选择器以 `[data-selected="true"]` 定位，primitives Card 会覆写该属性）与删除确认框两个 ref 焦点按钮。

## 验证

- 单元：`tests/modules/dev02/j2-agents.test.tsx`（29 个用例）、`tests/modules/dev02/agent-manage.test.tsx`（14 个用例：扩展维度、管理面板状态、占位动作不显成功、引导跳转、表单新字段）、`tests/contacts.test.tsx`、`tests/app-shell.test.tsx`、`tests/dev-preview-scenarios.test.tsx`。
- 浏览器：`tests/modules/dev02/j2-browser-contract.mjs`，6 视口 × 开发/生产。
- 全量：`npm run verify` 末尾包含 `verify:j2-browser`。
