# Handler 消息与执行行为清单：覆盖边界

状态：为 #1897 评审补充的静态清单及首版范围核对，不是全量迁移已通过证明。

本文件记录现状证据；目标去向另见[61项分层处置建议](actor-rustify-entry-layering-proposal-2026-10-24.md)。该建议表已覆盖全部入口，但不会改变本文对动态路径和未完成语义核对的限制。

源 head `bede7aa85673d1a3a744893130173421da010470`，产品树基于 dev `e384bba7bf112baf5af27612bafbd0b4c11db32b`。使用 AST 解析，不 import 或运行产品代码。

## 1. 这次增加了什么

旧清单只统计 61 个 ActorSpec 构造点。新清单解析了每个 factory 的实际 handler 入口，并提取 handler/helper 中的消息分支和 EffectLane 的 execute 路径：

- 61/61 个注册点解析到具体函数和文件行号。
- 451 个 handler 条件分支，157 个 effect executor 条件分支；**不是 608 种独立消息**，其中有 envelope 检查、payload 子分支和完成事件。
- 每个分支记录属性/容器写入、SQL verb、存储/I/O 调用线索，以及 tell/submit/wait/publish 等交接。
- 当前源码有 61 处 ActorRuntime 构造表达式、14 个名为 `_Reply` 的类定义；这些是静态位置，不是运行实例数或线程直方图。评审中的 51 与本次口径不同，不能直接推导 737 个生产线程的组成。

完整 handler 索引、消息分支 CSV、压缩原始 `catalog.json.gz` 和扫描/渲染脚本均已落仓。（复现材料已于 2026-11-06 移出仓库，原件见 `cb2584a8b` 及之前的历史）

## 2. 首版相关消息的语义核对

| 入口 / 消息或操作 | 读取 | 内存状态变化 | 持久变化 / 外部 I/O | v1 对应 |
|---|---|---|---|---|
| DispatchState GET/CONTAINS/VALUES/ITEMS/LENGTH/SNAPSHOT | 是 | 不应由查询引入业务变化 | 无存储 I/O | 一次按需/批量 Snapshot，避免逐 get RPC |
| DispatchState PUT/POP/SETDEFAULT/CLEAR/COMPARE_* | 读已有值 | 是，条件操作可能 no-op | 无存储 I/O | StateCasBatch，显式 changed |
| Inbox AdmittedPending/AdmittedDispatchable | 读待处理消息 | 会修改准入窗口/记忆 | 本路径无持久变更；仍留原权威串行处理 | Python 规则计划 + Snapshot/CAS；不能当作任意无状态纯读搬出权威 |
| Inbox FetchPending | 读当前候选 | 准入状态可能变化 | fetched stamp、ledger、失败状态、receipt 等可能实际写入 | 单资源事务，变化由完整操作证明 |
| Inbox BatchSnapshot | 选取/游标重放 | 准入状态可能变化 | `_batch_stamp` 会写 stamp/ledger/receipt；首次和稳定重复不同 | 条件事务；禁止无条件归为纯读 |
| Inbox DrainSystemNotices | 读通知 | 随存储投影更新 | 有行才删除；空操作不能发布假变化 | 条件事务 + scoped ChangeFeed |
| Inbox Receive/ACK | 读取现有事实 | 更新相应投影/准入 | 真实持久写入，含幂等路径 | Transaction + 已提交变化发布 |
| Inbox Submit/Retry/IO completion | 读 outbox/custody | pending I/O / completion 状态 | 事务和外部投递责任组合 | 不宣称 v1 三个 handler 已完整覆盖；需后续 I/O 驱动/责任映射 |
| InboxReadProjection（actor 之外） | 只读 SQLite | 无 actor 写入 | mode=ro 查询仍有 I/O | native query/snapshot；注册点清单不能代表所有流量 |
| StatePersistence 的 ReadDesiredQuery/ReadJournalQuery | 读取持久/投影状态 | 需保持查询无业务副作用 | 存储读取 | Query/Transaction 读路径；动态 query 枚举需逐项回归 |
| StatePersistence 的 lifecycle/session/receipt 命令 | 读前置事实 | 更新投影 | 持久写入及完成回执 | Transaction；不能因接口同类就宣称已迁移 |
| OrgSpace `_ReadStore` / `_Edit` / `_ImportEnvelope` 等 | 依 operation 区分 | CRDT 内存/缓存 | 查询、journal、文件等混合 | v1不覆盖 CRDT+SQL 多阶段一致性；保留后续 driver 核对 |
| CadenceTick / EffectCompleted | 读调度状态 | active/deferred/ack | effect 内容由固定依赖决定 | 共享 Timer/job；不是每个 tick 都是持久写 |
| Lark gateway Send*/Reply* | 读取 SDK/调用结果 | SDK/等待者状态 | 外部平台 I/O | Python 边缘/后续通用 I/O，不作为 v1 native SQL 路径 |

以上核对结合了实际 service 实现和 #1891 的行为负对照，而不只按命令名称分类。其余入口的全部分支仍保持 `STATIC_NEEDS_SEMANTIC_REVIEW`，在移植前补齐动态委托及提交边界，不能以缺少词法 write/IO 线索当作纯读证明。

## 3. 扫描器的具体限制

- 方法追踪深度至多 4；只处理可静态解析的本类/helper、继承入口和 EffectLane 构造。
- `self.owner.receive`、动态 getattr、注入 service/SDK/callback 等不能全面推断。例如 harness-runtime 的 generation wrapper 入口只转给 owner，其实际命令分派仍须独立核对。
- 属性写入可能只是在改局部结果对象；词法 I/O 名称也不等于一定发生 I/O；故 CSV 是证据索引，默认不标 PASS。
- 一条命令可能先提交、再发外部工作；分类不是互斥标签。读取命令也可能写 admission/stamp，不能按名字推断。

## 4. 对接口冻结的影响

v0.2 只确定 State/Snapshot、单 SQLite Transaction、ChangeFeed 和操作控制的限定接口，不再声称六个家族已经覆盖全部当前 handler。其他迁移项在覆盖表未验证前是“不在首版”，而不是保持 Pykka fallback 或返回伪造结果。

完整 native cutover 的条件仍包括：所有生产入口（含绕 actor 的读路径）有消息级映射、动态依赖和持久责任验证，目标产品只运行一套执行模型。该条件目前未达成，不影响根据限定接口选择首版实现框架，也不能被框架选择替代。
