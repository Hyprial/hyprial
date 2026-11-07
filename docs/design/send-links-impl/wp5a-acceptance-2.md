# WP5-A 第二次验收：ACCEPTED

- 验收：opus（claude-opus-5-5），2026-10-15
- 对象：分支 `wp5/send-links`，worktree `/private/tmp/hf-wp5-send-links`，HEAD `ba6ae30dc`；返工提交 `0b2bcfab6`（先红测试）→ `18c7e266f`（修复、authority 表、wp5.md §1.4）→ `ba6ae30dc`（证据）；比较基 `e043d4a06`
- 依据：wp5a-acceptance-1.md 第八节 9 条返工清单与探针 P4/P5/P6；README 裁定 56–66、71、72、74（启动结算扫描）、75（预算不是门）；wp5.md §1.4、§2–§5、§7、§8
- 结论：9 条返工全部落实，且每条我都用变异或复跑证实测试能守护；第 8 节的扫描有两处非阻断的健壮性缺口，记为集成者跟进项，不返工。

## 一、逐项结论

1. T11/T12（返工 1）：PASS。停顿现在放在 `sequence()` 返回之后、`bus.publish` 之前；capture logger 断言日志中无 `reply_reminder.stale_replay`（logger 经 `reminder_factory` 真实接入 `ReplyReminderConsumer`）。变异：scratch 副本把 `hooks/service.py:427` 的 `with self._publish_lock:` 改成 `if True:`，`test_turn_seq.py` 连跑 3 次都红（`assert [2, 1] == [1, 2]`，有一次 T12 也红）；HEAD 上 3 条通过。
2. M4 committed=False（返工 2）：PASS。包装器在 committed=False 时不调用内层 submit 就抛 `InboxAuthorityTimeout`（`inner_submits == 0`）；超时后断言没有 reply、请求仍 pending；重试后恰好 1 条 reply，`harnesses.turns == 1`；`(2 if committed else 2)` 已删。变异 A（确认 helper 把任何超时都当作已提交）：两个参数化都红（`1 == 2`；`[] == ['m-4']`）。变异 B（超时直接 `return False`，不推迟）：两个参数化都红（`turns 2 == 1`）。
3. M3（返工 3）：PASS。通过 `PythonHarnessProcess` 起真实的 `_user_proxy_worker` 子进程；`MISSING_RECIPIENT_HINT` 由 worker 自己按「无收件标记」规则产生，测试没有注入；应答经 `LarkReplyBridgeTransport` 到 adapter 的 `deliver_reply`，断言 `delivery.text == MISSING_RECIPIENT_HINT`、`reply_to`、机制 id、`from_actor`、请求已 ack、只有 1 个 turn。串行跑 3 次均通过（单次 0.26–0.31 秒）；carrier 目录 `-n 8` 跑 3 次均为 54 passed、2 xfailed。等待用带期限的轮询（`_run_bridge_while`，5 秒/15 秒），不用固定 sleep，没有发现时序依赖。
4. 保留名常量（返工 4）：PASS。`protocol.py:23` 改为 `TURN_HOOK_CALLBACK_ACTOR = TURN_HOOKS_ACTOR_NAME`（从 kernel 导入）；生产接线 `wiring/transport.py:606` 用的也是 kernel 的 `TURN_HOOKS_ACTOR_NAME`；M1 用 kernel 名拼 callback URI，并断言 `TURN_HOOK_CALLBACK_ACTOR == TURN_HOOKS_ACTOR_NAME == "_turn-hooks"`。`src/hyprial` 中字面量 `"_turn-hooks"` 只剩 `kernel/impl/contracts/reserved_actors.py:25` 一处。
5. 证据 WORM（返工 5）：PASS。`git diff 9438bdf2a..HEAD -- red.txt` 只有 82 行新增，没有删除；第 2–42 行（标题行之后）与 `git show 093702546:…/red.txt` 的 41 行逐字相同；之后依次追加了更正说明（正确写明 headless/turns 测试晚于实现提交 1332b6bcc）、第一次验收第二节的表、T11 无锁变体的红输出。`turn-events.json` 有 T1、T9 两段事件全文（`turn.started`/`turn.completed`，source/fidelity 与 §4 一致）；`blocked.txt` 列出 R5、R6、P2、P6、K6 与 GUI 环境，均标 BLOCKED/UNKNOWN，没有算作 PASS。
6. 裁定 75（返工 6）：PASS。两个应答函数共用私有 helper `_confirm_mechanism_answer`（forwards.py:804），只做 submit、提交确认和 ack；`tests/inbox_authority_exemptions.json` 在 e043d4a06 为 61 条，第一次验收时 65 条，现在 64 条：`_answer_*` 的四条合并为 `_confirm_mechanism_answer` 的 ack/submit 两条，另加 `_settle_stranded_rows` 的 ack 一条（带 reason）。`authority_migration.py` 同步（26 键、23 个不同目标），`test_authority_migration.py` 的计数与摘要同步并通过。G3：`intent="reply"` 只在 forwards.py:753、794，也就是两个应答函数里构造；helper 只有这两个函数调用；G3 测试不变且通过。§1.4 的数字与 `wc -l` 逐项一致：results 782、forwards 977、protocol 108、reconcile +25、bridge +1、ports +4、projection +38、admission +5，api +0、facade +2、lifecycle +1、read +6、router +3、hooks/service 730、turns 224、registry 880。
7. P5（返工 7）：PASS。`_reset_interactive_turn_stats` 在同一 actor 换代时，于 `_interactive_turn_stats_lock` 内删除该 actor 的所有旧 sessionRef 记账；`test_started_dedup_is_per_session_and_report` 断言 session-1 的记账在注册 session-2 后消失。变异：恢复 bafa5d9fb 版 registry.py 后该测试红，HEAD 上绿。
8. 过时文字（返工 8）：PASS。dedup 测试的模块文档与 e2e006 旁注都已改。
9. 启动结算扫描（返工 9、裁定 74）：PASS（有非阻断缺口，见第三节 S1、S2）。`bridge.start()` 在 `_reconcile_harness_actors()` 之后调一次 `_settle_stranded_rows`；`start()` 有 `_started` 守卫，同一个 bridge 只跑一次。只看 streaming 与 headless actor 中已盖戳（`has_fetched`）且没有任何失败结算记录的 pending 行，每行调一次 `settle_hint_for`，只有返回 ack 时才 ack；keep 行不碰，模型不重跑；上限 256 行。「没有 `has_fetched` 时静默跳过」只影响窄测试替身：生产中唯一的构造点 `wiring/transport.py:839` 传入 `graph.inbox = DeliveryCustodyFacade`（:588），`facade.py:665` 有 `has_fetched`，`:276` 有 `harness_failure_settlement`。R8 用的就是真 facade，所以 facade 失去这两个方法时 R8 会红，生产中不会被悄悄跳过。R8 两个参数化都通过。
10. 回归（第一次验收的 PASS 项）：PASS。wp5.md §8 第 6 条 grep 全部无输出（含 links/kernel/biz/shell/pi/codex/mcp/wiring/transport.py 的 diff，`links/ports.py` 未改）；第 7 条：应答函数 2、`_settle_mechanism_answer(` 3、projection.py 中 `fetched_at_ms IS NULL` 3 处且 B1 条件 1 处、read.py 分别为 5 和 1、ports.py 中 `completed_turn` 1 行、`def settle_hint_for` 1 处、queries.py 3 处 / results.py 5 处、`_publish_lock` 2 行、`fetch_pending` 豁免 1→3。第 9 条：carrier 4、contracts 5、sessions 4 个文件；唯一测试名 51 个（50 + R8）；`BLOCKED-on-WP2` 5 处。硬切、机制应答身份、单一判定、turn IPC 不含 harness 名、`(actor, sessionRef, reportId)` 去重、epoch 不变式（T6）对应的测试全部通过。
11. 运行：PASS（已知红以外没有新红）。carrier：54 passed、2 xfailed（串行）。焦点集（`-n 8`）：1 failed、1958 passed、2 skipped、2 xfailed，唯一失败为已知的 pi controlled_loader t04-06。`tests/daemon tests/shell`（`-n 8`）：9 failed、8269 passed、6 skipped、7 xfailed、1 xpassed。其中已知 8 条：pi t04-06、ipc cost heartbeat ratio、login/device t11、GUI lifecycle 5 条（本机 Node v26、src/gui 依赖未装，环境原因，记 BLOCKED，不算 PASS）。另 1 条 `inbox/…/custody/test_custody.py::test_async_notice_fault_restarts_actor_without_rolling_back_ttl` 单独重跑 3 次均绿，且焦点集 `tests/daemon/inbox` 中也绿；本包对 inbox actor 只改了 lifecycle.py 一行，判为并行负载下的 flake。结构门、边界门 `"violations": []`；ruff 输出 All checks passed；`git diff --check e043d4a06 HEAD -- ':!docs/evidence'` 无输出。

