# WP5-A 第一次验收：REJECTED

- 验收：opus（claude-opus-5-5），2026-10-15
- 对象：分支 `wp5/send-links`，worktree `/private/tmp/hf-wp5-send-links`，HEAD `9438bdf2a`；比较基 `e043d4a06`
- 依据：wp5.md v2（§9.4/§9.6 优先，§11 已并入）、README 裁定 56–66、71、72，design-send-links-2026-10-14.md §8.1/§8.2，`links/ports.py`
- 结论：实现主体正确（硬切、机制应答身份、settle_hint_for 单一判定、turn IPC 去重、seq 临界区、失败重试资格都按规格落地，且我用变异探针证实 seq 临界区本身有效），但 B8 的测试不能发现 B8 回归、M4 的「真未提交」分支是空测、WORM 红证据被覆盖删除、§1.4 预算 7 处超出。返工量小，见文末清单。

## 一、逐项结论

1. 范围与禁区：PASS。`git diff e043d4a06..HEAD --stat` 48 个文件；§8 第 6 条最后一条 diff（links、kernel、biz、shell、harnesses/pi、harnesses/codex、mcp、wiring/transport.py）无输出；另查 `codex_carrier_store.py`、`interactive_carrier_runtime.py`、`ipc/wire.py`、`contract/`、`pac/`、`inbox/links/io.py`、`dispatch/remote/`、`capabilities.py`、`pi_harness_*` 均未改。与 WP5-D（`e043d4a06..wp5d/send-links`，21 个文件）文件交集为空。`dispatch/remote/__init__.py` 999 行未动。
2. 7 文件门：PASS。carrier 4、inbox/contracts 5、sessions 4、tests carrier 4/headless 4/turns 3；新测试文件最大 580 行。`check_structural_limits.py` 与 `check_module_boundaries.py` 均 `"violations": []`。
3. §1.4 预算：FAIL（7 处超出，均未压缩其它代码）。results.py 730（预算 ≤700）；forwards.py 977（≤950）；turn_delivery/protocol.py 108（≤100）；reconcile.py +25（≤+15）；inbox/service/projection.py +38（≤+20）；inbox/contracts/ports.py +4（≤+3）；dispatch/admission.py +5（≤+4）。其余在预算内：contract.py 232、settlement.py 53、turns.py 224、registry.py 877、queries.py −10、router.py +3、hooks/service.py 730、runtime/settlement.py 482、runtime/__init__.py 41、bridge.py +0、facade.py +2、read.py +6、lifecycle.py +1。表外改动：application/__init__.py +6（现 998 行，距上限 2 行）、inbox/tracking/progress.py 文档 −1。
4. 硬切（裁定 31、§3.1）：PASS。§8 第 6 条五条 grep 全部无输出；`src/hyprial` 中 `_reply_and_ack`、`hyprial-agent-sdk-reply`、`_reply_already_*`、`_pending_reply_results` 零命中。§11.3 审计正则命中数由 47 降到 43，新增 `intent="reply"` 只在 `forwards.py` 的 `_answer_hook_request`（753 行）与 `_answer_mechanism_result`（805 行）两处；§8 第 7 条：两函数定义 2、`_settle_mechanism_answer(` 3、`settle_hint_for` 在 queries.py 与 results.py 各 3、`_publish_lock` 2 行、`completed_turn` 在 ports.py 1 行、两份派发查询各含 `fetched_at_ms IS NULL` 与 `s.terminal = 0 AND s.next_attempt_ms <= ?`。
5. 机制应答（裁定 58/59、B6）：PASS（测试有缺口，见第 9、10 项）。`is_mechanism_hook_request` = 权威 intent 为 request + payload 标记 + `parse_agent_uri(sender)[2] == "_turn-hooks"`；reconcile 的原文注入与 `delivery.hook_request` 都改由它导出，不再信 payload 标记。python_worker 判定用 `mechanism_of(_harness_kind(actor))`，即 `support(h, headless=True, HEADLESS_EXEC).mechanism`，不按 harness 名；forward 帧在机制分派之前已走原 forward 路径。应答 id 为 `uuid5(NAMESPACE_URL, "hyprial-turn-hook-reply:"|"hyprial-mechanism-reply:" + id)`，payload 带 `replyTo`、`origin.mechanism`，summary 由 `summary_from_text` 派生（空输出用 "(empty result)"），conversationId 保留，submit 确认后才 ack；超时留在 `_pending_settle_results` 以同一结果同一 id 重试，receipt conflict 视为已提交直接 ack。M1 用真 `InboxTurnHookInvoker.invoke` + 真 bridge delivery/result 路径 + 真 DeliveryCustodyFacade，handler 为确定性回声 connector（返回 HarnessResult，不往 inbox 塞 reply），符合 B6.6。注册守卫（裁定 63）已在基线：`kernel/impl/contracts/reserved_actors.py` 与 identity 注册入口。
6. headless 结算（§3.2–§3.3）：PASS（重启子情形有规格缺口，见探针 P4）。两处调用点各写同一段：`fetch_pending(only={id}, completed_turn=True)` → 权威 pending 行 → `settle_hint_for(gate=LinkAdmission.current_gate())` → ack 或不发；超时进 `_pending_settle_results`，reconcile 不派发其中 id。派发查询加 B1 条件；`completed_turn=True` 在同一 `with self._db:` 事务内盖 COALESCE 戳并删非 terminal `harness_failure_settlements`，不动 attempts 与 terminal 行；无 id 时抛 ValueError。H9（service 与 authority 两参数）、H10、R1、R2 三断点均绿。
7. settle_hint_for 单一判定（B5）：PASS。位于 `inbox/contracts/settlement.py`，四条规则与 §2.4 逐字一致；IPC 端 `_ipc_message_settle_hint` 与 headless 端调同一函数，返回体字段不变；`application/__init__.py` 的 `link_admission` 与 bridge 的 `_link_admission` 是同一实例（wiring/transport.py:869）。contract.py、settlement.py、turns.py 无任何 runtime 名或 `CARRIER_RUNTIMES`；K8 以 `zz_test_interactive` 注册会话，started/ended、settle_hint、ack 全通。
8. turn IPC（§4）：PASS（B8 的测试 FAIL，见第 9 项）。`session.turn.started` 新增、`session.turn.ended` 去掉 claude_interactive 限定，两者都经 `_fence_interactive_session` + 当前会话检查；按 `(actor, sessionRef)` 的 `CarrierTurnLedger`（started/ended 各 LRU 1024）在记账锁内判定去重，发布在锁外；ended 首次接受即固定 `endedAtMs`，重复报告返回同一时刻不再发事件；载体自报 seq/source/时刻不读；`_publish` 在 `_publish_lock` 内先取 seq 再 `bus.publish`。A3 不变量写在 wp5.md §4.5，T6(b) 关旧 bus、新 consumer 工厂、复用同一 inbox 文件，无 stale_replay。SESSION_SUPERSEDED 由 fence 返回（T2）；载体侧停止重试属 WP5-C。
9. B8 测试：FAIL。T11 的 monkeypatch 在**分配之前**睡眠（代码注释写「分配之后」），无锁时也不会出现 `[1, 0]`；T12 是压力测试。变异探针：在 scratch 副本把 `with self._publish_lock:` 改为 `if True:`，`test_turn_seq.py` 三条连跑 3 次全部通过。我写的探针（分配之后睡眠）在无锁变体上得到 `assert [1, 0] == [0, 1]`，在 HEAD 上通过。即实现正确、测试不守护。另 T11 未按规格断言「无 stale_replay」（只数提醒条数）。
10. 测试清单与数量：部分 FAIL。§7.1–§7.6 的 46 个名字全部存在，唯一测试名 50 个（多出 `test_injection_from_inbox_uses_summary_and_body_size`、`test_settle_hint_for_reply_and_event_ack_without_a_gate`、`test_settle_hint_for_invalid_gate_answer_is_keep_and_logged`、`test_expired_kept_row_does_not_block_new_work`）；`BLOCKED-on-WP2` 在 test_keep_recovery.py 计 5，R5、R6 为 `xfail(strict=True, reason="BLOCKED-on-WP2")`，R6 的 TTL 部分拆为真实断言的 `test_expired_kept_row_does_not_block_new_work`，理由成立。缺口：(a) M4 参数 `committed=False` 与 True 完全相同——`_TimesOutFirstSubmit` 两种参数都先真提交再抛超时，「真未提交后重试提交 1 条」从未被测（B6.7 明文要求）；最后的 `flaky.submits == (2 if committed else 2)` 也说明这一点。(b) M3 的 user-proxy 是脚本化回声 harness，输出由测试直接塞 `MISSING_RECIPIENT_HINT`，收件方是 `adapter:lark:...` 的 inbox 行，没有穿过真 python_worker（`_user_proxy_worker`）也没有穿过用户投递边界（B6.7 要求「必须穿过 python_worker 与用户投递边界，并检查人的提示原文」）。(c) M1 断言的是测试自己用 `TURN_HOOK_CALLBACK_ACTOR` 拼出的 URI，而生产接线 `wiring/transport.py:605` 用的是 kernel 的 `TURN_HOOKS_ACTOR_NAME`；protocol.py 另写了一份字面量 `"_turn-hooks"`，两者没有任何测试钉住相等（§1.4 写明「`_turn-hooks` 的常量化由测试钉住（M1）」）。(d) H9 以 `inbox.fetch_pending(only=...)` 代替 `message.read_body` IPC 盖戳，语义等价，接受。
11. §7.8 改写：大体 PASS。§9.3 的 9 条与 test_harness_reply_settlement.py、test_consumed_result_settlement.py 已改；`turn reports require the current claude_interactive session` 在 tests 中零命中。未改两处：`tests/daemon/delivery/dedup/test_reply_dedup_outlives_the_retry_schedule.py` 文档仍以 `_reply_and_ack` 为前提（测试本身仍绿、所测性质仍对机制应答成立）；`tests/integration/e2e/e2e006/test_e2e006_deepseek_approval.py:81` 仍以旧前缀构造「合成回复」做守卫（绿，作为对旧前缀的拒绝守卫仍有意义）。两者只需改文字。
12. 运行：焦点集 `tests/daemon/harnesses tests/daemon/application tests/daemon/progress tests/daemon/forwarding tests/biz/hooks/reminder tests/daemon/inbox tests/tooling/architecture`（`-n 8`）：1 failed、1956 passed、2 skipped、2 xfailed；唯一失败为已知 pi controlled_loader t04-06。`tests/daemon tests/shell`（`-n 8`）：9 failed、8267 passed、6 skipped、7 xfailed、1 xpassed。9 条 = 已知 3 条（pi controlled_loader t04-06、ipc cost heartbeat ratio、login/device t11）+ GUI 5 条 + 1 条 flake：GUI lifecycle 5 条在 `e043d4a06` 上同样失败（本机 Node v26.4.0、src/gui 依赖未装，环境原因，BLOCKED 非 PASS）；`test_codex_carrier_settlement.py::test_transient_daemon_errors_never_settle_terminally` 单独重跑 2 次均绿，判为并行负载下的 flake。另跑 §7.8 两个文件与 tests/daemon/turns：111 passed、1 xpassed。carrier 目录：52 passed、2 xfailed。ruff：All checks passed。`git diff --check e043d4a06 HEAD -- ':!docs/evidence'`：无输出。
13. authority 豁免：PASS。`tests/inbox_authority_exemptions.json` 61 → 65 条，净 +4，与裁定 66 一致：新增 `results.py DaemonEventBridge._complete_harness_results fetch_pending`、`results.py DaemonEventBridge._settle_claimed_result_business fetch_pending`（裁定 61）、`forwards.py DaemonEventBridge._answer_hook_request ack`、`… submit`（B6 新 owner）；`_reply_and_ack` 的 ack/submit 两条改名为 `_answer_mechanism_result` 的 ack/submit；`_submit_loud` 只改 reason。`authority_migration.py` 对应 4 键改名/新增 2 键，`test_authority_migration.py` 计数与摘要同步更新。`"operation": "fetch_pending"` 1 → 3（+2）。
14. 证据：FAIL。`9438bdf2a` 把 `a4d599f66` 提交的 `wp5a/red.txt`（41 行，真实的先红输出）整体覆盖为一行「UNKNOWN」，违反 docs 证据只追加（WORM）。该行还写「093702546（headless/turns）precede implementation」，事实相反：headless/turns 测试在实现提交 `1332b6bcc` 之后才提交。§8 要求的 `turn-events.json`、`blocked.txt` 缺失。
15. 历史：偏差可接受（有条件）。没有按裁定 71 在完成时整理提交；`1332b6bcc`（只有 src）与 `093702546`（只有测试与 §7.8 改写）主题为「wip」但各自可读、边界清楚。§8 第 10 条要求的「测试提交早于实现」对 K/G 组成立（a4d599f66 早于 aa2fdb8c7），对 headless/turns 组不成立；下表的先红证据替代之，接受，不要求重切历史。

