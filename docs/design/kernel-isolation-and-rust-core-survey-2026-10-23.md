# Kernel 隔离现状与 Rust/PyO3 可行性调查

- 状态：设计调查，不改产品行为
- 基线：`d5a4dd257d15320ce59a9bb56d116e786ab4e195`
- 日期：2026-10-23
- 方向来源：Allen 要求探查 core 隔离，并评估用 PyO3 把 kernel 运行引擎迁到 Rust。

## 结论

当前 actor runtime 隔离了状态、顺序和失败代际，没有隔离进程资源。IPC 请求、actor、重试线程和后台同步线程仍在同一个 CPython 进程中。这些任务共享 GIL、CPU、堆、垃圾回收、文件描述符和领域锁。后台任务不直接占用 IPC 专用线程。后台任务仍能争用 GIL 和领域锁，也能阻塞 IPC 等待的领域 worker。

静态扫描找到 248 个执行能力构造点。其中有 61 个 `ActorRuntime` 构造点、48 个 `EffectLane` 构造点、95 个原始线程构造点、1 个 `ThreadingHTTPServer` 构造点和 2 个动态 `ThreadingMixIn` server 类构造点。这个数字是源码构造点，不是运行时线程数。线程检测同时按构造原语和继承链工作，并解析源码内的间接子类。每个 actor、每个动态 orgfs space 和每个 threaded server 请求都可增加线程。扫描也包含由 daemon 启动、但在子进程中运行的 worker 入口。因此，单个 daemon 的准确线程数为 **UNKNOWN**。本调查按要求没有连接生产 daemon。

<!-- generated:conclusion-retry:start -->
当前源码有 122 个已分类的重试、重连、重启、重放或竞争等待机制。机械扫描保留旧候选，并按操作重做语义识别 Method coverage 中的形态。扫描列出 625 个候选函数，其中 136 个满足 operation-repeat 形态。复核处置为 classified=158、limit-class=5、dismissed=462、undisposed=0。E2 是 METHOD-BOUNDED：inventory complete with respect to the detected shapes listed in Method coverage; shapes listed as blind spots are flagged, not enumerated exhaustively。纯状态读取即使循环轮询也属于 observation wait，不属于 retry。依赖内部重试总数为 **UNKNOWN**。122 个机制没有共享的总预算。固定 10 ms 且等待 ACK 的 `EffectLane` 重投尤其危险：每条 lane 有容量，但 daemon 没有统一的唤醒率、CPU 或日志预算。
<!-- generated:conclusion-retry:end -->

近期开工顺序如下：

1. 先引入统一重试引擎和结构门。
2. 再把本地读取放入独立执行 lane，并只读不可变快照。
3. 把 orgfs 同步、投影和 blob 恢复移到独立进程。
4. 最后用 Rust runtime 替换 actor 调度、mailbox、监督、重试和 timer。

Rust 能减少调度器本身的 GIL 占用，并提供可控的 worker pool。Rust 不能修复无限重试、无预算日志、同步 Python handler 或错误的优先级。第 1～3 步仍是 Rust 迁移的前置条件。

## A. 当前隔离

### A1. 执行模型

daemon 主线程运行本地 IPC accept loop。每个已接收连接创建一个 `hyprial-daemon-ipc-client` 线程。每个请求再创建一个 `hyprial-daemon-ipc-request` 线程。连接槽上限为 64，所以同一时刻最多有 64 个连接 worker 和 64 个请求 worker。请求线程直接调用同步业务入口；15 秒只是调用方等待上限，不会取消已经开始的业务操作。

`ActorRuntime` 为每个 actor 启动一个 Pykka `ThreadingActor`。每个 actor 一次只处理一个消息。不同 `ActorRuntime` 不共享 worker pool。`EffectLane` 另建固定数量的线程。`GenerationScheduler` 为每个到期项创建 `threading.Timer`。因此，actor 数、effect lane 数和未到期 timer 数都会增加线程数。

orgfs web server 通过动态 `type(..., ThreadingMixIn, ...)` 为每个连接创建线程。Casdoor webhook 使用 `ThreadingHTTPServer` 为每个请求创建线程。这些线程不受 IPC 的 64 个连接槽约束。

下表是执行 lane 的逻辑地图。附录 E1 列出 248 个静态构造点。

| lane | 创建者 | 执行内容 | 与 IPC 请求共享的资源 |
|---|---|---|---|
| 主 accept loop | `DaemonApplication.run` | 接受本地连接，处理 FD 耗尽退避 | GIL、进程 FD、日志 |
| IPC client | IPC server | 读 1 个请求并写 1 个响应 | GIL、连接槽、关闭锁 |
| IPC request | IPC server | 同步调用 router 和领域服务 | GIL、领域锁、领域 actor/effect worker、SQLite |
| Pykka actor | `ActorRuntime.start` | 单 actor FIFO handler | GIL、堆、同步 event sink、timer |
| effect worker | `EffectLane` 及领域副本 | 领域 I/O；完成事件重投至 owner | GIL、领域锁、完成 ACK、lane 容量 |
| timer callback | `GenerationScheduler` | actor 重启和周期调度 | GIL、scheduler 锁、被调用领域资源 |
| maintenance | daemon scheduler 和 `BoundedCadence` | restore wake、dispatch、forwarding、清理 | GIL；每个 cadence 有自己的 effect lane |
| durable retry pump | `DaemonEventBridge` | 到期 outbox 发送 | GIL、inbox authority、transport；不占 maintenance tick |
| orgfs worker | 每个 `OrgFsMesh` | sync、blob、投影修复 | GIL、每 space 单线程 pool、orgfs 锁和 transport |
| transport dispatcher | Zenoh transport | native callback、query、liveliness | GIL、transport session、callback queue |
| harness/adapters | daemon 监督的子进程及其读写线程 | 模型、DSH、Lark 和 Python worker | 主 daemon 只共享主机 CPU/内存/FD；不共享 GIL |
| async loop | streaming harness 和 Agent SDK worker | async provider/session I/O | daemon 内 streaming loop 共享 GIL；子进程 loop 不共享 GIL |
| 日志、hook、usage、PAC、lifecycle | 各领域 | 磁盘、网络、恢复和周期任务 | GIL；有些 lane 还共享 SQLite 或领域锁 |

静态扫描不能判断条件分支、动态实例数和进程边界。E1 保留每个构造点的词法 owner 和线程名，供开发阶段逐项确认。

### A2. 重试清单

<!-- generated:a2-intro:start -->
附录 E2 给出 122 个已分类机制，并按代码记录 schedule、cap、预算范围和日志行为。分类规则不看函数名：循环在 delay、wait 或 backoff 后重做操作，或在 failure、overload 后重做操作，就是 retry；循环若只读取状态，则是 observation wait。表中的 caller deadline 只限制等待；除非该行明确说明取消，否则 caller deadline 不撤销已接受的 custody。以下缺口直接解释当前故障模式：
<!-- generated:a2-intro:end -->

- `EffectLane` 默认每 10 ms 重投完成事件，直到 owner ACK 或 lane 关闭。重投没有 attempt cap，也没有 daemon 总预算。
- agent-home effect lane 复制了同一种固定间隔重投。
- durable inbox 有持久 backoff，但预算按 message 分配。daemon 没有跨 message 的总发送预算。completion 初始重投到 1 秒后仍会进入 unsettled custody，并按 50 ms worker idle cadence 无限重投。
- harness 重启按 harness 记账。配置值 `0` 可表示无限重试。
- org join 每 0.5 秒重试，到 caller deadline 为止；默认 deadline 是 30 秒。
- Zenoh degraded recovery 按 1/2/4/8/16/30 秒退避，之后每 30 秒重试，直到成功、关闭或其他 reopen 获胜。startup endpoint bind collision 还会立即换 loopback port 重开，默认使用 5 次 external-I/O restart budget；移除 best-effort derived listener 可再增加 1 次 fallback open。
- orgfs scheduled supplier sync 首次失败后等待 0.25 秒再试。每个 `run_once` 有 4.5 秒预算，并允许 1 次 coalesced rerun；最多占用单个 `orgfs-worker-*` 约 9 秒。
- orgfs content provision 在 IPC caller 上循环 `request_blob`、等待和最多 0.1 秒 sleep，直到 caller deadline；blob 工作仍使用同一个 orgfs worker。`await_content` 的文档路径还会调用 `refresh_space`。`refresh_space` 在同一 caller deadline 内依次对在线 holder 调用 `mesh.sync_from`；`StoreError` 或 `OrgFsError` 后继续下一个 holder。IPC `orgfs.read/export` 还会在 `await_content` 返回后最多 sleep 0.25 秒，再重跑完整读取；默认 deadline 为 10 秒，输入范围为 0～120 秒。
- state database connection init 以最多 0.05 秒间隔重试，2 秒到期。write-lock acquisition 每次可先等 SQLite busy handler 2 秒，再 sleep 0.05 秒；默认 8 秒 retry deadline，最迟约 10 秒返回。Lark 和 user store 的首次 WAL 切换也每 0.05 秒重试，但受各自 5 秒 busy budget 限制。
- orgfs 的 projection、supplier 和 blob 恢复各自维护状态。这 3 类恢复共享每个 space 的 worker 和进程资源。projection 每个 failure epoch 最多尝试 2 次；后续成功只给更早失败记录 1 个 reset epoch，因此单记录每个 daemon generation 最多失败应用 4 次。inline blob 路径从同一 supplier 取回缺失 digest 后，只立即重跑 1 次 import。work-item mutation 和 repair 遇到 stale write 时会立即重读并重写，每次 caller operation 最多 3 次。
- route native partial cleanup 在 `route_registration/io.py:676,761-768` 以最长 0.5 秒的指数退避无限重投。失败尝试占用共享的 8 个 native worker 之一。
- service-connect reconcile 在每轮结束 5 秒后重新调度。失败的 teardown 和 desired mapping 留到后续轮次重新执行 `_unmap` 或 `_map`。manager lifetime 内没有 attempt cap 或单轮 aggregate deadline；每次 `_unmap`/`_map` 内的 controller caller 最多等待 3 秒，timeout 不取消已接受工作。reconcile 在这些调用期间持有 `ServiceManager._lock`，而 `service.list/connect/disconnect` 也需要该锁。相同 mapping 和 aggregate failure 的日志按 fingerprint 去重，重复失败不逐次记录。
- lifecycle `_deliver` 在 `lifecycle_coordinator.py:758-766` 每 5 ms 重试 actor admission。重试可无限钉住当前 effect 或 recovery worker。
- route completion publish 在 `route_registration/io.py:925-955` 以 5/10/20 ms 重试，默认到 1 秒 deadline；每次尝试和 deadline 过期都不写日志。
- session route mutation 从 5 ms 指数退避到 0.5 秒。除 `PORT_CLOSING` 外没有上限，并逐次写日志。stale-route 清理在整轮都拿不到 per-agent route lock 时每 5 ms 重试，也没有 attempt 或 elapsed cap。
- Lark adapter process restart 按 gateway 的 lifecycle policy 退避和隔离；stale worker 退出前固定等待 1 秒。reaction 和 adapter effect completion 每 10 ms 重试 actor admission，5 秒到期，并钉住 native/effect completion callback。
- lifecycle process manager 的 domain admission 默认以 5/10/20 ms 重试到 1 秒，并钉住唯一 manager worker。forward redrive 默认受 70 秒 operation deadline 限制，但已接受 effect 不会因此取消；compensation 开始后没有独立 elapsed cap。
- IPC accept 遇到 `EMFILE` 或 `ENFILE` 时从 50 ms 指数退避到 1 秒，直到资源恢复或 daemon 停止。
- agent migration owner 每秒检查 actor generation。结果未返回时没有总 deadline。orgfs lifecycle、replica、space 和 space-state 在接受后也可无限等待；generation 变化只控制是否重新提交，不构成 elapsed cap。
- PAC 通知每次最多扫描 128 条 durable row，失败发送没有 attempt cap。PAC actor-effect 和通知 completion admission 每 10 ms 重试，可能分别钉住 actor-effect worker 或 delivery thread。remote workflow 每秒扫描最多 32 条 durable outbox row；completion admission 也以 10 ms 无限重试。
- provider-auth 用一个 due-time heap 合并重试，默认间隔 1 秒；只要 notice、decision、helper 或 restore 状态仍保留，就没有 attempt cap。code-observation callback 的内部 admission 每 10 ms 重试，也没有 cap。
- usage、identity binding cache 以及 MCP poll/heartbeat 各自维护长期恢复 cadence，没有 daemon 总预算。MCP wake 失败按 1/2/4/8/16/32/60 秒退避，accepted-but-unread resignal 按 30/60/120/240/300 秒退避，两者都没有 attempt cap。MCP stateless proxy 在断连后默认立即重连 1 次，并复用 mutation request ID。
- routine coordinator 在 effect 完成后每 10 ms 重试 `tell`，直到接受、关闭或 coordinator 关闭，没有 attempt 或 elapsed cap。interactive carrier command admission 每 2 ms 重试，默认 0.25 秒到期。Codex app-server startup 每 50 ms 重试 socket connect/initialize 到 startup deadline；carrier poll pass 失败后按 `poll_seconds` 无限重试并逐次记录错误。
- Codex interactive `receive_response` 的成功 nonterminal read 是每 0.5 秒一次的 observation poll。`CodexAppServerRpcError` 分支会在 0.5 秒后重试 `read_turn`，因此计入 retry family。两条路径都无 wall-clock cap，并在 connector process 的默认 asyncio executor 上执行读取。
- actor supervision 有 3 个 named policy：默认分别为 3 次/60 秒、5 次/60 秒、2 次/120 秒，并使用带 jitter 的 capped exponential delay。actor supervision 只控制 actor 重启，不控制 handler 内部的 feature retry。
- 日志策略不统一。有的机制逐次记录，有的只计数，有的仅记录终态，有的重试完全静默。E2 不再用 scanner 标签推断日志；每行都按实际调用点区分 attempt、aggregate 和 terminal 记录。

<!-- generated:a2-summary:start -->
E2 对全部 625 个机械候选给出处置，其中 136 个满足 operation-repeat 形态。处置计数为 classified=158、limit-class=5、dismissed=462、undisposed=0。E2 是 METHOD-BOUNDED：inventory complete with respect to the detected shapes listed in Method coverage; shapes listed as blind spots are flagged, not enumerated exhaustively。检测到的 periodic re-entry 等盲区实例按 `limit-class:<shape>` 和 `file:line` 记录，不作 dismissal。依赖内部循环仍为 **UNKNOWN**。
<!-- generated:a2-summary:end -->

### A2.1 Method coverage and limits（方法覆盖和限制）

E2 的本地 AST 扫描识别以下形态：

- 在 `sleep`、`wait` 或 backoff 后重复同一操作；
- failure、exception、overload 或 refusal 后，在 `while` 或有界 attempt 循环中重新提交操作；
- `for`、`async for` 或带下标的 `while` 把候选传入同一调用。候选可以是 holder、supplier、peer、address 或 ID。失败后，循环执行 `continue`，或更新汇总状态并进入下一候选。该规则覆盖有 aggregate deadline 和无 aggregate deadline 的 failover iteration；
- 只重复读取状态、不重做 effect 的 observation wait；这类候选会明确驳回；
- 旧 scanner 的名称和语法候选全集；该超集防止检测器升级时静默丢行。

failover 识别先宽选，再逐行判断循环元素是同一逻辑操作的替代项，还是彼此独立的工作项或搜索项。`RuntimeContent.refresh_space`、`RuntimeLifecycle._join_sync`、Codex loaded-thread discovery、DSH address connect 和 machine-ID source fallback 属于前者。对 chat、org、message、record 或 cleanup item 的独立处理属于后者。

本轮按 R2.2 重新检查了 disposition 表中的每个 dismissal。只有不重新尝试操作的词法命中、状态观察、独立项处理和一次性消费可以 dismissal。usage 周期刷新和 PAC 未投递通知改归已有 family。org leave、orgfs anti-entropy、PAC GC、link service 和 service-connect reconcile 的跨 pass 重做改为 limit class。

已知盲区包括 periodic re-entry、递归重入、callback 自行重新注册、外部 scheduler 再入、动态调用目标，以及第三方库内部 retry。发现盲区实例时，证据脚本以 `limit-class:<shape>` 和 `file:line` 记录，不作 dismissal。当前实例包括 `LinkService._loop` 对失败 `tick` 的周期再入、link store 保留失败 mirror 到下一轮 flush，以及 `ServiceManager._scheduled_reconcile` 对失败 teardown 和 desired mapping 的周期再入。静态 AST 不能穷尽这些形态，也不能单独证明两个语法不同的调用是否属于同一逻辑操作。因此，E2 是 METHOD-BOUNDED 的迁移基线和回归告警，不是最终完整性证明。B1 的结构门才是最终完整性控制：除明确的 poll、drain 和 lock adapter 外，每个 retry、restart、reconnect 和 failover 都必须通过 runtime `RetryPolicy` 执行。未走该 policy 的路径必须使 CI 失败。

### A3. 饥饿路径

风险按证据排序。排序依据是共享资源、是否无上限、是否在已知热路径，以及能否阻塞 IPC 所需的领域结果。

1. **orgfs 共用 worker 和 IPC content replay：高。** scheduled supplier sync、projection repair 和 blob recovery 共用每个 space 的单个 `orgfs-worker-*`。一次 supplier job 可因 coalesced rerun 占用约 9 秒。`await_content` 的文档路径会在 caller deadline 内串行尝试在线 holder；每个失败的 `mesh.sync_from` 都会推进到下一个 holder。`with_content_wait` 默认可在 IPC request thread 上等待 10 秒，并在 `await_content` 后重跑完整 read/export；上限可配置到 120 秒。后台恢复直接延迟同一内容结果。
2. **lifecycle custody 和 compensation redrive：高。** `_deliver` 每 5 ms 无限重试并钉住 effect 或 recovery worker。domain admission 最长 1 秒，但已接受 effect 不因 70 秒 forward deadline 取消；compensation 开始后没有独立 elapsed cap，并钉住唯一 process-manager worker。
3. **route cleanup、mutation 和 lock acquisition：高。** native partial cleanup 以最长 0.5 秒的退避无限重试，占用默认 8 个 native worker 之一。session mutation 同样无限重试并钉住 caller。stale-route 清理每 5 ms 争用 per-agent route lock，也没有 cap。三者都可延迟 IPC route ensure/drop。
4. **service-connect 周期 reconcile 的锁队列：高。** `_scheduled_reconcile` 在每轮结束 5 秒后重新调度，没有 attempt cap。`reconcile` 持有 `ServiceManager._lock` 执行全部 retained teardown 和 desired mapping；每次 `_unmap`/`_map` 内的 controller caller 最多等待 3 秒，timeout 不取消已接受工作，且单轮没有 aggregate deadline。IPC `service.list/connect/disconnect` 需要同一把锁，因此慢外部调用期间的持锁会直接阻塞这些 IPC 路径。
5. **effect 和 completion admission 重投：高。** 48 个 `EffectLane` 构造点默认每 10 ms 重投，直到 owner ACK。inbox 超过 1 秒的初始 completion settlement 后，还会按 50 ms worker idle cadence 无限重投并保留 recipient key。routine coordinator 的 `_effect_loop` 也每 10 ms 无限重试 `tell`。remote workflow、PAC、turn、Lark、session 和 provider-auth 另有 2～50 ms 的 completion/admission replay；多项没有 attempt 或 elapsed cap。这些 replay 分别钉住领域 worker、delivery thread 或 callback。
6. **SQLite 和文件锁竞争：高。** `state_db.py:122-141,236-256` 在调用线程重试。write-lock 默认最迟约 10 秒返回，接近 15 秒 IPC deadline。Lark/user WAL 转换各可等 5 秒；identity transaction lock 默认可等 15 秒；底层 Windows blocking file lock 没有 elapsed cap。
7. **IPC 与 threaded server 同步调用：高。** IPC request 有独立线程，却同步等待 actor、effect lane、SQLite 和领域锁。15 秒 timeout 只停止调用方等待，不取消已接受工作。agent migration、desired-state late join 和多个 orgfs authority 在接受后也没有总 deadline。orgfs web server 和 Casdoor webhook 还会按连接或请求创建线程，不受 IPC 的 64 个连接槽约束。
8. **PAC 与 remote durable replay：中高。** PAC delivery 每轮最多取 128 条，transient send/record failure 没有 attempt cap；actor-effect 和 delivery completion admission 每 10 ms 无限重试。remote workflow 每秒最多取 32 条，completion admission 同样无 cap。durable row 防止丢失，但不限制总唤醒、发送或 GIL 预算。
9. **provider-auth 和 MCP 长期恢复：中高。** provider-auth 默认每秒从 coalesced heap 重试，只要状态仍保留就没有 attempt cap；code-observation admission 每 10 ms 重试。MCP poll/heartbeat 和 wake 都可持续到 channel 或 durable item 结束；wake 最终以 60 秒或 300 秒固定间隔继续。
10. **maintenance 和 durable inbox pump：中高。** inbox pump 与 maintenance tick 已分离，但两者仍共享进程、GIL 和领域 authority。durable inbox 的 attempt/expiry 按 message 管理，没有跨 message 的总发送预算。
11. **route completion publication：中。** publication 以 5/10/20 ms 后固定 20 ms 重试，默认 1 秒到期。并发 token 共享 route owner，且 attempt 和 deadline expiry 都不记录日志。
12. **actor handler 和 generation replay：中。** mailbox 保证单 actor FIFO，不提供时间片。CPU 密集或阻塞 handler 可长期占用 actor 和 GIL。session generation reconciliation、undelivered replay 和 7 项 generation reassociation 虽有 fence，但 orgfs lifecycle、space-state、replica、space 的接受后等待没有 deadline。
13. **transport callback 和 reconnect：中。** callback 有独立 dispatcher。Codex turn 的 nonterminal 状态读取是 observation wait；RPC error 分支则每 0.5 秒重试 `read_turn`。两者都在 connector process 的默认 asyncio thread executor 上运行。Codex carrier poll-pass fault 按 `poll_seconds` 无限重试。degraded recovery 按 1/2/4/8/16/30 秒后固定 30 秒继续；liveliness reconciliation 每次最多 5 个 1 秒 read。Zenoh SDK 内部线程和重试数仍为 **UNKNOWN**。
14. **子进程 harness、adapter 和 service 管理：低到中。** 子进程不共享 daemon GIL，但共享主机 CPU、内存和 I/O。launchd/systemd load retry 有边界；子进程总量没有 OS cgroup 或进程级 CPU 配额时仍可挤压 daemon。

这份排序不是性能实测。生产 daemon 未被读取。准确 CPU 占比、GIL 等待和 p99 延迟为 **UNKNOWN**。开发前必须用隔离 daemon 重放 orgfs storm，并记录每个 runtime owner 的 CPU counter、IPC 延迟和 queue depth。

### A4. Actor runtime 的保证边界

当前 runtime 保证：

- 每个 actor 有稳定 handle、单 mailbox 和单消息执行。
- mailbox admission 有容量，返回 `accepted`、`overloaded` 或 `closed`。
- actor 内部按 Pykka mailbox 顺序处理消息。
- 未开始的消息由 guardian 暂存 custody token；重启、隔离或停止时交给 `undelivered_sink`。
- supervision policy 有窗口、最大重启数、指数 backoff 和 jitter。
- generation fence 丢弃旧 timer 和旧 completion。
- 超出重启预算后，actor 进入 quarantine。
- `EffectLane` 在 owner ACK 前保留容量和 completion custody。

当前 runtime 不保证：

- 不保证 actor 之间公平，也没有优先级。
- 不限制 handler 的 CPU、wall time、内存或 GIL 占用。
- 不抢占阻塞 handler。
- 不提供 daemon 级重试预算、唤醒预算或日志预算。
- 不把本地读取置于后台任务之前。
- 不隔离进程资源，也不使用 cgroup、rlimit 或 CPU affinity。
- mailbox 有界不等于进程有界。领域 queue、future、timer 和子进程可另行增长。
- caller timeout 不会取消已开始的 actor 或领域 effect。

## B. Python 内的近期目标

### B1. 统一重试引擎

新增 kernel `RetryEngine`。业务模块只提交 `RetryOperation`，不得自己 sleep、建 timer 或修改 attempt。

`RetryPolicy` 至少包含以下字段：

- `policy_id` 和 `failure_class`；
- `initial_delay`、`multiplier`、`max_delay` 和 `jitter_ratio`；
- `max_attempts` 和 `max_elapsed`；
- `budget_key`、`max_concurrent` 和 `wake_rate`；
- `durability`、`idempotency_key` 和 `deadline_behavior`；
- `log_first`、`log_terminal`、`log_every_power_of_two` 和 `aggregate_interval`。

预算分 3 层：operation、lane 和 daemon。operation 控制单任务 cap。lane 防止一个功能占满 worker。daemon token bucket 限制所有 background retry 的唤醒和并发。control-plane retry 使用独立预算，不与 background 共用 token。

日志只记录首个失败、状态变化、2 的幂次 attempt 和终态。每 60 秒按 `policy_id + failure_class` 汇总 suppressed attempts、queue depth、oldest age 和 consumed budget。涉及 custody 的机制仍必须保留 durable attempt 事实。

迁移时先复制现有 schedule 和 cap。统一引擎不能借重构改变消息过期、幂等或重启语义。现有无限机制必须显式使用 `max_attempts=None`，并同时设置 `max_elapsed` 或 durable expiry。两者都为空时，结构门失败。

结构门解析 kernel、identity、daemon 和 biz 的 AST。以下行为只允许出现在 retry engine、poll、drain 和 lock adapter 白名单中：

- exception/failure 分支中的 `sleep` 或 `wait`；
- 用 `threading.Timer` 安排同一 operation；
- feature 层直接计算指数 backoff；
- retry、reconnect 或 restart 函数内直接循环；
- attempt 变量递增后再次调用同一 effect。

附录 E2 是初始迁移清单。结构门必须让每个机械候选带分类。新增未分类候选时，CI 失败。候选扫描仍可能漏掉动态或跨 callback 的重入。因此，B1 还必须检查所有允许重做 work 的调用点。每个 retry、restart、reconnect 和 failover 都必须使用 runtime `RetryPolicy`。这是最终完整性控制，不以 E2 候选生成器的覆盖率为前提。

### B2. 本地读取优先级下限

本地读取必须满足 4 条约束：

1. IPC 在解析 method 后，把只读请求送入专用 `ipc-read` lane。该 lane 至少保留 2 个 worker，不执行写请求。
2. read handler 只读取不可变 snapshot。read handler 不得访问网络、磁盘、SQLite、actor callback 或后台 worker。
3. 领域 owner 在状态变更后原子发布 snapshot。发布失败不阻塞已有 snapshot。
4. background lane 不能借用 `ipc-read` worker、queue、lock 或 retry token。

专用 Python 线程仍共享 GIL。因此，只拆线程不能形成严格优先级。CPU 密集 lane 必须移到子进程，或在短时间片后释放 GIL。验收使用隔离 daemon 注入 background storm：1000 次 `ping` 和 snapshot read 必须全部在现有 15 秒 deadline 内完成；p99 目标为 250 ms。CI 主机规格未固定，所以 250 ms 暂为提议值，不是现有产品承诺。

### B3. 进程隔离

首批迁移 3 类 heavy lane：

1. `orgfs-worker-*`：sync、CRDT apply、projection、blob 校验和恢复。
2. PAC/Git 工作：worktree 扫描、Git 命令编排和大结果解析。
3. usage、hook recap 和可批处理日志压缩：允许延后，不得影响控制面。

orgfs 子进程通过本机 Unix socket 或 Windows named pipe 接收 typed command。命令带 operation id、deadline、generation 和 idempotency key。响应带 version、effect receipt 和 snapshot delta。主进程保留 IPC 路由和全局 lifecycle。orgfs 子进程独占 orgfs SQLite、CRDT 和 projection 状态，避免两个进程同时成为 authority。

进程协议必须支持：bounded request queue、health snapshot、graceful drain、crash restart、generation fence 和 replay。大 blob 通过受限文件描述符或内容寻址文件交换，不复制到 JSON。子进程不得读取 daemon 凭据以外的用户状态。

## C. Rust/PyO3 kernel

### C1. 公共边界

`kernel/interface.py` 当前导出 209 个名称。15 个导出含 Python callback。9 个导出没有 upper-layer 的显式 import consumer。callback 判定覆盖直接 `Callable`、带 `__call__` 或泛型 callback port 的 `Protocol`，以及 signature 中递归携带这些类型的对象。`CommandSink` 和 `EventSink` 均为 callback port；普通 service protocol 不自动算 callback。consumer 是 identity、daemon、biz 和 shell 中显式 import 该名称的文件数，不是调用次数。

高扇出项是 `ipc_errors` 177 个 consumer、`AdmissionResult` 91 个、`ActorRuntime` 64 个、`ActorSpec` 60 个、`EffectCompleted` 47 个、`EffectLane` 46 个和 `EffectRequest` 44 个。完整 209 项清单见 E3。每行给出定义、跨边界 signature/type、callback 和 consumer 数。

209 个导出不宜一次迁入 Rust。URI、配置、平台文件权限、日志 payload 和产品常量仍留在 Python。第一个 Rust 边界只包含：

- actor id、generation、admission 和 snapshot 值对象；
- bounded mailbox；
- per-actor FIFO 和 one-in-flight 状态机；
- supervision window、quarantine 和 generation fence；
- retry budget、timer wheel 和 aggregated counters；
- effect completion custody 和 ACK 状态机。

Python 保留领域 handler、SQLite authority、network adapter、CLI、配置和业务类型。现有 `hyprial.kernel` Python façade 保持 import 路径不变。Façade 在边界处把 Python dataclass 转成 PyO3 class 或 msgpack-like primitive。不得把任意 Python closure 存进 Rust durable state。

### C2. Python actor 在 Rust scheduler 上运行

Rust runtime 使用固定 worker pool，不再为每个 actor 创建 1 个 OS 线程。每个 actor mailbox 是有界 MPSC queue。调度器只把“未运行且非空”的 actor 放入 ready queue。同一 actor 同时最多有 1 个 handler task，所以 FIFO 和 one-in-flight 不变。

执行 Python handler 时，Rust worker 获取 GIL，构造 command Python object，调用 handler，然后立即释放 GIL。mailbox admission、timer、预算、generation 检查、counter 和 Rust-only 状态在无 GIL 区域运行。Python callback 包括 handler、event sink、undelivered sink、effect execute 和 effect complete。

普通 CPython 3.12 下，同一时间仍只有 1 个 Python bytecode handler 持有 GIL。Rust 只能让调度、timer 和 Rust I/O 不占 GIL。CPU 密集 Python handler 仍会延迟 IPC Python 代码。CPU 密集 handler 必须迁到 Rust、释放 GIL 的 native call 或独立进程。

free-threaded CPython 需要单独评估。现有依赖和 handler 的线程安全没有证据。PyO3 扩展不得先声明 `gil_used = false`。只有依赖矩阵、actor handler 和 callback 都通过 free-threaded 压测后，才可发布 free-threaded wheel。时间和支持版本为 **UNKNOWN**。

### C3. 可独立发布的迁移步骤

| 步骤 | 交付 | 行为不变验收 | 发布和回滚 |
|---|---|---|---|
| R0 | 固化 E1～E3 扫描、retry 结构门和 actor trace schema | 当前 actor、effect、timer 测试；结构门；`contract/run-all.sh --isolated-only` | 纯 Python；无运行时切换 |
| R1 | 新建纯 Rust policy crate：backoff、budget、generation 和 admission 状态机 | Python 与 Rust 对同一生成 trace 逐事件相等；边界值和 property tests | crate 不进生产路径；无回滚成本 |
| R2 | PyO3 shadow runtime 读取真实 command trace，但 Pykka 仍是 authority | shadow snapshot、event 和决策逐项相等；任何差异使测试失败 | feature flag 只在隔离测试启用 |
<!-- generated:r3:start -->
| R3 | Rust timer 和 `RetryEngine` 成为 authority，actor 仍由 Pykka 执行 | 122 个 retry fixture 的 schedule/cap/durable outcome 相等；625 个候选处置保持 classified=158、limit-class=5、dismissed=462、undisposed=0；storm 下本地读取达到 B2 | 显式回退到 Python timer；不自动双写 |
<!-- generated:r3:end -->
| R4 | Rust mailbox、supervision 和 fixed worker pool 替换 `ActorRuntime` | `tests/kernel/actors`、领域 actor 测试、完整 `tests/` 和 isolated contract 全通过；crash/restart trace 相等 | 一个版本保留显式 Python backend |
| R5 | Rust effect custody 替换 `EffectLane` 和领域副本 | ACK、close、late completion、restart 和 overload fault injection 全通过 | generation fence 阻止双 backend completion |
| R6 | orgfs heavy lane 移入独立进程；Rust runtime 管理协议和预算 | orgfs 合同、断电恢复、重复 command、storm 和 p99 gate | 协议有版本；旧主进程不接新协议 |
| R7 | 默认 Rust backend，删除 Pykka runtime | 全量 Python、GUI 依赖 fixture、isolated contract、wheel smoke 和升级/降级测试 | 删除前至少 1 个发布周期保留显式回退 |

