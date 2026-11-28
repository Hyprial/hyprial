# GLOSSARY.md — Harness Bridge 词汇表

本仓库的**规范术语表**。写代码、文档、提交信息、契约语料时以本表为准；
提新术语前先查"退役/易混淆"列。命名裁决史见 docs/design/design.md §2 与
docs/reference/naming-audit-channel-adapter-route.md；组织与会话模型见
docs/design/cell-thread-conversation-model-2026-11-03.md（11-03 的原始设计；与 2026-11-16/17
裁定冲突处以本表为准）。

锚点为仓库内真实路径（2026-11-03 按深模块布局校正）；`src/hyprial/` 下的产品代码在
`kernel/identity/daemon/biz/shell` 五个域的 `impl/` 中。新增公开术语先进本表。
「组织与会话模型」一节的锚点由 `scripts/ci/check_glossary_anchors.py` 门禁：每个锚点是
仓库内存在的路径或 `路径::符号`，多个以「、」分隔，可带一个括注，`同上` 继承上一行。

## 产品与架构

| 术语 | 定义 | 退役/易混淆 | 锚点 |
|---|---|---|---|
| daemon | 持有传输会话、durable inbox、路由与受管运行时状态的长驻进程；消息与恢复的权威边界 | 不是"server"（去中心，无中心服务器） | src/hyprial/daemon/ |
| host | 运行一个 hyprial daemon 的环境（一个 hyprial home + 其 daemon）；agent 住在 host 上、在 host 之间 move，host 决定是否接纳外来 agent。用户可见的唯一称呼 | ~~node~~、~~machine~~、~~device~~（均改为 host，Allen 2026-10-27）；只作名词，不作动词；不等于网络主机名 | docs/design/daemon-request/handoff-2026-10-27.md §7 |
| harness | 运行 AI agent 的宿主程序（Claude Code、Codex、Pi 等） | 不指本仓代码本身 | src/hyprial/daemon/impl/harnesses/ |
| model connection | 模型连接：本机 Secret 内稳定引用、endpoint、协议、认证字段与配置修订；模型 ID 由调用方明确提供 | 不是模型目录或 Harness；不替代 grant | docs/design/model-connections-contract-stage-2026-11-24.md |
| connector | 把一个 harness 会话接到 daemon 的进程管理+注入层 | ~~proxy~~（语义偏同协议转发，已弃用） | design.md §2 命名词表 |
| adapter | 外部平台（Lark 等）接入，映射平台身份到统一寻址/投递模型 | ~~gateway~~、~~room~~（已退役） | src/hyprial/daemon/impl/adapters/ |
| user settings | 用户设置：当前账号的信息及登录、退出操作 | 不包含本地人员目录、平台身份映射管理或 Agent 凭据；设备和组织使用各自主导航入口 | gui/src/pages/SettingsPage.tsx、gui/src/features/account/impl/AccountLoginSection.tsx |
| local account logout | 退出本机登录：`auth.logout` 清除当前 Hyprial 实例的账号登录凭据和身份记录 | 登录后未初始化也可退出；不退出 Casdoor 授权网站的浏览器会话，不删除设备或 Agent 凭据；已运行的 daemon 会停止。再次登录重新发起授权，网站会话有效时可能不再要求密码 | src/hyprial/identity/impl/onboarding/logout.py |
| account login state | 账号登录状态：成功读取 `onboarding.status` 后显示“未登录”，已登录时仅显示账号邮箱 | 无法读取不等于未登录；等待授权、登录结果丢失、本机服务连接失败分别说明原因，不用“结果未确认，不视为已登录”作为通用提示 | gui/src/features/account/impl/connected-account-actions.ts、gui/src/features/account/impl/AccountLoginSection.tsx |
| device initialization | 初始化当前设备：已登录账号准备设备身份（`onboarding.device.ensure`），再启动初始化（`onboarding.initialize`），由状态确认 `initialized=true` 且进入 serving | `deviceReady` 仅表示设备身份已准备，不等于整个初始化完成；这里的设备身份不替代 host 的运行环境概念；退出登录后 GUI 不展示本机数据，不表示删除设备密钥；当前重新登录进入 bootstrap 后仍需初始化以恢复 daemon，同账号同设备复用设备身份，不承诺运行任务自动续接 | gui/src/features/settings/impl/device/device-actions.ts、src/hyprial/daemon/impl/application/onboarding/ |
| until | `auth.login.begin` 的可选参数：`authenticated`（缺省，与以往逐字节相同：客户端自己轮询 `auth.operation.get`，GUI 现行行为）或 `serving`（登录由 daemon 自己驱动：轮询发行方、登记设备、准备用户密钥、接受初始化，并在同一进程内升为 serving；客户端不需要轮询进度）。只在无账号的 bootstrap daemon 上有效，serving 或环境指定 owner 的 daemon 答 `IDENTITY_CONFLICT`；已有客户端轮询驱动的登录时答 `OPERATION_IN_PROGRESS`，已有 daemon 驱动的登录时不论 operationKey 都返回同一个操作（同一验证网址与代码） | 不是新方法，也不是初始化的别名；配套的 `auth.operation.get.waitSeconds`（0–900 秒，只对 daemon 驱动的登录有效，只在 daemon 自己的 wire 上生效、HTTP 投影不含该参数，停放时不占 daemon 的调度锁也不读日志，跨过监听器切换后以 `serving`，失败或 daemon 退出时以 `stopped` 作答）不是客户端轮询的间隔 | src/hyprial/daemon/impl/application/onboarding/first_login/driver.py、src/hyprial/daemon/impl/request_catalog/onboarding/schemas.py |
| completion | 登录操作与 `auth.login.begin` 结果里的可选对象，只出现在以 `until: serving` 发起的登录上，读取该操作的任何客户端（例如 GUI 轮询 CLI 发起的登录）都看得到：daemon 对这次登录的进度投影，`stage`（`authorizing`、`device`、`starting`、`serving`、`stopped`）、`identity`、`device`（`ready`、`deviceId`、`keyGeneration`、`userKey{state,code}`、`error{code,message}`、`retry`）、`failure{code,message}`。它是登录结束时取的快照，不是设备的实时视图，之后设备以 `onboarding.status` 为准；不含 pid 与 epoch（要这两个值，在 `stage` 为 `serving` 后读 `ping`）。`device.ready=false` 时登录仍成功，`retry` 为 `serving-start`：daemon 在每次 serving 启动时做一次恢复尝试（2026-11-16 决定），包括这次登录所导向的这一次启动（promote 之后的启动现在也运行恢复），没有进程内重试循环，也没有别的重试；初始化回执留在 `promoting` 直到设备登记完成 | 不是第二份 durable 状态（不入日志，权威仍是登录、设备、初始化三份回执）；不含设备码、令牌、任何密钥与异常文本（错误只有代码与按代码固定的文案）；`stopped` 表示这次驱动在未进入 serving 时结束，原因看登录 `state` 与 `failure` | src/hyprial/daemon/impl/application/onboarding/first_login/completion.py、src/hyprial/daemon/impl/application/onboarding/lifecycle/recovery.py |
| local status | 本机状态：设备页合并展示初始化结果、本机服务运行阶段和版本 | 版本来自 `daemon.status.version`，表示正在运行的程序版本，不表示该版本已发布；替代“当前设备 / 本机服务与维护”重复卡片；不据此推导外部网络、组织或 Agent 健康状态 | gui/src/features/settings/impl/LocalManagementSection.tsx |
| service update status | 服务更新状态：自动升级调度器、计划时间、最近执行及配置迁移记录的只读观测 | 不是新版本发现或手动升级入口；迁移记录完成不表示已安装最新版本 | gui/src/features/settings/impl/UpgradeStatusSection.tsx、gui/src/features/settings/impl/UpgradeStepsSection.tsx |
| daily update schedule | 每日计划：daemon 自动升级调度器按本机时间安排的触发时刻，当前为 03:17 / 15:17 | 有计划不等于开启自动升级；执行仍检查 `autoUpgrade`；GUI 暂无设置入口 | src/hyprial/daemon/impl/autoupdate/__init__.py |
| legacy launcher process count | 系统范围旧进程数：`upgrade.steps.status` 返回的旧 Hyprial MCP 启动命令进程条目数量 | 来自实时 `ps` 扫描，不是日志、不代表所有旧版本进程；不限当前 home；扫描失败目前也返回空数组 | src/hyprial/daemon/impl/upgrade/steps/mcp_entry.py |
| GUI login prerequisite | GUI 登录前置：账号状态确认后才挂载业务内容及其读取控制器，退出后移除；未登录业务页提示先登录 | 本机服务会话不等于账号登录；界面门槛不替代后端授权；账号未知不能当作已登录 | gui/src/app/AppShell.tsx、gui/src/features/account/impl/useAccountSettings.ts |
| runtime diagnostics | 运行诊断：`top.snapshot` 的观测时间、Agent 条目数和滞留收件人条目数 | 替代设备页“在线诊断”；条目数不等于在线数、异常数或健康结论 | gui/src/features/settings/impl/diagnostic/api.ts |
| log event summary | 日志事件摘要：`log.page` 返回的时间、级别、组件和事件安全投影 | 每页 20 条，沿 cursor 翻页，显示页码和上一页/下一页；不宣称已知总页数；不包含消息正文，按文件分页而非全局时间顺序；adapter 组件称“平台接入” | gui/src/features/settings/impl/logs/LogsSection.tsx |
| platform integration | 平台接入：用户面的 adapter 生命周期模块（添加、授权、启动/停止、身份同步、删除） | 不是 channel；channel 是协议层群组会话空间；技术面仍用 adapter；“使用已有应用”经 adapter.register 保存配置，不表示凭据验证、权限授权、启动或 Agent 绑定 | gui/src/features/connections/PlatformIntegrationsSection.tsx |
| model credential | 模型凭据：Key 为模型服务的 API Key；Auth 为 harness 的原生登录材料；Agent 设置中的栏目称“凭据设置” | 保存 Key 不等于已授权给 Agent，也不等于模型请求已验证成功；本页 Key `active` 显示“已保存”，不作健康结论 | gui/src/features/connections/CredentialsSection.tsx、gui/src/features/connections/shared-credentials-api.ts |
| model service endpoint | 服务地址：自定义模型服务的 API 基础地址，技术面称 endpoint / base URL | 不是模型名、harness 或飞书消息路由；共享 GUI 将地址与用户 Secret 一起保存，授权后由 Pi 后台 deepseek/deepseek-flash 消费；临时测试成功不等于已保存或启动 | contract/identity/model-service-routing.md、src/hyprial/daemon/impl/harnesses/model_provider.py |
| credential test | 凭据测试：使用指定服务商或服务地址、协议、Key 与测试模型发起一次最小模型请求 | 共享 GUI 消费 `agent.secret.provider-test`；测试不保存 Key、配置或测试模型、不创建 Agent/会话、不自动重试；HTTP 200 本身不足以证明通过，只有协议内模型文本可记 passed；不证明 Agent 可启动 | contract/daemon-request/credential-probe.md、src/hyprial/daemon/impl/model_probe |
| model API protocol | 兼容协议：模型服务使用的具体 API 格式，如 OpenAI Chat Completions / Anthropic Messages | 不是服务商或 harness；当前自定义 Key 保存和启动仅支持 Pi 后台 deepseek/deepseek-flash 的 OpenAI Chat Completions，探针支持范围不扩大启动范围 | gui/src/features/connections/credentials-view-model.ts、contract/identity/model-service-routing.md |
| Pi model provider | Pi 模型服务商：Pi 能识别的 provider ID，配合该服务商的模型 ID 与明确授权的凭据使用 | Kimi Code 为 `kimi-coding` / `KIMI_API_KEY`，Moonshot AI 为 `moonshotai` / `MOONSHOT_API_KEY`；不等于任意 endpoint 都可用；Claude/Codex 不因此获得相同兼容范围 | gui/src/features/connections/credentials-view-model.ts、src/hyprial/daemon/impl/harnesses/model_provider.py |
| adapter configuration reload | 重新加载全部配置：调用无参数的 `adapter.reload`，使 daemon 增量重读全部平台接入配置 | 区别于“刷新”（`adapter.list` 只读列表）；有接入或注册结果待恢复时显示在列表底部，不属于单条接入操作；不表示重新授权或创建应用 | gui/src/features/connections/PlatformIntegrationsSection.tsx、src/hyprial/daemon/impl/request_catalog/adapter/lifecycle.py |
| platform authorization operation | 平台授权操作：申请并等待平台管理员批准的一次操作 | adapter.authorization.begin/operation/cancel；操作取消只停止本次等待，不撤销已有平台权限；重启后的未终结回执为 unknown，只核对、不自动重新申请 | src/hyprial/daemon/impl/adapters/lark/credentials/authorization/service.py |
| platform app creation | 创建新应用：通过平台授权页面新建应用，并把得到的应用凭据保存为本机平台接入 | adapter.onboard.begin/operation/cancel；保存、加载和启动分别确认；取消只停止本地等待，不删除远端应用；configured=false 仅表示尚未确认保存，不能据此推断无远端或本地效果 | src/hyprial/daemon/impl/adapters/lark/credentials/onboard/service.py |
| platform permissions | 平台权限：平台实际授予的应用能力；按后端能力 ID 原样展示已授权能力和缺少的必需能力，不维护中文名称映射 | 由 adapter.authorization.status 独立读取；checkedAt 为 Unix 毫秒；`capabilities` 是 message-send 等能力 ID，不是 im:message 等原始 scope；doctor 另区分必需/可选、未声明/已声明未批准；状态未知不当作未授权 | src/hyprial/daemon/impl/adapters/lark/credentials/authorization/inventory.py、gui/src/features/connections/PlatformIntegrationDetail.tsx |
| platform identity sync | 同步平台身份：读取应用、机器人和可见群成员，将观测记录写入本机身份存储 | 不发送聊天消息、不授予 Hyprial 账号权限，也不等同于 Agent 平台绑定；显示同步状态与记录；`observed` 显示“已采集”，不表示等待本页人工审核；`verified` 沿用已有记录的“已核验”，同步本身不会将记录升级为 verified | src/hyprial/daemon/impl/request_catalog/adapter/identities.py、src/hyprial/daemon/impl/adapters/lark/credentials/identities.py、gui/src/features/connections/PlatformIntegrationDetail.tsx |
| platform identity lookup | 已退役：GUI「查询绑定」「配置绑定」两个面板与身份绑定列曾经通过 `identity.resolve`、`identity.override.set` 查询、配置平台账号对应的 Hyprial 用户；daemon 一侧的方法、解析器与绑定库已随 IM 绑定清除 R3 删除，GUI 面板的删除是 GUI 线的后续工作 | hyprial 不记录谁拥有哪个 IM 账号：IM 上的发送者就是拥有该账号的人；只有名字的人员登记见 `hyprial user add\|show\|list` | gui/src/features/connections/impl/platform/PlatformIdentityLookup.tsx、docs/guides/migration-im-push-by-agent-binding.md |
| platform binding | 平台绑定：Agent cell 文档里的一个 `role: "adapter"` 成员（`cell.members.<ref>.adapter` 指向本机 adapter 配置），入站消息经 cell 路由索引转给该 Agent；同一 adapter 被两个 Agent 的 cell 声明时是路由冲突，fail-closed（`adapter.list` 的 `agent` 为 null），cell 文档本身不限制一个 Agent 持有多个 adapter 成员 | 不是 channel；不是 adapter 生命周期（那是 platform integration）；pins 表与 adapter pin/unpin 已退役（I6-4），读写经 `agent.cell.get/update`（CAS） | gui/src/features/agents/impl/detail/PlatformBindingsTab.tsx、src/hyprial/daemon/impl/cells/adapter_routing/index.py |
| channel | 群组会话空间（IRC 频道原语） | 不是"频道功能"，是协议层概念；不用于外部平台接入；地址前缀 `channel:<owner>:<machine>:<name>` 是另一个意思：`adapter:` 地址的旧写法，已退役（F1c）：写入方全部改为 `adapter:`，读取方不再把它当作 `adapter:`（仅用户档案 squire 端点仍读取，随 S3c 删除），旧记录的会话 id 因此改变，见 docs/guides/cell-authority-migration.md 第 8 节 | design.md §2、src/hyprial/kernel/impl/primitives/uri.py |
| link | 网络层：两个节点之间可用的连接（成员名册 + tailcat 通道） | 2026-11-03 起回到本义；原 agent 间的发送授权与预算（send-link、`daemon/impl/links`、`hyprial link`）已随 T2a/T2b/T1c 移除 | src/hyprial/daemon/impl/forwarding/membership/、sidecar/hyprial-tailcat/internal/membership |
| PAC | programmatic agent calling：任务如何运行。声明式派单与跟踪层（任务图、条件、等待规则），**不替 worker 执行**；workflow（编写形态）、graph（编译后的运行形态）、routine（周期触发）都属于 PAC | 不是 workflow 引擎；workflow CLI 只是它的编译前端；与 cell、conversation 互不相关，任务图不是 cell（Allen 2026-11-16）；现状：OrgFS 侧的 `graph` cell 种类与 `composition/pac_mirror` 镜像已删除，任务图只存在于 PAC 自己的本地库 | src/hyprial/daemon/impl/pac/ |
| agent | 由一个或多个 harness 组成的 cell，有持久身份（`a.` id）/配置/存活 | 不等于 harness 进程（进程可重启，身份长存）；今天实现为单成员 | src/hyprial/identity/impl/agents/ |
| 组织 Agent 目录 | 当前组织成员各设备发布的 Agent 最小清单，`org.member-agents.list` 从已同步 OrgFS 文档汇总，供组织视图展示 | 清单可见性不等于 Agent 已显式加入组织，也不授予组织级操作权限；缺少成员清单时状态为 `partial` | src/hyprial/daemon/impl/org/network/member_agents/、docs/design/org-member-agent-directory-2026-11-18.md |
| agent status | Agent 运行状态：只由 `agent.list` 的 `runState` 得出，UI 归并为运行中 / 未运行 / 状态未确认；unavailable 详情显示“暂无运行信息”，stale 显示“运行信息已过期”，partial 显示“运行信息不完整” | 新建后没有运行观测不等于未运行或故障；不是凭据可用性、不是 adapter 在线/离线；推断仍带标记 | gui/src/features/agents/impl/api/index.ts::runStateFactsOf、src/hyprial/daemon/impl/cells/observability/model.py::RunState |
| Agent harness | 创建表单选择并由 `preferredHarness` 保存；GUI 启动及导入 Auth 沿用此值，概览仅显示一行“harness”（已有运行绑定时取 `harness`，否则取 `preferredHarness`） | 不拆“运行/创建时”两行，不提供临时切换；底层 start 的显式覆盖能力仍存在。不是缺少安装时的兜底，安装缺失应返回错误 | gui/src/features/agents/impl/AgentDetail.tsx |
| Agent 空闲 | `agent.list.runState.state=idle`：有当前且完整的运行观测，进程运行且没有开放中的 turn | 不等于 Auth 已验证或模型调用成功；`in-turn` 表示处理任务中 | src/hyprial/daemon/impl/cells/observability/reducer.py |
| Agent 未指定模型 | `agent.list.model` 未记录显式模型，显示“未设置” | 不等于没有模型；harness 可以依照自身配置解析，失败由实际启动/请求报告；不推断最终选中的模型 | gui/src/features/agents/impl/detail/OverviewTab.tsx |
| agent working directory | 工作目录：`agent.list.cwd`，harness 执行任务的目录 | 创建表单可填写 `agent.create.cwd`；留空由 daemon 建立 `<Hyprial home>/agents/<actor>/workspace`，不是 Agent home 本身；停止后可通过 `agent.start.cwd` 保存并启动；目录必须已存在且为绝对路径，不迁移 home 或凭据；不提供独立保存方法；目录选择器待可信宿主提供绝对路径，当前保留输入 | gui/src/features/agents/impl/AgentForm.tsx、src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py |
| Pi working-directory approval | 信任此工作目录并允许自动恢复 | `agent.start.approve=true` 只适用于 Pi，必须同时传显式绝对 `cwd`，严格解析并保存规范目录路径；批准随启动配置持久保存供自动恢复，不是一次性授权；页面重开或改目录后重新确认。`false` 在写入前拒绝，已有 `--approve` 不能覆盖；省略兼容旧调用，false 也不撤销既有批准；停止移除运行意图，关闭自动恢复。原有 Pi Git/私有 workspace 检查不变 | gui/src/features/agents/impl/AgentDetail.tsx、src/hyprial/daemon/impl/application/actors/agent_start/__init__.py |
| opened Agent chat | 开始对话：明确将 Agent 加入当前 GUI 聊天列表并选中 | 创建 Agent 只增加目录记录；真实已有会话仍从后端读取；尚未发送消息的打开记录仅保留在当前界面会话中，不创建第二份 durable 会话 | gui/src/state/appReducer.ts、gui/src/pages/ConnectedChatPage.tsx |
| agent ownership facts | Agent 所有者与主机：分别显示 `agent.list.owner` 与 `machine` 的真实值；已登录 owner 通常为邮箱，machine 为节点名 | 缺失显示“未提供”，不猜“我”或“本机”；不把主机名解释为组织或平台应用所有者 | gui/src/features/agents/impl/api/index.ts::rowOfAgentEntity |
| preview confirmation | 预览确认：先取得 daemon 签发的 operationId，再确认执行该预览 | 编号绑定调用者/会话、方法、参数、资源版本、epoch 和有效期；执行不能替换目标，未知结果不自动重发；HTTP 仅传递声明，领域负责版本校验和原子认领 | docs/design/daemon-request/design.md；src/hyprial/daemon/impl/request_catalog/preview.py |
| native Auth inventory | 原生 Auth 清单：`agent.provider.auth.list` 列出当前账号所拥有 Agent 的 harness 登录文件元数据，支持 actor/harness 筛选 | stored 显示“已保存”，只证明所需私有文件存在且非空；partial 显示“文件不完整”，unavailable 显示“读取失败”，都不能沿用为完整 Auth。导入/替换复用 agent.provider.login，移除复用 agent.provider.logout；有 registry 导入记录时，后端按每文件 1 MiB 上限校验摘要，不返回内容、路径或摘要；导入时间绑定 Agent 实体、home 资源与当前文件集，无记录/不匹配/不完整时为 null，不以 mtime 推断。startupConfirmedAt 仅表示本版本成功回复时间：绑定相同导入版本、Agent 实体、home 资源，由实际原生 Auth 选择及可信 turn 成功关联；无证据为 null，不表示从未启动或认证失败。替换/移除失效，单纯 running 或 Key 授权不能确认 Auth。Pi 需要 auth.json 与 models-store.json，Claude/Codex 各用自己的文件。沿用只重读，移除不停止 Agent、不删除 Key、不退出厂商网站 | src/hyprial/daemon/impl/request_catalog/agent/provider_auth/__init__.py；docs/design/gui-v0.1-method-plan-2026-11-13.md |
| shared credentials | 共享凭据：凭据设置中可读取的 owner Key 与服务秘钥授权记录 | 不含 Agent 的原生 Auth 文件；“暂无共享凭据”只说明该读取清单为空，不证明没有 Auth | gui/src/features/connections/shared-credentials-api.ts |
| saved user secret catalog | 已保存的用户 Secret 清单：`agent.secret.provider-list` 返回当前 daemon 的 user-provider 条目名与字段名 | 独立于 `agent.secret.list` 的授权行；只读元数据，不返回值、路径或登录材料，不证明已授权或已能启动；当前仅支持既有本地文件 authority | contract/identity/agent-secret-store.md |
| agent service secret grant | 服务秘钥授权：`agent.secret.list` 中 agent-private / agent-shared 来源的字段授权记录，凭据设置只读展示 | 可以承载认证用途，但不等于原生 Auth 文件、有效登录状态或已解封秘钥；不回显秘钥内容；与 `service grant` 不同：此处是本机 owner 把已保存的秘钥字段绑给本机 agent，不涉及跨设备网络暴露放行或 OrgFS 跨成员密文分发 | gui/src/features/connections/shared-credentials-api.ts |
| service grant | 跨设备服务授权：服务暴露方在 OrgFS 受保护段 `directory/service-grants/<owner>/<grantId>.json` 中写下的授权文档（`service.grant` / `service.revoke` / `service.grants`），声明把本机已暴露端口授予 org 内指定设备 `<owner>/<deviceId>` 或 `@members`（该 org 的成员设备，暴露方展开为准入公钥集合），并可附带用接收设备 X25519 公钥封给该设备的密文凭据（源自本机 user-provider 条目；带凭据时必须有服务名） | 与 `agent service secret grant` 不同：后者是本机把秘钥字段绑给本机 agent，前者是暴露方对 org 内其他设备的服务访问与凭据分发策略；不进 org cell、不经 org owner 转抄；`@members` 授权不得携带凭据；对同一（设备, 端口, 接收方）已有有效授权时原地更新，`grantId` 不变；撤销写 `revokedAtMs` 并清空 `credential`，已撤销或已过期超过 7 天的文档在授权人下一次授权或撤销时删除 | src/hyprial/daemon/impl/service_grants/models.py、src/hyprial/daemon/impl/service_grants/service.py |
| start status counts | 首页正常 / 异常计数：Agent 仅统计当前且明确观测的 `runState`；idle/in-turn 计正常，其余已知状态计异常。平台仅统计已绑定接入，online 且 processRunning 且无 error 计正常 | 任一 Agent 观测未知时整组显示“未确认”，不推算零；新建不保证已有观测；撤销 Key 不立即改变运行状态；任务未接入不显示虚构计数；不代表全局健康 | gui/src/pages/start/start-summary.ts、gui/src/pages/StartPage.tsx |
| worker | 被 PAC 派出执行任务的 agent 运行实例 | 与 agent 的关系：worker 是役，agent 是身 | docs/design/design-pac-worker-lifecycle.md |
| sidecar | 由 hyprial 校验并控制的辅助进程（Tailcat 设备密钥与跨网络转发） | 不是 daemon 的一部分；随 wheel 分发（`hyprial/_bin/hyprial-tailcat`） | sidecar/hyprial-tailcat |
| squire | owner 的个人助理：由打包模板 `squire` 创建的普通 agent（角色在 agent cell params `agent.role`）；squire 私信 owner 的路径已删除（#2989），发给 `user:<owner>` 的消息不经 squire | `user:<owner>` 不是 owner 适配器 thread 的别名（旧说法作废，见 user）；不是 adapter，也不再是代码特例（S10, Allen 2026-11-03）；user delivery 实现在 adapters/user_delivery | src/hyprial/daemon/impl/agent_templates/squire/、src/hyprial/daemon/impl/adapters/user_delivery/ |
| orgfs | 组织共享文件夹：spaces/docs/blob 的 mesh 复制；承载 cell 与交互记录 | 网络连通与消息投递**不依赖** orgfs；org 是建在 orgfs 之上的组织上下文 | src/hyprial/daemon/impl/orgfs/ |
| resident agent | 常驻 agent（keep 存活），对比一次性任务 worker | — | src/hyprial/identity/impl/agents/ |
| routine | 声明式周期触发器，属于 PAC：到点产生一次性 PAC run，**不做判断**，本身不含任务图 | 不是 cell（Allen 2026-11-16）；现状：`routine` cell 种类与 `agent-default` 覆盖配置已移除，默认 agent routine 一律用内置模板；看板已退役（2026-10-21），工作项改由 OrgFS 承载 | src/hyprial/daemon/impl/application/actors/routines.py、src/hyprial/biz/impl/routine/ |
| teleport | 将 agent home 与执行主体绑定的 routine 从源 host 经 Zenoh 迁移到目标 host 并重新启动；源 daemon 拥有编排，CLI/GUI 仅包装 `teleport.run`（规划中，尚未实现） | 将取代 `agent move`、`agent transfer` 命令及 `transfer.*` request；不是网络 `TransportSession`，不是本地 `agent home migrate` | docs/design/agent-teleport-handoff.md |