## 二、探针

- P-A（扫描幂等）：盖戳 r1/e1/q1 后关闭，连续两次重启：两次 pending 都只剩 `{q1}`，两次派发都为空，q1 的 `fetched_at_ms` 不变。PASS。
- P-B（超过上限的 ack 行）：300 条已盖戳的 reply：第一次启动后剩 44 条，第二次启动后剩 0 条。ack 行超过上限时，后续启动能接着结算。
- P-C（keep 行占满上限）：300 条已盖戳的 request（无 gate，答 keep），排在后面的 1 条已盖戳 reply 在 3 次重启后仍 pending。见 S1。
- P-D（gate 抛异常）：`settle_hint` 抛 `RuntimeError` 的 gate 会让 `bridge.start()` 整体抛出。见 S2。

## 三、非阻断缺口（交集成者跟进，不返工）

- S1（低）：上限按「检查过的已盖戳行」计数，keep 行每次启动都会重新占用名额，而且遍历顺序固定（按 recipient 排序，再按 arrival）。已盖戳的 keep 行多于 256 条时，排在它们之后的 ack 行要等到 TTL 或放弃阈值才会离开，重启不能把它们清出去。触发条件是单节点上同时有至少 256 条已盖戳、仍被 keep 的请求，WP2 的 W 会限制这种积压；而且这些行仍有 TTL 兜底，不会真正永久滞留。建议修法：reply/event 行不需要问 gate，不占名额；或者只让 gate 调用计入名额。
- S2（低）：扫描只捕获 `InboxAuthorityTimeout`/`Unavailable`；gate 的 `settle_hint` 若抛出其它异常，会中止 daemon 启动，并且每次重启都会重复。WP2 现在的 `LinkService.settle_hint`（links/service/gate.py:159）只做内存字典查找，实际不会抛，所以这是理论风险。建议在扫描中按行捕获异常，记日志并当作 keep 处理。
- S3（建议）：G3 没有钉住「`_confirm_mechanism_answer` 只由两个应答函数调用」。authority owner 现在是这个通用 helper，以后若有别处调用它，就能在不改豁免表的情况下获得 submit/ack。现在只有两个调用点（已 grep）。建议 G3 加一行断言。
- S4（可忽略）：一条已经通过 fence 的旧会话报告，若与换代并发，可能在清理之后重建旧 key 的记账；影响最多是一份 ≤2048 项的记账，直到注销。

