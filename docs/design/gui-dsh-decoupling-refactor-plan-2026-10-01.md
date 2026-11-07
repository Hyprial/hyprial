# GUI 与 DSH 解耦重构计划：DSH 回归普通 harness，GUI 回归 hyprial 能力壳

- 日期：2026-10-01
- 状态：方案（未开始实施）
- 关系：骑在 [`refactor-plan-deep-modules-2026-10-01.md`](refactor-plan-deep-modules-2026-10-01.md)（深模块五模块重构）之后或并行，只消费其 `shell`/`daemon` 公共面；src/gui 按深模块方案 §0 第 9 条是独立工程，本计划不改变其仓库归属，只改变其运行时宿主。

---

## 0. 决策问题

1. 本次重构中，能否把 DSH 从 GUI 中剥离，使 DSH 成为与 claude / codex / pi 平行的普通 harness，GUI 回归 `src/hyprial` 的能力壳？
2. 若剥离，GUI 的形态三选一：
   - (a) 继续保持 DSH 耦合（现状）；
   - (b) 独立 React 应用 + hyprial 自持的 Web 网关；
   - (c) 独立 React 应用 + 复用部分 DSH 前端依赖库。

**结论先行**：问题 1 答案是**可以，且架构上已经走了一半**；问题 2 推荐 **(b) 分阶段实施**，(c) 仅作战术性借用（设计 token / 样式契约），(a) 只作为迁移期的持有状态而非终态。

---

## 1. 现状事实（决策依据，均有代码证据）

### 1.1 DSH 已经是普通 harness（worker 侧剥离已完成）

`src/hyprial/harnesses/dsh.py`（1533 行）是与 `claude.py` / `codex.py` / `pi.py` 平行的适配器，其 docstring 自述："DSH already owns the model process, conversation state, and tool runtime. This adapter turns its unary HTTP API into HYPRIAL's small `TurnClient` seam: create or resume one DSH session, queue prompts, observe `turn/end`, and cancel the active turn on interrupt." 即：**headless worker 场景下 hyprial 启动 DSH 进程、经 token 登录的 Web API 建会话、投 prompt、轮询 `turn/start`/`turn/end`、支持中断**。"DSH 变成普通 harness"在 worker 侧已经是事实，无需新建。

### 1.2 GUI 对 DSH 的真实依赖只剩"交互式会话前端"

按功能盘点（依据 src/gui/README.md 与 plugins 代码）：

| 档 | 功能 | 权威在哪 | 对 DSH 的依赖 |
|---|---|---|---|
| A | H2B 控制台（总览/Agent 网络/Workflow/Routine/投递/日志/集成） | hyprial CLI（Host 只映射具名 query 到固定 `--json` 命令） | 仅渲染壳 |
| B | 通讯录、直聊/Carrier、飞书远程入口 pin、Workflow 工作台表单、PAC 工具 | hyprial daemon（inbox/session.register/delivery/pac）；GUI 侧仅持有绑定 ledger 索引 | 薄桥接 |
| C | 工作会话本体：`@Agent` 双发、`h2b_session_*` 模型工具、飞书值守唤醒进对话 | **DSH 会话引擎**（模型上下文、turn 生命周期、工具渲染都在 DSH 进程内） | **真耦合** |

A/B 两档的剥离是纯搬运：GUI 的 Host RPC 今天就是把 daemon IPC/CLI 包成浏览器可调用的形状，换成任何 Web 后端都一样成立。全部难点集中在 C 档。

### 1.3 hyprial 已有 C 档所需的引擎，缺的是"投影给前端的面"

已有：

- `harnesses/turn_runtime.py`：harness 中立的串行 turn 泵（"Harness-neutral serial turn pump"），turn 准入、FIFO 所有权、稳定投影；
- `harnesses/turn_ports.py`：`EnqueueTurnCommand` / `InterruptTurnCommand` / `TurnDeliveryProjection` 等冻结类型——**人类向活会话里发消息、排队、打断的命令面已存在**；
- `harnesses/streaming.py` + `interactive_carrier_runtime.py`：受管流式运行时与交互 carrier 状态机；
- `inbox/progress.py`：`hyprial-progress-event/v1` 进度事件契约（route C），golden-tested；
- daemon IPC：unix socket JSON 协议，GUI bridge 今天就在用。