## 二、先红证据（本次建立，替代缺失的 red.txt）

方法：在 scratch worktree 检出对应提交的 src，覆盖 HEAD 的 `tests/daemon/harnesses/carrier`，用本 worktree 的 `.venv`（`uv run --project`、`PYTHONPATH=<scratch>/src`，已核 `hyprial.__file__` 指向 scratch）。

| 代码状态 | 结果 | 说明 |
| --- | --- | --- |
| `e043d4a06`（a4d599f66 的父提交，实施前） | contract、headless_carrier、mechanism_answers 三文件收集即 `ModuleNotFoundError: …carrier.contract`；boundaries + keep_recovery + turns：22 failed、1 passed（G2 守护）、2 xfailed | K1–K9 先红形状与 §7.1 一致；G1/G3/G7 为 FileNotFoundError 或「still contains _reply_and_ack」，与 a4d599f66 原 red.txt 一致 |
| `aa2fdb8c7`（§2 契约已有、runtime/turns 未实现） | mechanism_answers 收集失败（缺 `TURN_HOOK_CALLBACK_ACTOR`） | 仅缺一个常量 |
| `aa2fdb8c7` + 仅补该常量（c2p） | 35 failed、2 errors（H9 两参数 teardown fd 泄漏，同时 FAILED）、17 passed、2 xfailed | 见下 |
| `1332b6bcc`（093702546 的父提交，实现已在） | 52 passed、2 xfailed | 证明 headless/turns 测试晚于实现提交 |

