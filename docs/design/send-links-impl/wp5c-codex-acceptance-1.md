# WP5-C codex interactive 第一次验收

结论：ACCEPTED

- 分支 `wp5c-codex/send-links`，HEAD cd4397fea，base ba6ae30dc（WP5-A）。提交：585c65a1e（先红测试）→ 2370a2a04（实现）→ cd4397fea（证据）。
- 验收者：opus 角色（claude-opus-5-5），2026-10-15。工作树开始时干净；未推送、未发 hyprial 消息、未改 `links/**` 与 `ports.py`。

## 逐项

1. 硬切（wp5c §3.1、§3.4） PASS。`_settle_final` 的 reply 分支、`_settle_reply_unavailable`、`reply_message_id` import、`reply_lost` 事件、`item.get("message")` 全删；`"message.reply"` 在 `codex/settlement.py`、`codex/carrier.py` 中 0 处，`harnesses/codex/` 与 `harnesses/carrier/` 递归也 0 处。读代码确认：注入只经 `injection_from_row` → `render_injection`；TURN_STARTED 恢复只 `thread/read`，失败走 CarrierRemoved 后由 daemon 重新列出（新格式）；`_refill_staged_fetched` 排除 `injection_format IS NULL`；剩下的 `get("message")` 只出现在 client/app_server/native_env 读 RPC 错误文本处，与正文无关。
2. KEPT 状态机（§3.3，B2） PASS。`CarrierKept(generation, delivery_id)` 走 `_current` 的 generation 栅栏；`_kept` 在一个 actor 命令内完成 store 置 KEPT（`final_output=NULL`）、`_states` 弹出、投影行弹出；`CarrierSettled` 仍只做 ack 后清理。`load` 的跨 session 集合含 KEPT；`kept_ids(actor)` 不按 session；`_recover` 对 KEPT 只记日志后 continue；`_known_ids` 同时供 `_apply_inbox_list` 与 `_list_may_hold` 使用。KEPT 行只在 `ok is True` 且 `messages` 为 list、id 不在其中时删除；`_apply_inbox_list` 只接 ListInbox（`message.pending.list`）结果，`pending.wait` 结果在结构上到不了删除路径。
3. B3 升级前行（§3.5） PASS。新增 `injection_format` 列（旧行 NULL）；旧 FETCHED 不进恢复投影、不 refill，由 fetched 列表按 metadata 重建（`record_fetched` 的 UPDATE 写回 summary 文本与格式）；列表成功且不含该 id 时删除；metadata 拿不到时保持 FETCHED、不起 turn。TURN_STARTED 只查状态；FINAL_OBSERVED 只按 hint 结算，不提交保存的最终文本；REMOTE_SETTLED 行为不变。X6 通过。
4. turn 报告（§3.2，B7） PASS（有偏差，见下）。`codex_turn_report` 表主键 (actor, report_id, phase)；按 `created_ms`、started 先于 ended、rowid 排序发送；失败即停止本轮（同一 reportId 的 ended 不会先于 started），指数退避，上限 5 s；SESSION_SUPERSEDED/STALE_SESSION/INVALID_ARGUMENT 删行并记 `worker.carrier.error` stage `turn-report`；结算不等报告确认；进程重启后从表中恢复（X7 与探针 P4 证实）。`endedAtMs` 首次接受时固定属 daemon 侧（WP5-A `_record_carrier_turn_ended`），载体以同一 reportId 重发，满足要求。`carrier/codex_carrier_support.py` 判定：codex 专用（import `CodexCarrierStore`，只服务 codex），但不破坏中立契约。`contract.py` 未改，也不 import 它；新文件里没有 runtime 名；G1/G2/G7（test_carrier_boundaries）与模块边界门都通过；同目录已有 codex 专用的 `codex_carrier_store.py` 先例；`harnesses/codex/` 已有 7 个直接文件，放不进去。因此不判 FAIL，记作偏差 D1。
5. R4 三个断点（§5） PASS。X8：写 KEPT 后 actor 清理前崩溃（monkeypatch 让 record_kept 写入后抛错）、carrier 进程重启、同 actor 换 sessionRef。每个断点之后消息仍 pending，没有 reply、ack，也没有新 turn；模型自行 reply/ack（行从成功列表中消失）后 KEPT 行被清。探针 P7（另一 session 留下的 KEPT 行 + 新消息）：只为新消息起一个 turn。
6. 测试 PASS。X1–X9、G3c、第 12 条 `test_carrier_kept_command_is_generation_fenced` 均在且全绿。先红：把最终版测试文件放进 ba6ae30dc 的临时 worktree 复跑，结果 11 failed、2 teardown errors（fd 泄漏、app-server 未停），见 `red-final` 复现。X1–X5、X7、G3c 为 AssertionError；X6 为 AssertionError（注入含 sentinel）；X8、X9、第 12 条为 AttributeError/ImportError（base 没有 `record_kept`/`CarrierKept`），与规格写的「AssertionError」不同，记偏差 D3。worker 自报的红是在测试文件后改之前跑的（11 红 + 4 teardown），后改只动了第 12 条的同步方式（drain → wait_for）和假 server 的 `interrupt_turn`，断言没有放松。改写的现有测试：删掉的 3 条只测 reply 路径（reply 已删）；新增 `test_settle_hint_failure_keeps_retrying_without_replying` 替代「行仍在、保留并重试」；ack-unavailable 终结/重试、瞬时错误不终结、毒行隔离、1024 毒行收敛这些守卫都保留，只把 reply 换成 settle_hint+ack。`tests/shell/cli/agents/codex/resume` 已复跑（test_reviewed_matrix 12 passed）。
7. 运行 PASS（带已知红）。
   - 聚焦组（harnesses、inbox_watch、ipc、delivery、shell/cli/agents、integration/native_harnesses、kernel/timers）：2 failed、1648 passed、1 skipped、2 xfailed（500 s）。失败的两条是已知红：pi controlled_loader t04–t06、ipc heartbeat ratio（0.67 < 0.70，机器共享负载）。
   - `tests/daemon` 全量跑完一次：2 failed、6680 passed、5 skipped、7 xfailed、1 xpassed（1059 s），失败的仍是上面两条已知红；kernel POSIX listener fd、wait_without_polling 本次没有红。worker 证据里记为 BLOCKED 的全量，现由本次补齐。
   - 结构门 violations []；模块边界门 violations []；`ruff check .` 通过；`git diff --check ba6ae30dc HEAD -- ':!docs/evidence'` 干净；wp5c §7 第 3 条中 codex 相关 grep 与禁区 diff 均无输出；`class CarrierKept` 1 行；store 中 KEPT ≥ 2 行。
   - 行数：settlement.py 496/520（证据写 497，差 1 行，无碍）、carrier.py 727/760、codex_carrier_store.py 420/420、interactive_carrier_runtime.py 573/640、codex_carrier_support.py 131（新）；`harnesses/carrier/` 5 个直接文件（规格写的是「不变 = 4」，见 D1）；`harnesses/codex/` 7 个。