缺口（C 档剥离必须先补的四块）：

1. **内容级流式输出面**。progress 契约明确划界："`seq` is never a reassembly cursor … This is the wall between route C and chunked streaming"、"A progress event is never authoritative output"。现有事件面刻意不支持正文流式——需要新路由（route D：content deltas）或接受"进度摘要 + 终态全文"的较粗渲染。
2. **会话投影存储**。daemon 持久化的是 delivery 与终态 result；带工具调用明细的完整对话历史目前留在各 harness 进程内（DSH 会话历史即是如此）。GUI 要渲染任意 harness 的会话，需要 daemon 侧的 harness 中立对话投影（消息、工具卡、子 agent 块的统一记录模型）。
3. **Web 网关**。daemon IPC 是本机 unix socket；浏览器需要 HTTP + WS/SSE 前端。今天这个角色由 DSH 进程内的 Host 插件扮演（`/plugins/h2b-talk/rpc`），剥离后要由 hyprial 自持（归属深模块方案的 `shell` 模块）。
4. **工具卡 / 子 agent 块的结构化记录**。progress event 只携带摘要字符串；渲染"紫色第三人块"级别的结构化 UI 需要工具调用与子 agent 的 typed 记录进入投影。

### 1.4 维持现状的耦合税（不重构的成本）

- 布局包需对上游 `dsh-ui-layout` 的 SHA-256 锁定 bundle 打评审补丁，且因 CLI latest 的 caret 依赖要同时维护 0.1.1-rc.2 与 0.1.5-rc.2 两份 vendored 构建；另有 `dsh-history-compat`、`dsh-gui-focus-compat` 两个兼容补丁包与专门的 release gate（`docs/dsh-latest-release-gate.md`）。
- 权威倒挂的症状已出现：直聊绑定需要 package-owned ledger 恢复（因为会话真相在 UI 层进程与浏览器缓存里）；飞书值守让 DSH Host 承担了概念上属于 daemon 的 headless 消息服务器职责。
- 网络侧 harness 中立、本地交互层 DSH 独占的不对称：用户无法在 GUI 中开一条由 Claude Code 承载的工作会话。

---

## 2. 目标架构

```text
浏览器（独立 React 应用，静态资源）
   │  HTTP + WebSocket/SSE（同源，token 鉴权沿用 daemon 本机信任模型）
   ▼
hyprial shell 模块：GUI Web 网关（Python，随 daemon 或独立受管进程）
   │  只走 daemon IPC 公共面 + contracts/ 锁定契约
   ▼
hyprial daemon：交互式会话为一等 daemon 对象
   ├── turn 泵（turn_runtime，已有）
   ├── 会话投影存储（新：消息/工具卡/子 agent 统一记录）
   ├── 内容流式路由（新：route D content deltas）
   └── harnesses/：claude | codex | pi | dsh | agent_sdk …（平行）
```

- DSH 的位置：与 claude/codex/pi 完全平行，由 `harnesses/dsh.py` 驱动；DSH 自己的 Web UI 不再承担产品 GUI 职责，降级为"power user 可选前端"。
- `hyprial gui` 命令面不变（start/stop/status/upgrade），实现从"启动 DSH + 插件"换成"启动 shell 网关 + 静态应用"。
- 安全模型平移：今天 Host 的具名 RPC 白名单、262144 字节上限、argv 不进任务内容、写操作显式确认等约束，原样落到网关的端点设计上；浏览器依然不直接触 daemon socket。

---

## 3. 三个选项评估

### (a) 保持 DSH 耦合

- 优点：零迁移成本；会话前端（流式渲染、工具卡、子 agent 块、composer、主题）继续白拿。
- 缺点：耦合税持续付息（vendored 补丁 + rc 兼容 + release gate）；权威倒挂与 harness 不对称永续存在；DSH 上游任何 rc 变更都是产品风险。
- 裁决：**不是终态，但是迁移期的合法持有状态**——下文阶段 0–2 期间 GUI 继续在 DSH 上运行，不允许因此阻塞 daemon 侧建设。