## 四、接受的偏差

- 豁免为 64 条（相对 61 净增 3 条），与裁定 66 的「净增 4」不同：helper 合并减 2，扫描 ack 加 1，属于裁定 74/75 的直接后果，接受。
- §1.4 七处超预算按裁定 75 写入实测数，不再返工，PR 正文须说明。
- 扫描跳过任何带失败结算记录的行（裁定写的是「到期失败记录」）：更保守；未到期的非终态记录到期后由派发路径重派，终态记录由其自身路径处理。接受。
- blocked.txt 没有列 §8 所说的「真实 TUI 联测」：它属于 WP5-C 的三个载体，接受。

## 五、merge-tree

- `refactor/org-structure-constrain`（`d78137d3a`，含 b86f425e4 与 WP5-D；集成分支已并入 WP2-B1/B2）：`projection.py` 1 个文件冲突，3 处，其余（lifecycle、facade、read、ports）自动合并。
- `wp2b-intg/send-links`（`329878c3d`）：同一文件、同 3 处，冲突块与上面逐字相同。
- `wp2b3/send-links`（`b420bd78b`）：同一文件、同 3 处，冲突块逐字相同；application/__init__.py、admission.py 自动合并。
- projection.py 三处冲突块：
  1. `fetch_pending` 的 `with self._db:` 内：集成侧是 `fresh = [... self._write_inbound_ledger_locked(...)]`，加上 with 块之外的 `for message in fresh: self._sink_injected(...)`；本包是 `self._clear_nonterminal_failures(messages, completed_turn)`。两边都保留：`fresh = [...]` 之后、仍在 with 块内加 `_clear_nonterminal_failures`，使盖戳、入站账和清失败记录在同一事务内；`_sink_injected` 循环留在 with 之外。
  2. `fetch_pending_admitted` 的同一位置，变量为 `admitted`：处理同第 1 处。
  3. 类尾：集成侧新增 `_inbound_ledger_meta`，本包新增 `_clear_nonterminal_failures`，两个方法都保留，中间空一行。