每一步都必须在隔离 home 中测试，不得连接生产 daemon。涉及行为变更时同步 `contract/`。R4、R5 和 R7 必须跑完整 Python 测试；设计阶段不运行完整套件。

### C4. 构建和打包

当前 Python wheel 使用 Hatchling，并有 GUI build hook。直接把根 build backend 改成 Maturin 会同时改变 GUI 打包，风险过大。首选方案是新增独立的 `hyprial-kernel` wheel，用 Maturin 构建 `hyprial._kernel`。主 wheel 在 `uv.lock` 中精确锁定同版本 kernel wheel。Python façade 继续位于主 wheel。

初始扩展以 CPython 3.12 为基线。是否使用 `abi3-py312` 取决于 PyO3 API 和 free-threaded 计划。macOS 和 Linux 的准确 architecture matrix 必须从发布 CI 得出；本调查未找到已批准矩阵，所以标为 **UNKNOWN**。发布 gate 至少包含：

- 每个支持平台从 wheel 安装，不调用本机 Rust 编译器；
- wheel 中扩展的 import、签名和版本匹配；
- `uv sync --extra test` 可解析锁文件；
- sdist 构建明确要求 Rust toolchain，不得静默退回 Python backend；
- 主 wheel 与 kernel wheel 版本不匹配时启动失败；
- macOS 和 Linux 上运行 actor trace、crash 和 IPC storm smoke。

### C5. Rust 不会修复的事项

Rust 不会自动修复以下问题：

- 无 cap 或无 expiry 的 retry policy；
- 每个 feature 自己记录 attempt，导致总预算缺失；
- Python handler 内的 CPU 密集循环；
- 同步领域锁和同步 SQLite 等待；
- 请求 timeout 后操作继续执行；
- 子进程没有 CPU 和内存配额；
- 每次失败都写日志造成 I/O storm；
- orgfs authority 和 IPC snapshot 边界不清。

因此，B1 的 policy、B2 的 read snapshot 和 B3 的进程边界不能等 Rust 完成后再补。

## D. 可测门槛、决策与待确认项

以下数值是首轮验收门槛，不是现有 SLA。固定 CI 主机确定后，基线数据可触发一次阈值修订。修订必须留证据，不能在失败后临时放宽。

| gate | workload | pass 条件 |
|---|---|---|
<!-- generated:g1:start -->
| G1 重试清单 | 对 625 个机械候选运行分类门 | classified=158、limit-class=5、dismissed=462、undisposed=0；122 个已知 family 都绑定 `policy_id`；发现的盲区实例均有 `limit-class:<shape>` 和 `file:line`；非 durable policy 同时缺少 attempt cap 和 elapsed cap 时失败 |
<!-- generated:g1:end -->
| G2 本地读取 | orgfs supplier 断开、100 个待恢复 digest、所有 background retry 开启时执行 1000 次 `ping` 和 snapshot read | 0 次超过 15 秒；p99 ≤ 250 ms；read queue depth 在测试结束后归零 |
| G3 重试预算 | 1000 个同时失败的 background operation，持续 10 分钟 | daemon background 唤醒率 ≤ 100/s；并发 effect ≤ 配置 cap；聚合日志 ≤ 2 条/`policy_id + failure_class`/分钟 |
| G4 actor 等价 | 对 Python 和 Rust backend 各重放至少 10000 条生成 trace，并注入 handler fault、late completion、timer race 和 close | admission、custody、generation、restart、quarantine 和最终 snapshot 的差异为 0 |
| G5 恢复稳定性 | orgfs 依赖离线 1 小时后恢复 | RSS 和 queue depth 不持续单调增长；依赖恢复后 5 分钟内清空到期队列；无 custody 丢失 |
| G6 进程故障 | 在每个 protocol 阶段 kill orgfs worker 100 次 | 主 daemon 保持 IPC read 可用；重复 command 不产生重复 effect；generation fence 拒绝旧响应 |
| G7 发布 | 每个批准的平台从 wheel 建立全新环境 | 不使用本机 Rust 编译器；import、actor trace、crash、IPC storm 和版本不匹配测试全部通过 |

G3 的 100/s 和 G2 的 250 ms 是建议初值。首个隔离基线若证明不合理，阈值修订须记录主机、数据和原因。G5 的“持续单调增长”须转成采样断言：稳定阶段连续 10 个一分钟窗口都增长时失败。

本调查建议批准 R0、B1 和隔离 storm fixture。R1 可与 B1 并行。R2 以前不得让 Rust runtime 成为 authority。

待确认项：

- 发布 CI 支持的 macOS/Linux architecture matrix；
- `hyprial-kernel` 独立 wheel 是否符合发布仓库策略；
- IPC read p99 的固定 CI 主机和阈值；
- orgfs 子进程是一 daemon 一个，还是一 space 一个；
- Python backend 保留几个发布周期；
- free-threaded CPython 的目标版本。

## E. 可复现证据

以下输出由提交内脚本生成。命令不连接 daemon，也不读取用户 home。输出中的 source baseline 指产品源码快照，不是调查文档提交；脚本会在 `src/` 偏离该快照时拒绝生成证据。

```bash
uv run --no-project --python 3.12 python scripts/verify/kernel_isolation_survey_selftest.py
uv run --no-project --python 3.12 python scripts/verify/kernel_isolation_survey.py execution
uv run --no-project --python 3.12 python scripts/verify/kernel_isolation_survey.py retries
uv run --no-project --python 3.12 python scripts/verify/kernel_isolation_survey.py retries --all-candidates > /tmp/kernel-retry-candidates.md
uv run --no-project --python 3.12 python scripts/verify/kernel_isolation_survey.py kernel
uv run --no-project --python 3.12 python scripts/verify/update_kernel_isolation_survey_evidence.py
uv run --no-project --python 3.12 python scripts/verify/update_kernel_isolation_survey_evidence.py --check
```


### E1. Execution-site scanner output

## Execution-site evidence

Source baseline: `d5a4dd257d15320ce59a9bb56d116e786ab4e195`. Execution construction sites: **248**; synchronization construction sites: **347**.

Execution counts: actor-runtime=61, async-task=11, default-executor-work=7, effect-worker-pool=48, event-loop=2, inherited-thread-spawner=2, per-request-thread-server=1, pykka-actor-thread=1, thread=95, thread-pool=7, timer-scheduler=9, timer-thread=4.

| file:line | primitive | lexical owner | configured name/prefix |
|---|---|---|---|
| `src/hyprial/biz/impl/hooks/recap/writer.py:84` | thread | `RecapWriter.enqueue` | `'hyprial-recap-writer'` |
| `src/hyprial/biz/impl/routine/execution/coordinator/__init__.py:91` | actor-runtime | `RoutineCoordinator.__init__` | `-` |
| `src/hyprial/biz/impl/routine/execution/coordinator/__init__.py:106` | thread | `RoutineCoordinator.__init__` | `f'routine-coordinator-effect-{index}'` |
| `src/hyprial/biz/impl/routine/execution/coordinator/__init__.py:115` | thread | `RoutineCoordinator.__init__` | `'routine-coordinator-recover'` |
| `src/hyprial/biz/impl/routine/execution/service.py:181` | actor-runtime | `RoutineFacade.__init__` | `-` |
| `src/hyprial/biz/impl/routine/execution/service.py:206` | thread | `RoutineFacade.__init__` | `'hyprial-routine-effects'` |
| `src/hyprial/biz/impl/routine/execution/service.py:212` | thread | `RoutineFacade.__init__` | `f'hyprial-routine-effect-{index}'` |
| `src/hyprial/biz/impl/usage.py:670` | thread | `UsageCache.start` | `'hyprial-usage-refresh'` |
| `src/hyprial/biz/impl/usage_actor.py:47` | actor-runtime | `UsageAuthority.__init__` | `-` |
| `src/hyprial/biz/impl/usage_actor.py:48` | timer-scheduler | `UsageAuthority.__init__` | `-` |
| `src/hyprial/biz/impl/usage_actor.py:56` | effect-worker-pool | `UsageAuthority.__init__` | `'usage-collectors'` |
| `src/hyprial/daemon/impl/adapters/lark/inbound/inbound_runtime.py:149` | thread-pool | `_InboundEffects.__init__` | `'hyprial-lark-inbound-effect'` |
| `src/hyprial/daemon/impl/adapters/lark/inbound/inbound_runtime.py:309` | actor-runtime | `LarkInboundRuntime.__init__` | `-` |
| `src/hyprial/daemon/impl/adapters/lark/outbound/gateway.py:84` | actor-runtime | `GatewayIoAuthority.__init__` | `-` |
| `src/hyprial/daemon/impl/adapters/lark/outbound/gateway.py:207` | thread | `GatewayIoAuthority.close` | `'lark-gateway-close'` |
| `src/hyprial/daemon/impl/adapters/lark/outbound/reactions.py:537` | thread-pool | `_NativeLane.__init__` | `f'hyprial-lark-reaction-{kind.value}'` |
| `src/hyprial/daemon/impl/adapters/lark/outbound/reactions.py:692` | actor-runtime | `ReactionEffectsRuntime.__init__` | `-` |
| `src/hyprial/daemon/impl/adapters/lark/runtime/effects.py:55` | thread-pool | `_ProcessEffects.__init__` | `'hyprial-lark-effect'` |
| `src/hyprial/daemon/impl/adapters/lark/runtime/effects.py:151` | thread | `_ProcessEffects.submit` | `'hyprial-lark-effect-shutdown'` |
| `src/hyprial/daemon/impl/adapters/lark/runtime/effects.py:476` | thread | `_ProcessEffects._run` | `-` |
| `src/hyprial/daemon/impl/adapters/lark/runtime/health.py:134` | thread | `_HealthReportDispatcher._ensure_thread` | `f'lark-health-report-{self._name}'` |
| `src/hyprial/daemon/impl/adapters/lark/runtime/health.py:474` | thread | `LarkStreamHealthMonitor._bounded_call` | `f'lark-{label}-probe-{self.name}'` |
| `src/hyprial/daemon/impl/adapters/lark/runtime/runtime.py:97` | thread | `_PortEventDispatcher.__init__` | `'hyprial-lark-port-events'` |
| `src/hyprial/daemon/impl/adapters/lark/runtime/runtime.py:159` | actor-runtime | `AdapterRuntime.__init__` | `-` |
| `src/hyprial/daemon/impl/adapters/lark/runtime/runtime.py:533` | thread | `AdapterRuntime._submit_reload_port` | `'hyprial-lark-config-reload'` |
| `src/hyprial/daemon/impl/adapters/lark/worker/control.py:37` | actor-runtime | `ControlIoOwner.__init__` | `-` |
| `src/hyprial/daemon/impl/adapters/lark/worker/entry.py:389` | thread | `main` | `f'lark-reply-bridge-{name}'` |
| `src/hyprial/daemon/impl/adapters/lark/worker/entry.py:501` | thread | `main` | `f'lark-health-{name}'` |
| `src/hyprial/daemon/impl/adapters/lark/worker/process.py:153` | thread | `LarkWorkerProcess.__init__` | `f'lark-ready-{name}'` |
| `src/hyprial/daemon/impl/adapters/lark/worker/process.py:564` | thread | `_StdoutDrain.__init__` | `'lark-worker-output'` |
| `src/hyprial/daemon/impl/adapters/lark/worker/sweep.py:91` | thread | `ReconnectSweepCoordinator.request` | `f'lark-reconcile-{self.name}'` |
| `src/hyprial/daemon/impl/adapters/lark/worker/sweep.py:168` | thread | `ReconnectSweepCoordinator._call_once` | `f'lark-reconcile-call-{self.name}'` |
| `src/hyprial/daemon/impl/application/__init__.py:579` | timer-scheduler | `DaemonApplication.__init__` | `-` |
| `src/hyprial/daemon/impl/application/messaging/operator_alarm.py:63` | effect-worker-pool | `OperatorAlarmDelivery.__init__` | `'operator-alarm-delivery'` |
| `src/hyprial/daemon/impl/application/messaging/status/refresh.py:108` | thread | `CoalescedRefreshAuthority._ensure_workers_locked` | `f'status-refresh-{index}'` |
| `src/hyprial/daemon/impl/application/netendpoints/services.py:20` | thread | `_ServiceScheduler.__init__` | `'service-connect-scheduler'` |
| `src/hyprial/daemon/impl/application/restore.py:739` | thread | `_RestoreMixin._start_restore_thread` | `'hyprial-daemon-restore'` |
| `src/hyprial/daemon/impl/application/shutdown.py:311` | thread | `_ShutdownMixin._arm_exit_backstop` | `'daemon-exit-backstop'` |
| `src/hyprial/daemon/impl/autoupdate/__init__.py:133` | thread | `InProcessAutoUpdateScheduler.start` | `'hyprial-autoupdate'` |
| `src/hyprial/daemon/impl/autoupdate/actor.py:83` | actor-runtime | `AutoUpdateAuthority.__init__` | `-` |
| `src/hyprial/daemon/impl/autoupdate/actor.py:84` | timer-scheduler | `AutoUpdateAuthority.__init__` | `-` |
| `src/hyprial/daemon/impl/autoupdate/actor.py:95` | effect-worker-pool | `AutoUpdateAuthority.__init__` | `'hyprial-autoupdate'` |
| `src/hyprial/daemon/impl/configuration/home_guard.py:235` | thread | `ActiveDaemonHeartbeat.claim` | `'hyprial-home-heartbeat'` |
| `src/hyprial/daemon/impl/configuration/home_guard.py:242` | thread | `ActiveDaemonHeartbeat.claim` | `'hyprial-home-duplicate-check'` |
| `src/hyprial/daemon/impl/configuration/home_lease_actor.py:115` | actor-runtime | `HomeLeaseAuthority.__init__` | `-` |
| `src/hyprial/daemon/impl/configuration/home_lease_actor.py:116` | timer-scheduler | `HomeLeaseAuthority.__init__` | `-` |
| `src/hyprial/daemon/impl/configuration/home_lease_actor.py:126` | effect-worker-pool | `HomeLeaseAuthority.__init__` | `'home-lease-file'` |
| `src/hyprial/daemon/impl/configuration/home_lease_actor.py:132` | effect-worker-pool | `HomeLeaseAuthority.__init__` | `'home-lease-duplicate'` |
| `src/hyprial/daemon/impl/context_snapshot/service.py:203` | thread-pool | `ContextSnapshotWorker.__init__` | `'hyprial-context-snapshot-io'` |
| `src/hyprial/daemon/impl/correlation/bounded_cadence.py:61` | actor-runtime | `BoundedCadence.__init__` | `-` |
| `src/hyprial/daemon/impl/correlation/bounded_cadence.py:81` | effect-worker-pool | `BoundedCadence.__init__` | `f'{name}-effect'` |
| `src/hyprial/daemon/impl/desired_state_io.py:116` | effect-worker-pool | `DesiredStateIoPort.__init__` | `'desired-state-io'` |
| `src/hyprial/daemon/impl/dispatch/remote/__init__.py:65` | actor-runtime | `RemoteWorkflow.__init__` | `-` |
| `src/hyprial/daemon/impl/dispatch/remote/__init__.py:95` | thread | `RemoteWorkflow.__init__` | `f'pac-remote-effect-{index}'` |
| `src/hyprial/daemon/impl/dispatch/remote/__init__.py:104` | thread | `RemoteWorkflow.__init__` | `'pac-remote-outcomes'` |
| `src/hyprial/daemon/impl/dispatch/remote/wire.py:198` | thread | `RemoteWire.receive` | `'pac-remote-rpc'` |
| `src/hyprial/daemon/impl/dispatch/runtime/diagnostics.py:43` | actor-runtime | `DispatchDiagnostics.__init__` | `-` |
| `src/hyprial/daemon/impl/dispatch/runtime/holds.py:43` | actor-runtime | `HoldRefreshCoordinator.__init__` | `-` |
| `src/hyprial/daemon/impl/dispatch/runtime/holds.py:56` | effect-worker-pool | `HoldRefreshCoordinator.__init__` | `'inbox-hold-refresh-io'` |
| `src/hyprial/daemon/impl/dispatch/runtime/offers.py:75` | actor-runtime | `DispatchOfferCoordinator.__init__` | `-` |
| `src/hyprial/daemon/impl/dispatch/runtime/offers.py:91` | effect-worker-pool | `DispatchOfferCoordinator.__init__` | `'dispatch-offer-io'` |
| `src/hyprial/daemon/impl/dispatch/runtime/progress.py:50` | actor-runtime | `ProgressPublishCoordinator.__init__` | `-` |
| `src/hyprial/daemon/impl/dispatch/runtime/progress.py:65` | effect-worker-pool | `ProgressPublishCoordinator.__init__` | `'dispatch-progress-io'` |
| `src/hyprial/daemon/impl/dispatch/runtime/state.py:72` | actor-runtime | `DispatchStateAuthority.__init__` | `-` |
| `src/hyprial/daemon/impl/forwarding/authority.py:101` | actor-runtime | `ForwardingControllerAuthority.__init__` | `-` |
| `src/hyprial/daemon/impl/forwarding/controller.py:140` | thread | `ForwardingSidecarController.__init__` | `'forward-sidecar-stdout'` |
| `src/hyprial/daemon/impl/forwarding/controller.py:143` | thread | `ForwardingSidecarController.__init__` | `'forward-sidecar-stderr'` |
| `src/hyprial/daemon/impl/forwarding/controller.py:154` | thread | `ForwardingSidecarController.__init__` | `'forward-sidecar-monitor'` |
| `src/hyprial/daemon/impl/forwarding/supervisor.py:96` | actor-runtime | `ForwardingSupervisorState.__init__` | `-` |
| `src/hyprial/daemon/impl/forwarding/supervisor.py:300` | timer-scheduler | `ForwardingSidecarSupervisor.__init__` | `-` |
| `src/hyprial/daemon/impl/harnesses/actor/actor.py:141` | actor-runtime | `HarnessRuntimeActor.__init__` | `-` |
| `src/hyprial/daemon/impl/harnesses/actor/behaviors/delivery.py:258` | effect-worker-pool | `_DeliveryBehavior._on_claim_results` | `'harness-result-claim-io'` |
| `src/hyprial/daemon/impl/harnesses/actor/behaviors/desired.py:571` | timer-thread | `_DesiredBehavior._continue_lifecycle_rejection` | `-` |
| `src/hyprial/daemon/impl/harnesses/actor/behaviors/process_events.py:98` | timer-thread | `_ProcessEventsBehavior._begin_start` | `-` |
| `src/hyprial/daemon/impl/harnesses/actor/io_port.py:80` | thread-pool | `ProcessIoPort.__init__` | `'hyprial-harness-io'` |
| `src/hyprial/daemon/impl/harnesses/actor/io_port.py:334` | thread | `ProcessIoPort._submit.schedule_settlement` | `'hyprial-harness-completion'` |
| `src/hyprial/daemon/impl/harnesses/agent_sdk/__init__.py:495` | async-task | `IsolatedAgentSdkClient.__aenter__` | `-` |
| `src/hyprial/daemon/impl/harnesses/agent_sdk/__init__.py:496` | async-task | `IsolatedAgentSdkClient.__aenter__` | `-` |
| `src/hyprial/daemon/impl/harnesses/agent_sdk/_agent_sdk_worker.py:507` | thread | `_serve` | `'agent-sdk-command-reader'` |
| `src/hyprial/daemon/impl/harnesses/agent_sdk/_agent_sdk_worker.py:571` | async-task | `_serve` | `-` |
| `src/hyprial/daemon/impl/harnesses/agent_sdk/_agent_sdk_worker.py:573` | async-task | `_serve` | `-` |
| `src/hyprial/daemon/impl/harnesses/agent_sdk/_agent_sdk_worker.py:616` | event-loop | `main` | `-` |
| `src/hyprial/daemon/impl/harnesses/carrier/interactive_carrier_runtime.py:498` | actor-runtime | `InteractiveCarrierRuntime.__init__` | `-` |
| `src/hyprial/daemon/impl/harnesses/codex/carrier.py:126` | default-executor-work | `CodexInteractiveTurnClient.query` | `-` |
| `src/hyprial/daemon/impl/harnesses/codex/carrier.py:146` | default-executor-work | `CodexInteractiveTurnClient.receive_response` | `-` |
| `src/hyprial/daemon/impl/harnesses/codex/carrier.py:180` | default-executor-work | `CodexInteractiveTurnClient.interrupt` | `-` |
| `src/hyprial/daemon/impl/harnesses/codex/carrier.py:261` | thread | `CodexInteractiveCarrier.__init__` | `f'hyprial-codex-interactive-carrier-{session_ref[:8]}'` |
| `src/hyprial/daemon/impl/harnesses/codex/client.py:367` | async-task | `CodexAppServerClient._enter_process` | `-` |
| `src/hyprial/daemon/impl/harnesses/codex/client.py:368` | async-task | `CodexAppServerClient._enter_process` | `-` |
| `src/hyprial/daemon/impl/harnesses/codex/client.py:369` | async-task | `CodexAppServerClient._enter_process` | `-` |
| `src/hyprial/daemon/impl/harnesses/dsh/api.py:69` | thread | `_close_stream_async` | `-` |
| `src/hyprial/daemon/impl/harnesses/dsh/api.py:174` | default-executor-work | `DshHttpApi.call` | `-` |
| `src/hyprial/daemon/impl/harnesses/dsh/process.py:354` | thread | `DshHarnessProcess._connect_generation` | `f'hyprial-dsh-exit-{self.spec.name}'` |
| `src/hyprial/daemon/impl/harnesses/dsh/process.py:449` | thread | `DshHarnessProcess._read_banner` | `f'hyprial-dsh-stdout-{self.spec.name}'` |
| `src/hyprial/daemon/impl/harnesses/dsh/process.py:452` | thread | `DshHarnessProcess._read_banner` | `f'hyprial-dsh-stderr-{self.spec.name}'` |
| `src/hyprial/daemon/impl/harnesses/pi/rpc_client.py:290` | async-task | `PiRpcClient.__aenter__` | `-` |
| `src/hyprial/daemon/impl/harnesses/pi/rpc_client.py:293` | async-task | `PiRpcClient.__aenter__` | `-` |
| `src/hyprial/daemon/impl/harnesses/pi/rpc_client.py:376` | async-task | `PiRpcClient._close` | `-` |
| `src/hyprial/daemon/impl/harnesses/pi/rpc_client.py:381` | async-task | `PiRpcClient._close` | `-` |
| `src/hyprial/daemon/impl/harnesses/protocol/common.py:170` | thread | `PtyHarnessProcess.__init__` | `f'hyprial-{harness}-{child.pid}-stdout'` |
| `src/hyprial/daemon/impl/harnesses/protocol/common.py:175` | thread | `PtyHarnessProcess.__init__` | `f'hyprial-{harness}-{child.pid}-stderr'` |
| `src/hyprial/daemon/impl/harnesses/python_worker/__init__.py:423` | thread | `PythonWorkerTurnAdapter._start` | `f'hyprial-{self.kind}-reader-{self.spec.name}'` |
| `src/hyprial/daemon/impl/harnesses/python_worker/__init__.py:424` | thread | `PythonWorkerTurnAdapter._start` | `f'hyprial-{self.kind}-stderr-{self.spec.name}'` |
| `src/hyprial/daemon/impl/harnesses/python_worker/_python_worker.py:500` | thread-pool | `run` | `'hyprial-jev'` |
| `src/hyprial/daemon/impl/harnesses/runtime/tmux.py:69` | thread | `_EnvironmentHandoff.__init__` | `-` |
| `src/hyprial/daemon/impl/harnesses/streaming/process.py:140` | thread | `SequentialTurnProcess.__init__` | `f'{thread_name}-observer'` |
| `src/hyprial/daemon/impl/harnesses/streaming/process.py:210` | thread | `SequentialTurnProcess.__init__` | `thread_name` |
| `src/hyprial/daemon/impl/harnesses/streaming/process.py:464` | event-loop | `SequentialTurnProcess._thread_main` | `-` |
| `src/hyprial/daemon/impl/harnesses/streaming/process.py:491` | default-executor-work | `SequentialTurnProcess._run` | `-` |
| `src/hyprial/daemon/impl/harnesses/streaming/process.py:796` | default-executor-work | `SequentialTurnProcess._run` | `-` |
| `src/hyprial/daemon/impl/harnesses/streaming/turn_io.py:324` | timer-thread | `_SequentialTurnIoMixin._abort_generation_io` | `-` |
| `src/hyprial/daemon/impl/harnesses/turn_delivery/channel/worker_relay.py:110` | thread | `BoundWorkerRelay.start` | `-` |
| `src/hyprial/daemon/impl/harnesses/turn_delivery/channel/worker_relay.py:134` | thread | `BoundWorkerRelay._accept` | `-` |
| `src/hyprial/daemon/impl/harnesses/turn_delivery/turn/turn_runtime.py:268` | thread | `_CompletionRelay.__init__` | `'hyprial-turn-completion-relay'` |
| `src/hyprial/daemon/impl/harnesses/turn_delivery/turn/turn_runtime.py:714` | actor-runtime | `TurnRuntime.__init__` | `-` |
| `src/hyprial/daemon/impl/hooks/service.py:455` | thread | `DaemonHookService._configuration` | `'hook-config-refresh'` |
| `src/hyprial/daemon/impl/identity/bindings.py:491` | thread | `OrgBindingCacheProjector.__init__` | `'hyprial-org-binding-cache'` |
| `src/hyprial/daemon/impl/inbox/actor/coordinator.py:69` | actor-runtime | `DeliveryCustodyCoordinator.__init__` | `-` |
| `src/hyprial/daemon/impl/inbox/actor/worker.py:79` | thread | `DeliveryIoWorker.__init__` | `f'hyprial-delivery-io-{index}'` |
| `src/hyprial/daemon/impl/inbox/links/wake.py:103` | thread | `RecipientWakeCoordinator.__init__` | `'hyprial-recipient-wake'` |
| `src/hyprial/daemon/impl/inbox/tracking/fetch_receipts.py:39` | actor-runtime | `FetchReceiptPublisher.__init__` | `-` |
| `src/hyprial/daemon/impl/ipc/ipc_request_owner.py:50` | actor-runtime | `IpcRequestOwner.__init__` | `-` |
| `src/hyprial/daemon/impl/ipc/ipc_request_owner.py:66` | effect-worker-pool | `IpcRequestOwner.__init__` | `'ipc-completions'` |
| `src/hyprial/daemon/impl/ipc/server.py:330` | thread | `_IpcServerMixin._new_ipc_client_worker` | `'hyprial-daemon-ipc-client'` |
| `src/hyprial/daemon/impl/ipc/server.py:541` | thread | `_IpcServerMixin._dispatch_ipc_response` | `'hyprial-daemon-ipc-request'` |
| `src/hyprial/daemon/impl/lifecycle/atomic_lifecycle_ports.py:65` | thread | `AtomicLifecycleDomainPort.__init__` | `f'hyprial-{domain}-lifecycle-port'` |
| `src/hyprial/daemon/impl/lifecycle/duplicate_actor.py:19` | actor-runtime | `DuplicateInstanceAuthority.__init__` | `-` |
| `src/hyprial/daemon/impl/lifecycle/health/database.py:137` | thread | `DatabaseHealth.start` | `'hyprial-storage-health'` |
| `src/hyprial/daemon/impl/lifecycle/lifecycle_coordinator.py:522` | actor-runtime | `LifecycleCoordinator.__init__` | `-` |
| `src/hyprial/daemon/impl/lifecycle/lifecycle_coordinator.py:527` | thread | `LifecycleCoordinator.__init__` | `f'lifecycle-effect-{index}'` |
| `src/hyprial/daemon/impl/lifecycle/lifecycle_coordinator.py:532` | thread | `LifecycleCoordinator.__init__` | `'lifecycle-recover'` |
| `src/hyprial/daemon/impl/lifecycle/maintenance_watchdog.py:167` | thread | `MaintenanceWatchdog.start` | `'hyprial-maintenance-watchdog'` |
| `src/hyprial/daemon/impl/lifecycle/maintenance_watchdog.py:233` | actor-runtime | `ActorMaintenanceWatchdog.__init__` | `-` |
| `src/hyprial/daemon/impl/lifecycle/maintenance_watchdog.py:255` | effect-worker-pool | `ActorMaintenanceWatchdog.__init__` | `'maintenance-watchdog-report'` |
| `src/hyprial/daemon/impl/lifecycle/maintenance_watchdog.py:325` | thread | `ActorMaintenanceWatchdog.start` | `'hyprial-maintenance-watchdog'` |
| `src/hyprial/daemon/impl/lifecycle_manager/process.py:137` | thread | `LifecycleProcessManager.__init__` | `'hyprial-lifecycle-process-manager'` |
| `src/hyprial/daemon/impl/links/service/core.py:127` | thread | `LinkService.start` | `'link-service'` |
| `src/hyprial/daemon/impl/network/routing/session_route_coordinator.py:112` | actor-runtime | `SessionRouteCoordinator.__init__` | `-` |
| `src/hyprial/daemon/impl/network/routing/session_route_coordinator.py:139` | effect-worker-pool | `SessionRouteCoordinator.__init__` | `'session-route-effects'` |
| `src/hyprial/daemon/impl/operations/management_actor.py:117` | actor-runtime | `RegistryManagementAuthority.__init__` | `-` |
| `src/hyprial/daemon/impl/operations/management_actor.py:127` | effect-worker-pool | `RegistryManagementAuthority.__init__` | `'registry-management-io'` |
| `src/hyprial/daemon/impl/operations/watchdog_actor.py:136` | actor-runtime | `_WatchdogAuthority.__init__` | `-` |
| `src/hyprial/daemon/impl/operations/watchdog_actor.py:144` | effect-worker-pool | `_WatchdogAuthority.__init__` | `f'{self._path.stem}-effects'` |
| `src/hyprial/daemon/impl/org/mesh.py:156` | actor-runtime | `OrgContextMesh.__init__` | `-` |
| `src/hyprial/daemon/impl/org/mesh.py:165` | effect-worker-pool | `OrgContextMesh.__init__` | `'org-context-effects'` |
| `src/hyprial/daemon/impl/org/network/access/actor.py:119` | timer-scheduler | `PeerAccessActor.__init__` | `-` |
| `src/hyprial/daemon/impl/org/network/access/actor.py:121` | actor-runtime | `PeerAccessActor.__init__` | `-` |
| `src/hyprial/daemon/impl/org/network/access/actor.py:129` | effect-worker-pool | `PeerAccessActor.__init__` | `'org-peer-access-rebuild'` |
| `src/hyprial/daemon/impl/org/network/casdoor/sync.py:210` | actor-runtime | `CasdoorMembershipSync.__init__` | `-` |
| `src/hyprial/daemon/impl/org/network/casdoor/sync.py:211` | timer-scheduler | `CasdoorMembershipSync.__init__` | `-` |
| `src/hyprial/daemon/impl/org/network/casdoor/sync.py:219` | effect-worker-pool | `CasdoorMembershipSync.__init__` | `'org-casdoor-reads'` |
| `src/hyprial/daemon/impl/org/network/casdoor/webhook.py:165` | per-request-thread-server | `CasdoorWebhookReceiver.__init__` | `-` |
| `src/hyprial/daemon/impl/org/network/casdoor/webhook.py:174` | thread | `CasdoorWebhookReceiver.start` | `'org-casdoor-webhook'` |
| `src/hyprial/daemon/impl/org/network/status/__init__.py:28` | thread | `_ProjectionLane.__init__` | `f'org-network-{name}'` |
| `src/hyprial/daemon/impl/orgfs/composition/directory.py:122` | actor-runtime | `RuntimeDirectory._start_directory_ingress` | `-` |
| `src/hyprial/daemon/impl/orgfs/composition/directory.py:136` | effect-worker-pool | `RuntimeDirectory._start_directory_ingress` | `'orgfs-directory-effects'` |
| `src/hyprial/daemon/impl/orgfs/composition/lifecycle.py:81` | actor-runtime | `RuntimeLifecycle._ensure_lifecycle_authority` | `-` |
| `src/hyprial/daemon/impl/orgfs/composition/lifecycle.py:102` | effect-worker-pool | `RuntimeLifecycle._ensure_lifecycle_authority` | `'orgfs-directory-lifecycle-effects'` |
| `src/hyprial/daemon/impl/orgfs/document/effects.py:153` | actor-runtime | `FacadeEffects._ensure_effect_lane_locked` | `-` |
| `src/hyprial/daemon/impl/orgfs/document/effects.py:187` | effect-worker-pool | `FacadeEffects._ensure_effect_lane_locked` | `'orgfs-post-commit-callbacks'` |
| `src/hyprial/daemon/impl/orgfs/document/effects.py:194` | effect-worker-pool | `FacadeEffects._ensure_effect_lane_locked` | `'orgfs-post-commit-network'` |
| `src/hyprial/daemon/impl/orgfs/document/space_state.py:197` | actor-runtime | `FacadeSpaceState._ensure_space_state_owner` | `-` |
| `src/hyprial/daemon/impl/orgfs/document/space_state.py:215` | effect-worker-pool | `FacadeSpaceState._ensure_space_state_owner` | `f'orgfs-space-{space_id[:12]}-state-work'` |
| `src/hyprial/daemon/impl/orgfs/mesh.py:171` | thread-pool | `OrgFsMesh.__init__` | `f'orgfs-worker-{store.space_id[:12]}'` |
| `src/hyprial/daemon/impl/orgfs/mesh.py:203` | actor-runtime | `OrgFsMesh.__init__` | `-` |
| `src/hyprial/daemon/impl/orgfs/mesh.py:212` | effect-worker-pool | `OrgFsMesh.__init__` | `f'orgfs-space-{store.space_id[:12]}-effects'` |
| `src/hyprial/daemon/impl/orgfs/projection/checkout_authority.py:116` | actor-runtime | `CheckoutAuthority.__init__` | `-` |
| `src/hyprial/daemon/impl/orgfs/projection/checkout_authority.py:132` | effect-worker-pool | `CheckoutAuthority.__init__` | `f'orgfs-checkout-{space_id[:12]}-io'` |
| `src/hyprial/daemon/impl/orgfs/storage/blob_authority.py:182` | actor-runtime | `BlobAuthority.__init__` | `-` |
| `src/hyprial/daemon/impl/orgfs/storage/blob_authority.py:215` | effect-worker-pool | `BlobAuthority.__init__` | `'orgfs-blob-effects'` |
| `src/hyprial/daemon/impl/orgfs/storage/replica_authority.py:211` | actor-runtime | `ReplicaAuthority.__init__` | `-` |
| `src/hyprial/daemon/impl/orgfs/storage/replica_authority.py:229` | effect-worker-pool | `ReplicaAuthority.__init__` | `f'orgfs-replica-{self.space_id[:12]}-effects'` |
| `src/hyprial/daemon/impl/orgfs/storage/space_authority.py:256` | actor-runtime | `OrgSpaceAuthority.__init__` | `-` |
| `src/hyprial/daemon/impl/orgfs/storage/space_authority.py:265` | effect-worker-pool | `OrgSpaceAuthority.__init__` | `f'orgfs-space-{space_id[:12]}-writer'` |
| `src/hyprial/daemon/impl/orgfs/webserver/server.py:420` | inherited-thread-spawner | `build_server` | `ThreadingMixIn` |
| `src/hyprial/daemon/impl/orgfs/webserver/server.py:426` | inherited-thread-spawner | `build_server` | `ThreadingMixIn` |
| `src/hyprial/daemon/impl/orgfs/webserver/server.py:457` | thread | `serve._stop` | `-` |
| `src/hyprial/daemon/impl/pac/actors/daemon.py:373` | thread | `PacActorService.__init__` | `f'hyprial-pac-actor-{index}'` |
| `src/hyprial/daemon/impl/pac/actors/daemon.py:380` | thread | `PacActorService.__init__` | `'hyprial-pac-clock'` |
| `src/hyprial/daemon/impl/pac/actors/daemon.py:385` | thread | `PacActorService.__init__` | `'hyprial-pac-scan'` |
| `src/hyprial/daemon/impl/pac/actors/gc.py:86` | thread | `PacGc.__init__` | `'hyprial-pac-gc'` |
| `src/hyprial/daemon/impl/pac/graphs/authority/__init__.py:137` | actor-runtime | `PacGraphAuthority.__init__` | `-` |
| `src/hyprial/daemon/impl/pac/graphs/authority/__init__.py:158` | thread | `PacGraphAuthority.__init__` | `'pac-graph-delivery'` |
| `src/hyprial/daemon/impl/pac/graphs/authority/__init__.py:163` | thread | `PacGraphAuthority.__init__` | `f'pac-actor-effect-{index}'` |
| `src/hyprial/daemon/impl/pac/workflows/runtime/core.py:91` | thread | `_GraphWorkflowServiceCore.__init__` | `f'hyprial-workflow-notify-{index}'` |
| `src/hyprial/daemon/impl/pac/workflows/runtime/core.py:98` | thread | `_GraphWorkflowServiceCore.__init__` | `'hyprial-pac-workflow'` |
| `src/hyprial/daemon/impl/processes/orphan_processes.py:591` | thread | `OrphanProcessRegistry._request_cleanup` | `f'hyprial-orphan-gc-{orphan_id}'` |
| `src/hyprial/daemon/impl/processes/orphan_processes.py:705` | actor-runtime | `OrphanProcessAuthority.__init__` | `-` |
| `src/hyprial/daemon/impl/processes/orphan_processes.py:727` | effect-worker-pool | `OrphanProcessAuthority.__init__` | `'orphan-process-custody-io'` |
| `src/hyprial/daemon/impl/processes/process_owner.py:158` | actor-runtime | `ProcessOwner.__init__` | `-` |
| `src/hyprial/daemon/impl/processes/process_owner.py:167` | effect-worker-pool | `ProcessOwner.__init__` | `'process-native-io'` |
| `src/hyprial/daemon/impl/processes/result_settlement.py:62` | actor-runtime | `ResultSettlementCoordinator.__init__` | `-` |
| `src/hyprial/daemon/impl/processes/result_settlement.py:73` | effect-worker-pool | `ResultSettlementCoordinator.__init__` | `'harness-result-settlement-io'` |
| `src/hyprial/daemon/impl/route_registration/io.py:117` | thread | `RouteRegistrationIo.__init__` | `f'hyprial-route-native-{index}'` |
| `src/hyprial/daemon/impl/route_registration/io.py:126` | thread | `RouteRegistrationIo.__init__` | `'hyprial-route-registration-io'` |
| `src/hyprial/daemon/impl/runtime/bridge.py:436` | thread | `DaemonEventBridge.start` | `'hyprial-inbox-retry-pump'` |
| `src/hyprial/daemon/impl/runtime/forwards.py:69` | thread | `_BridgeForwardsMixin._start_forward_thread` | `'hyprial-forward-pump'` |
| `src/hyprial/daemon/impl/runtime/forwards.py:568` | thread | `_BridgeForwardsMixin._notify_owner_of_diverted` | `'hyprial-owner-diverted-notice'` |
| `src/hyprial/daemon/impl/runtime/settlement.py:127` | actor-runtime | `ForwardSettlementCoordinator.__init__` | `-` |
| `src/hyprial/daemon/impl/runtime/settlement.py:144` | effect-worker-pool | `ForwardSettlementCoordinator.__init__` | `'harness-forward-io'` |
| `src/hyprial/daemon/impl/service_connect/io.py:64` | effect-worker-pool | `_BoundedLane.__init__` | `name` |
| `src/hyprial/daemon/impl/session_actor/actor.py:122` | actor-runtime | `SessionActor.__init__` | `-` |
| `src/hyprial/daemon/impl/session_actor/actor.py:235` | thread | `SessionActor._on_undelivered` | `'hyprial-session-undelivered'` |
| `src/hyprial/daemon/impl/session_actor/actor.py:448` | thread | `SessionActor._schedule_generation_replay` | `f'hyprial-session-replay-{generation}'` |
| `src/hyprial/daemon/impl/session_actor/worker.py:60` | thread | `_AgentEffectWorker.__init__` | `'hyprial-session-agent-effects'` |
| `src/hyprial/daemon/impl/squire/actors/delivery.py:101` | actor-runtime | `UserDeliveryActor.__init__` | `-` |
| `src/hyprial/daemon/impl/squire/actors/delivery.py:110` | effect-worker-pool | `UserDeliveryActor.__init__` | `'user-delivery-io'` |
| `src/hyprial/daemon/impl/squire/actors/ledger.py:81` | actor-runtime | `UserDeliveryLedgerAuthority.__init__` | `-` |
| `src/hyprial/daemon/impl/squire/actors/profile.py:119` | actor-runtime | `UserProfileAuthority.__init__` | `-` |
| `src/hyprial/daemon/impl/squire/actors/profile.py:127` | effect-worker-pool | `UserProfileAuthority.__init__` | `'user-profile-storage'` |
| `src/hyprial/daemon/impl/state_persistence/authority.py:89` | actor-runtime | `StatePersistenceAuthority.__init__` | `-` |
| `src/hyprial/daemon/impl/transfer/p2/actor.py:109` | actor-runtime | `AdmissionActor.__init__` | `-` |
| `src/hyprial/daemon/impl/transfer/p2/actor.py:117` | effect-worker-pool | `AdmissionActor.__init__` | `'transfer-p2-io'` |
| `src/hyprial/daemon/impl/transfer/p2/source/runner.py:40` | actor-runtime | `SourceRunner.__init__` | `-` |
| `src/hyprial/daemon/impl/transfer/p2/source/runner.py:45` | effect-worker-pool | `SourceRunner.__init__` | `'transfer-source-execute'` |
| `src/hyprial/daemon/impl/transport/presence_actor.py:77` | actor-runtime | `PresenceAuthority.__init__` | `-` |
| `src/hyprial/daemon/impl/transport/presence_actor.py:87` | effect-worker-pool | `PresenceAuthority.__init__` | `'presence-delete-reconcile'` |
| `src/hyprial/daemon/impl/transport/query_actor.py:51` | actor-runtime | `QueryIoOwner.__init__` | `-` |
| `src/hyprial/daemon/impl/transport/session_actor.py:198` | effect-worker-pool | `_CallbackPool.__init__` | `'transport-callback'` |
| `src/hyprial/daemon/impl/transport/session_actor.py:395` | timer-scheduler | `TransportSessionAuthority.__init__` | `-` |
| `src/hyprial/daemon/impl/transport/session_actor.py:396` | actor-runtime | `TransportSessionAuthority.__init__` | `-` |
| `src/hyprial/daemon/impl/transport/session_actor.py:410` | effect-worker-pool | `TransportSessionAuthority.__init__` | `'transport-data'` |
| `src/hyprial/daemon/impl/transport/session_actor.py:418` | effect-worker-pool | `TransportSessionAuthority.__init__` | `'transport-lifetime'` |
| `src/hyprial/daemon/impl/transport/session_recovery.py:189` | thread | `TransportRecoveryMixin._schedule_degraded_recovery` | `'hyprial-zenoh-session-recovery'` |
| `src/hyprial/daemon/impl/transport/zenoh/dispatchers.py:68` | thread | `_CallbackDispatcher._ensure_worker` | `f'hyprial-zenoh-callback-{index}'` |
| `src/hyprial/daemon/impl/transport/zenoh/dispatchers.py:233` | thread | `_QueryDispatcher._ensure_workers` | `f'hyprial-zenoh-query-{index}'` |
| `src/hyprial/daemon/impl/transport/zenoh/presence.py:228` | thread | `LivelinessDirectory._reestablish_presence` | `'hyprial-presence-rebuild-reconcile'` |
| `src/hyprial/daemon/impl/transport/zenoh/transport.py:199` | thread | `ZenohTransport.__init__` | `'hyprial-zenoh-events'` |
| `src/hyprial/identity/impl/agents/actor/_core.py:157` | actor-runtime | `AgentActor.__init__` | `-` |
| `src/hyprial/identity/impl/agents/home/effects.py:170` | thread | `AgentHomeEffectLane.__init__` | `'agent-home-effect'` |
| `src/hyprial/identity/impl/agents/state/read_port.py:127` | thread | `AgentReadEffectPort.__init__` | `f'agent-read-effect-{index}'` |
| `src/hyprial/identity/impl/agents/state/worker_state.py:217` | effect-worker-pool | `RestorePolicyAuthority._call` | `'agent-restore-policy-storage'` |
| `src/hyprial/identity/impl/agents/state/worker_state.py:309` | effect-worker-pool | `BlockingFailureAuthority._effects_locked` | `'agent-blocking-failure'` |
| `src/hyprial/identity/impl/agents/state/worker_state.py:444` | timer-thread | `BlockingFailureAuthority._schedule_retry` | `-` |
| `src/hyprial/identity/impl/provider_auth/actor/_authority.py:111` | actor-runtime | `ProviderAuthAuthority.__init__` | `-` |
| `src/hyprial/identity/impl/provider_auth/actor/_authority.py:119` | effect-worker-pool | `ProviderAuthAuthority.__init__` | `'provider-auth-state'` |
| `src/hyprial/identity/impl/provider_auth/actor/_authority.py:125` | effect-worker-pool | `ProviderAuthAuthority.__init__` | `'provider-auth-helper'` |
| `src/hyprial/identity/impl/provider_auth/actor/_authority.py:134` | effect-worker-pool | `ProviderAuthAuthority.__init__` | `'provider-auth-notify'` |
| `src/hyprial/identity/impl/provider_auth/actor/_authority.py:141` | thread | `ProviderAuthAuthority.__init__` | `'provider-auth-notify-retry'` |
| `src/hyprial/identity/impl/provider_auth/coordinator/_coordinator.py:501` | thread | `ProviderAuthCoordinator._spawn_round` | `f'hyprial-provider-auth-{episode.provider}'` |
| `src/hyprial/identity/impl/provider_auth/helper.py:159` | thread | `DeviceLoginRunner.__call__` | `-` |
| `src/hyprial/kernel/impl/actor_runtime/backends/pykka.py:71` | pykka-actor-thread | `_RuntimeActor` | `ThreadingActor` |
| `src/hyprial/kernel/impl/actor_runtime/effects.py:81` | thread | `EffectLane.__init__` | `f'{name}-{i}'` |
| `src/hyprial/kernel/impl/actor_runtime/runtime.py:68` | timer-scheduler | `ActorRuntime.__init__` | `-` |
| `src/hyprial/kernel/impl/hooks/bus.py:81` | effect-worker-pool | `_Lane.__init__` | `f'hook-lane-{name}'` |
| `src/hyprial/kernel/impl/hooks/intercept.py:206` | thread | `HookInterceptPort._start_invocation` | `f'hyprial-intercept-{request.key[:12]}'` |
| `src/hyprial/kernel/impl/log/writer.py:47` | actor-runtime | `LogWriter.__init__` | `-` |
| `src/hyprial/kernel/impl/platform/windows_owned_process.py:150` | default-executor-work | `WindowsOwnedProcessGroup.spawn` | `-` |