## 组织与会话模型（2026-11-03）

cell、PAC、conversation 是三个互不相关的概念，不是层次，也不是同一机制的几种 kind（Allen 2026-11-16）：
cell 是实体如何组织，PAC 是任务如何运行（见「产品与架构」），conversation 是主体之间如何交互。
网络层（presence、link）在它们之下。机制在代码（cell 运行时、流控闸门、投递队列），配置在数据（cell 文档）。
本节按 2026-11-16/17 的裁定修订：定义写裁定后的含义；标「现状」的是 dev 上代码今天的行为，与裁定不同处尚未实现。
11-03 的原始设计见 docs/design/cell-thread-conversation-model-2026-11-03.md，冲突处以本表为准。

| 术语 | 定义 | 退役/易混淆 | 锚点 |
|---|---|---|---|
| cell | 组织机制：把 harness、定时器等部件组合成一个单元，对外呈现为一个实体；agent 与 org 是 cell；可嵌套 | conversation、routine、graph、department 不是 cell（Allen 2026-11-16、2026-11-21）；现状：代码里的 cell 是 OrgFS 上的一份数据文档（kind、成员、权限面、参数表），种类里的 `department`、`graph`、`routine`、`conversation` 都已移除（`conversation` 只作存储类别保留，见 conversation）；通用运行时自 2026-11-03（阶段 A S3）起在 `daemon/impl/cells`；cell 设计 v3 在 intg/cell | src/hyprial/daemon/impl/cells/ |
| cell 运行时 | daemon 内唯一的 cell 读写入口（`CellRuntimePort`：index/lookup/ensure/update/retire/kick）；先写本机 `cells.sqlite3` 再经 `cell-runtime` 线程以 expect_version 写入 OrgFS，查找不做 OrgFS I/O | 断开 OrgFS 时查找照常；只有 authority（本机 owner）能写自己的 cell；`cells/observability` 是其子模块（运行观测），不是运行时本身 | src/hyprial/daemon/impl/cells/runtime/、src/hyprial/daemon/impl/cells/ports.py::CellRuntimePort、src/hyprial/daemon/impl/composition/cells/ |
| cell id（`c.`）/ cell 种类 | cell 的稳定 id：keyed 由 (kind, authority, key) 派生，subject 由一个稳定 id 派生（agent cell = `subject_cell_id('agent', a.id)` = `Agent.cell_id`），minted 为随机；当前种类为 agent、org；`params` 是一等的顶层参数表 | 文档 id 必须能由其 binding 重新派生；不存在 `department`、`workflow-definition`、`graph`、`routine` 种类（PAC 定义由 PAC 自己的本地库保存）；裁定（Allen 2026-11-16、2026-11-21）：只有 agent、org 是 cell，`department` 已由子 org 取代（J24），`conversation` 种类只作为存储类别保留；现状：`department`、`graph`、`routine`、`conversation` 种类已移除，早先版本留下的这四类文档不再解码、不读取也不删除；`conversation` 只作为记录目录的存储类别（`cells/conversation/<c.id>/`，`c.` id 派生与输入不变） | src/hyprial/kernel/impl/primitives/uri.py、src/hyprial/daemon/impl/cells/model.py |
| org cell | 表示组织及其成员/设备名册的 cell（`kind` 为 `"org"`，`keyed` 绑定目录 space id，`authority` 为 owner 的 `user:u.<sub>`，`scope` 须显式指定 `org`，仅使用 `org.*` 参数，子 org 由 `org.parent` 指向父 org） | 不作参数来源（`PARAM_SOURCE_KINDS` 仅含 `agent`）；`permissions` 必须为空；无默认 scope | src/hyprial/daemon/impl/cells/org/、src/hyprial/daemon/impl/cells/org/params.py::validate_org_cell |
| chat space | 存放 conversation 文档的 OrgFS 空间，按参与者划分：成员正好是对话参与者所属的 owner；同一组参与者的多个 conversation 是同一个空间里的多份文档（Allen 2026-11-17） | 现状：只有 owner-home 空间这一种；跨 owner 的 chat space 尚未实现 | src/hyprial/daemon/impl/composition/cells/spaces.py |
| owner-home 空间 | 成员只有该 owner 的 chat space：存放参与者都属于这个 owner 的 conversation；现状实现为 cell scope `owner` 对应的 OrgFS 空间，由 owner 拥有、名为 `hyprial-home`、space meta `kind=hyprial-owner-home` | 裁定（Allen 2026-11-17）：不再是单独的概念，只是 chat space 的一种；改名尚未实现；不是 org 目录空间；同一 owner 多设备各建一个时读取取并集、写入取 space id 最小者；只在写入需要时创建 | src/hyprial/daemon/impl/composition/cells/spaces.py |
| thread | 已退役的概念（除持久化归属键外）：2026-11-17 裁定 thread 与 conversation 只保留一层，名字是 conversation。原指 agent 与一个单一对象（另一个 agent、adapter 端点、owner）之间的通道，带流控参数 | 新写的代码与文档不再用 thread：消息的归属与记录说 conversation；现状：`hyprial message thread` 命令（T2a）、`threads.get`/`threads.list`/发送 `_thread_bind`/对话对外 `threadId`（T2b）以及 `daemon/impl/threads` 与 `composition/threads` 包（T1c）均已移除，仅保留网络/信封/记录键里的内部持久化 `thread_id`/`threadId` 归属戳与冻结键派生 `conversations/keys.py` | src/hyprial/daemon/impl/conversations/keys.py、docs/guides/send-link-and-thread-removal.md |
| send-link | 已退役的概念：原指一对 actor 之间的发送许可与流控（谁可以给谁发、按什么预算、准入控制） | 不单称 link（link 是网络层连接）；现状：`agent link` 命令（T2a）、`link.*` 请求与 `send-links` 契约、`link.minted`/`link.reclaimed` hook 与六个公开 `LINK_*` 错误码（T2b）、`daemon/impl/links` 与 `composition/links.py` 闸门包（T1c），以及 `inbox/service/link_ledger.py`、`LinkAdmission` 槽与 `outbox.link_hold` 读写（T1d）均已移除，仅保留网络传输与旧库兼容忽视语义 | docs/guides/send-link-and-thread-removal.md |
| conversation | 主体之间如何交互：一次交流。只负责两件事：一条消息属于哪次交流，以及交互记录。每个 conversation 是一份共享文档（1:1 也是），存放在成员正好是其参与者所属 owner 的 chat space 里。`message.send` 保留 `conversationId`；不传时消息归入两端的默认对话（Allen 2026-11-16/17） | 不是 cell，与 cell、PAC 互不相关；不负责送达（归投递队列）；组织结构不决定对话归属；现状：代码里 `conversation` 已降为存储类别（不再是 cell 种类，记录器不经 cell 运行时），记录键为（owner、thread、旧 conversationId）；`message.send` 不传 `conversationId` 时归入两端的默认对话（`dc.` 派生 id，已实现），经 HTTP 档位仍必须传；保留 actor 与三段式 adapter 按配对键归属；跨 owner 的对话双方各记一份，不是一份共享文档 | src/hyprial/daemon/impl/conversations/、src/hyprial/daemon/impl/composition/conversations/ |
| group conversation（群聊，`g.`） | 有成员表的 conversation：先创建，得到 `g.` id，成员是同一 owner 的人（`user:<owner>`）与他的 agent（`a.<id>`）；是 owner 的 chat space 里的一份文档，条目按 `entryId` 记一次，成员表与标题由文档里的系统条目（创建、加入、移除、离开）折叠得出；只有 owner 能管理成员（Allen 2026-11-17/18） | 不是 `channel`（协议层群组会话空间），也不是一个群一个 space；成员 id 不含机器字段，名字形式的 agent 地址不能做成员；现状：建群与成员接口（#2961）、群发送、历史与回复（#2968）、命令行（#2969）、投递与唤醒（#2972）、HTTP 发送（#3007）已实现；存储在 `conversations/<g.id>/log-<writer>/`，在 `cells/` 之外，旧读取方看不到；范围限单机、单 owner，跨机器与跨 owner 未做；唤醒只看 `mentions`（仅唤醒明确 `@` 的 agent 成员，不论作者是人还是 agent；未 `@` 的消息写入群文档但不唤醒任何人；经 `message.reply` / `harness_reply` 在群内回复时，daemon 默认把被回复消息的作者记入 `mentions`——仅当该作者是另一个当前 `agent` 成员时，回复人类 owner、回复自己或原发件人已移出群时不加——并支持 `--mention` / `mentions` 追加成员与 `--no-mention` / `noMention` 取消默认 `@`；两个 agent 互相回复会持续互相唤醒、无跳数上限；如需不被 `@` 也跟进并自动回复，由 agent 在运行时为自己添加定时查看群历史的 routine，以数据形式配置而不写死在代码里；Allen 2026-11-21 裁定取代 R28 前两条） | src/hyprial/daemon/impl/conversations/groups/ |
| 通信 | N 个有确定地址的 actor 之间发送信息的行为；所有通信都有 conversation 归属并被记录，不允许绕过（Allen 2026-11-17） | 保留 actor（`dispatch-coordinator`、`_turn-hooks`）也是通信主体，机制流量在展示一侧过滤，不在代码和架构里特殊化；现状：发给 `user:` 的消息已进入记录（#2954：进入本机用户 agent 的收件箱并写入对话文档）；保留 actor 的流量（PAC 派单、hook 交换）与三段式 adapter 地址（飞书入站及其回复）已进入记录，按两端派生的配对键归属 | src/hyprial/kernel/impl/contracts/reserved_actors.py::RESERVED_AGENT_ACTOR_NAMES、src/hyprial/daemon/impl/application/messaging/inbox_surface/ |
| user（`user:<owner>`） | 一个用户在每台机器上各有一个独立的用户 agent（id 由 registry 在每台机器上各自生成，不由 owner 推出，靠 registry 里的标记识别）；对话归属于 `user:<owner>`，不归属于某台机器上的用户 agent；建立 OrgFS 链接后把各机器的用户 agent 合并，尚未设计（Allen 2026-11-20） | 不是 owner 适配器 thread 的别名（旧说法作废）；现状：本机 agent 发给本机 owner 的 `user:<owner>` 的消息进本机用户 agent 的收件箱并记入对话（#2954）；发送方的 cell 有 adapter 成员时推到该 cell 写明的聊天（I3），否则推到本机用户 agent cell（`hyprial-user`）写明的聊天（I7a），不按 owner 的 IM 账号推送；发给另一 owner 的仍经 user delivery 交给对方节点，由对方的用户 agent cell 推送；两端都是 user 的对话尚未实现 | src/hyprial/daemon/impl/application/messaging/inbox_surface/send_targets/user.py、src/hyprial/daemon/impl/adapters/user_delivery/ |
| department | 已由子 org 取代（J24）；不再作为 cell 种类 | 早先留下的 `department` 文档不再解码、不读取也不删除，其参数不再生效 | src/hyprial/daemon/impl/cells/model.py::CellKind、src/hyprial/daemon/impl/cells/org/params.py::validate_org_cell |
| 交互记录 | 通信的正文（message/request/reply），写入所属 conversation 的文档 | 与投递队列分层：送达以队列为准，内容以记录为准；现状：记录 interaction 与 system 两个类别的消息，类别是记录条目上的 `messageClass` 字段，`conversations.list` 的条目带 `messageClass`（`threadId` 已于 T2b 从公开读取移除），写在 conversation 的记录目录（`cells/conversation/<c.id>/`）下；发件人为 `system:hyprial:<node>`、没有对端 actor 的通知不记录；发给 `user:` 的消息已记录（#2954） | src/hyprial/daemon/impl/conversations/record/ |
| 系统运行信息 | workflow 节点通知、看门狗、TTL 到期、actor_lost、PAC 内部派单 | 现状：都不唤醒人；裁定（Allen 2026-11-17）：其中 actor 之间的流量（PAC 派单、worker 回复、hook 交换）属于通信，进 conversation 记录，类别是记录上的 `messageClass` 字段供展示过滤（已实现：记录后按授权释放正文，保留期固定为代码默认值）；没有对端 actor 的通知（`system:hyprial:<node>` 发出）只走本地投递队列，不进 orgfs；没有对端 actor 的通知（看门狗、TTL 到期等）暂不改 | src/hyprial/kernel/impl/contracts/envelope.py::MessageClass、src/hyprial/daemon/impl/delivery/system_residue.py |
| route | adapter 上的一条命名外发路线（`adapter route` / `adapter.routes.*` 配置；`route:<adapter>:<route>` 地址命名空间已于 I4b 退役，本机 operator 用 `adapter.routes.send` 按路由名发送） | 主动向指定群/单聊发送时使用；候选来自平台入站观测，默认路由是未指定路线时的外发目标；cell adapter 成员是入站绑定，入站回复走 reply bridge；勿与 `route_registration`（交互会话投递路由）混淆 | src/hyprial/daemon/impl/network/route_delivery.py |
| session | agent 进程的运行会话（回合、心跳、绑定） | **不是**聊天；GUI 的 `sessions.*` 操作（`sessions.list`、`sessions.send` 等）里的 session 是另一个意思，指聊天会话，规范名是 conversation（此前写的“改称 thread”随 thread 退役作废） | src/hyprial/daemon/impl/session_actor/、src/hyprial/shell/impl/gui/adapter/ |