c2p 上的 17 条通过：K1–K7 及三条 K 补充、G1（这些在 e043d4a06 上为红，因 aa2fdb8c7 已实现 §2）；G2、H4、H6、H8、H10、M1 为规格标明的守护。无「非守护却在实施前为绿」的测试。
c2p 上红的形状：H1 存在 reply 行；H2 `set() == {'r1'}`（被 ack）；H3 无 ack；H5 message 等于正文；H7、R2×3 `AttributeError: '_pending_settle_results'`；H9 非 terminal 失败结算行未清（`1 == 0`）；R1 keep 被 ack；R6-TTL、R7 断言失败；M2 收到原文 `secret body`；M3–M5 id 不等（旧前缀）；M6 产生了 reply；T1–T10、K8 `unknown daemon method session.turn.started`/`METHOD_NOT_FOUND`；K9 `session.turn.started` 不在 `_IPC_STATS_METHODS`；T11、T12 红因 `observe_carrier_turn` 不存在，而不是乱序（见第 9 项）。
M1 三段复现（§7.4）：①基线 c2p 上 M1 绿（守护）；②在 HEAD 副本上令 `_settle_mechanism_answer` 恒返回 None（即删除自动回复、尚无机制应答）：M1 `hook exchange never completed`（handler 超时），M3、M4×2、M5 红，M2、M6 绿；③HEAD：全绿。