Synchronization counts: bounded-or-unbounded-queue=34, condition=35, lock=266, semaphore=11, unbounded-queue=1.

### E2. Retry scanner output

## Retry evidence

Source baseline: `d5a4dd257d15320ce59a9bb56d116e786ab4e195`. Curated retry/restart/reconnect families: **122**. Mechanical retry-shape candidates: **625**; operation-repeat shape candidates: **136**.

Disposition counts: classified=158, limit-class=5, dismissed=462, undisposed=0. Evidence qualification: inventory complete with respect to the detected shapes listed in Method coverage; shapes listed as blind spots are flagged, not enumerated exhaustively.

| family | anchor | delay schedule | cap | budget scope | attempt logging |
|---|---|---|---|---|---|
| actor supervision | `src/hyprial/kernel/impl/actor_runtime/guardian.py:262` `_process_failure` | configured base_backoff × 2^(attempt−1), capped at max_backoff, with configured jitter | configured max_restarts inside restart_window; defaults are 3/60s, 5/60s, or 2/120s | per actor guardian record; no daemon-global restart budget | ActorEvent records failure, scheduled delay, restart, or quarantine |
| effect completion custody | `src/hyprial/kernel/impl/actor_runtime/effects.py:162` `_run` | fixed retry_seconds, default 0.01s | uncapped until owner ACK or lane close | per EffectLane capacity; no daemon-global budget | counter only; no per-attempt log in kernel |
| agent-home effect completion custody | `src/hyprial/identity/impl/agents/home/effects.py:228` `_run` | fixed retry_seconds, default 0.01s | uncapped until owner ACK or close | per home effect lane; no daemon-global budget | no per-attempt log |
| provider-auth helper retry | `src/hyprial/identity/impl/provider_auth/actor/_authority.py:473` `_retry_loop` | fixed notification_retry_seconds, 1s by default, through one due-time heap | no attempt cap; retained notice/decision/helper/restore state or authority close ends replay | one coalesced heap entry per kind/key plus configured provider-auth custody capacities | retry counters retained; notice/helper outcomes logged; scheduler attempts are log-silent |
| agent state publication retry | `src/hyprial/identity/impl/agents/state/worker_state.py:443` `_schedule_retry` | fixed 0.1s threading.Timer after effect error, negative notice result, or overload | no attempt cap while the key remains pending; close ends replay | one pending entry per failure key; timers have no shared daemon budget | timer retries are log-silent |
| durable inbox delivery retry | `src/hyprial/daemon/impl/inbox/service/delivery/retry.py:832` `_log_retry` | durable backoff_ms tuple, last delay repeats | until expiry/terminal outcome; attempts durable | per message row; no daemon-global attempt budget | outbox retry event for every backoff step |
| inbox effect completion settlement | `src/hyprial/daemon/impl/inbox/actor/worker.py:371` `_settle_completion` | 0.005/0.01/0.02/0.05s then 0.05s | bounded by 1s settlement deadline per completion | per inbox worker completion | failure surfaced after deadline; per-sleep log absent |
| daemon outbox retry pump | `src/hyprial/daemon/impl/runtime/reconcile.py:49` `_retry_pump_run` | next durable due time; failed/empty round has bounded wait | thread lifetime; row policy supplies message cap/expiry | single daemon pump, per-message durable budgets | delivery service logs attempts; pump overrun/fault logging is caller-owned |
| recipient wake custody | `src/hyprial/daemon/impl/inbox/links/wake.py:352` `_wait_retry` | fixed 0.01s plus 0.1s completion waits by default | until settled, superseded, stale, or close | one coordinator; 128 pending recipients by default | unexpected settlement faults per attempt; admission waits are log-silent |
| IPC accept resource-exhaustion retry | `src/hyprial/daemon/impl/ipc/server.py:168` `_serve` | exponential from 0.05s, capped at 1s | until accept succeeds, server closes, or daemon stops | main IPC accept loop and process file-descriptor budget | each exhaustion logged best-effort |
| lifecycle journal recovery | `src/hyprial/daemon/impl/lifecycle_manager/process.py:320` `_guarded_recover` | 0.05s queue poll plus configured capped backoff on store faults | manager lifetime; operation deadline fences new forward work but does not revoke admitted effect custody | one process-manager queue; no daemon-global retry budget | first and rate-limited aggregate faults, plus recovery total |
| harness process reconcile/restart | `src/hyprial/daemon/impl/harnesses/actor/behaviors/reconcile.py:38` `_on_reconcile` | restart_after with configured base/exponential backoff | per-generation start-attempt budget; 0 explicitly means infinite | per harness record, not daemon-global | harness state/failure events retain each decision; no separate per-sleep log |
| streaming harness reconnect | `src/hyprial/daemon/impl/harnesses/streaming/process.py:466` `_run` | exponential from 0.25s, capped at 30s by default | process lifetime; no attempt cap in pump | per streaming worker process | each failed connection is reported through the turn-failure/error path; sleeps are log-silent |
| usage collection backoff | `src/hyprial/biz/impl/usage.py:693` `refresh_once` | 30m refresh cadence; exponential retry-after capped at 30m or 60m | usage-cache thread lifetime | one cache thread; budget tracked per source | failure retained in source snapshot; retry attempts log-silent |
| turn delivery retry | `src/hyprial/daemon/impl/harnesses/turn_delivery/turn/turn_runtime.py:546` `_io_failed` | no timer or backoff: the failure-decision callback receives retry=True immediately | max_delivery_attempts, 5 by default; terminal provider failures stop on attempt 1 | per turn runtime/pending command | turn-retry or turn-abandoned event per failed attempt |
| Codex carrier fact retry | `src/hyprial/daemon/impl/harnesses/carrier/codex_carrier_support.py:103` `flush` | retry_seconds * 2^failures, capped at 5s | process lifetime; no attempt cap | per carrier support instance | worker.carrier.error per failed report |
| Codex carrier admission replay | `src/hyprial/daemon/impl/harnesses/codex/settlement.py:386` `_retry_carrier_facts` | carrier poll cadence; immediate drain until overload | until accepted or carrier closes; bounded fact capacity | per Codex worker carrier-fact map | initial deferral/rejection logged; replay attempts log-silent |
| Codex turn-state RPC read retry | `src/hyprial/daemon/impl/harnesses/codex/carrier.py:131` `receive_response` | 0.5s after CodexAppServerRpcError; successful nonterminal reads are observation polls at the same interval | no attempt or elapsed cap; final turn state or interruption ends observation | one turn read at a time in the connector process default asyncio executor | transient RPC errors are log-silent; final error becomes the turn outcome |
| interactive carrier command admission | `src/hyprial/daemon/impl/harnesses/carrier/interactive_carrier_runtime.py:526` `submit` | fixed 0.002s after OVERLOADED | caller timeout, 0.25s by default | caller thread and one carrier actor mailbox | attempts are log-silent; terminal result is OVERLOADED |
| Codex app-server startup connection | `src/hyprial/daemon/impl/harnesses/codex/app_server.py:221` `start` | fixed 0.05s after socket absence or connect/initialize failure | startup_timeout_seconds wall-clock deadline | startup caller and one child process/socket | attempt exceptions are swallowed; exhaustion raises TimeoutError |
| Codex loaded-thread failover discovery | `src/hyprial/daemon/impl/harnesses/codex/app_server.py:332` `discover_thread` | immediate thread/read against each successive loaded thread ID after CodexAppServerRpcError or a non-candidate result | each ID from thread/loaded/list once; no aggregate deadline; each RPC uses request_timeout_seconds (default 15s) | one discovery caller serialized by the app-server RPC lock | per-ID failures and non-candidates are log-silent; exhaustion raises ConnectionError |
| Codex carrier poll-pass retry | `src/hyprial/daemon/impl/harnesses/codex/carrier.py:325` `_poll_main` | poll_seconds after a caught poll-pass exception; successful passes use state-machine delay or immediate 0s | carrier thread lifetime; no attempt cap | one carrier polling thread serializes heartbeat and poll lanes | every caught poll-pass exception is logged; waits have no separate log |
| DSH resolved-address connection failover | `src/hyprial/daemon/impl/harnesses/dsh/api.py:390` `_connect_addresses` | immediate connect against each successive resolved address after OSError | each resolved address once; no aggregate deadline; each connect gets the full effective request timeout | one HTTP request and its owned candidate sockets | per-address failures are log-silent; the final OSError is wrapped in DshApiError by the caller |
| Lark adapter process restart | `src/hyprial/daemon/impl/adapters/lark/runtime/handler.py:619` `_begin_reconcile_spawns` | named lifecycle policy delay; stale worker pauses 1s before exit | max_restarts within restart_window, then per-gateway quarantine | per gateway; no daemon-global budget | worker observations and restart/quarantine outcomes are logged; the scheduled sleep has no separate log |
| Lark reaction completion admission | `src/hyprial/daemon/impl/adapters/lark/outbound/reactions.py:704` `completion` | fixed 0.01s actor-admission retry | 5s deadline | pins the ACK or reply native lane completion callback | per-attempt and deadline-expiry logs absent |
| Lark adapter completion admission | `src/hyprial/daemon/impl/adapters/lark/runtime/runtime.py:170` `completion` | fixed 0.01s actor-admission retry | 5s deadline | pins the process-effect completion callback | per-attempt logs absent; deadline becomes correlation failure |
| Lark inbound transient retry | `src/hyprial/daemon/impl/adapters/lark/inbound/pipeline.py:110` `_with_transient_retry` | doubling backoff from configured initial to configured max | attempt count and wall-clock deadline | per inbound operation | helper emits no per-sleep log; terminal exhaustion reports attempts and elapsed time |
| identity binding cache rebuild | `src/hyprial/daemon/impl/identity/bindings.py:565` `_run` | on change plus 300s JWKS TTL or earlier key recheck | projector thread lifetime | one identity-binding projector thread | each failed rebuild logged; no attempt counter |
| identity binding publication retry | `src/hyprial/daemon/impl/identity/bindings.py:543` `retry_publish` | capped exponential from lifecycle constants | uncapped attempts while publication remains required | single projector pending slot; no daemon-global budget | warnings aggregated by warning interval; attempt counter retained |
| Zenoh degraded-session recovery | `src/hyprial/daemon/impl/transport/session_recovery.py:198` `_recover_degraded` | 1/2/4/8/16/30s then 30s | uncapped until success, close, or another reopen wins | one transport session recovery thread | last error retained per failure; terminal reopen event logged |
| Zenoh native call reopen | `src/hyprial/daemon/impl/transport/zenoh/transport.py:294` `_with_session` | immediate single reopen | one retry | per native call | terminal error; retry itself not logged |
| transport callback refresh | `src/hyprial/daemon/impl/transport/session_actor.py:585` `_schedule_recovery` | fixed 1s, coalesced by generation and recover_after | until callbacks complete, close, or session degrades | one scheduler entry per transport session | callback rejection counted; scheduled attempts log-silent |
| session route mutation retry | `src/hyprial/daemon/impl/application/actors/sessions/routes.py:776` `_settled_route_mutation` | exponential from 0.005s, capped at 0.5s | uncapped except PORT_CLOSING | caller thread and shared route-registration authority | every failed attempt logged |
| org join retry | `src/hyprial/daemon/impl/org/network/service.py:648` `_join_with_retry` | fixed 0.5s | caller deadline; 30s by default | caller thread; no shared budget | per-attempt log absent |
| org access incomplete rebuild | `src/hyprial/daemon/impl/org/network/access/actor.py:347` `_retry_later` | fixed 10s generation-fenced schedule | until complete rebuild or close | one named scheduler entry per access actor | failed allow logged; incomplete retry scheduling log-silent |
| orgfs scheduled supplier sync | `src/hyprial/daemon/impl/orgfs/network/supplier_health.py:228` `_run_scheduled_sync` | initial attempt plus one retry after 0.25s | 4.5s per run_once; at most one coalesced rerun | per-space queue 32 and single orgfs worker; supplier health shared across spaces | every failed attempt plus held/deferred/recovered state logged |
| orgfs projection repair | `src/hyprial/daemon/impl/orgfs/network/recovery.py:305` `_repair_projections` | 0.25s between attempt 1 and attempt 2 for one failure epoch | 2 attempts per epoch and one reset epoch after a later success; at most 4 failed applications per row per daemon generation | per document/writer/sequence failure record; repair runs on the shared orgfs worker | abandonment logs at each exhausted epoch; individual retry sleep is log-silent |
| orgfs blob recovery | `src/hyprial/daemon/impl/orgfs/network/recovery.py:689` `_run_blob_recovery` | immediate next supplier after failure; each discovered supplier is attempted once per recovery job | ends on first success or when no unattempted allowed supplier remains | one worker future per digest; pending digests and waiters share ORGFS_BLOB_FETCH_QUEUE_LIMIT | every supplier failure and terminal drop/recovery outcome is logged |
| orgfs content provisioning wait | `src/hyprial/daemon/impl/orgfs/composition/content.py:167` `await_content` | request_blob, wait to caller deadline, then sleep up to 0.1s | caller-supplied deadline | IPC caller plus per-space blob queue and single orgfs worker | content loop log-silent; blob worker logs fetch failure/drop |
| orgfs holder failover refresh | `src/hyprial/daemon/impl/orgfs/composition/content.py:92` `refresh_space` | immediate sync_from against each successive online holder after StoreError or OrgFsError | each holder once within the caller timeout; deadline is checked before and after each attempt | IPC content caller, one mesh, and the snapshotted online-holder set | attempt failures are log-silent; aggregate success is returned as a boolean |
| orgfs join holder failover | `src/hyprial/daemon/impl/orgfs/composition/lifecycle.py:599` `_join_sync` | immediate sync_from against each successive sorted holder after StoreError or OrgFsError | each holder once; no aggregate elapsed deadline beyond timeouts inside mesh operations | one lifecycle join request, one mesh, and the merged holder snapshot/candidate set | attempt failures are log-silent; exhaustion raises the final OrgFsError |
| transfer restore observation | `src/hyprial/daemon/impl/application/actors/transfer_p2.py:603` `_retry_transfer_restore` | restore-wake cadence, submitted by 1s daemon maintenance tick | bounded batch per pass; retained until outcome, stale, or exhaustion | one bounded restore-wake lane and deferred map | terminal/exhaustion warnings; unsuccessful observations mostly log-silent |
| harness restore eligibility replay | `src/hyprial/daemon/impl/application/restore.py:179` `_retry_restore_eligibility` | 1s daemon maintenance tick | configured batch; until accepted, stale, or close | one bounded pending eligibility map | overload logged on initial admission; replay attempts log-silent |
| PAC cleanup retry | `src/hyprial/daemon/impl/pac/actors/coordinator/types.py:215` `cleanup_retry_delay_ms` | 5 minutes × 2^(attempt−1), capped at 6 hours; attempt 0 is immediate | no attempt cap; retained until graph cleanup settles | per cleanup intent; at most 2 cleanup retries admitted per sweep | journal state records each reconcile failure |
| routine recovery loop | `src/hyprial/biz/impl/routine/execution/coordinator/__init__.py:395` `_recover_loop` | fixed 0.05s after failed recovery | coordinator lifetime | one routine coordinator thread | recovery loop emits no per-attempt log |
| routine coordinator completion admission | `src/hyprial/biz/impl/routine/execution/coordinator/__init__.py:228` `_effect_loop` | fixed 0.01s after OVERLOADED actor admission | no attempt or elapsed cap; accepted/CLOSED admission or coordinator close ends replay | pins one routine coordinator effect thread and its operation custody | admission retries are log-silent |
| turn-pulse filename collision | `src/hyprial/biz/impl/turn_pulse.py:19` `signal_turn_ended` | immediate retry with a new random filename after FileExistsError | three attempts | one short-lived hook caller and target directory | attempts are log-silent; exhaustion raises OSError |
| state database connection initialization | `src/hyprial/daemon/impl/state_db.py:122` `_open` | SQLite attempt then up to 0.05s sleep | 2s aggregate deadline | per-file connection lock; retry runs on caller thread | per-attempt and exhaustion logs absent |
| state database write-lock acquisition | `src/hyprial/daemon/impl/state_db.py:236` `_begin_write` | SQLite 2s busy handler per attempt plus fixed 0.05s sleep | 8s retry deadline by default; final attempt can reach about 10s total | per-file write lock; retry runs on caller thread | per-attempt and exhaustion logs absent |
| lifecycle domain admission | `src/hyprial/daemon/impl/lifecycle_manager/process.py:734` `_admit` | 0.005/0.01/0.02s then 0.02s by default | 1s deadline by default | pins the single lifecycle process-manager worker | per-attempt log absent; exhaustion becomes PORT_OVERLOADED |
| lifecycle completion admission | `src/hyprial/daemon/impl/lifecycle/lifecycle_coordinator.py:758` `_deliver` | fixed 0.005s actor-admission retry | uncapped until accepted, actor closes, or coordinator closes | pins the current lifecycle effect or recovery worker | per-attempt log absent |
| lifecycle recovery loop | `src/hyprial/daemon/impl/lifecycle/lifecycle_coordinator.py:767` `_recover_loop` | 0.01s between recovered batches; 0.05s idle/fault waits | coordinator lifetime | single lifecycle recovery thread | fault callback receives each caught recovery fault; successful batch retries are log-silent |
| session agent-command admission | `src/hyprial/daemon/impl/session_actor/worker.py:147` `_admit` | configured backoff tuple; default 0.005/0.01/0.02/0.05s then 0.05s | configured wall-clock deadline; 1s by default | one bounded Agent-effect queue, capacity 64 by default, and one worker thread per SessionActor | attempts are log-silent; terminal failure is returned as _AgentEffectUnavailable |
| pending availability notice replay | `src/hyprial/daemon/impl/runtime/forwards.py:588` `_retry_pending_notices` | runtime reconciliation cadence and settlement flush | no attempt cap; accepted delivery or runtime close removes custody | one runtime pending-notice map | first failure logged; repeated attempts log-silent |
| blocking-failure observer replay | `src/hyprial/daemon/impl/runtime/results.py:778` `_retry_blocking_failures` | runtime reconciliation cadence; tight close drain until deadline | bounded map; until accepted, observer removal, or close deadline | one runtime map with configured capacity | capacity exhaustion logged; replay attempts log-silent |
| MCP inbox-watch poll recovery | `src/hyprial/daemon/impl/mcp/channel/loops.py:64` `_run_channel_poll_loop` | Sleep.ms from the shared inbox-watch state machine; base poll interval is caller-configured | no attempt cap; Quiet verdict or task cancellation ends the loop | one async poll task per MCP channel | each failed non-wait action increments and reports the failure count; wait failures are debug logged |
| MCP inbox-watch heartbeat recovery | `src/hyprial/daemon/impl/mcp/channel/loops.py:212` `_run_channel_heartbeat_loop` | Sleep.ms from the shared inbox-watch state machine; heartbeat default comes from CHANNEL_HEARTBEAT_INTERVAL_SECONDS | no attempt cap; Quiet verdict or task cancellation ends the loop | one async heartbeat task per MCP channel | each failure increments and reports the failure count |
| MCP wake retry | `src/hyprial/daemon/impl/mcp/wake.py:423` `_default_retry_delay` | 1/2/4/8/16/32/60s then 60s for failures; accepted-but-unread resignal is 30/60/120/240/300s then 300s | no attempt cap; durable item state, acknowledgement, session removal, or coordinator close ends replay | shared wake queue, tail requeue prevents head blocking | each attempted wake outcome is sent to the diagnostic logger; log I/O failure is ignored |
| MCP stateless reconnect | `src/hyprial/daemon/impl/mcp/proxy.py:27` `call` | immediate reconnect with the same mutation request ID after DaemonDisconnected | reconnect_attempts plus the initial attempt; two total by default | one MCP tool call and fresh connection per attempt | attempts are log-silent; final DaemonDisconnected propagates |
| route native partial cleanup | `src/hyprial/daemon/impl/route_registration/io.py:613` `_execute_native` | exponential from 0.005s, capped at 0.5s | uncapped while preparation cleanup keeps failing | shared bounded native queue and 8 workers by default | per-attempt log absent; final preparation failure returned |
| route completion publication | `src/hyprial/daemon/impl/route_registration/io.py:901` `_settle` | 0.005/0.01/0.02s then 0.02s by default | 1s deadline by default | one route owner and per-token settlement state | per-attempt and deadline-expiry logs absent |
| inbox watch registration/list refresh | `src/hyprial/daemon/impl/inbox_watch.py:47` `backoff_ms` | exponential by failure count | watch lifetime | one watch actor schedule, separate failure counters | pure state machine retains failure counters but emits no log itself; channel loops log/report outcomes |
| agent migration owner generation retry | `src/hyprial/identity/impl/agents/actor/_core.py:521` `migration_owner_call` | 1s event waits; resubmit only after actor generation changes | uncapped until a result arrives | caller thread and one stable call id per migration owner request | per-wait and resubmit logs absent |
| forwarding sidecar supervision | `src/hyprial/daemon/impl/forwarding/supervisor.py:839` `_account_for_failure` | process-lifecycle SupervisionPolicy exponential delay with jitter via GenerationScheduler | max_restarts inside restart_window; exhaustion leaves forwarding down until daemon restart | per sidecar supervisor; no daemon-global restart budget | every restart decision or exhausted budget emits a warning/error event |
| routine effect completion admission | `src/hyprial/biz/impl/routine/execution/service.py:600` `_submit_completion` | fixed 0.01s actor-admission retry | no attempt cap; accepted admission, actor generation change, or service stop ends replay | pins one routine effect worker; configured effect queues bound retained items | admission retries are log-silent |
| routine alarm effect replay | `src/hyprial/biz/impl/routine/execution/service.py:554` `_effect_worker_loop` | 500ms not-before timestamp; refill polls at up to 50ms while idle | no attempt cap while the durable projection keeps the alarm effect pending | one effect id and retry timestamp per pending alarm; workers/queues are configured | replay scheduling is log-silent; completion carries the alarm error |
| Lark inbound completion admission | `src/hyprial/daemon/impl/adapters/lark/inbound/inbound_runtime.py:314` `completion` | fixed 0.01s actor-admission retry | 5s deadline | pins the single inbound effect worker completion callback | per-attempt logs absent; deadline fails the correlation |
| Lark gateway dependency close | `src/hyprial/daemon/impl/adapters/lark/outbound/gateway.py:213` `_drain_close` | fixed 0.01s after a false or exceptional 0.05s close attempt | no attempt or elapsed cap; successful close ends each dependency | one retained closer thread serializes runtime and dependent owners | last exception type is retained; attempts have no log |
| Lark state WAL transition | `src/hyprial/daemon/impl/adapters/lark/state/schema.py:72` `_switch_to_wal` | fixed 0.05s after SQLite OperationalError | SQLite busy timeout in _BUSY_TIMEOUT_MS; 5s in this baseline | one adapter-state connection; retry runs on its opening thread | per-attempt and exhaustion logs absent |
| user store WAL transition | `src/hyprial/identity/impl/users/store/_store.py:165` `_switch_to_wal` | fixed 0.05s after SQLite OperationalError | SQLite busy timeout in _SQLITE_BUSY_MS; 5s in this baseline | one user-store connection; retry runs on its opening thread | per-attempt and exhaustion logs absent |
| transfer quiesce reconciliation | `src/hyprial/daemon/impl/application/actors/transfer_p2.py:542` `_reconcile_transfer_quiesce` | invoked by 1s maintenance cadence; scans at most 32 rows per pass | 10s aggregate query/receipt/hold/finish deadline per pass; durable row remains for later passes | one maintenance caller plus lifecycle, harness, and transfer authorities | unavailable/finish failure and successful completion are logged; skipped rows are log-silent |
| harness lifecycle failure admission | `src/hyprial/daemon/impl/composition/harness_ports.py:187` `fail_lifecycle` | fixed up to 0.002s after OVERLOADED admission | caller-supplied wall-clock timeout | caller thread and harness actor mailbox; a new edge correlation is minted per attempt | attempts are log-silent; terminal result is a timeout error |
| harness lifecycle close admission | `src/hyprial/daemon/impl/harnesses/actor/behaviors/lifecycle.py:347` `_close_lifecycle_in_actor` | fixed up to 0.002s after OVERLOADED admission | caller-supplied close deadline | close caller and harness lifecycle actor mailbox | attempts are log-silent; boolean result reports exhaustion |
| home claim contention wait | `src/hyprial/daemon/impl/configuration/home_guard.py:250` `_claim_record` | fixed min(0.05s, keepalive_duration) while a final non-advancing heartbeat may clear | claim_wait_timeout; advancing heartbeat or expiry fails immediately | daemon startup thread and one home marker lock | individual waits are log-silent; terminal conflict is HYPRIALHomeInUse |
| daemon state ownership lock | `src/hyprial/daemon/impl/configuration/ownership.py:25` `acquire` | fixed max(0.001s, poll_interval), 0.05s poll by default | caller timeout, 0s by default | caller thread and one daemon.lock file | attempts are log-silent; exhaustion raises DaemonOwnershipBusy |
| identity transaction lock | `src/hyprial/identity/impl/identity_transaction.py:43` `acquire` | fixed max(0.001s, poll_interval), 0.05s by default | caller timeout, 15s by default | caller thread and one home-scoped lock file | attempts are log-silent; exhaustion raises IdentityTransactionBusy |
| squire JSON mutation lock | `src/hyprial/daemon/impl/squire/store_lock.py:18` `json_store_mutation` | up to 0.01s between nonblocking lock attempts | caller timeout, 5s by default | caller thread and one per-store lock file | attempts are log-silent; exhaustion raises TimeoutError |
| user JSON mutation lock | `src/hyprial/identity/impl/users/_store_lock.py:22` `json_store_mutation` | up to 0.01s between nonblocking lock attempts | caller timeout, 5s by default | caller thread and one per-store lock file | attempts are log-silent; exhaustion raises TimeoutError |
| desired-state late-result join | `src/hyprial/daemon/impl/desired_state_io.py:236` `_join_late` | fixed 0.01s correlation lookup poll | no local deadline; ends when a result/error appears | pins the caller thread and retained desired-state correlation | polls are log-silent |
| work-item stale-write mutation retry | `src/hyprial/daemon/impl/application/messaging/work_items.py:438` `_work_mutate` | immediate reread and rewrite after stale-write conflict | 3 attempts per caller operation | one IPC caller and orgfs facade write | attempts are log-silent; terminal conflict is returned with attempt count |
| work-item stale-write repair retry | `src/hyprial/daemon/impl/application/messaging/work_items.py:723` `_work_repair` | immediate reinspection and rewrite after stale-write conflict | 3 attempts per caller operation | one IPC caller and orgfs facade write | attempts are log-silent; terminal conflict is returned with attempt count |
| remote workflow outbox replay | `src/hyprial/daemon/impl/dispatch/remote/__init__.py:158` `_pump` | fixed 1s pump cadence; up to 32 durable rows selected each pass | no attempt cap; result publication or workflow shutdown ends replay | one remote-workflow pump thread and durable SQLite outbox | database-unavailable pass logs one warning; row failures remain for later passes without per-row retry log |
| remote workflow completion admission | `src/hyprial/daemon/impl/dispatch/remote/__init__.py:653` `_remote_effect_loop` | fixed 0.01s after OVERLOADED actor admission | no attempt cap; accepted admission, CLOSED result, or workflow stop ends replay | pins the single remote effect worker; queue capacity is configured | admission retries are log-silent |
| streaming failure-observer admission | `src/hyprial/daemon/impl/harnesses/streaming/failure.py:47` `_observer_drain` | fixed 0.05s after OVERLOADED observer admission | no attempt cap; accepted/closed observer, observer stop, or callback error ends the item | one bounded observer queue and one dedicated observer thread | callback exceptions and CLOSED rejection are logged; overload retries are log-silent |
| streaming close-command admission | `src/hyprial/daemon/impl/harnesses/streaming/process.py:417` `stop` | fixed 0.005s after OVERLOADED admission | stop_timeout_seconds wall-clock deadline | stop caller and one turn-runtime mailbox | attempts are log-silent; exhaustion raises RuntimeError |
| harness process I/O completion handoff | `src/hyprial/daemon/impl/harnesses/actor/io_port.py:342` `_handoff` | 0.01s receipt waits, 0.002s admission retries, and 0.05s waits for an untranslatable success after permanent close | no elapsed cap; processed receipt or translatable permanent close ends custody | one completion thread per submitted future plus ProcessIoPort in-flight/slot limits | handoff retries are log-silent; drain exposes retained custody |
| turn completion relay admission | `src/hyprial/daemon/impl/harnesses/turn_delivery/turn/turn_runtime.py:307` `_run` | fixed 0.002s after OVERLOADED delivery | no attempt cap; accepted/closed delivery or relay close ends the item | one queue of 8 and one relay thread | admission retries are log-silent |
| turn completion relay queue admission | `src/hyprial/daemon/impl/harnesses/turn_delivery/turn/turn_runtime.py:259` `_CompletionRelay` | queue.put blocks for up to 0.05s per full-queue attempt | caller timeout, 0.25s by default | caller thread and completion relay queue of 8 | attempts are log-silent; exhaustion returns OVERLOADED |
| lifecycle unresolved forward redrive | `src/hyprial/daemon/impl/lifecycle_manager/process.py:407` `_execute` | fixed min(0.02s, completion_timeout) after an unresolved step receipt | operation deadline is 70s by default and fences new forward work; an already admitted effect remains in custody until a receipt settles | pins the single lifecycle process-manager worker and one journaled operation | deadline/failure is journaled; individual redrive sleeps are log-silent |
| lifecycle unresolved compensation redrive | `src/hyprial/daemon/impl/lifecycle_manager/process.py:563` `_compensate` | fixed min(0.02s, completion_timeout) after an unresolved compensation receipt | no separate elapsed cap after compensation starts; receipt settlement or terminal compensation result ends it | pins the single lifecycle process-manager worker and one journaled operation | terminal result is journaled; individual redrive sleeps are log-silent |
| org-context generation reassociation | `src/hyprial/daemon/impl/org/mesh.py:344` `_submit` | immediate resubmit only after CLOSED admission and observation of a newer actor generation | no admission-attempt cap; accepted or non-reassociable rejection ends it; accepted synchronous calls wait up to 60s | one org-context authority with total custody capacity 128 | resubmits are log-silent |
| orgfs lifecycle generation reassociation | `src/hyprial/daemon/impl/orgfs/composition/lifecycle.py:276` `_ask_lifecycle` | immediate resubmit only after CLOSED admission and a newer generation | no admission-attempt cap; accepted or non-reassociable rejection ends it; accepted call then waits without a deadline | one directory lifecycle actor and per-operation waiter maps | resubmits are log-silent |
| orgfs space-state generation reassociation | `src/hyprial/daemon/impl/orgfs/document/space_state.py:448` `_submit_space_state` | immediate resubmit only after CLOSED admission and a newer generation | no admission-attempt cap; accepted or non-reassociable rejection ends it; accepted call then waits without a deadline | per-space actor custody is capped at _SPACE_STATE_CAPACITY; post-commit EffectLane has its own reservation cap | resubmits are log-silent |
| orgfs checkout generation reassociation | `src/hyprial/daemon/impl/orgfs/projection/checkout_authority.py:156` `_ask` | immediate resubmit only after CLOSED admission and a newer generation | no admission-attempt cap; accepted or non-reassociable rejection ends it; accepted wait uses the caller timeout | one checkout actor with capacity 32 | resubmits are log-silent |
| orgfs blob generation reassociation | `src/hyprial/daemon/impl/orgfs/storage/blob_authority.py:411` `_call` | immediate resubmit only after CLOSED admission and a newer generation | no admission-attempt cap; accepted or non-reassociable rejection ends it; accepted wait is 60s | one blob actor; mailbox/effect capacity and waiter map bound concurrent custody | resubmits are log-silent |
| orgfs replica generation reassociation | `src/hyprial/daemon/impl/orgfs/storage/replica_authority.py:455` `_ask` | immediate resubmit only after CLOSED admission and a newer generation | no admission-attempt cap; accepted or rejection ends it; accepted call then waits without a deadline | one replica actor with total custody capacity 128 | resubmits are log-silent |
| orgfs space generation reassociation | `src/hyprial/daemon/impl/orgfs/storage/space_authority.py:640` `_ask` | immediate resubmit only after CLOSED admission and a newer generation | no admission-attempt cap; accepted or rejection ends it; accepted call then waits without a deadline | one space authority with _SPACE_AUTHORITY_CAPACITY | resubmits are log-silent |
| PAC notification durable send replay | `src/hyprial/daemon/impl/pac/graphs/authority/__init__.py:892` `_delivery_loop` | wake-driven scan with at most a 1s wait between scans; selects at most 128 rows | no attempt cap for transient send/record failures; durable delivery row or permanent failure record ends replay | one graph delivery thread over durable pending rows | permanent refusal is recorded; transient send and record failures are log-silent here |
| PAC notification completion admission | `src/hyprial/daemon/impl/pac/graphs/authority/__init__.py:892` `_delivery_loop` | fixed 0.01s after OVERLOADED actor admission | no attempt cap; accepted/CLOSED admission or authority close ends the item | pins the one graph delivery thread after a successful external send | admission retries are log-silent |
| provider-auth code-observation admission | `src/hyprial/identity/impl/provider_auth/actor/_authority.py:801` `announce` | fixed 0.01s after rejected internal admission | no attempt cap; accepted admission or provider-auth stop ends replay | pins one provider-auth helper worker callback | admission retries are log-silent |
| route settled-call replay | `src/hyprial/daemon/impl/route_registration/client.py:85` `_call` | 5ms after OVERLOADED admission or unsettled receipt; command wait uses the configured client timeout before resubmission | ordinary calls are bounded by client timeout; wait_for_settlement calls have no attempt or elapsed cap | caller thread plus shared route-registration authority and correlation router | retries are log-silent; bounded exhaustion raises typed errors |
| session undelivered admission replay | `src/hyprial/daemon/impl/session_actor/actor.py:255` `_replay_undelivered` | fixed 0.005s after rejected admission | no attempt cap; accepted admission or session drain ends each item | one replay thread per active replay run and the actor-owned undelivered deque | admission retries are log-silent |
| session generation reconciliation | `src/hyprial/daemon/impl/session_actor/actor.py:455` `_replay_when_running` | 0.005s while generation is not running or custody blocks; 0.01s after desired-state reconciliation fault | no attempt cap; successful reconciliation, newer/stopped generation, or drain ends the thread | one replay thread per actor generation plus durable effect custody | retry sleeps are log-silent; reconciliation fault is only exposed through attempted state |
| session internal completion admission | `src/hyprial/daemon/impl/session_actor/actor.py:637` `_deliver_internal` | exponential 0.001s doubling to 0.05s | 1s wall-clock deadline; CLOSED terminates immediately | caller/effect worker and session actor mailbox | attempts and exhaustion are log-silent; boolean result carries failure |
| liveliness presence reconciliation | `src/hyprial/daemon/impl/transport/zenoh/presence.py:261` `_reconcile_confirms` | up to five reads, each with 1s timeout, with 0.4s between attempts | five attempts | one liveliness rebuild callback per identity | attempt exceptions and negative reads are swallowed; aggregate rebuild result is logged |
| machine identifier source failover | `src/hyprial/identity/impl/device/machine_binding.py:138` `_machine_identifier` | immediate read of the second Linux machine-ID path after OSError or an empty first value | two Linux paths, each once; then the platform-independent host/node fallback; no elapsed deadline | one synchronous machine-fingerprint caller | source failures are log-silent; the final fallback always returns a value |
| Windows IPC response-write settlement | `src/hyprial/kernel/impl/platform/windows_pipe.py:58` `_settle_response_write` | poll_interval after ERROR_IO_INCOMPLETE | no elapsed cap; Windows terminal OVERLAPPED status ends custody | one timed-out response holder per pending Windows pipe write | polls are log-silent; terminal OS error is propagated |
| Windows IPC busy-pipe connect | `src/hyprial/kernel/impl/platform/windows_pipe.py:357` `connect` | fixed 0.01s after ERROR_PIPE_BUSY | caller-supplied wall-clock timeout | IPC client caller thread and one named pipe endpoint | attempts are log-silent; timeout is typed |
| kernel blocking file lock | `src/hyprial/kernel/impl/platform/file_lock.py:20` `lock_exclusive` | fixed 0.05s on Windows lock contention when blocking=True | no elapsed cap in the primitive; nonblocking mode fails immediately | caller thread and one file descriptor; POSIX blocking is delegated to flock | wait attempts are log-silent |
| daemon service launchd load | `src/hyprial/daemon/impl/operations/service.py:313` `_load` | five launchctl attempts with 0.25/0.5/0.75/1.0s waits after the first four transient failures | five attempts; non-transient error terminates immediately | service-management caller thread and launchd | attempts are log-silent; terminal command error includes stderr |
| autoupdate service load | `src/hyprial/daemon/impl/autoupdate/__init__.py:683` `_load` | launchd: five attempts with 0.25/0.5/0.75/1.0s waits; systemd: fixed 0.5s | launchd: five attempts; systemd: 5s wall-clock deadline; non-transient errors terminate immediately | autoupdate-management caller thread and OS service manager | attempts are log-silent; terminal command error includes stderr |
| harness lifecycle rejection replay | `src/hyprial/daemon/impl/harnesses/actor/behaviors/desired.py:536` `_continue_lifecycle_rejection` | configured lifecycle retry base with exponential growth capped at 1s, scheduled by threading.Timer | failure_budget attempts, default 3; closing or exhausted budget terminalizes | per harness lifecycle correlation and one timer entry | timer retries are log-silent; terminal lifecycle failure is published |
| orgfs IPC content-operation replay | `src/hyprial/daemon/impl/application/messaging/orgfs_bridge.py:339` `with_content_wait` | after await_content returns, sleep up to 0.25s before rerunning the requested read operation | caller waitSeconds deadline: 10s default, constrained to 0..120s | IPC request thread plus per-space blob queue and shared orgfs worker | retries are log-silent; terminal pending error reports waitedSeconds |
| Codex remote settlement retry | `src/hyprial/daemon/impl/harnesses/codex/settlement.py:275` `_defer_settlement` | settlement_retry_seconds × 2^(attempt−1), capped at 5s | no attempt cap while the delivery snapshot remains pending | per delivery snapshot and bounded carrier fact handoff | every deferral logs attempt, delay, stage, and error |
| PAC actor-effect completion admission | `src/hyprial/daemon/impl/pac/graphs/authority/__init__.py:440` `_actor_effect_loop` | fixed 0.01s after OVERLOADED actor admission | no attempt cap; accepted/CLOSED admission or authority close ends the item | pins one PAC actor-effect worker; actor-effect queue/active-key sets bound work | admission retries are log-silent |
| stale session-route lock acquisition | `src/hyprial/daemon/impl/application/actors/sessions/routes.py:627` `_expire_stale_channel_routes_owned` | fixed 0.005s when a full pass acquires no per-agent route-effect lock | no attempt or elapsed cap while any observed stale session remains pending | caller or session-route effect worker plus per-agent route locks | contention retries are log-silent |
| Zenoh startup endpoint rebind | `src/hyprial/daemon/impl/application/netendpoints/endpoints.py:196` `_open_transport` | immediate reopen after selecting a new loopback port or removing one best-effort derived listener | loopback collisions use the external-I/O restart budget, 5 by default; derived listener removal adds at most one fallback open | daemon startup caller; every open may create SDK/native resources before failure | attempts are log-silent here; the final derived-listener error is returned to the caller |
| inbox unsettled-completion replay | `src/hyprial/daemon/impl/inbox/actor/worker.py:415` `_retry_unsettled_completions` | worker idle cadence, at most once after each 0.05s queue wait | no attempt or elapsed cap; accepted admission or drain handoff ends custody | retains pending count and active recipient keys on one DeliveryIoWorker | replay attempts are log-silent |
| orgfs inline blob import retry | `src/hyprial/daemon/impl/orgfs/network/recovery.py:552` `_recover_blob` | immediate single import replay after fetching the missing digest from the same supplier | exactly one replay; repeated rejection or fetch/import exception is terminal for the receive | inbound receive caller and blob store; no deferred worker queue on this path | fetch/import exception logs blob-fetch-failed; repeated rejection uses normal receive outcome logging |
| orgfs commit-many client registration retry | `src/hyprial/daemon/impl/orgfs/storage/store/journal.py:438` `commit_many` | immediate replay after registering newly observed mutation client IDs | at most len(items)+2 attempts | caller thread, one store lock, and one SQLite transaction per attempt | attempts are log-silent; exhaustion raises StoreError |
| IPC partial response write retry | `src/hyprial/daemon/impl/ipc/server.py:53` `_write_ipc_response` | socket timeout is poll_interval; retry resumes at the unsent byte offset | caller-supplied wall-clock timeout; daemon uses 15s | one IPC response thread and connection | timeouts are log-silent; terminal zero-progress or deadline raises |
| smolvm process-group termination retry | `src/hyprial/daemon/impl/transfer/execution/smolvm.py:172` `_run` | SIGTERM then SIGKILL, with up to 3s wait after each signal | two signal attempts after timeout or interruption | one transfer execution caller and its owned process group | signal lookup failures are swallowed; timeout/interruption is retained in the result |

