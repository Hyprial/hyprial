# @hyprial/dsh-hyprial-plugin
## Hyprial command entry

The migrated core uses `hyprial`, `HYPRIAL_HOME` and `~/.hyprial`. Use
`hyprial gui`, `hyprial gui status --json` and
`hyprial gui upgrade`. The Hyprial registry entry must select
`hyprial-install.json`; `h2b-install.json` is a historical receipt-path alias with the same Hyprial schema; it no longer targets the retired H2B installer.
The launcher translates inherited lifecycle variables and routes the existing
internal bridge command to Hyprial, retaining stored GUI identities.
Migration paths, environment settings and verification are documented in
[GUI migration notes](docs/hyprial-migration.md).



把 DeepSeek Harness 的 Web UI 改造成「飞书式三人群聊」的 H2B 插件；同时提供可持久加载的静态 DSH 包和便于快速迭代的动态 Cordis 源码。

团队使用请看 [更新与新功能使用指南](docs/team-feature-guide.md)：更新安装、DSH 入口、Codex 全局代理与登录、Agent 驱动 Workflow、飞书接入和会话同步。

## Agent GUI 定制

DSH 工作台新增个人界面设计、试用、发布和恢复入口。Agent 在原生会话内生成声明式界面包，完整业务模块保留原有状态与权限。安装包含启动时使用的布局包；升级后需要重新启动 DSH 才能加载新 Host。使用方式、模块边界、存储位置与恢复步骤见 [GUI 定制说明](docs/gui-customization.md)。

## 释放子 Agent

主 Agent 会话的「⚙ 会话配置」在归档上方提供「释放子 Agent…」。确认后停止选定的驻留可继续子级及其持有的下级、取消未处理队列并释放运行资源；保留会话历史，不归档或删除，不停止主 Agent。新消息仍可恢复可继续子会话。详见 [释放子 Agent 的语义与边界](docs/subagent-release.md)。

## GUI 指令

`hyprial gui` / `hyprial gui start` 打开 DSH 工作空间。
`hyprial gui status --json` 查询状态，`hyprial gui stop` 停止，
`hyprial gui upgrade` 更新 GUI 包并恢复此前运行的 DSH。
Dashboard 独立页已退役，`gui dashboard`、`gui dsh`、`gui all` 不再支持。
原有 DSH process.json、会话和个人布局保留；升级前对旧 Dashboard 做进程身份核验后停止，
不会重新启动 Dashboard，也不删除其历史进程记录。

## 目录内容

- `browser-tests/` —— DSH/GUI 编辑器浏览器验收的独立锁定工具依赖

- `imskin-host-plugin.js` —— 动态插件 Host 源码：受管执行 H2B 通讯录与只读控制面固定命令，并提供包私有 RPC
- `imskin-plugin.js` —— 动态插件客户端源码
- `static/host.js` / `static/client.js` —— DSH 启动与浏览器刷新时自动加载的静态双端包
- `dsh-web.patch.yml` —— 将静态包加入 Web profile 的便携 patch
- `h2b-session-bridge.mjs` —— H2B daemon 与 DSH Session 的最小多 Session bridge
- `h2b-control-bridge.mjs` —— H2B Control 受控操作桥；校验结构化输入并以 argv 数组调用固定 CLI
- `im-group-chat-mockup.html` —— 三人群聊静态线框 mockup（浏览器直接打开）
- `docs/b-track-multisession-mvp.md` —— B 轨 Demo 的范围、语义与后补项
- `docs/b-track-live-validation-2026-08-17.md` —— 跨机器直发、回执终态与同 Session 离线积压的实测证据
- `docs/team-deployment-prompt.md` —— 可直接交给团队 Agent 执行的静态部署、更新与验收 Prompt
- `docs/mfu-h2b-enterprise-demo.md` —— MFU Business Console、H2B Adapter 分层、操作与后补边界
- `docs/feishu-remote-entry.md` —— 飞书 App 固定到 DSH 工作会话的身份、路由与测试说明
- `integration/agent-task-client.js` —— 面向 H2B P0 的 capability-driven `agent.task` 薄客户端与 Session-scoped Host transport；严格对齐 MFU v1 namespace/identity/result 契约，并作为 MFU Integration v2 的正式执行通道
- `scripts/verify-agent-task-real.mjs` —— 经 DSH bridge 对本机 H2B daemon 执行 `start → progress → result.submitted → status/result → ACK` 的真实集成验证
- `docs/agent-task-client-contract.md` —— `agent.task` 能力、身份、结果与兼容边界
- `docs/h2b-control-console.md` —— H2B CLI 能力目录、P0 固定查询与后续写操作安全分级
- `contracts/mfu-agent-task-v1.lock.json` —— MFU 权威契约合并提交与 conformance fixture 哈希锁
- `README.md` —— 本文档

## 已实现