### (b) 独立 React 应用 + hyprial 自持 Web 网关（推荐）

- 优点：权威归位（会话真相在 daemon，浏览器是可弃投影——符合"失联韧性"第一原则）；harness 对称（交互会话可选任意 harness，DSH 只是其一）；耦合税终止；GUI 发布不再随 DSH npm latest 起舞。
- 缺点：要自建 DSH 今天白给的东西——composer、流式渲染、工具卡/子 agent 块渲染、主题系统；这是 DSH 抵押贷款的**本金**，也是本计划的主要工作量（集中在阶段 3）。
- 风险可控性：A/B 档功能今天已是"CLI 映射 + 渲染"，移植是确定性的；C 档风险集中在流式路由与投影模型设计，可用 contract/ 金样测试锁定后再动工。

### (c) React + 复用 DSH 前端依赖库

- 调查结论：DSH 的前端产物**不是可独立使用的组件库**。`gui-layout` 的 client 是由 vendored bundle 加补丁生成的 provider，依赖 DSH 运行时的 `__ModuleLoader__`、`window.__DSH_BOOT__`、ctx 注入的服务（layout/theme/session）；静态 client.js 的 React 也由 DSH module loader 提供。把这些库摘进独立应用，等于把 DSH 的客户端架构（模块加载器 + 服务注册表 + Host RPC 形态）整体搬进来却没有了它的服务器——省不下渲染逻辑的重写，反而继承一套无人维护的适配层。
- 裁决：**不作为架构选项**。仅允许战术性借用：设计 token 与样式契约（`shared/gui-style.mjs` 本来就是本仓产物）、三栏导航的交互模型、消息气泡/第三人块的视觉规范——以"照着重写"的方式吸收，不以 npm 依赖的方式引入。

---

## 4. 阶段计划

总原则：**契约先行、逐档切换、随时可回退**；每个阶段结束 GUI 都处于可用状态，不出现"新旧都不完整"的窗口期。

### 阶段 0：契约锁定（不写产品代码）

产出（全部进 `contract/`，金样测试守门）：

1. `gui-session-projection/v1`：harness 中立的会话记录模型——消息（人/agent/系统）、工具调用卡（名称、状态、摘要、明细引用）、子 agent 块（身份、状态、任务、结论）。对齐 DSH 侧现有渲染语义（气泡、第三人块四态）以免 UI 重写两遍。
2. `gui-content-stream/v1`（route D）：正文 delta 流。必须显式回答与 route C 的边界：delta 是否可重排（建议：seq 可作重组游标，与 progress 相反，因为它是内容而非观察）、断线重连的续传语义、与终态 reply 的权威关系（终态仍是唯一权威，delta 可整体丢弃重放）。
3. 网关端点清单：把今天 Host 的 OPERATIONS 白名单逐项映射为 HTTP/WS 端点，标注保留/改名/退役。

验收：三份契约过 contract 黑盒测试；深模块重构组确认归属（投影与流式路由属 daemon，网关属 shell）。

### 阶段 1：daemon 侧会话服务（daemon + shell 模块）

1. 交互式会话成为一等 daemon 对象：创建/列举/归档/恢复，内部挂 turn 泵，harness 可选（dsh 作为首个验证 harness，claude 紧随其后以证明对称性）。
2. 会话投影存储落地（daemon 持久层，WORM 追加，沿用 inbox 的权威语义）。
3. route D 内容流式在 `harnesses/dsh.py` 打通：DSH 适配器当前是 unary + turn 事件轮询，需要评估 DSH Web API 是否暴露增量输出；**若不暴露，dsh harness 降级为"进度摘要 + 终态全文"渲染，并在契约中标记该 harness 的流式能力等级**——这本身验证了"harness 能力分级"设计的必要性。
4. shell 网关：HTTP（静态资源 + REST）+ WS（投影订阅 + delta 流），token 鉴权，沿用 daemon 本机信任模型。

验收：`h2b` CLI 新增（或复用）入口可对一条 daemon 托管会话执行 发消息 → 看流式 → 打断 → 看历史 的全流程；隔离环境 E2E（沿用 `npm run test:agent-task:real` 的临时 home/socket 纪律）。