### Limit-class mechanism details

| limit class | file:line | lexical scope | schedule | cap | budget scope | logging | starvation evidence |
|---|---|---|---|---|---|---|---|
| limit-class:periodic-reentry | `src/hyprial/daemon/impl/service_connect/manager.py:494` | `_scheduled_reconcile` | fixed 5.0s after each completed pass; finally rearms one generation-fenced scheduler entry | manager lifetime; no attempt or aggregate pass deadline; each _map/_unmap controller wait is up to 3.0s without cancelling accepted work | one generation-fenced scheduler entry; service-control EffectLane capacity 16 with one worker; one manager RLock; no daemon-global retry budget | mapping and aggregate reconciliation transitions are fingerprint-deduplicated; successful closes log; repeated identical failures are log-silent | reconcile holds the manager lock across slow controller calls; service.list/connect/disconnect need the same lock and can block behind their cumulative wait |

### Cross-scope method-limit instances

| limit class | file:line | lexical scope | evidence |
|---|---|---|---|
| limit-class:periodic-reentry | `src/hyprial/daemon/impl/links/service/core.py:500` | `_loop` | LinkService._loop periodically calls tick after earlier passes return or fail |
| limit-class:periodic-reentry | `src/hyprial/daemon/impl/links/service/core.py:189` | `tick` | LinkService.tick returns after apply failure; LinkService._loop later calls tick again |
| limit-class:periodic-reentry | `src/hyprial/daemon/impl/links/store.py:625` | `_write_everywhere` | LinkStore._write_everywhere retains a failed mirror for the next flush pass |
| limit-class:periodic-reentry | `src/hyprial/daemon/impl/links/store.py:666` | `_resolve_stale` | LinkStore._resolve_stale retains a failed rewrite for the next flush pass |

### Mechanical retry-shape candidate dispositions (full review superset)