1. **飞书式三级导航**：一级应用栏提供「消息 / 通讯录 / H2B 控制」与底部设置；MFU 入口暂时隐藏（含收起侧栏），集成能力和数据保留。二级栏随一级入口切换对象列表；三级主区承载当前会话、联系人详情或 H2B 控制面。消息下管理 H2B 直聊、Agent 工作会话、待处理空白会话及默认折叠的系统/测试会话；`New Session` 只属于消息。侧栏收起时一级入口仍可用
2. **消息气泡**：助手左侧头像 + 灰色气泡，用户右侧头像 + 蓝色气泡
3. **子智能体「第三人」块**：覆盖 `subagent_fork` / `subagent` 工具卡，渲染成紫色身份块（头像 + 名字 + 状态条「处理中/完成」+ 任务 + 结论）
4. **会话配置按钮**：header 右上（session log 旁）的 ⚙ 按钮 → 「会话名称」+「修改图标」+ H2B 接入 +「飞书远程入口」+「归档会话」。显示名称通过 DSH 官方 Session binding 修改，只影响标题，不改变 Session ID、H2B Actor、飞书 pin 或历史记录。飞书入口从已配置的 Lark Adapter 中选择一个，经显式确认后把该 App 的全部入站会话固定到当前工作会话；首次固定可指定稳定、可读且节点内无在线冲突的入口名称，已绑定入口可在无 pending 消息时受控迁移名称。解除固定带 read-before-write fence，绑定漂移时拒绝覆盖。固定关系写入 Host ledger，浏览器关闭后仍由 DSH Host 接收、唤醒 Agent；飞书输入作为带来源前缀的普通用户消息显示在 DSH 对话流中，最终回复投回原飞书消息。已固定飞书入口的工作会话在消息侧栏显示 `飞书值守 · <adapter>`，避免工作区名遮蔽远程职责。固定后还可为明确的 `route:*` 开启「仅 Agent 输出」或「完整镜像」广播；默认关闭，飞书来源 turn 永不再次广播
5. **归档会话**：调 `workspaces.archiveSession`，归档会话从列表隐藏（DSH 现有能力，单向，暂无 unarchive）
6. **h2b 通讯录与直接聊天**：「通讯录」是独立一级应用，二级栏只显示可直聊的四段 `agent:owner:node:actor`，按在线/离线分组，并隐藏节点邮箱、channel route 与本机 `dsh-web-*` actor；点击联系人仅在三级主区查看详情，不创建 Session。「发消息」按完整目标 Agent URI 复用已有直聊，没有会话时自动创建，随后自动切回消息；不提供新建独立对话入口。直聊列表与 header 使用“会话名称 + 完整四段 canonical URI”，即使不同节点存在同名 actor 也能区分；直聊设置可通过 DSH 官方 Session binding 自定义或恢复默认名称，只改变显示，不改变身份、授权或投递目标。header 同时显示目标实时在线状态，并区分“已接入 H2B”与目标在线；发送后显示 daemon 是否已接受投递，拒绝时保留具体原因。中文输入法组合态 Enter 不发送；位于底部时自动跟随新消息，用户查看历史时暂停并显示「回到底部」。身份、Carrier 与工作关联等低频信息保留在 header 状态/操作弹层，不常驻占宽
7. **消息分区**：「消息」一级应用的二级栏统一管理 H2B 直聊与 Agent 工作会话，分别分组展示，待处理空白会话和系统/测试会话独立收纳；联系人选择、MFU 导航与会话列表不再挤在同一列，也不重复展示同一会话
8. **H2B 多 Session Bridge**：普通 DSH Session 默认不接入，未接入时 header 不显示 H2B；需要允许其他 Agent 发消息并唤醒当前工作会话时，从会话 ⚙ 设置接入。飞书远程入口使用独立的 `dsh-session-<hash8>` 稳定 Actor 与 `dsh-remote:<内部 Session UUID>` fence；标题修改不改变地址，也不替换 DSH 内部 UUID。旧的 `dsh-web-*` 地址继续服务普通 H2B 会话，避免迁移时丢失在途消息
9. **直聊 Conversation Carrier 与工作会话 1:1 关联**：每条直接聊天是人与该远端 Agent 的唯一网络 Carrier；工作会话只保存 backlink 和模型工作历史，不再另起一个用户直聊 actor。直接聊天的「在工作区处理」可以创建专用 Agent Session 或关联已有 Session；两边严格 1:1。远端回复由 Carrier 轮询和 mark 一次，同时显示在直聊并以带身份、Conversation-ID、Message-ID 和不可信边界的 `queue` 内容镜像到工作会话。两边共享远端 Conversation，但不合并 DSH 模型历史
10. **工作会话 `@Agent` 协作**：在普通 Agent Session 的行首输入 `@` 并从 H2B 候选中选择 canonical Agent，候选和绿色 `H2B 双发 · @Agent` 明示“自动接入 H2B、回复返回本会话”。提交时自动复用已关联的同目标 Carrier；没有关联则在后台创建或复用一条同目标的唯一直接聊天并建立工作关联，不切走当前页面。原生 Enter/发送按钮经 Carrier 发送远端，同时通过当前 Session 的 `conversation.send()` 让本地 DSH Agent 看到协作请求；远端回复随后镜像进同一工作会话。已关联其他目标时 fail closed，不静默改绑。DSH Agent 自主使用 MCP 时仍保持自己的 Agent actor，不冒充人的直聊 Carrier
11. **空白 Session 治理与直聊恢复**：尚未选择 Workspace、也没有开始对话的 Session 单独进入「待处理空白会话」，不再伪装成 Agent 工作会话；每行可归档，历史 H2B 标题还可尝试恢复。新直聊会把 `Session → 已核验 canonical Agent`、最近 100 条有界消息及工作会话 backlinks 写入 package-owned bridge ledger；浏览器缓存丢失后自动恢复，不打开会话、不启动轮询。旧版本只在用户显式操作且该 Session 恰好保留一个已核验 participant 时迁移；绝不根据显示标题猜身份，无法核验时明确要求归档后从通讯录重建
12. **Host/Client 能力协商**：Host 暴露版本化 `protocolVersion + operations + features`；Client 在使用持久历史、工作关联或自动恢复前先核对能力。浏览器已刷新但 DSH 仍运行旧 Host 时，创建与归档保持可用，需要新 Host 的恢复入口会明确提示受控重启，而不是显示一个点击无效的按钮
13. **MFU 双展示模式 + DSH Integration Plugin（方案 3）**：「MFU」是独立一级应用，二级栏提供 MFU 应用入口与「H2B 诊断」回退。三级主区默认提供独立窗口启动，也可切换为 sandbox iframe 内嵌操作；两种模式互斥，同一时刻只有一个 Adapter 连接。独立模式使用 MFU `/console?integration=dsh` 与精确 opener WindowProxy；内嵌模式使用 `/console?embed=dsh` 与精确 parent WindowProxy。两者都让 MFU 在自己的 origin 中运行，不把 Domain/Store 复制进 DSH；完整游戏仍在 MFU `/`。Integration v2 通过固定 Host bridge 把结构化 WorkPackage 映射到 H2B `agent.task.start/status/result/cancel`，DSH 不再为正式任务保存浏览器内协调状态；v1 `task.dispatch/status` 仅为旧 MFU 客户端兼容。只有 exact origin、精确 `event.source`、随机 nonce、协议版本、操作白名单、canonical Agent URI 和 64 KiB 请求上限全部校验后才允许调用。此前浏览器本地企业作战室保留为明确的 `H2B 诊断回退`，仅用于 MFU Web 不可达时排查网络链路
14. **H2B 控制台（P0/P1）**：一级「H2B 控制」按总览、Agent 网络、Workflow、Routine、投递、集成与系统分区展示本机 H2B 状态。总览把 version/ps/top/doctor 汇总成 daemon、版本、运行单元、健康检查与额度源首页，并把 warning/fail 和建议动作直接展开；系统页区分当前进程、系统服务安装、组织采纳候选与自动更新 timer，避免把“当前运行”误写成“可自动恢复”。P0 Host 只接受具名 query，并映射到仓库固定的 `--json` CLI；本机 H2B 尚未提供的命令会逐卡显示不可用。P1 提供单目标 Workflow 的 `plan → 确认 → run` 闭环、按 runId 查询/取消，以及 Routine 新建/状态/暂停/恢复/删除；操作表单把本机 online 稳定身份与网络 online+deliverable 目标分开，默认排除离线记录和 `dsh-web-*` 临时 Session，Agent 网络另设历史/临时与全部诊断视图。Agent 网络可确认后注册本机 Agent、按固定 Harness 启动 headless Worker，并只从 `h2b ps` 的运行中 Connector 选择一个精确 `connectorId` 停止，不开放身份删除、批量清理、`down --all` 或交互 Session 停止。投递页把 Outbox 与 sender-scoped `delivery status` 组合成可检索的终态中心，显示 fetched/pending/expired、holder、Conversation、原因与幂等键，并可按 Message ID 调用 `trajectory` 展开跨节点事件路径；集成页合并 Adapter、Channel、接收 pin、状态、权限诊断和既有平台身份记录，可确认后启动或停止当前精确 Adapter、加入或退出 Channel；不开放 Adapter 配置/凭据删除、全局 reload 或身份授权。接收 pin 不提供任意 Actor 编辑器，只在会话设置开放“精确 Lark Adapter ↔ 当前 DSH 工作会话 Actor”的窄操作，并要求确认及解除前一致性检查。只读查询不执行 ACK、prune 或重投。受控操作仍只接受具名 action：YAML 进入权限为 `0600` 的临时文件，任务内容不进 argv；Agent 名、Harness、完整四段 sender、runId、routine name、adapter name、channel name 和 connectorId 在启动 CLI 前校验，写操作必须显式勾选确认。daemon 停止、升级和身份删除仍未开放。Host RPC 更新后必须受控重启 DSH，Client 刷新后即可加载新界面