## 三、探针

- P1（B8）：分配之后强制停顿 0.25 秒的双线程探针：无锁变体 lane 观察到 `[1, 0]`；HEAD 递增。实现有效，现有 T11/T12 不守护（第 9 项）。
- P2（伪造 hook）：M2 已覆盖普通 agent 带 `turnHookRequest: true`：得到渲染块、`hook_request=False`、无应答、行保持 pending。只按 actor 段判定，未核 owner/machine 段；本节点的保留名由裁定 63 守住，跨节点来源的 `_turn-hooks` 是否可信属 WP2/路由准入，记 UNKNOWN。
- P3（reply 调用点）：§11.3 正则基线 47 处、HEAD 43 处，新增仅两个机制应答函数；无新的 `message.reply` 调用。
- P4（重启落在盖戳与 ack 之间）：用 reply 行模拟「completed_turn 盖戳已提交、ack 未执行即崩溃」，重建 inbox 与 bridge 后推 5 个 tick：0 次派发、行仍 pending 且已 fetched，永远不再结算。模型不重跑（正确），但 hint 为 ack 的行（reply/event，或 gate 答 ack 的请求）会一直占 pending/W 直到 TTL 或放弃阈值，与裁定 56 的初衷相反。`_pending_settle_results` 只在进程内，§3.2 只规定了进程内超时，规格没有覆盖进程崩溃；需集成者裁定是否补一次启动时的结算扫描（对 fetched 且无到期失败记录的 pending 行调 `settle_hint_for`，ack 则 ack，keep 不动）。
- P5（会话记账）：`_carrier_turn_ledgers` 只在注销与「新会话自身 key」上清除；同 actor 换代时旧 sessionRef 的记账不清，进程内按会话次数无界增长（每份 ≤2048 项）。旧报告已被 fence 拒绝，正确性无碍，属 §4.2「换代或被取代时整份清除」未落实。
- P6（authority 路径的 completed_turn）：经 authority 时 `FetchPendingCommand` 走 `fetch_pending_admitted`，轮末会再问一次 `admit_dispatch`；WP2 真 gate 下须核对这不重复占 W、gate 拒绝时不留下可再派发的未盖章行（裁定 70 同类问题）。

