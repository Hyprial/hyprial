# Native CommandHandler 接口 v0.2：首版原型基线

状态：接口与首版范围的评审草案；尚未实现或通过迁移验收。补充 v0.1 中未确定的执行语义。已选择 Ractor 作为唯一 actor 框架，见 [框架决定](actor-rustify-runtime-decision-2026-10-24.md)；下面定义应用需要的语义，不要求自建 actor runtime。

范围说明：本v0.2是先行接口草案，不是全量Rustify终态范围。用户要求的S2已扩展为完整生产替换和Pykka删除，见[S2设计](actor-rustify-s2-design-2026-10-24.md)；本文件尚未涵盖的资源能力不能作为S2的永久排除项。

## 1. 范围

- 首版 **node-only**，用于桌面/服务器 daemon；首个原型为当前 macOS arm64。Linux/Windows 需分别验收，不宣称移动端可用。
- 建议首版工作原语只有 State/Snapshot、单 SQLite Transaction、资源级 ChangeFeed，另有同一入口的 operation 控制命令。
- Timer 使用运行时的共享计时器；I/O、Process、CRDT、跨资源事务属于后续驱动，不以“六个家族”宣称覆盖全部 61 个注册点。
- Python 做领域 prepare/finalize；Rust 执行 owned 数据命令，不调用 Python 业务 callback，不承载 Inbox/PAC/Lark/Jev 规则。
- 生产目标只有一套 Rust 执行模型；不新增需要先上线的 Python 池化 actor 产品。旧 Python 实现可以作为隔离行为/批量接口对照。

## 2. 唯一入口与数据

下面定义逻辑 API，Rust/PyO3 绑定语法尚可按实现调整；不能改变这些语义。

```text
CommandHandler.dispatch(Command) -> Outcome 或其 awaitable

Command =
  Execute { operation_id, fingerprint, resource_id,
            expected_epoch, expected_revision?, operation, wait_deadline }
  Observe { operation_id }
  Wait { operation_id, wait_deadline }
  Cancel { operation_id }
  Shutdown { drain_deadline }

Operation =
  ReadSnapshot { keys, since_revision? }
  StateCasBatch { expected_revision, updates }
  SqliteQuery { registered_statement, parameters }
  SqliteTransaction { registered_statements, parameters, preconditions }
  WaitForChange { resource_id, after_revision, wait_deadline }

Outcome {
  operation_id,
  phase: rejected | pending | completed | failed,
  custody: none | volatile | durable(receipt_ref),
  value,
  commit: no_write | unchanged | committed | outcome_unknown,
  durability: transient | persistent,
  resource_epoch, resource_revision,
  changes, error
}
```

资源和 statement 由可信宿主注册，不接受未认证客户端自选任意数据库/路径/SQL。资源句柄、版本、参数和结果是值；不得携带 Python callable、SDK client、DB connection 或 ActorRef。

同一 operation_id + fingerprint 加入同一个操作，不重复入队；相同 ID 配不同内容返回冲突。重放既有结果不重新增加 revision 或发事件。ID/结果保留过期后，如不能从原持久回执证明事实，返回 unknown，不能把缺少缓存当作从未执行。

## 3. 接纳与容量

**只在 CommandHandler 的一个接纳 gate 中决定是否接收新工作。** 通过标准容量许可约束发送到 Ractor 的工作量，之后由 Ractor mailbox/必要的 Factory 管理执行。不叠加 Tower Buffer 或自建 ready-resource 调度队列。Factory 内部派发不构成第二次业务接纳；所有转移继续使用同一 operation 与容量责任。

首轮对照固定以下配置；这些是原型可复现的容量数字，不是未经测量的生产容量承诺。实现必须报告实际配置。

