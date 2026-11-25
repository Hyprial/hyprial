# Actor Rustify：Rust / Erlang / Elixir 通用执行与 handler 生态调研

日期：2026-10-24。状态：WIP 候选调研与取舍依据，已选择 Ractor，尚未接入产品。主讨论入口为 #1897 / `intg/actor-rustify → dev`，目标和上下文见 [讨论与计划](actor-rustify-discussion-and-plan-2026-10-24.md)。

当前决定：[Ractor + Tokio + PyO3](actor-rustify-runtime-decision-2026-10-24.md)，node-only；[接口 v0.2](actor-rustify-interface-v02-2026-10-24.md) 为评审草案。以下为候选能力记录；自建 Tokio/Tower executor 建议已因维护成本撤回。

调研目标：Python 保留领域规则，Rust 提供领域无关的通用执行能力；单一 CommandHandler/PyO3 入口，目标产品移除 Pykka。没有要求把所有代码模块各自变为 actor，也没有假定引入 Rust 会自动改善所有 handler 的成本。

## 1. 这是常见模式，但要分清现成部分

Erlang/OTP 把通用进程机制和具体逻辑的分离称为 **behaviours**：behaviour module 提供机制，callback module 实现具体处理。这是用户所说“actor model + handlers”的直接先例。[OTP Design Principles](https://www.erlang.org/doc/system/design_principles.html)

不过，现成框架通常只负责调用本语言中的业务 callback。它们并不自动把 Python 领域逻辑变成无 Python 回调的 Rust 数据命令执行器。Hyprial 仍需定义自己的小型通用操作集合、数据边界和既有完成责任；该部分不能由“选了一个 actor 库”代替。

本目标包含三种可组合的东西：

| 层次 | 要复用的东西 | 不应误认为 |
|---|---|---|
| 执行基础 | task、mailbox、按资源顺序、调度、监督、关闭 | 自动拥有领域业务语义或 durable 消息责任 |
| 通用处理组件 | request/response、限并发、超时、批处理、资源操作 handler | 每个组件都必须变成独立 actor |
| Python→Rust 数据命令适配 | 明确输入、前置条件、操作计划、通用结果 | 把任意 Python callable 交给 Rust 执行且无跨语言成本 |

## 2. Erlang/OTP：优先借鉴的结构

| 能力 | 官方提供什么 | 对 Hyprial 的借鉴 |
|---|---|---|
| `gen_server` | 通用资源所有者及 call/cast 接口；callback 返回 reply/state 等结果 | 回复、状态及后续动作应显式表达；不要藏在无类型回调的旁路里。[官方说明](https://www.erlang.org/doc/system/gen_server_concepts.html) |
| `gen_statem` | 事件驱动状态机，区分状态、事件与动作，支持多种 timeout 与事件延后 | generation、重试和恢复流程可以表达为状态转移；无状态变化并不自动意味着要发布变化。[官方说明](https://www.erlang.org/doc/system/statem.html) |
| `gen_event` | 一个通用 event manager 可动态挂多个 handler | 这是“一个运行机制 + 多个 handler”的具体实现；代码复用不要求每个 handler 单独有 mailbox。[官方 API](https://www.erlang.org/doc/apps/stdlib/gen_event.html) |
| `supervisor` | 子进程生命周期与重启策略、重启强度限制 | 监督机制与工作逻辑分开；重启策略显式配置，而非各业务自己再加一套重试循环。[官方 API](https://www.erlang.org/doc/apps/stdlib/supervisor.html) |

这些 API 属于 BEAM/OTP，不能作为普通 Rust crate 嵌入 PyO3 模块。直接使用意味着引入 BEAM 运行时和新的互操作边界，是另一条总体架构选择。本轮以借鉴为主。`Rustler` 用于从 Erlang/Elixir 调用 Rust NIF，并不会把 GenServer 变成可直接嵌入 Python 的 Rust actor 库。[Rustler](https://docs.rs/rustler/latest/rustler/)

`gen_event` 的合并 handler 也不表示可以把任意慢 I/O 都放进一个串行 manager；需要保留资源顺序与阻塞执行的隔离。这是设计借鉴，不是要求全 daemon 只有一个进程/队列。

## 3. Elixir：避免过度 actor 化与有界工作流

| 能力 | 可借鉴之处 | 适用边界 |
|---|---|---|
| `GenServer` | 通用服务器机制与业务 callback 的分离 | 官方明确指出进程应表达运行特性，而非用于代码组织；把简单计算都藏在 GenServer 后可能形成瓶颈。这直接支持取消容器 get/contains 一次一条 actor RPC 的做法。[使用边界](https://hexdocs.pm/elixir/GenServer.html#module-when-not-to-use-a-genserver) |
| `Task.Supervisor` / partitioning | 短期工作独立受监督，调度者本身也需要分区 | 可以借鉴 job 的生命周期管理，避免每个临时动作独立创建完整业务 actor；监督者自身不是无限容量。[官方说明](https://hexdocs.pm/elixir/Task.Supervisor.html) |
| `GenStage` | consumer 发出 demand，producer 按需求供给 | 借鉴下游容量驱动供给，避免维护循环不问下游是否能执行就持续生产工作。[官方说明](https://hexdocs.pm/gen_stage/GenStage.html) |
| `Broadway` | 接入 pipeline、批处理、acknowledger 与失败处理 | 借鉴批次大小/时间和完成处理位置；其自动 ACK 不等于 Hyprial 的存储 ACK/消费 ACK，不能直接替换既有消息责任。[官方说明](https://hexdocs.pm/broadway/Broadway.html) |

直接采用这些实现同样需要 BEAM。它们是成熟模式的参考，不是当前 Python+Rust 嵌入方案的无缝依赖，也没有在本轮测量其性能。

## 4. Rust：可直接进入原型的组件

以下版本是本次访问官方文档时看到的版本，不是产品已经锁定的依赖。

| 候选 | 类型与已核对能力 | 对当前目标的评价 |
|---|---|---|
| **Tokio + Tower 0.5.3** | Tokio 的任务/通道支持资源所有者模式；Tower 提供 `Service<Request> → Future<Result<Response, Error>>`、readiness 和可组合 Layers | 最贴近统一 request/result 入口与一组通用 handlers；Tower 不是 actor framework，需自行明确状态顺序、监督与 durable 完成责任。[Tokio](https://tokio.rs/tokio/tutorial/channels)、[Tower](https://docs.rs/tower/latest/tower/) |
| **Ractor 0.16.5** | 明确借鉴 Erlang gen_server；actor 的 message/state/arguments 分开，提供监督与 RPC primitives | 如果确实需要 actor 的状态所有权和监督树，值得优先比较；不要将 callback 再接成逐条 Python 调用。[官方 API](https://docs.rs/ractor/latest/ractor/) |
| **Ractor Factory** | job key + payload、worker 池、按 key 路由、队列/优先级等扩展 | 很接近通用 job handler 池；内部仍有 factory/worker 分层，需证明不会重新引入过多队列和不匹配的丢弃/重试语义。[Factory](https://docs.rs/ractor/latest/ractor/factory/index.html) |
| **ractor_actors 0.4.9** | TCP、file watcher、cron、stream 等可复用 utility actors | 最直接的“现成通用 actor 集合”样本，可借鉴部分 handler；文档依赖约束是 ractor ^0.15，与本次 ractor 0.16.5 不同，不能直接假定 latest 兼容。[官方说明与依赖](https://docs.rs/ractor_actors/latest/ractor_actors/) |
| **Kameo 0.22.2** | Tokio task 上的 actor，typed message/reply；明确提供 bounded/unbounded mailbox | 作为精简类型化 actor 候选；有界接纳可直接评估，关闭、kill、重启与已接纳任务责任须对照 Hyprial。[概览](https://docs.rs/kameo/latest/kameo/)、[Mailbox](https://docs.rs/kameo/latest/kameo/mailbox/index.html) |
| **Actix 0.13.5** | Tokio 上的 actor、Arbiter、Handler/Message、监督等机制 | 保留为候选与已有实验参照；普通/同步 actor 的容量、取消、状态实例和阻塞执行规则需适配，不因“Rust actor”而预先宣称更快。[官方 API](https://docs.rs/actix/latest/actix/) |

### Tokio + Tower 的接口匹配为何不足以成为最终选择

用户现在要的是“Python 决定业务，Rust 执行通用数据命令”。Tower 的协议无关 Service/Layer 恰好提供统一调用、readiness 与普通组件组合的接口，不强迫每层成为独立 actor。限并发、限速和过载拒绝有现成组件。[Service](https://docs.rs/tower/latest/tower/trait.Service.html)、[limit](https://docs.rs/tower/latest/tower/limit/index.html)、[load_shed](https://docs.rs/tower/latest/tower/load_shed/index.html)

这是接口匹配度判断，不是性能排名。Tokio/Tower 也不提供完整 actor 的消息循环、生命周期和监督机制。用户明确希望尽量少维护 core，因此仅凭接口相似不足以采用自建执行器；也不应每加一个 Layer 都加一个 Buffer/队列。

本轮已选择 Ractor 复用这些机制，首版不叠加 Tower。容量许可、通用存储操作及原有持久责任仍需集中适配，不宣称现成库自动消除所有应用代码。

## 5. “可直接使用”与“需要我们定义”的界线

可以直接组合或评估的现成机制：任务执行、通道、单消费者资源所有者、readiness、限并发、限速、监督、定时器和批处理组织。

尚未发现已验证、可直接替换 Hyprial 的成品同时满足：Python 业务规则、无逐条 Python callback 的 Rust 命令执行、现有 durable 状态格式和接纳/完成/恢复责任。这个结论是本轮已检查候选的范围判断，不是宣称生态中不存在其他项目。

我们需要定义的小型 native 操作面可以先围绕条件读写/CAS、参数化事务、队列/计时器等少量原语实验。SDK-specific 操作仍在 Python 边缘；原生核心不出现 Inbox/PAC/Lark/Jev 语义。不要为了支持任意 Python 控制流而演变成一门新的通用脚本虚拟机。

普通 actor 框架里的 callbacks 通常仍运行在该框架语言/VM 内。若让 Rust callback 每条消息再执行 Python callable，就回到了已测得更慢的简单适配。数据命令入口和跨语言成本仍然必须独立验证。

## 6. 下一步可判定的实验

1. 冻结一个端到端输入：Python 从业务请求生成通用计划，native 执行后返回结构化结果，Python 解释；计入所有转换及提交完成，不能只比较空 actor。
2. 按选定的 Ractor 实现原型，以相同批量接口的 Python 路径对照；固定容量、执行语义和并发，不并行接入多个产品 runtime。
3. 覆盖无变化命令、同 key 顺序、不同 key 并发、队列拒绝、外部等待者超时、已接纳操作的晚到完成及重启恢复。
4. 比较单条与自然批量命令；保持真实工作量一致，报告 command 数量、每命令 CPU、端到端延迟、线程与内存。所有 raw evidence 落仓库。
5. 不从标准库/框架文档的示例耗时推算 Hyprial 性能，也不把 BEAM 进程的特性直接当作 Rust/Python 线程特性。

当前已选 Ractor，并完成有限语义实验；未运行新增组合的真实热路径或 BEAM 实验，未改变产品依赖。Inbox 热修独立推进。选型不是迁移 PASS。

后续已完成[现有Dispatch内存功能的原生标定](../notes/ractor-dispatch-performance-2026-10-24.md)，进入[正式实施规划](actor-rustify-implementation-plan-2026-10-24.md)。SQLite/完整daemon、BEAM与移动端仍未测；产品依赖尚未改变。

后续决策与补充：用户已确定采用 Rust 路线，BEAM 仅借鉴设计理念。Actix 生态已有 actix-service 的 Service/Transform 和组合方法，Actix Web 在其上提供 HTTP middleware，不必默认在 Actix 上再叠 Tower。Ractor+Factory 的现成 key 路由/worker 管理与额外调度层的取舍，以及框架无关的六类 handler、组合函数和 callback 草案，见 [接口草案 v0.1](actor-rustify-handler-contract-draft-2026-10-24.md)。