## 消息面（inbox/transport）

| 术语 | 定义 | 退役/易混淆 | 锚点 |
|---|---|---|---|
| inbox / outbox | **投递队列**：收件人本机 durable 收件箱 / 发件人先落盘再投递；负责 ack、重试、TTL、离线暂存 | 2026-11-03 起只管送达状态，**不保留正文**（正文归交互记录）；不依赖 orgfs | src/hyprial/daemon/impl/inbox/ |
| envelope | 消息信封（wire 上的统一消息单位） | — | contract/wire/golden/ |
| intent | 消息意图（request/reply/ack/event），决定唤醒与回执行为 | — | contract/wire/golden/expectations.json |
| wake | 投递触发的接收方唤醒 | wake ≠ reply | 同上 |
| custody transfer | 托管移交：mailbox 先落盘再签收（custody-ack），接管重投义务 | 不是转发，是责任转移 | docs/design/design.md §3 |
| mailbox | 志愿承担离线兜底的角色（任意 daemon 可担任，**角色而非特殊节点**） | 无需选主，冗余投递无害 | docs/design/design.md §3 |
| DLQ | 重试超限后的本机死信队列，doctor 可查 | — | docs/design/design.md §3 |
| presence | 网络层：上下线真值（zenoh liveliness 为真值） | 只说明在线，不说明正在处理 | src/hyprial/daemon/impl/transport/zenoh/presence.py |
| desired state | daemon 的期望状态存储；干净启动与崩溃启动走同一恢复路径 | — | src/hyprial/daemon/impl/desired_state/、contract/daemon-lifecycle/ |