8. 合并就绪 PASS。`git merge-tree --write-tree` 对集成头 `refactor/org-structure-constrain`（现为 abc83dd1c，比任务所说的 4c2ff4c59 多一个 docs 提交；两者都试过）、`wp5c-pi/send-links` d8caa3abb、`wp5c-claude/send-links` 34b193ded 都无冲突。三个载体按文件比对没有重叠，自 base 以来与集成分支也没有重叠。
9. Contract-Impact（交 WP8） 已列，见下文「集成者合并时要做」。
10. 真实 codex TUI / app-server 联测：BLOCKED-on-WP8（WP5-B 的 MCP 面尚未合入，交互线程里的模型还不能 reply/read_body；证据 blocked.txt）。本分支不含 WP5-B 内容（capabilities.py、mcp/ 无 diff），符合裁定 57/65。

## 探针（scratchpad，未提交；7 条全过）

- P1 `_forget_gone_ids`：无 `ok`、`ok:false`、`ok:"true"`、`messages` 非数组或为 None、wait 形状 `{held, changed}`、`events` 形状，KEPT 行都保留；成功列表含该 id 保留，成功列表不含才删除。
- P2 旧 FETCHED 不在成功列表中：行被删，没有 prompt；旧 TURN_STARTED 不重发；没有 reply，旧最终文本不上线。
- P3 旧 FETCHED 在列表中但 summary 为 None：只起 1 个 turn，prompt 含 `(none;`，不含 sentinel。
- P4 started 与 ended 首发都失败后重启：从表中恢复，发送顺序为 started → ended。
- P5 started 一直失败时，ended 不会先发出；恢复后两条都被确认删除。
- P6 INVALID_ARGUMENT 时报告被丢弃；另一 sessionRef 的残留行在构造时直接删除，不发送。
- P7 另一 session 的 KEPT 行在新 sessionRef 下不重取、不起 turn。