| file:line | lexical scope | syntax | disposition | family or dismissal reason |
|---|---|---|---|---|
| `src/hyprial/biz/impl/hooks/quota/policy.py:243` | `QuotaWatchdog._transition` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/biz/impl/hooks/quota/policy.py:253` | `QuotaWatchdog._window_text` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/biz/impl/hooks/recap/writer.py:136` | `RecapWriter._write_recaps` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=get_nowait,wait | dismissed | queue idle wait; each recap is attempted once |
| `src/hyprial/biz/impl/hooks/recap/writer.py:189` | `<module>.recent_recaps` | signals=syntax; loops=1; shape=retry-failover; operations=append,loads | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/biz/impl/routine/execution/coordinator/__init__.py:59` | `RoutineCoordinator.__init__` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/biz/impl/routine/execution/coordinator/__init__.py:159` | `RoutineCoordinator.wait` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=get,monotonic,wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/biz/impl/routine/execution/coordinator/__init__.py:228` | `RoutineCoordinator._effect_loop` | signals=syntax,legacy-loop-delay; loops=2; shape=retry; operations=tell | classified | routine coordinator completion admission |
| `src/hyprial/biz/impl/routine/execution/coordinator/__init__.py:395` | `RoutineCoordinator._recover_loop` | signals=name,identifier,legacy-identifier,syntax,legacy-loop-delay; loops=1; shape=retry; operations=_recover,tell | classified | routine recovery loop |
| `src/hyprial/biz/impl/routine/execution/coordinator/__init__.py:406` | `RoutineCoordinator.close` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/biz/impl/routine/execution/coordinator/generation.py:344` | `_Generation.__call__` | signals=identifier,legacy-identifier,syntax; loops=1; shape=observation; operations=_schedule,values | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/biz/impl/routine/execution/coordinator/generation.py:420` | `_Generation._schedule` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/biz/impl/routine/execution/coordinator/generation.py:443` | `_Generation._completed` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/biz/impl/routine/execution/coordinator/generation.py:616` | `_Generation._retain_retry` | signals=name; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/biz/impl/routine/execution/registry/core.py:100` | `_RegistryCore.__call__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/biz/impl/routine/execution/registry/core.py:393` | `_RegistryCore._recover` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/biz/impl/routine/execution/registry/events.py:130` | `_RegistryEvents._pac_completed` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/biz/impl/routine/execution/service.py:131` | `RoutineFacade.__init__` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/biz/impl/routine/execution/service.py:347` | `RoutineFacade.recover` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | reads or joins recovery state once; it does not replay a failed command |
| `src/hyprial/biz/impl/routine/execution/service.py:519` | `RoutineFacade._effect_loop` | signals=syntax,legacy-loop-delay; loops=2; shape=observation; operations=get,wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/biz/impl/routine/execution/service.py:554` | `RoutineFacade._effect_worker_loop` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | classified | routine alarm effect replay |
| `src/hyprial/biz/impl/routine/execution/service.py:581` | `RoutineFacade._refill_effects` | signals=identifier,legacy-identifier; loops=2; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/biz/impl/routine/execution/service.py:600` | `RoutineFacade._submit_completion` | signals=syntax,legacy-loop-delay; loops=1; shape=retry; operations=tell | classified | routine effect completion admission |
| `src/hyprial/biz/impl/routine/storage/store.py:451` | `RoutineStore.retry_effect` | signals=name; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/biz/impl/turn_pulse.py:19` | `<module>.signal_turn_ended` | signals=syntax; loops=1; shape=retry; operations=open | classified | turn-pulse filename collision |
| `src/hyprial/biz/impl/usage.py:627` | `UsageCache.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/biz/impl/usage.py:683` | `UsageCache._run` | signals=syntax,legacy-loop-delay; loops=1; shape=retry; operations=_on_refresh | classified | usage collection backoff |
| `src/hyprial/biz/impl/usage.py:693` | `UsageCache.refresh_once` | signals=identifier,legacy-identifier,syntax,legacy-loop-delay; loops=1; shape=observation; operations=get | classified | usage collection backoff |
| `src/hyprial/biz/impl/usage_actor.py:168` | `UsageAuthority.stop` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic,snapshot | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/adapters/lark/credentials/identities.py:134` | `<module>.sync_identities` | signals=syntax; loops=2; shape=retry-failover; operations=_drain_member_pages | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/adapters/lark/credentials/reauth.py:480` | `<module>.recovered_text` | signals=name; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/adapters/lark/credentials/reauth.py:646` | `<module>.complete` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/adapters/lark/inbound/adapter.py:144` | `LarkAdapter.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/adapters/lark/inbound/adapter.py:233` | `LarkAdapter._process_inbound` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/adapters/lark/inbound/inbound_runtime.py:155` | `_InboundEffects.submit` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/adapters/lark/inbound/inbound_runtime.py:261` | `_InboundHandler._submit` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/adapters/lark/inbound/inbound_runtime.py:301` | `LarkInboundRuntime.__init__` | signals=legacy-loop-delay; loops=0; shape=lexical; operations=- | classified | Lark inbound completion admission |
| `src/hyprial/daemon/impl/adapters/lark/inbound/inbound_runtime.py:314` | `LarkInboundRuntime.__init__.completion` | signals=syntax,legacy-loop-delay; loops=1; shape=retry; operations=tell | classified | Lark inbound completion admission |
| `src/hyprial/daemon/impl/adapters/lark/inbound/inbound_runtime.py:358` | `LarkInboundRuntime.process` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/adapters/lark/inbound/inbound_runtime.py:404` | `LarkInboundRuntime.close` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/adapters/lark/inbound/pipeline.py:110` | `_InboundPipelineMixin._with_transient_retry` | signals=name,identifier,legacy-identifier,syntax,legacy-loop-delay; loops=1; shape=retry; operations=operation | classified | Lark inbound transient retry |
| `src/hyprial/daemon/impl/adapters/lark/inbound/pipeline.py:195` | `_InboundPipelineMixin._handle_inbound` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/adapters/lark/inbound/pipeline.py:304` | `_InboundPipelineMixin._forward_inbound` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/adapters/lark/inbound/pipeline.py:451` | `_InboundPipelineMixin._submit_inbound_request` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/adapters/lark/inbound/reconcile.py:139` | `_ReconcileDeadLetterMixin.recover_message` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/adapters/lark/inbound/reconcile.py:165` | `_ReconcileDeadLetterMixin._recover_pending_submission` | signals=name; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/adapters/lark/inbound/reconcile.py:251` | `_ReconcileDeadLetterMixin.reconcile_recent` | signals=identifier,legacy-identifier,syntax; loops=3; shape=retry-failover; operations=chat_type,list_chat_messages | dismissed | bounded scan classifies distinct dead-letter rows; it does not replay a failed row in the loop |
| `src/hyprial/daemon/impl/adapters/lark/outbound/gateway.py:213` | `GatewayIoAuthority._drain_close` | signals=legacy-loop-delay; loops=1; shape=lexical; operations=- | classified | Lark gateway dependency close |
| `src/hyprial/daemon/impl/adapters/lark/outbound/gateway.py:216` | `GatewayIoAuthority._drain_close.drain` | signals=syntax,legacy-loop-delay; loops=1; shape=retry; operations=close | classified | Lark gateway dependency close |
| `src/hyprial/daemon/impl/adapters/lark/outbound/reactions.py:288` | `_ReactionEffectStore.recover` | signals=name,identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | reads or joins recovery state once; it does not replay a failed command |
| `src/hyprial/daemon/impl/adapters/lark/outbound/reactions.py:588` | `_ReactionEffectHandler.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/adapters/lark/outbound/reactions.py:603` | `_ReactionEffectHandler.__call__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/adapters/lark/outbound/reactions.py:675` | `ReactionEffectsRuntime.__init__` | signals=identifier,legacy-identifier,legacy-loop-delay; loops=0; shape=lexical; operations=- | classified | Lark reaction completion admission |
| `src/hyprial/daemon/impl/adapters/lark/outbound/reactions.py:704` | `ReactionEffectsRuntime.__init__.completion` | signals=syntax,legacy-loop-delay; loops=1; shape=retry; operations=tell | classified | Lark reaction completion admission |
| `src/hyprial/daemon/impl/adapters/lark/outbound/reactions.py:784` | `ReactionEffectsRuntime.wait_idle` | signals=syntax,legacy-loop-delay; loops=1; shape=retry; operations=_admit | dismissed | each admitted barrier is a fresh state observation; no failed effect is replayed |
| `src/hyprial/daemon/impl/adapters/lark/outbound/sdk.py:63` | `LarkSdkGateway.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/adapters/lark/outbound/sdk.py:109` | `LarkSdkGateway.set_permission_recovery` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/adapters/lark/outbound/sdk.py:126` | `LarkSdkGateway._require_success` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/adapters/lark/outbound/sdk_base.py:81` | `LarkApiError.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/adapters/lark/outbound/stream.py:38` | `LarkEventStream.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/adapters/lark/outbound/stream.py:90` | `LarkEventStream._default_factory` | signals=legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/adapters/lark/outbound/stream.py:114` | `LarkEventStream._default_factory.ReadyClient._connect` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/adapters/lark/runtime/effects.py:639` | `_ProcessEffects.close` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic,wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/adapters/lark/runtime/handler.py:54` | `_AdapterHandler.__init__` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/adapters/lark/runtime/handler.py:145` | `_AdapterHandler._start` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/adapters/lark/runtime/handler.py:195` | `_AdapterHandler._remove` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/adapters/lark/runtime/handler.py:220` | `_AdapterHandler._reload` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/adapters/lark/runtime/handler.py:257` | `_AdapterHandler._restore` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/adapters/lark/runtime/handler.py:574` | `_AdapterHandler._complete_aggregate` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/adapters/lark/runtime/handler.py:619` | `_AdapterHandler._begin_reconcile_spawns` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | classified | Lark adapter process restart |
| `src/hyprial/daemon/impl/adapters/lark/runtime/handler.py:672` | `_AdapterHandler._finish_aggregate` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/adapters/lark/runtime/handler.py:691` | `_AdapterHandler._record_failure` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | records restart state; the classified harness policy owns replay |
| `src/hyprial/daemon/impl/adapters/lark/runtime/health.py:141` | `_HealthReportDispatcher._run` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/adapters/lark/runtime/health.py:182` | `<module>.required_reconcile_lookback` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/adapters/lark/runtime/runtime.py:139` | `AdapterRuntime.__init__` | signals=legacy-identifier,legacy-loop-delay; loops=0; shape=lexical; operations=- | classified | Lark adapter completion admission |
| `src/hyprial/daemon/impl/adapters/lark/runtime/runtime.py:170` | `AdapterRuntime.__init__.completion` | signals=syntax,legacy-loop-delay; loops=1; shape=retry; operations=tell | classified | Lark adapter completion admission |
| `src/hyprial/daemon/impl/adapters/lark/runtime/runtime.py:204` | `AdapterRuntime.__init__.handler_factory` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/adapters/lark/runtime/runtime.py:336` | `AdapterRuntime.submit` | signals=legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/adapters/lark/runtime/statuses.py:42` | `<module>._build_statuses` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/adapters/lark/runtime/statuses.py:181` | `_Authority.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/adapters/lark/state/schema.py:72` | `_LarkStateSchema._switch_to_wal` | signals=syntax,legacy-loop-delay; loops=1; shape=retry; operations=execute | classified | Lark state WAL transition |
| `src/hyprial/daemon/impl/adapters/lark/worker/entry.py:106` | `<module>._recovery_timing_from_environment` | signals=name; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/adapters/lark/worker/entry.py:148` | `<module>.main` | signals=identifier,legacy-identifier,legacy-loop-delay; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/adapters/lark/worker/entry.py:434` | `main.ready` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/adapters/lark/worker/entry.py:478` | `main.reconnect` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/adapters/lark/worker/entry.py:497` | `main.watch_health` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=check_once,wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/adapters/lark/worker/process.py:78` | `<module>._read_ready` | signals=syntax; loops=2; shape=observation; operations=_wait_readable,monotonic,read | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/adapters/lark/worker/process.py:260` | `LarkWorkerProcess._read_health_stream` | signals=syntax; loops=2; shape=observation; operations=_wait_readable,read | dismissed | stream loop parses new frames; malformed frames are discarded, not replayed |
| `src/hyprial/daemon/impl/adapters/lark/worker/sweep.py:18` | `<module>._reconcile_health_result` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/adapters/lark/worker/sweep.py:98` | `ReconnectSweepCoordinator._run` | signals=syntax; loops=1; shape=retry; operations=_reconcile_health_result,history_reconcile_started | dismissed | a coalesced pending reconnect requests one fresh sweep after success; failures terminate |
| `src/hyprial/daemon/impl/application/__init__.py:324` | `DaemonApplication.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/application/actors/agents_admin/destroys.py:178` | `_AgentDestroyMixin._terminate_interactive_carrier` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=_owner_process_status,monotonic | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/application/actors/agents_admin/destroys.py:428` | `_AgentDestroyMixin._remove_agent_routines` | signals=syntax,legacy-loop-delay; loops=1; shape=retry; operations=begin_remove | dismissed | waits for accepted routine removals; it does not resubmit them |
| `src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py:57` | `_AgentRegistryOpsMixin._handle_agent` | signals=syntax; loops=2; shape=retry-failover; operations=home_receipt | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/application/actors/agents_registry.py:382` | `_AgentsRegistryMixin._seed_agent_bindings` | signals=identifier,legacy-identifier; loops=2; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/application/actors/agents_registry.py:778` | `_AgentsRegistryMixin._agent_status_json` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/application/actors/routines.py:304` | `_RoutinesMixin._remove_registered_routine` | signals=legacy-loop-delay; loops=1; shape=lexical; operations=- | dismissed | joins one accepted routine operation; it does not resubmit it |
| `src/hyprial/daemon/impl/application/actors/routines.py:357` | `_RoutinesMixin._reconcile_routine_coordinators` | signals=syntax; loops=1; shape=retry-failover; operations=_ensure_routine_coordinator | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/application/actors/routines.py:396` | `_RoutinesMixin._ensure_routine_coordinator` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/application/actors/routines.py:609` | `_RoutinesMixin._ipc_routine_remove` | signals=legacy-loop-delay; loops=1; shape=lexical; operations=- | dismissed | joins one accepted routine operation; it does not resubmit it |
| `src/hyprial/daemon/impl/application/actors/sessions/routes.py:543` | `_SessionRoutesMixin._clean_dead_interactive_sessions` | signals=identifier,legacy-identifier; loops=2; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/application/actors/sessions/routes.py:627` | `_SessionRoutesMixin._expire_stale_channel_routes_owned` | signals=syntax,legacy-loop-delay; loops=2; shape=observation; operations=_accept_interactive_route_observation_locked,_close_interactive_route_effect,_try_interactive_route_effect,append,sleep | classified | stale session-route lock acquisition |
| `src/hyprial/daemon/impl/application/actors/sessions/routes.py:776` | `_SessionRoutesMixin._settled_route_mutation` | signals=identifier,legacy-identifier,syntax,legacy-loop-delay; loops=1; shape=retry; operations=call_settled | classified | session route mutation retry |
| `src/hyprial/daemon/impl/application/actors/transfer_p2.py:542` | `_TransferP2Mixin._reconcile_transfer_quiesce` | signals=identifier,legacy-identifier,syntax,legacy-loop-delay; loops=1; shape=cadence; operations=- | classified | transfer quiesce reconciliation |
| `src/hyprial/daemon/impl/application/actors/transfer_p2.py:603` | `_TransferP2Mixin._retry_transfer_restore` | signals=name,identifier,legacy-identifier,syntax,legacy-loop-delay; loops=1; shape=cadence; operations=- | classified | transfer restore observation |
| `src/hyprial/daemon/impl/application/messaging/delivery/lark.py:107` | `_LarkGatewayMixin._route_lark_gateway` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/application/messaging/delivery/lark.py:152` | `_LarkGatewayMixin._lark_gateway_with_scope_recovery` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/application/messaging/delivery/sends.py:521` | `_UserDeliveryMixin._deliver_route_target` | signals=syntax; loops=2; shape=retry-failover; operations=send_chat,send_chat_file,send_chat_image | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/application/messaging/delivery/sends.py:684` | `_UserDeliveryMixin._deliver_autoupdate_restart_notification` | signals=name; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/application/messaging/identity_binding.py:34` | `_BindingPublicationRetry.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/application/messaging/identity_binding.py:219` | `_IdentityBindingMixin._retry_identity_binding_publication` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | classified | identity binding publication retry |
| `src/hyprial/daemon/impl/application/messaging/identity_binding.py:398` | `_IdentityBindingMixin._publish_identity_binding_locked` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/application/messaging/inbox_surface/queries.py:305` | `_MessageQueriesMixin._ipc_message_pending_wait` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic,wait_for_version | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/application/messaging/maintenance.py:160` | `_MaintenanceMixin._scheduled_maintenance` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/application/messaging/maintenance.py:379` | `_MaintenanceMixin._record_maintenance_outcome` | signals=identifier,legacy-identifier; loops=3; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/application/messaging/maintenance.py:598` | `_MaintenanceMixin._run_scheduled_domains` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/application/messaging/operator_alarm.py:129` | `OperatorAlarmDelivery._message` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/application/messaging/operator_alarm.py:212` | `<module>._restart_action` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/application/messaging/operator_alarm.py:224` | `<module>._restart_command` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/application/messaging/orgfs_bridge.py:304` | `_OrgFsBridgeMixin._handle_orgfs` | signals=legacy-loop-delay; loops=0; shape=lexical; operations=- | classified | orgfs IPC content-operation replay |
| `src/hyprial/daemon/impl/application/messaging/orgfs_bridge.py:339` | `_OrgFsBridgeMixin._handle_orgfs.with_content_wait` | signals=syntax,legacy-loop-delay; loops=1; shape=retry; operations=operation | classified | orgfs IPC content-operation replay |
| `src/hyprial/daemon/impl/application/messaging/status/views.py:756` | `_StatusViewsMixin._ipc_targets` | signals=syntax; loops=4; shape=retry-failover; operations=profile_open_id | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/application/messaging/work_items.py:182` | `_WorkItemsMixin._work_inspect_history` | signals=syntax; loops=4; shape=retry-failover; operations=parse_work_item | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/application/messaging/work_items.py:438` | `_WorkItemsMixin._work_mutate` | signals=identifier,legacy-identifier,syntax; loops=1; shape=retry; operations=write_text | classified | work-item stale-write mutation retry |
| `src/hyprial/daemon/impl/application/messaging/work_items.py:723` | `_WorkItemsMixin._work_repair` | signals=identifier,legacy-identifier,syntax; loops=1; shape=retry; operations=write_text | classified | work-item stale-write repair retry |
| `src/hyprial/daemon/impl/application/netendpoints/endpoints.py:196` | `<module>._open_transport` | signals=identifier,legacy-identifier,syntax; loops=1; shape=retry; operations=close,zenohtransport | classified | Zenoh startup endpoint rebind |
| `src/hyprial/daemon/impl/application/netendpoints/services.py:58` | `_ServiceScheduler._run` | signals=syntax,legacy-loop-delay; loops=2; shape=observation; operations=get,monotonic,wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/application/ports.py:218` | `RoutineServicePort.recover` | signals=name; loops=0; shape=lexical; operations=- | dismissed | reads or joins recovery state once; it does not replay a failed command |
| `src/hyprial/daemon/impl/application/restore.py:179` | `_RestoreMixin._retry_restore_eligibility` | signals=name; loops=1; shape=lexical; operations=- | classified | harness restore eligibility replay |
| `src/hyprial/daemon/impl/application/restore.py:516` | `_RestoreMixin._restore_wake_scan` | signals=identifier,legacy-identifier; loops=3; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/application/startup.py:537` | `_StartupMixin.configure_user_adapters` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/application/wiring/services.py:22` | `_WiringServicesMixin._startup_adopt_domain_services` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/application/wiring/services.py:422` | `_WiringServicesMixin._startup_finalize_runtime` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/application/wiring/transport.py:788` | `_WiringTransportMixin._startup_compose_runtime_bridge` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/application/workflows.py:55` | `_WorkflowSurfaceMixin._ipc_workflow_worker_control` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/autoupdate/__init__.py:683` | `AutoUpdateManager._load` | signals=syntax,legacy-loop-delay; loops=2; shape=observation; operations=_raise_command,monotonic,range,runner,sleep | classified | autoupdate service load |
| `src/hyprial/daemon/impl/autoupdate/actor.py:357` | `AutoUpdateAuthority.stop` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic,snapshot | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/autoupdate/alert.py:187` | `<module>.latest_start_failure` | signals=syntax; loops=1; shape=retry-failover; operations=loads | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/autoupdate/alert.py:242` | `<module>.restart_failure_detail` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/autoupdate/alert.py:492` | `<module>.notify_restore_followup` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/autoupdate/alert.py:714` | `<module>.notify_upgrade_outcome` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/composition/events.py:108` | `CorrelatedDomainEvents.wait` | signals=syntax,legacy-loop-delay; loops=2; shape=observation; operations=get,monotonic,wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/composition/events.py:134` | `CorrelatedDomainEvents.wait_settled` | signals=syntax,legacy-loop-delay; loops=2; shape=observation; operations=get,wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/composition/harness_ports.py:187` | `HarnessPortClient.fail_lifecycle` | signals=syntax,legacy-loop-delay; loops=1; shape=retry; operations=submit | classified | harness lifecycle failure admission |
| `src/hyprial/daemon/impl/composition/harness_ports.py:242` | `HarnessPortClient.reconcile` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/composition/link_spaces/spaces.py:73` | `OrgLinkSpaces._resolve` | signals=syntax; loops=1; shape=retry-failover; operations=org_state_details | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/configuration/home_guard.py:250` | `ActiveDaemonHeartbeat._claim_record` | signals=syntax,legacy-loop-delay; loops=1; shape=retry; operations=_write_record | classified | home claim contention wait |
| `src/hyprial/daemon/impl/configuration/home_guard.py:322` | `ActiveDaemonHeartbeat._heartbeat_loop` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/configuration/ownership.py:25` | `DaemonStateOwnershipFence.acquire` | signals=syntax,legacy-loop-delay; loops=1; shape=retry; operations=lock_exclusive | classified | daemon state ownership lock |
| `src/hyprial/daemon/impl/context_snapshot/native.py:203` | `<module>.analyze_native_session` | signals=syntax; loops=1; shape=retry-failover; operations=loads | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/context_snapshot/recap.py:41` | `<module>.analyze_recaps` | signals=syntax; loops=2; shape=retry-failover; operations=loads | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/desired_state_io.py:100` | `DesiredStateIoPort.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/desired_state_io.py:236` | `DesiredStateIoPort._join_late` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=_late_result,sleep | classified | desired-state late-result join |
| `src/hyprial/daemon/impl/dispatch/matrix.py:309` | `<module>.diagnose` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/dispatch/remote/__init__.py:158` | `RemoteWorkflow._pump` | signals=syntax,legacy-loop-delay; loops=2; shape=retry-failover; operations=_flush,execute | classified | remote workflow outbox replay |
| `src/hyprial/daemon/impl/dispatch/remote/__init__.py:653` | `RemoteWorkflow._remote_effect_loop` | signals=syntax,legacy-loop-delay; loops=2; shape=retry; operations=tell | classified | remote workflow completion admission |
| `src/hyprial/daemon/impl/dispatch/runtime/holds.py:137` | `HoldRefreshCoordinator.close` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/dispatch/runtime/offers.py:99` | `DispatchOfferCoordinator.offer` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | waits for one correlated dispatch decision; it does not replay the offer |
| `src/hyprial/daemon/impl/dispatch/runtime/offers.py:138` | `DispatchOfferCoordinator._execute` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/dispatch/runtime/offers.py:223` | `DispatchOfferCoordinator.drain` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/dispatch/runtime/progress.py:149` | `ProgressPublishCoordinator.close` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/forwarding/controller.py:298` | `ForwardingSidecarController._await_response` | signals=syntax,legacy-loop-delay; loops=6; shape=observation; operations=get,monotonic,wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/forwarding/controller.py:746` | `ForwardingSidecarController._terminate_owned_process` | signals=legacy-loop-delay; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/forwarding/store.py:27` | `ExposureStore._read` | signals=syntax; loops=1; shape=retry-failover; operations=_normalize_exposure | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/forwarding/supervisor.py:149` | `ForwardingSupervisorState._apply` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/forwarding/supervisor.py:329` | `ForwardingSidecarSupervisor.state` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/forwarding/supervisor.py:454` | `ForwardingSidecarSupervisor.restart` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/forwarding/supervisor.py:598` | `ForwardingSidecarSupervisor._note_status_failure` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/forwarding/supervisor.py:839` | `ForwardingSidecarSupervisor._account_for_failure` | signals=identifier,legacy-identifier,legacy-loop-delay; loops=1; shape=lexical; operations=- | classified | forwarding sidecar supervision |
| `src/hyprial/daemon/impl/harnesses/actor/actor.py:104` | `HarnessRuntimeActor.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/actor/behaviors/delivery.py:204` | `_DeliveryBehavior._scan_results` | signals=syntax; loops=1; shape=retry-failover; operations=drain_results | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/harnesses/actor/behaviors/delivery.py:447` | `_DeliveryBehavior._on_stop_all` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/actor/behaviors/delivery.py:521` | `_DeliveryBehavior._on_start_completed` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/actor/behaviors/delivery.py:754` | `_DeliveryBehavior._dispatch_call_completion` | signals=identifier,legacy-identifier; loops=4; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/actor/behaviors/desired.py:486` | `_DesiredBehavior._handle_desired_lifecycle_finalize` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/actor/behaviors/desired.py:536` | `_DesiredBehavior._continue_lifecycle_rejection` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | classified | harness lifecycle rejection replay |
| `src/hyprial/daemon/impl/harnesses/actor/behaviors/desired.py:567` | `_DesiredBehavior._continue_lifecycle_rejection.retry` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | classified | harness lifecycle rejection replay |
| `src/hyprial/daemon/impl/harnesses/actor/behaviors/lifecycle.py:299` | `_LifecycleBehavior.close_runtime` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/actor/behaviors/lifecycle.py:347` | `_LifecycleBehavior._close_lifecycle_in_actor` | signals=syntax,legacy-loop-delay; loops=1; shape=retry; operations=_admit | classified | harness lifecycle close admission |
| `src/hyprial/daemon/impl/harnesses/actor/behaviors/lifecycle.py:564` | `_LifecycleBehavior._on_ensure` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/actor/behaviors/process_events.py:148` | `_ProcessEventsBehavior._record_failure` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | records restart state; the classified harness policy owns replay |
| `src/hyprial/daemon/impl/harnesses/actor/behaviors/process_events.py:201` | `_ProcessEventsBehavior._finish_start_attempt` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/actor/behaviors/process_events.py:549` | `_ProcessEventsBehavior._finish_failed_removal` | signals=identifier,legacy-identifier; loops=2; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/actor/behaviors/process_events.py:621` | `_ProcessEventsBehavior._finish_failed_ensure` | signals=identifier,legacy-identifier; loops=2; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/actor/behaviors/process_events.py:701` | `_ProcessEventsBehavior._drive_ensure_failure_fence` | signals=identifier,legacy-identifier,syntax; loops=3; shape=retry; operations=_cancel_start_timer | dismissed | loops visit distinct start attempts and timers; cancellation is not retried after failure |
| `src/hyprial/daemon/impl/harnesses/actor/behaviors/reconcile.py:38` | `_ReconcileBehavior._on_reconcile` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | classified | harness process reconcile/restart |
| `src/hyprial/daemon/impl/harnesses/actor/facade.py:129` | `HarnessRuntimeFacade.recover_restore` | signals=name; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/actor/facade.py:138` | `HarnessRuntimeFacade.reconcile` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/actor/io_port.py:246` | `ProcessIoPort.close` | signals=syntax,legacy-loop-delay; loops=2; shape=observation; operations=monotonic,wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/harnesses/actor/io_port.py:342` | `ProcessIoPort._handoff` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=wait | classified | harness process I/O completion handoff |
| `src/hyprial/daemon/impl/harnesses/agent_sdk/__init__.py:726` | `ClaudeAgentSdkProcess.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/agent_sdk/_agent_sdk_worker.py:505` | `<module>._serve` | signals=syntax,legacy-loop-delay; loops=2; shape=observation; operations=_receive_result,get,wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/harnesses/carrier/codex_carrier_support.py:72` | `TurnReportRelay.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/carrier/codex_carrier_support.py:103` | `TurnReportRelay.flush` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | classified | Codex carrier fact retry |
| `src/hyprial/daemon/impl/harnesses/carrier/contract.py:157` | `<module>.settle_injected` | signals=syntax; loops=1; shape=retry-failover; operations=settle_hint | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/harnesses/carrier/interactive_carrier_runtime.py:171` | `_CarrierShard.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/carrier/interactive_carrier_runtime.py:379` | `_CarrierShard._recover` | signals=name; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/carrier/interactive_carrier_runtime.py:526` | `InteractiveCarrierRuntime.submit` | signals=syntax,legacy-loop-delay; loops=1; shape=retry; operations=tell | classified | interactive carrier command admission |
| `src/hyprial/daemon/impl/harnesses/codex/app_server.py:221` | `CodexInteractiveAppServer.start` | signals=syntax,legacy-loop-delay; loops=1; shape=retry; operations=_validate_codex_native_load,request | classified | Codex app-server startup connection |
| `src/hyprial/daemon/impl/harnesses/codex/app_server.py:332` | `CodexInteractiveAppServer.discover_thread` | signals=syntax; loops=1; shape=retry-failover; operations=request | classified | Codex loaded-thread failover discovery |
| `src/hyprial/daemon/impl/harnesses/codex/carrier.py:131` | `CodexInteractiveTurnClient.receive_response` | signals=syntax,legacy-loop-delay; loops=1; shape=retry; operations=read_turn | classified | Codex turn-state RPC read retry |
| `src/hyprial/daemon/impl/harnesses/codex/carrier.py:184` | `CodexInteractiveCarrier.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/codex/carrier.py:305` | `CodexInteractiveCarrier.stop` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/codex/carrier.py:325` | `CodexInteractiveCarrier._poll_main` | signals=syntax,legacy-loop-delay; loops=1; shape=retry; operations=_poll_once | classified | Codex carrier poll-pass retry |
| `src/hyprial/daemon/impl/harnesses/codex/carrier.py:552` | `CodexInteractiveCarrier._apply_inbox_list` | signals=identifier,legacy-identifier,syntax; loops=2; shape=retry-failover; operations=_stage_carrier_fetched,get | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/harnesses/codex/client.py:667` | `CodexAppServerClient._turn_events` | signals=syntax; loops=1; shape=observation; operations=_read_turn,get,get_running_loop,time,wait_for | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/harnesses/codex/client.py:896` | `CodexAppServerProcess.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/codex/native_env.py:729` | `<module>._git_metadata_roots` | signals=syntax; loops=2; shape=retry-failover; operations=run | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/harnesses/codex/process.py:581` | `_ClientCloseMixin._close_host_process` | signals=legacy-loop-delay; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/codex/process.py:685` | `_ClientCloseMixin._wait_for_process_group_exit` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=exists,get_running_loop,time | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/harnesses/codex/settlement.py:275` | `_CarrierSettlementMixin._defer_settlement` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | classified | Codex remote settlement retry |
| `src/hyprial/daemon/impl/harnesses/codex/settlement.py:386` | `_CarrierSettlementMixin._retry_carrier_facts` | signals=name; loops=1; shape=lexical; operations=- | classified | Codex carrier admission replay |
| `src/hyprial/daemon/impl/harnesses/dsh/api.py:390` | `DshHttpApi._connect_addresses` | signals=syntax; loops=1; shape=retry-failover; operations=connect | classified | DSH resolved-address connection failover |
| `src/hyprial/daemon/impl/harnesses/dsh/client.py:310` | `DshApiClient.receive_response` | signals=syntax,legacy-loop-delay; loops=2; shape=observation; operations=get | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/harnesses/dsh/process.py:384` | `DshHarnessProcess._read_banner` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic,poll,wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/harnesses/dsh/process.py:540` | `DshHarnessProcess._reap` | signals=legacy-loop-delay; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/dsh_tui/carrier.py:174` | `DshPluginWakeDriver.wait_ready` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=get,monotonic | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/harnesses/dsh_tui/carrier.py:284` | `<module>.run_dsh_carrier` | signals=legacy-loop-delay; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/dsh_tui/carrier.py:352` | `run_dsh_carrier.wait_for_stop` | signals=syntax,legacy-loop-delay; loops=2; shape=observation; operations=is_set,sleep | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/harnesses/dsh_tui/carrier.py:366` | `run_dsh_carrier.watch_injection_edge` | signals=syntax,legacy-loop-delay; loops=2; shape=observation; operations=get,monotonic,sleep | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/harnesses/dsh_tui/carrier.py:395` | `run_dsh_carrier.watch_tui_process` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=cancel,run_sync,sleep | dismissed | periodic process-state observation; run_sync does not repeat failed work |
| `src/hyprial/daemon/impl/harnesses/pi/rpc_client.py:566` | `PiRpcClient._receive_response_locked` | signals=identifier,legacy-identifier; loops=2; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/pi/rpc_process.py:32` | `PiRpcProcess.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/protocol/common.py:356` | `PtyHarnessProcess.stop` | signals=legacy-loop-delay; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/python_worker/__init__.py:371` | `PythonWorkerTurnAdapter.stop` | signals=legacy-loop-delay; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/python_worker/__init__.py:658` | `PythonWorkerTurnAdapter._handle_terminal` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/python_worker/_exec_env_socket.py:41` | `<module>.main` | signals=identifier,legacy-identifier,syntax,legacy-loop-delay; loops=6; shape=observation; operations=monotonic,poll,wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/harnesses/python_worker/_python_worker.py:67` | `WorkerError.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/python_worker/_python_worker.py:379` | `<module>._error_frame` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/python_worker/_python_worker.py:403` | `<module>._run_call` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/python_worker/_python_worker.py:480` | `<module>.run` | signals=identifier,legacy-identifier; loops=2; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/runtime/_launch_cleanup.py:13` | `<module>.cleanup_launch_resources` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/runtime/tmux.py:164` | `<module>.new_detached_session` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/smolvm_runtime/__init__.py:141` | `<module>.reap_lock_husks` | signals=syntax; loops=1; shape=retry-failover; operations=iterdir,lstat,rmdir | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/harnesses/smolvm_runtime/__init__.py:327` | `SmolvmWorkerRuntime._clean_engine` | signals=syntax; loops=3; shape=retry-failover; operations=kill | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/harnesses/streaming/failure.py:47` | `_SequentialFailureMixin._observer_drain` | signals=syntax,legacy-loop-delay; loops=2; shape=observation; operations=_log_observer_event,get,observer,sleep,type | classified | streaming failure-observer admission |
| `src/hyprial/daemon/impl/harnesses/streaming/failure.py:156` | `_SequentialFailureMixin._turn_failure_decision` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/streaming/failure.py:174` | `_SequentialFailureMixin._report_turn_failure` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | bounded wait for one actor decision; it does not resubmit failed work |
| `src/hyprial/daemon/impl/harnesses/streaming/process.py:70` | `SequentialTurnProcess.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/streaming/process.py:417` | `SequentialTurnProcess.stop` | signals=syntax,legacy-loop-delay; loops=1; shape=retry; operations=submit | classified | streaming close-command admission |
| `src/hyprial/daemon/impl/harnesses/streaming/process.py:466` | `SequentialTurnProcess._run` | signals=identifier,legacy-identifier,syntax,legacy-loop-delay; loops=2; shape=observation; operations=_receive_result,get,time_ns | classified | streaming harness reconnect |
| `src/hyprial/daemon/impl/harnesses/streaming/process.py:868` | `SequentialTurnProcess._backoff_delay` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | classified | streaming harness reconnect |
| `src/hyprial/daemon/impl/harnesses/turn_delivery/channel/worker_relay.py:116` | `BoundWorkerRelay._accept` | signals=syntax; loops=1; shape=retry; operations=start | dismissed | accept loop starts one handler for each distinct connection; failed accepts terminate or time out |
| `src/hyprial/daemon/impl/harnesses/turn_delivery/service.py:58` | `InboxTurnHookInvoker.invoke` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic | dismissed | polls for one hook reply until its deadline; it does not resubmit the hook |
| `src/hyprial/daemon/impl/harnesses/turn_delivery/turn/turn_runtime.py:275` | `_CompletionRelay.submit` | signals=syntax; loops=1; shape=retry; operations=max,min,monotonic,put | classified | turn completion relay queue admission |
| `src/hyprial/daemon/impl/harnesses/turn_delivery/turn/turn_runtime.py:294` | `_CompletionRelay.close` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic | classified | turn completion relay queue admission |
| `src/hyprial/daemon/impl/harnesses/turn_delivery/turn/turn_runtime.py:307` | `_CompletionRelay._run` | signals=syntax,legacy-loop-delay; loops=2; shape=retry; operations=_deliver | classified | turn completion relay admission, turn completion relay queue admission |
| `src/hyprial/daemon/impl/harnesses/turn_delivery/turn/turn_runtime.py:325` | `_TurnShard.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/turn_delivery/turn/turn_runtime.py:357` | `_TurnShard.__call__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/turn_delivery/turn/turn_runtime.py:382` | `_TurnShard._enqueue` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/turn_delivery/turn/turn_runtime.py:546` | `_TurnShard._io_failed` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | classified | turn delivery retry |
| `src/hyprial/daemon/impl/harnesses/turn_delivery/turn/turn_runtime.py:639` | `_TurnShard._recovery_ready` | signals=name,identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/turn_delivery/turn/turn_runtime.py:689` | `TurnRuntime.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/turn_delivery/turn/turn_runtime.py:839` | `TurnRuntime.fail_io` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/turn_delivery/turn/turn_runtime.py:885` | `TurnRuntime._on_actor_event` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/turn_delivery/turn/turn_runtime.py:906` | `TurnRuntime._on_actor_event.ready` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/harnesses/turn_delivery/turn/turn_runtime.py:918` | `TurnRuntime._release_recovery_if_ready` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/identity/bindings.py:470` | `OrgBindingCacheProjector.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/identity/bindings.py:527` | `OrgBindingCacheProjector.schedule_publish` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/identity/bindings.py:543` | `OrgBindingCacheProjector.retry_publish` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | classified | identity binding publication retry |
| `src/hyprial/daemon/impl/identity/bindings.py:565` | `OrgBindingCacheProjector._run` | signals=identifier,legacy-identifier,syntax,legacy-loop-delay; loops=1; shape=retry; operations=retry_publish | classified | identity binding cache rebuild |
| `src/hyprial/daemon/impl/inbox/actor/coordinator.py:49` | `DeliveryCustodyCoordinator.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/inbox/actor/coordinator.py:95` | `DeliveryCustodyCoordinator.__init__.factory` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/inbox/actor/coordinator.py:310` | `DeliveryCustodyCoordinator._wait_state_idle` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic,snapshot | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/inbox/actor/coordinator.py:319` | `DeliveryCustodyCoordinator._on_io_completion` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | records completion state and due time; the inbox worker owns replay |
| `src/hyprial/daemon/impl/inbox/actor/coordinator.py:365` | `DeliveryCustodyCoordinator._record_durable_handoff` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/inbox/actor/custody/lifecycle.py:69` | `DeliveryCustodyLifecycleMixin.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/inbox/actor/custody/lifecycle.py:129` | `DeliveryCustodyLifecycleMixin._handle` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/inbox/actor/custody/lifecycle.py:478` | `DeliveryCustodyLifecycleMixin._defer_outbox_locked` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/inbox/actor/custody/outcomes.py:198` | `DeliveryCustodyOutcomesMixin._defer_custody_after_failure` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/inbox/actor/custody/outcomes.py:246` | `DeliveryCustodyOutcomesMixin._publish_final` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/inbox/actor/custody/retry.py:77` | `DeliveryCustodyRetryMixin._claim_retry` | signals=name,identifier,legacy-identifier; loops=1; shape=lexical; operations=- | classified | durable inbox delivery retry |
| `src/hyprial/daemon/impl/inbox/actor/custody/retry.py:174` | `DeliveryCustodyRetryMixin._claim_custody_retry` | signals=name,identifier,legacy-identifier; loops=1; shape=lexical; operations=- | classified | durable inbox delivery retry |
| `src/hyprial/daemon/impl/inbox/actor/custody/retry.py:456` | `DeliveryCustodyRetryMixin._apply_inflight_wakes` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | classified | durable inbox delivery retry |
| `src/hyprial/daemon/impl/inbox/actor/internal/projection.py:32` | `_SystemReply.wait` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic,wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/inbox/actor/internal/receipts.py:30` | `_CompletionReceipts.claim` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/inbox/actor/internal/receipts.py:65` | `_CompletionReceipts.wait` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=get,monotonic,wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/inbox/actor/internal/state.py:38` | `_ActorOwnedInboxService.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/inbox/actor/worker.py:40` | `DeliveryIoWorker.__init__` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/inbox/actor/worker.py:94` | `DeliveryIoWorker.drain` | signals=identifier,legacy-identifier,syntax,legacy-loop-delay; loops=2; shape=observation; operations=monotonic,wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/inbox/actor/worker.py:118` | `DeliveryIoWorker._take_ready` | signals=legacy-loop-delay; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/inbox/actor/worker.py:133` | `DeliveryIoWorker._run` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/inbox/actor/worker.py:166` | `DeliveryIoWorker._execute` | signals=syntax; loops=1; shape=retry-failover; operations=deliver_notice | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/inbox/actor/worker.py:252` | `DeliveryIoWorker._dispatch_item` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/inbox/actor/worker.py:371` | `DeliveryIoWorker._settle_completion` | signals=identifier,legacy-identifier,syntax,legacy-loop-delay; loops=1; shape=retry; operations=_completion_sink | classified | inbox effect completion settlement |
| `src/hyprial/daemon/impl/inbox/actor/worker.py:400` | `DeliveryIoWorker._retry_unsettled_handoffs` | signals=name; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/inbox/actor/worker.py:415` | `DeliveryIoWorker._retry_unsettled_completions` | signals=name; loops=1; shape=lexical; operations=- | classified | inbox unsettled-completion replay |
| `src/hyprial/daemon/impl/inbox/authority/facade.py:129` | `DeliveryCustodyFacade.submit` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/inbox/authority/facade.py:260` | `DeliveryCustodyFacade.settle_harness_failure` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/inbox/authority/facade.py:327` | `DeliveryCustodyFacade.retry_due` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | classified | durable inbox delivery retry |
| `src/hyprial/daemon/impl/inbox/authority/facade.py:350` | `DeliveryCustodyFacade.retry_custody_due` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | classified | durable inbox delivery retry |
| `src/hyprial/daemon/impl/inbox/authority/facade.py:655` | `DeliveryCustodyFacade.next_retry_due_ms` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/inbox/authority/read.py:497` | `InboxReadProjection.next_retry_due_ms` | signals=name; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/inbox/contracts/api.py:286` | `InboxPort.retry_due` | signals=name; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/inbox/contracts/api.py:288` | `InboxPort.next_retry_due_ms` | signals=name; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/inbox/links/io.py:129` | `CorrelatedInboxEventRouter.wait` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic,wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/inbox/links/wake.py:174` | `RecipientWakeCoordinator._run` | signals=identifier,legacy-identifier,syntax,legacy-loop-delay; loops=1; shape=cadence; operations=- | dismissed | loop/cadence without an operation repeated after delay or failure |
| `src/hyprial/daemon/impl/inbox/links/wake.py:203` | `RecipientWakeCoordinator._settle` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | classified | recipient wake custody |
| `src/hyprial/daemon/impl/inbox/links/wake.py:287` | `RecipientWakeCoordinator._recover_overflow` | signals=name; loops=1; shape=lexical; operations=- | classified | recipient wake custody |
| `src/hyprial/daemon/impl/inbox/links/wake.py:352` | `RecipientWakeCoordinator._wait_retry` | signals=name; loops=0; shape=lexical; operations=- | classified | recipient wake custody |
| `src/hyprial/daemon/impl/inbox/links/wake.py:356` | `RecipientWakeCoordinator.close` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic,wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/inbox/service/core.py:46` | `InboxServiceCoreMixin.__init__` | signals=identifier,legacy-identifier,syntax; loops=1; shape=retry; operations=len | dismissed | constructor validates distinct policy entries; no operation is replayed |
| `src/hyprial/daemon/impl/inbox/service/core.py:612` | `InboxServiceCoreMixin._attempt_direct` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/inbox/service/core.py:714` | `InboxServiceCoreMixin.retry_due` | signals=name,identifier,legacy-identifier; loops=1; shape=lexical; operations=- | classified | durable inbox delivery retry |
| `src/hyprial/daemon/impl/inbox/service/delivery/retry.py:180` | `InboxServiceRetryMixin.settle_harness_failure` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/inbox/service/delivery/retry.py:473` | `InboxServiceRetryMixin.retry_custody_due` | signals=name,identifier,legacy-identifier; loops=1; shape=lexical; operations=- | classified | durable inbox delivery retry |
| `src/hyprial/daemon/impl/inbox/service/delivery/retry.py:549` | `InboxServiceRetryMixin.next_retry_due_ms` | signals=name; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/inbox/service/delivery/retry.py:832` | `InboxServiceRetryMixin._log_retry` | signals=name; loops=0; shape=lexical; operations=- | classified | durable inbox delivery retry |
| `src/hyprial/daemon/impl/inbox/service/policy.py:22` | `<module>.online_retry_next_attempt_ms` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | pure due-time calculation; durable inbox delivery owns execution |
| `src/hyprial/daemon/impl/inbox_watch.py:47` | `<module>.backoff_ms` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | classified | inbox watch registration/list refresh |
| `src/hyprial/daemon/impl/inbox_watch.py:393` | `InboxWatch._register_done` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/inbox_watch.py:412` | `InboxWatch._refresh_done` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/inbox_watch.py:445` | `InboxWatch._list_done` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/ipc/router.py:977` | `_IpcDispatchMixin._ipc_autoupdate_notify` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/ipc/server.py:53` | `<module>._write_ipc_response` | signals=syntax; loops=1; shape=retry-failover; operations=send | classified | IPC partial response write retry |
| `src/hyprial/daemon/impl/ipc/server.py:168` | `_IpcServerMixin._serve` | signals=identifier,legacy-identifier,syntax,legacy-loop-delay; loops=1; shape=cadence; operations=- | classified | IPC accept resource-exhaustion retry |
| `src/hyprial/daemon/impl/ipc/server.py:518` | `_IpcServerMixin._dispatch_ipc_response` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic,wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/ipc/wire.py:432` | `_WireEvaluator._WG_012` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/ipc/wire.py:466` | `_WireEvaluator._WG_014` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/ipc/wire.py:641` | `_WireEvaluator._WG_028` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/lifecycle/atomic_lifecycle_ports.py:103` | `AtomicLifecycleDomainPort.drain` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic,wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/lifecycle/health/database.py:127` | `DatabaseHealth.start` | signals=legacy-loop-delay; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/lifecycle/health/database.py:131` | `DatabaseHealth.start.watch` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=check,wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/lifecycle/lifecycle_coordinator.py:112` | `_StepEffects.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/lifecycle/lifecycle_coordinator.py:283` | `_StepEffects._live_harness_prerequisites` | signals=syntax; loops=1; shape=retry-failover; operations=completed_receipt | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/lifecycle/lifecycle_coordinator.py:412` | `_Mailbox.__call__` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/lifecycle/lifecycle_coordinator.py:472` | `LifecycleCoordinator.__init__` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/lifecycle/lifecycle_coordinator.py:616` | `LifecycleCoordinator.recover` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | reads or joins recovery state once; it does not replay a failed command |
| `src/hyprial/daemon/impl/lifecycle/lifecycle_coordinator.py:623` | `LifecycleCoordinator.wait` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=get,monotonic,wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/lifecycle/lifecycle_coordinator.py:659` | `LifecycleCoordinator.drain` | signals=identifier,legacy-identifier,syntax,legacy-loop-delay; loops=3; shape=observation; operations=monotonic,wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/lifecycle/lifecycle_coordinator.py:725` | `LifecycleCoordinator._work` | signals=syntax,legacy-loop-delay; loops=1; shape=cadence; operations=- | dismissed | executes one queued lifecycle effect; completion replay is classified separately |
| `src/hyprial/daemon/impl/lifecycle/lifecycle_coordinator.py:758` | `LifecycleCoordinator._deliver` | signals=identifier,legacy-identifier,syntax,legacy-loop-delay; loops=1; shape=retry; operations=tell | classified | lifecycle completion admission |
| `src/hyprial/daemon/impl/lifecycle/lifecycle_coordinator.py:767` | `LifecycleCoordinator._recover_loop` | signals=name,identifier,legacy-identifier,syntax,legacy-loop-delay; loops=2; shape=retry; operations=_deliver,load | classified | lifecycle recovery loop |
| `src/hyprial/daemon/impl/lifecycle/maintenance_watchdog.py:148` | `MaintenanceWatchdog.start` | signals=legacy-loop-delay; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/lifecycle/maintenance_watchdog.py:157` | `MaintenanceWatchdog.start.watch` | signals=syntax,legacy-loop-delay; loops=1; shape=retry; operations=check | dismissed | periodic watchdog samples are distinct commands, not replay of a failed sample |
| `src/hyprial/daemon/impl/lifecycle/maintenance_watchdog.py:220` | `ActorMaintenanceWatchdog.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/lifecycle/maintenance_watchdog.py:315` | `ActorMaintenanceWatchdog.start` | signals=legacy-loop-delay; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/lifecycle/maintenance_watchdog.py:319` | `ActorMaintenanceWatchdog.start.watch` | signals=syntax,legacy-loop-delay; loops=1; shape=retry; operations=_submit | dismissed | periodic watchdog samples are distinct commands, not replay of a failed sample |
| `src/hyprial/daemon/impl/lifecycle/maintenance_watchdog.py:330` | `ActorMaintenanceWatchdog.close` | signals=identifier,legacy-identifier,syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic,snapshot | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/lifecycle/maintenance_watchdog.py:364` | `ActorMaintenanceWatchdog._receive` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/lifecycle_manager/process.py:62` | `LifecycleProcessManager.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/lifecycle_manager/process.py:201` | `LifecycleProcessManager.recover` | signals=name,syntax; loops=1; shape=observation; operations=_schedule,pending | dismissed | reads or joins recovery state once; it does not replay a failed command |
| `src/hyprial/daemon/impl/lifecycle_manager/process.py:208` | `LifecycleProcessManager.wait` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic,wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/lifecycle_manager/process.py:231` | `LifecycleProcessManager.drain` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic,wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/lifecycle_manager/process.py:268` | `LifecycleProcessManager._run` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/lifecycle_manager/process.py:320` | `LifecycleProcessManager._guarded_recover` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | classified | lifecycle journal recovery |
| `src/hyprial/daemon/impl/lifecycle_manager/process.py:348` | `LifecycleProcessManager._close_requested_and_drained` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/lifecycle_manager/process.py:364` | `LifecycleProcessManager._note_recover_fault` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | classified | lifecycle journal recovery |
| `src/hyprial/daemon/impl/lifecycle_manager/process.py:407` | `LifecycleProcessManager._execute` | signals=legacy-loop-delay; loops=1; shape=lexical; operations=- | classified | lifecycle unresolved forward redrive |
| `src/hyprial/daemon/impl/lifecycle_manager/process.py:563` | `LifecycleProcessManager._compensate` | signals=legacy-loop-delay; loops=1; shape=lexical; operations=- | classified | lifecycle unresolved compensation redrive |
| `src/hyprial/daemon/impl/lifecycle_manager/process.py:734` | `LifecycleProcessManager._admit` | signals=identifier,legacy-identifier,syntax,legacy-loop-delay; loops=1; shape=retry; operations=submit | classified | lifecycle domain admission |
| `src/hyprial/daemon/impl/links/service/core.py:493` | `LinkService._loop` | signals=syntax,legacy-loop-delay; loops=1; shape=retry; operations=tick | limit-class:periodic-reentry | tick retains failed link/apply or mirror work for a later service pass |
| `src/hyprial/daemon/impl/links/store.py:709` | `LinkStore._scan_members` | signals=syntax; loops=1; shape=retry-failover; operations=_members_of,update | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/mcp/channel/adapter.py:424` | `ClaudeChannelAdapter.settle_owed` | signals=syntax; loops=1; shape=retry-failover; operations=_call,_complete_if_terminal | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/mcp/channel/loops.py:34` | `<module>.signal_channel_recovery` | signals=name; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/mcp/channel/loops.py:44` | `<module>._consume_channel_recovery` | signals=name; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/mcp/channel/loops.py:64` | `<module>._run_channel_poll_loop` | signals=identifier,legacy-identifier,syntax,legacy-loop-delay; loops=1; shape=retry; operations=request_rewake | classified | MCP inbox-watch poll recovery |
| `src/hyprial/daemon/impl/mcp/channel/loops.py:212` | `<module>._run_channel_heartbeat_loop` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=_report_poll_failure | classified | MCP inbox-watch heartbeat recovery |
| `src/hyprial/daemon/impl/mcp/channel/loops.py:272` | `<module>._run_channel_daemon_loops` | signals=legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/mcp/channel/loops.py:316` | `<module>._watch_parent` | signals=syntax,legacy-loop-delay; loops=2; shape=observation; operations=_owner_process_status,getppid,info,monotonic,sleep | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/mcp/channel/session.py:78` | `<module>.run_channel_session` | signals=legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/mcp/channel/session.py:192` | `run_channel_session.poll_daemon` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/mcp/channel/session.py:239` | `<module>.serve_channel_stdio` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/mcp/proxy.py:19` | `StatelessDaemonProxy.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/mcp/proxy.py:27` | `StatelessDaemonProxy.call` | signals=identifier,legacy-identifier,syntax; loops=1; shape=retry; operations=connect,request | classified | MCP stateless reconnect |
| `src/hyprial/daemon/impl/mcp/wake.py:103` | `WakeCoordinator.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/mcp/wake.py:227` | `WakeCoordinator.observe_read` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/mcp/wake.py:268` | `WakeCoordinator.dispatch_due` | signals=identifier,legacy-identifier; loops=2; shape=lexical; operations=- | classified | MCP wake retry |
| `src/hyprial/daemon/impl/mcp/wake.py:423` | `<module>._default_retry_delay` | signals=name; loops=0; shape=lexical; operations=- | classified | MCP wake retry |
| `src/hyprial/daemon/impl/operations/management_actor.py:346` | `RegistryManagementAuthority.close` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/operations/service.py:313` | `ServiceManager._load` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=_raise_command,range,runner,sleep | classified | daemon service launchd load |
| `src/hyprial/daemon/impl/operations/service.py:335` | `ServiceManager._unload` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=status | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/operations/watchdog_actor.py:165` | `_WatchdogAuthority._receive` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/operations/watchdog_actor.py:315` | `_WatchdogAuthority.close` | signals=identifier,legacy-identifier,syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic,snapshot | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/org/mesh.py:274` | `OrgContextMesh._on_actor_event` | signals=identifier,legacy-identifier; loops=2; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/org/mesh.py:344` | `OrgContextMesh._submit` | signals=legacy-loop-delay; loops=1; shape=lexical; operations=- | classified | org-context generation reassociation |
| `src/hyprial/daemon/impl/org/mesh.py:595` | `OrgContextMesh.close` | signals=syntax,legacy-loop-delay; loops=2; shape=observation; operations=monotonic,snapshot | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/org/network/access/actor.py:85` | `PeerAccessActor.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/org/network/access/actor.py:172` | `PeerAccessActor.stop` | signals=syntax,legacy-loop-delay; loops=2; shape=observation; operations=monotonic | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/org/network/access/actor.py:248` | `PeerAccessActor._execute` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/org/network/access/actor.py:347` | `PeerAccessActor._retry_later` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | classified | org access incomplete rebuild |
| `src/hyprial/daemon/impl/org/network/casdoor/membership.py:226` | `CasdoorMembershipMixin._owned_org_spaces` | signals=syntax; loops=1; shape=retry-failover; operations=_require_space | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/org/network/casdoor/sync.py:265` | `CasdoorMembershipSync.stop` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/org/network/create/recovery.py:58` | `<module>.prepare_create` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/org/network/create/recovery.py:194` | `<module>.repair_candidates` | signals=identifier,legacy-identifier; loops=2; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/org/network/dialing/__init__.py:72` | `<module>._membership` | signals=syntax; loops=2; shape=retry-failover; operations=list_devices,resolve_org_directory_space | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/org/network/directory.py:74` | `<module>.network_directory_projection` | signals=syntax; loops=1; shape=retry-failover; operations=scan_devices | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/org/network/directory.py:745` | `OrgFsDirectoryStore._note_invalid_device` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/org/network/ipc.py:601` | `<module>._reissue_conflicting_identity` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/org/network/service.py:447` | `OrgNetworkService.delete` | signals=syntax; loops=1; shape=retry-failover; operations=remove_member | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/org/network/service.py:521` | `OrgNetworkService.process_leave_requests` | signals=syntax; loops=2; shape=retry-failover; operations=_remove_member_and_devices,list_leave_requests | limit-class:periodic-reentry | a failed member removal remains a pending leave request for the next pass |
| `src/hyprial/daemon/impl/org/network/service.py:608` | `OrgNetworkService._execute_step` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/org/network/service.py:648` | `OrgNetworkService._join_with_retry` | signals=name,identifier,legacy-identifier,syntax; loops=1; shape=retry; operations=join | classified | org join retry |
| `src/hyprial/daemon/impl/org/orgfs_migration.py:301` | `OrgContextOrgFsBridge.fetch` | signals=syntax; loops=1; shape=retry-failover; operations=_stage_candidate | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/orgfs/composition/content.py:92` | `RuntimeContent.refresh_space` | signals=syntax; loops=1; shape=retry-failover; operations=sync_from | classified | orgfs holder failover refresh |
| `src/hyprial/daemon/impl/orgfs/composition/content.py:167` | `RuntimeContent.await_content` | signals=identifier,legacy-identifier,syntax,legacy-loop-delay; loops=1; shape=retry; operations=request_blob | classified | orgfs content provisioning wait |
| `src/hyprial/daemon/impl/orgfs/composition/directory.py:444` | `RuntimeDirectory._anti_entropy_tick` | signals=syntax; loops=4; shape=retry; operations=schedule_sync_from | limit-class:periodic-reentry | a later anti-entropy tick can revisit work not completed by a failed pass |
| `src/hyprial/daemon/impl/orgfs/composition/directory.py:508` | `RuntimeDirectory._execute_directory_effect` | signals=syntax; loops=4; shape=retry; operations=schedule_sync_from | dismissed | loops schedule distinct directory rows; no failed submission is replayed here |
| `src/hyprial/daemon/impl/orgfs/composition/directory.py:553` | `RuntimeDirectory._close_directory_ingress` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic,snapshot | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/orgfs/composition/directory.py:656` | `RuntimeDirectory._device_owner_index` | signals=syntax; loops=3; shape=retry-failover; operations=listdir | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/orgfs/composition/directory.py:783` | `RuntimeDirectory._process_peer_liveliness` | signals=syntax; loops=2; shape=cadence; operations=- | dismissed | loop/cadence without an operation repeated after delay or failure |
| `src/hyprial/daemon/impl/orgfs/composition/lifecycle.py:251` | `RuntimeLifecycle._on_lifecycle_event` | signals=identifier,legacy-identifier; loops=2; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/orgfs/composition/lifecycle.py:276` | `RuntimeLifecycle._ask_lifecycle` | signals=legacy-loop-delay; loops=1; shape=lexical; operations=- | classified | orgfs lifecycle generation reassociation |
| `src/hyprial/daemon/impl/orgfs/composition/lifecycle.py:327` | `RuntimeLifecycle._close_lifecycle` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic,snapshot | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/orgfs/composition/lifecycle.py:599` | `RuntimeLifecycle._join_sync` | signals=syntax; loops=1; shape=retry-failover; operations=sync_from | classified | orgfs join holder failover |
| `src/hyprial/daemon/impl/orgfs/composition/space_purge.py:260` | `RuntimeSpacePurge._wait_for_space_lifecycle` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/orgfs/document/effects.py:100` | `FacadeEffects._publication_records` | signals=syntax; loops=2; shape=retry-failover; operations=loads | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/orgfs/document/effects.py:315` | `FacadeEffects.drain_broadcasts` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic,snapshot | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/orgfs/document/effects.py:378` | `FacadeEffects._on_effect_completion` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/orgfs/document/effects.py:425` | `FacadeEffects.detach_space` | signals=syntax,legacy-loop-delay; loops=3; shape=observation; operations=monotonic,snapshot | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/orgfs/document/effects.py:487` | `FacadeEffects.close_effects` | signals=syntax,legacy-loop-delay; loops=3; shape=observation; operations=monotonic,snapshot | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/orgfs/document/model.py:717` | `TreeDocument._raw_nodes` | signals=syntax; loops=1; shape=retry-failover; operations=loads | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/orgfs/document/model.py:727` | `TreeDocument.materialize` | signals=syntax; loops=3; shape=retry-failover; operations=append,loads | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/orgfs/document/space_state.py:408` | `FacadeSpaceState._on_space_state_event` | signals=identifier,legacy-identifier; loops=2; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/orgfs/document/space_state.py:448` | `FacadeSpaceState._submit_space_state` | signals=legacy-loop-delay; loops=1; shape=lexical; operations=- | classified | orgfs space-state generation reassociation |
| `src/hyprial/daemon/impl/orgfs/mesh.py:90` | `OrgFsMesh.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/orgfs/mesh.py:318` | `OrgFsMesh.close` | signals=syntax,legacy-loop-delay; loops=3; shape=observation; operations=monotonic,snapshot | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/orgfs/network/recovery.py:120` | `MeshRecovery._projection_retry_due` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | classified | orgfs projection repair |
| `src/hyprial/daemon/impl/orgfs/network/recovery.py:209` | `MeshRecovery._retry_failed_projection` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | classified | orgfs projection repair |
| `src/hyprial/daemon/impl/orgfs/network/recovery.py:305` | `MeshRecovery._repair_projections` | signals=identifier,legacy-identifier; loops=6; shape=lexical; operations=- | classified | orgfs projection repair |
| `src/hyprial/daemon/impl/orgfs/network/recovery.py:486` | `MeshRecovery._receive` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/orgfs/network/recovery.py:552` | `MeshRecovery._recover_blob` | signals=name; loops=0; shape=lexical; operations=- | classified | orgfs inline blob import retry |
| `src/hyprial/daemon/impl/orgfs/network/recovery.py:582` | `MeshRecovery._defer_blob_recovery` | signals=name,identifier,legacy-identifier; loops=1; shape=lexical; operations=- | classified | orgfs inline blob import retry |
| `src/hyprial/daemon/impl/orgfs/network/recovery.py:637` | `MeshRecovery.request_blob` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/orgfs/network/recovery.py:689` | `MeshRecovery._run_blob_recovery` | signals=name,identifier,legacy-identifier; loops=3; shape=lexical; operations=- | classified | orgfs blob recovery |
| `src/hyprial/daemon/impl/orgfs/network/supplier_health.py:228` | `SupplierSyncMixin._run_scheduled_sync` | signals=legacy-identifier,legacy-loop-delay; loops=1; shape=lexical; operations=- | classified | orgfs scheduled supplier sync |
| `src/hyprial/daemon/impl/orgfs/network/supplier_health.py:229` | `SupplierSyncMixin._run_scheduled_sync.run_once` | signals=identifier,legacy-identifier,syntax,legacy-loop-delay; loops=1; shape=retry; operations=_clear_supplier_sync_failure,_record_supplier_sync_failure,sync_from | classified | orgfs scheduled supplier sync |
| `src/hyprial/daemon/impl/orgfs/network/sync.py:575` | `MeshSync.sync_log_range_from` | signals=syntax; loops=2; shape=retry-failover; operations=_decode_request | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/orgfs/projection/checkout_authority.py:156` | `CheckoutAuthority._ask` | signals=legacy-loop-delay; loops=1; shape=lexical; operations=- | classified | orgfs checkout generation reassociation |
| `src/hyprial/daemon/impl/orgfs/projection/checkout_authority.py:224` | `CheckoutAuthority._on_actor_event` | signals=identifier,legacy-identifier; loops=2; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/orgfs/runtime.py:206` | `OrgFsRuntime._close_impl` | signals=syntax,legacy-loop-delay; loops=6; shape=observation; operations=monotonic,wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/orgfs/storage/blob_authority.py:268` | `BlobAuthority._on_actor_event` | signals=identifier,legacy-identifier; loops=2; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/orgfs/storage/blob_authority.py:411` | `BlobAuthority._call` | signals=legacy-loop-delay; loops=1; shape=lexical; operations=- | classified | orgfs blob generation reassociation |
| `src/hyprial/daemon/impl/orgfs/storage/blob_authority.py:525` | `BlobAuthority.close` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic,wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/orgfs/storage/replica.py:489` | `ReplicaStore.prime_from` | signals=syntax; loops=3; shape=retry; operations=loads,store_envelope | dismissed | loops consume distinct exported envelopes; failures propagate instead of replaying an envelope |
| `src/hyprial/daemon/impl/orgfs/storage/replica_authority.py:420` | `ReplicaAuthority._on_actor_event` | signals=identifier,legacy-identifier; loops=2; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/orgfs/storage/replica_authority.py:455` | `ReplicaAuthority._ask` | signals=legacy-loop-delay; loops=1; shape=lexical; operations=- | classified | orgfs replica generation reassociation |
| `src/hyprial/daemon/impl/orgfs/storage/replica_authority.py:552` | `ReplicaAuthority.close` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic,snapshot | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/orgfs/storage/space_authority.py:596` | `OrgSpaceAuthority._on_actor_event` | signals=identifier,legacy-identifier; loops=2; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/orgfs/storage/space_authority.py:640` | `OrgSpaceAuthority._ask` | signals=legacy-loop-delay; loops=1; shape=lexical; operations=- | classified | orgfs space generation reassociation |
| `src/hyprial/daemon/impl/orgfs/storage/space_authority.py:783` | `OrgSpaceAuthority.close` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic,snapshot | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/orgfs/storage/store/admission.py:348` | `StoreAdmission._tree_rows` | signals=syntax; loops=1; shape=retry-failover; operations=loads | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/orgfs/storage/store/journal.py:438` | `StoreJournal.commit_many` | signals=syntax; loops=1; shape=retry; operations=_commit_many_inner | classified | orgfs commit-many client registration retry |
| `src/hyprial/daemon/impl/orgfs/storage/store/membership.py:619` | `StoreMembership._journal_client_ids` | signals=syntax; loops=1; shape=retry-failover; operations=_decode_envelope,bytes | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/orgfs/storage/store/retirement.py:342` | `StoreRetirement.unconfirmed_commits` | signals=syntax; loops=2; shape=retry-failover; operations=_decode_envelope,bytes | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/orgfs/webserver/server.py:145` | `_OrgfsWebHandler._serve` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/orgfs/webserver/server.py:355` | `_OrgfsWebHandler._respond_orgfs_error` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/owner_migration/planning.py:159` | `<module>.plan_database` | signals=syntax; loops=4; shape=retry-failover; operations=execute,fetchall | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/pac/actors/coordinator/types.py:215` | `<module>.cleanup_retry_delay_ms` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | classified | PAC cleanup retry |
| `src/hyprial/daemon/impl/pac/actors/coordinator/types.py:234` | `<module>.throttle_cleanup_retries` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/pac/actors/coordinator/types.py:323` | `<module>.cleanup_stop_operation` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/pac/actors/coordinator/types.py:337` | `<module>.cleanup_operation_base` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/pac/actors/gc.py:99` | `PacGc._run` | signals=syntax,legacy-loop-delay; loops=1; shape=retry; operations=run_pass | limit-class:periodic-reentry | the timer survives run_pass failure and revisits still-eligible cleanup work |
| `src/hyprial/daemon/impl/pac/contracts/principals.py:165` | `<module>._graph_candidates` | signals=syntax; loops=1; shape=retry-failover; operations=parse_principal | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/pac/graphs/authority/__init__.py:440` | `PacGraphAuthority._actor_effect_loop` | signals=syntax,legacy-loop-delay; loops=2; shape=retry; operations=tell | classified | PAC actor-effect completion admission |
| `src/hyprial/daemon/impl/pac/graphs/authority/__init__.py:686` | `PacGraphAuthority.restart_workflow_worker` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/pac/graphs/authority/__init__.py:892` | `PacGraphAuthority._delivery_loop` | signals=syntax,legacy-loop-delay; loops=3; shape=retry-failover; operations=send,summary_from_text,tell | classified | PAC notification completion admission, PAC notification durable send replay |
| `src/hyprial/daemon/impl/pac/graphs/authority/generation.py:149` | `_Generation._wake_affected` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/pac/graphs/authority/generation.py:553` | `_Generation._write` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/pac/graphs/reactor.py:470` | `PacReactor._deliver` | signals=syntax; loops=1; shape=retry-failover; operations=send,summary_from_text | classified | PAC notification durable send replay |
| `src/hyprial/daemon/impl/pac/graphs/reactor.py:932` | `PacReactor.resend_undelivered` | signals=syntax; loops=1; shape=retry-failover; operations=send,summary_from_text | classified | PAC notification durable send replay |
| `src/hyprial/daemon/impl/pac/views/missions.py:87` | `<module>.load_missions` | signals=syntax; loops=1; shape=retry-failover; operations=get,request | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/pac/workflows/runtime/core.py:197` | `_GraphWorkflowServiceCore.recover` | signals=name; loops=0; shape=lexical; operations=- | dismissed | reads or joins recovery state once; it does not replay a failed command |
| `src/hyprial/daemon/impl/pac/workflows/runtime/operations.py:657` | `_GraphWorkflowServiceOps.restart_worker` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/pac/workflows/runtime/operations.py:670` | `_GraphWorkflowServiceOps._restart_worker_direct` | signals=name; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/pac/workflows/runtime/types.py:51` | `WorkflowTickAuthority.restart_workflow_worker` | signals=name; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/path_authz/policy.py:110` | `<module>._within` | signals=syntax; loops=1; shape=retry-failover; operations=samestat,stat | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/processes/orphan_processes.py:908` | `OrphanProcessAuthority.close` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/processes/process_owner.py:455` | `ProcessOwner.detach` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/processes/result_settlement.py:107` | `ResultSettlementCoordinator.close` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/processes/supervisor.py:45` | `<module>._start_backoff_max_seconds` | signals=name; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/processes/supervisor.py:113` | `ManagedHarnessRuntime.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/route_registration/client.py:85` | `RouteRegistrationClient._call` | signals=syntax,legacy-loop-delay; loops=3; shape=retry; operations=confirm_receipt_retired,submit | classified | route settled-call replay |
| `src/hyprial/daemon/impl/route_registration/io.py:58` | `RouteRegistrationIo.__init__` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/route_registration/io.py:234` | `RouteRegistrationIo.drain` | signals=syntax,legacy-loop-delay; loops=3; shape=observation; operations=monotonic,wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/route_registration/io.py:329` | `RouteRegistrationIo._run` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/route_registration/io.py:613` | `RouteRegistrationIo._execute_native` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | classified | route native partial cleanup |
| `src/hyprial/daemon/impl/route_registration/io.py:706` | `RouteRegistrationIo._complete_native` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/route_registration/io.py:901` | `RouteRegistrationIo._settle` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | classified | route completion publication |
| `src/hyprial/daemon/impl/route_registration/io.py:945` | `RouteRegistrationIo._service_resettlements` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | classified | route completion publication |
| `src/hyprial/daemon/impl/runtime/bridge.py:146` | `DaemonEventBridge.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/runtime/bridge.py:338` | `DaemonEventBridge.start` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/runtime/bridge.py:463` | `DaemonEventBridge.on_timer` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/runtime/bridge.py:586` | `DaemonEventBridge._harness_reconcile_completed` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/runtime/forwards.py:588` | `_BridgeForwardsMixin._retry_pending_notices` | signals=name; loops=1; shape=lexical; operations=- | classified | pending availability notice replay |
| `src/hyprial/daemon/impl/runtime/forwards.py:595` | `_BridgeForwardsMixin._report_stalled_deliveries` | signals=identifier,legacy-identifier; loops=2; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/runtime/forwards.py:803` | `_BridgeForwardsMixin.stop` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/runtime/forwards.py:884` | `_BridgeForwardsMixin._drain_blocking_failures_on_stop` | signals=identifier,legacy-identifier,syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/runtime/reconcile.py:37` | `_BridgeReconcileMixin._kick_retry_pump` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | classified | daemon outbox retry pump |
| `src/hyprial/daemon/impl/runtime/reconcile.py:49` | `_BridgeReconcileMixin._retry_pump_run` | signals=name,identifier,legacy-identifier,syntax,legacy-loop-delay; loops=1; shape=retry; operations=retry_due | classified | daemon outbox retry pump |
| `src/hyprial/daemon/impl/runtime/reconcile.py:130` | `_BridgeReconcileMixin._halt_retry_pump` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/runtime/reconcile.py:244` | `_BridgeReconcileMixin._dispatch_harness_deliveries` | signals=identifier,legacy-identifier; loops=3; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/runtime/reconcile.py:400` | `_BridgeReconcileMixin._drain_dispatch_offer_outcomes` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/runtime/results.py:633` | `_BridgeResultsMixin._settle_failed_result` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/runtime/results.py:778` | `_BridgeResultsMixin._retry_blocking_failures` | signals=name; loops=1; shape=lexical; operations=- | classified | blocking-failure observer replay |
| `src/hyprial/daemon/impl/runtime/settlement.py:38` | `<module>._retry_pump_wait_seconds` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | classified | daemon outbox retry pump |
| `src/hyprial/daemon/impl/runtime/settlement.py:219` | `ForwardSettlementCoordinator.close` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/runtime/settlement.py:509` | `_NoticeRecoveryMixin._recover_owed_notices` | signals=name,identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/runtime/settlement.py:563` | `_NoticeRecoveryMixin._flush_pending_notices_on_stop` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/service_connect/manager.py:494` | `ServiceManager._scheduled_reconcile` | signals=limit-class; loops=0; shape=lexical; operations=- | limit-class:periodic-reentry | the scheduled pass retains failed teardowns and desired mappings for later _unmap/_map attempts |
| `src/hyprial/daemon/impl/session_actor/actor.py:78` | `SessionActor.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/session_actor/actor.py:255` | `SessionActor._replay_undelivered` | signals=syntax,legacy-loop-delay; loops=1; shape=cadence; operations=- | classified | session undelivered admission replay |
| `src/hyprial/daemon/impl/session_actor/actor.py:280` | `SessionActor.generation` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=get,monotonic,snapshot | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/session_actor/actor.py:455` | `SessionActor._replay_when_running` | signals=syntax,legacy-loop-delay; loops=1; shape=cadence; operations=- | classified | session generation reconciliation |
| `src/hyprial/daemon/impl/session_actor/actor.py:637` | `SessionActor._deliver_internal` | signals=syntax,legacy-loop-delay; loops=1; shape=retry; operations=tell | classified | session internal completion admission |
| `src/hyprial/daemon/impl/session_actor/worker.py:29` | `_AgentEffectWorker.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/session_actor/worker.py:97` | `_AgentEffectWorker.drain` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic,wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/session_actor/worker.py:147` | `_AgentEffectWorker._admit` | signals=identifier,legacy-identifier,syntax,legacy-loop-delay; loops=1; shape=retry; operations=submit | classified | session agent-command admission |
| `src/hyprial/daemon/impl/squire/actors/delivery.py:150` | `UserDeliveryActor._execute` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/squire/actors/delivery.py:331` | `UserDeliveryActor.close` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic,snapshot | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/squire/actors/ledger.py:122` | `UserDeliveryLedgerAuthority.recover` | signals=name; loops=0; shape=lexical; operations=- | dismissed | reads or joins recovery state once; it does not replay a failed command |
| `src/hyprial/daemon/impl/squire/actors/profile.py:275` | `UserProfileAuthority.close` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/squire/probe.py:70` | `<module>.classify_run` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/squire/probe.py:88` | `<module>._extract_retry_at` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/squire/probe.py:165` | `RuntimeProber.probe` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/squire/store_lock.py:18` | `<module>.json_store_mutation` | signals=syntax,legacy-loop-delay; loops=1; shape=retry; operations=lock_exclusive | classified | squire JSON mutation lock |
| `src/hyprial/daemon/impl/state_db.py:122` | `StateDatabase._open` | signals=identifier,legacy-identifier,syntax,legacy-loop-delay; loops=1; shape=retry; operations=_connect_once,_is_transient_lock | classified | state database connection initialization |
| `src/hyprial/daemon/impl/state_db.py:236` | `StateDatabase._begin_write` | signals=identifier,legacy-identifier,syntax,legacy-loop-delay; loops=1; shape=retry; operations=_is_transient_lock,execute | classified | state database write-lock acquisition |
| `src/hyprial/daemon/impl/state_persistence/alarm.py:78` | `StateWriterAlarm.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/state_persistence/alarm.py:99` | `StateWriterAlarm.observe` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/transfer/archive/env_credentials.py:23` | `<module>.resolve_env_credentials` | signals=syntax; loops=1; shape=retry-failover; operations=resolve | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/transfer/execution/smolvm.py:172` | `<module>._run` | signals=syntax,legacy-loop-delay; loops=1; shape=retry; operations=killpg | classified | smolvm process-group termination retry |
| `src/hyprial/daemon/impl/transfer/landing/owner.py:306` | `<module>._remove_home` | signals=syntax; loops=2; shape=retry-failover; operations=unlink | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/transfer/landing/receive.py:183` | `receive_bundle._undo` | signals=syntax; loops=1; shape=retry-failover; operations=is_dir,is_symlink,rmdir,unlink | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/transfer/orchestrator.py:154` | `<module>._preflight` | signals=syntax; loops=1; shape=retry-failover; operations=has_binary | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/transfer/p2/journal.py:287` | `PhaseJournal.pending_quiesce` | signals=syntax; loops=1; shape=retry-failover; operations=_read,append | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/daemon/impl/transfer/p2/orchestration/target_tui.py:312` | `PreCommitRollbackReport.to_json` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/transfer/p2/orchestration/target_tui.py:320` | `<module>.rollback_pre_commit` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/transfer/p2/source/session.py:351` | `<module>.restart_session` | signals=name; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/transport/query_actor.py:166` | `QueryIoOwner.close` | signals=identifier,legacy-identifier,syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic,wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/transport/session_actor.py:303` | `_CallbackPool.drain` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic,wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/transport/session_actor.py:347` | `TransportSessionAuthority.__init__` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/transport/session_actor.py:467` | `TransportSessionAuthority.projection` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/transport/session_actor.py:490` | `TransportSessionAuthority._admit` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/transport/session_actor.py:522` | `TransportSessionAuthority._receive` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/transport/session_actor.py:585` | `TransportSessionAuthority._schedule_recovery` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | classified | transport callback refresh |
| `src/hyprial/daemon/impl/transport/session_actor.py:603` | `TransportSessionAuthority._pump` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/transport/session_actor.py:670` | `TransportSessionAuthority._invalidate_callback` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/transport/session_actor.py:683` | `TransportSessionAuthority._declare_native` | signals=legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/transport/session_actor.py:687` | `TransportSessionAuthority._declare_native.observed` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/transport/session_actor.py:939` | `TransportSessionAuthority.close` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/transport/session_recovery.py:130` | `SessionRecoveryMixin._recovery_allowed` | signals=name; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/transport/session_recovery.py:177` | `TransportRecoveryMixin._schedule_degraded_recovery` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | classified | Zenoh degraded-session recovery |
| `src/hyprial/daemon/impl/transport/session_recovery.py:198` | `TransportRecoveryMixin._recover_degraded` | signals=name,identifier,legacy-identifier,syntax,legacy-loop-delay; loops=1; shape=retry; operations=open | classified | Zenoh degraded-session recovery |
| `src/hyprial/daemon/impl/transport/session_recovery.py:268` | `TransportRecoveryMixin.session_health` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/transport/zenoh/config.py:141` | `ZenohConfig.__post_init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/transport/zenoh/config.py:167` | `ZenohConfig.from_environment` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/transport/zenoh/config.py:201` | `ZenohConfig.to_json` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/transport/zenoh/dispatchers.py:168` | `_QueryDispatcher.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/transport/zenoh/dispatchers.py:263` | `_QueryDispatcher.retry_cleanup` | signals=name,identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | one explicit native-cleanup pass; this function has no internal replay schedule |
| `src/hyprial/daemon/impl/transport/zenoh/dispatchers.py:304` | `_QueryDispatcher.quiesce` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic,wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/transport/zenoh/dispatchers.py:323` | `_QueryDispatcher.stop` | signals=identifier,legacy-identifier; loops=3; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/transport/zenoh/presence.py:261` | `LivelinessDirectory._reconcile_confirms` | signals=syntax,legacy-loop-delay; loops=1; shape=retry; operations=_confirm_present | classified | liveliness presence reconciliation |
| `src/hyprial/daemon/impl/transport/zenoh/transport.py:130` | `ZenohTransport.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/transport/zenoh/transport.py:270` | `ZenohTransport._settled` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=_wait_failed,monotonic,wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/transport/zenoh/transport.py:347` | `ZenohTransport._reconfigure_connect_drained` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/daemon/impl/transport/zenoh/transport.py:655` | `ZenohTransport._register` | signals=syntax; loops=1; shape=retry; operations=_registration,_settled,append,declare | classified | Zenoh native call reopen |
| `src/hyprial/daemon/impl/transport/zenoh/transport.py:955` | `ZenohTransport.close` | signals=syntax,legacy-loop-delay; loops=2; shape=observation; operations=monotonic,wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/daemon/impl/updates/resolution.py:88` | `<module>.resolve_remote` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/identity/impl/agents/actor/_base.py:391` | `_RuntimeLaunchState.wait_drained` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic,wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/identity/impl/agents/actor/_core.py:521` | `AgentActor.migration_owner_call` | signals=identifier,legacy-identifier,syntax,legacy-loop-delay; loops=1; shape=retry; operations=tell | classified | agent migration owner generation retry |
| `src/hyprial/identity/impl/agents/actor/_core.py:551` | `AgentActor.run_migration` | signals=legacy-loop-delay; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/identity/impl/agents/home/effects.py:148` | `AgentHomeEffectLane.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/identity/impl/agents/home/effects.py:228` | `AgentHomeEffectLane._run` | signals=identifier,legacy-identifier,syntax,legacy-loop-delay; loops=3; shape=retry; operations=_complete | classified | agent-home effect completion custody |
| `src/hyprial/identity/impl/agents/home/provisioner.py:499` | `AgentHomeProvisioner.workspace_summary` | signals=syntax; loops=2; shape=retry-failover; operations=stat | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/identity/impl/agents/registry/_core.py:333` | `AgentRegistry._import_legacy_files` | signals=syntax; loops=2; shape=retry-failover; operations=from_json,loads,read_text | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/identity/impl/agents/state/worker_state.py:257` | `RestorePolicyAuthority.close` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/identity/impl/agents/state/worker_state.py:371` | `BlockingFailureAuthority._complete` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/identity/impl/agents/state/worker_state.py:424` | `BlockingFailureAuthority._submit_owned` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/identity/impl/agents/state/worker_state.py:443` | `BlockingFailureAuthority._schedule_retry` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | classified | agent state publication retry |
| `src/hyprial/identity/impl/agents/state/worker_state.py:448` | `BlockingFailureAuthority._retry` | signals=name; loops=0; shape=lexical; operations=- | classified | agent state publication retry |
| `src/hyprial/identity/impl/agents/state/worker_state.py:454` | `BlockingFailureAuthority.close` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/identity/impl/device/machine_binding.py:138` | `<module>._machine_identifier` | signals=syntax; loops=1; shape=retry-failover; operations=read_text,strip | classified | machine identifier source failover |
| `src/hyprial/identity/impl/identity_transaction.py:43` | `IdentityTransactionLock.acquire` | signals=syntax,legacy-loop-delay; loops=1; shape=retry; operations=lock_exclusive | classified | identity transaction lock |
| `src/hyprial/identity/impl/provider_auth/actor/_authority.py:40` | `ProviderAuthAuthority.__init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/identity/impl/provider_auth/actor/_authority.py:306` | `ProviderAuthAuthority._release_episode_credit_locked` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/identity/impl/provider_auth/actor/_authority.py:342` | `ProviderAuthAuthority._defer_observation_locked` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/identity/impl/provider_auth/actor/_authority.py:371` | `ProviderAuthAuthority._accept_notice` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/identity/impl/provider_auth/actor/_authority.py:404` | `ProviderAuthAuthority._retire_superseded_notices` | signals=identifier,legacy-identifier; loops=2; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/identity/impl/provider_auth/actor/_authority.py:436` | `ProviderAuthAuthority._schedule_retry` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | classified | provider-auth helper retry |
| `src/hyprial/identity/impl/provider_auth/actor/_authority.py:461` | `ProviderAuthAuthority._cancel_retry` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/identity/impl/provider_auth/actor/_authority.py:473` | `ProviderAuthAuthority._retry_loop` | signals=name,identifier,legacy-identifier,syntax,legacy-loop-delay; loops=2; shape=cadence; operations=- | classified | provider-auth helper retry |
| `src/hyprial/identity/impl/provider_auth/actor/_authority.py:550` | `ProviderAuthAuthority._receive` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/identity/impl/provider_auth/actor/_authority.py:695` | `ProviderAuthAuthority._apply_decision` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/identity/impl/provider_auth/actor/_authority.py:759` | `ProviderAuthAuthority._start_helper` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/identity/impl/provider_auth/actor/_authority.py:791` | `ProviderAuthAuthority._run_helper` | signals=legacy-loop-delay; loops=0; shape=lexical; operations=- | classified | provider-auth code-observation admission |
| `src/hyprial/identity/impl/provider_auth/actor/_authority.py:801` | `ProviderAuthAuthority._run_helper.announce` | signals=syntax,legacy-loop-delay; loops=1; shape=retry; operations=_admit | classified | provider-auth code-observation admission |
| `src/hyprial/identity/impl/provider_auth/actor/_authority.py:833` | `ProviderAuthAuthority.status` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/identity/impl/provider_auth/actor/_authority.py:883` | `ProviderAuthAuthority.close` | signals=identifier,legacy-identifier,syntax,legacy-loop-delay; loops=3; shape=observation; operations=monotonic,snapshot | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/identity/impl/provider_auth/actor/_episode.py:17` | `_StopView.wait` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic,wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/identity/impl/provider_auth/coordinator/_coordinator.py:100` | `ProviderAuthCoordinator.resume_after_restore` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/identity/impl/provider_auth/coordinator/_coordinator.py:185` | `ProviderAuthCoordinator._handle_turn_failure` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/identity/impl/provider_auth/coordinator/_coordinator.py:396` | `ProviderAuthCoordinator._handle_account_failure` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/identity/impl/provider_auth/coordinator/_coordinator.py:547` | `ProviderAuthCoordinator._finish_round` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/identity/impl/provider_auth/coordinator/_coordinator.py:822` | `ProviderAuthCoordinator._recover` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/identity/impl/provider_auth/helper.py:171` | `DeviceLoginRunner._reap` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic,poll,wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/identity/impl/provider_auth/helper.py:195` | `DeviceLoginRunner._pump` | signals=legacy-loop-delay; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/identity/impl/users/_store_lock.py:22` | `<module>.json_store_mutation` | signals=syntax,legacy-loop-delay; loops=1; shape=retry; operations=lock_exclusive | classified | user JSON mutation lock |
| `src/hyprial/identity/impl/users/profile.py:71` | `RuntimeCapability.from_json` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/identity/impl/users/profile.py:108` | `RuntimeCapability.to_json` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/identity/impl/users/store/_store.py:165` | `UserStore._switch_to_wal` | signals=syntax,legacy-loop-delay; loops=1; shape=retry; operations=execute | classified | user store WAL transition |
| `src/hyprial/kernel/impl/actor_runtime/effects.py:51` | `EffectLane.__init__` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/kernel/impl/actor_runtime/effects.py:154` | `EffectLane._take_request` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/kernel/impl/actor_runtime/effects.py:162` | `EffectLane._run` | signals=identifier,legacy-identifier,syntax,legacy-loop-delay; loops=2; shape=retry; operations=_complete,_execute,_take_request | classified | effect completion custody |
| `src/hyprial/kernel/impl/actor_runtime/guardian.py:168` | `ActorGuardian.reset` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/kernel/impl/actor_runtime/guardian.py:186` | `ActorGuardian.drain` | signals=syntax,legacy-loop-delay; loops=4; shape=retry; operations=_record_load | dismissed | drain loop visits distinct actor records and only observes their settlement |
| `src/hyprial/kernel/impl/actor_runtime/guardian.py:249` | `ActorGuardian._restart_attempt_failed` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | classified | actor supervision |
| `src/hyprial/kernel/impl/actor_runtime/guardian.py:262` | `ActorGuardian._process_failure` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | classified | actor supervision |
| `src/hyprial/kernel/impl/actor_runtime/guardian.py:337` | `ActorGuardian._restart` | signals=name,identifier,legacy-identifier; loops=0; shape=lexical; operations=- | classified | actor supervision |
| `src/hyprial/kernel/impl/actor_runtime/guardian.py:392` | `ActorGuardian._prune_failures` | signals=identifier,legacy-identifier; loops=1; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/kernel/impl/actor_runtime/guardian.py:410` | `ActorGuardian._notify_undelivered` | signals=syntax; loops=1; shape=retry-failover; operations=sink | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/kernel/impl/actor_runtime/policies.py:23` | `SupervisionPolicy.__post_init__` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/kernel/impl/actor_runtime/policies.py:33` | `SupervisionPolicy.delay` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/kernel/impl/actor_runtime/runtime.py:30` | `<module>._log_event` | signals=identifier,legacy-identifier; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/kernel/impl/actor_runtime/scheduler.py:79` | `GenerationScheduler.shutdown` | signals=syntax,legacy-loop-delay; loops=2; shape=observation; operations=monotonic,wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/kernel/impl/contracts/channels/lark.py:6` | `<module>.lark_recovery_coverage` | signals=name; loops=0; shape=lexical; operations=- | dismissed | lexical retry/recovery token only; no repeated operation shape |
| `src/hyprial/kernel/impl/log/writer.py:101` | `LogWriter.wait_for_writes` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic,wait | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/kernel/impl/platform/file_lock.py:20` | `<module>.lock_exclusive` | signals=syntax,legacy-loop-delay; loops=1; shape=retry; operations=locking | classified | kernel blocking file lock |
| `src/hyprial/kernel/impl/platform/windows_owned_process.py:262` | `WindowsOwnedProcessGroup._wait_for_job_inactive` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/kernel/impl/platform/windows_owned_process.py:432` | `WindowsOwnedProcessGroup.force_close` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/kernel/impl/platform/windows_pipe.py:58` | `<module>._settle_response_write` | signals=syntax,legacy-loop-delay; loops=1; shape=retry; operations=getoverlappedresult | classified | Windows IPC response-write settlement |
| `src/hyprial/kernel/impl/platform/windows_pipe.py:175` | `PipeStream._sendall` | signals=syntax; loops=1; shape=retry; operations=_complete_response_write | classified | Windows IPC response-write settlement |
| `src/hyprial/kernel/impl/platform/windows_pipe.py:357` | `<module>.connect` | signals=syntax,legacy-loop-delay; loops=1; shape=retry; operations=createfile,max,monotonic,pipestream,settimeout | classified | Windows IPC busy-pipe connect |
| `src/hyprial/kernel/impl/processes/owned_process.py:161` | `<module>.linux_group_has_live_members` | signals=syntax; loops=1; shape=retry-failover; operations=read_text | dismissed | failover-shape review: iterates independent items or performs an observation/search; it does not repeat one logical effect against alternatives |
| `src/hyprial/kernel/impl/processes/owned_process.py:432` | `OwnedProcessGroup._wait_unregistered_group_gone` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=monotonic | dismissed | observation wait: the loop only polls state and does not replay work |
| `src/hyprial/kernel/impl/processes/owned_process.py:440` | `OwnedProcessGroup._wait_gone` | signals=syntax,legacy-loop-delay; loops=1; shape=observation; operations=exists,monotonic | dismissed | observation wait: the loop only polls state and does not replay work |

