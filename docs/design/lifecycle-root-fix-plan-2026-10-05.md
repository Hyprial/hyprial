# 生命周期操作根治方案（2026-10-05）

来源：PAC 图 wf-43a162e3 节点 `plan`。Allen 原话：「pac 失效的临时解决办法放行，然后让 hyprial-cli-developer 规划根治，基于最新的 dev」。
基线：dev b132c5a9（0.5.0 + #1130 + #1131 + #1132）。本文只给方案，不含实现。
生产读数全部由 hq-adjutant 代取（本节点不读写生产 `~/.hyprial`）。

## 结论

1. **0.5.0 上 PAC 起不来，主因不是「库里有卡住的旧行」，而是每一步生命周期都慢到 80 秒走不完。**
   - 10-05 17:30 重启后的新 daemon 上（之前卡住的行已被手工收口），单节点冒烟流程仍在 80 秒处超时。
   - 同一个 0.5.0 程序在全新的隔离 daemon 上，一次 create（含失败回滚）只要 0.6–1.0 秒。
   - 所以问题随生产状态的规模出现，代码路径本身没有错。
2. **慢会自我放大。**
   - 操作超时后进入补偿，补偿本身又是一串生命周期步骤。
   - 等结果的调用者每 50 ms 就往唯一的串行写入者塞一条读。
   - 卡在补偿里的操作还占着同一 actor 的资源，后续的 down/start 只能排在它后面。
3. **hq 最初的三个假设：一个被推翻，一个成立，一个部分成立。**
   - **跨代回执永远等不到：在 0.5.0 的 forward 步骤上被推翻。**
     - `LifecycleCoordinator` 构造时调用 `interrupt_running_operations`，把上一代所有 RUNNING 行转为 COMPENSATING（`SAGA_INTERRUPTED_BY_RESTART`，`lifecycle/lifecycle_coordinator.py:340`）。
     - 新一代不会把它们当作前进中的操作继续等回执。
   - **补偿没有时限：成立。**
     - COMPENSATING 分支遇到 `LifecycleStepUnresolved` 只做 `pass`（`lifecycle_coordinator.py:169`）。
     - 带死线的 `_settle_unresolved` 只在 forward 分支调用。
     - 生产实例：kanban 的 `lifecycle-start:587e1f80` 的 harness.ensure 补偿一直停在 dispatched。
   - **单线程串行队列：部分成立。**
     - 协调器有 4 个 effect worker、容量 32，并不是单线程。
     - 但同一 actor 的操作按资源串行（`_held`）。
     - 而且**所有日志读写都经过唯一的 `StatePersistenceAuthority`**，真正被串行化的是它。
4. **新发现的一条有害行为：超时后的补偿把一个仍在运行的 harness 的 Agent 绑定解掉了。**
   - hq 读到 4 条被补偿的 start：harness.ensure 的补偿是 `RemoveHarnessCommand`，结果为 changed=0（空操作）；agent.bind 的补偿是 `ReleaseAgentCommand`，结果为 changed=1。
   - 结果是 harness 还在跑，Agent 却没有绑定，和卡片 979d6df3（晚落定的 worker 被当成 actor_unowned）同形。

## 读数

| 编号 | 读数 | 来源 |
|---|---|---|
| R1 | 新 daemon（epoch f57fc7e6，17:30 起）上的冒烟操作 `pac-start:99c2a2bc`：09:32:50Z 提交；09:33:22Z 才到 agent.home；09:33:34Z codex 连接器就绪；09:34:11Z 在 80667 ms 处判为未落定。 | `daemon.jsonl`（只读） |
| R2 | 同一 0.5.0 程序、全新隔离 daemon（`HYPRIAL_NETWORK_ISOLATED=1`）：6 次 create（含回滚）每次 0.6–1.0 s。worker 被 codex 配置校验挡下，没真正跑起来，所以「就绪之后那一段」没有对比。 | 本机隔离实验 |
| R3 | 99c2a2bc 的 agent.create、route.persona.ensure、agent.bind、harness.ensure 四步 forward 全部 completed，之后被判超时进入补偿。排在它前面的 3 条 create 也都超时后 compensated。 | hq 代取 lifecycle-operations 副本 |
| R4 | `lifecycle_operations` 共 2443 行（completed 2209 / compensated 205 / failed 26 / compensating 3）；`lifecycle_effects` 10740 行；`pending_session_agent_effects` 69 行。`inbox.sqlite3` 204 MB。 | 同上 |
| R5 | `hyprial down/start kanban` 期间每 2 s 取一份栈，共 15 份：12 份里唯一的调用线程停在 `lifecycle_coordinator.result → settled_journal → StatePersistenceAuthority._call` 的等待上；15 份里没有任何 effect worker 在执行 `advance`；actor 线程几乎全部空闲。 | `diag-050-2026-10-05/dumps-lifecycle.txt` |
| R6 | 同期 macOS `sample`：462 个线程；等 GIL 的平均约 0.6 个线程；在 CPU 上的时间分散，没有单一热点。 | `sample3.txt` |
| R7 | 4 条被补偿 start 的 effect 行：harness.ensure 补偿 completed、changed=0；agent.bind 补偿 completed、changed=1；其中 kanban 那条的 harness 补偿仍 dispatched。 | hq 代取 |