## 发现与接受的偏差

- D1 `codex_carrier_support.py` 放在 `harnesses/carrier/`，与 wp5c §1「目录直接文件数不变」不符（4 → 5，≤ 7 门内）。接受，理由见第 4 项。若集成者要求严格目录归属，可在 WP8 前把 codex 专用的 store/support 一起移入 `harnesses/codex/` 的子包（例如与 `projection/` 同级），不属本包返工。
- D2 `codex_carrier_store.py` 恰好 420/420，是把两段未改动的多行 SQL（`record_remote_settled`、`settle_and_delete`）压成单行字符串换来的（约省 6 行）；同时 import 后多出一行空行。按裁定 75，行数预算不是门，但不应靠压缩其它代码凑数。接受，不返工；集成者把 wp5c §1 的 store 实测改记为约 426（展开后），或在后续触碰时恢复原排版。
- D3 X8、X9、第 12 条的先红原因是 AttributeError/ImportError，而规格写的是 AssertionError。base 上缺少对应 API，红因是真实的。接受。
- D4 报告在 INVALID_ARGUMENT 时丢弃（任务说明允许）。但 daemon 用同一个码表示「actor 当前没有 interactive session」（turns.py `_require_current_interactive_session`）。所以如果注册暂时失效时 flush 了报告，这条报告会被永久丢弃，而不是重试。目前 poll 步先做 heartbeat/注册再 flush，风险很低；建议 WP8 让 daemon 给这种情况一个专用码（例如 STALE_SESSION），载体侧不必改。
- D5 第 12 条测试在「过期 generation 被丢弃」这一负向断言上靠 `time.sleep(0.2)` 等待，属于弱断言；正向断言（真 generation 生效、final_output 清空、投影释放）是强的。接受。
- D6 删除 KEPT 依据的是 `message.pending.list`，而它经过 link gate 的 `admitted_pending`。理论上，如果一条已取过的行被 gate 暂时不列出，KEPT 记录会被误删，下次列出时会重新注入一次。这是规格本身的定义（「权威列表不含即删」），不算实现缺陷，记为 UNKNOWN 交 WP8 联测（裁定 70 要求已结算 id 不占 W；未结算的 kept 行是否始终被列出，没有测到）。

## 集成者合并时要做

1. Contract-Impact（WP8 写入 contract/ 或 PR 正文）：
   - 载体本地 sqlite 新增 `codex_turn_report` 表；`codex_interactive_delivery` 新增 `injection_format` 列（NULL 表示升级前的行）。
   - 新增状态 KEPT（跨 session 继承，只在成功列表不含该 id 时删除）。
   - 删除事件 `worker.carrier.settlement.reply_lost`。
   - `worker.carrier.settlement.superseded` 去掉 `replyMessageId`/`replyStatus` 两个字段；`settlement.pending` 去掉 `replied`。
   - 新增事件 `worker.carrier.settlement.kept`；`worker.carrier.error` 新增 stage `turn-report`、`turn-report-retry`；`worker.carrier.recovered` 新增 `recoveredState: KEPT`；`message.settle_hint` 成为 codex 的结算方法名（stage）。
   - 在 contract/ 中没有找到对已删字段的引用。
2. wp5c §1 把预算表和目录文件数更新为实测：`harnesses/carrier/` 5 个文件，新增 support 131 行，store 按 D2 处理。
3. PR 正文：真实 codex TUI 联测写 BLOCKED-on-WP8；第 7 项的两条已知红照抄分类。
4. WP5-B 合入后补跑交互 codex 端到端：summary 注入 → 模型 `harness_reply`/`harness_ack` → KEPT 行被清。
5. 可选（WP8）：D4 的 daemon 专用码；D6 的 gate 列出性联测。