### E3. Kernel-interface scanner output

## Kernel boundary evidence

Source baseline: `d5a4dd257d15320ce59a9bb56d116e786ab4e195`. `kernel/interface.py` exports: **209**; callback-bearing surfaces: **15**; exports with zero explicit upper-layer consumers: **9**. Call shapes: data=71, module/data=2, sync object; method blocking UNKNOWN=63, sync; blocking UNKNOWN=73.

Consumer count is the number of distinct Python files in identity, daemon, biz, and shell with an explicit `from hyprial.kernel[.interface] import NAME`. `sync` describes Python call shape only; whether it blocks is UNKNOWN unless a narrower contract proves it.

| export | definition | call shape | crossing signature/type | Python callback | consumers |
|---|---|---|---|---|---:|
| `_candidate` | `src/hyprial/kernel/impl/facts/tier_vocab.py:41` | sync; blocking UNKNOWN | `(harness: str, provider: str \| None, model: str) -> Candidate` | no | 0 |
| `ActorEvent` | `src/hyprial/kernel/impl/actor_runtime/contracts.py:70` | sync object; method blocking UNKNOWN | `class(kind: ActorEventKind; handle: ActorHandle; generation: int; command_type: str \| None; code: str \| None; detail: str \| None; restart_delay: float \| None)` | no | 8 |
| `ActorEventKind` | `src/hyprial/kernel/impl/actor_runtime/contracts.py:23` | sync object; method blocking UNKNOWN | `class` | no | 8 |
| `ActorHandle` | `src/hyprial/kernel/impl/actor_runtime/contracts.py:47` | sync object; method blocking UNKNOWN | `class(actor_id: str; name: str)` | no | 16 |
| `ActorRuntime` | `src/hyprial/kernel/impl/actor_runtime/runtime.py:53` | sync object; method blocking UNKNOWN | `class(self, *, event_sink: EventSink=_log_event, policies: Mapping[str, SupervisionPolicy] \| None=None, random_source: Callable[[], float] \| None=None, scheduler: GenerationScheduler \| None=None, cpu_accounting: RuntimeCpuCounters=runtime_cpu_counters)` | yes | 64 |
| `ActorSpec` | `src/hyprial/kernel/impl/actor_runtime/contracts.py:55` | sync object; method blocking UNKNOWN | `class(name: str; handler_factory: Callable[[], CommandHandler]; mailbox_capacity: int; supervision_profile: str; undelivered_sink: Callable[[object, str], None] \| None)` | yes | 60 |
| `ActorState` | `src/hyprial/kernel/impl/actor_runtime/contracts.py:16` | sync object; method blocking UNKNOWN | `class` | no | 2 |
| `ADAPTER_URI_PREFIX` | `src/hyprial/kernel/impl/primitives/uri.py:233` | data | `'adapter:'` | no | 5 |
| `AdmissionResult` | `src/hyprial/kernel/impl/actor_runtime/contracts.py:8` | sync object; method blocking UNKNOWN | `class` | no | 91 |
| `agent_uri_actor` | `src/hyprial/kernel/impl/primitives/uri.py:98` | sync; blocking UNKNOWN | `(value: str) -> str \| None` | no | 12 |
| `AGENT_URI_PREFIX` | `src/hyprial/kernel/impl/primitives/uri.py:14` | data | `'agent:'` | no | 1 |
| `atomic_json_write` | `src/hyprial/kernel/impl/configuration/persistent_config.py:327` | sync; blocking UNKNOWN | `(path: Path, value: object) -> None` | no | 24 |
| `bound_request` | `src/hyprial/kernel/impl/contracts/channels/worker_channel.py:78` | sync; blocking UNKNOWN | `(request: object, binding: WorkerBinding) -> tuple[str, dict[str, Any]]` | no | 2 |
| `build_probe_command` | `src/hyprial/kernel/impl/facts/tier_vocab.py:10` | sync; blocking UNKNOWN | `(harness: str, model: str, provider: str \| None=None) -> tuple[str, ...]` | no | 2 |
| `CallCostCounters` | `src/hyprial/kernel/impl/facts/cost_counters.py:80` | sync object; method blocking UNKNOWN | `class(self, keys: Iterable[str], *, wall: bool)` | no | 9 |
| `Candidate` | `src/hyprial/kernel/impl/facts/tier_vocab.py:31` | sync object; method blocking UNKNOWN | `class(harness: str; provider: str \| None; model: str)` | no | 1 |
| `canonical_agent_uri` | `src/hyprial/kernel/impl/primitives/uri.py:66` | sync; blocking UNKNOWN | `(owner: str, machine: str, actor: str) -> str` | no | 21 |
| `canonical_orgfs_uri` | `src/hyprial/kernel/impl/primitives/uri.py:113` | sync; blocking UNKNOWN | `(owner: str, space_id: str, node_id: str) -> str` | no | 2 |
| `canonical_user_uri` | `src/hyprial/kernel/impl/primitives/uri.py:341` | sync; blocking UNKNOWN | `(owner: str) -> str` | no | 32 |
| `Capability` | `src/hyprial/kernel/impl/facts/capabilities.py:7` | sync object; method blocking UNKNOWN | `class` | no | 7 |
| `capped_exponential` | `src/hyprial/kernel/impl/primitives/backoff.py:22` | sync; blocking UNKNOWN | `(base: float, cap: float, exponent: int) -> float` | no | 7 |
| `channel_generation` | `src/hyprial/kernel/impl/contracts/channels/channel.py:27` | sync; blocking UNKNOWN | `(*, build_version: object, protocol_version: object) -> str` | no | 2 |
| `CHANNEL_HEARTBEAT_INTERVAL_SECONDS` | `src/hyprial/kernel/impl/contracts/channels/channel.py:13` | data | `1.0` | no | 3 |
| `CHANNEL_LIVENESS_TTL_SECONDS` | `src/hyprial/kernel/impl/contracts/channels/channel.py:15` | data | `CHANNEL_HEARTBEAT_INTERVAL_SECONDS * CHANNEL_HEARTBEAT_MISS_BUDGET` | no | 3 |
| `CHANNEL_PROTOCOL_VERSION` | `src/hyprial/kernel/impl/contracts/channels/channel.py:9` | data | `2` | no | 4 |
| `CHANNEL_URI_PREFIX` | `src/hyprial/kernel/impl/primitives/uri.py:228` | data | `'channel:'` | no | 3 |
| `ChannelConfiguration` | `src/hyprial/kernel/impl/configuration/persistent_config.py:144` | sync object; method blocking UNKNOWN | `class(gateways: tuple[LarkGatewayConfig, ...]; version: int)` | no | 6 |
| `ChannelRouteConfig` | `src/hyprial/kernel/impl/configuration/persistent_config.py:34` | sync object; method blocking UNKNOWN | `class(name: str; type: str; native_id: str \| None; nickname: str \| None; members: tuple[str, ...])` | no | 1 |
| `child_state_environment` | `src/hyprial/kernel/impl/configuration/home.py:95` | sync; blocking UNKNOWN | `(hyprial_home: Path, state_dir: Path) -> dict[str, str]` | no | 5 |
| `CommandSink` | `src/hyprial/kernel/impl/contracts/ports.py:45` | sync object; method blocking UNKNOWN | `protocol(submit(self, command: CommandT) -> PortAdmission)` | yes | 8 |
| `configured_hyprial_home` | `src/hyprial/kernel/impl/configuration/home.py:34` | sync; blocking UNKNOWN | `(environ: Mapping[str, str] \| None=None) -> tuple[Path, str]` | no | 15 |
| `configured_harness_state_dir` | `src/hyprial/kernel/impl/configuration/home.py:58` | sync; blocking UNKNOWN | `(environ: Mapping[str, str] \| None=None, *, home_selector: Callable[[], Path] \| None=None) -> Path` | yes | 1 |
| `connect_named_pipe` | `src/hyprial/kernel/impl/platform/ports.py:34` | sync; blocking UNKNOWN | `(endpoint: Path, timeout: float) -> UNKNOWN` | no | 2 |
| `create_owned_process_group` | `src/hyprial/kernel/impl/platform/ports.py:11` | sync; blocking UNKNOWN | `(*, label: str) -> OwnedProcessGroup` | no | 2 |
| `current_uid` | `src/hyprial/kernel/impl/platform/privacy/account.py:10` | sync; blocking UNKNOWN | `() -> int` | no | 10 |
| `DAEMON_CLOSE_BUDGET_SECONDS` | `src/hyprial/kernel/impl/contracts/daemon/daemon_teardown.py:38` | data | `sum((seconds for _name, seconds in DAEMON_CLOSE_STEP_BUDGETS))` | no | 1 |
| `DAEMON_CLOSE_STEP_BUDGETS` | `src/hyprial/kernel/impl/contracts/daemon/daemon_teardown.py:17` | data | `(('autoupdate', 5.0), ('maintenance-scheduler', 5.0), ('service-connect', 5.0), ('lifecycle-manager', 5.0), ('lifecycle-port:agent', 5.0), ('lifecycle-port:session', 5.0), ('lifecycle-port:harness', 5.0), ('route-registration', 5.0), ('harnesses', 5.0), ('operator-alarms', 5.0), ('delivery-hooks', TURN_HOOK_CLOSE_TIMEOUT_SECONDS), ('hook-bus', TURN_HOOK_CLOSE_TIMEOUT_SECONDS), ('hooks-consumers', TURN_HOOK_CLOSE_TIMEOUT_SECONDS), ('restore-thread', 2.0), ('org-binding-cache', 5.0), ('remote-workflow', 5.0))` | no | 1 |
| `DAEMON_EXIT_BACKSTOP_SECONDS` | `src/hyprial/kernel/impl/contracts/daemon/daemon_teardown.py:8` | data | `15.0` | no | 1 |
| `DAEMON_STARTUP_PHASES` | `src/hyprial/kernel/impl/contracts/daemon/daemon_diagnostics.py:35` | data | `frozenset((phase.value for phase in DaemonStartupPhase))` | no | 1 |
| `DaemonLaunchResult` | `src/hyprial/kernel/impl/contracts/daemon/daemon_launch.py:33` | sync object; method blocking UNKNOWN | `class(ok: bool; started: bool; already_running: bool; running: bool; pid: int \| None; epoch: str \| None; phase: str \| None; status: Mapping[str, Any])` | no | 4 |
| `DaemonRequestError` | `src/hyprial/kernel/impl/contracts/daemon/ipc_errors.py:45` | sync object; method blocking UNKNOWN | `class(self, code: str, message: str, data: Any \| None=None)` | no | 47 |
| `DaemonStartupPhase` | `src/hyprial/kernel/impl/contracts/daemon/daemon_diagnostics.py:8` | sync object; method blocking UNKNOWN | `class` | no | 3 |
| `DEADLINE_NODE` | `src/hyprial/kernel/impl/facts/pac_dispatch_vocab.py:3` | data | `'deadline'` | no | 2 |
| `DEFAULT_GIT_URL` | `src/hyprial/kernel/impl/facts/update_source.py:7` | data | `FORGEJO_GIT_URL` | no | 1 |
| `default_hyprial_home` | `src/hyprial/kernel/impl/configuration/home.py:28` | sync; blocking UNKNOWN | `() -> tuple[Path, str]` | no | 1 |
| `darwin_group_has_live_members` | `src/hyprial/kernel/impl/processes/owned_process.py:198` | sync; blocking UNKNOWN | `(process_group_id: int) -> bool \| None` | no | 1 |
| `DEFAULT_PEER_PORT` | `src/hyprial/kernel/impl/contracts/channels/forwarding.py:8` | data | `7447` | no | 2 |
| `DEFAULT_POLICIES` | `src/hyprial/kernel/impl/actor_runtime/policies.py:42` | data | `Mapping[str, SupervisionPolicy]` | no | 5 |
| `delivery_address_error` | `src/hyprial/kernel/impl/primitives/uri.py:281` | sync; blocking UNKNOWN | `(value: str) -> str \| None` | no | 2 |
| `DISPATCH_SERVICE_ACTOR_NAME` | `src/hyprial/kernel/impl/contracts/reserved_actors.py:22` | data | `'dispatch-coordinator'` | no | 1 |
| `is_agent_id_segment` | `src/hyprial/kernel/impl/primitives/uri.py:50` | sync; blocking UNKNOWN | `(value: str) -> bool` | no | 0 |
| `is_device_id_segment` | `src/hyprial/kernel/impl/primitives/uri.py:44` | sync; blocking UNKNOWN | `(value: str) -> bool` | no | 1 |
| `is_identity_id_segment` | `src/hyprial/kernel/impl/primitives/uri.py:56` | sync; blocking UNKNOWN | `(value: str) -> bool` | no | 8 |
| `is_user_id_segment` | `src/hyprial/kernel/impl/primitives/uri.py:35` | sync; blocking UNKNOWN | `(value: str) -> bool` | no | 1 |
| `DesiredStateError` | `src/hyprial/kernel/impl/facts/launch_spec.py:26` | sync object; method blocking UNKNOWN | `class` | no | 12 |
| `DomainEffectClaim` | `src/hyprial/kernel/impl/facts/lifecycle_vocab.py:67` | sync object; method blocking UNKNOWN | `class(operation_id: str; attempt_token: str; changed: bool; created_by_operation: bool; resource_token: str)` | no | 6 |
| `DrainReport` | `src/hyprial/kernel/impl/actor_runtime/contracts.py:91` | sync object; method blocking UNKNOWN | `class(complete: bool; elapsed: float; remaining: tuple[ActorHandle, ...])` | no | 3 |
| `DurationParseError` | `src/hyprial/kernel/impl/primitives/duration.py:17` | sync object; method blocking UNKNOWN | `class` | no | 4 |
| `EffectCompleted` | `src/hyprial/kernel/impl/actor_runtime/effects.py:35` | sync object; method blocking UNKNOWN | `class(operation_id: str; generation: int; result: ResultT \| None; error: str \| None)` | no | 47 |
| `EffectLane` | `src/hyprial/kernel/impl/actor_runtime/effects.py:50` | sync object; method blocking UNKNOWN | `class(self, *, name: str, execute: Callable[[RequestT], ResultT], complete: Callable[[EffectCompleted[ResultT]], AdmissionResult], capacity: int=32, workers: int=1, retry_seconds: float=0.01, cpu_accounting: RuntimeCpuCounters=runtime_cpu_counters)` | yes | 46 |
| `EffectRequest` | `src/hyprial/kernel/impl/actor_runtime/effects.py:28` | sync object; method blocking UNKNOWN | `class(operation_id: str; generation: int; payload: RequestT)` | no | 44 |
| `DEFAULT_LANE_QUEUE` | `src/hyprial/kernel/impl/hooks/bus.py:37` | data | `256` | no | 1 |
| `EVENT_NAMES` | `src/hyprial/kernel/impl/hooks/events.py:39` | data | `frozenset[str]` | no | 0 |
| `EventSink` | `src/hyprial/kernel/impl/contracts/ports.py:49` | sync object; method blocking UNKNOWN | `protocol(publish(self, event: EventT) -> None)` | yes | 7 |
| `ExpectedActorError` | `src/hyprial/kernel/impl/actor_runtime/contracts.py:37` | sync object; method blocking UNKNOWN | `class(self, code: str, detail: str)` | no | 2 |
| `EXTERNAL_IO` | `src/hyprial/kernel/impl/actor_runtime/policies.py:11` | data | `'external_io'` | no | 3 |
| `FORGEJO_GIT_URL` | `src/hyprial/kernel/impl/facts/update_source.py:5` | data | `'ssh://git@git.internal.example.com/HyprialOS/harness-bridge.git'` | no | 1 |
| `FORWARDING_COMMAND_ENV` | `src/hyprial/kernel/impl/contracts/channels/forwarding.py:10` | data | `'HYPRIAL_FORWARDING_COMMAND'` | no | 5 |
| `FORWARDING_UP_ENV` | `src/hyprial/kernel/impl/contracts/channels/forwarding.py:11` | data | `'HYPRIAL_FORWARDING_UP'` | no | 5 |
| `GRAPH_END_STATES` | `src/hyprial/kernel/impl/hooks/events.py:64` | data | `frozenset[str]` | no | 0 |
| `GenerationScheduler` | `src/hyprial/kernel/impl/actor_runtime/scheduler.py:20` | sync object; method blocking UNKNOWN | `class(self, timer_factory: TimerFactory=threading.Timer)` | yes | 8 |
| `GraphEndState` | `src/hyprial/kernel/impl/hooks/events.py:69` | data | `Literal['completed', 'failed', 'cancelled']` | no | 0 |
| `graph_closed_event_payload` | `src/hyprial/kernel/impl/hooks/events.py:204` | sync; blocking UNKNOWN | `(*, graph_id: str, end_state: GraphEndState \| None, closed_by: str \| None, reason_ref: str \| None, closed_at_ms: int, fidelity: TurnFidelity='observed') -> Mapping[str, Any]` | no | 1 |
| `HOOK_EVENT_SCHEMA_VERSION` | `src/hyprial/kernel/impl/hooks/events.py:15` | data | `2` | no | 1 |
| `HOOKS_CONFIG_NAME` | `src/hyprial/kernel/impl/hooks/config.py:19` | data | `'hooks.json'` | no | 1 |
| `HookBus` | `src/hyprial/kernel/impl/hooks/bus.py:161` | sync object; method blocking UNKNOWN | `class(self, *, logger: Callable[..., None] \| None=None, monotonic: Callable[[], float]=time.monotonic)` | yes | 2 |
| `HookEvent` | `src/hyprial/kernel/impl/hooks/events.py:268` | sync object; method blocking UNKNOWN | `class(event: str; kind: HookKind; emitted_at_ms: int; seq: int; source: str; actor: str \| None; graph_id: str \| None; payload: Mapping[str, Any]; schema_version: int)` | no | 4 |
| `HookInterceptPort` | `src/hyprial/kernel/impl/hooks/intercept.py:70` | sync object; method blocking UNKNOWN | `class(self, *, invoke: InterceptInvoker, logger: Callable[..., None] \| None=None, monotonic: Callable[[], float]=time.monotonic, max_concurrent: int=DEFAULT_MAX_CONCURRENT_INTERCEPTS)` | yes | 2 |
| `HookEventPublisher` | `src/hyprial/kernel/impl/hooks/publisher.py:21` | sync object; method blocking UNKNOWN | `class(self, bus: HookBus, *, source: str, clock_ms: Any=None)` | yes | 10 |
| `HookRegistration` | `src/hyprial/kernel/impl/hooks/config.py:28` | sync object; method blocking UNKNOWN | `class(events: frozenset[str]; mode: HookMode; consumer: str \| None; handler: str \| None; timeout_ms: int; queue: int; options: Mapping[str, Any])` | no | 2 |
| `HooksConfig` | `src/hyprial/kernel/impl/hooks/config.py:39` | sync object; method blocking UNKNOWN | `class(hooks: tuple[HookRegistration, ...])` | no | 1 |
| `InterceptDecision` | `src/hyprial/kernel/impl/hooks/intercept.py:50` | sync object; method blocking UNKNOWN | `class(ready: bool; text: str \| None; outcome: str)` | no | 0 |
| `InterceptRequest` | `src/hyprial/kernel/impl/hooks/intercept.py:32` | sync object; method blocking UNKNOWN | `class(key: str; owner: str; handler: str; timeout_ms: int; document: Mapping[str, Any] \| Callable[[], Mapping[str, Any]])` | yes | 1 |
| `GUEST_WORKER_STATE` | `src/hyprial/kernel/impl/contracts/channels/worker_channel.py:13` | data | `'/run/hyprial-worker'` | no | 1 |
| `HarnessLaunchSpec` | `src/hyprial/kernel/impl/facts/launch_spec.py:50` | sync object; method blocking UNKNOWN | `class(harness: str; name: str; headless: bool; args: tuple[str, ...]; ownership: str; nickname: str \| None; cwd: str \| None; endpoint: str \| None; session_ref: str \| None; command: tuple[str, ...]; turn_timeout_seconds: float \| None; idle_timeout_seconds: float \| None; containerized: bool; pinned_owner: str \| None; container_image: str \| None; execution_runtime: SmolvmRuntimeSpec \| None; model_provider: str \| None; model: str \| None; status: str)` | no | 47 |
| `harden_private_path` | `src/hyprial/kernel/impl/platform/privacy/private_access.py:163` | sync; blocking UNKNOWN | `(path: Path, *, mode: int) -> None` | no | 6 |
| `has_execution_runtime` | `src/hyprial/kernel/impl/contracts/execution/execution_runtime.py:110` | sync; blocking UNKNOWN | `(value: object) -> bool` | no | 3 |
| `IDENTITY_BINDING_JWKS_TTL_SECONDS` | `src/hyprial/kernel/impl/contracts/daemon/lifecycle_budgets.py:52` | data | `300.0` | no | 1 |
| `IDENTITY_BINDING_PUBLISH_RETRY_MAX_SECONDS` | `src/hyprial/kernel/impl/contracts/daemon/lifecycle_budgets.py:50` | data | `300.0` | no | 1 |
| `IDENTITY_BINDING_PUBLISH_RETRY_SECONDS` | `src/hyprial/kernel/impl/contracts/daemon/lifecycle_budgets.py:49` | data | `5.0` | no | 1 |
| `IDENTITY_BINDING_PUBLISH_SKEW_SECONDS` | `src/hyprial/kernel/impl/contracts/daemon/lifecycle_budgets.py:48` | data | `30.0` | no | 1 |
| `IDENTITY_BINDING_PUBLISH_WARNING_INTERVAL_SECONDS` | `src/hyprial/kernel/impl/contracts/daemon/lifecycle_budgets.py:51` | data | `60.0` | no | 2 |
| `IDENTITY_BINDING_UNKNOWN_KID_REFRESH_INTERVAL_SECONDS` | `src/hyprial/kernel/impl/contracts/daemon/lifecycle_budgets.py:53` | data | `60.0` | no | 1 |
| `HYPRIALHomeNotInitialized` | `src/hyprial/kernel/impl/configuration/home.py:11` | sync object; method blocking UNKNOWN | `class(self, path: Path, source: str)` | no | 3 |
| `initialize_hyprial_home` | `src/hyprial/kernel/impl/configuration/home.py:75` | sync; blocking UNKNOWN | `(*, ensure_dispatch_policy: Callable[[Path], object]) -> Path` | yes | 1 |
| `INTERCEPT_EVENT_NAMES` | `src/hyprial/kernel/impl/hooks/events.py:67` | data | `frozenset[str]` | no | 0 |
| `ipc_errors` | `hyprial.kernel.impl.contracts.daemon` | module/data | `module` | no | 177 |
| `is_session_fetch` | `src/hyprial/kernel/impl/contracts/execution/session.py:23` | sync; blocking UNKNOWN | `(params: Mapping[str, object]) -> bool` | no | 1 |
| `is_windows_owned_process_group` | `src/hyprial/kernel/impl/platform/ports.py:20` | sync; blocking UNKNOWN | `(process_group: object) -> bool` | no | 2 |
| `lark_recovery_coverage` | `src/hyprial/kernel/impl/contracts/channels/lark.py:6` | sync; blocking UNKNOWN | `() -> dict[str, str]` | no | 2 |
| `LarkGatewayConfig` | `src/hyprial/kernel/impl/configuration/persistent_config.py:81` | sync object; method blocking UNKNOWN | `class(name: str; app_id: str; credential_ref: str; routes: tuple[ChannelRouteConfig, ...]; default_route: str \| None)` | no | 6 |
| `LaneProjection` | `src/hyprial/kernel/impl/hooks/bus.py:49` | sync object; method blocking UNKNOWN | `class(name: str; events: tuple[str, ...]; accepted: int; overloaded: int; closed: int; delivered: int; failed: int; shut: bool)` | no | 1 |
| `legacy_user_uri` | `src/hyprial/kernel/impl/primitives/uri.py:359` | sync; blocking UNKNOWN | `(owner: object) -> str` | no | 4 |
| `LIFECYCLE_IPC_MARGIN_SECONDS` | `src/hyprial/kernel/impl/contracts/daemon/lifecycle_budgets.py:31` | data | `10.0` | no | 3 |
| `LIFECYCLE_OPERATION_DEADLINE_SECONDS` | `src/hyprial/kernel/impl/contracts/daemon/lifecycle_budgets.py:22` | data | `70.0` | no | 6 |
| `LIFECYCLE_WAIT_MARGIN_SECONDS` | `src/hyprial/kernel/impl/contracts/daemon/lifecycle_budgets.py:30` | data | `10.0` | no | 6 |
| `LifecycleMutationCompleted` | `src/hyprial/kernel/impl/facts/lifecycle_vocab.py:56` | sync object; method blocking UNKNOWN | `class(correlation_id: str; attempt_token: str; generation: int; version: int; domain: str; provenance: MutationProvenance; payload: object; replayed: bool)` | no | 11 |
| `LifecycleMutationRequest` | `src/hyprial/kernel/impl/facts/lifecycle_vocab.py:29` | sync object; method blocking UNKNOWN | `class(correlation_id: str; attempt_token: str; operation_id: str; expected_resource_token: str \| None; payload: object; expected_entity_token: str \| None)` | no | 24 |
| `listen_named_pipe` | `src/hyprial/kernel/impl/platform/ports.py:42` | sync; blocking UNKNOWN | `(endpoint: Path, *, gui_write_sid: str \| None=None) -> UNKNOWN` | no | 1 |
| `linux_group_has_live_members` | `src/hyprial/kernel/impl/processes/owned_process.py:161` | sync; blocking UNKNOWN | `(process_group_id: int) -> bool \| None` | no | 1 |
| `lock_exclusive` | `src/hyprial/kernel/impl/platform/file_lock.py:20` | sync; blocking UNKNOWN | `(descriptor: int, *, blocking: bool=True) -> None` | no | 14 |
| `Logger` | `src/hyprial/kernel/impl/log/__init__.py:284` | sync object; method blocking UNKNOWN | `class(self, state_dir: Path, *, component: str, name: str, route: LogRoute='component', runtime: str \| None=None, rotation_hook: RotationHook \| None=None, writer: LogWriter \| None=None)` | yes | 27 |
| `ManagedHarnessProcess` | `src/hyprial/kernel/impl/processes/process_facts.py:28` | sync object; method blocking UNKNOWN | `protocol(running(self) -> bool; pid(self) -> int \| None; liveness(self) -> ProcessLiveness; stop(self) -> None)` | no | 10 |
| `MAX_FRAME_BYTES` | `src/hyprial/kernel/impl/contracts/channels/worker_channel.py:15` | data | `8 * 1024 * 1024` | no | 2 |
| `migrate_pre_trajectory_logs` | `src/hyprial/kernel/impl/log/__init__.py:210` | sync; blocking UNKNOWN | `(state_dir: Path) -> LogMigrationResult \| None` | no | 1 |
| `MutationProvenance` | `src/hyprial/kernel/impl/facts/lifecycle_vocab.py:12` | sync object; method blocking UNKNOWN | `class(created_by_operation: bool; changed: bool; resource_token: str)` | no | 17 |
| `OFFICIAL_GIT_URL` | `src/hyprial/kernel/impl/facts/update_source.py:3` | data | `'https://github.com/Hyprial/hyprial.git'` | no | 1 |
| `ObserverHook` | `src/hyprial/kernel/impl/hooks/bus.py:42` | sync object; method blocking UNKNOWN | `protocol(observe(self, event: HookEvent) -> None)` | yes | 1 |
| `operation_key` | `src/hyprial/kernel/impl/facts/pac_dispatch_vocab.py:13` | sync; blocking UNKNOWN | `(routine_name: str, task_uuid: str, occurrence_slot_ms: int \| None=None) -> str` | no | 2 |
| `ORGFS_CONTENT_PENDING` | `src/hyprial/kernel/impl/contracts/daemon/ipc_errors.py:241` | data | `'content-pending'` | no | 2 |
| `ORGFS_URI_PREFIX` | `src/hyprial/kernel/impl/primitives/uri.py:109` | data | `'orgfs:'` | no | 1 |
| `orgfs_web_url` | `src/hyprial/kernel/impl/primitives/uri.py:203` | sync; blocking UNKNOWN | `(value: str, *, network: OrgfsWebNetwork) -> str \| None` | no | 1 |
| `OrgfsWebNetwork` | `src/hyprial/kernel/impl/primitives/uri.py:161` | sync object; method blocking UNKNOWN | `class(web_host: str \| None; magic_dns_suffix: str \| None; fqdn: str \| None)` | no | 1 |
| `OTHER_KEY` | `src/hyprial/kernel/impl/facts/cost_counters.py:27` | data | `'(other)'` | no | 1 |
| `OwnedProcessGroup` | `src/hyprial/kernel/impl/processes/owned_process.py:248` | sync object; method blocking UNKNOWN | `class(self, *, label: str)` | no | 3 |
| `PAC_GC_INTERVAL_SECONDS` | `src/hyprial/kernel/impl/contracts/pac_gc.py:7` | data | `5 * 60.0` | no | 1 |
| `PAC_GC_JITTER_RATIO` | `src/hyprial/kernel/impl/contracts/pac_gc.py:8` | data | `0.1` | no | 1 |
| `PAC_GC_MAX_REMOVALS_PER_PASS` | `src/hyprial/kernel/impl/contracts/pac_gc.py:9` | data | `50` | no | 1 |
| `PAC_WORKTREE_GIT_TIMEOUT_SECONDS` | `src/hyprial/kernel/impl/contracts/daemon/lifecycle_budgets.py:41` | data | `30.0` | no | 2 |
| `parse_agent_uri` | `src/hyprial/kernel/impl/primitives/uri.py:77` | sync; blocking UNKNOWN | `(value: str) -> tuple[str, str, str] \| None` | no | 53 |
| `parse_channel_uri` | `src/hyprial/kernel/impl/primitives/uri.py:238` | sync; blocking UNKNOWN | `(value: str) -> tuple[str, str, str] \| None` | no | 8 |
| `parse_duration` | `src/hyprial/kernel/impl/primitives/duration.py:21` | sync; blocking UNKNOWN | `(value: object, label: str) -> float` | no | 5 |
| `parse_execution_runtime` | `src/hyprial/kernel/impl/contracts/execution/execution_runtime.py:106` | sync; blocking UNKNOWN | `(value: object) -> SmolvmRuntimeSpec \| None` | no | 1 |
| `parse_orgfs_uri` | `src/hyprial/kernel/impl/primitives/uri.py:139` | sync; blocking UNKNOWN | `(value: str) -> tuple[str, str, str] \| None` | no | 4 |
| `parse_route_uri` | `src/hyprial/kernel/impl/primitives/uri.py:270` | sync; blocking UNKNOWN | `(value: str) -> tuple[str, str] \| None` | no | 4 |
| `parse_user_uri` | `src/hyprial/kernel/impl/primitives/uri.py:370` | sync; blocking UNKNOWN | `(value: str) -> str \| None` | no | 10 |
| `resolve_node_id` | `src/hyprial/kernel/impl/primitives/node_identity.py:13` | sync; blocking UNKNOWN | `(environ: Mapping[str, str] \| None=None, *, hostname: Callable[[], str] \| None=None, warn: Callable[[str], object] \| None=None) -> str` | yes | 9 |
| `PersistentConfigError` | `src/hyprial/kernel/impl/configuration/persistent_config.py:13` | sync object; method blocking UNKNOWN | `class` | no | 8 |
| `PersistentConfigStore` | `src/hyprial/kernel/impl/configuration/persistent_config.py:208` | sync object; method blocking UNKNOWN | `class(self, hyprial_home: Path, state_dir: Path)` | no | 5 |
| `parse_linux_process_stat` | `src/hyprial/kernel/impl/processes/owned_process.py:139` | sync; blocking UNKNOWN | `(stat: str) -> tuple[str, int, str] \| None` | no | 1 |
| `PersistentConfiguration` | `src/hyprial/kernel/impl/configuration/persistent_config.py:203` | sync object; method blocking UNKNOWN | `class(channels: ChannelConfiguration; users: UserProfiles)` | no | 1 |
| `PortAdmission` | `src/hyprial/kernel/impl/contracts/ports.py:22` | sync object; method blocking UNKNOWN | `class` | no | 38 |
| `PortCommandRejected` | `src/hyprial/kernel/impl/contracts/ports.py:31` | sync object; method blocking UNKNOWN | `class(correlation_id: str; domain: str; generation: int; version: int; code: str; detail: str; admission: PortAdmission \| None)` | no | 32 |
| `RequestPortError` | `src/hyprial/kernel/impl/contracts/ports.py:8` | sync object; method blocking UNKNOWN | `class(self, code: str, message: str, data: object \| None=None)` | no | 5 |
| `RUNTIME_OVERFLOW_OWNER` | `src/hyprial/kernel/impl/facts/cost_counters.py:52` | data | `'_overflow'` | no | 1 |
| `RuntimeCpuCounters` | `src/hyprial/kernel/impl/facts/cost_counters.py:187` | sync object; method blocking UNKNOWN | `class(self, *, max_keys: int=_RUNTIME_MAX_KEYS, excluded_owners: Iterable[str]=())` | no | 1 |
| `runtime_cpu_counters` | `src/hyprial/kernel/impl/facts/cost_counters.py:276` | data | `RuntimeCpuCounters(excluded_owners=_RUNTIME_EXCLUDED_OWNERS)` | no | 2 |
| `PRE_TRAJECTORY_ARCHIVE` | `src/hyprial/kernel/impl/log/__init__.py:77` | data | `'archive-pre-trajectory'` | no | 1 |
| `path_is_reparse` | `src/hyprial/kernel/impl/platform/privacy/private_access.py:105` | sync; blocking UNKNOWN | `(metadata: os.stat_result) -> bool` | no | 3 |
| `posix_metadata_enforced` | `src/hyprial/kernel/impl/platform/privacy/account.py:21` | sync; blocking UNKNOWN | `() -> bool` | no | 5 |
| `PrivateAccessUnsupported` | `src/hyprial/kernel/impl/platform/privacy/private_access.py:81` | sync object; method blocking UNKNOWN | `class` | no | 6 |
| `private_metadata_violation` | `src/hyprial/kernel/impl/platform/privacy/private_access.py:113` | sync; blocking UNKNOWN | `(path: Path, metadata: os.stat_result, *, mode: int \| None) -> str \| None` | no | 11 |
| `private_tree_violation` | `src/hyprial/kernel/impl/platform/privacy/private_access.py:143` | sync; blocking UNKNOWN | `(path: Path, metadata: os.stat_result) -> str \| None` | no | 3 |
| `probe_process` | `src/hyprial/kernel/impl/platform/process.py:6` | sync; blocking UNKNOWN | `(pid: int) -> None` | no | 2 |
| `process_birth_identity` | `src/hyprial/kernel/impl/processes/owned_process.py:232` | sync; blocking UNKNOWN | `(pid: int) -> str \| None` | no | 2 |
| `PROCESS_CPU_PROBE_TIMEOUT_SECONDS` | `src/hyprial/kernel/impl/contracts/daemon/lifecycle_budgets.py:37` | data | `1.0` | no | 1 |
| `PROCESS_FORCE_KILL_SECONDS` | `src/hyprial/kernel/impl/processes/owned_process.py:23` | data | `0.25` | no | 2 |
| `PROCESS_FORCE_TERM_SECONDS` | `src/hyprial/kernel/impl/processes/owned_process.py:22` | data | `0.25` | no | 0 |
| `PROCESS_LIFECYCLE` | `src/hyprial/kernel/impl/actor_runtime/policies.py:12` | data | `'process_lifecycle'` | no | 3 |
| `ProcessLiveness` | `src/hyprial/kernel/impl/processes/process_facts.py:10` | sync object; method blocking UNKNOWN | `class(state: ProcessLivenessState; observed: bool; pid: int \| None; marker: str \| None; detail: str \| None)` | no | 6 |
| `ProcessLivenessProbeError` | `src/hyprial/kernel/impl/processes/process_facts.py:23` | sync object; method blocking UNKNOWN | `class` | no | 5 |
| `ProcessLivenessState` | `src/hyprial/kernel/impl/processes/process_facts.py:17` | sync object; method blocking UNKNOWN | `class` | no | 5 |
| `ReadinessReport` | `src/hyprial/kernel/impl/contracts/daemon/readiness.py:41` | sync object; method blocking UNKNOWN | `class(phase: str; verdict: str; source: str)` | no | 9 |
| `delivery_event_payload` | `src/hyprial/kernel/impl/hooks/events.py:150` | sync; blocking UNKNOWN | `(*, delivery_id: str, sender: str, conversation: str, message: str \| None=None, outcome: str \| None=None, failure_code: str \| None=None) -> Mapping[str, Any]` | no | 1 |
| `redact_turn_excerpt` | `src/hyprial/kernel/impl/hooks/events.py:74` | sync; blocking UNKNOWN | `(value: str, *, enforce_output_bound: bool=True) -> str` | no | 3 |
| `redact` | `src/hyprial/kernel/impl/log/__init__.py:125` | sync; blocking UNKNOWN | `(value: Any, *, key: str \| None=None) -> Any` | no | 6 |
| `reply_message_id` | `src/hyprial/kernel/impl/contracts/execution/session.py:29` | sync; blocking UNKNOWN | `(message_id: str) -> str` | no | 1 |
| `require_initialized_hyprial_home` | `src/hyprial/kernel/impl/configuration/home.py:49` | sync; blocking UNKNOWN | `() -> Path` | no | 1 |
| `RESERVED_AGENT_ACTOR_NAMES` | `src/hyprial/kernel/impl/contracts/reserved_actors.py:28` | data | `frozenset({DISPATCH_SERVICE_ACTOR_NAME, TURN_HOOKS_ACTOR_NAME})` | no | 4 |
| `resolve_hostname` | `src/hyprial/kernel/impl/processes/_dsh_resolver.py:14` | sync; blocking UNKNOWN | `(sender: Connection, host: str, port: int) -> None` | no | 1 |
| `route_path` | `src/hyprial/kernel/impl/log/__init__.py:144` | sync; blocking UNKNOWN | `(state_dir: Path, *, route: LogRoute, component: str, name: str, runtime: str \| None=None) -> Path` | no | 1 |
| `ROUTE_URI_PREFIX` | `src/hyprial/kernel/impl/primitives/uri.py:262` | data | `'route:'` | no | 1 |
| `safe_channel_build_version` | `src/hyprial/kernel/impl/contracts/channels/channel.py:21` | sync; blocking UNKNOWN | `(value: object) -> bool` | no | 2 |
| `safe_turn_identifier` | `src/hyprial/kernel/impl/hooks/events.py:91` | sync; blocking UNKNOWN | `(value: str) -> str` | no | 1 |
| `safe_turn_tool_names` | `src/hyprial/kernel/impl/hooks/events.py:95` | sync; blocking UNKNOWN | `(values: tuple[str, ...]) -> list[str]` | no | 0 |
| `SESSION_CARRIER_SOURCES` | `src/hyprial/kernel/impl/contracts/execution/session.py:12` | data | `frozenset({'claude-channel', 'dsh-channel', 'pi-extension', 'codex-app-server'})` | no | 4 |
| `SERVICE_CONTROL_TIMEOUT_SECONDS` | `src/hyprial/kernel/impl/contracts/channels/forwarding.py:13` | data | `3.0` | no | 3 |
| `SERVICE_RECONCILE_INTERVAL_SECONDS` | `src/hyprial/kernel/impl/contracts/channels/forwarding.py:15` | data | `5.0` | no | 1 |
| `SERVICE_REGISTRY_READ_TIMEOUT_SECONDS` | `src/hyprial/kernel/impl/contracts/channels/forwarding.py:14` | data | `1.0` | no | 1 |
| `session_fetch_params` | `src/hyprial/kernel/impl/contracts/execution/session.py:17` | sync; blocking UNKNOWN | `() -> dict[str, bool]` | no | 2 |
| `short_actor_name` | `src/hyprial/kernel/impl/primitives/uri.py:313` | sync; blocking UNKNOWN | `(value: str) -> str` | no | 7 |
| `SmolvmRuntimeSpec` | `src/hyprial/kernel/impl/contracts/execution/execution_runtime.py:11` | sync object; method blocking UNKNOWN | `class(smolvm_path: str; smolvm_sha256: str; rootfs_path: str; rootfs_digest: str; resize2fs_path: str; resize2fs_sha256: str; guest_command: tuple[str, ...]; guest_python: str; smolvm_bundle_digest: str)` | no | 3 |
| `STATE_AUTHORITY` | `src/hyprial/kernel/impl/actor_runtime/policies.py:10` | data | `'state_authority'` | no | 2 |
| `STATE_DONE` | `src/hyprial/kernel/impl/facts/pac_dispatch_vocab.py:5` | data | `'done'` | no | 2 |
| `STATE_ESCALATED` | `src/hyprial/kernel/impl/facts/pac_dispatch_vocab.py:7` | data | `'escalated'` | no | 2 |
| `SupervisionPolicy` | `src/hyprial/kernel/impl/actor_runtime/policies.py:16` | sync object; method blocking UNKNOWN | `class(max_restarts: int; restart_window: float; base_backoff: float; max_backoff: float; jitter_ratio: float)` | no | 2 |
| `SupportLevel` | `src/hyprial/kernel/impl/facts/capabilities.py:21` | sync object; method blocking UNKNOWN | `class` | no | 5 |
| `TARGET_KIND_AGENT` | `src/hyprial/kernel/impl/primitives/uri.py:391` | data | `'agent'` | no | 5 |
| `TARGET_KIND_CHANNEL_ROUTE` | `src/hyprial/kernel/impl/primitives/uri.py:394` | data | `'channel_route'` | no | 3 |
| `TARGET_KIND_HOST` | `src/hyprial/kernel/impl/primitives/uri.py:392` | data | `'host'` | no | 7 |
| `TARGET_KIND_UNKNOWN` | `src/hyprial/kernel/impl/primitives/uri.py:395` | data | `'unknown'` | no | 1 |
| `TARGET_KIND_USER` | `src/hyprial/kernel/impl/primitives/uri.py:393` | data | `'user'` | no | 2 |
| `TEARDOWN_BUDGETED_SECONDS` | `src/hyprial/kernel/impl/contracts/daemon/daemon_teardown.py:45` | data | `DAEMON_CLOSE_BUDGET_SECONDS + DAEMON_EXIT_BACKSTOP_SECONDS` | no | 4 |
| `tier_for_model` | `src/hyprial/kernel/impl/facts/tier_vocab.py:67` | sync; blocking UNKNOWN | `(model: str \| None) -> str \| None` | no | 2 |
| `TIERS` | `src/hyprial/kernel/impl/facts/tier_vocab.py:45` | data | `dict[str, tuple[Candidate, ...]]` | no | 6 |
| `TURN_HOOK_CLOSE_TIMEOUT_SECONDS` | `src/hyprial/kernel/impl/contracts/daemon/daemon_teardown.py:13` | data | `5.0` | no | 1 |
| `TURN_HOOKS_ACTOR_NAME` | `src/hyprial/kernel/impl/contracts/reserved_actors.py:25` | data | `'_turn-hooks'` | no | 2 |
| `turn_event_payload` | `src/hyprial/kernel/impl/hooks/events.py:106` | sync; blocking UNKNOWN | `(*, delivery_id: str, sender: str, conversation: str, started_at_ms: int, ended_at_ms: int, status: str, tool_names: tuple[str, ...], output: str, prompt: str, tool_calls: tuple[tuple[str, bool], ...], fidelity: TurnFidelity, failure_code: str \| None=None) -> Mapping[str, Any]` | no | 1 |
| `usage_refreshed_payload` | `src/hyprial/kernel/impl/hooks/events.py:185` | sync; blocking UNKNOWN | `() -> Mapping[str, Any]` | no | 1 |
| `unlock` | `src/hyprial/kernel/impl/platform/file_lock.py:41` | sync; blocking UNKNOWN | `(descriptor: int) -> None` | no | 9 |
| `uri` | `hyprial.kernel.impl.primitives` | module/data | `module` | no | 3 |
| `user_agent` | `src/hyprial/kernel/impl/primitives/outbound.py:29` | sync; blocking UNKNOWN | `(component: str) -> str` | no | 4 |
| `with_user_agent` | `src/hyprial/kernel/impl/primitives/outbound.py:37` | sync; blocking UNKNOWN | `(headers: Mapping[str, str] \| None, agent: str) -> dict[str, str]` | no | 4 |
| `uri_scheme` | `src/hyprial/kernel/impl/primitives/uri.py:326` | sync; blocking UNKNOWN | `(value: str) -> str \| None` | no | 6 |
| `windows_process_identity` | `src/hyprial/kernel/impl/platform/ports.py:50` | sync; blocking UNKNOWN | `(pid: int) -> str` | no | 1 |
| `WORK_NODE` | `src/hyprial/kernel/impl/facts/pac_dispatch_vocab.py:9` | data | `'work'` | no | 2 |
| `WorkerBinding` | `src/hyprial/kernel/impl/contracts/channels/worker_channel.py:48` | sync object; method blocking UNKNOWN | `class(actor: str; session_ref: str; daemon_epoch: str)` | no | 3 |
| `WorkerChannelError` | `src/hyprial/kernel/impl/contracts/channels/worker_channel.py:41` | sync object; method blocking UNKNOWN | `class(self, code: str, message: str)` | no | 1 |
| `worker_cleanup_complete_event_payload` | `src/hyprial/kernel/impl/hooks/events.py:241` | sync; blocking UNKNOWN | `(*, graph_id: str, actor_node: str, actor_name: str, operation_id: str, at_ms: int, worker_state: str='stopped', fidelity: TurnFidelity='observed') -> Mapping[str, Any]` | no | 1 |