## 重构结构（深模块）

| 术语 | 定义 | 备注 | 锚点 |
|---|---|---|---|
| kernel | 基础核心依赖：actor 模型、wire 词汇表、OS 原语 | 不允许出现在依赖环里 | docs/design/refactor-plan-deep-modules-2026-10-01.md |
| identity | agent / user 的封装（身份、配置、存活、registry） | 名词层；"是什么" | 同上 |
| daemon（层） | 平台层：进程生命周期 + 消息权威边界 + 全部能力域 | 动词层；"做什么" | 同上 |
| biz | 最外层业务封装（usage、quota、recap/hooks、routine 等） | 判据：删掉它平台核心承诺仍成立 | 同上 |
| shell | 人机交互接口（cli、onboarding、login、gui_*） | 依赖方向 kernel ← identity ← daemon ← biz ← shell | 同上 |
| interface / impl | 每模块的公共面（审定的 __all__ + lazy 转发）/ 私有实现 | 跨模块 import 经公共面，钻 impl 即违规 | 同上 |
| deep module | 接口远小于实现的模块（Ousterhout） | 本重构的方法论 | 同上 |

## 契约与验证

| 术语 | 定义 | 备注 | 锚点 |
|---|---|---|---|
| contract | 跨实现行为契约语料库：金样本 + 可执行裁判 + CI 门禁 | 不是 protobuf 类 IDL；类 Web Platform Tests | contract/README.md 语料地图 |
| golden | 冻结的期望行为数据（信封 fixture + 声明式期望） | 每条 case 带 sources 证据链 | contract/wire/golden/ |
| scenario | 可执行端到端流程（E2E-001…），带证据闸门 | 发版准入的载体 | contract/e2e/ |
| gate | CI 中 fail-closed 的门禁（不进白名单即红） | 白名单条目必须带理由 | tests/support/ci/ci_workflow_policy.py |
| PASS / FAIL / BLOCKED | 验证三态；BLOCKED = 未执行任何断言 | **BLOCKED 不是 PASS，skip 不是结论** | ROADMAP.md |
| WORM | write-once-read-many：证据只追加不篡改 | notes/evidence 适用 | docs/notes/ |

## 仓库治理

| 术语 | 定义 | 备注 | 锚点 |
|---|---|---|---|
| ROADMAP.md | 长期目标与发版义务（原名 GOALS.md）：协调者/worker/通道全断时仍可被发现、看懂、执行 | 发版时过一遍；完成不删除、标 ✅；条目自含判据+命令摘要+红线，实测记录归 docs/notes/roadmap/ | ROADMAP.md |
| 失联韧性 | 工作原则：任何关键信息不得只存在于消息通道或某人记忆里，必须落仓库 | 本仓库协作方式的第一原则 | AGENTS.md、ROADMAP.md、design.md |
| uv 强制 | 仓内 Python 一律 `uv run`，裸 python 会被 hook 改写 | Claude/Codex 双 harness 生效 | .agents/hooks/ |
| 五桶 | docs/{design,guides,reference,domains,evidence}（收拢目标） | 证据归 docs/notes/（WORM） | 重构方案 §5.3 |