日志页通过受控 `h2b log` 查询最近 5、15 或 60 分钟的结构化事件，可按 level、component、name、完整 Agent URI、Conversation ID 与 Message/Correlation ID 精确过滤；Host 强制限制最大一小时窗口，Client 最多展示最新 200 条，且不持续轮询。

任务运行页统一展示 Agent Task 与 PAC Workflow：前者通过正式 facade 查询 `externalRef` / `targetRef` / `conversationId` / `resultRef` 和完整交付，后者保留通用 PAC 状态与取消操作；两者共用 H2B 执行底座，但不混用取消接口或业务契约。

控制台总览同时是操作首页：Agent / Worker、Workflow、Routine、投递、日志和集成的快捷入口按 Host capabilities 标记「可操作 / 部分可用 / 只读 / 当前版本不可用」，点击直接进入对应工作区。
Workflow 现在以 [Agent 工作台](docs/workflow-workbench.md) 为主入口：选择或新建标准 DSH 工作会话，用自然语言提出目标；Agent 工具保存版本化方案，GUI 展示确定性差异、CLI 校验与固定版本的运行证据。支持名称/超时快速修改、YAML 导入导出、从历史执行快照复制方案，以及针对具体目标交给 Agent 分析。已有结构化工作包表单位于“手工派发（高级）”，通用 PAC 历史位于“Workflow 运行中心 · 全部运行”。

