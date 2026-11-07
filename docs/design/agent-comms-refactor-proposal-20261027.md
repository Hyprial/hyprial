# agent comms 观察集合与 daemon 查询重构提案

状态：**WIP / PLAN ONLY / 等待外部方案评审；产品实现尚未开始。**

本提案按 owner 2026-10-27 的最新指示，参考 [PR #2004](https://code.hyprial.com/HyprialOS/harness-bridge/pulls/2004) 的计划先行流程。这里只提交设计与既有审计证据，不提交产品或测试实现，不申请合并旧功能栈。owner 已指定：由 codex-network 经本人的飞书机器人发送摘要及 PR 链接，owner 人工转交 cli-developer 等外部评审者；评审通过、阻塞项关闭且 owner 转达结果后，再开发。internal-group 网络故障期间不自行改用其他 agent 通道送审。

- 当前固定基线：`dev@3252c112c2ddc29374ce04c3fda1463334d36f65`，本次重新 fetch / 远端读回确认。
- 历史候选：`intg/network-local@44c3b55ea7bdbd5e47e3230b76c3f9a9a2c9b4c8`。
- 新实现集成分支：`intg/agent-comms`，已建立，目前仍等于上述 dev，尚无产品增量。
- 本计划分支：`design/agent-comms-refactor-plan-20261027`，目标为 `dev`；后续四个实现 PR 的目标才是 `intg/agent-comms`。
- 提案作者：codex-network。本文的自检与历史审计不构成独立评审通过。

## 1. 之前完成了什么，哪些尚未交付

原目标是观察本机 agent 的通信拓扑、频次及时间演进。已有四个功能 PR 在旧集成分支中依次落地：

| 历史 PR | 已有能力 | 实际落地位置 |
|---|---|---|
| [#1372](https://code.hyprial.com/HyprialOS/harness-bridge/pulls/1372) | 本地通信元数据、无正文统计投影 | `intg/network-local` |
| [#1381](https://code.hyprial.com/HyprialOS/harness-bridge/pulls/1381) | `org network inspect` 终端摘要 | 同上 |
| [#1382](https://code.hyprial.com/HyprialOS/harness-bridge/pulls/1382) | 离线 HTML、有向拓扑、时间回放与排名 | 同上 |
| [#1383](https://code.hyprial.com/HyprialOS/harness-bridge/pulls/1383) | monitor、增量读取与报告刷新 | 同上 |

四个 PR 均已 merged/closed，但这不等于功能进入 dev。旧提升 [#1402](https://code.hyprial.com/HyprialOS/harness-bridge/pulls/1402) 为 closed/unmerged；已补充由新 intg 替代的说明。旧集成分支、提交及其评审/验收记录保留，不再作为当前新实现的提升来源。

截至本次规划，已完成旧代码盘点、最新架构对照、两项合成缺陷复现、四阶段规划和新 intg 建立。历史审计范围共 46 个路径，其中 19 个属于源代码/契约/测试，27 个为历史证据；逐文件坐标及哈希见 [原实现清单](../evidence/agent-comms-plan-20261027/original-feature-inventory.json)。这些数量描述旧候选，不是本计划 PR 的实现规模。

## 2. 为什么需要重构

这是命令语义、权限边界、数据读取和统计口径共同变化，不能仅给旧命令改名。

| 发现 | 依据与验证级别 | 改造要求 |
|---|---|---|
| 旧 CLI 导入并执行 daemon 的历史读取器，直接接触状态目录/SQLite/日志 | 旧 `commands/org/activity.py`、`monitor.py` 源码核查 | CLI 仅解析、调用既有 IPC、输出；查询与状态访问归 daemon |
| `org network` 已有成员、设备、映射等语义；本功能观察消息通信 | 当前 `commands/org/commands.py` 与 owner 明确纠正 | 不挂载为其 inspect/monitor 子命令，不改变原成员/映射命令 |
| 单中心参数不能覆盖本机全部或用户指定多个 agent；旧过滤实际按 owner/node 纳入其他本机 agent | 合成复现 R1：名单只有 alpha，仍出现 beta→remote | 以 canonical actor 集合精确匹配任一端点 |
| 旧读面遗漏新存储中的 `sender_copies` | 合成复现 R2：存在出站副本行，旧结果计数为 0 | 在消息存储所属层提供 inbox/outbox/sender_copies 元数据只读投影 |
| 旧增量刷新仍扫描 inbox 全键，并重复投影保留历史；不同连接还可能取得不同快照 | 旧 `incremental.py` 源码核查；本轮未重测生产性能 | daemon 管理有界后台扫描、共享缓存和来源水位；watch 不触发每 5 秒全表扫描 |
| 日志出现不等于投递成功；batch 的首消息 turn 不能代表所有成员已处理 | 当前发送与 batch 源码核查 | 消息观测、送达、读取、回复严格分开；本期只承诺可追溯观测统计 |
| 旧 HTML 以整机/owner/node 组织统计，旧退出路径的 `warn` 调用缺少新参数 | 源码核查 | 全部图表使用同一观察集合；组织归属有证据，输出 API 对齐现行规范 |

[诊断结果](../evidence/agent-comms-plan-20261027/diagnostic-results.json) 记录 R1/R2 与两项静态检查。它们是作者先前执行的合成复现和源码检查，本次计划修改没有重新运行产品实验，也没有独立评审者的结论。不能把历史旧版 PASS 或当前基线门禁 PASS 写成新方案已实现、已验收。

## 3. 产品语义与 CLI 表面

功能名称为 `agent comms`，观察对象是 agent 集合，而非 org 或全网。

```bash
hyprial agent comms agent-a
hyprial agent comms agent-a agent-b
hyprial agent comms --all
hyprial agent comms agent-a agent-b --window 7d --json
hyprial agent comms --all --watch --html ./comms-new.html
```

| 输入 | 拟定语义 |
|---|---|
| `[AGENT ...]` | 一个或多个位置参数，CLI 接收 list，IPC 传 JSON array；不要求用户输入 Python tuple 或逗号字符串 |
| `--all` | 本机当前注册的全部 agent，含仍注册但未运行的 agent；合法托管 agent 按本机 registry 判定，不按 URI 的 node 字符串猜测 |
| 选择约束 | 名单与 `--all` 二选一；都未提供或混用均报参数错误，不隐式选全机 |
| `--window` | 仅 `1d` / `7d` / `30d`，默认 `1d`；时间由 daemon 确定并回显 UTC `[start,end)` |
| `--watch` | 默认 5 秒刷新；复用 daemon 的查询状态，不在 CLI 启动日志读器 |
| `--html PATH` | 创建新的离线报告；watch 时只更新本进程创建且未被外部改写的报告 |
| `--json` | 单次为 `CliResult` 的一个 JSON 对象，watch 为 `CliStream` NDJSON；进度/警告走 stderr |

不增加 `--org`、采样条数、数据库路径或轮询间隔等一般用户参数。远端 agent、人类、adapter/route 可以是通信对端；首期不把远端 agent 加为观察中心，不向远端采集历史。

显式名单由 daemon 解析并去重；别名与 canonical URI 指向相同 agent 时只选一次，输入顺序不改变结果。任一成员未知、歧义或越权时整体拒绝，不静默丢弃。`--all` 遇到空注册集合返回明确的空观察集合，不将空集解释为匹配全网络。

`--all --watch` 随 daemon 注册快照变化；响应必须带实际选中的 canonical 集合及成员代际/摘要。变更期间旧快照可以被标为 stale，但不能被重新贴上新成员集合的标签。显式名单仍逐次验证，成员注销不悄悄改变用户选择。

### 与现有命令的边界

- `org network`：组织成员、设备、映射与连接配置；保留原行为。
- `message send/read/reply/ack/batch`：业务消息的发送、读取、消费与确认；本统计不会触发这些副作用。
- `message status/trajectory/history`：某条消息、会话的状态或记录；可作为理解现有事实和鉴权的参考，不通过逐条 CLI 调用拼成本功能。
- `agent comms`：指定集合参与的通信拓扑与观测频次；不展示正文，不把可视化状态当作业务确认。

## 4. 集合统计与来源规则

令实际观察集合为 `S`，仅当 `sender ∈ S OR recipient ∈ S` 时纳入该接收分支。保留跨 org 和集合内部通信，排除集合外对端之间的边。

计数单位为去重后的 `(messageId, canonical recipient)` 接收分支。一个消息发往两个接收者产生两个分支；同一分支出现在多个数据源仍只计一次。必须先查齐候选分支的相关来源、归一化并处理冲突，再筛时间窗与集合。仅用低权威 sender/时间做预筛选可能隐藏权威来源冲突，不能据此优化。

四个互斥分类：

| 分类 | 条件 |
|---|---|
| 对外发送 | sender 在 S，recipient 不在 S |
| 外部接收 | sender 不在 S，recipient 在 S |
| 集合内通信 | 两端都在 S，且不是同一个 actor |
| 自发自收 | 两端是 S 内同一个 actor |

`总观测分支数 = 对外发送 + 外部接收 + 集合内通信 + 自发自收`。每 agent 的发送/接收量可以分别呈现，但不能把它们相加作为集合总量。例如 A→B，S={A,B} 时总量为 1；A 的发送和 B 的接收各为 1。

来源计划：读取现有 inbox、outbox、sender_copies 元数据，以及已存在的结构化日志；不新增消息权威库，不从原始对话正文反推通信。持久消息元数据优先于诊断日志。相同优先级的持久来源身份冲突不任意选一条，而是排除该分支并计入冲突/覆盖说明；只有日志且缺少稳定 ID 或接收分支的事件，不伪造 ID 混进消息总量。字段级来源与时间选择表在 C1 契约中固定，尤其不能让重复日志把窗口外的权威记录重新带入窗口。

日志只表示观测：send 事件可能先于 durable submit，日志队列可能丢弃；batch turn 可只关联首个 messageId。因此本期不从这些事件推出“全部已送达/已读/已处理/已回复”。来源的保留策略和时钟不同，也不能推断跨节点因果顺序。

响应中的 `ready` 表示本次可访问来源扫描结束，不表示历史完整。必须区分来源存在但零记录、缺失、不可读、轮转/清理、时间范围不足和冲突抑制；不能合并为一个 0。

## 5. 调用链、模块归属与现有能力

```text
CLI：参数 / 既有 daemon request / CliResult 或 CliStream / 新建报告
                  ↓ 现有本地 IPC 协议
daemon：认证 caller → 解析及授权观察集合 → comms 查询服务
                                              ↓
                         消息权威模块的只读元数据端口 + 日志适配器
                                              ↓
                         纯聚合 → 有界、带来源覆盖的快照
```

当前已有 `_daemon_request` / `_daemon_request_once` 和 `ipc/router.py`，不新增通用 RPC、任意 SQL 接口或另一个 daemon。以下路径均相对 `src/hyprial/`：

| 现有位置 | 复用或扩展方式 |
|---|---|
| `shell/impl/cli/commands/common/support.py` | 复用 IPC 与结构化错误处理；不借导出 `read_network_activity` 让 CLI 执行 daemon 业务 |
| `daemon/impl/ipc/router.py` | 仅增加薄方法分发；当前已到 1000 行，先按现有模式提取相关 handler 接线，不能直接追加超限 |
| `daemon/impl/application/messaging/visibility.py` 与 `inbox_surface/history/` | 复用会话 fence 和新读面的严格拒绝原则；观察集合与 caller 身份分别传递 |
| `daemon/impl/composition/views.py` 的 AgentRegistryView | 根据真实 registry 解析本机/托管身份；`agent.resolve` 的 scheme-known 不能替代注册判定 |
| `daemon/impl/inbox/authority/`、`inbox/history.py` | 在所属存储模块增加仅元数据的只读投影，沿用事务与授权边界；不取现有 history 正文返回再丢弃 |
| `daemon/impl/application/wiring/transport.py`、`application/shutdown.py` | 最小生命周期接线，启动/关闭有界查询服务，不把业务塞入 application mixin |

拟定新增归属：`daemon/impl/operations/comms/` 放查询服务、纯聚合和日志适配；存储适配仍在 `inbox/authority/` 的子模块，暴露窄只读端口。`shell/impl/cli/commands/agent/comms/` 放 CLI 与报告。不跨域导入 `impl`，不向公共 facade 导出 reader 供 CLI 直调。目录最多 7 个直接文件、代码最多 1000 行；接近上限的 router/read/facade 先按责任拆分，不靠删空行过门。

### 调用者权限：本提案选择与需评审的条件

观察集合不是调用身份，名单中的 actor 不能被当作 caller。建议首期由本地 owner 操作入口提供多 agent / `--all` 查询；带有效 session 的普通 agent 仅能请求自身可见数据，不能因同 owner/node 自动获得整机图。托管 agent 的可观察权以既有授权端口为准，无明确读取授权就拒绝，不能把“托管已注册”当作可读许可。

新查询不继承 sender-auth 的 observe 兼容放行；失效、过期、其他 agent 的 session 必须拒绝。当前 `_identity_binding_operator` 明确包含 unverified operator 兼容路径，它不是完整鉴权；不能简单把“没有 session 参数”解释为 owner。当前 message history 的 `_authenticated_message_caller` 是可参考构件，但其会话主体规则不能直接替代多中心授权。

**请 cli-developer/身份模块评审者确认现行 operator 可信入口与多中心读取授权的公共接口。** 若安全调用身份不能由现有链路可靠取得，必须先增加所属模块的受审查能力；不得在本功能中伪装 user、接受任意 `from` 或扩大 worker-channel 方法白名单来绕过。普通 agent 自身查询与 owner 全机查询的区分是本期安全范围，不承诺未批准的跨 owner 监控。

## 6. 拟议 IPC 与有界执行

方法名拟为 `agent.comms`；以下是待 C1 固化的请求/响应形状，不是已发布协议：

```json
{"selector":{"mode":"explicit","agents":["agent-a","agent-b"]},"window":"1d"}
```

`selector.mode=all-local` 时不允许 agents 字段；认证上下文沿用经评审的现有请求绑定，不能放在 selector 内。输出白名单包括 schemaVersion、state、实际 selection、window、snapshot、sources/coverage、totals、nodes、edges、timeBuckets、warnings；不返回正文、逐消息 ID 清单、私有存储路径或会话凭据。

快照携带 daemon epoch、snapshot ID、生成时间、数据截止时间、年龄、成员摘要及来源水位。缓存键至少包含规范化观察集合、窗口和授权可见范围；不能跨 caller 权限复用未裁剪内容。

- `warming`：已排队/正在扫描，迅速返回状态；首次没有完成结果时不给假零统计。
- `ready`：该代际可访问来源扫描完成，覆盖缺口仍随响应返回。
- `stale`：旧快照可展示，但明确原 selection/window/时间；后台更新中，不重贴新标签。
- 参数、权限、来源失败和资源超限使用结构化错误/覆盖状态；全量来源不可读不能返回成功空图。

后台扫描由 daemon 拥有，多个相同请求合并；不能在 IPC handler 持续占用客户端槽位等待冷扫描。CLI 退出只结束观察，后台任务在无人引用、过期或 daemon 关闭时按生命周期取消/回收；不是一个持久业务工作流。

提议初始内部预算为：最多 2 个扫描任务、最多 8 个查询缓存项，单快照序列化上限 4 MiB（低于现有 IPC 8 MiB 帧限制），单来源读取用有界 keyset 页，扫描内存设总预算。具体页大小/内存字节预算与 P95 时延需在 C2 隔离测量后定稿；若无法满足，不允许静默裁边、漏 agent 或用近似去重冒充精确总量。超限给结构化原因和覆盖说明；正常全机样本必须在已批准预算内完成，否则 C2 不通过。

增量策略使用存储所属层的变更代际/有效游标和日志 file identity/offset；缺失可靠变更信号时做有界重建，不以 rowid 递增假设忽略删除/清理。消息候选发现和补齐去重来源在一致读事务中完成；日志与数据库无法组成同一事务，分别标注水位，不声称全源原子时刻。日志轮转、半行、截断、过期清理、时间窗滑动与缓存淘汰必须保留去重正确性，必要时重新 warming。

查询不得执行 DDL、迁移或构造会写库的 InboxService。若 EXPLAIN/性能结果证明缺少索引，由存储归属模块按正常迁移路径单独审查，不在读请求里创建索引。全量历史扫描不能藏到 watch 每次刷新里。

## 7. 旧实现的取用与退役

以下均为历史候选中的路径；不把旧分支整包合并到新基线。

| 旧组件 | 处置 |
|---|---|
| `operations/network/projection.py` | 取用可证明正确的归一化/聚合思路；替换 owner/node 过滤、重复统计和含糊状态语义 |
| `operations/network/history.py` | 替换为消息存储所属层的元数据投影及 daemon 日志适配；补出站副本，保留只读约束 |
| `operations/network/incremental.py` | 重做 daemon 生命周期、有界扫描/缓存、可靠水位与去重；不搬运 CLI 常驻 reader |
| `commands/org/activity.py`、`monitor.py`、相关注册 | 替换为 `commands/agent/comms/`；不注册旧 org 子命令别名，以免继续混淆既有 org network |
| `org/network_report/` | 取用离线资产、转义/CSP、文件保护；重做集合拓扑/指标和刷新消费层 |
| `daemon/interface.py` 的历史 reader 导出 | 不带入新实现；若届时 dev 已出现调用者，先列清并迁移，不盲删当前公共能力 |
| `contract/wire/network-activity.md` 与旧测试 | 历史保留；新增 `agent-comms` 契约和针对当前行为的测试，不将原通过数继承给新版本 |

旧四 PR 与历史卡保留完成事实；#1402 保持关闭未合并。新实现最终逐项记录旧文件/行为到新 PR 的映射。原始数据和已有报告快照只读，不覆盖、不删除。

## 8. 完整用户旅程与验收

用户选择 A/B 或本机全部 → CLI 请求 daemon → daemon 验证 caller/集合 → 返回 warming 或快照 → 显示集合内与外部的有向通信 → 按窗口查看频次/排名 → 可选导出离线报告或持续观察 → Ctrl-C 清理观察并保留可离线阅读的最后完整报告。

必须能从单一快照核对终端、JSON、HTML 的实际集合、窗口、总数和方向。组织分簇只有在 registry/已有目录提供真实归属时显示；多归属以标签表达，不复制节点，也不把 owner/device 当 org。全机集合改变时图与统计一起换代。

HTML 新建目标若已存在即拒绝。watch 使用临时文件与原子替换，只更新本进程创建、摘要/身份仍匹配的报告；外部改写或符号链接替换时停止更新，不覆盖用户文件。报告仅嵌入输出白名单元数据、离线资源及 CSP，动态字符串转义，禁用远端追踪请求。正常退出去掉自动刷新；异常退出后旧文件仍可离线读。

| 验收组 | 必测内容 |
|---|---|
| 集合与计数 | 单个/多个/全部、输入乱序与重复别名、A→B只计一次、自收单列、fanout按接收分支、集合外边排除、跨org保留 |
| 来源与时间 | inbox/outbox/sender_copies/日志多源去重；冲突不被预筛选隐藏；先去重再窗口；UTC边界；缺失与零不同；batch不虚构成员状态 |
| 身份与只读 | operator与session边界、过期/伪造/越权拒绝、合法托管、零消费/ack/写库；读取前后原数据及附件哈希不变 |
| 生命周期 | 冷扫描时并发IPC仍响应；请求合并、超限、轮转、半行、删除、窗口滑动、缓存回收、daemon重启/关闭 |
| CLI与报告 | help/参数规范、单JSON/NDJSON、stderr与退出码、Ctrl-C、断连；真实浏览器检查箭头/集合/排名/离线/CSP与文件覆盖保护 |

所有行为变化先有失败测试再实现。C2/C3 用真实隔离 daemon/IPC；C4 用真实浏览器和真实终端。测试 home、state、socket、凭据均隔离，不探测生产数据。记录测试 SHA、实际导入路径、输入规模、来源覆盖与结果；不使用 skip 或旧分支结果冒充通过。性能同时测冷启动、无变更刷新、追加、轮转/删除、多个观察集合，检查重复刷新无整库全键扫描。

## 9. 四阶段线性开发与新 intg

本节为方案评审通过、阻塞项关闭且 owner 转达结果后的安排，不能被看板状态或旧请求自动触发。

| 顺序 | 分支 / 职责 | 合入前出口 |
|---|---|---|
| C1 | `feat/agent-comms-contract`：集合/指标/来源/错误/快照契约与纯聚合 | 单多全、内部去重、fanout、自收、冲突、窗口红绿测试；协议形状明确；权限接口未定不解锁C2 |
| C2，依赖C1 | `feat/agent-comms-daemon`：权威只读投影、授权解析、IPC与有界后台服务 | 真实隔离IPC、只读/越权、来源补齐、冷扫描并发/预算/关闭；不得通过直接读私有表绕开模块接口 |
| C3，依赖C2 | `feat/agent-comms-cli`：列表与--all、IPC-only输出与watch | 帮助/清单/错误/退出/断连与实际IPC覆盖；无DB/日志回退；统计与快照一致 |
| C4，依赖C3 | `feat/agent-comms-report`：HTML、浏览器/性能/集成验证与提升准备 | 报告保护、统一图表口径、文档/skill/契约同步；完整新intg证据及dev提升申请 |

仅允许一条实现/评审/验收/落地链在途。每卡从新 intg 最新实际落地 head 建分支，PR base=`intg/agent-comms`；前一片独立评审和适用验收通过并实际落地，才创建下一片实现分支。已评审 head 不 rebase/amend；需要上游更新时 merge-forward，并对新 head 重新验证。每片执行适用焦点测试、结构/模块边界/ruff，以及仓库要求的全套检查，已知基线失败须绑定实测对照而非直接豁免。

新 intg 从最新 dev 建立，当前为空。评审放行后开工时再次核对实时 dev；若主线已前进，先按已批准的线性策略更新基线并记录新 SHA。四片完成后另开新的 dev 提升 PR，执行届时门禁；作者不自签独立评审、不自行合并或部署。计划 PR 的评审不等于实现 PR 验收。

## 10. 看板、人工交接与开工条件

旧卡为 `49d78c92-71fa-4fb2-8394-01c480dfb40c`、`a1799a5b-b4da-46ad-b85f-fa26a2392ece`、`f4fade7e-be40-435d-9cf5-28f2dd662b79`、`5592343b-93a7-4305-8a33-1da76a0e4532`。只读顾问返回的 10-21 快照表明 project=`harness-bridge`、owner=`devusr`、四卡均 done/archive；同步故障下不将快照当作今天的查重结果。

此前已提交 project 查询和四卡规划请求，尚未取得落卡回执。**owner 最新要求取代此前“落卡后直接开工”的安排：先计划 PR、人工外部评审，再开发。** 当前不主动重试内部派单；恢复后先核对旧请求是否落板，把新卡与本计划及评审结论关联，后卡设置真实 UUID 依赖。已完成旧卡只加替代说明，不重开清单，不将故障/搁置归档冒充完成。

人工交接顺序：作者自查本计划 → Draft PR → codex-network-bot 发给 owner → owner 转 cli-developer 等外部评审者 → owner 将意见或 PR 评论指针带回 → 修订计划并逐条记录处理 → owner 转达评审放行结果 → 四阶段线性实现。没有自动发起外部 agent 消息、评审代签或自动合并。

请求评审者明确回应：

1. `agent comms [AGENT ...]` / `--all` 是否符合当前 CLI 归属、列表及输出规范？是否与 message/agent 既有命令重复？
2. caller 与观察集合分离、owner 多中心读取、普通 agent 自身读取、合法托管的授权边界，以及应复用的可信 operator 公共入口是否成立？这是开工前必须落定的接口条件。
3. daemon 模块归属、消息存储元数据端口、router/read 等满额文件的接线拆分是否合理？禁止 CLI 直接读库的范围是否闭合？
4. 分支去重、四类总量、日志与持久元数据的事实级别、来源覆盖及成员换代是否满足用户期望？
5. 后台任务/缓存/响应预算与性能验收是否可行？具体内存预算和时延目标由 C2 测量定稿，是否需要额外前置约束？
6. C1–C4 的依赖、新 intg 与旧 PR 退役处置是否可批准？有哪些必须交所属模块审核的接口依赖？

请将结论绑定本 PR 的具体 head，列出阻塞项、建议项和复审要求。外部评审未返回前，本线不将提案自检写成“评审通过”，也不开始产品实现。

## 11. 证据与本次验证范围

- 旧源码/契约/测试按固定旧 intg 定位：[历史候选树](https://code.hyprial.com/HyprialOS/harness-bridge/src/commit/44c3b55ea7bdbd5e47e3230b76c3f9a9a2c9b4c8)。
- 当前调用架构按固定 dev 定位：[计划基线树](https://code.hyprial.com/HyprialOS/harness-bridge/src/commit/3252c112c2ddc29374ce04c3fda1463334d36f65)。
- 随本 PR 附带的两个 JSON 为既有审计读数和旧文件清单，保留原字节；不包含运行原始消息、凭据或用户报告数据。
- 本次修改仅设计文档与审计证据。校验文档链接、代码围栏、git diff whitespace、证据 JSON/哈希及差分范围；不为文档变化跑产品功能测试。
- 同一固定 dev 的结构与模块边界门已在准备阶段通过；这只证明基线，没有新产品实现的测试结果。
- 新方案的功能、真实 IPC、性能、浏览器和全量验收均待实现后执行。现有复现不替代这些验收；新提案未获独立评审。