| 参数 | 原型值 | 计数范围 |
|---|---:|---|
| global_outstanding | 1024 | 已接纳、尚未被执行 owner 确认终态的操作 |
| per_resource_outstanding | 128 | 等待和执行中但未结算的操作总数；与旧 mailbox 队列深度口径不同，迁移时须明确对照 |
| per_resource_running | 1 | 同资源最多一个有序操作执行；不是 128 个并行 writer |
| global_watchers / per_resource_watchers | 256 / 24 | ChangeFeed/结果等待的保留观察者；不占写入执行席位 |
| Tokio async workers / blocking workers | 4 / 4 | 共享线程预算，SQLite/阻塞 I/O 不在 async worker 上执行 |
| max inline request/result | 各 1 MiB | 接纳前检查请求并预留结果预算；查询须有有界输出计划，提交后超限不得伪称未执行；大数据分页/handle另行验收 |
| buffered command/result bytes | 64 MiB | 与条数预算共同检查，避免大 payload 绕过条数限制 |
| terminal result cache | 1024 条且受字节预算限制 | 非 durable 真值；persistent 结果的最终依据仍是原数据库回执 |

容量不足立即返回 `rejected/overloaded`，未接纳、无副作用；关闭中返回 `rejected/closing`。不无限等待入队，不自动重试或静默丢弃。观察/取消/关闭在同一入口中处理，不因业务队列满而被困在另一个工作队列；观察者本身仍受上表预算约束。

如果公开迁移路径需要超过原型限额，必须在迁移验收前明确调整配置和对照，不能静默截断或把新限额当作已兼容现有产品。

## 4. 顺序与事务

- 有序操作在接纳时分配 resource-local sequence，并与向资源 owner 的发送顺序一致；同一 epoch 内串行执行。不同资源可并行，调度交给 Ractor/Tokio，不自建轮转器，也不宣称框架保证严格 round-robin。多提交者顺序与繁忙资源的影响必须实测。
- `ReadSnapshot` 读取当前已发布的不可变快照，可以不进写队列；它不承诺排在已接纳但未完成的写之后。需要 read-after-write 的调用先等待原 operation 完成，或使用有序 SqliteQuery。
- version/epoch 前置条件与相关写在同一受保护边界检查。不能在 Python 检查后排队并无条件写入。
- v1 的原子范围是一个资源/一个 SQLite 事务。跨数据库、跨 CRDT/数据库、跨外部 API 的“原子计划”明确不支持；不得用锁或 sequence 假装拥有跨资源回滚。
- 单业务步骤使用批量 snapshot/CAS/transaction；不把每个 dict.get 变成一个 PyO3 + 排队请求。

## 5. 完成、超时与取消

driver 返回后，执行 owner 先记录提交事实、发布必要投影/变化、保存可观察终态，最后释放 operation 容量。不等外部 Python finalize 回调来决定状态是否已经提交。

- `no_write` 表示没有持久写入；transient StateCas 的真实变化单独体现在 changes/revision 中。
- `unchanged` 表示持久操作没有造成已提交变化，不能通过成功 bool、非空数据或单独的 SQL 匹配行数代替证明。
- `committed` 只在提交事实确定后报告；部分阶段已提交、后续失败时仍保留该事实。
- `outcome_unknown` 不自动重试、不换新 operation_id；先查询原操作或原持久回执。
- wait_deadline 只结束观察，返回 pending/unknown 与 operation_id；不撤销操作，也不释放执行中的容量。
- Cancel 仅在能证明未开始、无副作用时返回 cancelled；执行中则返回 too_late/unsupported，或由驱动给出明确协作取消结果。不能随意 abort 一个正在提交的 SQLite/文件操作。

## 6. generation 重启与 daemon 重启

这两者必须区分；现有 EffectLane 的内存 reserve/ack 不能直接当作跨 daemon 的 durable 保证。

**执行者/handler generation 重启：** 接纳 owner 和未开始队列独立于 worker 保留；已开始但结果不明的操作先 reconcile，不盲目重跑。旧 generation 完成被 fence 处理且其责任得到结算，不能重复更新投影。未确认旧执行者已停止或写 fence 有效前，不启动第二个 writer。

这是待验收的应用要求，不是普通 Ractor actor 的默认保证。优先复用 Factory 等已有队列/worker 管理；Factory 也不能恢复未知的在途提交。许可须随操作责任结算，不能仅因 panic 丢弃消息对象就当作执行已结束。当前实验只覆盖正常完成时许可返还，故障接线仍 UNKNOWN。