节点详情提供 [任务对话](docs/workflow-task-discussions.md)：在 Workflow 内查看只读执行证据并与明确 Agent 沟通，复用唯一直聊但为每个运行节点持久分配独立话题。任务沟通不复用执行会话的完成回执通道，不自动转发给关联工作会话；普通直聊不再继承整段会话的旧 Workflow 引用。

## H2B 多 Session Demo：最小环境

前提：

- `h2b`、Bash、Git 和网络可用；Node/npm、pnpm 与 DSH 可由安装脚本补齐。Web 地址默认为 `http://127.0.0.1:3080`
- H2B daemon 已运行（本机或经 SSH 转发的可达位置）
- 静态 Host 会从插件安装目录解析 bridge 与去重账本，不依赖当前 DSH Session 选择的 Workspace；`H2B_DSH_DEMO_CWD` 只控制上报给 daemon 的 worker 工作目录（见下）
- 未经授权的入站身份始终 fail closed。长期运维/操作者身份可配置静态精确白名单；从通讯录或结构化 `@Agent` 选择的身份会经过 H2B `targets` 核验，并只授权给承载该远端 Conversation 的直接聊天 Carrier

DSH 进程启动前需要设置三个环境变量（改动后需由操作者选择合适窗口重启 DSH，不能为 Demo 擅自中断正在运行的 Session）：

```bash
# 1)（可选）长期运维身份的精确静态白名单；动态参与者无需预写在这里
export H2B_DSH_DEMO_ALLOW_FROM='agent:owner:node:test-sender,user:owner'

# 2) h2b daemon 的 Unix socket 路径（默认 ~/.h2b/state/daemon.sock）
#    同机运行通常无需设置；隔离环境或 SSH 转发场景必须显式指向 DSH 侧可达的路径
export HARNESS_SOCKET_PATH='/absolute/path/to/daemon.sock'

# 3)（可选）DSH worker 工作目录；默认 process.cwd()。仅当 DSH 与仓库不同目录、
#    或 DSH 与 h2b 不在同一台机器时才需要
export H2B_DSH_DEMO_CWD='/absolute/path/to/dsh-hyprial-plugin'
```

### 三个变量的作用域与路径归属

| 变量 | 谁读取 | 路径必须存在于哪一侧 |
|---|---|---|
| `H2B_DSH_DEMO_ALLOW_FROM` | bridge（DSH 进程内） | 纯身份串，无路径 |
| `HARNESS_SOCKET_PATH` | bridge（DSH 进程内） | **h2b daemon 那一侧**创建的 socket；bridge 侧（DSH 进程）必须能访问该路径 —— 同机即 daemon 默认 socket；SSH 转发场景把它指向转发后在 DSH 侧可见的 socket 路径 |
| `H2B_DSH_DEMO_CWD` | bridge 经 `session.register` 上报 daemon，daemon 用作 DSH worker 的启动/工作目录 | **DSH 那一侧**（worker 实际运行的地方）。DSH 与 h2b 不同机时，必须是远端 DSH 机器上真实存在的目录，不是本机目录 |

### 远端场景（DSH 与 h2b 不在同一台机器，如 SSH 只转发 HTTP）

这是受支持的配置，不是退化路径：**配好上面两个变量即可**，无需同机。

- `HARNESS_SOCKET_PATH`：把 h2b daemon 的 socket 通过 SSH 转发到 DSH 侧可见的路径（如 `~/.h2b/state/daemon.sock` 的转发副本），再把它指给该变量。
- `H2B_DSH_DEMO_CWD`：必须指向**远端 DSH 机器上真实存在**的目录（例如远端的本仓库路径）。常见的失败现象：把本机临时目录传过去，DSH 那边根本没有该目录 —— 结果是 worker 能注册、状态显示在线，但 turn 无结果，很难排查。判断方法：该路径在 worker 实际运行的机器上 `ls` 必须存在。

`H2B_DSH_DEMO_ALLOW_FROM` 只能写经过绑定核验的完整 `agent:` / `user:` 身份，不能写群、通道、conversation 或 Session ID。每个 Carrier 最多动态授权 3 个参与者；授权键仍是完整 canonical `agent:` 身份，并通过 H2B 目标目录精确核验。解除工作关联只停止镜像，不销毁仍可独立使用的直接聊天及其目标授权；归档直聊才执行 Carrier 授权清理。

### 关联工作会话与 `@Agent`