## 四、接受的偏差

- 历史未按裁定 71 整理、未按 §7.4 三段提交：接受。每个提交可读；先红与三段复现由本次在 scratch 中建立（第二节）。
- 测试名多出 4 条、R6 拆成 xfail 部分与 TTL 真实断言：接受。
- H9 用 `fetch_pending(only=…)` 代替 `message.read_body` 盖戳：接受（read_body 正是调这条）。
- headless 取门用 `_current_link_gate()`（`current_gate()` 加 getattr 回退）：接受，同一实例已核。
- `test_authority_migration.py` 的计数与摘要更新：接受，属豁免变更的必然同步。

## 五、merge-tree

- `refactor/org-structure-constrain`（`1a0e394ff`，含 `6619a919d`）：无冲突。合并树上结构门、边界门 `violations: []`；carrier + application + inbox 在合并树上 665 passed、8 errors，8 条全为 `test_daemon_zenoh_failloud.py` 要求 scratch 目录下存在 `.venv/bin/hyprial`（环境，不是冲突）。
- `wp5d/send-links`（`fb0de74c5`）：无冲突，文件不相交。
- `wp2b-intg/send-links`（`fe8df76de`）：1 个文件冲突 `src/hyprial/daemon/impl/inbox/service/projection.py`，3 处：
  1. `fetch_pending` 的 `with self._db:` 内：WP2-B 的 `fresh = [... _write_inbound_ledger_locked(...)]` 与 `_sink_injected` 循环 对 本包的 `self._clear_nonterminal_failures(messages, completed_turn)`；
  2. `fetch_pending_admitted` 同位置，变量为 `admitted`；
  3. 类尾：WP2-B 新增 `_inbound_ledger_meta` 对 本包新增 `_clear_nonterminal_failures`。
  三处都是两边并存即可；合并后该文件 667 行。runtime/forwards.py 977、results.py 730、application/__init__.py 998、router.py 936、hooks/service.py 730 在两种合并树上不变。

## 六、集成者合入时要做

1. 按第五节解 projection.py 三处，两边都留；合并后核 `completed_turn=True` 的轮末 fetch 对已 fetched 行不重复写入 inbound ledger，不重复占 W（P6）。
2. WP2 绑定后补 K6 的同一性断言（IPC 端 `link_admission.gate` 与 headless 端 `current_gate()` 同一 gate），并执行 R5、R6。
3. application/__init__.py 已 998 行，后续包在此加行前先拆。
4. PR 正文写 Contract-Impact、Skill-Impact（§10），列出净增 4 条 authority 豁免（第 13 项）与理由。
5. 裁定 P4（启动结算扫描）是否在本包返工或另立项。

## 七、交 WP8 的 Contract-Impact