- 我在 scratch worktree 上按上述方式解决了冲突（未提交，已删除）：合并后的 projection.py 659 行；结构门、边界门 `violations: []`；`tests/daemon/harnesses/carrier tests/daemon/inbox tests/daemon/links tests/daemon/application tests/daemon/turns tests/daemon/delivery tests/biz/hooks tests/tooling/architecture`（`-n 8`）：1611 passed、4 skipped、2 xfailed、1 xpassed、8 errors，8 个 error 全部是 `test_daemon_zenoh_failloud.py` 要求 scratch 中存在 `.venv/bin/hyprial`（环境问题，与第一次验收相同）。authority 豁免表在合并树上自动合并，摘要测试通过。

## 六、集成者合入时要做

1. 按第五节解 projection.py 三处，两边都留；合并后跑 carrier、inbox、links、tooling/architecture。
2. 合并后注意语义交点：headless streaming 路径派发时不盖 `fetched_at_ms`，所以这类行第一次 fetch 就是轮末的 `completed_turn=True`。也就是说，WP2-B 的入站账（`_write_inbound_ledger_locked`，INSERT OR IGNORE，幂等）与 `_sink_injected` 对 headless 行在轮末才写，而不是在注入时。要核对这是否符合 WP2 B7/B8 的「已注入」定义；P6（经 authority 时 `fetch_pending_admitted` 再问一次 `admit_dispatch`，不得重复占 W）在真 gate 下核对。
3. WP2 绑定后补 K6 的同一性断言，执行 R5、R6（strict xfail 会因此变成 XPASS 并报红，届时去掉 xfail）。
4. 第三节 S1–S3 的修法可以在 WP3-B 或 WP8 收尾时顺手做，或另立小单；S2 应在真 gate 可能抛异常之前处理。
5. application/__init__.py 998 行，后续包在此加行前先拆分。
6. PR 正文写 Contract-Impact（第七节）、Skill-Impact、§1.4 超预算实测数（裁定 75），列出豁免 61→64 的变化与理由。
7. login/device t11 仍归 WP8 收尾清单（裁定 70 后的记录）。

## 七、交 WP8 的 Contract-Impact（承接第一次验收第七节，加启动扫描）

- 新 IPC `session.turn.started`（`actor`、`sessionRef`、`reportId` ≤128、可选 `deliveryId`；返回 `{ok, startedAtMs}`）；`session.turn.ended` 接受任何当前 interactive 会话，新增可选 `status`（completed/failed）、`deliveryId`，返回体新增 `endedAtMs`；两者按 `(actor, sessionRef, reportId)` 幂等，时刻由 daemon 首次接受时固定；载体自报的 seq、source、时刻都不读。同 actor 换代时，旧 sessionRef 的去重记账被清除，旧报告仍被 fence 拒绝。
- 事件 `turn.started`/`turn.completed`/`turn.failed` 覆盖 interactive 载体：source `daemon.turns`，`fidelity: observed`，sender/conversation/prompt/output 为空串；所有 `turn.*` 在同一发布临界区内取 seq。
- `message.settle_hint` 对仍 pending 的 reply/event 行答 `ack`（裁定 56）；gate 的非法答案按 `keep` 处理，并记 `message.settle_hint.invalid`。
- headless 不再自动回复：前缀 `hyprial-agent-sdk-reply:` 消失；只保留两种机制应答，id 前缀为 `hyprial-turn-hook-reply:`、`hyprial-mechanism-reply:`，payload 带 `replyTo` 与 `origin.mechanism`（`turn-hook`/`python_worker`），并带 summary。user-proxy 的格式提示原文经 adapter `deliver_reply` 送达人。
- 启动结算扫描（裁定 74）：daemon 启动时，对已盖戳、无失败记录的 pending 行按 `settle_hint_for` 处理一次，ack 则 ack，keep 不动，模型不重跑，上限 256 行；扫描遇 inbox 超时时记日志事件 `harness.stranded_sweep_deferred`。
- 注入文本格式（§2.3）与 golden `tests/daemon/harnesses/carrier/injection_golden.json`；wire golden `WG-001` 的 `autoReply` 未改（交 WP8）。
- 日志事件：`harness.settle.deferred`、`harness.mechanism_answer.deferred` 取代 `harness.reply.deferred`，`harness.reply.already_answered` 删除，新增 `harness.stranded_sweep_deferred`。
- authority 豁免：61 → 64（`fetch_pending` 两条、`_confirm_mechanism_answer` 的 ack/submit 两条、`_settle_stranded_rows` 的 ack 一条；删除 `_reply_and_ack` 两条）。