- 从 H2B 直接聊天选择「在工作区处理」：默认创建专用工作会话；也可以切到「关联已有会话」。创建路径固定调用 `sessions.create({ workspaceId })`，不会使用可能复用无关空白 Session 的 `connectWorkspace()`。
- 同一用户对完整目标 Agent URI 只有一个直聊，后端在账本锁内检查唯一性；多个工作会话可关联同一直聊，每个工作会话仍只关联一个目标。新回复会同步到所有关联工作会话。
- 在 Agent 工作会话的行首输入 `@`，必须从候选列表选择目标。选择后继续输入消息，按原生 Enter 或点击原生发送按钮，即可经直接聊天 Carrier 发送目标 H2B Agent，并同时提交给当前 DSH Agent。
- 候选列表会标注「Enter 双发 · 自动接入 H2B」，选择后发送按钮旁持续显示绿色 `H2B 双发 · @Agent`。提交后若没有关联，插件会后台创建或复用直聊 Carrier 并建立工作关联；不会切走当前工作会话。非行首 `@` 不提供 H2B 候选。
- 从哪个 Direct Carrier 发起，远端回复就先回到哪个 Carrier；关联工作会话收到的是同一消息的身份标注镜像。关联不会把两套 DSH 历史合并。用户输入的协作走 Direct Carrier；本地 Agent 自主 MCP 协作仍走它自己的 actor。
- 该路径使用 DSH rc.7 官方 `CommandClaim` 与 session-scoped `conversation.send()`，不修改 DSH 源码、不替换 composer、不监听 DOM。
- 远端失败时 CommandClaim 保留原草稿；若远端已接受但当前 Agent 提交失败，同一条消息重试只补当前 Agent，不重复发送远端。进程/页面在两步之间崩溃的持久幂等留待后补。
- 直聊 Session 与 canonical Agent 的身份绑定、最近 100 条有界聊天记录及工作会话 backlinks 同时写入 package-owned bridge ledger；浏览器 `localStorage` 只是 UI 缓存，丢失后会从可信 ledger 自动恢复。打开前先读取后端直聊索引；并发创建只接受一个绑定，竞争失败的空白 Session 自动归档并打开已有直聊。历史重复项合并到较早建立的会话，原始账本保存在 `humanChatArchive`，旧 Session 记录为别名，浏览器独有记录与草稿在清理前备份到 `h2b-human-chat-merge-backup-v1`。

人工验证时只新建专用 Demo Session，并限制同一 Session 单浏览器 Tab：打开会话 ⚙ 设置 →「将本会话接入 H2B」，再从 header 的绿色 `H2B 已接入` 状态复制 Actor URI，让已在白名单内的 sender 发唯一 marker。入站应在约 2 秒内以新 Turn 或 next-turn queue 进入目标 Session；回复由 Agent 正常输出或其 H2B 工具完成，轻量状态面板不承担 Reply/Ack。不要把生产或正在工作的 Session 临时接入测试流量。

## 永久自动加载（推荐）

团队成员首次部署或更新时，推荐把
[`docs/team-deployment-prompt.md`](docs/team-deployment-prompt.md) 完整交给其 Agent
执行；该文件包含环境归属、受控重启、真实收发验收和最终报告要求。

首次安装或更新后可使用仓库提供的受检入口：

```bash
h2b install gui
```

这是普通用户的推荐入口：h2b 锁定并展示本次 Git commit，确认后调用本仓根目录
`install.sh`。本仓优先复用兼容的 Node/npm；缺少时把经过 SHA-256 校验的 Node 24 LTS
安装到 `$H2B_HOME/apps/gui/runtime/node`，并按需安装 pnpm。实际安装或升级时从官方 npm
解析 DSH `latest`，在独立候选目录生成本次精确版本和依赖锁，安装并通过真实浏览器
兼容性验证后再切换。失败保留此前运行时；普通启动不联网更新。GUI 发布版本未变时，
应用管理器的普通升级仍是空操作；需要刷新 DSH 时使用 `hyprial gui upgrade --force`
并先核对源码替换计划。详见 [latest 策略与验收](docs/dsh-latest-release-gate.md)。
安装器不会自动使用 `sudo` 或修改 shell rc。

Node 下载和 npm 安装默认先使用当前/官方源，网络失败时自动回退 npmmirror；只想使用
国内源时可设置 `H2B_INSTALL_MIRROR=cn`，只想使用官方源时设置为 `official`。也可通过
`H2B_GUI_NODE_DIST_URL`、`H2B_GUI_NODE_MIRROR`、`H2B_GUI_NPM_MIRROR` 指定企业或本地
镜像，这些设置只影响本次命令，不会写入 npm 全局配置。维护者在当前 checkout 调试时
仍可直接使用：

```bash
bash scripts/install-local.sh
bash scripts/start-web.sh
```

`setup:local` 会按需安装 Node/npm、pnpm、DSH，检查 H2B、构建静态 Client、运行隔离
测试并把本仓注册到 DSH Web profile；`start-web.sh` 会恢复安装器管理的 Node PATH、
验证 daemon socket 和可选 worker cwd，并先查询 Web
profile：已注册本插件时直接启动，未注册时才为本次启动加载 `dsh-web.patch.yml`，避免
同一个 `h2b-talk` loader id 被重复插入。

GUI 安装默认把 `dsh-codex` 一并注册到同一个 Web profile，启动 DSH 后即可看到
“设置 → OpenAI Codex”。`h2b gui upgrade` 也会补装缺失插件；已有的版本或本地链接继续
使用，不自动覆盖登录、代理或已保存的模型设置。首次补装使用当前 npm 源的 `dsh-codex`，
其 bundle 为未设置默认模型的用户提供 Codex 模型及搜索路由。未登录时，请在设置页面
使用 ChatGPT 登录；安装和升级不会启动 OAuth，也不读取本地 Codex CLI 的认证文件。
安装最后会验证 Web profile 的配置组合；插件下载或配置加载失败会报错，不报告安装成功。