R5 加 R6 的含义：一次读的往返很长，但写入者自己并不忙，CPU 也不集中在哪个函数上。
**延迟花在排队和调度上，不是花在 SQL 上。** 现有读数还分不出排在哪一段（runtime 的全局 `tell` 锁？mailbox？线程调度？），所以下面 c 的第一步是测量，不是猜。

## a. 根因复现（规格；本节点未执行）

- **A1 补偿无时限。**
  - 做法：在隔离 daemon 或单元级协调器上，用一个从不回执补偿的假 harness 端口，起一条 create 并让它超时进入补偿。然后对同一 actor 提交 down。
  - 现状（预期红）：补偿永远停在 COMPENSATING；down 在 `_held` 后面等满自己的死线。
  - 修好后：补偿到死线判 FAILED 并告警，释放资源，down 正常走完。
- **A2 读放大。**
  - 做法：给 `StatePersistenceAuthority` 包一层计数，跑一条 5 步 create。
  - 记录：`ReadJournalQuery` 的条数；`wait()` 期间每秒的读数。
  - 预期现状：每次 `advance` 只推进一步，却重新读一遍全部 effect 状态；`wait()` 每 50 ms 读一次。
- **A3 晚成功补偿解绑。**
  - 做法：假 harness 端口在死线之后才回报 ensure 成功。
  - 断言：补偿结束后 harness 仍在，而 Agent 绑定被释放。现状为红，即复现了 R7。

## b. 修法

- **B1 补偿有死线。**
  - COMPENSATING 分支在超过 `LIFECYCLE_OPERATION_DEADLINE_SECONDS` 后，对未结算的补偿步骤走与 forward 相同的出口。harness 用 `fail_lifecycle`。
  - agent、session、route 三个域不等回执，**直接向域读当前真值**，据此把这一步记为完成或未完成。
    这依赖三个域都能回答「这一步的效果现在在不在」；本文未逐一核实，S2 实施的第一步就是核实，答不了的域只能判 FAILED 并告警。
  - 到时仍无法确认的，整条操作判 FAILED，发一条 `actor_alert`，并释放 `_held` 资源。
  - 从此一条卡住的操作只影响它自己。
- **B2 跨代补偿不等死去那一代的回执。**
  - 保留 U0c「重启后补偿、不续跑」。
  - 补偿步骤若带着上一代的 correlation，同样按 B1 向域查询真值，不再等那个永远不会来的回执。
- **B3 补偿要整体一致：harness 没移除，就不解 Agent 绑定。**
  - 补偿按逆序执行；harness.ensure 的补偿若返回 changed=0（harness 仍在，或已被更新一代接管），就把它视为「资源不归本操作所有」。
  - 对应的 agent.bind 补偿跳过，并记一条事件。
  - 这条与慢无关，单独就该修。
- **B4 晚成功不算失败（与 PAC 的契约）。**
  - 现在调用方在 80 s 处拿到「未落定」，PAC 记为 launch_failed，而操作随后自己补偿掉了已经成功的 worker。
  - 方向：调用方的等待与操作的死线分开。PAC 启动改为跟随操作的终态（COMPLETED 即成功，哪怕晚到），不再用固定 80 s 判失败。
  - 这改变 PAC 启动的语义，实施前请 Allen 确认。

## c. 慢的部分

