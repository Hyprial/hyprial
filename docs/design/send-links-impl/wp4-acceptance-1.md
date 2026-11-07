# WP4 第一次验收（opus，2026-10-14）：REJECTED，返工清单

验收对象：wp4/send-links @ c37852d4a，base 210287b40。焦点 406 passed、结构门与 ruff 过、全量 11 failed 全部在 base 上复现（无回归）。被拒原因是规格 §10 的阻断项只做了一部分。以下每条都必须在返工中落实，并在 PR 正文/证据文件里逐条对应。

## 不符项（必须全部修）

1. 【先红证据】分支上没有只含测试的提交（892f765c6 混合了产品与测试）。要求：补一个只含测试的提交置于实现之前（可用 git rebase -i 拆分，或在当前 HEAD 之上先回退实现再分两步重提交），并在证据文件记录在 base 上运行新测试的先红输出；至少 B2（标记旁路）与 B3（盖 fetched / W 重复计数）要有行为级的红，不只是 ImportError。
2. 【Contract-Impact】新增 delivery_status 的 `state="refused"`、`reason=LINK_UNKNOWN`、`ConsumptionState.refused` 是已发布字段语义变更，contract/ 未同步。要求：在 docs/evidence/wp4-send-links-implementation-2026-10-14.md 里写 `Contract-Impact: <放行理由>`（WP8 同步 contract/），并列出受影响的 contract/ 文件。
3. 【唯一缺省，§2.2.5】`inbox/service/core.py:80` 的 `link_admission` 默认 None，`receive.py` 的 `_link_ingress_code` 与 `projection.py` 的 `_admit_dispatch` 把 None 当作「无 gate」放行，这是第二套缺省。要求：InboxService 默认 `link_admission or LinkAdmission(...)`（未绑定即 fail-closed），裸 service 的既有测试显式绑定 `permissive_admission_slot()`；`git grep "_link_admission is None"` 为空；`git grep "link_admission or LinkAdmission("` 恰 2 处。
4. 【B1.5 回复边界】只测了「被拒的回复保留」。补：无 replyTo、replyTo 类型错、指向别人的关联、已关闭的关联、link 已回收、以及 W 满时有效回复经三个入口（list、wait、headless）都被放行。
5. 【B3.4/B3.5】线程断言只覆盖 receive 与 list；补 wait 与 headless 路径的线程断言；补一个 list/wait/headless 并发测试（真线程）。
6. 【B4.4/B4.5】补「事务回滚时不发 notice」测试；dedup 要在接收端结果上断言，不只比较发送端 id；拒收 notice 暂存 deque 的 maxlen=256 会静默丢弃，改为无界或溢出时记日志并测。
7. 【B5】补：生产 offer coordinator 的队头阻塞不发生（用假 gate 即可，不需要 WP2）；回收排干；等待 TTL 过期；W 收缩后重评；W 与出站共享。
8. 【B6】`inboundAwaitingInjection` 在绑定 gate 时只来自内存 `_held`，首次 list 或 tick 评估前为 0，重启后为 0：改为从 inbox 行加只读 governs 与评估状态计算；`read.py` 的 `held_unfetched_count` 补 `fetched_at_ms IS NULL` 过滤；补 ack 与 TTL 清理测试。
9. 【T17】`test_one_slot_is_shared` 手工注入槽，没有走 `transport.py` 的真实接线也没核 bridge 的槽。改为经真实 `_startup_compose_runtime_bridge` 路径（验收者用 monkeypatch 组合的探针模式可行），断言 coordinator 的 `service_options["link_admission"]` 与 bridge 的槽是同一对象且进程内只有一个 LinkAdmission。
10. 【验收 6 的字面】`admission.py:156,182` 出现 `admit_send`/`settle_hint` 字样（是测试替身 E2EPermissiveLinkGate 的方法定义）。把 E2EPermissiveLinkGate 移出产品代码（放 tests/support/links/gate.py），`HYPRIAL_E2E_PERMISSIVE_LINK_GATE` 这个开关若保留必须在读取处打 warn 日志，并在证据里写明它只用于隔离测试、WP2 接线后删除。

## 建议（不计入拒绝，做了要在证据里写）
- `held_message_ids` 在 IPC 线程迭代 OrderedDict 而 actor 线程在改：加锁或快照。
- 两条回复路径（`inbox_surface/send.py:318-330` 的 message.send 带 replyTo 只设 intent=reply 不写 body replyTo；`runtime/forwards.py:717` 的 headless 自动回复）不写 body 的 replyTo，接收方拿到 reply_to=None。前者属 WP3-A（裁定 39），后者随 WP5 删除。本包不改，但证据里要记。

## 新裁定（集成者，README 第 51 条）
daemon 内部 actor（`dispatch-coordinator`、`_turn-hooks`）作为发送方或接收方都不受 link 约束，按 actor 保留名判、不限节点；`link_governed` 据此同时检查 sender 与 recipient。