普通用户安装完成后直接启动：

```bash
h2b gui
h2b gui -d
h2b gui status
h2b gui stop
```

该命令从 h2b 的安装回执定位固定的 GUI checkout，并调用本仓 manifest 声明的
`scripts/start-web.sh`；它与用于启动网络 Agent 的 `h2b start dsh` 不是同一个概念。
不带参数时在前台运行；`-d` 启动由 H2B 管理的单实例后台服务，`status` 查看其 PID、
地址和日志位置，`stop` 只停止身份匹配的受管进程。维护者仍可在 checkout 内直接运行
`bash scripts/start-web.sh`。

非默认机器配置先执行：

```bash
cp .env.example .env.local
```

`.env.local` 只属于当前机器且已 gitignore；不要在其中放入需要提交或转发的凭据。
脚本不会初始化 H2B、修改网络端点、覆盖身份配置或终止正在运行的 DSH 进程。安装阶段
不要求 H2B daemon 已启动；`start:web` 才检查 daemon socket 和 `h2b doctor`。
维护者可用 `bash scripts/install-local.sh --check` 和
`bash scripts/start-web.sh --check` 只执行前置验证，不注册插件或启动服务。

永久注册方式是把校验过的源码打包成版本化 tgz 并注册到 Web profile，然后直接启动该 profile：

```bash
npm run build:static
node scripts/hyprial-plugin-package.mjs install
dsh --profile web --host 127.0.0.1 --port 3080
```

`install` 会把包按内容哈希落到 `~/.dsh/hyprial-packages/<sha256>/`，以不可变的
`file:` 依赖写入 profile，并在 profile 内留下 `hyprial-plugin-install.json`
记录版本、哈希与来源提交；重复安装不会复用可变目录。开发迭代需要直接指向
checkout 时改用 `node scripts/hyprial-plugin-package.mjs install --link`。

未执行 profile 注册时，也可以只为当前进程使用仓库 patch：

```bash
dsh --profile web --patch "$PWD/dsh-web.patch.yml" --host 127.0.0.1 --port 3080
```

静态包会进入 `window.__DSH_BOOT__`：DSH 重启会重新加载 Host，浏览器刷新会重新加载 Client，不再执行 `cordis_define` / `cordis_run`，也不需要浏览器批准动态 Package。Host 根据自身模块位置生成 bridge 的绝对路径，因此当前 Session 可选择任意 Workspace；`H2B_DSH_DEMO_CWD` 不参与 bridge 文件定位，只用于指定 worker 实际运行的工作目录。

Host 默认把注入去重、远程入口名、飞书 binding 与直聊状态写入 `${H2B_HOME:-~/.h2b}/state/dsh-web-injected.json`，而不是升级器会替换的 GUI `source/` checkout。固定 bridge 命令仅把该 state 目录设为 `workspace-write` root；package 代码仍只读，普通 Agent Session 选择其他 Workspace 也不会改变 ledger 权限。运维可在 DSH 启动前用绝对路径 `H2B_DSH_DEMO_LEDGER` 覆盖位置。

不要同时把 `h2b-talk` 持久注册到 profile 又通过 `--patch` 插入，否则 DSH 会按重复
loader id 拒绝启动。仓库的 `start:web` 已自动处理这两个模式。

## 动态开发模式（可选）

动态 Cordis 插件不跨重启或页面刷新持久化，只用于快速开发。需要临时加载时二选一：

1. 对 agent 说：**「按 README 同时用 imskin-host-plugin.js 和 imskin-plugin.js 恢复皮肤」**（推荐）
2. 手动两步：
   - `cordis_define`（`plugin.kind: new`，`idPrefix: imskin`，`code.host` = `imskin-host-plugin.js` 的 `return {...}` 主体，`code.client` = `imskin-plugin.js` 的 `return {...}` 主体）
   - `cordis_run`（返回的 pluginId、packageId，`mode: run`）

普通 bridge `status` / `identity` 与 `h2b_session_*` 工具使用同一套 Session 身份解析：有持久化远程入口名或 binding 时查询该远程 Actor，否则查询 Web Actor。远程 Actor 离线时如实返回未注册，不回退到仍在线的旧 `dsh-web-*` 别名。账本内的 Actor 或 Session 引用冲突会明确报错；诊断查询不会注册、迁移或删除身份。显式 Web/remote 生命周期操作仍保留原有语义。

## kanban 薄入口（⚠️ bridge **不在这个包里**）