- **C0 先测量。** 在 `StatePersistenceAuthority` 和 actor runtime 上加指标，进 `hyprial ps` 和 ipc stats：
  - mailbox 深度；
  - 每条命令的排队时长与执行时长，按命令类型和来源统计；
  - runtime 全局锁（`guardian.tell` / `_command_started` 共用一把锁，锁内还有一次 `uuid4()`）的等待和持有时长。
  - 有了这些读数，才能判断 R5 那段往返时间花在哪。
- **C1 读不经过写入者。**
  - `ReadJournalQuery` 和 `ReadDesiredQuery` 是只读的；SQLite 在 WAL 下，读连接能看到已提交的状态。
  - 读走独立的只读连接，不进唯一写入者的 mailbox。写仍然只走写入者。
  - 这一条预计是最大的一块：一次 create 的往返几乎都是读。
- **C2 等待不轮询。** `LifecycleCoordinator.wait()` 改为等协调器内部的终态通知（它在 `DomainCompleted` 时已经持有结果），不再每 50 ms 读一次日志。
- **C3 恢复扫描事件驱动。** `_recover_loop` 每 200 ms 调一次 `pending()` 并逐条 `load()`，改为只在重启、提交、终态时扫描。
- **与 b 是否同根？** 部分同根。慢让操作超时，超时引出补偿，补偿又加负载，形成正反馈。只修 b 能止血，只修 c 能降低触发概率，两者都要做。

## d. 存量收口（不再手改库）

- 新版本启动时，在 `interrupt_running_operations` 之后加一道收口：
  - 对所有 COMPENSATING 行按 B1/B2 处理：向域查询真值，能判定的落定，判定不了的判 FAILED 并告警。
  - 打一行统计日志：按 kind、state、原因计数。
- 同时做一次一致性巡检：找出「harness 在跑而 Agent 无绑定」（R7 形态）的 actor。处理方式是重新绑定，或发告警交人裁决。
  - 不自动停进程，因为它们可能正在干活。
- `pending_session_agent_effects` 的 69 行是否属于积压，实施 S6 时一并判定，判据写进测试。

## e. 实施拆分与验收

每一项都要有能让它变红的突变。顺序按「先止血，再提速，再收口」。

| 拆分 | 内容 | 验收 | 突变（应变红） |
|---|---|---|---|
| S1 | C0 指标：写入者队列、排队与执行时长、runtime 锁 | `hyprial ps --json` 出现这些字段；单元测试注入延迟后读数随之变化 | 去掉记录调用 |
| S2 | B1 补偿死线 + FAILED + 告警 + 释放资源（含 B2 跨代） | A1 由红变绿：同一 actor 的后续操作在死线后能执行 | 删掉补偿分支的死线检查 |
| S3 | B3 harness 未移除则不解绑 | A3 由红变绿 | 恢复无条件 `ReleaseAgentCommand` |
| S4 | C1 读走只读连接 | A2：一次 create 期间写入者 mailbox 收到的读命令数为 0；在写入者注入 200 ms 延迟后，create 的耗时不随之线性增长 | 把读路由回写入者 |
| S5 | C2 + C3 等待与恢复不轮询 | 操作进行中，`wait()` 期间日志读次数为 0；空闲时恢复扫描次数为 0 | 恢复 50 ms 和 200 ms 轮询 |
| S6 | d 存量收口 + 一致性巡检 | 用 0.4.6 形态的种子行（COMPENSATING、带上一代 correlation）启动，启动后无 COMPENSATING 行，并有统计日志 | 跳过收口步骤 |
| S7 | B4 PAC 跟随操作终态（需 Allen 确认） | 晚到的 COMPLETED 被 PAC 记为成功，worker 不被补偿 | 恢复固定 80 s 判失败 |

建议先做 S2 和 S3（止血，互相独立），S1 与它们并行。看到 S1 的读数后再做 S4、S5，最后做 S6 和 S7。

## 待验证项

- R7 中 harness 移除为何是空操作：harness 已经不在，还是被代际或资源令牌挡住。要看 harness actor 对那几个 correlation 的回执。
- R5 中一次读往返的时间花在哪一段。由 S1 回答。
- 「online」不能当活着的证据：e2e-verifier、squire、introducer 的心跳年龄都约 2129 s。它们能否干活，以一条需回复的消息为准（hq-adjutant 在跟）。