### 阶段 2：独立 React 壳 MVP（A/B 档全量 + C 档只读）

- 新应用（React + TypeScript，构建产物为纯静态文件由网关托管）：三栏导航、通讯录、直聊、H2B 控制台七分区、Workflow/Routine 表单——这些今天就是 CLI 映射，移植是机械工作。
- 会话视图只读：历史 + 实时投影渲染，composer 灰显。
- 与 DSH 版 GUI 并行运行（不同端口），同一 daemon。

验收：A/B 档功能对照 DSH 版逐项 parity  checklist；真实浏览器验收脚本沿用 `tools/browser/` 工具链。

### 阶段 3：交互 composer（C 档切换，本金偿还期）

- composer：发送、排队、打断、中文输入法组合态处理、@Agent 候选与双发（语义改为 daemon 层实现：本地 turn + carrier 发送，不再依赖 DSH CommandClaim）。
- 流式渲染、工具卡、子 agent 块按阶段 0 契约实现。
- 飞书值守迁移：入站唤醒直接由 daemon 驱动 harness worker（Lark adapter 已具备此链路），DSH Host 的值守职责退役。
- feature flag 灰度：按会话切换新旧前端。

验收：真实收发 E2E（人 ↔ 远端 agent ↔ 本地 daemon 托管会话）；失联韧性演练（杀网关、杀 daemon、浏览器刷新后投影自恢复，无 ledger 类补救机制——因为权威已在 daemon）。

### 阶段 4：退役与收尾

- `hyprial gui` 默认指向新栈；DSH GUI 插件进入维护模式（仅安全修复），一个版本周期后停止随产品打包（`gui_product.py` 的 `_gui` bundle 换成新应用）。
- `vendor/dsh-codex` 保留为 codex harness 的兼容件，与 GUI 脱钩。
- 文档：`docs/gui-*` 系列修订；本计划转入 `docs/notes/` 存证。

---

## 5. 与深模块重构的协同

- 时序：阶段 0 可随时开始（契约与模块边界无关）；**阶段 1 建议在深模块 S1–S6 的边界收敛之后动手**，网关与会话服务直接落在最终的 daemon/shell 模块里，避免搬两次家。若深模块进度落后，阶段 1 可在现有包结构下先行（AGENTS.md：新结构落地前新增代码按现有包结构放置），但必须只经公共面调用，深模块搬家时随包迁移。
- 依赖方向天然合规：网关（shell）→ daemon → identity/kernel，无逆向边。

## 6. 风险登记

| 风险 | 等级 | 缓解 |
|---|---|---|
| DSH Web API 不暴露增量输出，dsh harness 流式降级 | 高 | 阶段 1 第 3 步先做 spike；契约内建"流式能力等级"，UI 按等级渲染 |
| 会话投影模型设计错误导致 UI/契约双返工 | 高 | 阶段 0 用 DSH 现有渲染语义反向校验契约；金样测试守门 |
| 工具卡/子 agent 块的 harness 间语义不齐（claude 的工具模型 ≠ DSH 的） | 中 | 契约取交集起步，harness 特有信息走 `details` 引用不透明 payload |
| composer 体验回退（输入法、粘贴图片、草稿） | 中 | 阶段 3 的 parity checklist 逐项对 DSH 版实测 |
| 团队迁移成本（成员习惯 DSH 设置页、Codex 登录流程） | 中 | 阶段 4 保留 DSH 版一个周期；provider 登录迁入网关设置页（provider_auth 已有底座） |
| 深模块重构时序漂移 | 中 | 阶段 1 允许现有包结构先行，只走公共面 |

## 7. 不做清单（显式排除）

- 不重写 daemon IPC 协议本身；网关是其消费者。
- 不动 `src/hyprial/harnesses/dsh.py` 的 worker 驱动语义（除阶段 1 第 3 步的流式扩展）。
- 不追求 DSH 版 GUI 的全部视觉细节像素级复刻；parity 以功能与信息完整性为准。
- 不在本计划中解决多机 Web 访问（网关仍是本机信任模型；远端访问沿用 SSH 转发纪律）。