**整个 daemon 重启：** volatile 接纳不承诺跨进程存活；旧 epoch 的观察返回明确 unknown-after-restart。只有原权威数据库中已经持久记录的 intent/receipt，才可报告 durable custody 并据此恢复。需要 durable acceptance 而驱动未实现时必须拒绝该能力，不能降级为内存接纳后仍宣称可靠保存。

`recover` 使用原资源的日志/回执，不建立第二份全局 durable ledger。恢复结果区分 completed、definitely-not-started、in-doubt。领域重建规则由 Python 生成恢复数据计划；核心不反向执行 Python callback。

## 7. Shutdown / drain

先关闭新工作接纳，继续运行已接纳的有序操作和完成处理，再关闭资源。停止接纳不等于丢弃队列，也不等于停止结果查询。

drain_deadline 到达但仍有工作时返回 `DrainReport { complete:false, queued, running, in_doubt }`，不能报告 stopped/PASS；不靠丢弃 future 或强杀 writer 伪造完成。只有操作责任已经结算或明确保存在原 durable 边界后，宿主才可以按其关闭协议释放 runtime。执行期间的容量一直由 owner 持有。

## 8. ChangeFeed 硬要求

- 订阅必须指定 resource_id 与 after_revision；只唤醒订阅相关资源的观察者。
- 普通回复、纯读、no-op、回滚不增加 revision、不发布变化。真实 transient/persistent 变化区分标记，不能把内存准入变化伪装成持久消息变化。
- 注册订阅与检查 revision 必须防止 lost wake；保持“先发生变化、后订阅旧版本”也能立即观察到新版本。
- 重放同一提交不能重复发新 revision。观察者落后于保留窗口时返回 resync-required/latest_revision，不默认为没有变化。
- ChangeFeed 不把阻塞等待放在资源写入席位中，也不创建一条 OS 线程服务每个订阅。

## 9. 首版组合与 callback

v1 对外只提供上述粗粒度操作和同资源批次。v0.1 的任意 sequence/parallel/retry 不是已经可执行的 DSL；普通 Rust 内部函数/Service 组合可以使用，但跨资源原子与业务条件语言不在 v1。

Rust driver：`open / execute / recover / close`。执行层统一处理提交、fault、取消和观测。Python 只做 `prepare / finalize`，或消费数据事件形成下一条外部输入，不把 Python callable 注册进核心。

handler context 只提供当前资源访问、时钟、取消观察和提交/计量记录，不提供向另一个 actor 的 submit/tell 端口。资源内部的辅助步骤是普通调用，不能偷偷重入顶层 CommandHandler。

首版不引入 Tower，handler 组合使用普通 Rust 函数/trait。Ractor 负责消息执行、生命周期与监督通知；应用集中配置必要策略和原有提交责任。普通 RPC timeout、stop/kill 或可重试 job 的默认行为，不能直接当作事务取消/重放协议。

## 10. 必须通过的 conformance cases

| 场景 | 必须观察到的结果 |
|---|---|
| StateCas/事务 no-op | 结果可正常返回，revision 固定，events 为空 |
| 真实事务变化 | 提交后推进资源 revision，仅相关订阅者被唤醒 |
| 队列/字节预算满 | 接纳前拒绝，无副作用，不无限排队 |
| 同 key、多 key | 同 key 有序且单执行；独立 key 可并行 |
| 重复 operation / 指纹冲突 | 前者加入原操作，无重复变化；后者拒绝 |
| 等待超时 | 操作责任仍在，可用同 ID 查结果 |
| 取消与提交竞争 | 不能把已经开始/提交的操作回报成未执行 |
| handler 重启 | 未开始责任保留；不明执行先 reconcile，旧代结果不重复生效 |
| daemon 重启 | 区分 volatile 与已持久 intent/receipt，不承诺内存消息存活 |
| drain 截止 | 未完成明确 incomplete，不能丢队列后报告成功 |
| 订阅竞争与落后 | 无 lost wake；过期 cursor 明确要求 resync |
| Python 边界 | core 内 Python 业务 callback 次数为 0；计入 prepare/finalize 的完整成本 |

这份接口足以开始限定原型，不表示六个 handler 家族已经覆盖全部当前消息。完整迁移必须先完成剩余消息级语义映射，见 [消息清单与覆盖边界](actor-rustify-message-catalog-2026-10-24.md)。