把**那台机器上**的 TaskWarrior 看板（[HyprialOS/kanban](https://code.hyprial.com/HyprialOS/kanban)）经 Host RPC 暴露给 Client。

GUI 自动读取已安装 Kanban 的路径配置。DSH 工具栏提供本机只读快照；刷新不会执行同步。配置优先级、同步记录、工作台操作入口与验证方式见 [GUI 看板接入](docs/kanban-gui.md)。

```
Host method   h2b-kanban-rpc / h2b-kanban-capabilities
operations    board / board-html / export   —— ⚠️【权威在 kanban-tw】的 tools/panel/bridge.py
环境变量      H2B_KANBAN_BRIDGE     kanban 的 tools/panel/bridge.py 在这台机器上的绝对路径
              H2B_KANBAN_DATA_DIR   TaskWarrior 的库目录（沙箱要放行它）
              H2B_KANBAN_TASK_BIN   `task` 的绝对路径
              ⚠️ 三个都必须在 DSH 进程启动前设置，【一个都不猜】
```

### ⚠️ `H2B_KANBAN_DATA_DIR` 该填什么，以及为什么这里不给默认值

那个值**去问 TaskWarrior 自己**，不要照着习惯写：

```bash
task _get rc.data.location      # ← 这就是该填的值
```

需要它，是因为 **TaskWarrior 3.x 即使只读操作也要在库目录写 WAL/SHM**；
默认沙箱只给 `workspaceRoot + /tmp` 可写 ⇒ 库目录打不开，
报 `unable to open database file: Error code 14`（实测 2026-08-19）。
⇒ 所以这条路把沙箱**收窄到那一个目录**（不是放开全权）。

而这里**没有默认值**，是有意的 —— 上一版写的是 `$HOME/.task`：

```
⇒ 记：一个默认值也是一份复制品。它比显式赋值更难被发现，
  因为它平时【正好是对的】，只在对方改了布局时才错 ——
  而那时它不报错，只是【空板】，而空板看起来完全正常。
⇒ 也记：`~/.task` 正是上面那条判断标准里说的「对方的业务词」，
  而它当初就写在【印着那条标准的同一个文件】里。
```

### ⚠️ 为什么 bridge 不在这里

上一版把它放在包内，而那意味着**别人的业务逻辑住在这个仓**：换掉这个插件就得重写，
而它里面写着 `rc.report.kanban.filter` —— 报表改名要**两个仓一起改**，
而不一致的那一半没人会发现。

```
⇒ 记：一个「薄入口」薄的应该是【这一侧】——
  判断标准很简单：这个仓的代码里出现过对方的业务词吗？出现了，它就不是薄的。
```

⇒ 现在这里只有**一句常量命令 + 一个由运维给出的路径**：
`python3 "$H2B_KANBAN_BRIDGE" rpc`。那个路径**不是浏览器输入**，
所以本仓那条安全属性（没有请求的字节进 argv）仍然成立。
⚠️ 有一格测试专门守它：**这个包的源码里不许出现 TaskWarrior 的配置键或过滤语法**。

⚠️ 没设 `H2B_KANBAN_BRIDGE` 时**明确报错**，不猜一个路径 ——
猜错的样子是「装了但不生效」，而那是无声的。

### 板的沙箱:**由板自己说的那句话决定**

```
board-html 的信封里带 requiresScripts —— 由 kanban 那侧【从产物算出来】
⇒ 这一侧【读它】决定 iframe 的 sandbox：
     requiresScripts=true  ⇒ sandbox="allow-scripts"（⚠️ 不给 allow-same-origin）
     否则                  ⇒ sandbox=""
```

⚠️ **为什么不是写死的**:上一版这里固定 `sandbox=''`,理由写在注释里 ——
「评审 2026-08-19 数过:render.py 出的页面里 `<script>` **0 个**,一点代价都没有」。
**那句话当时是真的。** 后来 kanban 那边加了筛选,页面里就有脚本了,
而这句话没人改、也没有任何东西守它:

```
⇒ 筛选在这条路上【从来没工作过】——
  下拉里的"值"永远是一个 disabled 的 "—"，看起来像"没有可选值"，而不是"坏了"
⇒ 而两边的判据当时都是绿的
```

> **⇒ 记:一个当时为真的事实,一旦被【另一个仓】写成前提,它就需要一个守它的东西 ——**
> **而"事实"这种东西不会自己报警。**

⚠️ **`allow-same-origin` 一直不给**:iframe 仍是 opaque origin,拿不到
cookie / localStorage / 父页面。给的只是"它能跑自己的筛选"。
⇒ 有一格测试把这一条钉死(两个方向 + 新窗口那条路必须用**同一个**决定点)。

### ⚠️ 最低兼容的 kanban 版本

```
kanban-tw ≥ 73f1db4（板产物带 <meta kanban-requires-scripts>，bridge 信封带 requiresScripts）
```

比它旧的版本：`render.py` **已经**产出筛选脚本，而信封里**没有**那个字段。

⚠️ 而这一侧**不猜**：信封里缺 `requiresScripts`（或它不是布尔）⇒ **明确报错，不出板**。

```
若猜成"不需要脚本" ⇒ sandbox='' ⇒ 筛选脚本不执行
⇒ 用户看到一个【一直禁用的 "—"】，而屏幕上看不出哪里坏了
⇒ 那正是这条线最初那个 bug 的原样重现，只是触发条件从"沙箱写死"换成了"两个仓版本不齐"
```

> **⇒ 记：一个"缺失时取保守值"的默认，要先问【什么情况下会缺失】——**
> **若答案是"对面是旧版"，那它就不是保守，是把一次【不兼容】变成了一次【无声的功能关闭】。**

📌 而同一条链的两端现在**处置一致**：kanban 侧 bridge 读不到那个 `<meta>` ⇒ 报错；
这一侧信封里缺那个字段 ⇒ 也报错。**读不到就响。**

### 「新窗口」入口

标题栏的 **⧉ 新窗口** 会另开一个窗口,里面放**同一个 iframe、同一个 sandbox**。
⚠️ 它**不是**把 HTML 直接写进新窗口 —— 那样板会同源且无沙箱,比 popover 里还宽。
> **⇒ 记:一个安全相关的取值,若有第二个产地,那它实际上没有产地。**

### 看板视图的已知限度（都是实测量出来的）

```
① 板有一个【字节天花板】：kanban 这条通道 stdoutMaxBytes = 4MB
   ⚠️ 原先是 262144（256KB），而实测 118 张卡的 board-html = 243152 字节 ——
     JSON 转义之后就超 ⇒ 真机上板打不开。而板【只会越来越大】：
     done 列按设计不设上限。⇒ 改成 4MB，按每张约 2KB 估约 2000 张卡。
   ⚠️ h2b 那条通道【没有连带放宽】，仍是 256KB —— 它的载荷本来就该是小的
   ⇒ 撞上时是「output exceeded the safety limit」，响的，不是静默截断
② kanbanRpc 【不问 capabilities】（demoRpc 会问）
   ⇒ 在旧 Host 上，这条路不会给出「请重启 DSH 加载新版 Host」那句话，
     而是以另一种形状失败。真正拦住它的是 Host 的 allowlist（当场拒掉）
③ 那格「client 里不许出现列名」只读 `imskin-plugin.js`，【不读生成物】
   ⇒ 生成物另有一句断言只核了"含 board-html"。眼下不构成问题（它是构建出来的）
④ ⛔ **新开第三条挂载路径而不用 boardSandbox()，判据看不见**
   现有两处（popover / 新窗口）各有一格正面判据，两种写死都会红；第三处没有。
   ⚠️ 上一版想用"数出全部挂载点"来守它，而"逐行截 // + 正则"这条实现
     既误红（块注释 / 普通字符串）又漏判（违规写在 `src:'https://…'` 后面时被 URL 里的 // 截掉）
   ⇒ 要做对需要真正的 JS 词法分析；yaosh 2026-08-20 判：成本超过价值，降级并明写
⑤ ⚠️ sandbox 那一条【没有端到端的浏览器证据】：
   判定逻辑由四格变异钉住（永远给 / 永远不给 / 新窗口另写一个 / 连 same-origin 一起给）
   而"allow-scripts 那一档里筛选真的能用"在 headless 下反复取不到读数
   ⇒ 它留给 kanban 那侧【在 DSH 网页上点一遍】的那一轮 e2e，不在这里假装验过
```

## 开发验证

```bash
npm ci --ignore-scripts
npm run build:static
npm test
```

Workflow 工作台还需使用完全隔离的 daemon 验证提案工具、真实 CLI 校验、一次性授权、派发/回复、运行快照与复用：

```bash
H2B_BIN="$(command -v h2b)" npm run test:cli:real
```

这个脚本清除继承的 H2B/DSH 身份与配置，使用临时 home、socket 和本机测试目标，结束后停止测试 daemon 并删除临时数据。

修改 `agent.task` Host/bridge、身份授权或 H2B Workflow 对接后，在专用测试环境额外运行：

```bash
npm run test:agent-task:real
```

该命令会注册一个唯一的临时 DSH Session，以该 Session actor 创建真实 H2B
Workflow Run，提交 typed progress 与显式 `result.submitted`，从 daemon 持久层查询
completed 状态和完整结果，ACK 测试投递后注销 Session。Workflow Run 与结果作为
审计证据保留；不会修改业务工作区，也不会向现有 Agent 派发测试任务。

测试覆盖固定命令与 JSON stdin、**kanban 薄入口（两端 allowlist 一致、命令是常量、session 身份注入并覆盖调用方自设的值、退出码 4 保留闸门原话）**、daemon IPC fence、Actor 派生、精确 allowlist、注入 ledger、多 Session 定向 binding、DSH 拒绝时不 mark、Reply/Ack/Send 契约、Cordis timer，以及静态 Host 路由和 Client 启动清单契约。

## 设计要点 / 待办

三人群聊 = 你 + 主智能体 + 子智能体（真实 subagent，由主智能体调度、以第三人块呈现）。

体验顾问 subagent「阿澜」建议的下一步：

1. 状态条拆四态（排队 / 处理中 / 完成 / 失败）
2. 折叠只留「一行摘要 + 步骤数」，完整过程/日志收进右栏详情，主时间线禁止内联展开
3. 头像 = 稳定色 + 首字符 + 状态点

其余待做：header 群聊标题、消息气泡组件化（shadow 消息节点）、会话列表搜索/工作区分组恢复。

### 团队 Codex 兼容版

GUI 安装流程同时安装仓库携带的 `dsh-codex` 兼容构建包。源码位于
`vendor/dsh-codex`，版本包和 SHA-256 清单位于 `packages/dsh-codex`；无需发布 npm
或让团队成员自行编译。Codex 登录、刷新及模型通信使用独立代理进程。
维护、安装及回退见 [兼容版说明](docs/codex-compatibility.md)。

## GUI PAC v2 自动任务（测试分支）

新增 `h2b_pac_*` 原生工具与 Host 派工，支持协调者、实施者、审核者三个独立 GUI
会话，以及授权远端的结构化新任务请求。默认不启用，不复用旧 Workflow v1 面板。
配置、发起任务、返工和故障恢复见 [操作指南](docs/pac/gui-operation-guide.md)。

职责资源及适用边界见 [主仓资源盘点](docs/pac/role-resources.md)，
迁移验证见 [验证记录](docs/pac/validation-20260917.md)。