- 新 IPC `session.turn.started`（`actor`、`sessionRef`、`reportId` ≤128、可选 `deliveryId`；返回 `{ok, startedAtMs}`）；`session.turn.ended` 接受任何当前 interactive 会话，新增可选 `status`（completed/failed）、`deliveryId`，返回体新增 `endedAtMs`；两者按 `(actor, sessionRef, reportId)` 幂等，时刻由 daemon 首次接受时固定，载体自报 seq/source/时刻不读。
- 事件 `turn.started`/`turn.completed`/`turn.failed` 覆盖 interactive 载体：source `daemon.turns`，`fidelity: observed`，sender/conversation/prompt/output 为空串；所有 `turn.*` 在同一发布临界区内取 seq。
- `message.settle_hint` 对仍 pending 的 reply/event 行答 `ack`（裁定 56）；gate 非法答案为 `keep` 并记 `message.settle_hint.invalid`。
- headless 不再自动回复：前缀 `hyprial-agent-sdk-reply:` 消失；只有两种机制应答，id 前缀 `hyprial-turn-hook-reply:`、`hyprial-mechanism-reply:`，payload 带 `replyTo` 与 `origin.mechanism`（`turn-hook`/`python_worker`），带 summary。
- 注入文本格式（§2.3）与 golden `tests/daemon/harnesses/carrier/injection_golden.json`；wire golden `WG-001` 的 `autoReply` 未改（交 WP8）。
- 日志事件：`harness.settle.deferred`、`harness.mechanism_answer.deferred` 取代 `harness.reply.deferred`，`harness.reply.already_answered` 删除。

## 八、返工清单

1. 修 T11：把停顿放在 `sequence()` 返回之后、`bus.publish` 之前（可仿 P1），并按规格用 capture logger 断言无 `reply_reminder.stale_replay`；用「去掉 `_publish_lock`」的变体证明它会红，把这次红输出追加进 red.txt。
2. 修 M4 的 `committed=False`：让包装器在不调用内层 submit 的情况下抛 `InboxAuthorityTimeout`，断言重试后恰 1 条 reply、worker 未再起 turn；删掉 `(2 if committed else 2)`。
3. 让 M3 满足 B6.7：用真 `_user_proxy_worker`（或其最小真实入口）产生 `MISSING_RECIPIENT_HINT`，并让应答穿过用户投递边界（`user:` 路径 / deliver_user），断言人收到的文本含提示原文。若真 worker 在测试中不可起，写明 BLOCKED 及原因，不以脚本化输出冒充。
4. `turn_delivery/protocol.py` 的 `TURN_HOOK_CALLBACK_ACTOR` 改为引用 kernel 的 `TURN_HOOKS_ACTOR_NAME`（或加一条测试钉住二者相等且与 wiring/transport.py 接线一致），使 M1 真正钉住生产接线。
5. 恢复 `docs/evidence/org-structure-constrain-2026-10-14/wp5a/red.txt` 被删的 41 行原文（追加回去，不改写），把错误的「093702546 precede implementation」更正为追加说明，并追加本文件第二节的表；补 `turn-events.json`（T1、T9 记录的事件全文）与 `blocked.txt`（R5、R6、P2 跨节点 UNKNOWN、GUI 环境 BLOCKED）。
6. 预算：forwards.py、results.py、protocol.py、reconcile.py、projection.py、ports.py、admission.py 七处超出。在不压缩其它代码的前提下能减的先减（例：两个机制应答函数的 submit/ack 尾段可合成一个只做提交确认的私有 helper，但要同步 authority owner 与 G3 允许表，需先问集成者）；其余超出写进 PR 正文，请集成者按实测数放宽预算。
7. `_carrier_turn_ledgers` 在同 actor 换代时清除旧 sessionRef 的记账（P5），加一条断言。
8. 改两处过时文字：`test_reply_dedup_outlives_the_retry_schedule.py` 模块文档改为机制应答函数；e2e006 第 81 行旁注明旧前缀守卫的用途。
9. 待集成者对 P4 裁定后，若要求本包处理：加启动结算扫描与一条「盖戳后崩溃、重启后 ack 行被结算、keep 行不动、模型不重跑」的测试。
